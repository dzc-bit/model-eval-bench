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

**记分板与排行榜不再从这里的记录现算**（2026-10-02）：工作台点「结束本轮」时
先把成绩写进 `results` 台账（runs/_results/ledger.json），再真删这条记录。
台账与记录脱钩，所以记录没了榜单还有数；反过来「废弃本轮」什么都不写。
只有还在跑、还没结束的记录不参与统计——成绩要等收尾才落账。
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

from . import chat, config, errors, grade, keyring, packs
from . import report as report_mod
from . import results as results_ledger
from . import sandbox, util

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

    :param wait_s: 保留旧调用签名（跑批仍传 30s）；文件夹沙箱没有盘符可等，值被忽略。
    """
    del wait_s
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
                sandbox.prepare(cfg, run, meta, log=log)
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
        sandbox.rebuild(cfg, run, meta, log=logger)
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


def _workspace_missing(cfg: dict, run: dict) -> bool:
    """工作区是否已不可用。批次 auto_release 回收评分后的沙箱，就是这种形态。"""
    workspace = str(run.get("sandbox") or "")
    return not workspace or not os.path.isdir(workspace)


def _prepare_round_workspace(cfg: dict, run: dict, meta: dict) -> None:
    """给丢了工作区的 run 补建一个全新基线沙箱（不动作轮次与已推进的 attempt）。

    批次 auto_release 会在评分落定后回收沙箱让出磁盘；轮次还有剩余时用户
    随时可能在工作台「进入第二轮」，promote 在这里把工作区补回来。注意：
    上一轮模型写的代码已随回收丢失，第二轮从干净基线开始；rounds 里的
    成绩原样保留，不按「重建」语义作废。
    """
    run["status"] = "preparing"
    save_run(cfg, run)
    # 成功路径由 prepare 置回 ready 并落盘；失败路径它自己会把 run 判成 error
    sandbox.prepare(cfg, run, meta, log=lambda m: None)


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
    workspace_missing = _workspace_missing(cfg, run)
    if current not in graded_attempts:
        # attempt 已推进、工作区却被批次回收（T2-04 实测）：再点一次
        # 「进入第二轮」按补建工作区处理，不重复消耗轮次机会。
        if not (current > 1 and workspace_missing):
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "第 %d 轮还没有校验结果，不能进入下一轮。先运行校验，或点「作废本轮成绩」重来。" % current,
                run_id,
            )
        _prepare_round_workspace(cfg, run, meta)
        run = get_run(cfg, run_id)
        return {"run_id": run_id, "attempt": int(run.get("attempt") or current),
                "can_promote": int(run.get("attempt") or current) < int(run.get("attempts_allowed") or 1),
                "reprepared": True}
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
    if workspace_missing:
        _prepare_round_workspace(cfg, run, meta)
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
    # 补丁落盘到运行记录目录：以前只在当次把正文下发给前端，刷新就没了，
    # 报告窗与复盘都拿不回参考解。写在 run 目录里，真删时随记录一起被 purge_run
    # 带走，不需要为它单开删除分支。
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run_id)
    revealed_path = os.path.join(run_dir_path, REVEALED_PATCH_FILE)
    try:
        util.write_text_atomic(revealed_path, text)
    except OSError as exc:
        raise errors.HarnessError(
            errors.E_INTERNAL,
            "参考解没能存进运行记录目录，已揭晓未生效。请检查磁盘是否可写。",
            str(exc),
        )
    run["revealed"] = True
    save_run(cfg, run)
    return {
        "run_id": run_id,
        "patch": text,
        "stored_at": REVEALED_PATCH_FILE,
        "notice": "该轮已标记为「已揭晓」，按规则不计入通过率统计。",
    }


def load_revealed_patch(cfg: dict, run: dict) -> str:
    """读运行记录里落盘的参考解正文（没有就返回空串）。

    报告窗与复盘从这里取，刷新页面后参考解仍在，不再依赖「当次下发」。
    """
    run_dir_path = run.get("run_dir") or _run_dir_of(cfg, str(run.get("run_id") or ""))
    try:
        with open(os.path.join(run_dir_path, REVEALED_PATCH_FILE), "rb") as fh:
            return util.decode_output(fh.read())
    except OSError:
        return ""


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

#: 揭晓参考解时落盘的补丁文件名（写进运行记录目录，随记录一起被真删）。
REVEALED_PATCH_FILE = "revealed.patch"


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

    批次跑完会自动释放，服务重启也会带走监控线程。**工作台不再用这个口子**：
    单轮收尾统一走 finish_round（记台账 + 真删记录），这里只留给跑批与批量回收。
    """
    logger = log or (lambda m: None)
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型或评分正在使用这一轮，等它结束再回收沙箱。", run_id)
        if chat.send_active(run_id):
            # exclusive 只锁落盘动作，盖不住在飞的发送线程；T2-04 事故里模型
            # 比回收晚一分钟还在写，工作区被删得只剩残留。
            raise errors.HarnessError(
                errors.E_RUN_BUSY, "模型正在输出，等这条消息结束再回收沙箱。", run_id)
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


