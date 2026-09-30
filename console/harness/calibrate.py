"""盲测校准排队（设计文档 §6.4）。

出题模型不得给自己出的题做校准；盲测的做法是「同一个模型实例跑 N 次
只给第 1 级提示词的一次作答」，记录 pass@1 与置信区间。

本模块只负责排队：建 N 条干净沙箱的队列记录。
排队阶段只建记录；真正开始跑某一次时才创建文件夹沙箱。
"""

from __future__ import annotations

import os
from typing import List

from . import config, errors, packs, runs, util

#: 单次排队上限，防止一次把磁盘和记录目录灌满
MAX_TRIALS = 20


def enqueue(cfg: dict, task: str, model: str, trials: int = 5) -> dict:
    """排队 N 个干净沙箱。"""
    meta = packs.load_meta(cfg, task)
    config.find_model(cfg, model)
    try:
        trials = int(trials)
    except (TypeError, ValueError):
        raise errors.HarnessError(errors.E_BAD_REQUEST, "trials 必须是一个整数。")
    if trials < 1:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "至少要排 1 次盲测。")
    trials = min(trials, MAX_TRIALS)

    queued: List[dict] = []
    existing = [
        r for r in runs.list_runs(cfg)
        if r.get("task") == task and str(r.get("model")) == model
        and r.get("status") == "queued" and r.get("calibration")
    ]
    for index in range(trials):
        run_id = "%s__%s__cal%s" % (
            util.sanitize_id(task), util.sanitize_id(model),
            util.now_stamp() + "-%02d" % (len(existing) + index + 1))
        run = {
            "run_id": run_id,
            "task": task,
            "model": model,
            "attempt": 1,
            "attempts_allowed": meta["attempts"],
            "status": "queued",
            "created_at": util.iso_now(),
            "updated_at": util.iso_now(),
            "revealed": False,
            "rounds": [],
            "note": "",
            "calibration": True,
            "calibration_index": len(existing) + index + 1,
            "drive": "",
            "sandbox": "",
            "baseline_commit": "",
            "baseline_digest": "",
        }
        run["run_dir"] = os.path.join(cfg["runs_root"], *run_id.split("__"))
        util.ensure_dir(run["run_dir"])
        runs.save_run(cfg, run)
        queued.append(run)

    return {
        "task": task,
        "model": model,
        "trials": len(queued),
        "queued_total": len(existing) + len(queued),
        "target_band": (meta.get("calibration") or {}).get("target_band") or [],
        "run_ids": [r["run_id"] for r in queued],
        "notice": "排队完成。开始跑第一次盲测时点「准备沙箱」，会自动消耗一个排队名额。",
    }


def queue_status(cfg: dict, task: str = "", model: str = "") -> dict:
    """排队概览：按题×模型统计已排 / 已跑。"""
    items = []
    for run in runs.list_runs(cfg):
        if task and run.get("task") != task:
            continue
        if model and str(run.get("model")) != str(model):
            continue
        if not run.get("calibration"):
            continue
        items.append({
            "run_id": run["run_id"],
            "task": run.get("task"),
            "model": run.get("model"),
            "index": run.get("calibration_index"),
            "status": run.get("status"),
            "attempt": run.get("attempt", 1),
            "score": run.get("last_score"),
            "passed": run.get("last_score") is not None and float(run.get("last_score") or 0) >= 100.0,
            "updated_at": run.get("updated_at"),
        })
    queued = [i for i in items if i["status"] == "queued"]
    done = [i for i in items if i["status"] not in {"queued"}]
    passes = sum(1 for i in done if i["passed"])
    low, high = runs.wilson_interval(passes, len(done))
    return {
        "items": items,
        "queued": len(queued),
        "done": len(done),
        "pass1": passes,
        "pass_rate": round(passes / len(done), 3) if done else 0.0,
        "ci_low": round(low, 3),
        "ci_high": round(high, 3),
        "note": "校准必须由非出题模型盲测；样本少时 Wilson 区间会很宽，别拿单次结果下结论。",
    }


def cancel(cfg: dict, run_id: str) -> dict:
    """取消一个还没开始的排队项。"""
    run = runs.get_run(cfg, run_id)
    if run.get("status") != "queued":
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "这一项已经开始跑了，不能取消。要停请用「清空改动」或「重建沙箱」。",
            run_id,
        )
    run_dir = run.get("run_dir") or runs.run_dir(cfg, run)
    if os.path.isdir(run_dir):
        util.remove_tree(run_dir)
    return {"run_id": run_id, "cancelled": True}
