"""并行会话：一次为「多道题 × 多个模型」准备独立工作区。

设计要点（文件夹沙箱）：

1. **并发闸门只限制工作线程数量**。每个 run 使用 sandbox_root 下的独立目录，
   不创建盘符映射；默认并发由 config.max_concurrency 控制，也可以显式调小。
2. **准备沙箱本身是同步阻塞的**（`runs.create_run` 内部铺树 + 注入 + git init），
   放到线程池里跑，HTTP 请求立刻返回批次 id，前端轮询进度——与单轮校验的
   异步语义保持一致（`POST /api/runs/{id}/grade` 也是异步的）。
3. **每个 item 独立成败**。一道题失败（题包坏了、模型档案没了）不能拖垮整批，
   记到该 item 的 `error` 里，批次继续。
4. **一个条目占用一个槽位，直到用户在工作台点「结束本轮」**（2026-10-02 语义修正）：
   校验只是过程，`结束本轮`（或`废弃本轮`）才是这次尝试的终点。旧实现一看到
   run.status=graded 就回收槽位、把条目判成「已完成」，于是
   (a) 队列在用户还没读完第一轮结果时就派下了下一条；
   (b) 条目把第 1 轮的分数当成最终成绩钉死，用户接着跑第 2 轮拿满分也刷不回来
       （T3-08 实测：台账 100 分通过，批次行却写着 85.7 未通过）。
   现在：出分后条目停在 `awaiting_finish`（槽位保留、分数随轮次刷新），
   等收尾出口把记录收走，再从台账读最终成绩落定。

线程模型：一个「批次线程」负责调度，每个 item 再交给一个工作线程；
批次线程只做派发与状态汇总，不持有任何长事务锁。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from . import chat, config, errors, packs, results as results_ledger, runs, util

#: 同时保留的批次（内存态）上限，防止长时间运行堆爆
MAX_BATCHES = 40
#: 单个批次的条目上限（题目 × 模型 的笛卡尔积）
MAX_ITEMS = 100
_LOCK = threading.RLock()
#: batch_id → 批次状态（内存态；落盘只在 batch 目录留一份快照）
_BATCHES: Dict[str, dict] = {}

#: 条目的终态：到这里就不再占槽位，也不许再被调度或对账改写。
#: ``graded`` = 已进台账；``discarded`` = 结束了但没成绩（未校验 / 作废 / 废弃）。
TERMINAL_ITEM_STATUS = {"graded", "discarded", "error", "cancelled", "skipped"}
#: 「已出分，等你结束本轮」：槽位仍然占着，队列不往下派（这就是用户要的节拍）。
AWAITING_STATUS = "awaiting_finish"
#: 还在占用槽位的中间态（准备 / 就绪 / 校验 / 等结束本轮）。
BUSY_ITEM_STATUS = {"preparing", "ready", "grading", AWAITING_STATUS}


# --------------------------------------------------------------------------
# 并发上限
# --------------------------------------------------------------------------

def max_concurrency(cfg: dict, requested: Optional[int] = None) -> int:
    """算出实际并发数：不超过配置上限，也不小于 1。"""
    try:
        pool = max(1, int(cfg.get("max_concurrency") or 1))
    except (TypeError, ValueError):
        pool = 1
    if requested is None:
        return pool
    try:
        want = int(requested)
    except (TypeError, ValueError):
        return pool
    if want < 1:
        return 1
    # 文件夹工作区不需要盘符，仍用配置上限避免线程失控。
    return min(want, pool)


# --------------------------------------------------------------------------
# 批次生命周期
# --------------------------------------------------------------------------

def _now() -> str:
    return util.iso_now()


def _batch_dir(cfg: dict, batch_id: str) -> str:
    return os.path.join(cfg["sandbox_root"], "_batches", util.sanitize_id(batch_id))


def _save_batch(cfg: dict, batch: dict) -> None:
    """把批次快照落盘（用于服务重启后仍能看到历史批次）。"""
    try:
        path = os.path.join(_batch_dir(cfg, batch["batch_id"]), "batch.json")
        util.ensure_dir(os.path.dirname(path))
        # Atomic writes share a pid-based temporary filename; serialize concurrent workers.
        with _LOCK:
            util.write_json_atomic(path, _public_batch(batch))
    except OSError:
        # 落盘失败不影响批次继续跑（内存态才是权威）
        pass


def _public_batch(batch: dict) -> dict:
    """给前端看的批次视图（去掉线程句柄之类的不可序列化字段）。"""
    items = batch["items"]
    return {
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "updated_at": batch["updated_at"],
        "status": batch["status"],
        "mode": batch.get("mode", "interactive"),
        "concurrency": batch["concurrency"],
        "problems": batch.get("problems") or [],
        "total": len(items),
        "done": sum(1 for i in items if i["status"] in TERMINAL_ITEM_STATUS),
        # 「通过」看每一条**最好的一轮**有没有全绿：第 2 轮才做对也算做对，
        # 不能只认第 1 轮（旧实现把出分当终点，第 1 轮 85.7 就永久钉在那一行）。
        "passed": sum(1 for i in items if i.get("best_passed")),
        "running": sum(1 for i in items if i["status"] in BUSY_ITEM_STATUS),
        "awaiting": sum(1 for i in items if i["status"] == AWAITING_STATUS),
        "queued": sum(1 for i in items if i["status"] == "pending"),
        "items": items,
    }


def start(cfg: dict, items: List[dict], concurrency: Optional[int] = None,
          auto_release: bool = True, auto_send: bool = False, log=None) -> dict:
    """建一个并行会话批次并在后台准备独立沙箱。

    :param items: `[{"task": "T1-01", "model": "gpt-x", "attempt": 1}, ...]`
    :param concurrency: 想同时跑几个；默认 = config.max_concurrency
    :param auto_release: 条目落定后是否回收它的沙箱工作区。
    :param auto_send: 沙箱就绪后是否自动把第 1 级提示词发给模型（无人值守作答）。
        默认关闭：跑批历来只负责准备，发送与校验由人驱动。开启后校验仍然手动。

    就绪会话一直占用一个槽位，直到用户在工作台**点「结束本轮」收尾**（或废弃、
    或手动回收后跳过）：校验出分只让条目进入 `awaiting_finish`，不派发下一条。
    """
    if not items:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "批量跑批至少要有一个条目。")
    if len(items) > MAX_ITEMS:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "一次最多 %d 个条目，请拆成多批。" % MAX_ITEMS,
            "收到 %d 个" % len(items),
        )

    # 先把每个条目的题包与模型档案校验一遍：坏条目当场报错，不浪费一轮调度
    prepared: List[dict] = []
    problems: List[str] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            problems.append("第 %d 个条目不是 JSON 对象，已跳过。" % (index + 1))
            continue
        task = str(raw.get("task") or "").strip()
        model = str(raw.get("model") or "").strip()
        attempt = max(1, int(raw.get("attempt") or 1))
        if not task or not model:
            problems.append("第 %d 个条目缺少 task 或 model。" % (index + 1))
            continue
        try:
            meta = packs.load_meta(cfg, task)
            profile = config.find_model(cfg, model)
            if not chat.is_supported_model(profile):
                raise errors.HarnessError(
                    errors.E_CHAT_UNSUPPORTED,
                    "当前批量工作台只支持 OpenAI-compatible Chat Completions 模型。",
                    "protocol=%s api_mode=%s" % (
                        profile.get("protocol"), profile.get("api_mode", config.DEFAULT_OPENAI_API_MODE)),
                )
        except errors.HarnessError as exc:
            problems.append("第 %d 个条目（%s × %s）：%s" % (index + 1, task, model, exc.message))
            continue
        if attempt > meta["attempts"]:
            problems.append(
                "第 %d 个条目（%s）第 %d 轮超出该题上限 %d。"
                % (index + 1, task, attempt, meta["attempts"]))
            continue
        prepared.append({
            "index": index,
            "task": task,
            "model": model,
            "attempt": attempt,
            "title": meta["title"],
            "prompt": next((p.get("text", "") for p in packs.load_prompts(meta)
                            if int(p.get("level", 0)) == attempt), ""),
            "status": "pending",
            "run_id": "",
            "sandbox": "",
            "drive": "",
            # score/passed 是**当前这一轮**的成绩；best_* 是到目前为止最好的一轮。
            # 两者一起给，用户才看得出「第 2 轮才做对」不是没做对。
            "score": None,
            "passed": False,
            "best_score": None,
            "best_passed": False,
            "round": attempt,
            "rounds": 0,
            "best_round": 0,
            "ledgered": False,
            "entry_id": "",
            "error": "",
            "events": [],
            "started_at": "",
            "finished_at": "",
        })

    if not prepared:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "这一批没有可执行的条目。",
            "；".join(problems[:8]),
        )

    batch_id = "batch-%s-%s" % (util.now_stamp(), uuid.uuid4().hex[:6])
    concurrency = max_concurrency(cfg, concurrency)
    batch = {
        "batch_id": batch_id,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "mode": "interactive",
        "concurrency": concurrency,
        "items": prepared,
        "problems": problems,
        "cancel": False,
        "_cancel_event": threading.Event(),
        "auto_release": bool(auto_release),
        "auto_send": bool(auto_send),
    }
    with _LOCK:
        _BATCHES[batch_id] = batch
        _trim_batches()
    _save_batch(cfg, batch)

    thread = threading.Thread(
        target=_run_batch, args=(cfg, batch_id, log), name="batch-%s" % batch_id, daemon=True)
    thread.start()

    payload = _public_batch(batch)
    payload["problems"] = problems
    payload["notice"] = (
        "已开始准备会话：并发 %d。会话就绪后请打开对应工作台；"
        "**每一条都要在工作台点「结束本轮」收尾后，队列里的下一条才会开工**，"
        "校验出分本身不会让出槽位。" % concurrency
    )
    return payload


def _run_snapshot(cfg: dict, run_id: str) -> Optional[dict]:
    """轻量读一条 run 记录（不扫全树）：批次读侧对账用。

    返回 None = 记录目录里没有这条 run（已被收尾出口删掉）；
    返回 {} = 文件在但读不出内容（坏 JSON）——那是「状态未知」，不能当成「已收尾」。
    """
    if not run_id:
        return None
    path = os.path.join(runs.dir_of_run_id(cfg, run_id), "run.json")
    if not os.path.isfile(path):
        return None
    doc = util.read_json(path, default=None)
    return doc if isinstance(doc, dict) else {}


def _write_doc(cfg: dict, doc: dict) -> None:
    try:
        # 与 _save_batch 同口径：内存态里挂着 _cancel_event 这类不可序列化的
        # 内部字段，落盘必须走 _public_batch 白名单，否则对一次账就炸一次。
        util.write_json_atomic(
            os.path.join(_batch_dir(cfg, str(doc.get("batch_id") or "")), "batch.json"),
            _public_batch(doc))
    except OSError:
        pass


def _safe_run(cfg: dict, run_id: str) -> Optional[dict]:
    """读一条运行记录；记录已被「结束本轮 / 废弃本轮」收走时返回 None。

    记录消失是**正常终态**而不是错误：收尾出口的全部磁盘后果就是把它删干净，
    批次必须据此落定，而不是把条目永远挂在「等评分」上。
    """
    if not run_id:
        return None
    try:
        return runs.get_run(cfg, run_id)
    except errors.HarnessError as exc:
        if exc.code == errors.E_RUN_NOT_FOUND:
            return None
        raise


def _round_no_of(rnd: dict) -> int:
    try:
        return int(rnd.get("attempt") or 0)
    except (TypeError, ValueError):
        return 0


def _round_summary(run: dict) -> dict:
    """从运行记录里算「到目前这一轮为止」的成绩口径。

    作数轮 = 既没被作废（voided）也不判无效（invalidated）的轮次，与台账同口径。
    ``score``/``passed`` 是**当前这一轮**（run.last_*），``best_*`` 是最好的一轮。
    """
    def _score(rnd: dict) -> float:
        try:
            return float(rnd.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    rounds = [r for r in (run.get("rounds") or [])
              if isinstance(r, dict) and not r.get("voided") and not r.get("invalidated")]
    best: Optional[dict] = None
    for rnd in rounds:
        if best is None or (_score(rnd), 1 if rnd.get("passed") else 0) > (
                _score(best), 1 if best.get("passed") else 0):
            best = rnd
    last_score = run.get("last_score")
    return {
        "round": int(run.get("attempt") or 1),
        "rounds": len(rounds),
        "score": last_score,
        "passed": bool(run.get("last_passed")),
        "best_score": _score(best) if best is not None else last_score,
        "best_passed": bool(best.get("passed")) if best is not None else bool(run.get("last_passed")),
        "best_round": _round_no_of(best) if best is not None else int(run.get("attempt") or 1),
    }


def _apply_round_summary(item: dict, run: dict) -> None:
    """把运行记录里的轮次口径刷进条目（槽位保留期间每次对账都刷一遍）。"""
    summary = _round_summary(run)
    item["round"] = summary["round"]
    item["rounds"] = summary["rounds"]
    item["score"] = summary["score"]
    item["passed"] = summary["passed"]
    item["best_score"] = summary["best_score"]
    item["best_passed"] = summary["best_passed"]
    item["best_round"] = summary["best_round"]


def _ledger_index(cfg: dict) -> Dict[str, dict]:
    """{source_run_id: 台账条目}：读侧对账一次读全，别按条目反复扫台账文件。"""
    try:
        entries = results_ledger.load_entries(cfg)
    except Exception:  # noqa: BLE001 - 台账读不出来就退化成"没有条目"
        return {}
    out: Dict[str, dict] = {}
    for entry in entries:
        run_id = str(entry.get("source_run_id") or "")
        if run_id:
            out[run_id] = entry
    return out


def _settle_from_ledger(cfg: dict, item: dict, entries: Optional[Dict[str, dict]] = None) -> None:
    """运行记录已经消失：按台账把条目落定成终态。

    台账是最终成绩的唯一权威（记分板与排行榜都只读它）：有条目 = 用户点了
    「结束本轮」，分数、轮数、通过与否一律抄台账，不用批次自己那份可能过期的
    最后一轮数字；没条目 = 废弃本轮（或没成绩就结束），不留成绩。
    """
    run_id = str(item.get("run_id") or "")
    if entries is not None:
        entry = entries.get(run_id)
    else:
        try:
            entry = results_ledger.find_by_source_run(cfg, run_id)
        except Exception:  # noqa: BLE001 - 台账读失败不该把条目卡在中间态
            entry = None
    if entry:
        item["status"] = "graded"
        item["ledgered"] = True
        item["entry_id"] = str(entry.get("entry_id") or "")
        item["score"] = entry.get("score")
        item["passed"] = bool(entry.get("passed"))
        item["best_score"] = entry.get("score")
        item["best_passed"] = bool(entry.get("passed"))
        item["rounds"] = int(entry.get("rounds") or 1)
        item["best_round"] = int(entry.get("best_round") or 1)
        item["round"] = int(entry.get("best_round") or item.get("round") or 1)
        _add_event(item, "已结束本轮：成绩已记入台账（代表分 %s 分，%s）"
                   % (entry.get("score"), "全绿" if entry.get("passed") else "未全绿"), "graded")
    else:
        item["status"] = "discarded"
        item["ledgered"] = False
        item["entry_id"] = ""
        _add_event(item, "这一轮已结束，但没有可计入台账的成绩（未校验 / 已作废 / 已废弃）", "discarded")
    item["finished_at"] = _now()


def _reconcile(cfg: dict, doc: dict) -> dict:
    """按 run 的真实状态补齐批次条目（只读侧对账，不派发）。

    服务一重启就会带走批次里的监控线程，条目于是永远停在「工作区就绪，等待评分」，
    而工作台里那一轮早就校验完了——人还会照着旧状态再点一次启动评分。
    读的时候对一次账并落盘，批次总状态也跟着收敛。

    与内存态监控同一套口径：run 有报告 → `awaiting_finish`（等结束本轮）；
    run 不见了 → 从台账落定终态；**已经是 graded 的旧条目也拿台账校一遍**
    （2026-10-02 之前的老快照把第 1 轮的分数钉成了最终成绩，T3-08 那行因此
    一直写着「85.7 未通过」，而台账里它是第 2 轮 100 分通过）。
    """
    changed = False
    items = doc.get("items") or []
    # 一次读全台账：几百条条目也不该按条目反复扫文件
    needs_ledger = any(
        str(i.get("run_id") or "") and (str(i.get("status") or "") == "graded"
                                        or str(i.get("status") or "") not in TERMINAL_ITEM_STATUS)
        for i in items)
    entries = _ledger_index(cfg) if needs_ledger else {}

    for item in items:
        run_id = str(item.get("run_id") or "")
        status = str(item.get("status") or "")
        if not run_id or status == "pending":
            continue
        if status in TERMINAL_ITEM_STATUS and status != "graded":
            continue
        if status == "graded":
            # 终态校对：老实现会在第 1 轮出分那一刻把条目判成「已完成」并钉死分数
            entry = entries.get(run_id)
            if entry and (
                    item.get("score") != entry.get("score")
                    or bool(item.get("passed")) != bool(entry.get("passed"))
                    or int(item.get("rounds") or 0) != int(entry.get("rounds") or 1)):
                item["score"] = entry.get("score")
                item["passed"] = bool(entry.get("passed"))
                item["best_score"] = entry.get("score")
                item["best_passed"] = bool(entry.get("passed"))
                item["rounds"] = int(entry.get("rounds") or 1)
                item["best_round"] = int(entry.get("best_round") or 1)
                item["ledgered"] = True
                item["entry_id"] = str(entry.get("entry_id") or "")
                _add_event(item, "按台账校正代表分：第 %d 轮 %s 分"
                           % (int(entry.get("best_round") or 1), entry.get("score")), "graded")
                changed = True
            elif entry and item.get("entry_id") != entry.get("entry_id"):
                item["ledgered"] = True
                item["entry_id"] = str(entry.get("entry_id") or "")
                changed = True
            continue
        snap = _run_snapshot(cfg, run_id)
        if snap is None:
            _settle_from_ledger(cfg, item, entries)
            changed = True
            continue
        if not snap:
            continue      # 记录读不出来：状态未知，保持原状等人来看
        run_status = str(snap.get("status") or "")
        if run_status in {"error", "cancelled"}:
            item["status"] = run_status
            item["finished_at"] = item.get("finished_at") or _now()
            if run_status == "error":
                err = snap.get("last_error") or {}
                item["error"] = (err.get("message") if isinstance(err, dict) else "") or "校验失败"
        elif run_status == "graded":
            _apply_round_summary(item, snap)
            item["status"] = AWAITING_STATUS
        else:
            _apply_round_summary(item, snap)
            item["status"] = run_status if run_status in {"preparing", "grading"} else "ready"
        _add_event(item, "批次监控已中断，按运行记录补齐状态", "ready")
        changed = True
    # 计数是落盘时快照下来的，补齐状态后必须一起重算，否则「1/3 完成」会一直骗人
    done = [i for i in items if i.get("status") in TERMINAL_ITEM_STATUS]
    counters = {
        "done": len(done),
        "passed": sum(1 for i in items if i.get("best_passed")),
        "running": sum(1 for i in items if i.get("status") in BUSY_ITEM_STATUS),
        "awaiting": sum(1 for i in items if i.get("status") == AWAITING_STATUS),
        "queued": sum(1 for i in items if i.get("status") == "pending"),
    }
    if items and all(i.get("status") in TERMINAL_ITEM_STATUS for i in items):
        counters["status"] = "finished"
    stale = any(doc.get(key) != value for key, value in counters.items())
    if not changed and not stale:
        return doc
    doc.update(counters)
    doc["updated_at"] = _now()
    _write_doc(cfg, doc)
    return doc


def release(cfg: dict, batch_id: str, index: int) -> dict:
    """手动回收某一条的沙箱工作区。

    批次自动释放只发生在监控线程看到评分结束的那一刻；线程一死（服务重启）
    就没人释放磁盘，校验完的沙箱一直躺在 sandboxes/ 下。这个入口让人主动关掉。
    只删沙箱目录，`runs/` 里的记录、报告、diff 全部保留。
    """
    try:
        pos = int(index)
    except (TypeError, ValueError):
        raise errors.HarnessError(errors.E_BAD_REQUEST, "批次条目序号必须是整数。")
    with _LOCK:
        batch = _BATCHES.get(batch_id)
    doc = batch if batch is not None else get(cfg, batch_id)
    doc = _reconcile(cfg, doc)
    items = doc.get("items") or []
    if not 0 <= pos < len(items):
        raise errors.HarnessError(errors.E_BAD_REQUEST, "没有这个批次条目。")
    item = items[pos]
    run = _run_snapshot(cfg, str(item.get("run_id") or ""))
    if not str(item.get("sandbox") or ""):
        return {"released": False, "message": "这一条已经没有可回收的沙箱工作区。"}
    if run:
        # 走门面：独占锁、在飞闸门、状态守卫都在 runs.release_sandbox 一处，
        # 这里不再自己拆 sandbox.destroy（两份实现必然漂移）。
        result = runs.release_sandbox(cfg, str(run.get("run_id") or ""))
        if not result.get("released"):
            return {"released": False, "batch_id": batch_id, "index": pos,
                    "message": result.get("message") or "这一条已经没有可回收的沙箱工作区。"}
    item["sandbox"] = ""
    _add_event(item, "沙箱工作区已手动回收（运行记录与报告保留）", "ready")
    doc["updated_at"] = _now()
    if batch is None:
        _write_doc(cfg, doc)
    else:
        _save_batch(cfg, batch)
    return {"released": True, "batch_id": batch_id, "index": pos,
            "message": "沙箱工作区已回收；成绩与报告仍在 runs/ 里。"}


def get(cfg: dict, batch_id: str) -> dict:
    """取批次状态（优先内存态，其次落盘快照）。"""
    with _LOCK:
        batch = _BATCHES.get(batch_id)
        if batch:
            payload = _public_batch(batch)
            payload["problems"] = batch.get("problems") or []
            return payload
    path = os.path.join(_batch_dir(cfg, batch_id), "batch.json")
    doc = util.read_json(path, default=None)
    if isinstance(doc, dict):
        return _reconcile(cfg, doc)
    raise errors.HarnessError(
        errors.E_NOT_FOUND, "找不到这个批次（可能已被清理）。", batch_id)


def list_batches(cfg: dict) -> dict:
    """列出已知批次（内存 + 落盘），按时间倒序。"""
    out: Dict[str, dict] = {}
    root = os.path.join(cfg["sandbox_root"], "_batches")
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name, "batch.json")
            doc = util.read_json(path, default=None)
            if isinstance(doc, dict) and doc.get("batch_id"):
                out[doc["batch_id"]] = _reconcile(cfg, doc)
    with _LOCK:
        for batch_id, batch in _BATCHES.items():
            payload = _public_batch(batch)
            payload["problems"] = batch.get("problems") or []
            out[batch_id] = payload
    items = sorted(out.values(), key=lambda b: str(b.get("created_at") or ""), reverse=True)
    return {"batches": items, "count": len(items)}


def cancel(cfg: dict, batch_id: str) -> dict:
    """请求取消：只拦还没开工的条目，已派发的条目自然跑完。

    2026-10-02 之前的行为是把 preparing/ready/grading 一并判死（写
    cancel_requested、把 ready 改成 cancelled）并回收沙箱，模型做完的
    工作跟着一起被删；现在停止只负责「不再派发新条目」，在飞条目的
    收尾（评分 / 结束 / 废弃）回到工作台自己的出口。
    """
    with _LOCK:
        batch = _BATCHES.get(batch_id)
        if not batch:
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "这个批次不在运行中（可能已经结束）。",
                batch_id,
            )
        if batch.get("status") in {"finished", "cancelled"}:
            return {"batch_id": batch_id, "status": batch["status"]}
        batch["cancel"] = True
        _cancel_event(batch).set()
        batch["status"] = "cancelling"
        for item in batch.get("items") or []:
            if item.get("status") == "pending":
                _mark_item_cancelled(batch, item, "排队会话已取消")
        batch["updated_at"] = _now()
    _save_batch(cfg, batch)
    return {"batch_id": batch_id, "status": "cancelling"}


def _cancel_event(batch: dict) -> threading.Event:
    """取批次内部取消信号；兼容从测试/旧快照构造的批次。"""
    event = batch.get("_cancel_event")
    if not isinstance(event, threading.Event):
        event = threading.Event()
        batch["_cancel_event"] = event
    if batch.get("cancel"):
        event.set()
    return event


def _mark_item_cancelled(batch: dict, item: dict, message: str = "会话已取消") -> None:
    """幂等地把条目推到终态，确保 done/queued 与前端一致。"""
    if item.get("status") in TERMINAL_ITEM_STATUS:
        return
    item["status"] = "cancelled"
    item["finished_at"] = _now()
    batch["updated_at"] = _now()
    _add_event(item, message, "cancelled")


def _mark_item_skipped(batch: dict, item: dict, message: str) -> None:
    """把「已出分但不再等结束本轮」的条目推到终态并让出槽位。

    成绩不会因为跳过而丢：工作台那边照样能结束本轮，台账条目照样生成，
    这条只是不再占着批次的槽位（批次已停止 / 用户主动跳过时用）。
    """
    if item.get("status") in TERMINAL_ITEM_STATUS:
        return
    item["status"] = "skipped"
    item["finished_at"] = _now()
    batch["updated_at"] = _now()
    _add_event(item, message, "skipped")


def _trim_batches() -> None:
    """内存里只保留最近 MAX_BATCHES 个批次。"""
    if len(_BATCHES) <= MAX_BATCHES:
        return
    ordered = sorted(_BATCHES.values(), key=lambda b: str(b.get("created_at") or ""))
    for stale in ordered[:len(_BATCHES) - MAX_BATCHES]:
        _BATCHES.pop(stale["batch_id"], None)


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------

def _run_batch(cfg: dict, batch_id: str, log) -> None:
    """批次线程体：按工作区槽位派发会话，等用户「结束本轮」后继续排队。"""
    emit = log or (lambda m: None)
    with _LOCK:
        batch = _BATCHES.get(batch_id)
    if not batch:
        return

    items = batch["items"]
    limit = max(1, int(batch["concurrency"]))
    cancel_event = _cancel_event(batch)
    # 用信号量把「准备中 / 就绪 / 校验中 / 等结束本轮」的条目卡在配置上限内。
    # 槽位的释放点 = 条目落定（用户点了结束本轮或废弃），不是校验出分那一刻。
    gate = threading.Semaphore(limit)
    threads: List[threading.Thread] = []

    for item in items:
        if item.get("status") in TERMINAL_ITEM_STATUS:
            continue          # 已落定（含排队中被移除的）：不派发、不重复收尾
        if cancel_event.is_set():
            with _LOCK:
                _mark_item_cancelled(batch, item, "排队会话已取消")
            _save_batch(cfg, batch)
            continue
        acquired = False
        while not cancel_event.is_set():
            if gate.acquire(timeout=0.1):
                acquired = True
                break
        if not acquired:
            with _LOCK:
                _mark_item_cancelled(batch, item, "排队会话已取消")
            _save_batch(cfg, batch)
            continue
        worker = threading.Thread(
            target=_run_item, args=(cfg, batch, item, gate, emit, cancel_event),
            name="batch-item-%d" % item["index"], daemon=True)
        threads.append(worker)
        worker.start()

    for worker in threads:
        worker.join()

    with _LOCK:
        batch["status"] = "cancelled" if cancel_event.is_set() else "finished"
        batch["updated_at"] = _now()
    _save_batch(cfg, batch)
    summary = _public_batch(batch)
    emit("批次 %s 结束：%d/%d 完成，%d 通过" % (
        batch_id, summary["done"], summary["total"], summary["passed"]))


def _auto_send_first_prompt(cfg: dict, run: dict, batch: dict, item: dict) -> None:
    """把第 1 级提示词发给模型（跑批勾了「自动发送」时）。

    发送失败不毁掉这一轮：工作区已经就绪，用户还能在工作台手动发，
    所以只记一条事件说明原因，不把条目判成 error。
    """
    try:
        meta = packs.load_meta(cfg, str(run.get("task") or ""))
        level1 = [p for p in packs.load_prompts(meta) if int(p.get("level") or 0) == 1]
        if not level1 or not str(level1[0].get("text") or "").strip():
            raise errors.HarnessError(errors.E_TASK_INVALID, "这道题没有第 1 级提示词。")
        chat.start_send(cfg, run, level1[0]["text"])
        message = "已自动发送第 1 级提示词，模型开始作答"
        kind = "ready"
    except errors.HarnessError as exc:
        message = "自动发送失败：%s 请在工作台手动发送。" % exc.message
        kind = "error"
    with _LOCK:
        _add_event(item, message, kind)
        batch["updated_at"] = _now()
    _save_batch(cfg, batch)


def _run_item(cfg: dict, batch: dict, item: dict, gate: threading.Semaphore, emit,
              cancel_event: threading.Event | None = None) -> None:
    """准备一个独立会话，然后守着它直到用户「结束本轮」，再让出槽位。

    槽位的持有期是这次尝试的**完整生命周期**（准备 → 作答 → 校验 → 结束本轮），
    不是「到出分为止」：出分只把条目推进到 ``awaiting_finish``。
    """
    run = None
    released = False
    cancel_event = cancel_event or _cancel_event(batch)
    try:
        with _LOCK:
            if cancel_event.is_set() or item.get("status") in TERMINAL_ITEM_STATUS:
                # 派发与停止/移除之间的竞态：条目已经不需要跑了，原样退出（幂等）。
                _mark_item_cancelled(batch, item, "排队会话已取消")
                return
            item["status"] = "preparing"
            item["started_at"] = _now()
            batch["updated_at"] = _now()
            _add_event(item, "正在准备独立工作区", "preparing")
        _save_batch(cfg, batch)

        if cancel_event.is_set():
            raise errors.HarnessError(errors.E_RUN_CANCELLED, "批次已取消，未开始准备沙箱。")

        # 已派发的条目不再受停止影响：prepare 不传取消信号，让它建完沙箱，
        # 模型的工作与评分都留在工作台自己的出口上收尾（2026-10-02 语义修正）。
        kwargs = {
            "claim_queued": False,
            "wait_s": 30.0,
            "log": lambda m: emit("[%s×%s] %s" % (item["task"], item["model"], m)),
        }
        run = runs.create_run(
            cfg, item["task"], item["model"], item["attempt"], **kwargs)

        with _LOCK:
            item["run_id"] = run["run_id"]
            item["sandbox"] = run.get("sandbox", "")
            item["drive"] = run.get("drive", "")
            item["status"] = "ready"
            item["round"] = int(run.get("attempt") or item.get("attempt") or 1)
            batch["updated_at"] = _now()
            _add_event(item, "工作区已就绪，等待模型操作与人工评分", "ready")
        _save_batch(cfg, batch)

        # 无人值守作答：跑批建好沙箱后直接把第 1 级提示词交给模型。
        # 只发不收——校验仍然由人启动（§3 的口径）。
        if batch.get("auto_send"):
            _auto_send_first_prompt(cfg, run, batch, item)

        # 守到这次尝试被收尾为止：记录消失（结束本轮 / 废弃本轮）才是终点。
        # 批量会话不代替用户触发评分，也不代替用户结束本轮。
        final = None
        settled = False
        while True:
            current = _safe_run(cfg, run["run_id"])
            if current is None:
                # 记录已被收尾出口整条删掉：按台账落定（这就是 fix：不再把
                # 第 1 轮的分数钉死，也不再在第一轮出分后就判「已完成」）。
                with _LOCK:
                    _settle_from_ledger(cfg, item)
                    batch["updated_at"] = _now()
                _save_batch(cfg, batch)
                settled = True
                break

            current_status = str(current.get("status") or "")
            with _LOCK:
                changed = False
                _apply_round_summary(item, current)
                if current_status in {"error", "cancelled"}:
                    final = current
                elif current_status == "graded":
                    if item.get("status") != AWAITING_STATUS:
                        item["status"] = AWAITING_STATUS
                        _add_event(item, "第 %d 轮已出分（%s 分），等你回工作台点「结束本轮」"
                                   % (int(current.get("attempt") or 1), current.get("last_score")),
                                   "awaiting_finish")
                        changed = True
                elif current_status == "grading":
                    if item.get("status") != "grading":
                        item["status"] = "grading"
                        _add_event(item, "工作台已启动校验", "grading")
                        changed = True
                elif item.get("status") != "ready":
                    item["status"] = "ready"
                    changed = True
                if changed:
                    batch["updated_at"] = _now()
            if changed:
                _save_batch(cfg, batch)
            if final is not None:
                break

            if cancel_event.is_set() and current_status == "graded":
                # 停止批次：出分即让出槽位，不再等「结束本轮」。成绩不会丢——
                # 工作台那边照样可以结束本轮，台账条目照样生成，批次不去抢。
                with _LOCK:
                    _mark_item_skipped(
                        batch, item,
                        "批次已停止：这一条已出分，成绩以工作台「结束本轮」为准")
                _save_batch(cfg, batch)
                settled = True
                break

            if cancel_event.is_set():
                # 停止后不再打扰本条目：保持低频观察，等它自然落定。
                time.sleep(0.5)
            else:
                cancel_event.wait(0.5)

        if final is not None:
            with _LOCK:
                if final.get("status") == "cancelled":
                    _mark_item_cancelled(batch, item, "运行记录已取消")
                else:
                    item["status"] = "error"
                    err = final.get("last_error") or {}
                    message = err.get("message") if isinstance(err, dict) else ""
                    item["error"] = message or "校验失败"
                    item["finished_at"] = _now()
                    batch["updated_at"] = _now()
                    _add_event(item, "评分失败", "error")
            _save_batch(cfg, batch)

        if settled and item.get("status") in {"graded", "discarded"}:
            # 记录已被收尾出口删掉，沙箱跟着一起没了：只清字段，不去碰回收门面
            # （对着一条不存在的记录调 release_sandbox 只会刷一条假报错）。
            _clear_item_workspace(cfg, batch, item)
        elif _release_item_sandbox(cfg, batch, run, emit):
            _clear_item_workspace(cfg, batch, item)
        released = True
    except errors.HarnessError as exc:
        if exc.code == errors.E_RUN_CANCELLED:
            # 只可能来自「派发后、建 run 前」的取消：条目还没开工，按未跑处理。
            with _LOCK:
                _mark_item_cancelled(batch, item, "会话已取消")
            _save_batch(cfg, batch)
            if run:
                if _release_item_sandbox(cfg, batch, run, emit):
                    _clear_item_workspace(cfg, batch, item)
                    released = True
        else:
            with _LOCK:
                item["status"] = "error"
                item["error"] = exc.message
                item["finished_at"] = _now()
                batch["updated_at"] = _now()
                _add_event(item, "会话失败：%s" % exc.message, "error")
            _save_batch(cfg, batch)
            emit("[%s×%s] 条目失败：%s" % (item["task"], item["model"], exc.message))
    except Exception as exc:  # noqa: BLE001 - 单条失败绝不能带崩整批
        with _LOCK:
            item["status"] = "error"
            item["error"] = "未预期错误：%r" % exc
            item["finished_at"] = _now()
            batch["updated_at"] = _now()
            _add_event(item, "会话异常", "error")
        _save_batch(cfg, batch)
        emit("[%s×%s] 条目异常：%r" % (item["task"], item["model"], exc))
    finally:
        if run and not released and item.get("status") in TERMINAL_ITEM_STATUS:
            if _release_item_sandbox(cfg, batch, run, emit):
                _clear_item_workspace(cfg, batch, item)
        gate.release()


def remove_item(cfg: dict, batch_id: str, index: int) -> dict:
    """把一条移出批次：排队中的叫「移除」，已出分等结束本轮的叫「跳过」。

    只对两种状态生效：

    - `pending`：还没开工，移出后永远不会被派发；
    - `awaiting_finish`：已经出分但用户还没回工作台点「结束本轮」。用户可能
      先去看别的题、或者干脆想放一放——这一条不该把整条队列堵死在这里。
      **跳过不动它的运行记录与成绩**：工作台那边照常结束本轮，台账条目照常生成。

    其余状态（准备中/作答中/校验中）归工作台自己的出口管，批次侧绝不满地杀。
    """
    try:
        pos = int(index)
    except (TypeError, ValueError):
        raise errors.HarnessError(errors.E_BAD_REQUEST, "批次条目序号必须是整数。")
    with _LOCK:
        batch = _BATCHES.get(batch_id)
    doc = batch if batch is not None else get(cfg, batch_id)
    doc = _reconcile(cfg, doc)
    items = doc.get("items") or []
    if not 0 <= pos < len(items):
        raise errors.HarnessError(errors.E_BAD_REQUEST, "没有这个批次条目。")
    item = items[pos]
    status = str(item.get("status") or "")
    if status == "pending":
        item["status"] = "cancelled"
        item["finished_at"] = _now()
        doc["updated_at"] = _now()
        _add_event(item, "排队中移除：这条还没有开工，不会再派发", "cancelled")
        message = "条目已移出批次；正在跑的条目不受影响。"
    elif status == AWAITING_STATUS:
        item["status"] = "skipped"
        item["finished_at"] = _now()
        doc["updated_at"] = _now()
        _add_event(item, "已跳过：这一条已出分，槽位让给队列；"
                         "工作台里照样可以「结束本轮」，成绩照样进台账", "skipped")
        message = "已跳过这一条，槽位让给队列；它的运行记录与成绩仍归工作台管。"
    else:
        raise errors.HarnessError(
            errors.E_BAD_REQUEST,
            "这一条现在是「%s」，不能移出：排队中的可以移除，已出分等结束本轮的可以跳过；"
            "其余状态请到工作台收尾。" % (status or "未知"))
    if batch is None:
        _write_doc(cfg, doc)
    else:
        _save_batch(cfg, batch)
    return {"batch_id": batch_id, "index": pos, "removed": True,
            "status": item["status"], "message": message}


def _add_event(item: dict, message: str, kind: str) -> None:
    events = item.setdefault("events", [])
    events.append({"at": _now(), "kind": kind, "message": message})
    del events[:-30]


def _clear_item_workspace(cfg: dict, batch: dict, item: dict) -> None:
    with _LOCK:
        item["sandbox"] = ""
        item["drive"] = ""
        batch["updated_at"] = _now()
        _add_event(item, "工作区槽位已释放", "released")
    _save_batch(cfg, batch)


def _release_item_sandbox(cfg: dict, batch: dict, run: dict, emit) -> bool:
    """回收这一条的沙箱工作区，把位置让给还没跑的条目。

    只回收沙箱目录；`runs/<题>/<模型>/<时间>/` 里的
    run.json / report.json / diff.patch 全部保留，记分板照常统计。
    必须走 runs.release_sandbox 这道门面（独占锁 + 在飞闸门 + 状态守卫都在那）：
    2026-10-02 的事故里这里直接调 sandbox.destroy，模型晚了一分钟还在写，
    工作区被删得只剩残留。在飞时拒绝回收并保留目录，返回 False。
    """
    if not batch.get("auto_release"):
        return False
    try:
        latest = runs.get_run(cfg, run["run_id"])
    except Exception:
        latest = {}
    # 评分落定但轮次还没用完：沙箱里的模型代码是工作台「进入第二轮」的起点，
    # 这时回收会让 promote 只剩一条空记录（2026-10-02 T2-04 实测）。留给用户
    # 自己的出口收尾：继续第二轮 / 结束本轮 / 废弃 / 批次视图手动回收。
    if (latest.get("status") == "graded"
            and int(latest.get("attempt") or 1) < int(latest.get("attempts_allowed") or 1)):
        emit("[%s×%s] 轮次还有剩余，沙箱保留供第二轮使用"
             % (run.get("task"), run.get("model")))
        return False
    try:
        result = runs.release_sandbox(cfg, run["run_id"])
    except errors.HarnessError as exc:
        emit("[%s×%s] 工作区暂不回收：%s" % (run.get("task"), run.get("model"), exc.message))
        return False
    except Exception as exc:  # noqa: BLE001 - 回收失败不该翻掉已拿到成绩
        emit("[%s×%s] 回收沙箱失败（不影响成绩）：%r"
             % (run.get("task"), run.get("model"), exc))
        return False
    run.update(runs.get_run(cfg, run["run_id"]))
    if result.get("released"):
        emit("[%s×%s] 已回收沙箱工作区" % (run.get("task"), run.get("model")))
    elif result.get("message"):
        emit("[%s×%s] %s" % (run.get("task"), run.get("model"), result["message"]))
    return bool(result.get("released"))
