"""第 4 层隔离：现场拼评分树 + 越界检测 + 分组部分分（设计文档 §4.2 / §4.4 / §5）。

评分树（模型不可改，来自原始快照骨架）：
    pyproject.toml  backend/  tests/  scripts/  .gitignore  package.json  frontend/
    node_modules/（从受测仓库复制到评分树的实体目录，仅前端题）
  + 沙箱 overlay（仅 allowed_paths 内的文件，含模型删掉的文件）
  + hidden/ 隐藏测试（此刻才出现）

改测试、改 pytest/vitest 配置、塞 conftest 全部无效：它们不在 allowed_paths 里，
压根不会进评分树。
"""

from __future__ import annotations

import difflib
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional

from . import checks, errors, packs, sandbox, util
from .checks.pytest import make_resolver as make_pytest_resolver
from .checks.vitest import make_resolver as make_vitest_resolver

Log = Callable[[str], None]

#: 评分树在沙箱根下的暂存目录前缀
GRADE_DIR_PREFIX = "_grade"

#: 越界检测第 3 项：这些文件即使被改也不影响评分树，但要记违规
FORBIDDEN_CHANGE_PATTERNS = [
    "**/conftest.py", "**/pytest.ini", "**/tox.ini", "**/setup.cfg",
    "**/vitest.config.*", "**/vite.config.*", "**/jest.config.*",
    "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/poetry.lock", "**/requirements-release.lock.txt", "**/uv.lock",
    "pyproject.toml", "package.json", "**/package.json",
]

#: 运行产物：模型跑测试留下的噪音，单独归到提示里，不当越界
NOISE_GLOBS = [
    "**/__pycache__/**", "**/*.pyc", "**/*.pyo", "**/.pytest_cache/**",
    "**/*.egg-info/**", "**/.coverage", "**/coverage.xml", "**/htmlcov/**",
    "**/*.log", "**/*.tmp", "**/.DS_Store",
]

#: 默认的逻辑行数上限（与 config.grade.diff_line_cap 一致，缺项时兜底）
DEFAULT_DIFF_LINE_CAP = 4000


def _noop(_msg: str) -> None:
    """默认日志回调。"""


# --------------------------------------------------------------------------
# 统一环境注入（设计文档 §4.3 末条）
# --------------------------------------------------------------------------

def build_env(cfg: dict, workdir: str) -> dict:
    """构造子进程环境：禁网代理、禁字节码、锁哈希种子、清掉外部干扰。"""
    env = os.environ.copy()
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"          # 锁死，保证同样输入同样输出
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 外部 shell 里的 PYTHONPATH / PYTEST_ADDOPTS 会污染评分，一律清掉
    for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "COV_CORE_SOURCE"):
        env.pop(key, None)
    env["GRADE_WORKDIR"] = workdir
    return env


# --------------------------------------------------------------------------
# 越界检测（设计文档 §4.4 六项）
# --------------------------------------------------------------------------

def load_baseline_manifest(run: dict) -> dict:
    """读准备沙箱时登记的基线全树清单。"""
    run_dir = run.get("run_dir")
    if run_dir:
        cached = util.read_json(os.path.join(run_dir, "baseline_manifest.json"), default=None)
        if isinstance(cached, dict):
            return cached
    return run.get("baseline_manifest") or {}


def collect_changes(cfg: dict, run: dict) -> dict:
    """全树哈希比对：沙箱现状 vs 准备时登记的基线清单。"""
    baseline = load_baseline_manifest(run)
    current = util.tree_manifest(run["sandbox"])
    return util.manifest_diff(baseline, current)


#: 注释行前缀：越界文件若只动了这些行，按提示处理而不是作废整轮
COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--")


def _diff_lines_by_file(diff_text: str) -> Dict[str, List[str]]:
    """把合并后的 unified diff 拆成「每个文件的增删正文行」。"""
    per_file: Dict[str, List[str]] = {}
    current = ""
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):].strip()
            per_file.setdefault(current, [])
            continue
        if not current or not line or line[0] not in "+-":
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        per_file[current].append(line[1:].strip())
    return per_file


def _comment_only(lines: List[str]) -> bool:
    """这一组改动行是否只有注释与空行。拿不准就算代码——宁可判红。"""
    if not lines:
        return False
    return all((not body) or body.startswith(COMMENT_PREFIXES) for body in lines)


