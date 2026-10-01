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
            # 本轮起点：created_at 是这条记录被建出来的时刻，重建/清空都不会动它，
            # 所以"本轮跑了多久"必须另起一个字段，否则时间会跨模型累加。
            "round_started_at": util.iso_now(),
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
        # 回到基线就是新纪元：旧报告、旧改动证据和**旧对话**一起归档。
        # 对话不归档会被 _model_history 原样喂给下一个模型，等于共享答案。
        _archive_epoch(run.get("run_dir") or _run_dir_of(cfg, run["run_id"]),
                       int(run.get("attempt") or 1))
        _void_rounds(run, "清空改动后沙箱回到基线，旧成绩不再描述当前代码")
        run["last_score"] = None
        run["last_passed"] = None
        run["round_started_at"] = util.iso_now()
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
        # 重建是新纪元：旧对话、旧改动证据与旧轮次记录整体归档。轮次只作废不删除
        # （记录目录与 run_id 都不变，删了就没法复盘上一个模型到底做了什么）。
        _archive_epoch(run.get("run_dir") or _run_dir_of(cfg, run["run_id"]),
                       int(run.get("attempt") or 1))
        _void_rounds(run, "重建沙箱：上一个模型的对话与成绩不再计入这一轮")
        run["last_score"] = None
        run["last_passed"] = None
        run["round_started_at"] = util.iso_now()
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
    # 只有"当前这一轮真的考过"才允许进入下一轮：前端 canPromote 一直是这么假设的，
    # 后端不拦的话连点或直调 API 就能跳级，白拿一次提示词等级。
    graded_attempts = {int(r.get("attempt") or 0) for r in (run.get("rounds") or [])
                       if not r.get("voided")}
    if current not in graded_attempts:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "第 %d 轮还没有校验结果，不能进入下一轮。先运行校验，或点「作废本轮成绩」重来。" % current,
            run_id,
        )
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
    # 新一轮的计时起点：不重置的话，第 2 轮的"用时"里含第 1 轮的对话时间，
    # 排行榜会把两轮的工作量算成一个更快的成绩。
    run["round_started_at"] = util.iso_now()
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


def _archive_report(run_dir_path: str) -> str:
    """把当前报告挪成 report-discarded-<时间戳>.json，返回新路径（没有报告则空串）。

    只挪不删：那是模型真实考出来的一次结果，评完就销毁会让复盘无从下手。
    """
    report_path = os.path.join(run_dir_path, "report.json")
    if not os.path.isfile(report_path):
        return ""
    stamp = "".join(ch for ch in util.iso_now() if ch.isdigit())
    target = os.path.join(run_dir_path, "report-discarded-%s.json" % stamp)
    os.replace(report_path, target)
    return target


#: 回基线（清空改动 / 重建沙箱）时要整体归档的证据。
#: chat.jsonl 必须在列：`chat._model_history()` 全量回放它，不归档就等于把
#: 上一个模型的提示词、回答、工具原文结果和思考一起喂给下一个模型。
EPOCH_ARTIFACTS = ("report.json", "diff.patch", "notes.md", "grade.log", "chat.jsonl")


def _archive_artifacts(run_dir_path: str, names: list, label: str = "epoch") -> str:
    """把列出的文件挪进 epochs/<时间戳>/，返回归档目录（没有可挪的文件则空串）。"""
    present = [n for n in names if os.path.isfile(os.path.join(run_dir_path, n))]
    if not present:
        return ""
    stamp = "".join(ch for ch in util.iso_now() if ch.isdigit())
    target = os.path.join(run_dir_path, "epochs", "%s-%s" % (stamp, label))
    suffix = 1
    while os.path.exists(target):        # 同一秒内连续两次归档不能互相覆盖
        suffix += 1
        target = os.path.join(run_dir_path, "epochs", "%s-%s-%d" % (stamp, label, suffix))
    util.ensure_dir(target)
    for name in present:
        os.replace(os.path.join(run_dir_path, name), os.path.join(target, name))
    return target


