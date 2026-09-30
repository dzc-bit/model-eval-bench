"""批量跑批：一次把「多道题 × 多个模型」排进后台并发执行（并发版评测台）。

设计要点（与设计文档的盘符池硬约束对齐）：

1. **盘符池就是并发闸门**。每个沙箱要独占一个盘符（`Q:`/`R:`/`S:`），
   所以同时在跑的沙箱数天然不能超过盘符数。默认并发 = `len(drive_pool)`，
   也可以显式调小；调大没有意义（会卡在盘符分配上）。
2. **准备沙箱本身是同步阻塞的**（`runs.create_run` 内部铺树 + 注入 + git init），
   放到线程池里跑，HTTP 请求立刻返回批次 id，前端轮询进度——与单轮校验的
   异步语义保持一致（`POST /api/runs/{id}/grade` 也是异步的）。
3. **每个 item 独立成败**。一道题失败（题包坏了、模型档案没了）不能拖垮整批，
   记到该 item 的 `error` 里，批次继续。
4. **不改动 runs/ 的落盘格式**。批量跑批产出的仍是普通 run 记录，
   记分板、任务库历史、报告页不用改一行就能看到它们。

线程模型：一个「批次线程」负责调度，每个 item 再交给一个工作线程；
批次线程只做派发与状态汇总，不持有任何长事务锁（避免与盘符锁互相等待）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Dict, List, Optional

from . import config, errors, packs, runs, sandbox, util

#: 同时保留的批次（内存态）上限，防止长时间运行堆爆
MAX_BATCHES = 40
#: 单个批次的条目上限（题目 × 模型 的笛卡尔积）
MAX_ITEMS = 100
#: 单个条目的整体超时：准备 + 校验（秒）。超时记 error，不拖住整批。
ITEM_TIMEOUT_S = 1800

_LOCK = threading.RLock()
#: batch_id → 批次状态（内存态；落盘只在 batch 目录留一份快照）
_BATCHES: Dict[str, dict] = {}


# --------------------------------------------------------------------------
# 并发上限
# --------------------------------------------------------------------------

def max_concurrency(cfg: dict, requested: Optional[int] = None) -> int:
    """算出实际并发数：不超过盘符池大小，也不小于 1。"""
    pool = len(cfg.get("drive_pool") or []) or 1
    if requested is None:
        return pool
    try:
        want = int(requested)
    except (TypeError, ValueError):
        return pool
    if want < 1:
        return 1
    # 盘符不够时硬件上跑不动更多，夹到池子大小
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
        util.write_json_atomic(path, _public_batch(batch))
    except OSError:
        # 落盘失败不影响批次继续跑（内存态才是权威）
        pass


def _public_batch(batch: dict) -> dict:
    """给前端看的批次视图（去掉线程句柄之类的不可序列化字段）。"""
    return {
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "updated_at": batch["updated_at"],
        "status": batch["status"],
        "concurrency": batch["concurrency"],
        "total": len(batch["items"]),
        "done": sum(1 for i in batch["items"] if i["status"] in {"graded", "error"}),
        "passed": sum(1 for i in batch["items"] if i["status"] == "graded" and i.get("passed")),
        "running": sum(1 for i in batch["items"] if i["status"] in {"preparing", "grading"}),
        "items": batch["items"],
    }


def start(cfg: dict, items: List[dict], concurrency: Optional[int] = None,
          auto_release: bool = True, log=None) -> dict:
    """建一个批次并立刻在后台开跑。

    :param items: `[{"task": "T1-01", "model": "gpt-x", "attempt": 1}, ...]`
    :param concurrency: 想同时跑几个；默认 = 盘符池大小
    :param auto_release: 每条跑完后是否立刻回收它的沙箱与盘符。

        **默认开**。盘符池只有 Q/R/S 三个，而跑批动辄十几条；如果每条都把
        沙箱留着占盘符，跑到第 4 条就会 `E_DRIVE_UNAVAILABLE`（实测正是如此）。
        跑批的用途是"批量拿分"，报告已经落盘（`runs/<题>/<模型>/<时间>/`），
        沙箱本体没有保留价值，回收掉才能让后面的条目排上队。
        想要留着沙箱继续手动改代码的场景，走单轮流程（不经过批次）即可。
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
            config.find_model(cfg, model)
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
            "status": "pending",
            "run_id": "",
            "score": None,
            "passed": False,
            "error": "",
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
        "concurrency": concurrency,
        "items": prepared,
        "problems": problems,
        "cancel": False,
        "auto_release": bool(auto_release),
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
        "已开始跑批：并发 %d（盘符池上限）。准备沙箱是同步耗时的，"
        "进度请轮询本接口。" % concurrency
    )
    return payload


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
        return doc
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
                out[doc["batch_id"]] = doc
    with _LOCK:
        for batch_id, batch in _BATCHES.items():
            payload = _public_batch(batch)
            payload["problems"] = batch.get("problems") or []
            out[batch_id] = payload
    items = sorted(out.values(), key=lambda b: str(b.get("created_at") or ""), reverse=True)
    return {"batches": items, "count": len(items)}