# --------------------------------------------------------------------------
# 成绩台账与「结束本轮」（2026-10-02 收尾语义收敛）
# --------------------------------------------------------------------------

def record_run_result(cfg: dict, run: dict, origin: str = "run") -> Optional[dict]:
    """把一条运行记录的作数轮写成台账条目；没有可记的成绩时返回 None。

    口径与旧记分板逐条对齐（回填后榜单数字必须一模一样）：
    - 只收 ``_counted_rounds``：被「继续对话（本轮分数作废）」作废的轮次，
      以及越界/回归判无效的轮次，一条都不进台账；
    - 整轮已揭晓参考解的运行一条都不收——揭晓等于看过答案；
    - 代表分 = 作数轮里的最高分；``pass1`` 看第 1 轮有没有全绿；
    - 同一条记录已经有条目（回填之后又点一次结束）就不再写，避免重复计数。
    """
    run_id = str(run.get("run_id") or "")
    if run_id and results_ledger.has_source_run(cfg, run_id):
        return None
    if run.get("revealed"):
        return None
    counted = _counted_rounds(run)
    if not counted:
        return None

    def _score(rnd: dict) -> float:
        try:
            return float(rnd.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    best = max(counted, key=lambda r: (_score(r), -_round_no(r)))
    raw_model = str(run.get("model") or "")
    # round_started_at 只描述当前这一轮：最高分轮不是当前轮时（早先轮次拿了
    # 最高分、后来又 promote 出新一轮），拿当前轮起点去减更早的终点会得出
    # finish < start 的负差，被 max(0,…) 夹成假 0——不如老老实实置回「未知」。
    wall = None
    if _round_no(best) == int(run.get("attempt") or 1):
        start_s = _timestamp_seconds(run.get("round_started_at") or run.get("created_at"))
        finish_s = _timestamp_seconds(best.get("graded_at"))
        if start_s is not None and finish_s is not None:
            wall = max(0.0, finish_s - start_s)
    work = best.get("model_work_seconds")
    try:
        work = float(work) if work is not None else None
    except (TypeError, ValueError):
        work = None
    if work is None:
        try:
            work = model_work_seconds(cfg, run) or None
        except errors.HarnessError:
            work = None

    return results_ledger.make_entry(
        run.get("task"), canonical_model(cfg, raw_model), raw_model,
        source_run_id=run_id, origin=origin,
        rounds=len(counted), best_round=_round_no(best),
        score=_score(best),
        passed=any(r.get("passed") is True for r in counted),
        pass1=any(_round_no(r) == 1 and r.get("passed") is True for r in counted),
        model_work_seconds=work, wall_seconds=wall,
        graded_at=best.get("graded_at"),
    )


def _round_no(rnd: dict) -> int:
    try:
        return int(rnd.get("attempt") or 0)
    except (TypeError, ValueError):
        return 0


def finish_round(cfg: dict, run_id: str, log: Log = None) -> dict:
    """结束本轮：成绩先写入台账，然后真删整条运行记录。

    这是工作台唯一的收尾出口（2026-10-02）。两件事的顺序不能反：
    先删记录再记账，成绩就跟着记录一起没了——而榜单要的正是「记录没了还有数」。

    没有可计入台账的成绩时（没跑校验 / 全被作废 / 已揭晓参考解）照样结束，
    只是不写条目：这次尝试连同过程一起丢掉，正是「结束本轮」在没成绩时的样子。
    """
    logger = log or (lambda m: None)
    with chat.exclusive(run_id, blocking=False) as acquired:
        if not acquired:
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "模型正在处理这一轮，等当前消息完成后再结束本轮。",
                run_id,
            )
        run = get_run(cfg, run_id)
        if _GRADING.get(run_id) or run.get("status") == "grading":
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "这一轮正在校验中，等校验结束后再结束本轮。",
                run_id,
            )
        if run.get("status") == "preparing":
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "沙箱还在准备中，等它就绪再结束本轮。",
                run_id,
            )
        run_dir_path = run.get("run_dir") or _run_dir_of(cfg, run_id)
        if not os.path.isdir(run_dir_path):
            raise errors.HarnessError(
                errors.E_RUN_NOT_FOUND,
                "运行记录目录不存在，可能已经被删除过了。",
                run_id,
            )
        # 锁内记账 + 删除：purge_run 的约定就是调用方持着这一轮的会话锁。
        entry = record_run_result(cfg, run)
        logger("结束本轮：%s" % ("成绩已记入台账 %s" % entry["entry_id"] if entry else "没有可计入台账的成绩"))
        if entry:
            results_ledger.append_entry(cfg, entry)
        else:
            entry = None
        purged = purge_run(cfg, run)
    notice = ("本轮成绩已记入台账，记分板与排行榜按最高分那条展示；"
              "运行记录、对话与沙箱已彻底删除，下次再跑是全新一轮。"
              if entry else
              "这一轮没有可计入台账的成绩（未校验 / 已作废 / 已揭晓参考解），"
              "记录、对话与沙箱已彻底删除，不留成绩。")
    return {
        "run_id": run_id,
        "finished": True,
        "ledgered": bool(entry),
        "entry": entry,
        "purged": purged,
        "notice": notice,
    }


