"""验收：批量跑批（并发）。

覆盖三个必须成立的点：

1. **并发上限 = 配置的工作线程上限**。文件夹沙箱不再依赖盘符槽位；
   显式传更大的值要被夹回配置上限，传非法值要有合理兜底。
2. **每条独立成败**。一条失败（模型档案不存在、题目不存在）不能带崩整批，
   错误记在该条上，其余照跑。
3. **跑完自动回收**。跑批动辄十几条，不回收工作区会持续占用磁盘与运行记录槽位。

真正的"并发跑起来"依赖工作区准备与评分，放在集成层面的手工验收里；
这里用桩把 runs/sandbox 换掉，保证纯逻辑可测、不碰真实工作区。
"""

from __future__ import annotations

import os
import time
import threading

import pytest

from harness import batch, errors, packs, runs as runs_mod, sandbox, util
from conftest import BACKEND_TASK, make_run


# --------------------------------------------------------------------------
# 并发上限
# --------------------------------------------------------------------------

def test_auto_send_posts_the_level_one_prompt(cfg, monkeypatch):
    """跑批勾了自动发送：沙箱就绪后把第 1 级提示词交给模型，校验仍归人。"""
    from conftest import BACKEND_TASK
    sent = []
    monkeypatch.setattr(batch.chat, "start_send",
                        lambda c, run, text: sent.append(text) or {"accepted": True})
    monkeypatch.setattr(batch, "_save_batch", lambda c, b: None)
    item = {"task": BACKEND_TASK, "model": "m", "events": []}

    batch._auto_send_first_prompt(cfg, {"task": BACKEND_TASK, "run_id": "R1"}, {"updated_at": ""}, item)

    assert len(sent) == 1 and sent[0].strip()
    assert any("自动发送" in e["message"] for e in item["events"])


def test_auto_send_failure_keeps_the_workspace_usable(cfg, monkeypatch):
    """自动发送失败不毁掉这一轮：只记事件，用户仍可在工作台手动发送。"""
    from conftest import BACKEND_TASK

    def boom(_c, _run, _text):
        raise errors.HarnessError(errors.E_CHAT_FAILED, "接口不通")

    monkeypatch.setattr(batch.chat, "start_send", boom)
    monkeypatch.setattr(batch, "_save_batch", lambda c, b: None)
    item = {"task": BACKEND_TASK, "model": "m", "events": []}

    batch._auto_send_first_prompt(cfg, {"task": BACKEND_TASK, "run_id": "R1"}, {"updated_at": ""}, item)

    assert [e["kind"] for e in item["events"]] == ["error"]
    assert "手动发送" in item["events"][0]["message"]


