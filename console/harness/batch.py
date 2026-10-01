"""并行会话：一次为「多道题 × 多个模型」准备独立工作区。

设计要点（文件夹沙箱）：

1. **并发闸门只限制工作线程数量**。每个 run 使用 sandbox_root 下的独立目录，
   不创建盘符映射；默认并发由 config.max_concurrency 控制，也可以显式调小。
2. **准备沙箱本身是同步阻塞的**（`runs.create_run` 内部铺树 + 注入 + git init），
   放到线程池里跑，HTTP 请求立刻返回批次 id，前端轮询进度——与单轮校验的
   异步语义保持一致（`POST /api/runs/{id}/grade` 也是异步的）。
3. **每个 item 独立成败**。一道题失败（题包坏了、模型档案没了）不能拖垮整批，
   记到该 item 的 `error` 里，批次继续。
4. 每个会话准备后保持就绪，直到用户在对应工作台提交评分；评分完成后回收工作区，
   再继续准备排队会话。run 仍使用原有落盘格式。

线程模型：一个「批次线程」负责调度，每个 item 再交给一个工作线程；
批次线程只做派发与状态汇总，不持有任何长事务锁。
"""

from __future__ import annotations

import os
import inspect
import threading
import time
import uuid
from typing import Dict, List, Optional

from . import chat, config, errors, packs, runs, sandbox, util

#: 同时保留的批次（内存态）上限，防止长时间运行堆爆
MAX_BATCHES = 40
#: 单个批次的条目上限（题目 × 模型 的笛卡尔积）
MAX_ITEMS = 100
_LOCK = threading.RLock()
#: batch_id → 批次状态（内存态；落盘只在 batch 目录留一份快照）
_BATCHES: Dict[str, dict] = {}


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
        "total": len(batch["items"]),
        "done": sum(1 for i in items if i["status"] in {"graded", "error", "cancelled"}),
        "passed": sum(1 for i in items if i["status"] == "graded" and i.get("passed")),
        "running": sum(1 for i in items if i["status"] in {"preparing", "ready", "grading"}),
        "queued": sum(1 for i in items if i["status"] == "pending"),
        "items": items,
    }


