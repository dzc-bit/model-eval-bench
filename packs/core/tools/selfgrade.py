"""出题侧自验门禁：临时拼"评分树"跑 pytest，按 hidden/groups.json 出分组得分。

这不是通用 harness（通用实现由 ``console/harness/`` 负责，本脚本不碰也**不得**
被写成第二套 harness）。它只做一件事：让出题人能在**不依赖 console/ 的前提下**
把设计文档 §5.3 的四项门禁跑出来——

1. 锚解：应用 ``reference/fix.patch`` 后隐藏组全绿；
2. 半成品：``reference/partial.patch``（只修一个出口）得分 < 100；
3. 注入态：不修复时计分组全红，重复跑 N 次稳定不 flaky；
4. 参考解不触碰 ``forbidden_paths``；
5. p2p 白名单在参考解下保持全绿。

评分树骨架按 §5.4 拼：**原始快照（tests/ 已按 visible.prune 裁剪 + 配置 +
scripts/ + .gitignore）+ 注入 patch + 可选参考解 patch + hidden/ 隐藏测试**。
模型产物（沙箱里 allowed_paths 内的文件）在真 harness 里 overlay 进来；本脚本
用 ``--patch`` 直接模拟"模型改出来的 diff"，语义等价。

全部纯标准库（unified diff 自己解析，不调 ``git apply``，也不调 ``patch``），
因此换台机器就能跑。临时树默认落在 ``tools/_grade/<task>-<state>/``，跑完可留可删。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

# ---------------------------------------------------------------------------
# 一、最小 unified diff 应用器（够用即止：只处理本仓库会出现的补丁形态）
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _split_patch(patch_text: str) -> list[tuple[str, list[tuple[int, int, list[str]]]]]:
    """把 patch 拆成 ``[(相对路径, [(起始行, 删除数, 行列表)])]``。"""
    files: list[tuple[str, list[tuple[int, int, list[str]]]]] = []
    current: str | None = None
    hunks: list[tuple[int, int, list[str]]] = []
    lines = patch_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            if current is not None:
                files.append((current, hunks))
            current, hunks = None, []
        elif line.startswith("--- "):
            # 下一个 +++ 行给出目标路径
            pass
        elif line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if not match:
                raise ValueError(f"无法解析 hunk 头：{line!r}")
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) else 1
            body: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.startswith("@@") or candidate.startswith("diff --git ") or candidate.startswith("--- "):
                    index -= 1
                    break
                if candidate.startswith("\\"):  # \ No newline at end of file
                    index += 1
                    continue
                body.append(candidate)
                index += 1
            hunks.append((old_start, old_count, body))
        index += 1
    if current is not None:
        files.append((current, hunks))
    return files


def apply_patch(root: Path, patch_text: str) -> list[str]:
    """在 ``root`` 下应用 unified diff，返回被改动的相对路径列表。"""
    touched: list[str] = []
    for rel_path, hunks in _split_patch(patch_text):
        if not hunks:
            continue
        target = root / rel_path
        if not target.is_file():
            raise FileNotFoundError(f"补丁目标不存在：{rel_path}")
        original = target.read_text(encoding="utf-8").splitlines()
        output: list[str] = []
        cursor = 0  # original 中已消费到的下标
        for old_start, _old_count, body in hunks:
            start = old_start - 1
            if start < cursor:
                raise ValueError(f"{rel_path}: hunk 起点 {old_start} 早于上一段的结束位置")
            output.extend(original[cursor:start])
            cursor = start
            for line in body:
                marker, content = line[:1], line[1:]
                if marker == " ":
                    if original[cursor] != content:
                        raise ValueError(
                            f"{rel_path}:{cursor + 1} 上下文不匹配\n  期望 {content!r}\n  实际 {original[cursor]!r}"
                        )
                    output.append(content)
                    cursor += 1
                elif marker == "-":
                    if original[cursor] != content:
                        raise ValueError(
                            f"{rel_path}:{cursor + 1} 待删行不匹配\n  期望 {content!r}\n  实际 {original[cursor]!r}"
                        )
                    cursor += 1
                elif marker == "+":
                    output.append(content)
                else:
                    raise ValueError(f"{rel_path}: 未知 diff 行 {line!r}")
        output.extend(original[cursor:])
        target.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
        touched.append(rel_path)
    return touched


# ---------------------------------------------------------------------------
# 二、按 visible.prune 裁剪快照里的守卫测试
# ---------------------------------------------------------------------------


def _prune_test_file(path: Path, patterns: list[str]) -> int:
    """删掉匹配 ``patterns`` 的顶层 test 函数/类，返回删除数量。"""
    if not patterns:
        return 0
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    doomed: list[tuple[int, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        name = node.name
        if not name.startswith("test"):
            continue
        if not any(fnmatch(name, pattern) for pattern in patterns):
            continue
        # 装饰器要从最上面一行一起删。不能用"往上找以 @ 开头的行"——parametrize
        # 那种多行装饰器删掉函数体后会剩一个孤零零的右括号，整个文件当场语法错。
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        doomed.append((start, node.end_lineno or node.lineno))
    if not doomed:
        return 0
    kept: list[str] = []
    skip_until = -1
    for number, line in enumerate(lines):
        if number <= skip_until:
            continue
        for start, end in doomed:
            if number == start:
                skip_until = end  # end_lineno 是 1-based 末行，作 0-based 排他末行
                break
        else:
            kept.append(line)
    text = "".join(kept)
    try:
        ast.parse(text)
    except SyntaxError as error:  # 裁剪把文件改坏了属于出题事故，必须当场炸掉
        raise ValueError(f"裁剪 {path.name} 后语法错误：{error}") from error
    path.write_text(text, encoding="utf-8", newline="\n")
    return len(doomed)


def prune_visible_tests(tree_root: Path, prune_patterns: list[str]) -> int:
    """应用 ``visible.prune``，支持按用例或整文件裁剪。

    ``tests/test_x.py::test_name_*`` 删除匹配的顶层测试；没有 ``::`` 时
    删除整文件，与正式 snapshot 门禁的语义保持一致。
    """
    by_file: dict[str, list[str]] = {}
    for entry in prune_patterns:
        normalized = str(entry).replace("\\", "/")
        if "::" not in normalized:
            target = tree_root / normalized
            if not target.is_file():
                raise FileNotFoundError(f"visible.prune 指向不存在的测试文件：{normalized}")
            target.unlink()
            continue
        file_part, name_part = normalized.split("::", 1)
        by_file.setdefault(file_part, []).append(name_part)
    removed = 0
    for file_part, names in by_file.items():
        target = tree_root / file_part
        if not target.is_file():
            raise FileNotFoundError(f"visible.prune 指向不存在的测试文件：{file_part}")
        removed += _prune_test_file(target, names)
    return removed


# ---------------------------------------------------------------------------
# 三、拼树 + 跑 pytest + 分组计分
# ---------------------------------------------------------------------------

SNAPSHOT_PATHS = ["backend", "tests", "scripts", "pyproject.toml", ".gitignore"]


def build_tree(repo: Path, task_dir: Path, meta: dict, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for relative in SNAPSHOT_PATHS:
        source = repo / relative
        if not source.exists():
            continue
        target = dest / relative
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        else:
            shutil.copy2(source, target)
    prune_visible_tests(dest, meta.get("visible", {}).get("prune", []))
    hidden_src = task_dir / "hidden" / "tests_hidden"
    if hidden_src.is_dir():
        shutil.copytree(hidden_src, dest / "tests_hidden")
    # 评分树里禁写字节码：与 harness 环境一致，也避免污染对比。
    (dest / "pytest.ini").unlink(missing_ok=True)


def resolve_state_patches(task_dir: Path, meta: dict, state: str, extra: Path | None) -> list[Path]:
    patches = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    if state == "baseline":
        patches = []
    if state == "fixed":
        patches.append(task_dir / "reference" / "fix.patch")
    elif state == "partial":
        patches.append(task_dir / "reference" / "partial.patch")
    if extra is not None:
        patches.append(extra)
    missing = [str(path) for path in patches if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少补丁：" + ", ".join(missing))
    return patches


def run_pytest(tree: Path, node_files: list[str], timeout: int) -> tuple[dict[str, str], str]:
    report = tree / "report.xml"
    if report.exists():
        report.unlink()
    env_note = "PYTHONDONTWRITEBYTECODE=1"
    command = [
        sys.executable,
        "-m",
        "pytest",
        *node_files,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    import os

    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["NO_PROXY"] = "*"
    environment["PYTHONHASHSEED"] = "0"
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=tree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
    )
    elapsed = time.time() - started
    outcomes: dict[str, str] = {}
    if not report.is_file():
        return outcomes, f"未能生成 junit 报告（{env_note}）。stdout 尾部：\n{completed.stdout[-2000:]}"
    root = ET.parse(report).getroot()
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        file_part = classname.replace(".", "/")
        nodeid = f"{file_part}::{name}" if file_part else name
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
            elif child.tag == "skipped":
                status = "skipped"
        outcomes[nodeid] = status
    return outcomes, f"pytest 用时 {elapsed:.1f}s，收集 {len(outcomes)} 条结果"


def _match_outcome(node_id: str, outcomes: dict[str, str]) -> str | None:
    if node_id in outcomes:
        return outcomes[node_id]
    # JUnit removes `.py`, folds module separators into `/`, and places class
    # names in the classname path; pack IDs may also include a `hidden/` prefix.
    if "::" not in node_id:
        return None
    file_part, _, rest = node_id.partition("::")
    normalized_file = file_part.replace("\\", "/")
    if normalized_file.startswith("hidden/"):
        normalized_file = normalized_file[len("hidden/"):]
    if normalized_file.endswith(".py"):
        normalized_file = normalized_file[:-3]
    # pytest's JUnit writer stores class names in the classname path. Map
    # `file.py::TestCase::test_name` to `file/TestCase::test_name` exactly.
    rest_parts = rest.split("::")
    if len(rest_parts) > 1:
        expected = f"{normalized_file}/{'/'.join(rest_parts[:-1])}::{rest_parts[-1]}"
    else:
        expected = f"{normalized_file}::{rest}"
    normalized = {key.replace("\\", "/"): value for key, value in outcomes.items()}
    if expected in normalized:
        return normalized[expected]
    parametrised = [
        value for key, value in normalized.items()
        if key.startswith(expected + "[")
    ]
    if parametrised and len(set(parametrised)) == 1:
        return parametrised[0]
    return None


def grade(task_dir: Path, meta: dict, outcomes: dict[str, str]) -> dict:
    groups_path = task_dir / "hidden" / "groups.json"
    spec = json.loads(groups_path.read_text(encoding="utf-8"))
    p2p_path = task_dir / "p2p.json"
    p2p_spec = json.loads(p2p_path.read_text(encoding="utf-8")) if p2p_path.is_file() else {"tests": []}

    group_reports = []
    weight_total = 0
    weight_passed = 0
    for group in spec["groups"]:
        if group.get("mode") == "regression":
            continue
        tests = group.get("tests", [])
        weight = int(group.get("weight", 1))
        weight_total += weight
        missing = [node for node in tests if _match_outcome(node, outcomes) is None]
        failures = [node for node in tests if _match_outcome(node, outcomes) == "failed"]
        # 一个用例都没声明/都没收集到的组不能算通过：那正是「checker 压根没跑」的形状，
        # 曾经让前端隐藏用例从未执行过的题拿到 100。
        passed = bool(tests) and not missing and not failures
        weight_passed += weight if passed else 0
        group_reports.append(
            {
                "id": group["id"],
                "weight": weight,
                "passed": passed,
                "failed_tests": failures,
                "missing_tests": missing,
            }
        )

    p2p_failures = [node for node in p2p_spec.get("tests", []) if _match_outcome(node, outcomes) != "passed"]
    score = round(100.0 * weight_passed / weight_total, 2) if weight_total else 0.0
    if p2p_failures:
        score = 0.0
    return {
        "score": score,
        "weight_passed": weight_passed,
        "weight_total": weight_total,
        "groups": group_reports,
        "p2p_failures": p2p_failures,
        "p2p_total": len(p2p_spec.get("tests", [])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务包自验门禁")
    parser.add_argument("--task", required=True, help="任务 ID")
    parser.add_argument("--repo", required=True, help="受测仓库根（只读）")
    parser.add_argument(
        "--state",
        default="injected",
        choices=["baseline", "injected", "fixed", "partial", "custom"],
        help="评分树状态：注入态 / 锚解 / 半成品 / 原始基线 / 自定义补丁",
    )
    parser.add_argument("--patch", default=None, help="--state custom 时额外应用的补丁")
    parser.add_argument("--repeat", type=int, default=1, help="重复跑次数（门禁要求 20）")
    parser.add_argument("--timeout", type=int, default=600, help="单次 pytest 超时秒数")
    parser.add_argument("--out", default=None, help="把 JSON 结果写到该文件")
    parser.add_argument("--keep", action="store_true", help="保留评分树目录")
    args = parser.parse_args(argv)

    tools_dir = Path(__file__).resolve().parent
    task_dir = tools_dir.parent / "tasks" / args.task
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    repo = Path(args.repo)

    # fail-closed：selfgrade 只懂 pytest。题目声明了 vitest 检查（slim-py+fe 快照）
    # 时，本脚本拼不出前端评分树，硬跑只会漏掉前端分组、给出虚高的"假绿"分数。
    # 这类题目一律改用 runs/blind/tools/packgate.py（与生产 harness 同语义）。
    kinds = {
        str(check.get("kind") or "pytest").lower()
        for check in (meta.get("checks") or [{"kind": "pytest"}])
    }
    unsupported = sorted(kinds - {"pytest"})
    if unsupported:
        print(
            f"[{args.task}] selfgrade 只支持 pytest 检查；本题声明了 "
            f"{', '.join(unsupported)}。请改用 runs/blind/tools/packgate.py 跑门禁，"
            "不要用本脚本的分数作为入库证据。",
            file=sys.stderr,
        )
        return 2

    extra = Path(args.patch) if args.patch else None
    patches = resolve_state_patches(task_dir, meta, args.state, extra)

    tree = tools_dir / "_grade" / f"{args.task}-{args.state}"
    build_tree(repo, task_dir, meta, tree)
    touched: list[str] = []
    for patch in patches:
        touched.extend(apply_patch(tree, patch.read_text(encoding="utf-8")))

    groups_spec = json.loads((task_dir / "hidden" / "groups.json").read_text(encoding="utf-8"))
    p2p_spec = json.loads((task_dir / "p2p.json").read_text(encoding="utf-8"))
    node_ids: list[str] = []
    for group in groups_spec["groups"]:
        node_ids.extend(group.get("tests", []))
    node_ids.extend(p2p_spec.get("tests", []))
    # 用例 ID 两种写法都合法（packs/core/README §七）：`tests_hidden/x.py::t`（相对
    # hidden/ 层，规范形态）与 `hidden/tests_hidden/x.py::t`（相对评分树根，harness
    # 的 _qualify_node_id 会归一成它）。本脚本的评分树把隐藏测试直接铺在树根的
    # tests_hidden/ 下，所以选文件时要把 `hidden/` 前缀剥掉；对答案的匹配仍由
    # _match_outcome 的尾名兜底，两种写法都收得到。
    files = sorted({_strip_hidden_prefix(node.split("::", 1)[0]) for node in node_ids})

    runs = []
    for attempt in range(1, args.repeat + 1):
        outcomes, note = run_pytest(tree, files, args.timeout)
        report = grade(task_dir, meta, outcomes)
        report["attempt"] = attempt
        runs.append(report)
        print(f"[{args.task}/{args.state}] 第 {attempt} 次：得分 {report['score']}（{note}）")
        for group in report["groups"]:
            mark = "绿" if group["passed"] else "红"
            print(f"    {mark} {group['id']}（权重 {group['weight']}）"
                  f" 失败 {len(group['failed_tests'])} 缺失 {len(group['missing_tests'])}")
        if report["p2p_failures"]:
            print(f"    p2p 回归破坏 {len(report['p2p_failures'])}/{report['p2p_total']}")

    scores = [run["score"] for run in runs]
    summary = {
        "task": args.task,
        "state": args.state,
        "repo": str(repo),
        "patches": [str(path) for path in patches],
        "touched_files": sorted(set(touched)),
        "forbidden_overlap": sorted(
            set(touched) & _forbidden_matches(meta.get("forbidden_paths", []), touched)
        ),
        "runs": runs,
        "repeat": args.repeat,
        "score_min": min(scores),
        "score_max": max(scores),
        "stable": len(set(scores)) == 1,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not args.keep:
        shutil.rmtree(tree, ignore_errors=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{args.task}/{args.state}] 结果已写入 {args.out}")
    return 0


def _strip_hidden_prefix(rel: str) -> str:
    """剥掉用例 ID 文件部分的 ``hidden/`` 前缀（本脚本树里隐藏层铺在树根）。"""
    normalized = rel.replace("\\", "/")
    return normalized[len("hidden/"):] if normalized.startswith("hidden/") else normalized


def _forbidden_matches(forbidden: list[str], touched: list[str]) -> set[str]:
    """返回 ``touched`` 里命中 forbidden 的那些（glob 匹配）。"""
    hit: set[str] = set()
    for path in touched:
        for pattern in forbidden:
            normalized = pattern.replace("**", "*")
            if fnmatch(path, pattern) or fnmatch(path, normalized) or fnmatch(Path(path).name, pattern):
                hit.add(path)
    return hit


if __name__ == "__main__":
    raise SystemExit(main())