def test_batch_get_reconciles_items_after_a_restart(cfg):
    """服务重启带走监控线程后，读批次要按 run 的真实状态补齐。

    两段事实都要对：
    1. run 已出分 → 条目是「等你结束本轮」（槽位仍然占着，队列不往下派）；
    2. 用户点了「结束本轮」→ 记录被整条删掉、成绩进台账 → 条目按台账落定。
    旧实现只认第 1 步并把 graded 当终点，于是第 2 轮的满分永远刷不进这一行。
    """
    import json

    from conftest import make_run
    from harness import results as ledger, runs, util

    run = make_run(cfg, model="对账模型")
    run["status"] = "graded"
    run["last_score"] = 66.7
    run["last_passed"] = False
    run["attempts_allowed"] = 2
    run["rounds"] = [{"attempt": 1, "score": 66.7, "passed": False,
                      "graded_at": "2026-01-01T00:10:00"}]
    runs.save_run(cfg, run)

    doc = {
        "batch_id": "batch-recon", "created_at": "x", "updated_at": "x", "status": "running",
        "concurrency": 1, "problems": [], "auto_release": True,
        "items": [{"index": 0, "task": run["task"], "model": run["model"], "attempt": 1,
                   "status": "ready", "run_id": run["run_id"], "sandbox": "sandboxes/在用",
                   "events": []}],
    }
    path = os.path.join(batch._batch_dir(cfg, "batch-recon"), "batch.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.write_json_atomic(path, doc)

    out = batch.get(cfg, "batch-recon")
    assert out["items"][0]["status"] == "awaiting_finish"
    assert out["items"][0]["score"] == 66.7
    assert out["items"][0]["best_passed"] is False
    assert out["status"] == "running", "还没结束本轮，批次不许判成已完成"
    assert out["awaiting"] == 1

    # 用户在工作台点「结束本轮」：成绩进台账、记录被真删
    entry = ledger.append_entry(cfg, ledger.make_entry(
        run["task"], run["model"], run["model"], source_run_id=run["run_id"],
        rounds=2, best_round=2, score=100.0, passed=True, pass1=False))
    os.remove(os.path.join(runs.dir_of_run_id(cfg, run["run_id"]), "run.json"))

    out2 = batch.get(cfg, "batch-recon")
    assert out2["items"][0]["status"] == "graded"
    assert out2["items"][0]["score"] == 100.0
    assert out2["items"][0]["best_passed"] is True
    assert out2["items"][0]["rounds"] == 2
    assert out2["items"][0]["entry_id"] == entry["entry_id"]
    assert out2["passed"] == 1
    assert out2["status"] == "finished"
    assert json.load(open(path, encoding="utf-8"))["items"][0]["status"] == "graded"


def test_reconcile_repairs_stale_graded_numbers_from_the_ledger(cfg):
    """老快照把第 1 轮的分数钉成最终成绩：读一次就对到台账上（T3-08 实测）。

    旧实现在 run.status=graded 那一刻就把条目判成「已完成」，于是第 2 轮 100 分
    通过、台账条目写着 score=100/passed=true，批次行还挂在「85.7 未通过」。
    台账是成绩的唯一权威，读侧必须把它校回来。
    """
    from harness import results as ledger, util

    entry = ledger.append_entry(cfg, ledger.make_entry(
        "TEST-01", "stub", "stub", source_run_id="R-stale",
        rounds=2, best_round=2, score=100.0, passed=True, pass1=False))

    doc = {
        "batch_id": "batch-stale", "created_at": "x", "updated_at": "x", "status": "finished",
        "concurrency": 1, "problems": [], "auto_release": True,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "graded", "run_id": "R-stale", "sandbox": "",
                   "score": 85.7, "passed": False, "events": []}],
    }
    path = os.path.join(batch._batch_dir(cfg, "batch-stale"), "batch.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.write_json_atomic(path, doc)

    out = batch.get(cfg, "batch-stale")
    item = out["items"][0]
    assert item["score"] == 100.0
    assert item["passed"] is True and item["best_passed"] is True
    assert item["rounds"] == 2 and item["best_round"] == 2
    assert item["entry_id"] == entry["entry_id"]
    assert any("台账" in e["message"] for e in item["events"])
    assert out["passed"] == 1


def test_reconcile_leaves_legacy_graded_rows_alone(cfg):
    """台账里没有这一条的老记录（台账之前就结束的尝试）不许被改成「没留成绩」。"""
    from harness import util

    doc = {
        "batch_id": "batch-legacy", "created_at": "x", "updated_at": "x", "status": "finished",
        "concurrency": 1, "problems": [], "auto_release": True,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "graded", "run_id": "R-legacy", "sandbox": "",
                   "score": 66.0, "passed": True, "events": []}],
    }
    path = os.path.join(batch._batch_dir(cfg, "batch-legacy"), "batch.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.write_json_atomic(path, doc)

    out = batch.get(cfg, "batch-legacy")
    assert out["items"][0]["status"] == "graded"
    assert out["items"][0]["score"] == 66.0
    assert out["items"][0]["passed"] is True


def test_batch_get_reconciles_finished_without_score(cfg):
    """记录没了、台账里也没有这一条（废弃本轮）→ 条目落定成「没留成绩」。"""
    from conftest import make_run
    from harness import runs, util

    run = make_run(cfg, model="废弃模型")
    runs.save_run(cfg, run)
    os.remove(os.path.join(runs.dir_of_run_id(cfg, run["run_id"]), "run.json"))

    doc = {
        "batch_id": "batch-discard", "created_at": "x", "updated_at": "x", "status": "running",
        "concurrency": 1, "problems": [], "auto_release": True,
        "items": [{"index": 0, "task": run["task"], "model": run["model"], "attempt": 1,
                   "status": "awaiting_finish", "run_id": run["run_id"], "sandbox": "",
                   "events": []}],
    }
    path = os.path.join(batch._batch_dir(cfg, "batch-discard"), "batch.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    util.write_json_atomic(path, doc)

    out = batch.get(cfg, "batch-discard")
    assert out["items"][0]["status"] == "discarded"
    assert out["items"][0]["ledgered"] is False
    assert out["status"] == "finished"
    assert out["passed"] == 0


def test_concurrency_defaults_to_configured_limit(cfg):
    assert batch.max_concurrency(cfg) == cfg["max_concurrency"]


def test_concurrency_is_capped_by_configured_limit(cfg):
    """请求 8 条并发，配置上限为 3 → 夹到 3。"""
    assert batch.max_concurrency(cfg, 8) == cfg["max_concurrency"]


def test_concurrency_smaller_request_is_honoured(cfg):
    assert batch.max_concurrency(cfg, 1) == 1
    assert batch.max_concurrency(cfg, 2) == 2


def test_concurrency_invalid_values_fall_back(cfg):
    """非法值不该抛异常：退回配置上限；0/负数夹到 1。"""
    assert batch.max_concurrency(cfg, "abc") == cfg["max_concurrency"]
    assert batch.max_concurrency(cfg, None) == cfg["max_concurrency"]
    assert batch.max_concurrency(cfg, 0) == 1
    assert batch.max_concurrency(cfg, -5) == 1


def test_concurrency_missing_or_invalid_config_falls_back_to_one():
    """缺少或非法的工作线程上限时也不能算出 0 并发。"""
    assert batch.max_concurrency({}) == 1
    assert batch.max_concurrency({"max_concurrency": 0}) == 1
    assert batch.max_concurrency({"max_concurrency": "bad"}) == 1


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
            {"index": 0, "task": "A", "model": "m", "status": "graded",
             "passed": True, "score": 100, "best_passed": True, "best_score": 100},
            {"index": 1, "task": "A", "model": "m", "status": "grading",
             "passed": False, "score": None, "best_passed": False, "best_score": None},
            {"index": 2, "task": "A", "model": "m", "status": "pending",
             "passed": False, "score": None, "best_passed": False, "best_score": None},
            {"index": 3, "task": "A", "model": "m", "status": "ready",
             "passed": False, "score": None, "best_passed": False, "best_score": None},
            {"index": 4, "task": "A", "model": "m", "status": "awaiting_finish",
             "passed": False, "score": 85.7, "best_passed": True, "best_score": 100},
            {"index": 5, "task": "A", "model": "m", "status": "discarded",
             "passed": False, "score": None, "best_passed": False, "best_score": None},
            {"index": 6, "task": "A", "model": "m", "status": "skipped",
             "passed": False, "score": 50, "best_passed": False, "best_score": 50},
            {"index": 7, "task": "A", "model": "m", "status": "error", "passed": False, "score": None},
            {"index": 8, "task": "A", "model": "m", "status": "cancelled", "passed": False, "score": None},
        ],
    }
    view = batch._public_batch(doc)
    assert view["total"] == 9
    assert view["done"] == 5          # graded + discarded + skipped + error + cancelled
    # 通过看的是「最好的一轮」：awaiting_finish 那条第 2 轮已经全绿，也要算通过
    assert view["passed"] == 2
    assert view["running"] == 3       # grading + ready + awaiting_finish 都占着槽位
    assert view["awaiting"] == 1
    assert view["queued"] == 1
    assert view["mode"] == "interactive"
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

    def fake_release(*a, **k):
        called["n"] += 1
        return {"released": True}

    monkeypatch.setattr(batch.runs, "release_sandbox", fake_release)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}

    assert batch._release_item_sandbox(cfg, {"auto_release": False}, run, lambda m: None) is False
    assert called["n"] == 0
    assert run["sandbox"] == "x" and run["drive"] == "Q:"