def _archive_epoch(run_dir_path: str, attempt: int = 0) -> str:
    """把这一"纪元"的全部产物挪进 epochs/，返回归档目录（无可归档则空串）。

    纪元的边界就是"沙箱回到基线"：从这一刻起沙箱是全新的，之前那份对话与
    改动既不该再展示给下一个模型，也不该再算成它的成果。
    """
    if not os.path.isdir(run_dir_path):
        return ""
    names = list(EPOCH_ARTIFACTS)
    if attempt:
        names.append("round-%d.json" % attempt)
    return _archive_artifacts(run_dir_path, names, label="epoch")


def _void_rounds(run: dict, reason: str) -> int:
    """把还没作废的轮次全部标成作废（不删：它们仍是历史，只是不再算分）。"""
    count = 0
    for rnd in run.get("rounds") or []:
        if not rnd.get("voided"):
            rnd["voided"] = True
            rnd["voided_at"] = util.iso_now()
            rnd["void_reason"] = reason
            count += 1
    return count


def reopen(cfg: dict, run_id: str) -> dict:
    """作废本轮成绩，退回「可继续对话」的状态。

    误点一次校验不该毁掉一次尝试：本轮分数标成 voided（不进 pass@k 与均分），
    报告挪成 report-discarded-*.json 留在原地当证据；模型改完再校验会追加新的一轮。
    已揭晓参考解的轮次不允许重开——那等于给了无限次看答案后重试。

    除 graded 之外还接受「状态早已退回 ready、目录里却还挂着旧报告」这种形态：
    轮次记账是后补的，那批 run 的 rounds 为空、last_score 为 None，报告却一直
    被 run_view 读出来，不放开的话面板上永远挂着一个作废不掉的 0 分。
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
        if run.get("status") == "grading":
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "校验正在进行，等它出结果再作废本轮，否则两份结果会互相覆盖。",
                run_id,
            )
        run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run_id)
        report_path = os.path.join(run_dir_path, "report.json")
        if run.get("status") != "graded" and not os.path.isfile(report_path):
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "这一轮还没有校验结果，不需要作废；直接在内置对话里发送即可。",
                run_id,
            )
        voided = 0
        for rnd in reversed(run.get("rounds") or []):
            if not rnd.get("voided"):
                rnd["voided"] = True
                rnd["voided_at"] = util.iso_now()
                voided += 1
                break
        archived = ""
        try:
            archived = _archive_report(run_dir_path)
        except OSError as exc:
            raise errors.HarnessError(
                errors.E_INTERNAL,
                "旧报告挪不动，本轮成绩未能作废。请把报错记下来再处理。",
                str(exc),
            )
        run["status"] = "ready"
        run["last_score"] = None
        run["last_passed"] = None
        save_run(cfg, run)
    return {
        "run_id": run_id,
        "status": run["status"],
        "voided_rounds": voided,
        "report_archived": bool(archived),
        "notice": "本轮分数已作废，不计入通过率与均分；模型改完后重新校验会记作新一轮结果。"
                  if not archived else
                  "本轮成绩已作废，旧报告已挪到 report-discarded-*.json 留证；"
                  "面板回到「还没有校验结果」，让模型动手改完再重新校验。",
    }


def release_sandbox(cfg: dict, run_id: str, log: Log = None) -> dict:
    """回收这一轮的工作区目录：只删沙箱，runs/ 里的记录、报告、diff 全部保留。

    批次跑完会自动释放，但服务重启会带走监控线程，校验完的沙箱就一直占着磁盘；
    工作台单轮 run 更是从来没有释放入口（只有清空改动与重建）。这个口子补上两者。
    """
    logger = log or (lambda m: None)
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型或评分正在使用这一轮，等它结束再回收沙箱。", run_id)
        run = get_run(cfg, run_id)
        if run.get("status") in {"preparing", "grading"}:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "这一轮正在准备或校验中，先等它结束再回收沙箱。", run_id)
        workspace = str(run.get("sandbox") or "")
        if not workspace or not os.path.isdir(workspace):
            return {"run_id": run_id, "released": False, "sandbox": "",
                    "message": "这一轮已经没有可回收的沙箱工作区了。"}
        sandbox.destroy(cfg, run, log=logger)
        save_run(cfg, run)
        return {"run_id": run_id, "released": True, "sandbox": "",
                "message": "沙箱工作区已回收；成绩与报告仍在 runs/ 里。"}


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
            attempt_no = int(run.get("attempt") or 1)
            # 同一 attempt 再校验：旧那份是真实考出来的结果，先归档再写新的。
            # 直接覆盖会让第一次尝试的证据永远消失，记分板的 trials 还会 +1。
            _archive_artifacts(run_dir_path, ["round-%d.json" % attempt_no], label="retry")
            util.write_json_atomic(os.path.join(run_dir_path, "report.json"), final)
            util.write_json_atomic(
                os.path.join(run_dir_path, "round-%d.json" % attempt_no), final)
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
                # 排行榜按这个排名，不按"从建号到交卷的墙钟"
                "model_work_seconds": model_work_seconds(cfg, run),
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
    """取上一轮的报告（红转绿对比用）。

    作废的轮次要跳过：回基线之后旧轮次只是留证，拿它当"上一轮"会让红转绿
    对比凭空造出一个从未发生过的进步。
    """
    run_dir_path = run.get("run_dir") or ""
    if not run_dir_path:
        return None
    voided = {int(r.get("attempt") or 0) for r in (run.get("rounds") or []) if r.get("voided")}
    for index in range(attempt - 1, 0, -1):
        if index in voided:
            continue
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


def model_work_seconds(cfg: dict, run: dict, since: object = None) -> float:
    """模型实际动手的秒数：每条提问到"这一条彻底回完"的间隔之和。

    不能用 `created_at` 到现在的时间：那里面混着挂机、混着上一个模型、也混着
    排队校验，实测一条 run 因此显示成 28.9 小时，而它自己的校验只花了 3.4 秒。
    默认只算本轮（起点 `round_started_at`）；老记录没有这个字段时退回全量跨度。
    """
    try:
        records = chat._read_records(run)
    except errors.HarnessError:
        return 0.0
    floor = _timestamp_seconds(since if since is not None else run.get("round_started_at"))
    segments: list = []
    for item in records:
        stamp = _timestamp_seconds(item.get("created_at"))
        if stamp is None or (floor is not None and stamp < floor):
            continue
        if item.get("role") == "user" or not segments:
            segments.append([stamp, stamp])
        else:
            segments[-1][1] = max(segments[-1][1], stamp)
    return round(sum(max(0.0, end - start) for start, end in segments), 1)


def run_view(cfg: dict, run: dict, log_tail: int = 200) -> dict:
    """GET /api/runs/{id} 的响应体：状态 / 日志 / 报告一次给全。"""
    doc = load_report(cfg, run)
    try:
        config.find_model(cfg, str(run.get("model") or ""))
        model_gone = False
    except errors.HarnessError:
        model_gone = True
    view = {
        "run_id": run["run_id"],
        "task": run.get("task"),
        "model": run.get("model"),
        "model_gone": model_gone,
        "round_started_at": run.get("round_started_at") or run.get("created_at"),
        "model_work_seconds": model_work_seconds(cfg, run),
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
        "note": "单元格 = pass@1 通过数/作数尝试数，均分取各尝试最佳轮的均值；"
                "建了记录但从未跑完校验的尝试不进分母；「已揭晓」区不计入通过率。",
    }


def _purge_path(root: str, path: object, sink: list) -> None:
    """删掉一条路径，但先确认它在这个根目录里面。

    删除是不可逆的，路径越界（比如记录里存了个 ../ 或者被手工改过的绝对路径）
    必须当场中止而不是"少删一个目录继续走"。
    """
    target = str(path or "")
    if not target or not os.path.exists(target):
        return
    if not util.path_within(root, target):
        raise errors.HarnessError(
            errors.E_INTERNAL,
            "要删除的路径不在预期根目录内，已中止删除。",
            target,
        )
    util.remove_tree(target)
    sink.append(target)


def _prune_empty_dirs(removed: str, stop_at: str) -> None:
    """记录目录删空后把上层空壳一并收掉，别在 runs/ 里留一串空目录。

    从被删目录的**父级**往上收：父级本身可能已经空了（这条 run 是该档案在这道题下
    唯一的记录），也可能还有兄弟记录（看到非空就停）。从被删的那个目录本身开始判空
    是错的——它刚被 rmtree 掉，listdir 直接抛 FileNotFoundError。
    """
    stop = os.path.normpath(stop_at)
    current = os.path.dirname(os.path.normpath(removed))
    while current and current != stop and current.startswith(stop + os.sep):
        try:
            if os.listdir(current):
                return
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def delete_run(cfg: dict, run_id: str) -> dict:
    """真删一条运行记录：记录目录、沙箱副本、评分树全部移除，不可恢复。

    「废弃」的语义就是这次尝试不算数、也不占地方：对话记录（含 epochs/ 归档）、
    报告、diff、指纹、依赖基线跟着记录目录一起走。共享的快照缓存
    sandboxes/.snapshots 不动 —— 那是受测仓库的基线，别的 run 还要用。
    对话或校验进行中拒绝删除。
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
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run_id)
    if not os.path.isdir(run_dir_path):
        raise errors.HarnessError(
            errors.E_RUN_NOT_FOUND,
            "运行记录目录不存在，可能已经被删除过了。",
            run_id,
        )
    return {"run_id": run_id, "deleted": True, "purged": purge_run(cfg, run),
            "notice": "记录、对话与沙箱已彻底删除，不可恢复。"}