def _classify_violations(cfg: dict, changes: dict, allowed: list, noise: list,
                         diff_text: str = "") -> tuple:
    """把越界改动分成「硬违规」「只提示」两类。

    硬违规 → 本轮判红（设计文档 §4.2：allowed_paths 之外任何变化记 violations，命中即红）；
    运行噪音（__pycache__、.log 之类）与「越界但只改了注释」→ 只提示，不影响判分。
    后者是为了不把测量变成惩罚：模型在无权文件里改一行注释，既不进入评分树
    （`_apply_overlay` 只收 allowed_paths），也不该抹掉它其余的正确改动。
    """
    hard, soft = [], []
    diff_lines = _diff_lines_by_file(diff_text)
    for kind in ("added", "modified", "removed"):
        for rel in changes[kind]:
            if packs.allowed_match(rel, allowed):
                continue
            if util.match_any(rel, noise):
                soft.append({"path": rel, "change": kind, "reason": "运行产物"})
                continue
            if kind == "modified" and _comment_only(diff_lines.get(rel, [])):
                soft.append({"path": rel, "change": kind, "reason": "越界但只改了注释，未进入评分树"})
                continue
            reason = "改动了无权修改的文件"
            if util.match_any(rel, FORBIDDEN_CHANGE_PATTERNS):
                reason = "改动了测试/构建配置（评分树用原始副本，改了也没用）"
            elif kind == "added":
                reason = "新增了不在允许范围内的文件"
            hard.append({"path": rel, "change": kind, "reason": reason})
    return hard, soft


def check_baseline_intact(run: dict, log: Log = _noop) -> list:
    """第 1 项：基线引用没被模型改写（否则 diff 就失去意义了）。"""
    problems = []
    sandbox_path = run.get("sandbox")
    registered = run.get("baseline_commit")
    if not registered:
        return [{"kind": "baseline_missing", "message": "沙箱没有登记基线，请重建沙箱。"}]
    current = util.git(sandbox_path, "rev-parse", "baseline", timeout=30).stdout.strip()
    if not current:
        problems.append({"kind": "baseline_lost",
                         "message": "沙箱里的 baseline 引用已丢失，请重建沙箱。"})
    elif current != registered:
        problems.append({"kind": "baseline_rewritten",
                         "message": "沙箱的基线提交被改写了，本轮成绩不可信。请重建沙箱。"})
    return problems


def build_diff_text(cfg: dict, run: dict, changes: dict, limit: int | None = None) -> dict:
    """生成 unified diff 存档，并统计逻辑增删行数。

    不用 `git diff`：模型可以改 .gitignore 让 git 闭嘴，全树比对更诚实。
    原始内容从沙箱自己的 baseline 提交里取（`git show baseline:<路径>`），不额外占磁盘。
    """
    sandbox_path = run["sandbox"]
    chunks: List[str] = []
    added_lines = 0
    removed_lines = 0
    oversized: List[str] = []
    files = changes["changed"]
    if limit:
        files = files[:limit]
    for rel in files:
        current_path = os.path.join(sandbox_path, *rel.split("/"))
        try:
            if os.path.getsize(current_path) > util.MAX_READ_BYTES:
                oversized.append(rel)
                continue
            with open(current_path, "rb") as fh:
                new_raw = fh.read()
            new_text = util.decode_output(new_raw)
        except OSError:
            new_text = ""
        old_result = util.git(sandbox_path, "show", "baseline:%s" % rel, timeout=30)
        old_text = old_result.stdout if old_result.ok else ""
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        if old_lines == new_lines:
            continue
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile="a/%s" % rel, tofile="b/%s" % rel, n=3,
        ))
        if not diff:
            continue
        added_lines += sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed_lines += sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        chunks.append("".join(diff))
    text = "".join(chunks)
    return {
        "text": text,
        "files": files,
        "oversized": oversized,
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "changed_lines": added_lines + removed_lines,
    }


# --------------------------------------------------------------------------
# 相似度标记（设计文档 §4.2 第 3 层 / §4.4 第 5 项）
# --------------------------------------------------------------------------

