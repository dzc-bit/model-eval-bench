"""运行记录与统计（设计文档 §16）。

每轮目录：runs/<任务>/<模型>/<时间>/
    run.json              运行状态（沙箱、盘符、基线指纹、轮次成绩）
    baseline_manifest.json 准备时的全树哈希清单（越界检测的基准）
    diff.patch            本轮改动
    report.json           最近一轮报告
    round-<n>.json        每一轮的报告
    notes.md              中文小结
    grade.log             校验日志（前端轮询这个）

一条 run = 一次「选题 + 选模型」的完整评测会话，可以有多轮（attempt）。
pass@1 取第 1 轮成绩，pass@k 取前 k 轮里有没有全绿。
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable, Dict, List, Optional

from . import config, errors, grade, packs, report as report_mod, sandbox, util

Log = Callable[[str], None]

#: 进程内串行化记录写入
_STORE_LOCK = threading.RLock()
#: 正在校验中的 run_id，避免重复触发
_GRADING: Dict[str, bool] = {}
_GRADING_LOCK = threading.Lock()

#: Wilson 区间用的 z 值（95% 置信）
_Z = 1.959963985


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

class RunLogger:
    """把一次校验的日志同时写到文件与内存（前端轮询读内存那份）。"""

    def __init__(self, run_dir: str, keep: int = 400):
        self.path = os.path.join(run_dir, "grade.log")
        self.keep = keep
        self.lines: List[str] = []
        self._lock = threading.Lock()
        util.ensure_dir(run_dir)

    def __call__(self, message: str) -> None:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), message)
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > self.keep:
                del self.lines[:len(self.lines) - self.keep]
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def tail(self, limit: int = 200) -> List[str]:
        with self._lock:
            return self.lines[-limit:]


# --------------------------------------------------------------------------
# 目录与读写
# --------------------------------------------------------------------------

def run_dir(cfg: dict, run: dict) -> str:
    """记录的落盘目录（由 run_id 反推）。"""
    return dir_of_run_id(cfg, run["run_id"])


def dir_of_run_id(cfg: dict, run_id: str) -> str:
    """由 run_id 推记录目录。每一段单独 sanitize：run_id 会从 URL 参数进来，
    直接 join 等于把 `..` 一起拼进路径。"""
    parts = [util.sanitize_id(p) for p in str(run_id).split("__")]
    if len(parts) >= 3:
        return os.path.join(cfg["runs_root"], *parts)
    return os.path.join(cfg["runs_root"], util.sanitize_id(run_id))


def _new_run_id(cfg: dict, task: str, model: str) -> str:
    """生成不重复的 run_id：<任务>__<模型>__<时间>。"""
    base = "%s__%s__%s" % (util.sanitize_id(task), util.sanitize_id(model), util.now_stamp())
    candidate = base
    suffix = 1
    while os.path.isdir(dir_of_run_id(cfg, candidate)):
        suffix += 1
        candidate = "%s-%d" % (base, suffix)
    return candidate


def save_run(cfg: dict, run: dict) -> None:
    """把运行状态原子落盘。"""
    with _STORE_LOCK:
        run["updated_at"] = util.iso_now()
        run["run_dir"] = run_dir(cfg, run)
        slim = {k: v for k, v in run.items() if k not in {"baseline_manifest"}}
        util.write_json_atomic(os.path.join(run["run_dir"], "run.json"), slim)


def list_runs(cfg: dict) -> List[dict]:
    """扫描全部运行记录（按时间倒序）。缺目录/坏文件都不影响其它记录。"""
    out: List[dict] = []
    root = cfg["runs_root"]
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        if "run.json" not in filenames:
            continue
        run = util.read_json(os.path.join(dirpath, "run.json"), default=None)
        if isinstance(run, dict) and run.get("run_id"):
            run["run_dir"] = dirpath
            out.append(run)
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def get_run(cfg: dict, run_id: str) -> dict:
    """按 run_id 取记录。

    这里不做 sanitize：它只跟已落盘的 run_id 比对，不用它拼路径，
    收敛一遍反而会让中文模型名（"归档模型"）的记录永远查不到。
    路径安全由 run_dir 那一侧的逐段 sanitize 负责。
    """
    wanted = str(run_id)
    for run in list_runs(cfg):
        if run.get("run_id") == wanted:
            return run
    raise errors.HarnessError(
        errors.E_RUN_NOT_FOUND,
        "找不到这次运行记录（可能已被删除）。请回到任务库重新准备沙箱。",
        run_id,
    )


def reserved_drives(cfg: dict, exclude: str = "") -> dict:
    """当前被占用的盘符 → run_id，用于盘符池分配。"""
    out = {}
    for run in list_runs(cfg):
        if run.get("run_id") == exclude:
            continue
        drive = run.get("drive")
        if drive and run.get("sandbox") and os.path.isdir(run["sandbox"]):
            out[drive] = run["run_id"]
    return out


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

def create_run(cfg: dict, task: str, model: str, attempt: int = 1,
               claim_queued: bool = True, wait_s: float = 0.0,
               log: Log = None) -> dict:
    """准备一轮新运行：读题包 → 建记录 → 准备沙箱。

    若有同一题同模型的排队中校准沙箱（§6.4 盲测排队），直接认领一个，
    这样校准排了 N 个沙箱后，跑一次就消耗一个，不会白占盘符。

    :param wait_s: 盘符暂时用尽时的等待上限（秒）。跑批会传一个非零值，
        避免"上一条刚回收、下一条还没拿到"的毫秒级窗口被误判为池子用尽。
    """
    meta = packs.load_meta(cfg, task)
    config.find_model(cfg, model)          # 模型档案不存在就直接报错
    attempt = max(1, int(attempt or 1))
    if attempt > meta["attempts"]:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "%s 是%s题，最多 %d 次机会，第 %d 轮不存在。"
            % (task, {"easy": "初级", "medium": "中级", "hard": "高级", "king": "王者"}.get(meta["tier"], meta["tier"]),
               meta["attempts"], attempt),
        )

    logger = log or (lambda m: None)
    run_id = _new_run_id(cfg, task, model)
    with _STORE_LOCK:
        run = {
            "run_id": run_id,
            "task": task,
            "model": model,
            "attempt": attempt,
            "attempts_allowed": meta["attempts"],
            "status": "preparing",
            "created_at": util.iso_now(),
            "updated_at": util.iso_now(),
            "revealed": False,
            "rounds": [],
            "note": "",
            "calibration": False,
            "drive": "",
            "sandbox": "",
            "baseline_commit": "",
            "baseline_digest": "",
        }
        run["run_dir"] = dir_of_run_id(cfg, run_id)
        util.ensure_dir(run["run_dir"])
        save_run(cfg, run)

    claimed = _claim_queued(cfg, task, model, logger) if claim_queued else None
    if claimed:
        logger("认领了一个排队中的校准沙箱：%s" % claimed["run_id"])
        target_dir = run["run_dir"]
        util.remove_tree(target_dir)
        os.replace(claimed["run_dir"], target_dir)
        run = util.read_json(os.path.join(target_dir, "run.json"), default=run) or run
        run["calibration"] = True
        run["status"] = "ready"
        run["updated_at"] = util.iso_now()
        save_run(cfg, run)
        return run

    sandbox.prepare(cfg, run, meta, reserved=reserved_drives(cfg, exclude=run_id),
                    wait_s=wait_s, log=logger)
    save_run(cfg, run)
    return run


def _claim_queued(cfg: dict, task: str, model: str, log: Log) -> Optional[dict]:
    """认领一个排队的校准沙箱（同题同模型、状态 queued）并当场把沙箱铺好。

    排队时不占盘符：盘符池只有 Q/R/S 三个，排 5 个沙箱就爆了。
    真正用到时才 materialize，那时快照缓存已经热了，铺沙箱是亚秒级的。
    """
    for run in list_runs(cfg):
        if (run.get("status") == "queued" and run.get("task") == task
                and str(run.get("model")) == str(model)):
            meta = packs.load_meta(cfg, task)
            run["status"] = "preparing"
            save_run(cfg, run)
            try:
                sandbox.prepare(cfg, run, meta,
                                reserved=reserved_drives(cfg, exclude=run["run_id"]), log=log)
            except errors.HarnessError:
                run["status"] = "queued"
                save_run(cfg, run)
                return None
            run["calibration"] = True
            run["status"] = "ready"
            save_run(cfg, run)
            return run
    return None


def reset_sandbox(cfg: dict, run_id: str, log: Log = None) -> dict:
    """清空改动：秒级回基线（只重置沙箱，不动记录与成绩）。"""
    logger = log or (lambda m: None)
    run = get_run(cfg, run_id)
    meta = packs.load_meta(cfg, run["task"])
    if not run.get("sandbox") or not os.path.isdir(run["sandbox"]):
        raise errors.HarnessError(
            errors.E_SANDBOX_MISSING,
            "沙箱已经不在了，无法清空改动。请点「重建沙箱」。",
            str(run.get("sandbox")),
        )
    result = sandbox.reset_changes(run["sandbox"], logger)
    run["status"] = "ready"
    run["updated_at"] = util.iso_now()
    save_run(cfg, run)
    result["run_id"] = run["run_id"]
    result["sandbox"] = run["sandbox"]
    result["drive"] = run.get("drive", "")
    return result


def rebuild_sandbox(cfg: dict, task: str, run_id: str = "", log: Log = None) -> dict:
    """重建沙箱：释放盘符 → 整树删除 → 重做全流程。"""
    logger = log or (lambda m: None)
    run = get_run(cfg, run_id) if run_id else _latest_run_of_task(cfg, task)
    if run.get("task") != task:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "这个运行记录不属于该任务。", run.get("run_id"))
    meta = packs.load_meta(cfg, task)
    logger("开始重建沙箱：%s" % run["run_id"])
    sandbox.rebuild(cfg, run, meta, reserved=reserved_drives(cfg, exclude=run["run_id"]), log=logger)
    run["status"] = "ready"
    run["rounds"] = []
    save_run(cfg, run)
    return {"run_id": run["run_id"], "sandbox": run["sandbox"], "drive": run.get("drive", "")}


def _latest_run_of_task(cfg: dict, task: str) -> dict:
    for run in list_runs(cfg):
        if run.get("task") == task:
            return run
    raise errors.HarnessError(
        errors.E_RUN_NOT_FOUND,
        "这道题还没有任何运行记录，请先点「准备沙箱」。",
        task,
    )


def promote(cfg: dict, run_id: str) -> dict:
    """解锁下一轮提示词（同一沙箱继续改，不清空已有代码）。"""
    run = get_run(cfg, run_id)
    meta = packs.load_meta(cfg, run["task"])
    if run.get("revealed"):
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "这一轮已经揭晓过参考解，不能再进入下一轮。请点「清空改动」换个模型重来。",
            run_id,
        )
    current = int(run.get("attempt") or 1)
    if current >= meta["attempts"]:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "%s 是%s题，最多 %d 次机会，已经用完了。可以查看参考解或换模型重来。"
            % (run["task"], {"easy": "初级", "medium": "中级", "hard": "高级", "king": "王者"}.get(meta["tier"], meta["tier"]),
               meta["attempts"]),
            run_id,
        )
    run["attempt"] = current + 1
    run["attempts_allowed"] = meta["attempts"]
    save_run(cfg, run)
    return {"run_id": run_id, "attempt": run["attempt"], "can_promote": run["attempt"] < meta["attempts"]}


def reveal(cfg: dict, run_id: str) -> dict:
    """查看参考解：内容返回给前端，同时把这一轮标记为已揭晓（不进统计）。"""
    run = get_run(cfg, run_id)
    meta = packs.load_meta(cfg, run["task"])
    path = packs.reference_path(meta, "fix.patch")
    if not path:
        raise errors.HarnessError(
            errors.E_TASK_INVALID,
            "这道题还没有写参考解（reference\\fix.patch），无法揭晓。",
            run["task"],
        )
    try:
        with open(path, "rb") as fh:
            text = util.decode_output(fh.read())
    except OSError as exc:
        raise errors.HarnessError(errors.E_TASK_INVALID, "参考解读取失败。", str(exc))
    run["revealed"] = True
    save_run(cfg, run)
    return {
        "run_id": run_id,
        "patch": text,
        "notice": "该轮已标记为「已揭晓」，按规则不计入通过率统计。",
    }


def set_note(cfg: dict, run_id: str, note: str) -> dict:
    run = get_run(cfg, run_id)
    run["note"] = str(note or "")[:4000]
    save_run(cfg, run)
    return {"run_id": run_id, "note": run["note"]}


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def is_grading(run_id: str) -> bool:
    with _GRADING_LOCK:
        return bool(_GRADING.get(run_id))


def start_grade(cfg: dict, run_id: str) -> dict:
    """异步启动校验：立刻返回，实际跑在后台线程里（前端轮询 GET /api/runs/{id}）。"""
    run = get_run(cfg, run_id)
    with _GRADING_LOCK:
        if _GRADING.get(run_id):
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "这一轮正在校验中，请等当前校验结束。重复点击不会重复跑。",
                run_id,
            )
        _GRADING[run_id] = True
    try:
        meta = packs.load_meta(cfg, run["task"])
    except errors.HarnessError as exc:
        with _GRADING_LOCK:
            _GRADING.pop(run_id, None)
        raise exc
    if not run.get("sandbox") or not os.path.isdir(run["sandbox"]):
        with _GRADING_LOCK:
            _GRADING.pop(run_id, None)
        raise errors.HarnessError(
            errors.E_SANDBOX_MISSING,
            "沙箱还没准备好，无法校验。请先点「准备沙箱」。",
            run_id,
        )
    run["status"] = "grading"
    save_run(cfg, run)

    thread = threading.Thread(
        target=_grade_worker, args=(cfg, run_id), name="grade-%s" % run_id, daemon=True)
    thread.start()
    return {"run_id": run_id, "status": "grading"}


def _grade_worker(cfg: dict, run_id: str) -> None:
    """后台线程体：跑校验 → 落盘报告 → 更新状态。"""
    logger = RunLogger(_run_dir_of(cfg, run_id))
    run = None
    try:
        run = get_run(cfg, run_id)
        meta = packs.load_meta(cfg, run["task"])
        logger("开始校验：%s 第 %d 轮" % (run["task"], int(run.get("attempt") or 1)))
        result = grade.run_grade(cfg, run, meta, logger)
        previous = _previous_round(run, int(run.get("attempt") or 1))
        final = report_mod.build(run, meta, result, previous)

        with _STORE_LOCK:
            run_dir_path = _run_dir_of(cfg, run_id)
            util.write_json_atomic(os.path.join(run_dir_path, "report.json"), final)
            util.write_json_atomic(
                os.path.join(run_dir_path, "round-%d.json" % int(run.get("attempt") or 1)), final)
            util.write_text_atomic(
                os.path.join(run_dir_path, "diff.patch"), result.get("_diff_text") or "")
            util.write_text_atomic(
                os.path.join(run_dir_path, "notes.md"), report_mod.notes_markdown(run, meta, final))
            run["rounds"] = [r for r in (run.get("rounds") or [])
                             if r.get("attempt") != int(run.get("attempt") or 1)]
            run["rounds"].append({
                "attempt": int(run.get("attempt") or 1),
                "score": final.get("score", 0),
                "passed": final.get("passed", False),
                "invalidated": final.get("invalidated", False),
                "graded_at": final.get("graded_at"),
                "report": "round-%d.json" % int(run.get("attempt") or 1),
            })
            run["last_score"] = final.get("score", 0)
            run["last_passed"] = final.get("passed", False)
            run["status"] = "graded"
            save_run(cfg, run)
        logger("校验完成：得分 %s%s" % (
            final.get("score"), "（本轮作废：%s）" % final.get("invalid_reason") if final.get("invalidated") else ""))
    except errors.HarnessError as exc:
        logger("校验失败：%s" % exc.message)
        if run is not None:
            run["status"] = "error"
            run["last_error"] = {"code": exc.code, "message": exc.message}
            save_run(cfg, run)
    except Exception as exc:  # noqa: BLE001 - 后台线程不能让异常逃出去
        logger("校验过程出现未预期错误：%r" % exc)
        if run is not None:
            run["status"] = "error"
            run["last_error"] = {"code": errors.E_INTERNAL, "message": str(exc)}
            save_run(cfg, run)
    finally:
        with _GRADING_LOCK:
            _GRADING.pop(run_id, None)


def _run_dir_of(cfg: dict, run_id: str) -> str:
    return dir_of_run_id(cfg, run_id)


def _previous_round(run: dict, attempt: int) -> Optional[dict]:
    """取上一轮的报告（红转绿对比用）。"""
    run_dir_path = run.get("run_dir") or ""
    if not run_dir_path:
        return None
    for index in range(attempt - 1, 0, -1):
        doc = util.read_json(os.path.join(run_dir_path, "round-%d.json" % index), default=None)
        if isinstance(doc, dict):
            return doc
    return None


def load_report(cfg: dict, run: dict) -> Optional[dict]:
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run["run_id"])
    return util.read_json(os.path.join(run_dir_path, "report.json"), default=None)


def load_diff(cfg: dict, run: dict) -> str:
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run["run_id"])
    try:
        with open(os.path.join(run_dir_path, "diff.patch"), "rb") as fh:
            return util.decode_output(fh.read())
    except OSError:
        return ""


def run_view(cfg: dict, run: dict, log_tail: int = 200) -> dict:
    """GET /api/runs/{id} 的响应体：状态 / 日志 / 报告一次给全。"""
    doc = load_report(cfg, run)
    view = {
        "run_id": run["run_id"],
        "task": run.get("task"),
        "model": run.get("model"),
        "attempt": int(run.get("attempt") or 1),
        "attempts_allowed": int(run.get("attempts_allowed") or 1),
        "status": run.get("status", "unknown"),
        "sandbox": run.get("sandbox", ""),
        "drive": run.get("drive", ""),
        "baseline_digest": run.get("baseline_digest", ""),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "revealed": bool(run.get("revealed")),
        "calibration": bool(run.get("calibration")),
        "note": run.get("note", ""),
        "rounds": run.get("rounds") or [],
        "grading": is_grading(run["run_id"]),
        "last_error": run.get("last_error"),
        "report": doc,
        "log": [],
    }
    if run.get("status") in {"grading", "graded", "error"}:
        logger_path = os.path.join(run.get("run_dir") or _run_dir_of(cfg, run["run_id"]), "grade.log")
        view["log"] = _read_log_tail(logger_path, log_tail)
    return view


def _read_log_tail(path: str, limit: int) -> List[str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    lines = [ln for ln in util.decode_output(raw).splitlines() if ln.strip()]
    return lines[-limit:]


# --------------------------------------------------------------------------
# 记分板（设计文档 §16）
# --------------------------------------------------------------------------

def wilson_interval(passes: int, trials: int, z: float = _Z) -> tuple:
    """Wilson 置信区间；样本太小时退化成 [0,1]。"""
    if trials <= 0:
        return (0.0, 1.0)
    p = passes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (max(0.0, center - margin), min(1.0, center + margin))


def scoreboard(cfg: dict) -> dict:
    """行=任务、列=模型的记分板矩阵；揭晓过的轮次单列，不进主统计。"""
    runs = list_runs(cfg)
    models: List[str] = []
    tasks: List[str] = []
    for run in runs:
        if run.get("model") not in models:
            models.append(str(run.get("model")))
        if run.get("task") not in tasks:
            tasks.append(str(run.get("task")))

    for task in packs.list_tasks(cfg):
        if task["id"] not in tasks:
            tasks.append(task["id"])
    for model in cfg.get("models", []):
        mid = str(model.get("id"))
        if mid not in models:
            models.append(mid)

    cells = {}
    for task in tasks:
        row = {}
        for model in models:
            pair = [r for r in runs if r.get("task") == task and str(r.get("model")) == model]
            row[model] = _cell_stats(pair)
        cells[task] = row

    task_meta_index = {t["id"]: t for t in packs.list_tasks(cfg)}
    matrix = []
    for task in tasks:
        meta_item = task_meta_index.get(task, {})
        matrix.append({
            "task": task,
            "title": meta_item.get("title", ""),
            "tier": meta_item.get("tier", ""),
            "target_band": meta_item.get("target_band") or [],
            "cells": cells[task],
        })

    return {
        "generated_at": util.iso_now(),
        "tasks": tasks,
        "models": models,
        "matrix": matrix,
        "totals": _totals(matrix),
        "note": "单元格 = 通过轮数/总轮数（pass@1），括号内是平均得分与 Wilson 95% 区间；"
                "「已揭晓」区不计入通过率。",
    }


def _cell_stats(pair: List[dict]) -> dict:
    """一个 (任务 × 模型) 单元格的统计。"""
    scored = [r for r in pair if not r.get("revealed")]
    revealed = [r for r in pair if r.get("revealed")]
    trials = len(scored)
    first_round_passes = 0
    for run in scored:
        for rnd in run.get("rounds") or []:
            if int(rnd.get("attempt") or 0) == 1:
                if rnd.get("passed"):
                    first_round_passes += 1
                break
    any_pass = 0
    scores: List[float] = []
    for run in scored:
        best = False
        for rnd in run.get("rounds") or []:
            score = float(rnd.get("score") or 0)
            scores.append(score)
            if rnd.get("passed"):
                best = True
        if best:
            any_pass += 1
    low, high = wilson_interval(first_round_passes, trials)
    avg = round(sum(scores) / len(scores), 1) if scores else 0.0
    return {
        "trials": trials,
        "pass1": first_round_passes,
        "pass_any": any_pass,
        "pass_rate": round(first_round_passes / trials, 3) if trials else 0.0,
        "avg_score": avg,
        "ci_low": round(low, 3),
        "ci_high": round(high, 3),
        "revealed": len(revealed),
    }


def _totals(matrix: List[dict]) -> dict:
    trials = sum(cell["trials"] for row in matrix for cell in row["cells"].values())
    passes = sum(cell["pass1"] for row in matrix for cell in row["cells"].values())
    low, high = wilson_interval(passes, trials)
    return {
        "trials": trials,
        "pass1": passes,
        "pass_rate": round(passes / trials, 3) if trials else 0.0,
        "ci_low": round(low, 3),
        "ci_high": round(high, 3),
        "revealed": sum(cell["revealed"] for row in matrix for cell in row["cells"].values()),
    }


def scoreboard_csv(board: dict) -> str:
    """导出 CSV：主矩阵一块，已揭晓单列一块。"""
    tasks = board["tasks"]
    models = board["models"]
    out = ["任务,档位," + ",".join("%s(pass@1/轮数,均分,Wilson95%%)" % m for m in models)]
    for row in board["matrix"]:
        cells = []
        for model in models:
            cell = row["cells"].get(model) or {"trials": 0, "pass1": 0, "avg_score": 0,
                                              "ci_low": 0, "ci_high": 0}
            cells.append("%d/%d,%.1f,[%.2f,%.2f]" % (
                cell["pass1"], cell["trials"], cell["avg_score"], cell["ci_low"], cell["ci_high"]))
        out.append("%s,%s,%s" % (row["task"], row["tier"], ",".join(cells)))
    out.append("")
    out.append("# 已揭晓轮次（不计入通过率主统计）")
    out.append("任务,模型,已揭晓轮数")
    for row in board["matrix"]:
        for model in models:
            cell = row["cells"].get(model) or {}
            if cell.get("revealed"):
                out.append("%s,%s,%d" % (row["task"], model, cell["revealed"]))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# 模型档案
# --------------------------------------------------------------------------

def _normalize_api_mode(protocol: str, value: object, *, strict: bool = False) -> str:
    """归一化模型档案的 API 形态。

    ``protocol=openai`` 需要明确记录调用哪条公开 endpoint；旧档案没有
    ``api_mode`` 时按常见的 Chat Completions 兼容方式补齐。其它协议没有
    OpenAI endpoint，统一记为 ``native``，避免留下一个看似可用的误导值。
    ``strict`` 只用于写入接口，让非法新配置尽早失败。
    """
    if str(protocol or "").lower() != "openai":
        return "native"

    aliases = {
        "response": "responses",
        "response_api": "responses",
        "chat/completions": "chat_completions",
        "chat-completion": "chat_completions",
        "chat-completions": "chat_completions",
        "completion": "completions",
        "legacy_completions": "completions",
    }
    mode = str(value or config.DEFAULT_OPENAI_API_MODE).strip().lower()
    mode = aliases.get(mode, mode)
    allowed = config.OPENAI_API_MODES
    if mode not in allowed:
        if strict:
            raise errors.HarnessError(
                errors.E_MODEL_INVALID,
                "OpenAI 兼容协议的接口形态只能是 %s。"
                % "、".join(allowed),
                mode,
            )
        return config.DEFAULT_OPENAI_API_MODE
    return mode


def _model_view(item: dict) -> dict:
    """返回可供 API/前端消费的模型档案副本，并补齐新字段。"""
    out = dict(item)
    protocol = str(out.get("protocol") or "custom").lower()
    out["api_mode"] = _normalize_api_mode(protocol, out.get("api_mode"), strict=False)
    return out


def list_models(cfg: dict) -> List[dict]:
    return [_model_view(m) for m in cfg.get("models", []) if isinstance(m, dict)]


def upsert_model(cfg: dict, payload: dict) -> dict:
    """新增或更新模型档案；只保存脱敏后的 Key。"""
    model_id = util.sanitize_id(payload.get("id"))
    if not model_id:
        raise errors.HarnessError(errors.E_MODEL_INVALID, "模型档案需要一个 id（英文标识即可）。")
    protocol = str(payload.get("protocol") or "custom").lower()
    if protocol not in config.MODEL_PROTOCOLS:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID,
            "协议只能是 %s 之一。" % "、".join(config.MODEL_PROTOCOLS),
            str(payload.get("protocol")),
        )
    api_mode = _normalize_api_mode(protocol, payload.get("api_mode"), strict=True)
    models = list_models(cfg)
    entry = {
        "id": model_id,
        "protocol": protocol,
        "api_mode": api_mode,
        "base_url": str(payload.get("base_url") or ""),
        "model": str(payload.get("model") or ""),
        "key_masked": str(payload.get("key_masked") or ""),
        "note": str(payload.get("note") or "")[:500],
    }
    for index, item in enumerate(models):
        if str(item.get("id")) == model_id:
            models[index] = entry
            break
    else:
        models.append(entry)
    config.update_models(models)
    return entry


def delete_model(cfg: dict, model_id: str) -> dict:
    models = list_models(cfg)
    remaining = [m for m in models if str(m.get("id")) != str(model_id)]
    if len(remaining) == len(models):
        raise errors.HarnessError(
            errors.E_MODEL_NOT_FOUND, "找不到模型档案 %s，删除失败。" % model_id, str(model_id))
    config.update_models(remaining)
    return {"id": model_id, "deleted": True, "remaining": len(remaining)}