def purge_run(cfg: dict, run: dict) -> list:
    """删掉一条 run 名下的全部派生数据，返回被删掉的路径。

    调用方负责持住这一轮的会话锁。共享的快照缓存 sandboxes/.snapshots 不动：
    那是受测仓库的基线，别的 run 还要用。
    """
    run_id = str(run.get("run_id") or "")
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run_id)
    purged: list = []
    _purge_path(cfg["sandbox_root"],
                os.path.join(cfg["sandbox_root"], grade.GRADE_DIR_PREFIX, util.sanitize_id(run_id)), purged)
    _purge_path(cfg["sandbox_root"], run.get("sandbox"), purged)
    _purge_path(cfg["runs_root"], run_dir_path, purged)
    _prune_empty_dirs(run_dir_path, cfg["runs_root"])
    return purged


def _live_rounds(run: dict) -> List[dict]:
    """计入统计的轮次：被「继续对话」作废掉的那些不算成绩，也不进均分。"""
    return [rnd for rnd in (run.get("rounds") or []) if isinstance(rnd, dict) and not rnd.get("voided")]


def _counted_rounds(run: dict) -> List[dict]:
    """成绩作数的轮次：作废（voided）与判无效（invalidated：越界/回归/校验出错）都不算。"""
    return [rnd for rnd in _live_rounds(run) if not rnd.get("invalidated")]