def backfill_ledger(cfg: dict, dry_run: bool = False) -> dict:
    """一次性回填：按新口径把 runs/ 里在册的记录写成台账条目（记录本身保留）。

    回填不删任何记录——迁移的是「成绩的读取来源」，不是数据本身。已有条目的
    记录会被跳过，所以重复执行不会把同一次尝试数两遍。

    回填后的数字必须与回填前完全一致（T1-01/T1-02 100、T1-03 0、T2-05 83.3），
    这条由 tests/test_results_ledger.py 的 ``test_backfill_matches_legacy_numbers`` 锁住。
    """
    added: List[str] = []
    skipped: List[str] = []
    for run in list_runs(cfg):
        run_id = str(run.get("run_id") or "")
        if results_ledger.has_source_run(cfg, run_id):
            skipped.append(run_id)
            continue
        entry = record_run_result(cfg, run, origin="backfill")
        if entry is None:
            skipped.append(run_id)
            continue
        if not dry_run:
            entry = results_ledger.append_entry(cfg, entry)
        added.append(entry["entry_id"] or run_id)
    return {
        "added": len(added),
        "skipped": len(skipped),
        "dry_run": bool(dry_run),
        "ledger": results_ledger.ledger_path(cfg),
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
            final.get("score"), "（%s）" % final.get("invalid_reason") if final.get("invalidated") else ""))
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
        # 揭晓过的参考解从运行记录目录读：刷新页面后报告窗还能再看到它，
        # 不再依赖「揭晓那一次把正文下发给前端」。
        "revealed_patch": load_revealed_patch(cfg, run) if run.get("revealed") else "",
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