def _normalized_lines(diff_text: str) -> List[str]:
    """把 diff 里的增删行归一化后取出，用来比内容不比格式。"""
    out = []
    for line in diff_text.splitlines():
        if not line or line[0] not in "+-":
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        body = line[1:].strip()
        if body:
            out.append(body)
    return out


def similarity_vs_reference(meta: dict, model_diff: str,
                            threshold: float = 0.6) -> dict:
    """模型 diff 与参考解/历史修复的相似度；超阈值打「疑似抄历史」标记。"""
    refs = packs.reference_patches(meta)
    if not model_diff.strip() or not refs:
        return {"checked": len(refs), "flagged": False, "max_ratio": 0.0, "matches": []}

    model_lines = _normalized_lines(model_diff)
    model_set = set(model_lines)
    matches = []
    for path in refs:
        try:
            with open(path, "rb") as fh:
                ref_text = util.decode_output(fh.read())
        except OSError:
            continue
        ref_lines = _normalized_lines(ref_text)
        ref_set = set(ref_lines)
        union = model_set | ref_set
        overlap = len(model_set & ref_set) / len(union) if union else 0.0
        ratio = difflib.SequenceMatcher(
            None, model_lines, ref_lines, autojunk=False).ratio() if ref_lines else 0.0
        score = max(overlap, ratio)
        matches.append({
            "reference": os.path.basename(path),
            "overlap": round(overlap, 3),
            "ratio": round(ratio, 3),
            "score": round(score, 3),
            "flagged": score >= threshold,
        })
    matches.sort(key=lambda m: m["score"], reverse=True)
    return {
        "checked": len(matches),
        "threshold": threshold,
        "flagged": any(m["flagged"] for m in matches),
        "max_ratio": matches[0]["score"] if matches else 0.0,
        "matches": matches,
    }


# --------------------------------------------------------------------------
# 评分树拼装
# --------------------------------------------------------------------------

def _copy_skeleton(cfg: dict, grade_dir: str, meta: dict, log: Log) -> None:
    """铺不可改的原始层：与沙箱同源的基线骨架（已含题目注入）。"""
    skeleton = sandbox.skeleton_for(cfg, meta, log)
    root = skeleton["path"]
    util.remove_tree(grade_dir)
    util.ensure_dir(grade_dir)
    count = 0
    for path in util.iter_files(root):
        name = os.path.basename(path)
        if name in {".keep", "_snapshot.json"}:
            continue
        rel = util.rel_posix(path, root)
        util.copy_file(path, os.path.join(grade_dir, *rel.split("/")))
        count += 1
    log("评分树骨架：%d 个原始文件（模型无权修改）" % count)


def _apply_overlay(grade_dir: str, sandbox_path: str, allowed: list,
                   changes: dict, log: Log) -> dict:
    """把沙箱里 allowed_paths 内的产物盖到评分树上。

    只收 allowed_paths：模型改的 tests/、pyproject.toml、conftest 全部落不到评分树上。
    模型在允许范围内删掉的文件也要跟着删，否则原始副本会把删除抹掉。
    """
    overlaid, removed = [], []
    for kind in ("added", "modified"):
        for rel in changes[kind]:
            if not packs.allowed_match(rel, allowed):
                continue
            src = os.path.join(sandbox_path, *rel.split("/"))
            dst = os.path.join(grade_dir, *rel.split("/"))
            if not util.path_within(grade_dir, dst) or not os.path.isfile(src):
                continue
            if os.path.isdir(dst) and not os.path.isfile(dst):
                util.remove_tree(dst)
            util.copy_file(src, dst)
            overlaid.append(rel)
    for rel in changes["removed"]:
        if not packs.allowed_match(rel, allowed):
            continue
        dst = os.path.join(grade_dir, *rel.split("/"))
        if util.path_within(grade_dir, dst) and os.path.isfile(dst):
            os.remove(dst)
            removed.append(rel)
    log("评分树叠加：收 %d 个文件、传播 %d 处删除" % (len(overlaid), len(removed)))
    return {"overlaid": sorted(set(overlaid)), "deletions": sorted(set(removed))}