def test_release_item_sandbox_frees_the_workspace(cfg, monkeypatch):
    """auto_release=True 且门面放行：清空沙箱字段、用最新记录刷新调用方快照。"""
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, run_id: {"released": True})
    monkeypatch.setattr(batch.runs, "get_run", lambda c, run_id: {
        "run_id": run_id, "status": "ready", "sandbox": "", "drive": "",
        "task": "T", "model": "M", "last_score": 88.0,
    })

    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []
    assert batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append) is True

    assert run["sandbox"] == "" and run["drive"] == ""
    assert run["last_score"] == 88.0, "回收后要用最新落盘记录刷新，不许留陈旧快照"
    assert any("已回收" in line for line in logs)


def test_release_item_sandbox_refused_while_model_sends(cfg, monkeypatch):
    """在飞闸门拒绝回收：目录与字段都保留，事件说明原因（2026-10-02 T2-04 事故）。"""
    def busy(cfg, run_id):
        raise errors.HarnessError(
            errors.E_RUN_BUSY, "模型正在输出，等这条消息结束再回收沙箱。", run_id)

    monkeypatch.setattr(batch.runs, "release_sandbox", busy)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []

    assert batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append) is False
    assert run["sandbox"] == "x", "在飞被拒时绝不能清掉工作区字段"
    assert any("暂不回收" in line for line in logs)


def test_release_failure_does_not_raise(cfg, monkeypatch):
    """回收失败不能翻掉已经拿到的成绩。"""
    def boom(*a, **k):
        raise OSError("删不掉")

    monkeypatch.setattr(batch.runs, "release_sandbox", boom)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []
    assert batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append) is False
    assert any("回收沙箱失败" in line for line in logs)


def test_auto_release_keeps_sandbox_when_rounds_remain(cfg, monkeypatch):
    """评分落定但轮次没用完：auto_release 不回收沙箱，留给「进入第二轮」。"""
    def fail_release(cfg, run_id):
        pytest.fail("还有剩余轮次的沙箱不许被自动回收")

    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: {
        "run_id": rid, "status": "graded", "attempt": 1, "attempts_allowed": 3,
        "task": "T", "model": "M",
    })
    monkeypatch.setattr(batch.runs, "release_sandbox", fail_release)
    run = {"run_id": "r", "sandbox": "x", "drive": "Q:", "task": "T", "model": "M"}
    logs = []
    assert batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append) is False
    assert run["sandbox"] == "x"
    assert any("保留" in line for line in logs)

    # 轮次用完 → 照常回收
    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: {
        "run_id": rid, "status": "graded", "attempt": 2, "attempts_allowed": 2,
        "task": "T", "model": "M", "sandbox": "",
    })
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})
    assert batch._release_item_sandbox(cfg, {"auto_release": True}, run, logs.append) is True