def _model_alias_index(cfg: dict) -> Dict[str, str]:
    """历史档案身份 → 当前模型 id 的归并索引（只读换算，不改任何记录）。

    供应商重构后同一个真实模型有两种身份：老记录的 run.model 是老档案 id
    （读时迁移记进了供应商的 legacy_ids，并随 expand_models 带到每个扁平档案上），
    新记录是当前的扁平模型 id。归并依据分三层，精确的优先、模糊的不猜：

    1. 模型自身的 id 与 qualified_id（provider::model）——本来就是当前身份；
    2. 读时迁移把老档案 id 记成了模型 name，且该 name 确实出现在 legacy_ids
       里（防止用户手填的展示名被误当成身份别名）；
    3. legacy_ids 里剩下的老档案 id：只有供应商下恰好一个模型时归属才无歧义，
       此时才归并；多个模型时不猜——猜错等于把成绩记到别的模型头上。

    命中不到任何现存档案的老名字不进索引，调用方按原名处理（model_gone 语义）。
    """
    index: Dict[str, str] = {}
    providers = {str(p.get("id") or ""): p
                 for p in cfg.get("providers", []) if isinstance(p, dict)}
    for item in cfg.get("models", []):
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        index.setdefault(mid, mid)
        qualified = str(item.get("qualified_id") or "").strip()
        if qualified:
            index.setdefault(qualified, mid)
        provider = providers.get(str(item.get("provider_id") or ""))
        legacy = {str(x or "").strip() for x in (item.get("legacy_ids") or [])}
        legacy.update(str(x or "").strip() for x in (provider or {}).get("legacy_ids") or [])
        legacy.discard("")
        name = str(item.get("name") or "").strip()
        if name and name in legacy:
            index.setdefault(name, mid)
    for provider in providers.values():
        legacy = [str(x or "").strip() for x in provider.get("legacy_ids") or []]
        owned = [str(m.get("id") or "").strip() for m in provider.get("models") or []
                 if isinstance(m, dict) and str(m.get("id") or "").strip()]
        if len(owned) != 1:
            continue
        for old in legacy:
            if old:
                index.setdefault(old, owned[0])
    return index


def canonical_model(cfg: dict, model: object, index: Optional[Dict[str, str]] = None) -> str:
    """run.model 的统计身份：命中现存档案的归到当前模型 id，命中不到的原样返回。

    run.model 是历史事实，本函数只给 scoreboard / task_leaderboard 换算分组键，
    从不改写记录；档案已删的老名字走原名单列，不并进任何现行列。
    """
    raw = str(model or "")
    if not raw:
        return ""
    if index is None:
        index = _model_alias_index(cfg)
    return index.get(raw, raw)


def scoreboard(cfg: dict) -> dict:
    """行=任务、列=模型的记分板矩阵。数据源是成绩台账，不是 runs/ 里的记录。

    只有「结束」过的尝试在这里——成绩等收尾才落账，还 在跑、或已废弃的尝试
    一律不进统计。列按「模型身份」分组：台账存的是写入当时的档案原名，读的
    时候经 canonical_model 归并到当前档案 id，同一真实模型不裂成两列。
    """
    alias_index = _model_alias_index(cfg)
    grouped = results_ledger.group_by_cell(
        cfg, lambda name: canonical_model(cfg, name, alias_index))

    models: List[str] = []
    tasks: List[str] = []
    for (task, model) in grouped:
        if model not in models:
            models.append(model)
        if task not in tasks:
            tasks.append(task)

    for task in packs.list_tasks(cfg):
        if task["id"] not in tasks:
            tasks.append(task["id"])
    for model in cfg.get("models", []):
        mid = str(model.get("id") or "")
        if mid and mid not in models:
            models.append(mid)

    cells = {}
    for task in tasks:
        cells[task] = {model: _cell_stats(grouped.get((task, model)) or [])
                       for model in models}

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
        "note": "数据源是成绩台账（runs/_results/ledger.json）：只有点过「结束本轮」"
                "的尝试才在这里，每次结束各留一条，单元格展示最高分那条；"
                "pass@1 = 第 1 轮就全绿的条目数 / 条目数；作废轮、判无效轮与"
                "已揭晓参考解的尝试永不进台账；还在跑或已废弃的记录不计入。",
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


