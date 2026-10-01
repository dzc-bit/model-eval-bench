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
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from . import chat, config, errors, grade, keyring, packs, report as report_mod, sandbox, util

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
    """扫描全部运行记录（按时间倒序）。缺目录/坏文件都不影响其它记录。

    只下钻规范布局 runs/<task>/<model>/<时间戳>/：根层的 blind（出题侧工具与盲测）
    和任何层的 _ 开头目录（_quarantine 整理隔离区等）一律不下钻——曾经 os.walk
    一锅端，把隔离区里的测试夹具运行当成真实成绩，记分板因此冒出大量幽灵档案。
    """
    out: List[dict] = []
    root = cfg["runs_root"]
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else len(rel.split(os.sep))
        if depth == 0:
            dirnames[:] = [d for d in dirnames if not d.startswith("_") and d != "blind"]
        else:
            dirnames[:] = [d for d in dirnames if not d.startswith("_")]
        if depth >= 3:
            dirnames[:] = []  # 时间戳层之下不再有运行记录
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
    """兼容旧调用方；文件夹沙箱不需要全局盘符预留。"""
    del cfg, exclude
    return {}


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

def create_run(cfg: dict, task: str, model: str, attempt: int = 1,
               claim_queued: bool = True, wait_s: float = 0.0,
               log: Log = None,
               cancel_event: threading.Event | None = None) -> dict:
    """准备一轮新运行：读题包 → 建记录 → 准备沙箱。

    若有同一题同模型的排队中校准沙箱（§6.4 盲测排队），直接认领一个，
    这样校准排了 N 个名额后，真正使用时才创建文件夹沙箱。

    :param wait_s: 保留旧调用签名；文件夹沙箱不等待盘符。
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
    if cancel_event is not None and cancel_event.is_set():
        raise errors.HarnessError(errors.E_RUN_CANCELLED, "批次已取消，未创建新的运行记录。")
    # run["task"] 用 meta["id"] 归一：Windows 文件系统大小写不敏感，"t1-01" 与
    # "T1-01" 都能找到题包，但按原串分组成绩会裂成两行、排行榜严格比较会漏记录。
    task = meta["id"]
    with _STORE_LOCK:
        # run_id 查重、建目录、首次落盘必须同一把锁：ThreadingHTTPServer 下两个线程
        # 同秒为同「题×模型」建 run，锁外查重会双双得到同一 run_id → 同一目录互相
        # 覆盖 → 沙箱互删、成绩串档。
        run_id = _new_run_id(cfg, task, model)
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

    if cancel_event is not None and cancel_event.is_set():
        run["status"] = "cancelled"
        run["last_error"] = {
            "code": errors.E_RUN_CANCELLED,
            "message": "批次已取消，未开始准备沙箱。",
        }
        save_run(cfg, run)
        raise errors.HarnessError(errors.E_RUN_CANCELLED, "批次已取消，未开始准备沙箱。")
    claimed = _claim_queued(cfg, task, model, logger, attempt) if claim_queued else None
    if claimed:
        logger("认领了一个排队中的校准沙箱：%s" % claimed["run_id"])
        target_dir = run["run_dir"]
        claimed_dir = claimed.get("run_dir") or dir_of_run_id(cfg, claimed["run_id"])
        # The queued run has already been prepared in its own directory. Keep that
        # directory so its sandbox and local dependency baseline stay paired with
        # the run record; the newly-created directory is only an empty placeholder.
        if util.norm(target_dir) != util.norm(claimed_dir):
            util.remove_tree(target_dir)
        claimed["calibration"] = True
        claimed["status"] = "ready"
        claimed["updated_at"] = util.iso_now()
        save_run(cfg, claimed)
        return claimed

    prepare_kwargs = {
        "reserved": reserved_drives(cfg, exclude=run_id),
        "wait_s": wait_s,
        "log": logger,
    }
    if cancel_event is not None:
        prepare_kwargs["cancel_event"] = cancel_event
    try:
        sandbox.prepare(cfg, run, meta, **prepare_kwargs)
    except errors.HarnessError as exc:
        if exc.code == errors.E_RUN_CANCELLED:
            run["status"] = "cancelled"
            run["last_error"] = {"code": exc.code, "message": exc.message}
            save_run(cfg, run)
        raise
    save_run(cfg, run)
    return run


def _claim_queued(cfg: dict, task: str, model: str, log: Log,
                  attempt: int = 1) -> Optional[dict]:
    """认领一个排队的校准沙箱（同题同模型、状态 queued）并当场把沙箱铺好。

    排队时只建记录，真正用到时才创建目录；耗时取决于快照和依赖体积。
    attempt 不匹配不认领：第 2/3 轮请求不能静默换成第 1 轮的沙箱与提示词级别。
    """
    for run in list_runs(cfg):
        if (run.get("status") == "queued" and run.get("task") == task
                and str(run.get("model")) == str(model)):
            if int(run.get("attempt") or 1) != attempt:
                continue
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
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "模型正在处理这次对话，暂时不能清空沙箱。请等当前消息完成后重试。",
                run_id,
            )
        run = get_run(cfg, run_id)
        if run.get("status") == "grading":
            raise errors.HarnessError(errors.E_RUN_BUSY, "校验正在进行，暂时不能清空沙箱。", run_id)
        if run.get("cancel_requested") or run.get("status") == "cancelled":
            raise errors.HarnessError(errors.E_RUN_CANCELLED, "这一轮已被取消，不能清空沙箱。", run_id)
        if not run.get("sandbox") or not os.path.isdir(run["sandbox"]):
            raise errors.HarnessError(
                errors.E_SANDBOX_MISSING,
                "沙箱已经不在了，无法清空改动。请点「重建沙箱」。",
                str(run.get("sandbox")),
            )
        meta = packs.load_meta(cfg, run["task"])
        dependencies_source = (
            sandbox.node_modules_baseline(run) if sandbox.needs_frontend(meta) else ""
        )
        if sandbox.needs_frontend(meta) and not dependencies_source:
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "node_modules 本地基线不存在，无法安全清空依赖改动。请重建沙箱。",
                run_id,
            )
        result = sandbox.reset_changes(run["sandbox"], logger, dependencies_source)
        run["status"] = "ready"
        run["updated_at"] = util.iso_now()
        save_run(cfg, run)
        result["run_id"] = run["run_id"]
        result["sandbox"] = run["sandbox"]
        result["drive"] = run.get("drive", "")
        return result


def rebuild_sandbox(cfg: dict, task: str, run_id: str = "", log: Log = None) -> dict:
    """重建沙箱：回收目录 → 重做全流程。"""
    logger = log or (lambda m: None)
    target_id = run_id or ""
    # 无 run_id 时先找记录，再用真实 ID 获取同一把会话锁。
    if not target_id:
        target_id = _latest_run_of_task(cfg, task)["run_id"]
    with chat.exclusive(target_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "模型正在处理这次对话，暂时不能重建沙箱。请等当前消息完成后重试。",
                target_id,
            )
        run = get_run(cfg, target_id)
        if run.get("task") != task:
            raise errors.HarnessError(errors.E_BAD_REQUEST, "这个运行记录不属于该任务。", run.get("run_id"))
        if run.get("status") == "grading":
            raise errors.HarnessError(errors.E_RUN_BUSY, "校验正在进行，暂时不能重建沙箱。", target_id)
        if run.get("cancel_requested") or run.get("status") == "cancelled":
            raise errors.HarnessError(errors.E_RUN_CANCELLED, "这一轮已被取消，不能重建沙箱。", target_id)
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
    # get→改→save 与评分线程收尾的写回交错会丢更新，统一走会话锁串行化
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型或评分正在使用这一轮，稍后再试。", run_id)
        return _promote_locked(cfg, run_id)


def _promote_locked(cfg: dict, run_id: str) -> dict:
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
    # 同一个沙箱继续改（模型已写的代码保留），对话要能接着进行：
    # 评分后的轮次是冻结态，不切回 ready 的话输入框会一直禁用
    run["status"] = "ready"
    save_run(cfg, run)
    return {"run_id": run_id, "attempt": run["attempt"], "can_promote": run["attempt"] < meta["attempts"]}


def reveal(cfg: dict, run_id: str) -> dict:
    """查看参考解：内容返回给前端，同时把这一轮标记为已揭晓（不进统计）。"""
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型或评分正在使用这一轮，稍后再试。", run_id)
        return _reveal_locked(cfg, run_id)


def _reveal_locked(cfg: dict, run_id: str) -> dict:
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


def reopen(cfg: dict, run_id: str) -> dict:
    """把已校验的一轮退回「可继续对话」，并作废本轮分数。

    误点一次校验不该毁掉一次尝试：本轮分数标成 voided（不进 pass@k 与均分），
    报告文件留在原地当证据；模型改完再校验会追加新的一轮。
    已揭晓参考解的轮次不允许重开——那等于给了无限次看答案后重试。
    """
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "这一轮正在被其它操作使用，等它结束再继续对话。",
                run_id,
            )
        run = get_run(cfg, run_id)
        if run.get("cancel_requested") or run.get("status") == "cancelled":
            raise errors.HarnessError(
                errors.E_RUN_CANCELLED,
                "这一轮已被批次取消，不能重开。",
                run_id,
            )
        if run.get("revealed"):
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "这一轮已经揭晓过参考解，不能再继续对话重算成绩。",
                run_id,
            )
        if run.get("status") != "graded":
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "这一轮还没有校验结果，不需要重开；直接在内置对话里发送即可。",
                run_id,
            )
        voided = 0
        for rnd in reversed(run.get("rounds") or []):
            if not rnd.get("voided"):
                rnd["voided"] = True
                rnd["voided_at"] = util.iso_now()
                voided += 1
                break
        run["status"] = "ready"
        run["last_score"] = None
        run["last_passed"] = None
        save_run(cfg, run)
    return {
        "run_id": run_id,
        "status": run["status"],
        "voided_rounds": voided,
        "notice": "本轮分数已作废，不计入通过率与均分；模型改完后重新校验会记作新一轮结果。",
    }


def set_note(cfg: dict, run_id: str, note: str) -> dict:
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型或评分正在使用这一轮，稍后再试。", run_id)
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
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "模型正在处理这次对话，请等当前消息完成后再启动校验。",
                run_id,
            )
        run = get_run(cfg, run_id)
        if run.get("cancel_requested") or run.get("status") == "cancelled":
            raise errors.HarnessError(
                errors.E_RUN_CANCELLED,
                "这一轮已被批次取消，不能再启动校验。",
                run_id,
            )
        with _GRADING_LOCK:
            if _GRADING.get(run_id) or run.get("status") == "grading":
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
    run_lock = chat.lock_for(run_id)
    run_lock.acquire()
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
        run_lock.release()


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
    """改动正文：优先评分产物 diff.patch；还没跑过校验时回退到沙箱实时改动。

    实时回退用与评分同一套全树比对语义（collect_changes + build_diff_text），
    所以模型 write_file 之后、评分之前，工作台也能看到真实改动。
    """
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run["run_id"])
    try:
        with open(os.path.join(run_dir_path, "diff.patch"), "rb") as fh:
            return util.decode_output(fh.read())
    except OSError:
        pass
    try:
        changes = grade.collect_changes(cfg, run)
        return grade.build_diff_text(cfg, run, changes, limit=50)["text"]
    except (errors.HarnessError, OSError, ValueError, KeyError):
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
        "chat_busy": chat.send_active(run["run_id"]),
        # 模型是否已经回复过：校验按钮的前置条件，没动手时点了只会判 0
        "model_acted": chat.has_model_reply(run),
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
        model = str(run.get("model") or "")
        task = str(run.get("task") or "")
        if not model or not task:
            continue  # 缺 task/model 的坏记录不建幽灵行列
        if model not in models:
            models.append(model)
        if task not in tasks:
            tasks.append(task)

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


def _archive_run_record(cfg: dict, run: dict) -> str:
    """把一条运行记录整目录移入隔离区并清理沙箱副本，返回归档路径。

    模型改动已存档在记录目录的 diff.patch 里，沙箱是可重建的派生数据。
    """
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run["run_id"])
    if not os.path.isdir(run_dir_path):
        raise errors.HarnessError(
            errors.E_RUN_NOT_FOUND,
            "运行记录目录不存在，可能已被删除。",
            str(run.get("run_id") or ""),
        )
    quarantine_root = os.path.join(cfg["runs_root"], "_quarantine", "manual-deletes")
    util.ensure_dir(quarantine_root)
    target = os.path.join(quarantine_root, str(run.get("run_id") or "run"))
    suffix = 2
    while os.path.exists(target):
        target = os.path.join(quarantine_root, "%s-%d" % (run.get("run_id"), suffix))
        suffix += 1
    shutil.move(run_dir_path, target)
    sandbox_path = str(run.get("sandbox") or "")
    if sandbox_path and os.path.isdir(sandbox_path):
        util.remove_tree(sandbox_path)
    return target


def delete_run(cfg: dict, run_id: str) -> dict:
    """删除一条运行记录。

    遵循工作区的删除纪律：记录目录**整目录移入** runs/_quarantine/manual-deletes/
    （list_runs 不扫隔离区，统计里立刻消失；要恢复手工移回原位即可），
    关联沙箱副本用 util.remove_tree 清掉。对话或校验进行中拒绝删除。
    """
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "这一轮正在对话，等当前消息处理完再删除。",
                run_id,
            )
        run = get_run(cfg, run_id)
    if _GRADING.get(run_id) or run.get("status") == "grading":
        raise errors.HarnessError(
            errors.E_RUN_BUSY,
            "这一轮正在校验中，等校验结束后再删除。",
            run_id,
        )
    archived_to = _archive_run_record(cfg, run)
    return {"run_id": run_id, "deleted": True, "archived_to": archived_to}


def _live_rounds(run: dict) -> List[dict]:
    """计入统计的轮次：被「继续对话」作废掉的那些不算成绩，也不进均分。"""
    return [rnd for rnd in (run.get("rounds") or []) if isinstance(rnd, dict) and not rnd.get("voided")]


def _cell_stats(pair: List[dict]) -> dict:
    """一个 (任务 × 模型) 单元格的统计。

    作废轮（invalidated：越界/回归/校验出错）不计通过、不计分，与排行榜同口径——
    旧实现把作废轮的 0 分计入平均、把作废轮的 passed 计入通过率，两个视图给出
    互相矛盾的结论。
    """
    def _score(rnd: dict) -> float:
        try:
            return float(rnd.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    scored = [r for r in pair if not r.get("revealed")]
    revealed = [r for r in pair if r.get("revealed")]
    trials = len(scored)
    first_round_passes = 0
    for run in scored:
        for rnd in _live_rounds(run):
            if int(rnd.get("attempt") or 0) == 1:
                if rnd.get("passed") and not rnd.get("invalidated"):
                    first_round_passes += 1
                break
    any_pass = 0
    scores: List[float] = []
    for run in scored:
        best = False
        # 作废轮（voided，用户点「继续对话」放弃的）与越界轮（invalidated，
        # 改了测试/配置被拦下的）都不计分、不算通过——两套语义都要排掉。
        for rnd in _live_rounds(run):
            if rnd.get("invalidated"):
                continue
            scores.append(_score(rnd))
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
        # 供记分板「删除记录」入口列出这一格背后的运行
        "run_ids": [str(r.get("run_id") or "") for r in pair if r.get("run_id")],
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


def _timestamp_seconds(value: object) -> Optional[float]:
    """把运行记录时间转为可比较的秒数；兼容带 Z 和无时区的旧记录。"""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def task_leaderboard(cfg: dict, task_id: str) -> dict:
    """返回该题未揭晓、未作废且通过的记录，按轮数和用时升序。"""
    meta = packs.load_meta(cfg, task_id)
    entries = []
    for run in list_runs(cfg):
        if run.get("task") != meta["id"] or run.get("revealed"):
            continue
        passed_rounds = []
        for result in run.get("rounds") or []:
            if not isinstance(result, dict) or result.get("passed") is not True or result.get("invalidated"):
                continue
            try:
                attempt = int(result.get("attempt") or 0)
            except (TypeError, ValueError):
                continue
            if attempt > 0:
                passed_rounds.append((attempt, result))
        if not passed_rounds:
            continue

        attempt, result = min(passed_rounds, key=lambda pair: pair[0])
        created_at = run.get("created_at")
        completed_at = result.get("graded_at")
        start_s = _timestamp_seconds(created_at)
        finish_s = _timestamp_seconds(completed_at)
        duration_s = max(0.0, finish_s - start_s) if start_s is not None and finish_s is not None else None
        try:
            score = float(result.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        entries.append({
            "run_id": run.get("run_id", ""),
            "model": str(run.get("model") or ""),
            "rounds": attempt,
            "duration_s": round(duration_s, 3) if duration_s is not None else None,
            "completed_at": completed_at,
            "score": score,
        })

    def sort_key(entry: dict) -> tuple:
        completed_s = _timestamp_seconds(entry.get("completed_at"))
        duration = entry.get("duration_s")
        return (
            entry["rounds"],
            duration if duration is not None else float("inf"),
            completed_s if completed_s is not None else float("inf"),
            entry["model"].casefold(),
            entry["run_id"],
        )

    entries.sort(key=sort_key)
    for index, entry in enumerate(entries, start=1):
        entry["rank"] = index
    return {"task": meta["id"], "title": meta["title"], "entries": entries}


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


#: API 回传给浏览器的模型档案字段白名单。config.json 是手工可编辑的，
#: 有人把真实密钥直接写进档案字段时，不能原样回显。
#: 2026-10-01 重构：去掉 key_env（密钥改按供应商存，不再有自定义变量名字段），
#: 加入 provider / 容量字段。
_MODEL_VIEW_FIELDS = ("id", "name", "provider_id", "provider_name", "qualified_id",
                      "protocol", "api_mode", "base_url", "model",
                      "context_window", "max_tokens", "key_masked", "note")


def _model_record(item: dict) -> dict:
    """写回 config.json 用的档案：只保留可持久化字段，不带只读诊断结果。

    诊断字段（``key_candidates`` 等）是「服务端此刻的环境变量状态」，
    落盘就成了过期事实，还会污染手工维护的 config.json。
    """
    out = {k: item.get(k) for k in _MODEL_VIEW_FIELDS if k in item}
    protocol = str(out.get("protocol") or "custom").lower()
    out["api_mode"] = _normalize_api_mode(protocol, out.get("api_mode"), strict=False)
    return out


def _model_view(item: dict) -> dict:
    """返回可供 API/前端消费的模型档案副本（字段白名单），并补齐新字段。

    补的四个字段都是只读诊断口径，且**只含环境变量名**：优先级由
    ``chat.key_candidates`` 单点定义，前端不再自己复刻一遍顺序；
    密钥取值本身永不离开服务端。
    """
    out = {k: item.get(k) for k in _MODEL_VIEW_FIELDS if k in item}
    protocol = str(out.get("protocol") or "custom").lower()
    out["api_mode"] = _normalize_api_mode(protocol, out.get("api_mode"), strict=False)
    status = chat.key_status(item)
    out["key_candidates"] = status["candidates"]
    out["key_env_effective"] = status["effective"]
    out["key_present"] = status["present"]
    # ready = 协议接得住 + base_url 显式填了 + 服务端确实读到了密钥。
    # 只代表「可以开始检测」，不代表服务商那边一定通（那要 doctor 的 reach 档）。
    out["ready"] = bool(chat.is_supported_model(item)
                        and chat.has_usable_base_url(item)
                        and status["present"])
    return out


def list_models(cfg: dict) -> List[dict]:
    """档案列表（含只读诊断字段，供 API/前端用）。"""
    return [_model_view(m) for m in cfg.get("models", []) if isinstance(m, dict)]


def _model_records(cfg: dict) -> List[dict]:
    """档案列表（仅可持久化字段，供写回 config.json 用）。"""
    return [_model_record(m) for m in cfg.get("models", []) if isinstance(m, dict)]


def _provider_view(provider: dict, cfg: dict) -> dict:
    """供应商条目的对外视图：原始字段 + 只读诊断（密钥是否存在、能否使用）。

    诊断只回传**是否存在**与候选变量名，取值永不离开服务端。
    """
    out = {k: provider.get(k) for k in config.PROVIDER_FIELDS if k in provider}
    out["models"] = [
        {k: m.get(k) for k in config.PROVIDER_MODEL_FIELDS if k in m}
        for m in (provider.get("models") or [])
    ]
    # 密钥状态按供应商算：本机密钥文件 + 环境变量候选
    probe = {"provider_id": provider.get("id"), "protocol": provider.get("protocol")}
    stored = bool(keyring.get_key(str(provider.get("id") or "")))
    status = chat.key_status(probe)
    out["key_present"] = stored or status["present"]
    out["key_stored"] = stored
    out["key_candidates"] = status["candidates"]
    out["key_masked"] = str(provider.get("key_masked") or "")
    # ready = 协议接得住 + 有端点 + 有模型 + 服务端确实读到了密钥。
    # 只代表「可以开始检测」，不代表服务商那边一定通（那要 doctor 的 reach 档）。
    first = (provider.get("models") or [{}])[0]
    out["ready"] = bool(
        str(provider.get("base_url") or "").strip()
        and (provider.get("models") or [])
        and chat.has_usable_base_url(first)
        and out["key_present"]
    )
    return out


def list_providers(cfg: dict) -> dict:
    """供应商列表（嵌套结构，供「模型档案」页编辑）。"""
    providers = [_provider_view(p, cfg) for p in cfg.get("providers", []) if isinstance(p, dict)]
    return {
        "providers": providers,
        # 老配置被读时迁移过：界面据此提示「已按端点合并，保存后生效」
        "migrated": bool(cfg.get("providers_migrated")),
    }


def _normalize_provider_payload(payload: dict) -> dict:
    """校验并归一一个供应商条目（含它的模型清单）。

    逐模型容量与输出上限留空就继承供应商的 default_*，与 DSH 的
    「exact model capacity wins, otherwise adapter default」同一条兜底链。
    """
    pid = util.sanitize_id(payload.get("id"))
    if not pid:
        raise errors.HarnessError(errors.E_MODEL_INVALID, "供应商需要一个 id（英文标识即可）。")
    protocol = str(payload.get("protocol") or "openai").lower()
    if protocol not in config.MODEL_PROTOCOLS:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID,
            "协议只能是 %s 之一。" % "、".join(config.MODEL_PROTOCOLS),
            str(payload.get("protocol")),
        )
    api_mode = _normalize_api_mode(protocol, payload.get("api_mode"), strict=True)
    base_url = str(payload.get("base_url") or "").strip()
    if not base_url:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID, "供应商需要填写接口地址（base_url）。", pid)

    def _positive_int(value, field):
        if value in (None, ""):
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise errors.HarnessError(
                errors.E_MODEL_INVALID, "%s 必须是正整数。" % field, str(value))
        if n <= 0:
            raise errors.HarnessError(
                errors.E_MODEL_INVALID, "%s 必须是正整数。" % field, str(value))
        return n

    default_ctx = _positive_int(payload.get("default_context_window"), "默认上下文窗口")         or config.DEFAULT_CONTEXT_WINDOW
    default_max = _positive_int(payload.get("default_max_tokens"), "默认输出上限")         or config.DEFAULT_MAX_TOKENS

    models = []
    seen = set()
    for raw in payload.get("models") or []:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or "").strip()
        if not mid:
            continue
        if mid in seen:
            raise errors.HarnessError(
                errors.E_MODEL_INVALID, "同一个供应商下模型 id 不能重复：%s。" % mid, mid)
        seen.add(mid)
        models.append({
            "id": mid,
            "name": str(raw.get("name") or mid).strip() or mid,
            "context_window": _positive_int(raw.get("context_window"), "上下文窗口") or default_ctx,
            "max_tokens": _positive_int(raw.get("max_tokens"), "输出上限") or default_max,
            "note": str(raw.get("note") or "")[:500],
        })
    if not models:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID, "供应商至少要有一个模型。", pid)

    return {
        "id": pid,
        "display_name": str(payload.get("display_name") or pid).strip() or pid,
        "protocol": protocol,
        "api_mode": api_mode,
        "base_url": base_url,
        "default_context_window": default_ctx,
        "default_max_tokens": default_max,
        "note": str(payload.get("note") or "")[:500],
        "models": models,
    }


def upsert_provider(cfg: dict, payload: dict) -> dict:
    """新增或更新供应商（连同它的模型清单）。

    明文密钥只进本机密钥文件（按供应商 id 存），config.json 只存脱敏值。
    """
    entry = _normalize_provider_payload(payload)
    pid = entry["id"]
    api_key = str(payload.get("api_key") or "").strip()
    previous_id = util.sanitize_id(payload.get("previous_id"))

    providers = [dict(p) for p in cfg.get("providers", [])]
    if previous_id and previous_id != pid:
        # 改编号：旧供应商连同它的密钥一起搬走，不留重复
        providers = [p for p in providers if str(p.get("id")) != previous_id]
        keyring.rename_key(previous_id, pid)
    if api_key:
        entry["key_masked"] = keyring.set_key(pid, api_key)
    else:
        # 留空表示「不改已存密钥」：把原有的脱敏值带过去，否则界面上会显示成没配。
        old = next((p for p in providers if str(p.get("id")) == pid), None)
        if old and old.get("key_masked"):
            entry["key_masked"] = old["key_masked"]

    for index, item in enumerate(providers):
        if str(item.get("id")) == pid:
            providers[index] = entry
            break
    else:
        providers.append(entry)

    config.update_providers(providers)
    return entry


def delete_provider(cfg: dict, provider_id: str, with_runs: bool = False) -> dict:
    """删除供应商（连同已存密钥与其下所有模型）；with_runs=True 时一并隔离运行记录。

    记分板的档案芯片随「供应商 + 名下记录」一起消失；正被对话/校验占用的
    运行记录会跳过并列入 skipped_busy，不阻塞整体删除。
    """
    providers = [dict(p) for p in cfg.get("providers", [])]
    target = next((p for p in providers if str(p.get("id")) == str(provider_id)), None)
    if target is None:
        raise errors.HarnessError(
            errors.E_MODEL_NOT_FOUND, "找不到供应商 %s，删除失败。" % provider_id, str(provider_id))
    remaining = [p for p in providers if str(p.get("id")) != str(provider_id)]
    config.update_providers(remaining)
    keyring.remove_key(provider_id)

    # 名下运行记录按「限定名前缀」或「老裸 id」两种形态匹配：
    # 老记录里 model 字段是档案 id，重构后是 provider::model。
    owned_prefix = "%s::" % provider_id
    legacy_ids = {str(m.get("id")) for m in (target.get("models") or [])}
    legacy_ids.add(str(provider_id))

    removed_runs: List[str] = []
    skipped_busy: List[str] = []
    if with_runs:
        for run in list_runs(cfg):
            model = str(run.get("model") or "")
            if not (model.startswith(owned_prefix) or model in legacy_ids):
                continue
            rid = str(run.get("run_id") or "")
            if not rid or chat.send_active(rid):
                if rid:
                    skipped_busy.append(rid)
                continue
            with chat.exclusive(rid, blocking=False) as acquired:
                if not acquired:
                    skipped_busy.append(rid)
                    continue
                try:
                    removed_runs.append(_archive_run_record(cfg, run))
                except errors.HarnessError as exc:
                    if exc.code == errors.E_RUN_NOT_FOUND:
                        continue  # 记录目录已不在，视为已处理
                    raise
    return {
        "id": provider_id,
        "deleted": True,
        "remaining": len(remaining),
        "removed_runs": removed_runs,
        "skipped_busy": skipped_busy,
    }


def discover_models(cfg: dict, payload: dict) -> dict:
    """问端点「你能提供哪些模型」，返回候选清单（不落盘）。

    对齐 DSH 的 discovery：候选只是**可采纳的建议**，什么被服务始终由配置决定。
    已在本供应商清单里的模型标 ``configured``，界面据此只显示可新增的。
    """
    provider_id = util.sanitize_id(payload.get("id"))
    provider = next(
        (p for p in cfg.get("providers", []) if str(p.get("id")) == str(provider_id)), None)
    # 支持"还没保存就试拉"：表单里现填的 base_url / api_key 优先
    base_url = str(payload.get("base_url") or (provider or {}).get("base_url") or "").strip()
    if not base_url:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID, "先填接口地址，再拉取模型列表。", provider_id)
    protocol = str(payload.get("protocol") or (provider or {}).get("protocol") or "openai").lower()
    typed_key = str(payload.get("api_key") or "").strip()

    candidates = chat.list_remote_models(
        base_url=base_url, protocol=protocol, provider_id=provider_id, api_key=typed_key)

    configured = {str(m.get("id")) for m in ((provider or {}).get("models") or [])}
    for item in candidates:
        item["configured"] = str(item.get("id")) in configured
    return {"provider_id": provider_id, "models": candidates}




def delete_model(cfg: dict, model_id: str, with_runs: bool = False) -> dict:
    """删除模型档案（连同已存密钥）；with_runs=True 时把名下运行记录一并移入隔离区。

    记分板的档案芯片随「档案本身 + 名下记录」一起消失；正被对话/校验占用的
    运行记录会跳过并列入 skipped_busy，不阻塞整体删除。
    """
    models = _model_records(cfg)
    remaining = [m for m in models if str(m.get("id")) != str(model_id)]
    if len(remaining) == len(models):
        raise errors.HarnessError(
            errors.E_MODEL_NOT_FOUND, "找不到模型档案 %s，删除失败。" % model_id, str(model_id))
    config.update_models(remaining)
    keyring.remove_key(model_id)
    removed_runs: List[str] = []
    skipped_busy: List[str] = []
    if with_runs:
        for run in list_runs(cfg):
            if str(run.get("model") or "") != str(model_id):
                continue
            rid = str(run.get("run_id") or "")
            if not rid or chat.send_active(rid):
                if rid:
                    skipped_busy.append(rid)
                continue
            with chat.exclusive(rid, blocking=False) as acquired:
                if not acquired:
                    skipped_busy.append(rid)
                    continue
                try:
                    removed_runs.append(_archive_run_record(cfg, run))
                except errors.HarnessError as exc:
                    if exc.code == errors.E_RUN_NOT_FOUND:
                        continue  # 记录目录已不在，视为已处理
                    raise
    return {
        "id": model_id,
        "deleted": True,
        "remaining": len(remaining),
        "removed_runs": removed_runs,
        "skipped_busy": skipped_busy,
    }
