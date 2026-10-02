"""Read-only item difficulty statistics from the result ledger and task packs.

Run with ``python console/harness/itemstats.py`` from any working directory.
The command only reads ``runs/_results/ledger.json`` and pack ``meta.json`` files.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def load_entries(runs_root: Path) -> list:
    """Read usable entries, tolerating a missing or malformed ledger."""
    doc = _read_json(runs_root / "_results" / "ledger.json")
    raw = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        return []
    return [
        entry for entry in raw
        if isinstance(entry, dict) and entry.get("task") and entry.get("model_raw")
    ]


def load_task_metadata(packs_root: Path) -> dict:
    """Map task ids to the target metric, target band, and title in each pack."""
    tasks = {}
    if not packs_root.is_dir():
        return tasks
    for repo in sorted(packs_root.iterdir()):
        if not repo.is_dir() or repo.name.startswith("."):
            continue
        task_root = repo / "tasks" if (repo / "tasks").is_dir() else repo
        if not task_root.is_dir():
            continue
        for task_dir in sorted(task_root.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue
            raw = _read_json(task_dir / "meta.json")
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            calibration = raw.get("calibration")
            calibration = calibration if isinstance(calibration, dict) else {}
            task_id = str(raw["id"])
            tasks[task_id] = {
                "title": str(raw.get("title") or ""),
                "target_metric": str(calibration.get("target_metric") or "pass_at_1"),
                "target_band": calibration.get("target_band"),
            }
    return tasks


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _metric_value(entry: dict, metric: str) -> float:
    if metric == "pass_at_3":
        return 1.0 if entry.get("passed") is True else 0.0
    return 1.0 if entry.get("pass1") is True else 0.0


def _valid_band(raw):
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    low, high = (_number(value, float("nan")) for value in raw)
    if not math.isfinite(low) or not math.isfinite(high) or low > high:
        return None
    return [low, high]


def _group_correlations(entries: list) -> list:
    observations = {}
    for entry in entries:
        score = _number(entry.get("score"))
        groups = entry.get("groups")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not group.get("id"):
                continue
            group_id = str(group["id"])
            item = observations.setdefault(group_id, {
                "id": group_id,
                "port": str(group.get("port") or ""),
                "weight": group.get("weight", 1),
                "values": [],
            })
            item["values"].append((bool(group.get("passed")), score))

    out = []
    for item in observations.values():
        values = item.pop("values")
        passed_scores = [score for passed, score in values if passed]
        failed_scores = [score for passed, score in values if not passed]
        n = len(values)
        mean_passed = statistics.mean(passed_scores) if passed_scores else None
        mean_failed = statistics.mean(failed_scores) if failed_scores else None
        mean_score = statistics.mean(score for _, score in values) if values else 0.0
        variance = sum((score - mean_score) ** 2 for _, score in values) / n if n else 0.0
        deviation = math.sqrt(variance)
        correlation = None
        if passed_scores and failed_scores and deviation > 0:
            fraction_passed = len(passed_scores) / n
            correlation = (
                (mean_passed - mean_failed)
                * math.sqrt(fraction_passed * (1.0 - fraction_passed))
                / deviation
            )
        out.append({
            **item,
            "n": n,
            "passed_n": len(passed_scores),
            "failed_n": len(failed_scores),
            "mean_score_passed": round(mean_passed, 3) if mean_passed is not None else None,
            "mean_score_failed": round(mean_failed, 3) if mean_failed is not None else None,
            "point_biserial_r": round(correlation, 4) if correlation is not None else None,
        })
    out.sort(key=lambda group: (
        group["point_biserial_r"] is None,
        -abs(group["point_biserial_r"] or 0.0),
        group["id"],
    ))
    return out


def summarize_task(task_id: str, entries: list, metadata: dict) -> dict:
    """Summarize one task's ledger entries without inferring missing old groups."""
    task_info = metadata.get(task_id) or {}
    metric = str(task_info.get("target_metric") or "pass_at_1")
    task_entries = [entry for entry in entries if str(entry.get("task") or "") == task_id]
    n = len(task_entries)
    scores = [_number(entry.get("score")) for entry in task_entries]
    metric_values = [_metric_value(entry, metric) for entry in task_entries]
    p = statistics.mean(metric_values) if metric_values else None
    p_std = statistics.stdev(metric_values) if n > 1 else None
    score_mean = statistics.mean(scores) if scores else None
    score_std = statistics.stdev(scores) if n > 1 else None
    quick_full_score = any(
        _number(entry.get("score")) >= 100.0 and int(_number(entry.get("rounds"), 1)) <= 2
        for entry in task_entries
    )
    ceiling = quick_full_score or (p == 1.0 and n > 0)
    floor = p == 0.0 and n > 0
    band = _valid_band(task_info.get("target_band"))

    if n == 0:
        band_status = "no_data"
        recommendation = "没有已结束的有效尝试；先积累实测样本。"
    elif band is None:
        band_status = "no_target_band"
        recommendation = "题包未提供有效 target_band，暂不建议按难度带定档。"
    elif p < band[0]:
        band_status = "below_target"
        recommendation = "实测 p 低于目标带下沿；复核可赢性与档位。"
    elif p > band[1]:
        band_status = "above_target"
        recommendation = "实测 p 高于目标带上沿；复核是否需要提高难度。"
    else:
        band_status = "within_target"
        recommendation = "实测 p 在目标带内；暂保留当前档位。"
    if ceiling and band_status not in {"no_data", "no_target_band"}:
        recommendation += " 存在一至两轮满分或全通过的天花板信号。"

    return {
        "task": task_id,
        "title": str(task_info.get("title") or ""),
        "n": n,
        "target_metric": metric,
        "p": round(p, 4) if p is not None else None,
        "p_std": round(p_std, 4) if p_std is not None else None,
        "mean_score": round(score_mean, 3) if score_mean is not None else None,
        "score_std": round(score_std, 3) if score_std is not None else None,
        "ceiling": ceiling,
        "floor": floor,
        "target_band": band,
        "band_status": band_status,
        "recommendation": recommendation,
        "small_sample": n < 5,
        "groups": _group_correlations(task_entries),
    }


def summarize(entries: list, metadata: dict, task_filter: str = "") -> dict:
    task_ids = set(metadata)
    task_ids.update(str(entry.get("task") or "") for entry in entries)
    task_ids.discard("")
    if task_filter:
        task_ids = {task_id for task_id in task_ids if task_id == task_filter}
    return {
        "tasks": [summarize_task(task_id, entries, metadata) for task_id in sorted(task_ids)]
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=PROJECT_ROOT / "runs")
    parser.add_argument("--packs-root", type=Path, default=PROJECT_ROOT / "packs")
    parser.add_argument("--task", default="", help="只输出指定题号")
    args = parser.parse_args(argv)
    result = summarize(
        load_entries(args.runs_root),
        load_task_metadata(args.packs_root),
        task_filter=args.task,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