def _cell_stats(pair: List[dict]) -> dict:
    """一个 (任务 × 模型) 单元格的统计。

    口径（与排行榜同口径）：
    - trials 分母 = **真实跑过的尝试数**：建了记录但从未进入评分流程、或所有轮次
      都被作废/判无效的 run 不进分母（旧实现把它们记成一次失败尝试，通过率被稀释）。
    - pass@1 = 作数尝试里第 1 轮全绿的数量。
    - 均分 = 每条 run 只贡献一个代表分（其作数轮的最高分）在全部作数尝试上的均值——
      同一档案对同一题的多次尝试各算一次，不再把每一轮都摊进平均（重复计入），
      与排行榜「一条记录一个代表成绩」的口径对齐。
    """
    def _score(rnd: dict) -> float:
        try:
            return float(rnd.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    scored = [r for r in pair if not r.get("revealed")]
    revealed = [r for r in pair if r.get("revealed")]
    counted = [r for r in scored if _counted_rounds(r)]
    trials = len(counted)
    first_round_passes = 0
    for run in counted:
        for rnd in _counted_rounds(run):
            if int(rnd.get("attempt") or 0) == 1:
                if rnd.get("passed"):
                    first_round_passes += 1
                break
    any_pass = 0
    scores: List[float] = []
    for run in counted:
        best = None
        run_passed = False
        for rnd in _counted_rounds(run):
            value = _score(rnd)
            best = value if best is None else max(best, value)
            if rnd.get("passed"):
                run_passed = True
        if run_passed:
            any_pass += 1
        if best is not None:
            scores.append(best)
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
    out = ["任务,档位," + ",".join("%s(pass@1/作数尝试数,均分,Wilson95%%)" % m for m in models)]
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
            if not isinstance(result, dict) or result.get("passed") is not True:
                continue
            # 作废轮（用户点「作废本轮」）与越界/回归轮（invalidated）都不算成绩，
            # 排行榜以前只挡后者，满分轮被作废之后仍然排第 1。
            if result.get("invalidated") or result.get("voided"):
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
        completed_at = result.get("graded_at")
        # 墙钟用时（本轮起点 → 交卷）只作为兜底与对照：里面混着挂机与思考。
        start_s = _timestamp_seconds(run.get("round_started_at") or run.get("created_at"))
        finish_s = _timestamp_seconds(completed_at)
        wall_s = max(0.0, finish_s - start_s) if start_s is not None and finish_s is not None else None
        # 排名口径 = 模型实际工作时间：优先用轮次记录里落盘的那一份（就是那一轮的
        # 跨度），老记录没这个字段就按本轮起点现算，再算不出来才退回墙钟。
        work_s = result.get("model_work_seconds")
        try:
            work_s = float(work_s) if work_s is not None else None
        except (TypeError, ValueError):
            work_s = None
        if not work_s:
            fresh = model_work_seconds(cfg, run)
            work_s = fresh if fresh else None
        try:
            score = float(result.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        entries.append({
            "run_id": run.get("run_id", ""),
            "model": str(run.get("model") or ""),
            "rounds": attempt,
            "duration_s": round(work_s, 3) if work_s is not None else (
                round(wall_s, 3) if wall_s is not None else None),
            # 两个口径都下发：排行榜排的是模型工作时间，墙钟留着做对照，
            # 否则"挂机两小时"和"模型干两小时"看起来是同一个成绩。
            "model_work_seconds": round(work_s, 3) if work_s is not None else None,
            "wall_seconds": round(wall_s, 3) if wall_s is not None else None,
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
_MODEL_VIEW_FIELDS = ("id", "protocol", "api_mode", "base_url", "model",
                      "key_masked", "key_env", "note")


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


def upsert_model(cfg: dict, payload: dict) -> dict:
    """新增或更新模型档案；明文密钥只进本机密钥文件，config.json 只存脱敏值。"""
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
    key_env = str(payload.get("key_env") or "").strip()
    if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise errors.HarnessError(
            errors.E_MODEL_INVALID,
            "key_env 必须是合法的服务端环境变量名。",
            key_env,
        )
    api_key = str(payload.get("api_key") or "").strip()
    previous_id = util.sanitize_id(payload.get("previous_id"))
    # 写回的必须是原始档案，不能把只读诊断字段一起落盘
    models = _model_records(cfg)
    if previous_id and previous_id != model_id:
        # 改编号：旧档案连同它的密钥一起搬走，而不是留下重复档案
        models = [m for m in models if str(m.get("id")) != previous_id]
        keyring.rename_key(previous_id, model_id)
    entry = {
        "id": model_id,
        "protocol": protocol,
        "api_mode": api_mode,
        "base_url": str(payload.get("base_url") or ""),
        "model": str(payload.get("model") or ""),
        "key_masked": str(payload.get("key_masked") or ""),
        "key_env": key_env,
        "note": str(payload.get("note") or "")[:500],
    }
    if api_key:
        entry["key_masked"] = keyring.set_key(model_id, api_key)
    for index, item in enumerate(models):
        if str(item.get("id")) == model_id:
            models[index] = entry
            break
    else:
        models.append(entry)
    config.update_models(models)
    return entry


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
    purged_paths: List[str] = []
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
                purged_paths.extend(purge_run(cfg, run))
                removed_runs.append(rid)
    return {
        "id": model_id,
        "deleted": True,
        "remaining": len(remaining),
        # removed_runs 是档案名下的 run_id（给"删了几条"这句话用），
        # purged_paths 是实际被抹掉的目录（排障时要能核对到底动了哪些路径）。
        "removed_runs": removed_runs,
        "purged_paths": purged_paths,
        "skipped_busy": skipped_busy,
    }