def _cell_stats(entries: List[dict]) -> dict:
    """一个 (任务 × 模型) 单元格的统计，数据源是台账条目。

    台账里**每次结束都留一条**，所以口径比旧记分板更简单也更诚实：
    - ``attempts`` = 结束的尝试数（一条条目一次尝试）。建了记录但没结束的
      （还在跑、已废弃）根本不进台账，不会稀释通过率。
    - ``pass1`` = 其中第 1 轮就全绿的条目数；``pass_rate`` = pass1 / attempts。
    - ``best_score`` = 分数最高那条的分数（榜单展示的就是它）；
      ``avg_score`` = 各条代表分的均值，两者一起给才看得出"是稳还是撞了一次"。
    - Wilson 区间随旧口径一起废弃：条目不再等于"通过的样本"，分母混着失败的
      尝试，硬算区间只会在一行样本上给出假精确。
    """
    def _score(entry: dict) -> float:
        try:
            return float(entry.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    attempts = len(entries)
    scores = [_score(e) for e in entries]
    best_entry = results_ledger.best_of(entries)
    pass1 = sum(1 for e in entries if e.get("pass1"))
    last_at = max((str(e.get("ended_at") or "") for e in entries), default="")
    return {
        "attempts": attempts,
        "pass1": pass1,
        "pass_rate": round(pass1 / attempts, 3) if attempts else 0.0,
        "best_score": round(_score(best_entry), 1) if best_entry else 0.0,
        "best_rounds": int(best_entry.get("rounds") or 1) if best_entry else 0,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "last_at": last_at,
        # 台账条目 id：台账是成绩不是记录，没有「回工作台打开」这条路，
        # 唯一的去处是删掉这个档案（连台账条目一起清）。
        "entry_ids": [str(e.get("entry_id") or "") for e in entries],
    }


def _totals(matrix: List[dict]) -> dict:
    attempts = sum(cell["attempts"] for row in matrix for cell in row["cells"].values())
    passes = sum(cell["pass1"] for row in matrix for cell in row["cells"].values())
    return {
        "attempts": attempts,
        "pass1": passes,
        "pass_rate": round(passes / attempts, 3) if attempts else 0.0,
    }


def scoreboard_csv(board: dict) -> str:
    """导出 CSV：一张矩阵，一行一个 (任务 × 模型) 的台账统计。"""
    models = board["models"]
    out = ["任务,档位," + ",".join("%s(结束次数,pass@1/次数,最高分,均分)" % m for m in models)]
    for row in board["matrix"]:
        cells = []
        for model in models:
            cell = row["cells"].get(model) or {}
            cells.append("%d,%d/%d,%.1f,%.1f" % (
                cell.get("attempts", 0), cell.get("pass1", 0), cell.get("attempts", 0),
                cell.get("best_score", 0.0), cell.get("avg_score", 0.0)))
        out.append("%s,%s,%s" % (row["task"], row["tier"], ",".join(cells)))
    out.append("")
    out.append("# 数据源：成绩台账 runs/_results/ledger.json（点过「结束本轮」的尝试；"
               "作废轮、判无效轮与已揭晓参考解的尝试永不进台账）")
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
    """该题的排行榜：同一模型取台账里分数最高的那一条。

    数据源与记分板同源（同一份台账、同一条模型身份归并规则），口径按新模型
    收敛为「先看最高分，再看用了几个轮次，最后看模型工作时间」：

    1. 分数高的在前——榜单展示的就是这个模型在这道题上最好的一次；
    2. 同分比轮数：一次就全绿的比改了三次才全绿的强；
    3. 再比模型实际工作时间（挂机不算）；墙钟只作为对照下发给前端做 tooltip。

    台账保留了每一次结束的条目，所以 ``attempts`` 一并下发，用户能看出
    「最高分那条」是稳出来的还是撞出来的。
    """
    meta = packs.load_meta(cfg, task_id)
    alias_index = _model_alias_index(cfg)
    grouped = results_ledger.group_by_cell(
        cfg, lambda name: canonical_model(cfg, name, alias_index))

    by_model: Dict[str, List[dict]] = {}
    for (task, model), items in grouped.items():
        if task != meta["id"]:
            continue
        by_model.setdefault(model, []).extend(items)

    entries = []
    for model, items in by_model.items():
        best = results_ledger.best_of(items)
        if best is None:
            continue
        work = best.get("model_work_seconds")
        try:
            work = float(work) if work is not None else None
        except (TypeError, ValueError):
            work = None
        wall = best.get("wall_seconds")
        try:
            wall = float(wall) if wall is not None else None
        except (TypeError, ValueError):
            wall = None
        try:
            score = float(best.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        entries.append({
            "entry_id": str(best.get("entry_id") or ""),
            "model": model,
            "attempts": len(items),
            "score": score,
            "rounds": int(best.get("rounds") or 1),
            "duration_s": round(work, 3) if work is not None else (
                round(wall, 3) if wall is not None else None),
            # 两个口径都下发：排行榜排的是模型工作时间，墙钟留着做对照，
            # 否则"挂机两小时"和"模型干两小时"看起来是同一个成绩。
            "model_work_seconds": round(work, 3) if work is not None else None,
            "wall_seconds": round(wall, 3) if wall is not None else None,
            "completed_at": best.get("graded_at", ""),
            "ended_at": best.get("ended_at", ""),
        })

    def sort_key(entry: dict) -> tuple:
        duration = entry.get("duration_s")
        return (
            -entry["score"],
            entry["rounds"],
            duration if duration is not None else float("inf"),
            str(entry.get("completed_at") or ""),
            entry["model"].casefold(),
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


def _provider_ownership(provider: dict) -> tuple:
    """供应商名下的匹配规则：限定名前缀 + 老档案 id 集合。

    删除记录、删除台账条目、``run_count`` 计数**必须共用这一份**——三处各写一遍
    必然漂移，而漂移的后果是「删完档案还剩一列查无此人的幽灵成绩」。
    """
    pid = str(provider.get("id") or "")
    owned_prefix = "%s::" % pid if pid else ""
    legacy_ids = {str(m.get("id")) for m in (provider.get("models") or [])
                  if isinstance(m, dict) and m.get("id")}
    legacy_ids.add(pid)
    # 读时迁移记下的老档案 id 也算名下（与 key_owner_candidates 同一份名单）：
    # 老记录的 model 字段挂的是它们，漏了就会留下删不掉的幽灵记录。
    for item in (provider.get("legacy_ids") or []):
        value = str(item or "")
        if value:
            legacy_ids.add(value)
    legacy_ids.discard("")
    return owned_prefix, legacy_ids


def owned_run_ids(cfg: dict, provider: dict) -> List[str]:
    """该供应商名下的运行记录 run_id（只读，给 run_count 与级联删除共用）。"""
    owned_prefix, legacy_ids = _provider_ownership(provider)
    out: List[str] = []
    for run in list_runs(cfg):
        model = str(run.get("model") or "")
        rid = str(run.get("run_id") or "")
        if rid and model and (model.startswith(owned_prefix) or model in legacy_ids):
            out.append(rid)
    return out


def _provider_view(provider: dict, cfg: dict) -> dict:
    """供应商条目的对外视图：原始字段 + 只读诊断（密钥是否存在、能否使用）。

    诊断只回传**是否存在**与候选变量名，取值永不离开服务端。
    """
    out = {k: provider.get(k) for k in config.PROVIDER_FIELDS if k in provider}
    out["models"] = [
        {k: m.get(k) for k in config.PROVIDER_MODEL_FIELDS if k in m}
        for m in (provider.get("models") or [])
    ]
    # 密钥状态按供应商算：本机密钥文件 + 环境变量候选。
    # 本机文件要走「新 id → 老档案 id」整条候选链：读时迁移出的供应商 id 是从
    # 端点推的（local-20128），密钥仍挂在老档案名下，只按新 id 查会把
    # 「✓ 已存密钥」显示成「未配置密钥」，用户以为密钥丢了去重填一把。
    legacy_ids = provider.get("legacy_ids") or []
    probe = {"provider_id": provider.get("id"), "protocol": provider.get("protocol"),
             "legacy_ids": legacy_ids}
    owners = chat.key_owner_candidates(provider.get("id"), legacy_ids)
    stored = any(keyring.get_key(candidate) for candidate in owners)
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
    # 级联计数由后端下发（只读，无副作用）：删除确认框要显示「连同 N 条记录
    # 一并删除」，前端以前是自己 GET /api/runs 再按前缀 + 老档案 id 复刻一遍
    # delete_provider 的匹配逻辑——两份必然漂移，漂了就是删不干净的幽灵成绩。
    owned_prefix, legacy_ids = _provider_ownership(provider)
    out["run_count"] = len(owned_run_ids(cfg, provider))
    out["entry_count"] = results_ledger.count_for_models(cfg, owned_prefix, legacy_ids)
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


def _merge_legacy_ids(*sources) -> List[str]:
    """把几处来源里的老档案 id 合成一份去重名单（保持出现顺序）。

    供应商 id 是从端点推出来的，密钥却仍可能挂在老档案 id 名下；保存一次就
    把 ``legacy_ids`` 弄丢，等于把密钥锁在门外——所以编辑保存必须原样带走。
    """
    out: List[str] = []
    for source in sources:
        items = source.get("legacy_ids") if isinstance(source, dict) else source
        for item in (items or []):
            value = str(item or "").strip()
            if value and value not in out:
                out.append(value)
    return out


def upsert_provider(cfg: dict, payload: dict) -> dict:
    """新增或更新供应商（连同它的模型清单）。

    明文密钥只进本机密钥文件（按供应商 id 存），config.json 只存脱敏值。
    """
    entry = _normalize_provider_payload(payload)
    pid = entry["id"]
    api_key = str(payload.get("api_key") or "").strip()
    previous_id = util.sanitize_id(payload.get("previous_id"))

    providers = [dict(p) for p in cfg.get("providers", [])]
    # 老档案 id 沿用原条目里的：表单不传这个字段，服务端自己接着走，
    # 否则迁移来的供应商保存一次就再也找不到它那把密钥。
    source = next((p for p in providers if str(p.get("id")) == (previous_id or pid)), None)
    legacy_ids = _merge_legacy_ids(payload.get("legacy_ids"), source)
    if legacy_ids:
        entry["legacy_ids"] = legacy_ids
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


def delete_provider(cfg: dict, provider_id: str, with_runs: bool = True) -> dict:
    """删除供应商（连同已存密钥、其下所有模型、名下运行记录与台账条目）。

    **级联是默认且唯一的语义**（2026-10-02）：删档案就是彻底删除，前端不再有
    级联勾选项。``with_runs`` 参数只为兼容旧调用保留，恒按 True 执行——
    留着「不删记录」那条路就等于让记分板上出现查无此人的幽灵列。

    正被对话/校验占用的运行记录会跳过并列入 skipped_busy；台账条目照删。
    """
    providers = [dict(p) for p in cfg.get("providers", [])]
    target = next((p for p in providers if str(p.get("id")) == str(provider_id)), None)
    if target is None:
        raise errors.HarnessError(
            errors.E_MODEL_NOT_FOUND, "找不到供应商 %s，删除失败。" % provider_id, str(provider_id))
    remaining = [p for p in providers if str(p.get("id")) != str(provider_id)]
    config.update_providers(remaining)

    # 名下归属口径与 run_count / 台账清理共用同一份（见 _provider_ownership）
    owned_prefix, legacy_ids = _provider_ownership(target)

    # 密钥按「新 id → 老档案 id」整条链清：迁移来的供应商 id 是从端点推的，
    # 密钥多半还挂在老档案名下，只删新 id 会把明文密钥永远留在本机。
    # 别删到别人的：名单与其它供应商（含它们的老档案 id）重叠时跳过。
    still_used = {str(p.get("id")) for p in remaining}
    for p in remaining:
        still_used.update(str(x) for x in (p.get("legacy_ids") or []))
    for owner in chat.key_owner_candidates(provider_id, target.get("legacy_ids") or []):
        if owner and owner not in still_used:
            keyring.remove_key(owner)

    removed_runs: List[str] = []
    purged_paths: List[str] = []
    skipped_busy: List[str] = []
    for run in list_runs(cfg):
        model = str(run.get("model") or "")
        if not (model.startswith(owned_prefix) or model in legacy_ids):
            continue
        rid = str(run.get("run_id") or "")
        if not rid or chat.send_active(rid):
            if rid:
                skipped_busy.append(rid)
            continue
        if _GRADING.get(rid):
            skipped_busy.append(rid)
            continue
        with chat.exclusive(rid, blocking=False) as acquired:
            if not acquired:
                skipped_busy.append(rid)
                continue
            purged_paths.extend(purge_run(cfg, run))
            removed_runs.append(rid)
    removed_entries = results_ledger.remove_for_models(cfg, owned_prefix, legacy_ids)
    return {
        "id": provider_id,
        "deleted": True,
        "remaining": len(remaining),
        # 与 delete_model 同一口径：removed_runs 是名下 run_id（给"删了几条"用），
        # purged_paths 是实际抹掉的目录（排障时核对到底动了哪些路径），
        # removed_entries 是被清掉的台账条目（记分板上那一列随之消失）。
        "removed_runs": removed_runs,
        "removed_entries": removed_entries,
        "purged_paths": purged_paths,
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
    # 用户的实际工作流是「填地址 → 填密钥 → 点拉取 → 勾模型 → 保存」——
    # 拉取时那份表单**还没保存**，所以这里必须能用请求体里现填的 base_url / api_key。
    # 这也是为什么「换了几个供应商都不行」：每次都回退到本机存的旧密钥（已失效），
    # 新填的那把根本没参与请求。
    # 支持"还没保存就试拉"：表单里现填的 base_url / api_key 优先
    base_url = str(payload.get("base_url") or (provider or {}).get("base_url") or "").strip()
    if not base_url:
        raise errors.HarnessError(
            errors.E_MODEL_INVALID, "先填接口地址，再拉取模型列表。", provider_id)
    protocol = str(payload.get("protocol") or (provider or {}).get("protocol") or "openai").lower()
    typed_key = str(payload.get("api_key") or "").strip()

    candidates = chat.list_remote_models(
        base_url=base_url, protocol=protocol, provider_id=provider_id, api_key=typed_key,
        legacy_ids=(provider or {}).get("legacy_ids") or [])

    configured = {str(m.get("id")) for m in ((provider or {}).get("models") or [])}
    for item in candidates:
        item["configured"] = str(item.get("id")) in configured
    return {"provider_id": provider_id, "models": candidates}




def delete_model(cfg: dict, model_id: str, with_runs: bool = True) -> dict:
    """删除模型档案（连同已存密钥、名下运行记录与台账条目）。

    与 delete_provider 同一语义：级联是真删，没有"只删档案留记录"的选项。
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
    for run in list_runs(cfg):
        if str(run.get("model") or "") != str(model_id):
            continue
        rid = str(run.get("run_id") or "")
        if not rid or chat.send_active(rid) or _GRADING.get(rid):
            if rid:
                skipped_busy.append(rid)
            continue
        with chat.exclusive(rid, blocking=False) as acquired:
            if not acquired:
                skipped_busy.append(rid)
                continue
            purged_paths.extend(purge_run(cfg, run))
            removed_runs.append(rid)
    removed_entries = results_ledger.remove_for_models(cfg, "", {str(model_id)})
    return {
        "id": model_id,
        "deleted": True,
        "remaining": len(remaining),
        # removed_runs 是档案名下的 run_id（给"删了几条"这句话用），
        # purged_paths 是实际被抹掉的目录（排障时要能核对到底动了哪些路径）。
        "removed_runs": removed_runs,
        "removed_entries": removed_entries,
        "purged_paths": purged_paths,
        "skipped_busy": skipped_busy,
    }
