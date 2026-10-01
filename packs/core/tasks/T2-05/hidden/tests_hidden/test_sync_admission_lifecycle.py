"""T2-05 隐藏测试：同步任务准入、心跳与失活回收全生命周期验证。

断言不变量：
1. 准入原子性：同一签名在途任务在并发下只能起一个 worker，重复提交返回 admission="reused"；
2. 心跳与防失活：进度推进与查询均续期心跳，无活动僵尸任务按失活超时回收；
3. 终态回收：终态记录过期后其签名与取消标记一并清理，失活回收后名额即时释放；
4. 消费方出口：409 响应必须能查出占用名额的任务编号（字段名不约束），GET /sync/jobs/{id} 必须保留 admission 快照。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import date
from http import HTTPStatus
from threading import Event
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from astock_backtester.data.sync import SyncCapacityError, SyncJobManager
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.models import SyncJobStatus
from astock_backtester.service import create_server

_OPENER = build_opener(ProxyHandler({}))
_LOOPBACK_TIMEOUT_S = 10


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def _request_json_allow_error(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _make_status(job_id: str, status: str = "running", admission: str = "started") -> SyncJobStatus:
    return SyncJobStatus(
        job_id=job_id,
        mode="full_market_bootstrap",
        status=status,
        admission=admission,
        total_symbols=1,
        start_date=date(2015, 1, 1),
        end_date=date(2015, 1, 5),
    )



# ==============================================================================
# 组 1：admission_exit（权重 1）
# ==============================================================================

def test_serial_identical_submissions_reuse_in_flight_job(tmp_path):
    """串行重复提交：同签名在途任务直接复用并返回 reused。"""
    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    first = _make_status("job-serial-1", "running")
    admitted1, is_new1 = manager._admit(first, sig)
    assert is_new1 is True
    assert admitted1.admission == "started"

    second = _make_status("job-serial-2", "running")
    admitted2, is_new2 = manager._admit(second, sig)
    assert is_new2 is False
    assert admitted2.admission == "reused"
    assert admitted2.job_id == admitted1.job_id
    assert len(manager._jobs) == 1


def test_concurrent_submissions_admit_single_worker(tmp_path):
    """并发重复提交：两线程同时提交相同签名任务，必须原子判定只起一个。"""
    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig = "full_market_bootstrap|2015-01-01|2015-01-05|000001"

    entered_put = Event()
    orig_put = manager._put_locked

    def slow_put(status):
        entered_put.set()
        time.sleep(0.3)
        return orig_put(status)

    manager._put_locked = slow_put

    thread_result = {}

    def worker():
        first = _make_status("job-concurrent-1", "running")
        admitted, is_new = manager._admit(first, sig)
        thread_result["job"] = admitted
        thread_result["is_new"] = is_new

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    assert entered_put.wait(timeout=2.0), "慢写入未被触发"

    second = _make_status("job-concurrent-2", "running")
    admitted2, is_new2 = manager._admit(second, sig)

    t.join(timeout=3.0)
    assert not t.is_alive()

    assert len(manager._jobs) == 1
    assert is_new2 is False
    assert admitted2.admission == "reused"
    assert thread_result["job"].admission == "started"


# ==============================================================================
# 组 2：heartbeat_exit（权重 1）
# ==============================================================================

def test_progress_writeback_prolongs_job_lifecycle(tmp_path, monkeypatch):
    """进度写回续命：进度推进应刷新心跳，防止被判为失活。"""
    import astock_backtester.data.sync as sync_module
    monkeypatch.setattr(sync_module, "RUNNING_JOB_STALE_SECONDS", 0.1)

    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig_a = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    job_a, _ = manager._admit(_make_status("job-a", "running"), sig_a)

    time.sleep(0.07)
    manager._put_locked(job_a.model_copy(deep=True))
    time.sleep(0.07)

    # 距准入已过 0.14s (> 0.1s)，但距 _put_locked 仅过 0.07s (< 0.1s)
    sig_b = "full_market_bootstrap|2015-01-01|2015-01-05|000002"
    manager._admit(_make_status("job-b", "running"), sig_b)

    current_a = manager._jobs.get("job-a")
    assert current_a is not None
    assert current_a.status == "running"


def test_recent_read_protects_job_from_stale_reap(tmp_path, monkeypatch):
    """近期被读也算活着：调用方轮询读取的任务受到回收保护。"""
    import astock_backtester.data.sync as sync_module
    monkeypatch.setattr(sync_module, "RUNNING_JOB_STALE_SECONDS", 0.1)

    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig_a = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    job_a, _ = manager._admit(_make_status("job-a", "running"), sig_a)

    time.sleep(0.06)
    read_a = manager.get_job("job-a")
    assert read_a is not None and read_a.status == "running"
    time.sleep(0.06)

    # 距准入已过 0.12s (> 0.1s)，但距读取仅过 0.06s (< 0.1s)
    sig_b = "full_market_bootstrap|2015-01-01|2015-01-05|000002"
    manager._admit(_make_status("job-b", "running"), sig_b)

    current_a = manager.get_job("job-a")
    assert current_a is not None
    assert current_a.status == "running"


def test_unattended_inactive_job_is_reaped_as_failed(tmp_path, monkeypatch):
    """无人推进无人读的僵尸被回收：超时无心跳的任务被标为 failed。"""
    import astock_backtester.data.sync as sync_module
    monkeypatch.setattr(sync_module, "RUNNING_JOB_STALE_SECONDS", 0.1)

    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig_a = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    manager._admit(_make_status("job-a", "running"), sig_a)

    time.sleep(0.14)

    sig_b = "full_market_bootstrap|2015-01-01|2015-01-05|000002"
    manager._admit(_make_status("job-b", "running"), sig_b)

    current_a = manager._jobs.get("job-a")
    assert current_a is not None
    assert current_a.status == "failed"
    assert "失活" in (current_a.last_error or "")


# ==============================================================================
# 组 3：reclaim_exit（权重 1）
# ==============================================================================

def test_terminal_job_prunes_signatures_and_cancellation_markers(tmp_path, monkeypatch):
    """终态记录与标记同生共死：过期清理时其去重签名与取消标记必须一并清空。"""
    import astock_backtester.data.sync as sync_module
    monkeypatch.setattr(sync_module, "TERMINAL_JOB_RETENTION_SECONDS", 0.0)
    monkeypatch.setattr(sync_module, "JOB_READ_STALE_SECONDS", 0.0)

    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig_a = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    job_a, _ = manager._admit(_make_status("job-a", "running"), sig_a)

    completed_a = job_a.model_copy(deep=True, update={"status": "completed"})
    manager._store(completed_a)
    manager._cancelled.add("job-a")

    manager._prune_locked(time.monotonic())

    assert manager._signatures == {}
    assert manager._cancelled == set()


def test_reaped_stale_job_releases_capacity_for_new_submission(tmp_path, monkeypatch):
    """失活回收释放预算：reap 后同签名任务再次提交应顺利起新任务。"""
    import astock_backtester.data.sync as sync_module
    monkeypatch.setattr(sync_module, "RUNNING_JOB_STALE_SECONDS", 0.1)

    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=1)
    sig_a = "full_market_bootstrap|2015-01-01|2015-01-05|000001"
    manager._admit(_make_status("job-a", "running"), sig_a)

    time.sleep(0.14)
    manager._reap_stale_running_locked(time.monotonic())
    assert manager._jobs["job-a"].status == "failed"

    new_job, is_new = manager._admit(_make_status("job-a-retry", "running"), sig_a)
    assert is_new is True
    assert new_job.admission == "started"
    assert new_job.job_id == "job-a-retry"


# ==============================================================================
# 组 4：consumer_exit（权重 1）
# ==============================================================================

def test_http_capacity_conflict_reports_running_jobs(tmp_path):
    """409 响应必须带 running_jobs：超出并发预算时如实暴露在途任务列表。"""
    class StubSyncManager:
        def __init__(self):
            self.calls = 0

        def start_full_market(self, symbols, start_date, end_date):
            self.calls += 1
            if self.calls == 3:
                raise SyncCapacityError(["job-1"], 2)
            admission = "started" if self.calls == 1 else "reused"
            return _make_status("job-1", "running", admission=admission)

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = StubSyncManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/sync/full-market"
        payload = {"symbols": ["000001"], "start_date": "2015-01-01", "end_date": "2015-01-05"}

        _request_json("POST", url, payload)
        _request_json("POST", url, payload)

        status, body = _request_json_allow_error("POST", url, payload)
        assert status == HTTPStatus.CONFLICT
        assert body["code"] == "sync_capacity"
        # 契约只要求「占用名额的任务编号可查」，不规定字段名：断言 body["running_jobs"]
        # 等于把锚解自己起的名字当成题目要求，模型换个字段名或写进文案就判错。
        # 注入态的异常文案只有「已有 N 个在跑（上限 M）」，不含编号，判别力不丢。
        assert "job-1" in json.dumps(body, ensure_ascii=False), (
            "409 必须暴露当前占用名额的任务编号（字段名与形态自定，写进提示文案也算）：%s" % body)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_sync_job_query_exposes_admission_status(tmp_path):
    """GET /sync/jobs/{id} 必须暴露 admission：内部复用准入标记对前端可见。"""
    class StubSyncManager:
        def get_job(self, job_id: str):
            if job_id == "job-reused":
                return _make_status("job-reused", "running", admission="reused")
            return None

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = StubSyncManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/sync/jobs/job-reused"
        data = _request_json("GET", url)
        assert "job" in data
        assert data["job"].get("admission") == "reused"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ==============================================================================
# 组 5：coherence（权重 2）
# ==============================================================================

def test_full_lifecycle_coherence_across_concurrency_and_cleanup(tmp_path, monkeypatch):
    """全周期链：串行复用 → 并发竞态只起一个 → 完成后清理干净 → 同签名再进起全新任务。"""
    import astock_backtester.data.sync as sync_module
    manager = SyncJobManager(warehouse=Warehouse(tmp_path), provider=object(), max_concurrent_jobs=2)
    sig = "full_market_bootstrap|2015-01-01|2015-01-05|000001"

    # 1. 串行复用
    first = _make_status("chain-1", "running")
    admitted1, is_new1 = manager._admit(first, sig)
    assert is_new1 is True
    assert admitted1.admission == "started"

    second = _make_status("chain-2", "running")
    admitted2, is_new2 = manager._admit(second, sig)
    assert is_new2 is False
    assert admitted2.admission == "reused"
    assert len(manager._jobs) == 1

    # 2. 并发竞态
    entered_put = Event()
    orig_put = manager._put_locked

    def slow_put(status):
        entered_put.set()
        time.sleep(0.3)
        return orig_put(status)

    manager._put_locked = slow_put
    sig2 = "full_market_bootstrap|2015-01-01|2015-01-05|000002"

    worker_result = {}
    def worker():
        job_w = _make_status("chain-conc-1", "running")
        admitted_w, is_new_w = manager._admit(job_w, sig2)
        worker_result["job"] = admitted_w
        worker_result["is_new"] = is_new_w

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert entered_put.wait(timeout=2.0)

    job_main = _make_status("chain-conc-2", "running")
    admitted_main, is_new_main = manager._admit(job_main, sig2)
    t.join(timeout=3.0)

    manager._put_locked = orig_put

    assert is_new_main is False
    assert admitted_main.admission == "reused"
    conc_jobs = [j for j in manager._jobs.values() if j.job_id in ("chain-conc-1", "chain-conc-2")]
    assert len(conc_jobs) == 1

    # 3. 终态清理
    monkeypatch.setattr(sync_module, "TERMINAL_JOB_RETENTION_SECONDS", 0.0)
    monkeypatch.setattr(sync_module, "JOB_READ_STALE_SECONDS", 0.0)

    for jid in list(manager._jobs.keys()):
        cur = manager._jobs[jid]
        manager._store(cur.model_copy(deep=True, update={"status": "completed"}))
        manager._cancelled.add(jid)

    manager._prune_locked(time.monotonic())
    assert manager._signatures == {}
    assert manager._cancelled == set()
    assert len(manager._jobs) == 0

    # 4. 同签名再进来是全新 started
    final_job = _make_status("chain-final", "running")
    admitted_final, is_new_final = manager._admit(final_job, sig)
    assert is_new_final is True
    assert admitted_final.admission == "started"
