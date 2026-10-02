"""盲测校准执行器（编排侧一次性工具，不属于评测台运行时，不进 packs/console）。

按设计文档 §6.4 组织非出题模型盲测：T1–T3 为只给第 1 级提示词的独立作答，
王者为同一沙箱内最多三轮逐级提示的完整会话：
  build   建工作区（快照白名单 + visible.prune 裁剪，无 hidden/）→ git 单提交 baseline → subst 盘符
  collect 清理运行垃圾 → 收 taker 变更清单/diff → 判 violations → 释放盘符
  grade   拼含前端的评分树（快照+prune+注入 patch+hidden）→ 整文件 overlay 盲测者在 allowed_paths 内的产物
          → pytest + vitest → 按所有 checker 的 groups/p2p 计分（复用 packgate）
  record  把一次试验写入任务包 calibration/results.json 的 blind_runs

评分语义与 §4.2 第 4 层一致：allowed_paths 内的文件整文件覆盖；violation（allowed/forbidden 之外
的变更）→ 本轮作废（记 0 分），与生产 harness 的"命中即本轮红"对齐。

用法：
  python runs\\blind\\tools\\blindrun.py build  --task T1-01 --trial 1
  python runs\\blind\\tools\\blindrun.py collect --task T1-01 --trial 1
  python runs\\blind\\tools\\blindrun.py grade  --task T1-01 --trial 1 [--prompt-level 1] [--timeout 600]
  python runs\\blind\\tools\\blindrun.py record --task T1-01 --trial 1 --model "GLM-5.3-Flash 盲测实例"
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
TOOLS = ROOT / "packs" / "core" / "tools"
CALIB = ROOT / "runs" / "blind" / "calib"
DRIVE_POOL = ["Q:", "R:", "S:"]

sys.path.insert(0, str(TOOLS))
import packgate as pg  # noqa: E402  复用正式的多 checker 评分语义


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd)}\n{done.stdout}\n{done.stderr}")
    return done.stdout


def _task_dir(task: str) -> Path:
    return ROOT / "packs" / "core" / "tasks" / task


def _load_meta(task: str) -> dict:
    return json.loads((_task_dir(task) / "meta.json").read_text(encoding="utf-8"))


def _trial_dir(task: str, trial: int) -> Path:
    return CALIB / task / f"trial-{trial}"


def _trial_json(task: str, trial: int) -> Path:
    return _trial_dir(task, trial) / "trial.json"


def _read_trial(task: str, trial: int) -> dict:
    return json.loads(_trial_json(task, trial).read_text(encoding="utf-8"))


def _write_trial(task: str, trial: int, data: dict) -> None:
    _trial_json(task, trial).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _path_matches(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for pattern in patterns:
        p = pattern.replace("\\", "/")
        if p.endswith("/**"):
            if rel.startswith(p[:-2]) or rel == p[:-3]:
                return True
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.replace("**", "*")):
            return True
    return False


def _free_drives() -> list[str]:
    out = subprocess.run(["subst"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    mapped = {line[:2] for line in out.stdout.splitlines() if len(line) >= 2}
    return [d for d in DRIVE_POOL if d not in mapped]


def _clean_junk(workspace: Path) -> None:
    for name in (".pytest_cache", "report.xml"):
        target = workspace / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    for root, dirs, files in os.walk(workspace, topdown=True):
        for name in list(dirs):
            target = Path(root) / name
            if pg.hutil.is_junction(str(target)):
                dirs.remove(name)
            elif name in {"__pycache__", ".pytest_cache"}:
                shutil.rmtree(target, ignore_errors=True)
                dirs.remove(name)
        for name in files:
            if name.endswith(".pyc"):
                (Path(root) / name).unlink(missing_ok=True)


def _remove_trial_dir(tdir: Path) -> None:
    """Remove a prior trial without following frontend node_modules junctions."""
    if not tdir.exists():
        return
    for rel in ("workspace/node_modules", "grade-tree/node_modules"):
        junction = tdir / rel
        if pg.hutil.is_junction(str(junction)):
            junction.rmdir()
    shutil.rmtree(tdir)


def cmd_build(args: argparse.Namespace) -> None:
    meta = _load_meta(args.task)
    tdir = _trial_dir(args.task, args.trial)
    workspace = tdir / "workspace"
    if tdir.exists():
        _remove_trial_dir(tdir)
    tdir.mkdir(parents=True)
    # Blind participants must see the same injected, redacted, frontend-capable
    # baseline that production grading evaluates. Hidden tests remain excluded.
    task_dir = _task_dir(args.task)
    inject = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    pg.build_tree(args.task, pg._load_meta(args.task), workspace, inject)
    (workspace / "pytest.ini").unlink(missing_ok=True)
    _run(["git", "init", "-q"], workspace)
    _run(["git", "add", "-A"], workspace)
    _run(["git", "-c", "user.name=blindrun", "-c", "user.email=blindrun@local",
          "commit", "-q", "-m", "baseline"], workspace)
    drive = None
    for candidate in _free_drives():
        done = subprocess.run(["subst", candidate, str(workspace)], capture_output=True, text=True)
        if done.returncode == 0:
            drive = candidate
            break
    _write_trial(args.task, args.trial, {
        "task": args.task, "trial": args.trial, "workspace": str(workspace),
        "drive": drive, "snapshot": meta.get("repo", {}).get("snapshot"),
        "commit": meta.get("repo", {}).get("commit"),
    })
    print(f"工作区就绪：{workspace}")
    print(f"盘符：{drive or '（无空闲盘符，走绝对路径）'}")


def cmd_collect(args: argparse.Namespace) -> None:
    info = _read_trial(args.task, args.trial)
    meta = _load_meta(args.task)
    workspace = Path(info["workspace"])
    _clean_junk(workspace)
    _run(["git", "add", "-A"], workspace)
    patch = _run(["git", "diff", "--cached"], workspace)
    status = _run(["git", "diff", "--cached", "--name-status"], workspace)
    changed: list[dict] = []
    for line in status.splitlines():
        line = line.strip()
        if not line:
            continue
        mark, rel = line.split(maxsplit=1) if " " in line else (line, "")
        rel = rel.replace("\\", "/").strip('"')
        if rel.startswith("a/"):
            rel = rel[2:]
        changed.append({"state": mark, "path": rel})
    allowed = meta.get("allowed_paths", [])
    forbidden = meta.get("forbidden_paths", [])
    for item in changed:
        rel = item["path"]
        item["in_allowed"] = _path_matches(rel, allowed)
        item["in_forbidden"] = _path_matches(rel, forbidden)
    violations = [i["path"] for i in changed
                  if not i["in_allowed"] or i["in_forbidden"]]
    if info.get("drive"):
        subprocess.run(["subst", info["drive"], "/D"], capture_output=True)
        info["drive"] = None
    patch_path = _trial_dir(args.task, args.trial) / "taker.patch"
    patch_path.write_text(patch, encoding="utf-8")
    info.update({
        "changed": changed, "violations": violations,
        "patch": str(patch_path), "collected": True,
    })
    _write_trial(args.task, args.trial, info)
    print(f"变更 {len(changed)} 处，违规 {len(violations)} 处：{violations}")
    for item in changed:
        print(f"  {item['state']} {item['path']}（allowed={item['in_allowed']}）")


def cmd_grade(args: argparse.Namespace) -> None:
    info = _read_trial(args.task, args.trial)
    if not info.get("collected"):
        raise SystemExit("先跑 collect")
    meta = _load_meta(args.task)
    history = info.setdefault("grade_history", [])
    round_number = len(history) + 1
    max_rounds = int(meta.get("attempts") or 1)
    if round_number > max_rounds:
        raise SystemExit(f"该 trial 已完成 {max_rounds} 轮评分")
    prompt_level = args.prompt_level or 1
    if meta.get("tier") == "king" and prompt_level != round_number:
        raise SystemExit(f"王者 trial 第 {round_number} 次评分应对应第 {round_number} 级提示词")
    if history and history[-1].get("passed"):
        raise SystemExit("该 trial 已经通过，不应继续消耗下一轮提示")
    task_dir = _task_dir(args.task)
    grade_tree = _trial_dir(args.task, args.trial) / "grade-tree"
    if grade_tree.exists():
        node_modules = grade_tree / "node_modules"
        if pg.hutil.is_junction(str(node_modules)):
            node_modules.rmdir()
        shutil.rmtree(grade_tree)
    pg_meta = pg._load_meta(args.task)
    inject = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    pg.build_tree(args.task, pg_meta, grade_tree, inject)
    workspace = Path(info["workspace"])
    overlay, removed = [], []
    for item in info["changed"]:
        rel = item["path"]
        if not item.get("in_allowed") or item.get("in_forbidden"):
            continue  # 违规产物不进评分树，与生产 harness 一致
        source = workspace / rel
        target = grade_tree / rel
        if item["state"] == "D" or not source.exists():
            if target.exists():
                target.unlink()
                removed.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        overlay.append(rel)
    report = _grade_all_checks(grade_tree, pg_meta, args.timeout)
    report["overlay_files"] = overlay
    report["removed_files"] = removed
    report["violations"] = info.get("violations", [])
    report["invalid"] = bool(info.get("violations"))
    out = _trial_dir(args.task, args.trial) / "grade.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = not report["invalid"] and report["score"] == 100.0
    history.append({
        "round": round_number,
        "prompt_level": prompt_level,
        "score": report["score"],
        "passed": passed,
        "invalid": report["invalid"],
        "failed_groups": [group["id"] for group in report["groups"] if not group["passed"]],
        "p2p_broken": len(report.get("p2p_failures", [])),
    })
    info["grade_history"] = history
    info["collected"] = False
    _write_trial(args.task, args.trial, info)
    print(f"得分 {report['score']}（overlay {len(overlay)} 文件；违规 {len(report['violations'])}）")
    for group in report["groups"]:
        mark = "绿" if group["passed"] else "红"
        failed = sum(1 for case in group.get("cases", []) if case.get("outcome") == "failed")
        missing = sum(1 for case in group.get("cases", []) if case.get("outcome") == "missing")
        print(f"  {mark} {group['id']}（权重 {group['weight']}）"
              f" 失败 {failed} 缺失 {missing}")
    if report["p2p_failures"]:
        print(f"  p2p 回归破坏 {len(report['p2p_failures'])}/{report['p2p_total']}")


def _grade_all_checks(grade_tree: Path, meta: dict, timeout_s: int) -> dict:
    """把每种 checker 的隐藏组与 p2p 白名单都计入一次盲测结果。"""
    per_check = []
    for spec in meta.get("checks") or [{"kind": "pytest"}]:
        kind = str(spec.get("kind") or "pytest").lower()
        groups, p2p_tests, outcome = pg._run_check(grade_tree, meta, spec, timeout_s)
        per_check.append((kind, groups, p2p_tests, outcome))
    return pg._grade(meta, per_check)


def _wilson_interval(passes: int, runs: int) -> list[float] | None:
    if runs <= 0:
        return None
    import math

    z = 1.96
    p = passes / runs
    denom = 1 + z * z / runs
    center = (p + z * z / (2 * runs)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * runs)) / runs) / denom
    return [round(max(0.0, center - margin), 3),
            round(min(1.0, center + margin), 3)]


def cmd_record(args: argparse.Namespace) -> None:
    info = _read_trial(args.task, args.trial)
    grade = json.loads((_trial_dir(args.task, args.trial) / "grade.json").read_text(encoding="utf-8"))
    results_path = _task_dir(args.task) / "calibration" / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    failed_groups = [g["id"] for g in grade["groups"] if not g["passed"]]
    meta = _load_meta(args.task)
    tier = meta.get("tier")
    metric = results.get("target_metric") or (meta.get("calibration") or {}).get("target_metric") or "pass_at_1"
    is_pass_at_3 = metric == "pass_at_3"
    history = info.get("grade_history") or []
    first_pass = next((entry for entry in history if entry.get("passed")), None)
    rounds_used = (first_pass["round"] if first_pass else len(history)) if is_pass_at_3 else 1
    if is_pass_at_3:
        max_rounds = int(meta.get("attempts") or 3)
        if not 1 <= rounds_used <= max_rounds or len(history) > max_rounds:
            raise SystemExit(f"pass_at_3 需要 1..{max_rounds} 轮有效评分历史")
    passed = bool(first_pass) if is_pass_at_3 else (not grade["invalid"]) and grade["score"] == 100.0
    if first_pass:
        score = first_pass["score"]
        failed_groups = first_pass["failed_groups"]
        p2p_broken = first_pass["p2p_broken"]
    else:
        score = 0.0 if grade["invalid"] else grade["score"]
        failed_groups = [g["id"] for g in grade["groups"] if not g["passed"]]
        p2p_broken = len(grade.get("p2p_failures", []))
    row = {
        "run_id": f"{args.task}-blind-{args.trial:02d}",
        "model": args.model,
        "tier": tier,
        "prompt_level": rounds_used,
        "score": score,
        "failed_groups": failed_groups,
        "p2p_broken": p2p_broken,
        "notes": args.note or ("违规作废：" + "、".join(grade["violations"]) if grade["invalid"] else ""),
    }
    if is_pass_at_3:
        row["rounds_used"] = rounds_used
        row["pass@3"] = passed
    else:
        row["pass@1"] = passed
    rows = results.setdefault("blind_runs", {}).setdefault("rows", [])
    rows[:] = [r for r in rows if r.get("run_id") != row["run_id"]]
    rows.append(row)
    runs = len(rows)
    outcome_key = "pass@3" if is_pass_at_3 else "pass@1"
    passes = sum(1 for r in rows if r.get(outcome_key))
    p = passes / runs if runs else None
    ci = _wilson_interval(passes, runs)
    band = results.get("target_band") or []
    in_band = bool(band and p is not None and band[0] <= p <= band[1])
    summary = {
        "runs": runs,
        "confidence_interval": ci,
        "in_band": in_band if p is not None else None,
        "conclusion": None,
    }
    summary[metric] = p
    results["summary"] = summary
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已记录 {row['run_id']}：{outcome_key}={passed} score={row['score']}；"
          f"当前 {runs} 次通过 {passes}（{p}），区间 {ci}，目标带 {band} → in_band={in_band}")


def main() -> int:
    parser = argparse.ArgumentParser(description="盲测校准执行器")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("build", "collect", "grade", "record"):
        p = sub.add_parser(name)
        p.add_argument("--task", required=True)
        p.add_argument("--trial", type=int, required=True)
        if name == "grade":
            p.add_argument("--timeout", type=int, default=600)
        if name == "record":
            p.add_argument("--model", required=True)
            p.add_argument("--note", default="")
        if name == "grade":
            p.add_argument("--prompt-level", type=int, choices=(1, 2, 3), default=None,
                           help="pass_at_3 当前评分对应的提示级别；必须按顺序逐轮使用")
    args = parser.parse_args()
    {"build": cmd_build, "collect": cmd_collect,
     "grade": cmd_grade, "record": cmd_record}[args.cmd](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