def _overlay_hidden(grade_dir: str, overlay_src: str, overlay_rel: str, log: Log) -> int:
    """隐藏测试此刻才进评分树（保持题包里 hidden/ 这一层的原布局）。"""
    target = os.path.join(grade_dir, *overlay_rel.split("/"))
    count = util.copy_tree(overlay_src, target)
    log("叠加隐藏测试：%d 个文件 → %s/" % (count, overlay_rel))
    return count


def build_grade_tree(cfg: dict, run: dict, meta: dict, changes: dict,
                     log: Log = _noop) -> str:
    """拼出评分树目录并返回其绝对路径。"""
    grade_dir = os.path.join(cfg["sandbox_root"], GRADE_DIR_PREFIX, util.sanitize_id(run["run_id"]))
    if not util.path_within(cfg["sandbox_root"], grade_dir):
        raise errors.HarnessError(errors.E_INTERNAL, "评分树路径越界，已中止。", grade_dir)
    _copy_skeleton(cfg, grade_dir, meta, log)

    if sandbox.needs_frontend(meta):
        dependency_baseline = sandbox.node_modules_baseline(run)
        if not dependency_baseline:
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "评分树缺少 node_modules 本地基线，无法运行前端校验。",
            )
        sandbox.copy_node_modules(dependency_baseline, grade_dir, log)

    _apply_overlay(grade_dir, run["sandbox"], meta["allowed_paths"], changes, log)
    overlaid_layers = set()
    for spec in (meta.get("checks") or [{}]):
        try:
            hidden = packs.load_hidden_for(meta, spec)
        except errors.HarnessError:
            continue
        if hidden["overlay_rel"] in overlaid_layers:
            continue          # 多条 check 共用同一层 hidden，只搬一次
        overlaid_layers.add(hidden["overlay_rel"])
        # 目的地由 checker 决定：vitest 的 root 是 frontend/，隐藏用例必须落进
        # frontend/src/ 才会被 `src/tests_hidden_fe/…` 命中（放在树根它永远看不见）。
        _overlay_hidden(grade_dir, hidden["overlay_src"],
                        hidden.get("overlay_dest_rel") or hidden["overlay_rel"], log)
    util.ensure_dir(os.path.join(grade_dir, ".grade-cache"))
    return grade_dir


# --------------------------------------------------------------------------
# 分组计分（设计文档 §5.1）
# --------------------------------------------------------------------------

def _grade_groups(groups: list, p2p_tests: list, resolve_map: dict,
                  log: Log = _noop) -> dict:
    """把用例结果按组归拢，算出每组红绿与权重。"""
    resolved = []
    for group in groups:
        if group.get("mode") == "regression":
            continue
        resolver = resolve_map.get(str(group.get("kind") or "pytest"), make_pytest_resolver({}))
        cases = []
        for node_id in group["tests"]:
            info = resolver(node_id)
            if info is None:
                cases.append({"node_id": node_id, "outcome": "missing",
                              "message": "用例没有被收集到（隐藏测试可能导入失败）"})
            else:
                cases.append({
                    "node_id": node_id,
                    "outcome": info.outcome,
                    "duration": round(info.duration, 3),
                    "message": info.message,
                    "detail": info.detail[:4000],
                })
        passed = bool(cases) and all(c["outcome"] == "passed" for c in cases)
        resolved.append({
            "id": group["id"],
            "title": group.get("title") or group["id"],
            "weight": group["weight"],
            "passed": passed,
            "cases": cases,
            "total": len(cases),
            "passed_count": sum(1 for c in cases if c["outcome"] == "passed"),
        })

    regression = []
    for group in groups:
        if group.get("mode") != "regression":
            continue
        tests = group.get("tests") or p2p_tests
        resolver = resolve_map.get(str(group.get("kind") or "pytest"), make_pytest_resolver({}))
        for node_id in tests:
            info = resolver(node_id)
            if info is not None and not info.passed:
                regression.append({
                    "node_id": node_id,
                    "outcome": info.outcome,
                    "message": info.message,
                })
    return {"groups": resolved, "regressions": regression}