def start(cfg: dict, items: List[dict], concurrency: Optional[int] = None,
          auto_release: bool = True, auto_send: bool = False, log=None) -> dict:
    """建一个并行会话批次并在后台准备独立沙箱。

    :param items: `[{"task": "T1-01", "model": "gpt-x", "attempt": 1}, ...]`
    :param concurrency: 想同时跑几个；默认 = config.max_concurrency
    :param auto_release: 每条完成评分后是否回收它的沙箱工作区。
    :param auto_send: 沙箱就绪后是否自动把第 1 级提示词发给模型（无人值守作答）。
        默认关闭：跑批历来只负责准备，发送与校验由人驱动。开启后校验仍然手动。

        就绪会话一直占用一个槽位。用户在该 run 的工作台操作并启动评分后，批次
        记录成绩并回收工作区，再派发下一条，避免清掉仍在使用的工作区。
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
            "score": None,
            "passed": False,
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
        "提交评分并结束后，系统会回收该工作区并继续准备队列。" % concurrency
    )
    return payload


def _run_snapshot(cfg: dict, run_id: str) -> dict:
    """轻量读一条 run 记录（不扫全树）：批次读侧对账用。"""
    if not run_id:
        return {}
    doc = util.read_json(os.path.join(runs.dir_of_run_id(cfg, run_id), "run.json"), default=None)
    return doc if isinstance(doc, dict) else {}


def _write_doc(cfg: dict, doc: dict) -> None:
    try:
        util.write_json_atomic(
            os.path.join(_batch_dir(cfg, str(doc.get("batch_id") or "")), "batch.json"), doc)
    except OSError:
        pass


def _reconcile(cfg: dict, doc: dict) -> dict:
    """按 run 的真实状态补齐批次条目。

    服务一重启就会带走批次里的监控线程，条目于是永远停在「工作区就绪，等待评分」，
    而工作台里那一轮早就校验完了——人还会照着旧状态再点一次启动评分。
    读的时候对一次账并落盘，批次总状态也跟着收敛。
    """
    changed = False
    for item in doc.get("items") or []:
        run_id = str(item.get("run_id") or "")
        if not run_id or item.get("status") not in {"pending", "preparing", "ready"}:
            continue
        snap = _run_snapshot(cfg, run_id)
        status = str(snap.get("status") or "")
        if status not in {"graded", "error", "cancelled"}:
            continue
        item["status"] = status
        if snap.get("last_score") is not None:
            item["score"] = snap.get("last_score")
            item["passed"] = bool(snap.get("last_passed"))
        _add_event(item, "批次监控已中断，按运行记录补齐状态", "ready")
        changed = True
    items = doc.get("items") or []
    # 计数是落盘时快照下来的，补齐状态后必须一起重算，否则「1/3 完成」会一直骗人
    done = [i for i in items if i.get("status") in {"graded", "error", "cancelled"}]
    counters = {
        "done": len(done),
        "passed": sum(1 for i in done if i.get("passed")),
        "running": sum(1 for i in items if i.get("status") in {"preparing", "ready", "grading"}),
        "queued": sum(1 for i in items if i.get("status") == "pending"),
    }
    if items and all(i.get("status") in {"graded", "error", "cancelled"} for i in items):
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
    if str(run.get("status") or "") in {"preparing", "grading"}:
        raise errors.HarnessError(
            errors.E_RUN_BUSY, "这一条正在准备或校验中，先等它结束再回收沙箱。")
    if not str(item.get("sandbox") or ""):
        return {"released": False, "message": "这一条已经没有可回收的沙箱工作区。"}
    if run:
        sandbox.destroy(cfg, run, log=lambda m: None)
        run["sandbox"] = ""
        run["drive"] = ""
        runs.save_run(cfg, run)
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
    """请求取消：停止排队/准备，已经评分的条目让当前校验自然收尾。"""
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
            elif item.get("run_id") and item.get("status") in {"preparing", "ready", "grading"}:
                _request_run_cancel(cfg, item["run_id"])
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
    if item.get("status") in {"graded", "error", "cancelled"}:
        return
    item["status"] = "cancelled"
    item["finished_at"] = _now()
    batch["updated_at"] = _now()
    _add_event(item, message, "cancelled")


def _request_run_cancel(cfg: dict, run_id: str) -> None:
    """在取消与用户点击评分之间建立闸门。"""
    try:
        run = runs.get_run(cfg, run_id)
        if run.get("status") in {"graded", "error", "cancelled"}:
            return
        run["cancel_requested"] = True
        if run.get("status") == "ready":
            run["status"] = "cancelled"
            run["last_error"] = {
                "code": errors.E_RUN_CANCELLED,
                "message": "批次已取消，这个工作区不会再启动评分。",
            }
        runs.save_run(cfg, run)
    except Exception:
        # worker 随后会再次读取并回收；取消请求本身不能被历史记录异常阻塞。
        return


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
    """批次线程体：按工作区槽位派发会话，等人工评分后继续排队。"""
    emit = log or (lambda m: None)
    with _LOCK:
        batch = _BATCHES.get(batch_id)
    if not batch:
        return

    items = batch["items"]
    limit = max(1, int(batch["concurrency"]))
    cancel_event = _cancel_event(batch)
    # 用信号量把同时准备或等待评分的条目卡在配置上限内
    gate = threading.Semaphore(limit)
    threads: List[threading.Thread] = []

    for item in items:
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
    """准备一个独立会话；取消时协作终止准备并回收槽位。"""
    run = None
    released = False
    cancel_event = cancel_event or _cancel_event(batch)
    try:
        with _LOCK:
            if cancel_event.is_set():
                _mark_item_cancelled(batch, item, "排队会话已取消")
                return
            item["status"] = "preparing"
            item["started_at"] = _now()
            batch["updated_at"] = _now()
            _add_event(item, "正在准备独立工作区", "preparing")
        _save_batch(cfg, batch)

        if cancel_event.is_set():
            raise errors.HarnessError(errors.E_RUN_CANCELLED, "批次已取消，未开始准备沙箱。")

        # 旧测试桩没有 cancel_event 参数，只有真正支持时才传入，兼容既有调用方。
        kwargs = {
            "claim_queued": False,
            "wait_s": 30.0,
            "log": lambda m: emit("[%s×%s] %s" % (item["task"], item["model"], m)),
        }
        try:
            signature = inspect.signature(runs.create_run)
            supports_cancel = (
                "cancel_event" in signature.parameters
                or any(p.kind == inspect.Parameter.VAR_KEYWORD
                       for p in signature.parameters.values())
            )
        except (TypeError, ValueError):
            supports_cancel = True
        if supports_cancel:
            kwargs["cancel_event"] = cancel_event
        run = runs.create_run(
            cfg, item["task"], item["model"], item["attempt"], **kwargs)

        with _LOCK:
            item["run_id"] = run["run_id"]
            item["sandbox"] = run.get("sandbox", "")
            item["drive"] = run.get("drive", "")
            item["status"] = "ready"
            batch["updated_at"] = _now()
            _add_event(item, "工作区已就绪，等待模型操作与人工评分", "ready")
        _save_batch(cfg, batch)

        # 取消与用户启动评分之间的竞态：取消已设置时不再留下可评分的 run。
        if cancel_event.is_set():
            _request_run_cancel(cfg, run["run_id"])
            with _LOCK:
                _mark_item_cancelled(batch, item)
            _save_batch(cfg, batch)
            _release_item_sandbox(cfg, batch, run, emit)
            _clear_item_workspace(cfg, batch, item)
            released = True
            return

        # 无人值守作答：跑批建好沙箱后直接把第 1 级提示词交给模型。
        # 只发不收——校验仍然由人启动（§3 的口径）。
        if batch.get("auto_send"):
            _auto_send_first_prompt(cfg, run, batch, item)

        # 批量会话不代替用户触发评分；保持工作区到评分结束。
        final = None
        while True:
            current = runs.get_run(cfg, run["run_id"])
            current_status = current.get("status")
            if current_status in {"graded", "error", "cancelled"}:
                final = current
                break
            if cancel_event.is_set() and current_status != "grading":
                _request_run_cancel(cfg, run["run_id"])
                with _LOCK:
                    _mark_item_cancelled(batch, item)
                _save_batch(cfg, batch)
                _release_item_sandbox(cfg, batch, run, emit)
                _clear_item_workspace(cfg, batch, item)
                released = True
                return

            if current_status == "grading":
                with _LOCK:
                    if item["status"] != "grading":
                        item["status"] = "grading"
                        batch["updated_at"] = _now()
                        _add_event(item, "工作台已启动校验", "grading")
                        changed = True
                    else:
                        changed = False
                if changed:
                    _save_batch(cfg, batch)
            if cancel_event.is_set():
                # 评分中的 run 不强杀；取消信号已被消费，保持低频观察直到落定。
                time.sleep(0.5)
            else:
                cancel_event.wait(0.5)

        with _LOCK:
            if final.get("status") == "cancelled":
                _mark_item_cancelled(batch, item, "运行记录已取消")
            elif final.get("status") == "error":
                item["status"] = "error"
                err = final.get("last_error") or {}
                item["error"] = err.get("message") or "校验失败"
                item["finished_at"] = _now()
                batch["updated_at"] = _now()
                _add_event(item, "评分失败", "error")
            else:
                item["status"] = "graded"
                item["score"] = final.get("last_score")
                item["passed"] = bool(final.get("last_passed"))
                item["finished_at"] = _now()
                batch["updated_at"] = _now()
                _add_event(item, "评分完成", "graded")
        _save_batch(cfg, batch)
        _release_item_sandbox(cfg, batch, run, emit)
        _clear_item_workspace(cfg, batch, item)
        released = True
    except errors.HarnessError as exc:
        if exc.code == errors.E_RUN_CANCELLED or cancel_event.is_set():
            with _LOCK:
                _mark_item_cancelled(batch, item, "会话已取消")
            _save_batch(cfg, batch)
            if run:
                _release_item_sandbox(cfg, batch, run, emit)
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
        if cancel_event.is_set():
            with _LOCK:
                _mark_item_cancelled(batch, item, "会话已取消")
            _save_batch(cfg, batch)
        else:
            with _LOCK:
                item["status"] = "error"
                item["error"] = "未预期错误：%r" % exc
                item["finished_at"] = _now()
                batch["updated_at"] = _now()
                _add_event(item, "会话异常", "error")
            _save_batch(cfg, batch)
            emit("[%s×%s] 条目异常：%r" % (item["task"], item["model"], exc))
    finally:
        if run and not released and item.get("status") in {"error", "cancelled"}:
            _release_item_sandbox(cfg, batch, run, emit)
            _clear_item_workspace(cfg, batch, item)
        gate.release()


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


def _release_item_sandbox(cfg: dict, batch: dict, run: dict, emit) -> None:
    """回收这一条的沙箱工作区，把位置让给还没跑的条目。

    只回收沙箱目录；`runs/<题>/<模型>/<时间>/` 里的
    run.json / report.json / diff.patch 全部保留，记分板照常统计。
    """
    if not batch.get("auto_release"):
        return
    try:
        latest = runs.get_run(cfg, run["run_id"])
        sandbox.destroy(cfg, latest, log=lambda m: None)
        latest["sandbox"] = ""
        latest["drive"] = ""
        runs.save_run(cfg, latest)
        run.update(latest)
        emit("[%s×%s] 已回收沙箱工作区" % (latest.get("task"), latest.get("model")))
    except Exception as exc:  # noqa: BLE001 - 回收失败不该翻掉已拿到成绩
        emit("[%s×%s] 回收沙箱失败（不影响成绩）：%r"
             % (run.get("task"), run.get("model"), exc))
