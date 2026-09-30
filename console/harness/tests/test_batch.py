"""验收：批量跑批（并发）。

覆盖三个必须成立的点：

1. **并发上限 = 盘符池大小**。盘符就是沙箱槽位，跑得比盘符多没有意义；
   显式传更大的值要被夹回池子大小，传非法值要有合理兜底。
2. **每条独立成败**。一条失败（模型档案不存在、题目不存在）不能带崩整批，
   错误记在该条上，其余照跑。
3. **跑完自动回收**。跑批动辄十几条而盘符只有三个，不回收的话第 4 条就会
   `E_DRIVE_UNAVAILABLE`（这条是实测踩出来的：4 条挂 1 条）。

真正的"并发跑起来"依赖 subst 盘符，放在集成层面的手工验收里；
这里用桩把 runs/sandbox 换掉，保证纯逻辑可测、不碰真实盘符。
"""

from __future__ import annotations

import time

import pytest

from harness import batch, errors


# --------------------------------------------------------------------------
# 并发上限
# --------------------------------------------------------------------------

def test_concurrency_defaults_to_drive_pool(cfg):
    assert batch.max_concurrency(cfg) == len(cfg["drive_pool"])


def test_concurrency_is_capped_by_drive_pool(cfg):
    """请求 8 条并发，池子只有 3 个盘符 → 夹到 3。"""
    assert batch.max_concurrency(cfg, 8) == len(cfg["drive_pool"])


def test_concurrency_smaller_request_is_honoured(cfg):
    assert batch.max_concurrency(cfg, 1) == 1
    assert batch.max_concurrency(cfg, 2) == 2


def test_concurrency_invalid_values_fall_back(cfg):
    """非法值不该抛异常：退回池子大小；0/负数夹到 1。"""
    assert batch.max_concurrency(cfg, "abc") == len(cfg["drive_pool"])
    assert batch.max_concurrency(cfg, None) == len(cfg["drive_pool"])
    assert batch.max_concurrency(cfg, 0) == 1
    assert batch.max_concurrency(cfg, -5) == 1


def test_concurrency_single_drive_pool():
    """盘符池只有一个（或被配置成空）时也不能算出 0 并发。"""
    assert batch.max_concurrency({"drive_pool": ["Q:"]}) == 1
    assert batch.max_concurrency({"drive_pool": []}) == 1


# --------------------------------------------------------------------------
# 入参校验（坏条目当场报错，不浪费一轮调度）
# --------------------------------------------------------------------------

def test_start_rejects_empty_items(cfg):
    with pytest.raises(errors.HarnessError) as excinfo:
        batch.start(cfg, [])
    assert "至少要有一个条目" in excinfo.value.message


def test_start_rejects_too_many_items(cfg):
    items = [{"task": "T1-01", "model": "m"} for _ in range(batch.MAX_ITEMS + 1)]
    with pytest.raises(errors.HarnessError) as excinfo:
        batch.start(cfg, items)
    assert "最多" in excinfo.value.message


def test_start_reports_problems_for_unknown_model(cfg):
    """模型档案不存在时整批都不可执行 → 报错并带上原因。"""
    with pytest.raises(errors.HarnessError) as excinfo:
        batch.start(cfg, [{"task": "TEST-01", "model": "不存在的模型"}])
    assert "没有可执行的条目" in excinfo.value.message


def test_start_reports_missing_task(cfg):
    with pytest.raises(errors.HarnessError) as excinfo:
        batch.start(cfg, [{"task": "没有这道题", "model": "任意"}])
    assert "没有可执行的条目" in excinfo.value.message


def test_start_rejects_attempt_beyond_limit(cfg, monkeypatch):
    """第 3 轮的初级题（只给 1 次机会）要被挡下。"""
    _stub_model(cfg, monkeypatch, "stub-model")
    with pytest.raises(errors.HarnessError) as excinfo:
        batch.start(cfg, [{"task": "TEST-01", "model": "stub-model", "attempt": 9}])
    assert "没有可执行的条目" in excinfo.value.message