def compute_score(graded: dict) -> dict:
    """score = 100 × Σ通过组权重 / Σ总权重（设计文档 §5.1）。"""
    groups = graded["groups"]
    total_weight = sum(g["weight"] for g in groups) or 0.0
    passed_weight = sum(g["weight"] for g in groups if g["passed"])
    ratio = (passed_weight / total_weight) if total_weight else 0.0
    score = round(100.0 * ratio, 1)
    p2p_broken = bool(graded["regressions"])
    return {
        "score": 0.0 if p2p_broken else score,
        "raw_score": score,
        "total_weight": total_weight,
        "passed_weight": passed_weight,
        "p2p_broken": p2p_broken,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def run_grade(cfg: dict, run: dict, meta: dict, log: Log = _noop) -> dict:
    """跑一轮校验，返回报告字典（不抛异常，失败也出报告）。"""
    started = time.time()
    budget = meta.get("budget") or {}
    timeout_s = int(budget.get("grade_timeout_s") or cfg["timeouts"]["grade_default_s"])
    timeout_s = max(10, min(timeout_s, int(cfg["timeouts"].get("grade_max_s", 1800))))
    cap = int(budget.get("diff_line_cap") or cfg["grade"].get("diff_line_cap", DEFAULT_DIFF_LINE_CAP))

    # ① 沙箱完整性（§4.4 第 6 项）——坏了就别浪费时间，直接中止
    integrity = sandbox.verify_integrity(cfg, run, meta)
    if integrity:
        log("沙箱完整性自检未通过，已中止校验")
        return _aborted_report(cfg, run, meta, integrity, time.time() - started)

    # ② 全树哈希 diff + 越界检测
    log("开始全树哈希比对，统计模型改动")
    changes = collect_changes(cfg, run)
    diff = build_diff_text(cfg, run, changes)
    # 先出 diff 正文再判越界：注释级改动要靠改动行本身区分，不能只看路径就作废整轮
    violations, noise = _classify_violations(
        cfg, changes, meta["allowed_paths"], NOISE_GLOBS, diff["text"])
    baseline_problems = check_baseline_intact(run, log)
    log("改动文件 %d 个；越界 %d 项；提示 %d 项"
        % (len(changes["changed"]), len(violations), len(noise)))

    over_cap = diff["changed_lines"] > cap
    log("diff 统计：+%d / -%d 行（上限 %d%s）"
        % (diff["added_lines"], diff["removed_lines"], cap, "，已超限" if over_cap else ""))

    similarity = similarity_vs_reference(
        meta, diff["text"], float(cfg["grade"].get("similarity_threshold", 0.6)))
    if similarity.get("flagged"):
        log("相似度标记：与 %s 的相似度 %.2f，疑似抄历史"
            % (similarity["matches"][0]["reference"], similarity["max_ratio"]))

    # ③ 拼评分树
    grade_dir = build_grade_tree(cfg, run, meta, changes, log)
    env = build_env(cfg, grade_dir)

    # ④ 跑 checkers
    graded, check_reports, run_error = _run_checks(cfg, meta, grade_dir, env, timeout_s, log)
    duration = time.time() - started

    # ⑤ 判分
    scoring = compute_score(graded)
    invalid = bool(violations) or scoring["p2p_broken"]
    if violations:
        scoring["score"] = 0.0

    return {
        "run_id": run["run_id"],
        "task": run["task"],
        "model": run["model"],
        "attempt": run.get("attempt", 1),
        "graded_at": util.iso_now(),
        "duration_s": round(duration, 1),
        "score": scoring["score"],
        "raw_score": scoring["raw_score"],
        "total_weight": scoring["total_weight"],
        "passed_weight": scoring["passed_weight"],
        "passed": scoring["score"] >= 100.0 and not violations,
        "p2p_broken": scoring["p2p_broken"],
        "regressions": graded["regressions"],
        "groups": graded["groups"],
        "violations": violations,
        "noise": noise,
        "similarity": similarity,
        "diff": {
            "files": diff["files"],
            "added_lines": diff["added_lines"],
            "removed_lines": diff["removed_lines"],
            "changed_lines": diff["changed_lines"],
            "line_cap": cap,
            "over_cap": over_cap,
        },
        "baseline_problems": baseline_problems,
        "checks": check_reports,
        "grade_dir": grade_dir,
        "error": run_error,
        "invalidated": invalid,
        "invalid_reason": _invalid_reason(violations, graded["regressions"], run_error),
        # 下划线开头的字段是内部中转数据，不进 report.json
        "_diff_text": diff["text"],
    }


def _invalid_reason(violations: list, regressions: list, run_error: str) -> str:
    if violations:
        return "改动越界（%d 项），本轮作废" % len(violations)
    if regressions:
        return "破坏了既有通过用例（%d 条），本轮作废" % len(regressions)
    if run_error:
        return "校验过程出错，本轮作废"
    return ""


def _aborted_report(cfg: dict, run: dict, meta: dict, integrity: list, duration: float) -> dict:
    return {
        "run_id": run["run_id"], "task": run["task"], "model": run["model"],
        "attempt": run.get("attempt", 1), "graded_at": util.iso_now(),
        "duration_s": round(duration, 1), "score": 0.0, "raw_score": 0.0,
        "total_weight": 0.0, "passed_weight": 0.0, "passed": False,
        "p2p_broken": False, "regressions": [], "groups": [],
        "violations": [], "noise": [],
        "similarity": {"checked": 0, "flagged": False, "max_ratio": 0.0, "matches": []},
        "diff": {"files": [], "added_lines": 0, "removed_lines": 0,
                 "changed_lines": 0, "line_cap": 0, "over_cap": False},
        "baseline_problems": [], "checks": [],
        "grade_dir": "", "error": "sandbox_broken", "invalidated": True,
        "invalid_reason": "沙箱环境损坏：" + "；".join(p["message"] for p in integrity),
        "integrity": integrity,
    }


def _run_checks(cfg: dict, meta: dict, grade_dir: str, env: dict, timeout_s: int,
                log: Log) -> tuple:
    """按 meta.checks 逐个跑 checker，返回 (分组结果, 报告摘要, 错误信息)。"""
    check_reports: list = []
    resolve_map: Dict[str, Callable] = {}
    all_groups: list = []
    all_p2p: list = []
    run_error = ""

    specs = meta.get("checks") or [{"kind": "pytest"}]
    for spec in specs:
        kind = str(spec.get("kind") or "pytest").lower()
        try:
            hidden = packs.load_hidden_for(meta, spec)
        except errors.HarnessError as exc:
            run_error = exc.message
            log("任务包问题：%s" % exc.message)
            break

        checker = checks.get(kind)
        if checker is None:
            run_error = "未注册的 checker 类型：%s" % kind
            log(run_error)
            break

        groups = [dict(g, kind=kind) for g in hidden["groups"]]
        p2p_tests = hidden["p2p_tests"]
        all_groups.extend(groups)
        all_p2p.extend(p2p_tests)
        node_ids = [t for g in groups if g.get("mode") != "regression" for t in g["tests"]]
        node_ids += p2p_tests

        try:
            ctx = checks.CheckContext(
                workdir=grade_dir, spec=spec, kind=kind, node_ids=node_ids,
                timeout_s=timeout_s, env=env, log=log,
                batch=0, batch_total=1,
                tmp_dir=os.path.join(grade_dir, ".grade-cache"),
            )
            outcome = checker(ctx)
        except Exception as exc:  # noqa: BLE001 - checker 崩了也要出报告
            run_error = "checker %s 执行失败：%s" % (kind, exc)
            log(run_error)
            break

        if outcome.timed_out:
            run_error = "checker %s 超过 %d 秒被中止" % (kind, timeout_s)
            log(run_error)

        check_reports.append({
            "kind": outcome.kind,
            "command": outcome.command,
            "returncode": outcome.returncode,
            "duration_s": round(outcome.duration_s, 1),
            "timed_out": outcome.timed_out,
            "summary": outcome.summary_line(),
            "notes": outcome.notes,
            "log_tail": (outcome.stdout + "\n" + outcome.stderr)[-8000:],
        })
        # 后注册的 checker 覆盖先注册的（同一 kind 只保留最后一次）
        resolve_map[kind] = _make_resolver(kind, outcome.cases)

    graded = _grade_groups(all_groups, all_p2p, resolve_map, log)
    return graded, check_reports, run_error


def _make_resolver(kind: str, cases: dict) -> Callable:
    """按 checker 类型选合适的用例查找器。"""
    if kind == "vitest":
        return make_vitest_resolver(cases)
    if kind == "pytest":
        return make_pytest_resolver(cases)
    return lambda node_id: cases.get(node_id)
