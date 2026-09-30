"""通用题包门禁 runner（编排侧工具）。

按真实 harness 的语义拼评分树（snapshot.build：白名单拷贝→脱敏→裁剪→注入→泄漏兜底），
再驱动 checks 注册表里的 pytest / vitest checker，按 groups/p2p 做加权计分。
与 selfgrade 的差别：支持 slim-py+fe（node_modules 联接 + vitest 组），树布局与
生产 harness 一致（hidden/ 整层进树，groups 节点 ID 带 hidden/ 前缀）。

用法：
  python packgate.py --task T1-02 --state injected [--repeat 20] [--out gate.json]
  state ∈ baseline / injected / fixed / partial / custom（custom 需 --patch 模型 diff）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
HARNESS = ROOT / "console"
TOOLS = ROOT / "packs" / "core" / "tools"
GATES = ROOT / "runs" / "blind" / "gates"

sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(TOOLS))

import selfgrade as sg  # noqa: E402  复用 unified diff 应用器
from harness import checks as hchecks  # noqa: E402
from harness import packs as hpacks  # noqa: E402
from harness import sandbox as hsbox  # noqa: E402
from harness import snapshot as hsnap  # noqa: E402
from harness import util as hutil  # noqa: E402
from harness.grade import build_env  # noqa: E402

CFG = json.loads((HARNESS / "config.json").read_text(encoding="utf-8"))


def _log(msg: str) -> None:
    print(msg, flush=True)


def _task_dir(task: str) -> Path:
    return ROOT / "packs" / "core" / "tasks" / task


def _load_meta(task: str) -> dict:
    meta = json.loads((_task_dir(task) / "meta.json").read_text(encoding="utf-8"))
    meta["pack_dir"] = str(_task_dir(task))
    return meta


def _needs_frontend(meta: dict) -> bool:
    return str((meta.get("repo") or {}).get("snapshot") or "").endswith("+fe")


def _apply_patches(dest: Path, patches: list[Path]) -> int:
    touched: list[str] = []
    for patch in patches:
        touched.extend(sg.apply_patch(dest, patch.read_text(encoding="utf-8")))
    return len(touched)


def build_tree(task: str, meta: dict, dest: Path, patches: list[Path]) -> None:
    def injector(workdir: str, log) -> int:
        return _apply_patches(Path(workdir), patches)

    hsnap.build(CFG, meta, str(dest), log=_log, injector=injector)
    if _needs_frontend(meta):
        target = hsbox.repo_node_modules(CFG)
        link = str(dest / "node_modules")
        result = hutil.run_cmd(["cmd", "/c", "mklink", "/J", link, target], timeout=60)
        if not result.ok or not hutil.is_junction(link):
            raise RuntimeError("node_modules 联接失败：%s" % result.tail(6))
        _log("已联接 node_modules（只读复用）")


def _overlay_hidden(dest: Path, meta: dict, spec: dict) -> dict:
    hidden = hpacks.load_hidden_for(meta, spec)
    target = dest / hidden["overlay_rel"]
    if not target.exists():
        count = hutil.copy_tree(hidden["overlay_src"], str(target))
        _log("叠加隐藏层 %s/：%d 个文件" % (hidden["overlay_rel"], count))
    return hidden


def _run_check(dest: Path, meta: dict, spec: dict, timeout_s: int) -> tuple:
    kind = str(spec.get("kind") or "pytest").lower()
    hidden = _overlay_hidden(dest, meta, spec)
    checker = hchecks.get(kind)
    if checker is None:
        raise RuntimeError("未注册的 checker：%s" % kind)
    groups = [dict(g, kind=kind) for g in hidden["groups"]]
    node_ids = [t for g in groups if g.get("mode") != "regression" for t in g["tests"]]
    node_ids += hidden["p2p_tests"]
    env = build_env(CFG, str(dest))
    ctx = hchecks.CheckContext(
        workdir=str(dest), spec=spec, kind=kind, node_ids=node_ids,
        timeout_s=timeout_s, env=env, log=_log,
        batch=0, batch_total=1, tmp_dir=str(dest / ".grade-cache"),
    )
    if kind == "vitest":
        _relocate_fe_hidden(dest, node_ids)
    outcome = checker(ctx)
    return groups, hidden["p2p_tests"], outcome


def _relocate_fe_hidden(dest: Path, node_ids: list) -> None:
    """把 hidden-fe 的用例搬进 frontend/src/tests_hidden_fe/，vitest 的 root 才收得到。"""
    src = dest / "hidden-fe" / "tests_hidden_fe"
    if not src.is_dir():
        return
    target = dest / "frontend" / "src" / "tests_hidden_fe"
    target.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        shutil.copy2(item, target / item.name)


def _grade(meta: dict, per_check: list) -> dict:
    groups = []
    p2p_failures = []
    weight_total = 0.0
    weight_passed = 0.0
    for kind, check_groups, p2p_tests, outcome in per_check:
        if kind == "vitest":
            resolver = hchecks.vitest.make_resolver(outcome.cases)
        else:
            resolver = hchecks.pytest.make_resolver(outcome.cases)
        for group in check_groups:
            if group.get("mode") == "regression":
                continue
            cases = []
            for node_id in group.get("tests", []):
                info = resolver(node_id)
                if info is None:
                    cases.append({"node_id": node_id, "outcome": "missing", "message": "用例未被收集"})
                else:
                    cases.append({"node_id": node_id, "outcome": info.outcome,
                                  "message": info.message[:500]})
            passed = bool(cases) and all(c["outcome"] == "passed" for c in cases)
            weight = float(group.get("weight", 1))
            weight_total += weight
            weight_passed += weight if passed else 0.0
            groups.append({"id": group["id"], "weight": weight, "passed": passed,
                           "cases": cases})
        for node_id in p2p_tests:
            info = resolver(node_id)
            if info is not None and not info.passed:
                p2p_failures.append({"node_id": node_id, "outcome": info.outcome,
                                     "message": info.message[:300]})
    score = round(100.0 * weight_passed / weight_total, 2) if weight_total else 0.0
    return {"score": 0.0 if p2p_failures else score, "raw_score": score,
            "groups": groups, "p2p_failures": p2p_failures,
            "p2p_total": sum(1 for _, _, tests, _ in per_check for _ in tests)}


def run_gate(task: str, state: str, extra: Path | None, repeat: int,
             timeout: int, out: Path | None, keep: bool) -> dict:
    meta = _load_meta(task)
    task_dir = _task_dir(task)
    inject = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    if state == "baseline":
        inject = []
    if state == "fixed":
        inject.append(task_dir / "reference" / "fix.patch")
    elif state == "partial":
        inject.append(task_dir / "reference" / "partial.patch")
    if extra is not None:
        inject.append(extra)
    missing = [str(p) for p in inject if not p.is_file()]
    if missing:
        raise FileNotFoundError("缺少补丁：" + ", ".join(missing))

    dest = GATES / f"{task}-{state}"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    build_tree(task, meta, dest, inject)
    (dest / ".grade-cache").mkdir(parents=True, exist_ok=True)  # --basetemp 的父目录必须先在

    timeout_s = int((meta.get("budget") or {}).get("grade_timeout_s") or timeout)
    runs = []
    for attempt in range(1, repeat + 1):
        per_check = []
        for spec in (meta.get("checks") or [{"kind": "pytest"}]):
            groups, p2p_tests, outcome = _run_check(dest, meta, spec, timeout_s)
            _log("[%s/%s] %s %s：%s" % (task, state, attempt, spec.get("kind"),
                                        outcome.summary_line()))
            per_check.append((str(spec.get("kind") or "pytest"), groups, p2p_tests, outcome))
        report = _grade(meta, per_check)
        report["attempt"] = attempt
        runs.append(report)
        _log("  第 %d 次：得分 %s（p2p 破坏 %d）" % (
            attempt, report["score"], len(report["p2p_failures"])))
        for group in report["groups"]:
            mark = "绿" if group["passed"] else "红"
            _log("    %s %s（权重 %g）" % (mark, group["id"], group["weight"]))
            for case in group["cases"]:
                if case["outcome"] != "passed":
                    _log("      · %s [%s] %s" % (case["node_id"], case["outcome"],
                                                 case.get("message", "")[:160]))
    scores = [r["score"] for r in runs]
    summary = {
        "task": task, "state": state,
        "patches": [str(p) for p in inject],
        "runs": runs, "repeat": repeat,
        "score_min": min(scores), "score_max": max(scores),
        "stable": len(set(scores)) == 1,
        "finished_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _log("结果已写入 %s" % out)
    if not keep:
        shutil.rmtree(dest, ignore_errors=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="题包门禁 runner")
    parser.add_argument("--task", required=True)
    parser.add_argument("--state", default="injected",
                        choices=["baseline", "injected", "fixed", "partial", "custom"])
    parser.add_argument("--patch", default=None, help="state=custom 时的模型 diff")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    run_gate(args.task, args.state,
             Path(args.patch) if args.patch else None,
             args.repeat, args.timeout,
             Path(args.out) if args.out else None, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