def cancel(cfg: dict, batch_id: str) -> dict:
    """请求取消：已开跑的条目跑完当前这一步，未开始的不再派发。"""
    with _LOCK:
        batch = _BATCHES.get(batch_id)
        if not batch:
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "这个批次不在运行中（可能已经结束）。",
                batch_id,
            )
        batch["cancel"] = True
        batch["status"] = "cancelling"
        batch["updated_at"] = _now()
    _save_batch(cfg, batch)
    return {"batch_id": batch_id, "status": "cancelling"}


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
    """批次线程体：按并发上限派发条目，等全部结束再收尾。"""
    emit = log or (lambda m: None)
    with _LOCK:
        batch = _BATCHES.get(batch_id)
    if not batch:
        return

    items = batch["items"]
    limit = max(1, int(batch["concurrency"]))
    # 用信号量把"同时在跑的条分数"卡在盘符池大小上
    gate = threading.Semaphore(limit)
    threads: List[threading.Thread] = []

    for item in items:
        if batch.get("cancel"):
            with _LOCK:
                item["status"] = "cancelled"
            continue
        gate.acquire()
        worker = threading.Thread(
            target=_run_item, args=(cfg, batch, item, gate, emit),
            name="batch-item-%d" % item["index"], daemon=True)
        threads.append(worker)
        worker.start()

    for worker in threads:
        worker.join()

    with _LOCK:
        batch["status"] = "cancelled" if batch.get("cancel") else "finished"
        batch["updated_at"] = _now()
    _save_batch(cfg, batch)
    summary = _public_batch(batch)
    emit("批次 %s 结束：%d/%d 完成，%d 通过" % (
        batch_id, summary["done"], summary["total"], summary["passed"]))


def _run_item(cfg: dict, batch: dict, item: dict, gate: threading.Semaphore, emit) -> None:
    """单个条目：准备沙箱 → 校验 → 等结果。失败只记在本条上。"""
    try:
        with _LOCK:
            item["status"] = "preparing"
            item["started_at"] = _now()
            batch["updated_at"] = _now()
        _save_batch(cfg, batch)

        # 准备沙箱（同步阻塞；盘符分配由 sandbox._DRIVE_LOCK 兜底串行）
        # wait_s 给一个容忍窗口：上一条的盘符回收与本条的分配之间有毫秒级间隙，
        # 不等就会把本该成功的条目判成"盘符池用尽"（实测 4 条里挂 1 条）。
        run = runs.create_run(
            cfg, item["task"], item["model"], item["attempt"],
            claim_queued=False, wait_s=30.0,
            log=lambda m: emit("[%s×%s] %s" % (item["task"], item["model"], m)),
        )
        with _LOCK:
            item["run_id"] = run["run_id"]
            item["status"] = "grading"
            batch["updated_at"] = _now()
        _save_batch(cfg, batch)

        # 触发校验（异步），然后轮询直到落定
        runs.start_grade(cfg, run["run_id"])
        deadline = time.time() + ITEM_TIMEOUT_S
        final = None
        while time.time() < deadline:
            time.sleep(1.0)
            current = runs.get_run(cfg, run["run_id"])
            if current.get("status") in {"graded", "error"}:
                final = current
                break
        if final is None:
            raise errors.HarnessError(
                errors.E_GRADE_TIMEOUT,
                "这一条超过 %d 秒还没结束，已放弃等待。" % ITEM_TIMEOUT_S,
                run["run_id"],
            )

        with _LOCK:
            if final.get("status") == "error":
                item["status"] = "error"
                err = final.get("last_error") or {}
                item["error"] = err.get("message") or "校验失败"
            else:
                item["status"] = "graded"
                item["score"] = final.get("last_score")
                item["passed"] = bool(final.get("last_passed"))
            item["finished_at"] = _now()
            batch["updated_at"] = _now()
        _save_batch(cfg, batch)
        _release_item_sandbox(cfg, batch, run, emit)
    except errors.HarnessError as exc:
        with _LOCK:
            item["status"] = "error"
            item["error"] = exc.message
            item["finished_at"] = _now()
            batch["updated_at"] = _now()
        _save_batch(cfg, batch)
        emit("[%s×%s] 条目失败：%s" % (item["task"], item["model"], exc.message))
    except Exception as exc:  # noqa: BLE001 - 单条失败绝不能带崩整批
        with _LOCK:
            item["status"] = "error"
            item["error"] = "未预期错误：%r" % exc
            item["finished_at"] = _now()
            batch["updated_at"] = _now()
        _save_batch(cfg, batch)
        emit("[%s×%s] 条目异常：%r" % (item["task"], item["model"], exc))
    finally:
        gate.release()


def _release_item_sandbox(cfg: dict, batch: dict, run: dict, emit) -> None:
    """回收这一条的沙箱与盘符，把位置让给还没跑的条目。

    只回收盘符映射与沙箱目录；`runs/<题>/<模型>/<时间>/` 里的
    run.json / report.json / diff.patch 全部保留，记分板照常统计。
    """
    if not batch.get("auto_release"):
        return
    try:
        sandbox.destroy(cfg, run, log=lambda m: None)
        run["sandbox"] = ""
        run["drive"] = ""
        runs.save_run(cfg, run)
        emit("[%s×%s] 已回收沙箱与盘符" % (run.get("task"), run.get("model")))
    except Exception as exc:  # noqa: BLE001 - 回收失败不该翻掉已拿到成绩
        emit("[%s×%s] 回收沙箱失败（不影响成绩）：%r"
             % (run.get("task"), run.get("model"), exc))