def test_release_sandbox_refuses_while_send_in_flight(cfg, monkeypatch):
    """在飞闸门：发送没归零时 release_sandbox 不得删工作区，放行后才能回收。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "在飞模型",
                   run_id="TEST-07__在飞模型__20260101-000000",
                   run_dir=runs_mod.dir_of_run_id(cfg, "TEST-07__在飞模型__20260101-000000"))
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=lambda m: None)
    try:
        runs_mod.save_run(cfg, run)
        monkeypatch.setattr(runs_mod.chat, "send_active", lambda run_id: True)
        with pytest.raises(errors.HarnessError) as excinfo:
            runs_mod.release_sandbox(cfg, run["run_id"])
        assert "模型正在输出" in excinfo.value.message
        assert os.path.isdir(run["sandbox"]), "在飞时工作区必须原封不动"

        monkeypatch.setattr(runs_mod.chat, "send_active", lambda run_id: False)
        result = runs_mod.release_sandbox(cfg, run["run_id"])
        assert result["released"] is True
        assert not os.path.isdir(run["sandbox"])
    finally:
        sandbox.destroy(cfg, run, log=lambda m: None)


def test_remove_item_dequeues_pending_and_skips_awaiting_finish(cfg, monkeypatch):
    """排队中的可以移出；已出分等结束本轮的可以跳过（让出槽位）；开工中的拒绝。"""
    from harness import runs

    graded_run = make_run(cfg, model="已出分")
    graded_run["status"] = "graded"
    graded_run["last_score"] = 70.0
    graded_run["last_passed"] = False
    graded_run["attempts_allowed"] = 2
    runs.save_run(cfg, graded_run)

    live_run = make_run(cfg, model="作答中")
    live_run["status"] = "ready"
    runs.save_run(cfg, live_run)

    doc = {
        "batch_id": "b-remove", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [
            {"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
             "status": "pending", "events": []},
            {"index": 1, "task": live_run["task"], "model": live_run["model"], "attempt": 1,
             "status": "ready", "run_id": live_run["run_id"], "events": []},
            {"index": 2, "task": graded_run["task"], "model": graded_run["model"], "attempt": 1,
             "status": "awaiting_finish", "run_id": graded_run["run_id"], "events": []},
        ],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    try:
        res = batch.remove_item(cfg, "b-remove", 0)
        assert res["removed"] is True
        assert doc["items"][0]["status"] == "cancelled"
        assert any("移除" in e["message"] for e in doc["items"][0]["events"])

        # 已出分等结束本轮：跳过只让出槽位，不动运行记录与成绩
        res2 = batch.remove_item(cfg, "b-remove", 2)
        assert res2["removed"] is True and res2["status"] == "skipped"
        item2 = doc["items"][2]
        assert item2["status"] == "skipped"
        assert item2["run_id"] == graded_run["run_id"], "跳过不许碰运行记录"
        assert item2["score"] == 70.0
        assert any("结束本轮" in e["message"] for e in item2["events"])

        with pytest.raises(errors.HarnessError) as excinfo:
            batch.remove_item(cfg, "b-remove", 1)
        assert "排队" in excinfo.value.message
        assert doc["items"][1]["status"] == "ready", "已开工的条目不许被移除波及"
        assert doc["items"][1]["run_id"] == live_run["run_id"]

        with pytest.raises(errors.HarnessError):
            batch.remove_item(cfg, "b-remove", 9)
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)


def test_run_item_never_starts_a_removed_item(cfg, monkeypatch):
    """派发与移除之间的竞态护栏：已被移除的条目就算派发了也原样退出，不建沙箱。"""
    doc = {
        "batch_id": "b-race", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "cancelled", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    monkeypatch.setattr(
        batch.runs, "create_run",
        lambda *a, **k: pytest.fail("被移除的条目绝不能再进入准备"))
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    gate = threading.Semaphore(0)
    worker = threading.Thread(target=batch._run_item,
                              args=(cfg, doc, doc["items"][0], gate, lambda message: None))
    worker.start()
    worker.join(2)
    try:
        assert not worker.is_alive()
        assert doc["items"][0]["status"] == "cancelled"
        assert gate.acquire(blocking=False), "护栏退出也必须释放并发闸门"
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)


# --------------------------------------------------------------------------
# 端到端：用桩把「准备 + 校验」换掉，验证并发闸门与逐条成败
# --------------------------------------------------------------------------

def _stub_model(cfg, monkeypatch, model_id: str) -> None:
    """把模型档案挂进配置（避免真的写 config.json）。"""
    accepted = set(model_id if isinstance(model_id, (list, tuple, set)) else [model_id])
    monkeypatch.setattr(
        batch.config, "find_model",
        lambda c, mid: {"id": mid, "protocol": "openai",
                        # 空 base_url 现在会被 chat._base_url 直接拒绝（以前静默兜底到
                        # api.openai.com），所以桩必须给一个合法地址
                        "base_url": "http://model.invalid/v1", "model": mid}
        if mid in accepted else (_ for _ in ()).throw(
            errors.HarnessError(errors.E_MODEL_NOT_FOUND, "找不到模型档案 %s。" % mid)),
    )


def test_batch_waits_for_finish_before_next_item(cfg, monkeypatch):
    """出分不等于收工：队列里的下一条必须等工作台「结束本轮」才开工。

    2026-10-02 用户口径：跑批的节拍是「一条一条收尾」，不是「一条一条出分」。
    旧实现在 run.status=graded 那一刻就回收槽位并派发下一条，用户还在读第 1 轮
    结果，下一题已经开跑了。
    """
    _stub_model(cfg, monkeypatch, ["model-a", "model-b"])
    cfg["max_concurrency"] = 2

    # 覆盖两个真实题的 meta 读取：给一个不会失败的最小 meta
    monkeypatch.setattr(batch.packs, "load_meta", lambda c, t: {
        "id": t, "title": "桩题 %s" % t, "tier": "easy", "attempts": 3, "pack_dir": ".",
    })
    monkeypatch.setattr(batch.packs, "load_prompts", lambda meta: [
        {"level": 1, "text": "公开提示词第一轮"}, {"level": 2, "text": "第二轮提示词"},
    ])

    counter = {"n": 0, "active": 0, "max_active": 0}
    lock = threading.Lock()
    run_state = {}
    closed = set()

    def fake_create_run(c, task, model, attempt=1, claim_queued=True, wait_s=0.0, log=None):
        with lock:
            counter["n"] += 1
            idx = counter["n"]
            counter["active"] += 1
            counter["max_active"] = max(counter["max_active"], counter["active"])
        time.sleep(0.05)                      # 模拟铺沙箱的耗时
        run_id = "r%d" % idx
        run_state[run_id] = {
            "run_id": run_id, "task": task, "model": model, "status": "ready",
            "sandbox": "sandbox-%d" % idx, "drive": "", "attempt": attempt,
            "rounds": [], "last_score": None, "last_passed": None,
        }
        with lock:
            counter["active"] -= 1
        return dict(run_state[run_id])

    def fake_get_run(c, rid):
        if rid in closed:
            raise errors.HarnessError(errors.E_RUN_NOT_FOUND, "没有这条运行记录。", rid)
        return dict(run_state[rid])

    monkeypatch.setattr(batch.runs, "create_run", fake_create_run)
    monkeypatch.setattr(batch.runs, "get_run", fake_get_run)
    monkeypatch.setattr(batch.runs, "start_grade", lambda *_: pytest.fail("批次不能自动评分"))
    monkeypatch.setattr(batch.runs, "save_run", lambda c, r: None)
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})

    def close_run(run_id, score, passed):
        """在工作台点「结束本轮」：成绩进台账、记录被真删。"""
        from harness import results as ledger
        entry = ledger.append_entry(cfg, ledger.make_entry(
            "TEST-01", "stub", "stub", source_run_id=run_id,
            rounds=1, best_round=1, score=score, passed=passed, pass1=passed))
        closed.add(run_id)
        return entry

    items = [
        {"task": "TEST-01", "model": "model-a"},
        {"task": "TEST-01", "model": "model-b"},
        {"task": "TEST-02", "model": "model-a"},
    ]
    view = batch.start(cfg, items, concurrency=2)
    batch_id = view["batch_id"]

    def wait_until(predicate):
        deadline = time.time() + 10
        while time.time() < deadline:
            doc = batch.get(cfg, batch_id)
            if predicate(doc):
                return doc
            time.sleep(0.05)
        pytest.fail("等待批次状态超时：%r" % batch.get(cfg, batch_id))

    ready = wait_until(lambda doc: sum(i["status"] == "ready" for i in doc["items"]) == 2)
    assert [i["model"] for i in ready["items"] if i["status"] == "ready"] == ["model-a", "model-b"]
    assert ready["items"][0]["task"] == ready["items"][1]["task"] == "TEST-01"
    assert ready["items"][0]["prompt"] == "公开提示词第一轮"
    assert ready["items"][2]["status"] == "pending"
    assert counter["max_active"] == 2
    assert ready["mode"] == "interactive"

    # 第 1 轮出分（未全绿）：条目停在「等你结束本轮」，队列一动不动
    first_run_id = ready["items"][0]["run_id"]
    run_state[first_run_id].update({
        "status": "graded", "last_score": 85.7, "last_passed": False,
        "rounds": [{"attempt": 1, "score": 85.7, "passed": False}],
    })
    awaiting = wait_until(lambda doc: doc["items"][0]["status"] == batch.AWAITING_STATUS)
    assert awaiting["items"][2]["status"] == "pending", "出分就把下一条派出去 = 旧缺陷"
    assert awaiting["items"][0]["score"] == 85.7
    assert awaiting["items"][0]["best_passed"] is False
    assert awaiting["awaiting"] == 1
    assert counter["n"] == 2, "第 3 条不许在结束本轮之前开始准备"

    # 用户点「结束本轮」：记录收走、成绩进台账 → 槽位释放，第 3 条才开工
    close_run(first_run_id, 85.7, False)
    nxt = wait_until(lambda doc: doc["items"][2]["status"] == "ready")
    assert nxt["items"][0]["status"] == "graded"
    assert nxt["items"][0]["score"] == 85.7
    assert counter["n"] == 3

    # 其余两条各自结束本轮 → 整批完成
    for item in nxt["items"]:
        if item["status"] == "ready":
            close_run(item["run_id"], 100.0, True)
    final = wait_until(lambda doc: doc.get("status") == "finished")
    assert final["done"] == 3 and final["total"] == 3
    assert all(i["status"] == "graded" for i in final["items"])
    assert final["passed"] == 2, "TEST-01×model-a 第 1 轮未过；另两条满分"
    assert final["concurrency"] <= cfg["max_concurrency"]


def test_batch_item_follows_later_rounds_then_ledger(cfg, monkeypatch):
    """T3-08 的教训：第 1 轮 85.7、第 2 轮满分，批次行必须跟着走并以台账落定。

    旧实现一看到 graded 就把条目判成「已完成」并钉死第 1 轮的分数，
    于是用户第 2 轮做对、结束本轮写出 100 分的台账条目，批次页还挂着「85.7 未通过」。
    """
    from harness import results as ledger

    run_id = "r-rounds"
    state = {
        "run_id": run_id, "task": "TEST-01", "model": "stub", "status": "ready",
        "sandbox": "sandbox", "drive": "", "attempt": 1, "attempts_allowed": 2,
        "rounds": [], "last_score": None, "last_passed": None,
    }
    closed = set()

    def fake_get_run(c, rid):
        if rid in closed:
            raise errors.HarnessError(errors.E_RUN_NOT_FOUND, "没有这条运行记录。", rid)
        return dict(state)

    monkeypatch.setattr(batch.runs, "create_run", lambda *a, **k: dict(state))
    monkeypatch.setattr(batch.runs, "get_run", fake_get_run)
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)

    doc = {
        "batch_id": "b-rounds", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "pending", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    item = doc["items"][0]
    gate = threading.Semaphore(0)
    worker = threading.Thread(target=batch._run_item,
                              args=(cfg, doc, item, gate, lambda message: None))
    worker.start()
    try:
        def wait_for(predicate, label, timeout=6):
            deadline = time.time() + timeout
            while time.time() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            pytest.fail("等待超时：%s（现状 %r）" % (label, item))

        wait_for(lambda: item["status"] == "ready", "工作区就绪")

        # 第 1 轮出分未全绿：槽位不许让出去
        state.update({"status": "graded", "last_score": 85.7, "last_passed": False,
                      "rounds": [{"attempt": 1, "score": 85.7, "passed": False}]})
        wait_for(lambda: item["status"] == batch.AWAITING_STATUS, "第 1 轮等你结束本轮")
        assert item["best_score"] == 85.7 and item["best_passed"] is False
        assert gate.acquire(blocking=False) is False, "出分不是终点，槽位必须留着"

        # 进入第 2 轮并做对：同一行要跟着刷成满分
        state.update({"status": "ready", "attempt": 2})
        wait_for(lambda: item["round"] == 2, "进入第 2 轮")
        state.update({
            "status": "graded", "last_score": 100.0, "last_passed": True,
            "rounds": [{"attempt": 1, "score": 85.7, "passed": False},
                       {"attempt": 2, "score": 100.0, "passed": True}],
        })
        wait_for(lambda: item["best_score"] == 100.0, "第 2 轮满分")
        assert item["status"] == batch.AWAITING_STATUS
        assert item["passed"] is True and item["best_passed"] is True
        assert item["rounds"] == 2 and item["best_round"] == 2

        # 用户点「结束本轮」：记录被收走、成绩进台账 → 条目按台账落定
        entry = ledger.append_entry(cfg, ledger.make_entry(
            "TEST-01", "stub", "stub", source_run_id=run_id,
            rounds=2, best_round=2, score=100.0, passed=True, pass1=False))
        closed.add(run_id)
        worker.join(4)
        assert not worker.is_alive()
        assert item["status"] == "graded"
        assert item["score"] == 100.0 and item["passed"] is True
        assert item["rounds"] == 2 and item["best_round"] == 2
        assert item["entry_id"] == entry["entry_id"]
        assert item["ledgered"] is True
        assert any("台账" in e["message"] for e in item["events"])
        assert gate.acquire(blocking=False) is True, "落定后闸门必须释放"
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)


def test_batch_survives_single_item_failure(cfg, monkeypatch):
    """单条准备失败 → 该条 error，其余会话仍可准备并由用户收尾。"""
    _stub_model(cfg, monkeypatch, "stub")
    monkeypatch.setattr(batch.packs, "load_meta", lambda c, t: {
        "id": t, "title": "桩题", "tier": "easy", "attempts": 3, "pack_dir": ".",
    })

    seen = {"n": 0}
    run_state = {}
    closed = set()

    def flaky_create_run(c, task, model, attempt=1, claim_queued=True, wait_s=0.0, log=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise errors.HarnessError(errors.E_DRIVE_UNAVAILABLE, "盘符池已用尽")
        run_id = "ok%d" % seen["n"]
        run_state[run_id] = {
            "run_id": run_id, "task": task, "model": model, "status": "ready",
            "sandbox": "sandbox", "drive": "", "attempt": 1,
            "rounds": [], "last_score": None, "last_passed": None,
        }
        return dict(run_state[run_id])

    def fake_get_run(c, rid):
        if rid in closed:
            raise errors.HarnessError(errors.E_RUN_NOT_FOUND, "没有这条运行记录。", rid)
        return dict(run_state[rid])

    monkeypatch.setattr(batch.runs, "create_run", flaky_create_run)
    monkeypatch.setattr(batch.runs, "start_grade", lambda *_: pytest.fail("批次不能自动评分"))
    monkeypatch.setattr(batch.runs, "get_run", fake_get_run)
    monkeypatch.setattr(batch.runs, "save_run", lambda c, r: None)
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})

    view = batch.start(cfg, [
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-01", "model": "stub"},
        {"task": "TEST-01", "model": "stub"},
    ], concurrency=1)
    batch_id = view["batch_id"]

    deadline = time.time() + 10
    final = None
    while time.time() < deadline:
        doc = batch.get(cfg, batch_id)
        for item in doc["items"]:
            if item["status"] == "ready":
                run_state[item["run_id"]].update({"status": "graded", "last_score": 10.0,
                                                  "last_passed": False})
            if item["status"] == batch.AWAITING_STATUS:
                # 用户直接结束本轮（没有可计入台账的成绩 → discarded）
                closed.add(item["run_id"])
        if doc.get("status") in {"finished", "cancelled"}:
            final = doc
            break
        time.sleep(0.05)

    assert final is not None
    statuses = [i["status"] for i in final["items"]]
    assert statuses.count("error") == 1
    assert statuses.count("discarded") == 2
    errored = [i for i in final["items"] if i["status"] == "error"][0]
    assert "盘符池已用尽" in errored["error"]


def test_cancel_marks_batch_cancelling(cfg):
    """取消发出事件、标记排队条目，并先进入 cancelling。"""
    doc = {
        "batch_id": "b-cancel", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "T", "model": "M", "status": "pending", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(),
    }
    with batch._LOCK:
        batch._BATCHES["b-cancel"] = doc
    try:
        res = batch.cancel(cfg, "b-cancel")
        assert res["status"] == "cancelling"
        assert doc["cancel"] is True
        assert doc["_cancel_event"].is_set()
        assert doc["items"][0]["status"] == "cancelled"
    finally:
        with batch._LOCK:
            batch._BATCHES.pop("b-cancel", None)


def test_cancel_spares_ready_session_until_it_settles(cfg, monkeypatch):
    """停止不打扰已开工的条目：ready 的会话原地保留，出分后让出槽位（2026-10-02 语义）。"""
    run_state = {"run_id": "r-cancel", "task": "TEST-01", "model": "stub",
                 "status": "ready", "sandbox": "sandbox", "drive": ""}
    released = []
    monkeypatch.setattr(batch.runs, "create_run", lambda *a, **k: dict(run_state))
    monkeypatch.setattr(batch.runs, "get_run", lambda *a, **k: dict(run_state))
    monkeypatch.setattr(batch.runs, "save_run", lambda c, run: run_state.update(run))
    monkeypatch.setattr(batch.runs, "release_sandbox",
                        lambda c, rid: released.append(rid) or {"released": True})
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    doc = {
        "batch_id": "b-cancel-ready", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "pending", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    gate = threading.Semaphore(0)
    worker = threading.Thread(target=batch._run_item,
                              args=(cfg, doc, doc["items"][0], gate, lambda message: None))
    worker.start()
    deadline = time.time() + 2
    while time.time() < deadline and doc["items"][0]["status"] != "ready":
        time.sleep(0.01)
    assert doc["items"][0]["status"] == "ready"
    assert batch.cancel(cfg, doc["batch_id"])["status"] == "cancelling"

    # 停止之后：条目不被判死、run 不被改写、沙箱不回收
    time.sleep(0.3)
    assert doc["items"][0]["status"] == "ready"
    assert run_state.get("status") == "ready"
    assert "cancel_requested" not in run_state
    assert not released

    # 用户在工作台正常评分 → 批次已停止，不再等「结束本轮」：让出槽位并回收工作区
    run_state.update({"status": "graded", "last_score": 90.0, "last_passed": True})
    worker.join(2)
    assert not worker.is_alive()
    item = doc["items"][0]
    assert item["status"] == "skipped"
    assert item["score"] == 90.0 and item["passed"] is True
    assert released == ["r-cancel"]
    assert not item["sandbox"]
    assert gate.acquire(blocking=False)
    with batch._LOCK:
        batch._BATCHES.pop(doc["batch_id"], None)


def test_cancel_during_preparation_lets_it_finish(cfg, monkeypatch):
    """准备中的条目不再被停止杀掉：沙箱建完、条目就绪，之后照常走完生命周期。"""
    started = threading.Event()
    hold = threading.Event()

    def slow_create_run(c, task, model, attempt=1, **kwargs):
        started.set()
        hold.wait(2)                      # 让 cancel 落在准备窗口内
        return {"run_id": "r-prep", "task": task, "model": model, "status": "ready",
                "sandbox": "sandbox", "drive": ""}

    run_state = {"r-prep": {"run_id": "r-prep", "task": "TEST-01", "model": "stub",
                            "status": "ready", "sandbox": "sandbox", "drive": ""}}
    released = []
    monkeypatch.setattr(batch.runs, "create_run", slow_create_run)
    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: dict(run_state[rid]))
    monkeypatch.setattr(batch.runs, "release_sandbox",
                        lambda c, rid: released.append(rid) or {"released": True})
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    doc = {
        "batch_id": "b-cancel-preparing", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "pending", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    gate = threading.Semaphore(0)
    worker = threading.Thread(target=batch._run_item,
                              args=(cfg, doc, doc["items"][0], gate, lambda message: None))
    worker.start()
    assert started.wait(2)
    assert batch.cancel(cfg, doc["batch_id"])["status"] == "cancelling"
    hold.set()
    deadline = time.time() + 2
    while time.time() < deadline and doc["items"][0]["status"] != "ready":
        time.sleep(0.01)
    assert doc["items"][0]["status"] == "ready", "准备中的条目不许被停止判死"
    assert "cancel_requested" not in run_state["r-prep"]
    assert not released

    # 用户在工作台正常评分 → 批次已停止，出分即让出槽位，闸门释放
    run_state["r-prep"].update({"status": "graded", "last_score": 50.0, "last_passed": False})
    worker.join(3)
    assert not worker.is_alive()
    try:
        assert doc["items"][0]["status"] == "skipped"
        assert doc["items"][0]["score"] == 50.0
        assert released == ["r-prep"]
        assert gate.acquire(blocking=False)
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)


def test_cancel_while_gate_waiting_does_not_block_batch(cfg, monkeypatch):
    """闸门被占用时取消，排队条目应立即结束，批次线程不能卡死。"""
    run_state = {}
    counter = {"n": 0}

    def fake_create_run(c, task, model, attempt=1, **kwargs):
        counter["n"] += 1
        run_id = "r-gate-%d" % counter["n"]
        run_state[run_id] = {
            "run_id": run_id, "task": task, "model": model, "status": "ready",
            "sandbox": "sandbox-%d" % counter["n"], "drive": "",
        }
        return dict(run_state[run_id])

    monkeypatch.setattr(batch.runs, "create_run", fake_create_run)
    monkeypatch.setattr(batch.runs, "get_run", lambda c, rid: dict(run_state[rid]))
    monkeypatch.setattr(batch.runs, "save_run", lambda c, run: run_state[run["run_id"]].update(run))
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    doc = {
        "batch_id": "b-cancel-gate", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [
            {"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
             "status": "pending", "events": []},
            {"index": 1, "task": "TEST-01", "model": "stub", "attempt": 1,
             "status": "pending", "events": []},
        ],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    scheduler = threading.Thread(target=batch._run_batch,
                                 args=(cfg, doc["batch_id"], lambda message: None))
    scheduler.start()
    deadline = time.time() + 2
    while time.time() < deadline and doc["items"][0]["status"] != "ready":
        time.sleep(0.01)
    assert doc["items"][0]["status"] == "ready"
    assert doc["items"][1]["status"] == "pending"
    assert batch.cancel(cfg, doc["batch_id"])["status"] == "cancelling"
    # 已开工的条目继续活着；还没轮到的条目被拦下
    assert doc["items"][1]["status"] == "cancelled"
    run_state["r-gate-1"].update({"status": "graded", "last_score": 66.0, "last_passed": True})
    scheduler.join(3)
    try:
        assert not scheduler.is_alive()
        assert doc["status"] == "cancelled"
        # 停止后不再等「结束本轮」：出分即跳过让位（成绩以工作台为准）
        assert [item["status"] for item in doc["items"]] == ["skipped", "cancelled"]
        assert doc["items"][0]["score"] == 66.0
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)


def test_cancel_during_grading_keeps_result_but_cancels_batch(cfg, monkeypatch):
    """取消不强杀已开始的评分；评分结果保留在条目上，批次终态为 cancelled。"""
    run_state = {
        "run_id": "r-grading", "task": "TEST-01", "model": "stub", "status": "ready",
        "sandbox": "sandbox", "drive": "",
    }

    monkeypatch.setattr(batch.runs, "create_run", lambda *a, **k: dict(run_state))
    monkeypatch.setattr(batch.runs, "get_run", lambda *a, **k: dict(run_state))
    monkeypatch.setattr(batch.runs, "save_run", lambda c, run: run_state.update(run))
    monkeypatch.setattr(batch.runs, "release_sandbox", lambda c, rid: {"released": True})
    monkeypatch.setattr(batch, "_save_batch", lambda *a, **k: None)
    doc = {
        "batch_id": "b-cancel-grading", "created_at": "t", "updated_at": "t",
        "status": "running", "concurrency": 1,
        "items": [{"index": 0, "task": "TEST-01", "model": "stub", "attempt": 1,
                   "status": "pending", "events": []}],
        "problems": [], "cancel": False, "_cancel_event": threading.Event(), "auto_release": True,
    }
    with batch._LOCK:
        batch._BATCHES[doc["batch_id"]] = doc
    scheduler = threading.Thread(target=batch._run_batch,
                                 args=(cfg, doc["batch_id"], lambda message: None))
    scheduler.start()
    deadline = time.time() + 2
    while time.time() < deadline and doc["items"][0]["status"] != "ready":
        time.sleep(0.01)
    assert doc["items"][0]["status"] == "ready"
    run_state["status"] = "grading"
    deadline = time.time() + 2
    while time.time() < deadline and doc["items"][0]["status"] != "grading":
        time.sleep(0.01)
    assert doc["items"][0]["status"] == "grading"
    assert batch.cancel(cfg, doc["batch_id"])["status"] == "cancelling"
    assert "cancel_requested" not in run_state, "停止不许再往 run 上写取消标志"
    run_state.update({"status": "graded", "last_score": 77.0, "last_passed": True})
    scheduler.join(3)
    try:
        assert not scheduler.is_alive()
        assert doc["status"] == "cancelled"
        assert doc["items"][0]["status"] == "skipped"
        assert doc["items"][0]["score"] == 77.0
        assert doc["items"][0]["passed"] is True
    finally:
        with batch._LOCK:
            batch._BATCHES.pop(doc["batch_id"], None)