# --------------------------------------------------------------------------
# 批次视图（不真跑，只验证数据结构）
# --------------------------------------------------------------------------

def test_public_batch_shape():
    doc = {
        "batch_id": "b1", "created_at": "t0", "updated_at": "t1", "status": "running",
        "concurrency": 3,
        "items": [
            {"index": 0, "task": "A", "model": "m", "status": "graded", "passed": True, "score": 100},
            {"index": 1, "task": "A", "model": "m", "status": "grading", "passed": False, "score": None},
            {"index": 2, "task": "A", "model": "m", "status": "pending", "passed": False, "score": None},
            {"index": 3, "task": "A", "model": "m", "status": "error", "passed": False, "score": None},
        ],
    }
    view = batch._public_batch(doc)
    assert view["total"] == 4
    assert view["done"] == 2          # graded + error
    assert view["passed"] == 1
    assert view["running"] == 1       # grading 计入进行中
    assert "batch_id" in view and "items" in view


def test_get_unknown_batch_raises(cfg):
    with pytest.raises(errors.HarnessError):
        batch.get(cfg, "batch-不存在")


# --------------------------------------------------------------------------
# 自动回收（本轮实测暴露的缺陷）
# --------------------------------------------------------------------------

def test_release_item_sandbox_respects_auto_release(cfg, monkeypatch):
    """auto_release=False 时绝不能动沙箱（用户可能要留着改代码）。"""
    called = {"n": 0}

    def fake_destroy(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(batch.sandbox, "destroy", fake_destroy)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}

    batch._release_item_sandbox(cfg, {"auto_release": False}, run, lambda m: None)
    assert called["n"] == 0
    assert run["sandbox"] == "x" and run["drive"] == "Q:"


def test_release_item_sandbox_frees_drive(cfg, monkeypatch):
    """auto_release=True 时要释放盘符、清空沙箱字段，并落盘。"""
    monkeypatch.setattr(batch.sandbox, "destroy",
                        lambda c, r, log=None: (r.update({"sandbox": "", "drive": ""}), None)[1])
    saved = []
    monkeypatch.setattr(batch.runs, "save_run", lambda c, r: saved.append(dict(r)))

    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []
    batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append)

    assert run["sandbox"] == "" and run["drive"] == ""
    assert saved and saved[0]["drive"] == ""
    assert any("回收" in line for line in logs)


def test_release_failure_does_not_raise(cfg, monkeypatch):
    """回收失败不能翻掉已经拿到的成绩。"""
    def boom(*a, **k):
        raise OSError("删不掉")

    monkeypatch.setattr(batch.sandbox, "destroy", boom)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []
    batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append)   # 不抛
    assert any("回收沙箱失败" in line for line in logs)


# --------------------------------------------------------------------------
# 端到端：用桩把「准备 + 校验」换掉，验证并发闸门与逐条成败
# --------------------------------------------------------------------------

def _stub_model(cfg, monkeypatch, model_id: str) -> None:
    """把模型档案挂进配置（避免真的写 config.json）。"""
    monkeypatch.setattr(
        batch.config, "find_model",
        lambda c, mid: {"id": mid, "protocol": "openai", "base_url": "", "model": mid}
        if mid == model_id else (_ for _ in ()).throw(
            errors.HarnessError(errors.E_MODEL_NOT_FOUND, "找不到模型档案 %s。" % mid)),
    )


