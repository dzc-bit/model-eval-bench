"""把已完成的盲测运行回填进题包的 calibration/results.json。

校准纪律（设计文档 §9）：出题者不做盲测、不自填校准数据。本工具不做任何
"计算难度"的事——它只是把 runs/ 里**已经跑完的**校准运行（calibration=true、
有评分轮次）的客观结果汇总回 results.json，替代手抄：

    python backfill_calibration.py --task T1-01 \
        --runs-root "../../runs" --packs-root "../../packs"

默认 dry-run（只打印将写入的内容）；加 --write 才落盘；加 --mark-calibrated
才会把 calibrated 置 true（应由组织盲测的人在复核后显式给出，而不是工具默认）。

统计口径与 harness 一致：作废轮（invalidated）不计；一行 = 一次盲测运行
取其最好的一次有效评分轮；pass@1 = 满分行数 / 有效行数；置信区间用 Wilson 95%。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys


def wilson(passes: int, trials: int, z: float = 1.96) -> tuple:
    if trials <= 0:
        return (0.0, 1.0)
    p = passes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    spread = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, (centre - spread) / denom), min(1.0, (centre + spread) / denom))


def iter_calibration_runs(runs_root: str, task: str) -> list:
    rows = []
    if not os.path.isdir(runs_root):
        return rows
    for dirpath, _dirnames, filenames in os.walk(runs_root):
        if "run.json" not in filenames:
            continue
        try:
            with open(os.path.join(dirpath, "run.json"), "r", encoding="utf-8") as fh:
                run = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(run, dict) or not run.get("calibration"):
            continue
        if str(run.get("task") or "") != task:
            continue
        best = None
        for rnd in run.get("rounds") or []:
            if not isinstance(rnd, dict) or rnd.get("invalidated"):
                continue
            try:
                score = float(rnd.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if best is None or score > best[0]:
                best = (score, rnd)
        if best is None:
            continue
        score, rnd = best
        failed_groups = sorted({
            str(g.get("id") or "")
            for g in (rnd.get("groups") or [])
            if isinstance(g, dict) and not g.get("passed")
        } - {""})
        rows.append({
            "run_id": run.get("run_id", ""),
            "model": str(run.get("model") or ""),
            "tier": str(run.get("tier") or run.get("attempts_allowed") or ""),
            "prompt_level": int(rnd.get("attempt") or 1),
            "pass@1": 1 if (score >= 100.0 and not rnd.get("invalidated")) else 0,
            "score": score,
            "failed_groups": failed_groups,
            "p2p_broken": bool(rnd.get("p2p_broken")),
            "notes": str(rnd.get("invalid_reason") or ""),
        })
    rows.sort(key=lambda r: str(r["run_id"]))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="题号，如 T1-01")
    parser.add_argument("--runs-root", default=os.path.join("runs"),
                        help="运行记录根目录（默认 ./runs）")
    parser.add_argument("--packs-root", default=os.path.join("packs"),
                        help="题包根目录（默认 ./packs）")
    parser.add_argument("--write", action="store_true", help="真正写回 results.json（默认只预览）")
    parser.add_argument("--mark-calibrated", action="store_true",
                        help="盲测复核通过后由组织者显式给出；把 calibrated 置 true")
    args = parser.parse_args()

    results_path = os.path.join(args.packs_root, "core", "tasks", args.task,
                                "calibration", "results.json")
    if not os.path.isfile(results_path):
        print("找不到 %s" % results_path, file=sys.stderr)
        return 2
    with open(results_path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    rows = iter_calibration_runs(args.runs_root, args.task)
    # 口径与 harness 的 _cell_stats 一致：所有有效评分行都进分母，
    # p2p_broken 行的 pass@1 本来就是 0（score<100），当失败计入。
    valid = rows
    passes = sum(r["pass@1"] for r in valid)
    low, high = wilson(passes, len(valid))
    band = doc.get("target_band") or []
    in_band = None
    if len(band) == 2 and valid:
        rate = passes / len(valid)
        in_band = float(band[0]) <= rate <= float(band[1])

    doc["blind_runs"]["rows"] = rows
    doc["summary"] = {
        "runs": len(rows),
        "pass_at_1": round(passes / len(valid), 3) if valid else None,
        "confidence_interval": [round(low, 3), round(high, 3)] if valid else None,
        "in_band": in_band,
        "conclusion": (
            "回填自 runs/ 中 %d 条已评分的校准运行；样本 %d 条有效。"
            "样本少时区间很宽，不要据此对难度带下强结论。" % (len(rows), len(valid))
            if rows else "runs/ 里没有已评分的校准运行，空表保持不变。"
        ),
    }
    if args.mark_calibrated and rows:
        doc["calibrated"] = True

    preview = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.write:
        with open(results_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(preview + "\n")
        print("已写入 %s（%d 行）" % (results_path, len(rows)))
    else:
        print(preview)
        print("\n[dry-run] 未写入；确认后加 --write", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