def test_batch_runs_all_items_and_records_scores(cfg, monkeypatch):
    """用桩跑一整批：4 条全部完成，得分正确落到每条上。"""
    _stub_model(cfg, monkeypatch, "stub")

    # 覆盖两个真实题的 meta 读取：给一个不会失败的最小 meta
    monkeypatch.setattr(batch.packs, "load_meta", lambda c, t: {
        "id": t, "title": "桩题 %s" % t, "tier": "easy", "attempts": 3, "pack_dir": ".",
    })

    counter = {"n": 0}
    lock = __import__("threading").Lock()

    def fake_create_run(c, task, model, attempt=1, claim_queued=True, wait_s=0.0, log=None):
        with lock:
            counter["n"] += 1
            idx = counter["n"]
        time.sleep(0.05)                      # 模拟铺沙箱的耗时
        return {"run_id": "r%d" % idx, "task": task, "model": model, "status": "ready"}

    monkeypatch.setattr(batch.runs, "create_run", fake_create_run)
    monkeypatch.setattr(batch.runs, "start_grade", lambda c, rid: {"status": "grading"})
    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: {
        "run_id": rid, "status": "graded", "last_score": 42.0, "last_passed": False,
    })
    monkeypatch.setattr(batch.runs, "save_run", lambda c, r: None)
    monkeypatch.setattr(batch.sandbox, "destroy", lambda c, r, log=None: None)
    monkeypatch.setattr(batch, "ITEM_TIMEOUT_S", 30)

    items = [
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-02", "model": "stub"},
        {"task": "TEST-02", "model": "stub"},
    ]
    view = batch.start(cfg, items, concurrency=2)
    batch_id = view["batch_id"]

    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        doc = batch.get(cfg, batch_id)
        if doc.get("status") in {"finished", "cancelled"}:
            final = doc
            break
        time.sleep(0.1)

    assert final is not None, "批次没有在预期时间内结束"
    assert final["status"] == "finished"
    assert final["done"] == 4 and final["total"] == 4
    assert all(i["status"] == "graded" for i in final["items"])
    assert all(i["score"] == 42.0 for i in final["items"])
    # 并发被夹到盘符池大小以内
    assert final["concurrency"] <= len(cfg["drive_pool"])


def test_batch_survives_single_item_failure(cfg, monkeypatch):
    """单条准备失败 → 该条 error，其余照常完成（整批不崩）。"""
    _stub_model(cfg, monkeypatch, "stub")
    monkeypatch.setattr(batch.packs, "load_meta", lambda c, t: {
        "id": t, "title": "桩题", "tier": "easy", "attempts": 3, "pack_dir": ".",
    })

    seen = {"n": 0}

    def flaky_create_run(c, task, model, attempt=1, claim_queued=True, wait_s=0.0, log=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise errors.HarnessError(errors.E_DRIVE_UNAVAILABLE, "盘符池已用尽")
        return {"run_id": "ok%d" % seen["n"], "task": task, "model": model, "status": "ready"}

    monkeypatch.setattr(batch.runs, "create_run", flaky_create_run)
    monkeypatch.setattr(batch.runs, "start_grade", lambda c, rid: {"status": "grading"})
    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: {
        "run_id": rid, "status": "graded", "last_score": 10.0, "last_passed": False,
    })
    monkeypatch.setattr(batch.runs, "save_run", lambda c, r: None)
    monkeypatch.setattr(batch.sandbox, "destroy", lambda c, r, log=None: None)

    view = batch.start(cfg, [
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-01", "model": "stub"},
    ], concurrency=1)
    batch_id = view["batch_id"]

    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        doc = batch.get(cfg, batch_id)
        if doc.get("status") in {"finished", "cancelled"}:
            final = doc
            break
        time.sleep(0.1)

    assert final is not None
    statuses = [i["status"] for i in final["items"]]
    assert statuses.count("error") == 1
    assert statuses.count("graded") == 2
    errored = [i for i in final["items"] if i["status"] == "error"][0]
    assert "盘符池已用尽" in errored["error"]


def test_cancel_marks_batch_cancelling(cfg):
    """取消把批次标成 cancelling（未开始的条目不再派发）。"""
    doc = {
        "batch_id": "b-cancel", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1, "items": [], "problems": [], "cancel": False,
    }
    with batch._LOCK:
        batch._BATCHES["b-cancel"] = doc
    try:
        res = batch.cancel(cfg, "b-cancel")
        assert res["status"] == "cancelling"
        assert doc["cancel"] is True
    finally:
        with batch._LOCK:
            batch._BATCHES.pop("b-cancel", None)
