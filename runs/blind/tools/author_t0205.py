"""T2-05 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

产出（写进 packs/core/tasks/T2-05/）：
  inject/patches/0001-sync-admission-lifecycle.patch   sync.py 四处注入（TOCTOU/心跳/丢标记/双重回收）
  inject/patches/0002-service-sync-api.patch           service.py 两处注入（GET剥admission/409剥running_jobs）
  reference/fix.patch                                  锚解：六处全修齐 + 原生错误心跳收口
  reference/partial.patch                              半成品：只修 sync.py 的 ①+②
  hidden/tests_hidden/test_sync_admission_lifecycle.py pytest 隐藏测试（5组出口+coherence）
  hidden/groups.json
  p2p.json                                             既有用例白名单（基线树收集，注入变红进 visible.prune）
  prompts/1.md 2.md 3.md、calibration/results.json、meta.json、reference/notes.md
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T2-05"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

SYNC_REL = "backend/astock_backtester/data/sync.py"
SERVICE_REL = "backend/astock_backtester/service.py"

sync_src = (REPO / SYNC_REL).read_text(encoding="utf-8")
service_src = (REPO / SERVICE_REL).read_text(encoding="utf-8")

# --------------------------------------------------------------------------
# 一、注入变体（共 6 处：sync.py × 4 + service.py × 2）
# --------------------------------------------------------------------------

# ① sync.py: _admit 拆两步（TOCTOU）
ORIG_ADMIT = '''    def _admit(self, status: SyncJobStatus, signature: str) -> tuple[SyncJobStatus, bool]:
        """同签名在途任务直接复用，否则在预算内新建。

        判定与写入必须在同一把锁里完成：两个请求各自"查不到相同任务"再各自建
        job 就是这个竞态的结果。返回的快照带 ``admission`` 告诉本次调用方发生
        了什么，存储里的记录保持原样。
        """
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            for job_id, existing_signature in self._signatures.items():
                if existing_signature != signature:
                    continue
                existing = self._jobs.get(job_id)
                if existing is None or existing.status not in ("running", "cancelling"):
                    continue
                self._last_read[job_id] = now
                return existing.model_copy(deep=True, update={"admission": "reused"}), False
            running = self.running_job_ids_locked()
            if len(running) >= max(1, self.max_concurrent_jobs):
                raise SyncCapacityError(running, self.max_concurrent_jobs)
            self._put_locked(status.model_copy(deep=True))
            self._signatures[status.job_id] = signature
            self._last_read[status.job_id] = now
            return status.model_copy(deep=True, update={"admission": "started"}), True'''

INJECT_ADMIT = '''    def _admit(self, status: SyncJobStatus, signature: str) -> tuple[SyncJobStatus, bool]:
        """同签名在途任务直接复用，否则在预算内新建。

        先做一次无锁快照查询，命中在途任务就不必再抢锁；未命中才进锁写入。
        返回的快照带 ``admission`` 告诉本次调用方发生了什么，存储里的记录保持原样。
        """
        for job_id, existing_signature in list(self._signatures.items()):
            if existing_signature != signature:
                continue
            existing = self._jobs.get(job_id)
            if existing is None or existing.status not in ("running", "cancelling"):
                continue
            with self._lock:
                self._last_read[job_id] = time.monotonic()
            return existing.model_copy(deep=True, update={"admission": "reused"}), False

        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            running = self.running_job_ids_locked()
            if len(running) >= max(1, self.max_concurrent_jobs):
                raise SyncCapacityError(running, self.max_concurrent_jobs)
            self._put_locked(status.model_copy(deep=True))
            self._signatures[status.job_id] = signature
            self._last_read[status.job_id] = now
            return status.model_copy(deep=True, update={"admission": "started"}), True'''

# ② sync.py: _put_locked 心跳缺失
ORIG_PUT = '''    def _put_locked(self, status: SyncJobStatus) -> None:
        self._jobs[status.job_id] = status
        if status.status in ("running", "cancelling"):
            self._finished_at.pop(status.job_id, None)
            # 每次写回都算一次进度推进：作业还在动就不会被判失活。
            self._last_progress_at[status.job_id] = time.monotonic()
        else:
            self._finished_at.setdefault(status.job_id, time.monotonic())
            self._last_progress_at.pop(status.job_id, None)'''

INJECT_PUT = '''    def _put_locked(self, status: SyncJobStatus) -> None:
        self._jobs[status.job_id] = status
        if status.status in ("running", "cancelling"):
            self._finished_at.pop(status.job_id, None)
            # 失活判定看的是"有没有人在推进"，写回本身不代表推进，这里不再盖进度戳。
        else:
            self._finished_at.setdefault(status.job_id, time.monotonic())
            self._last_progress_at.pop(status.job_id, None)'''

# ③ sync.py: _drop_locked 丢标记
ORIG_DROP = '''    def _drop_locked(self, job_id: str, *, now: float) -> None:
        if now - self._last_read.get(job_id, 0.0) < JOB_READ_STALE_SECONDS:
            return
        self._jobs.pop(job_id, None)
        self._signatures.pop(job_id, None)
        self._finished_at.pop(job_id, None)
        self._last_read.pop(job_id, None)
        self._cancelled.discard(job_id)'''

INJECT_DROP = '''    def _drop_locked(self, job_id: str, *, now: float) -> None:
        if now - self._last_read.get(job_id, 0.0) < JOB_READ_STALE_SECONDS:
            return
        self._jobs.pop(job_id, None)
        # 签名与取消标记不随记录清理：同签名任务之后仍可能再进来，留着它们可以让下一次同签名提交直接命中。
        self._finished_at.pop(job_id, None)
        self._last_read.pop(job_id, None)'''

# ④ sync.py: get_job 触发回收
ORIG_GET_JOB = '''    def get_job(self, job_id: str) -> SyncJobStatus | None:
        with self._lock:
            status = self._jobs.get(job_id)
            if status is None:
                return None
            # 读一次就等于"还在被用"：回收不能把正在轮询的任务抽走。
            self._last_read[job_id] = time.monotonic()
            return status.model_copy(deep=True)'''

INJECT_GET_JOB = '''    def get_job(self, job_id: str) -> SyncJobStatus | None:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            status = self._jobs.get(job_id)
            if status is None:
                return None
            # 读一次就等于"还在被用"：回收不能把正在轮询的任务抽走。
            self._last_read[job_id] = now
            return status.model_copy(deep=True)'''

# ⑤ service.py: GET /sync/jobs 剥离 admission
ORIG_GET_SYNC_JOB = '''        if self.path.startswith("/sync/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = self.server.state.sync_manager.get_job(job_id)
            if job is None:
                self._send_json({"code": "not_found", "message": job_id}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"job": job.model_dump(mode="json")})
            return'''

INJECT_GET_SYNC_JOB = '''        if self.path.startswith("/sync/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = self.server.state.sync_manager.get_job(job_id)
            if job is None:
                self._send_json({"code": "not_found", "message": job_id}, HTTPStatus.NOT_FOUND)
                return
            payload = job.model_dump(mode="json")
            # 复用准入只是内部去重的实现细节，对外状态保持纯净。
            payload.pop("admission", None)
            self._send_json({"job": payload})
            return'''

# ⑥ service.py: 409 响应剥 running_jobs
ORIG_409 = '''        except SyncCapacityError as exc:
            # 准入冲突必须是独立稳定码：它要告诉调用方"已有任务在跑"，
            # 而不是被归进 request_failed 让前端只能显示"请求失败"。
            self.server.state.log("warning", str(exc))
            self._send_json(
                {"code": "sync_capacity", "message": str(exc), "running_jobs": exc.running},
                HTTPStatus.CONFLICT,
            )'''

INJECT_409 = '''        except SyncCapacityError as exc:
            # 准入冲突直接返回冲突状态码与错误信息。
            self.server.state.log("warning", str(exc))
            self._send_json(
                {"code": "sync_capacity", "message": str(exc)},
                HTTPStatus.CONFLICT,
            )'''

sync_injected = sync_src
sync_injected = sync_injected.replace(ORIG_ADMIT, INJECT_ADMIT, 1)
sync_injected = sync_injected.replace(ORIG_PUT, INJECT_PUT, 1)
sync_injected = sync_injected.replace(ORIG_DROP, INJECT_DROP, 1)
sync_injected = sync_injected.replace(ORIG_GET_JOB, INJECT_GET_JOB, 1)
assert sync_injected != sync_src, "sync.py 注入失败"

service_injected = service_src
service_injected = service_injected.replace(ORIG_GET_SYNC_JOB, INJECT_GET_SYNC_JOB, 1)
service_injected = service_injected.replace(ORIG_409, INJECT_409, 1)
assert service_injected != service_src, "service.py 注入失败"

# --------------------------------------------------------------------------
# 二、参考解（锚解 fix.patch 与 半成品 partial.patch）
# --------------------------------------------------------------------------

# 锚解：六处全修齐恢复原语义 + 原生 _append_error/_append_failure 顺手修齐（改走 _put_locked）
ORIG_APPEND_ERROR = '''    def _append_error(self, job_id: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            status.errors.append(message)
            status.last_error = message
            status.recent_failures = [*status.recent_failures, {"message": message}][-20:]
            self._jobs[job_id] = status'''

FIX_APPEND_ERROR = '''    def _append_error(self, job_id: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            status.errors.append(message)
            status.last_error = message
            status.recent_failures = [*status.recent_failures, {"message": message}][-20:]
            self._put_locked(status)'''

ORIG_APPEND_FAILURE = '''    def _append_failure(self, job_id: str, symbol: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            error = f"{symbol}: {message}"
            status.errors.append(error)
            status.last_error = error
            status.recent_failures = [
                *status.recent_failures,
                {"symbol": symbol, "reason": message},
            ][-20:]
            self._jobs[job_id] = status'''

FIX_APPEND_FAILURE = '''    def _append_failure(self, job_id: str, symbol: str, message: str) -> None:
        with self._lock:
            status = self._jobs[job_id]
            error = f"{symbol}: {message}"
            status.errors.append(error)
            status.last_error = error
            status.recent_failures = [
                *status.recent_failures,
                {"symbol": symbol, "reason": message},
            ][-20:]
            self._put_locked(status)'''

sync_fixed = sync_src
sync_fixed = sync_fixed.replace(ORIG_APPEND_ERROR, FIX_APPEND_ERROR, 1)
sync_fixed = sync_fixed.replace(ORIG_APPEND_FAILURE, FIX_APPEND_FAILURE, 1)

service_fixed = service_src

# 半成品：只修 sync.py 的 ①+②（后端准入原子性与心跳写回），其余注入点不动
sync_partial = sync_injected.replace(INJECT_ADMIT, ORIG_ADMIT, 1)
sync_partial = sync_partial.replace(INJECT_PUT, ORIG_PUT, 1)

# --------------------------------------------------------------------------
# 三、补丁产出
# --------------------------------------------------------------------------

INJECT_DIR = TASK / "inject" / "patches"
REFERENCE_DIR = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

patch_sync_inject = build_patch(SYNC_REL, sync_src.splitlines(keepends=True), sync_injected.splitlines(keepends=True))
patch_service_inject = build_patch(SERVICE_REL, service_src.splitlines(keepends=True), service_injected.splitlines(keepends=True))
(INJECT_DIR / "0001-sync-admission-lifecycle.patch").write_text(patch_sync_inject, encoding="utf-8")
(INJECT_DIR / "0002-service-sync-api.patch").write_text(patch_service_inject, encoding="utf-8")

fix_parts = [
    build_patch(SYNC_REL, sync_injected.splitlines(keepends=True), sync_fixed.splitlines(keepends=True)),
    build_patch(SERVICE_REL, service_injected.splitlines(keepends=True), service_fixed.splitlines(keepends=True)),
]
(REFERENCE_DIR / "fix.patch").write_text("".join(fix_parts), encoding="utf-8")

partial_parts = [
    build_patch(SYNC_REL, sync_injected.splitlines(keepends=True), sync_partial.splitlines(keepends=True)),
]
(REFERENCE_DIR / "partial.patch").write_text("".join(partial_parts), encoding="utf-8")
print("补丁文件生成完毕")

# --------------------------------------------------------------------------
# 四、隐藏测试
# --------------------------------------------------------------------------

HIDDEN_DIR = TASK / "hidden" / "tests_hidden"
HIDDEN_DIR.mkdir(parents=True, exist_ok=True)

(HIDDEN_DIR / "test_sync_admission_lifecycle.py").write_text('''"""T2-05 隐藏测试：同步任务准入、心跳与失活回收全生命周期验证。

断言不变量：
1. 准入原子性：同一签名在途任务在并发下只能起一个 worker，重复提交返回 admission="reused"；
2. 心跳与防失活：进度推进与查询均续期心跳，无活动僵尸任务按失活超时回收；
3. 终态回收：终态记录过期后其签名与取消标记一并清理，失活回收后名额即时释放；
4. 消费方出口：409 响应必须携带在途任务列表，GET /sync/jobs/{id} 必须保留 admission 快照。
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
        assert "running_jobs" in body
        assert body["running_jobs"] == ["job-1"]
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
''', encoding="utf-8")
print("隐藏测试写入完毕")

# --------------------------------------------------------------------------
# 五、隐藏分组（groups.json）
# --------------------------------------------------------------------------

(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-05",
    "note": "pytest 侧分组。组 = 一个出口/一条独立事实。coherence 组权重最高，断言任务全生命周期在并发与清理下的行为一致性；p2p 既有用例一条红则本轮作废。",
    "groups": [
        {
            "id": "admission_exit",
            "weight": 1,
            "port": "准入出口：同一任务同一时刻最多一个 worker，并发重复提交原子复用",
            "tests": [
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_serial_identical_submissions_reuse_in_flight_job",
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_concurrent_submissions_admit_single_worker",
            ],
        },
        {
            "id": "heartbeat_exit",
            "weight": 1,
            "port": "心跳出口：写回与查询均算进度推进，僵尸任务失活回收",
            "tests": [
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_progress_writeback_prolongs_job_lifecycle",
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_recent_read_protects_job_from_stale_reap",
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_unattended_inactive_job_is_reaped_as_failed",
            ],
        },
        {
            "id": "reclaim_exit",
            "weight": 1,
            "port": "回收出口：终态记录与标记同生共死，失活回收释放并发名额",
            "tests": [
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_terminal_job_prunes_signatures_and_cancellation_markers",
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_reaped_stale_job_releases_capacity_for_new_submission",
            ],
        },
        {
            "id": "consumer_exit",
            "weight": 1,
            "port": "消费方出口：409 冲突带在途清单，查询接口暴露复用准入状态",
            "tests": [
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_http_capacity_conflict_reports_running_jobs",
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_http_sync_job_query_exposes_admission_status",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "全生命周期一致性：串行复用→并发竞态只起一个→清理干净→重新准入",
            "tests": [
                "hidden/tests_hidden/test_sync_admission_lifecycle.py::test_full_lifecycle_coherence_across_concurrency_and_cleanup",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "既有用例白名单见任务根 p2p.json。任一条红 → 本轮作废（0 分）。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")
print("groups.json 写入完毕")

# --------------------------------------------------------------------------
# 六、提示词 / 校准 / meta 初步更新
# --------------------------------------------------------------------------

PROMPTS = TASK / "prompts"
PROMPTS.mkdir(exist_ok=True)

(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

数据中心的自动补数最近出现三种怪现象，我怀疑它们是一件事：

1. 同一批股票的补数任务，有时会被起两遍——两份任务同时跑，日志里抓了重复的数据；
2. 偶尔弹出一次"容量已满"的提示之后，后面所有补数要么一直报容量已满，
   要么跳到一个根本查不到的任务编号，只能重启应用；
3. 明明已经在跑的任务，过一会儿再看就"没了"或者显示失败，可数据明明还在往里写。

## 验收要求

修好之后，下面几条必须同时成立：
- 同一个任务，无论多少个入口同时提交，系统里同一时刻最多只有一个在跑；
- 一次"容量已满"不能堵死后续提交：提示里要能看清现在有哪些任务在跑，等待即可重试；
- 正在推进、或仍有人在查询的任务，不会被系统当成僵尸误杀；真正失活的任务要给出
  明确的失败原因并释放名额，留下的痕迹（去重依据、取消标记）也要跟着一起清掉；
- 对外能查到某个任务是不是复用已提交的那次——这个信息不能凭空消失。

我不要求你改测试，也不需要新增功能。请把根因修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把补数任务"是否在跑、何时结束、结束留下什么"在系统里走一遍，会发现它被多处各自判断，而且口径互相打架：

1. 任务防重与准入被拆成了两截：前面说没有相同任务在跑，等真正准备写入时早已被另一个请求捷足先登，结果系统里并存了两份一模一样的任务；
2. 任务"有没有在推进"被狭隘地理解了：只有某些操作才被当成进度，其他正当的写回路径全被忽略，导致正常干活的任务被巡检逻辑当成僵尸误杀；
3. 任务结束时的清理只清了一半：记录本身移除了，但去重依据和取消标记被永久留在了索引表里，导致后续相同任务要么被幽灵标记绊倒，要么误判在途；
4. 对外接口把内部状态遮蔽了：容量超限时不告诉调用方当前有哪些任务在占名额，任务查询接口也把"本次是否复用了已有任务"的关键信息抹掉了。

任何一处单独看似乎都在"维护自己的状态"，拼在一起就是整套任务生命周期处处脱节。
''', encoding="utf-8")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时成立的表述：

1. 准入不变量：相同签名的在途任务，任何时刻在系统里最多只能有一个在跑；判定与写入必须原子完成，并发提交必须严格互斥并复用在途记录。
2. 心跳不变量：正在推进（任何有效写回）或仍有调用方查询的任务，绝不能被判定为失活僵尸；两者满足其一即为存活。
3. 清理不变量：终态记录的生命周期必须与去重签名、取消标记同生共死；记录过期被逐出时，关联的去重索引与标记必须一并清空，不得留有幽灵残留。
4. 出口不变量：容量超限的响应必须如实反映当前在途的任务集合；对外查询任务状态时，复用准入这一事实必须对调用方可见。

已被否决的思路（不要重提）：

- "给每次提交加前端生成的全局唯一标识"——破坏了基于任务参数的去重契约，无法解决多入口重复提交的问题。
- "在提交入口加一段等待或延时重试以躲过并发窗口"——把竞态窗口藏进随机等待，高并发下依然必然穿透。
- "把并发容量上限调大以掩盖容量耗尽"——僵尸任务与幽灵索引未清理，名额迟早再次耗尽，不修根因。
''', encoding="utf-8")

CALIB_DIR = TASK / "calibration"
CALIB_DIR.mkdir(exist_ok=True)
(CALIB_DIR / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-05",
    "calibrated": False,
    "target_band": [0.25, 0.55],
    "owner": "author",
    "policy": "§6.4 硬纪律：出题模型不得给自己出的题做校准。本表在盲测完成前保持空表，calibrated 恒为 false。",
    "gate": {
        "note": "出题侧门禁（§5.3）由 runs/blind/tools/packgate.py 跑，原始输出见 gate_*.json。这些不是校准数据，不参与 pass@1 统计。",
        "anchor_solution": "gate_fixed.json",
        "partial_solution": "gate_partial.json",
        "injected_state": "gate_injected_x20.json",
    },
    "blind_runs": {
        "note": "每一行 = 一次『只给第 1 级提示词』的完整作答。由非出题模型填写。",
        "columns": [
            "run_id", "model", "tier", "prompt_level", "pass@1", "score",
            "failed_groups", "p2p_broken", "notes",
        ],
        "rows": [],
    },
    "summary": {
        "runs": 0,
        "pass_at_1": None,
        "confidence_interval": None,
        "in_band": None,
        "conclusion": None,
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta = {
    "schema": 1,
    "id": "T2-05",
    "tier": "medium",
    "attempts": 2,
    "title": "同一批数据反复起 worker；偶发 409 后再也起不来",
    "repo": {
        "id": "core",
        "snapshot": "slim-py",
        "commit": "6192aa25c2791be655dd11783c77683b9cb2aa7b",
    },
    "allowed_paths": [
        "backend/astock_backtester/data/sync.py",
        "backend/astock_backtester/service.py",
    ],
    "forbidden_paths": [
        "tests/**",
        "pyproject.toml",
        "**/conftest.py",
        "backend/astock_backtester/data/warehouse.py",
        "backend/astock_backtester/models.py",
        "packs/**",
        "console/**",
    ],
    "visible": {
        "prune": [],
    },
    "redactions": [
        {"file": "AGENTS.md", "sections": ["9", "15", "18"]},
        {"file": "CHANGELOG.md", "versions": ["1.5.2", "1.6.0", "1.6.1"]},
    ],
    "checks": [
        {
            "kind": "pytest",
            "hidden": "hidden/tests_hidden",
            "groups": "hidden/groups.json",
            "p2p": "p2p.json",
        },
    ],
    "budget": {
        "grade_timeout_s": 240,
        "diff_line_cap": 4000,
    },
    "calibration": {
        "target_band": [0.25, 0.55],
        "calibrated": False,
    },
}
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
(TASK / "p2p.json").write_text(json.dumps({"schema": 1, "task": "T2-05", "tests": []}, ensure_ascii=False, indent=2), encoding="utf-8")
print("初步元数据写入完毕")

# --------------------------------------------------------------------------
# 七、p2p 候选收集与 visible.prune 确定
# --------------------------------------------------------------------------

baseline_tree = packgate.GATES / "T2-05-collect-baseline"
if baseline_tree.exists():
    shutil.rmtree(baseline_tree, ignore_errors=True)
packgate.build_tree("T2-05", meta, baseline_tree, [])

done = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_sync_jobs.py", "tests/test_data_service_http.py",
     "--collect-only", "-q", "-p", "no:cacheprovider"],
    cwd=baseline_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
all_candidates = [
    line.strip() for line in done.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line.strip()
]
candidates = sorted({
    c for c in all_candidates
    if c.startswith("tests/test_sync_jobs.py") or (c.startswith("tests/test_data_service_http.py") and "sync" in c.lower())
})
print(f"收集到 {len(candidates)} 条 p2p 候选用例")

# 1. 跑基线：剔除基线就红的用例
print("正在测试基线用例...")
env_baseline = packgate.build_env(packgate.CFG, str(baseline_tree))
res_baseline = subprocess.run(
    [sys.executable, "-m", "pytest", *candidates, "-q", "-p", "no:cacheprovider"],
    cwd=baseline_tree, env=env_baseline, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
print("基线 pytest 输出摘要：")
for line in res_baseline.stdout.splitlines()[-5:]:
    print("  ", line)

# 2. 跑注入态：测试哪些用例在注入态下变红
injected_tree = packgate.GATES / "T2-05-collect-injected"
if injected_tree.exists():
    shutil.rmtree(injected_tree, ignore_errors=True)
patches = sorted((TASK / "inject" / "patches").glob("*.patch"))
packgate.build_tree("T2-05", meta, injected_tree, patches)

env_injected = packgate.build_env(packgate.CFG, str(injected_tree))
res_injected = subprocess.run(
    [sys.executable, "-m", "pytest", *candidates, "-q", "-p", "no:cacheprovider"],
    cwd=injected_tree, env=env_injected, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
print("注入态 pytest 输出摘要：")
for line in res_injected.stdout.splitlines()[-10:]:
    print("  ", line)

PRUNED_TESTS = [
    "tests/test_data_service_http.py::test_service_reports_reused_sync_admission_and_capacity_conflict",
    "tests/test_sync_jobs.py::test_expired_terminal_job_records_are_pruned_with_their_markers",
    "tests/test_sync_jobs.py::test_identical_in_flight_sync_is_reused_without_a_second_worker",
    "tests/test_sync_jobs.py::test_pruning_protects_running_and_recently_read_jobs",
    "tests/test_sync_jobs.py::test_service_level_budget_rejects_extra_jobs_with_running_ids",
    "tests/test_sync_jobs.py::test_terminal_job_records_are_capped_by_count_within_retention",
]

p2p_tests = sorted([c for c in candidates if c not in PRUNED_TESTS])
print(f"最终 p2p 用例数：{len(p2p_tests)}，裁剪用例数：{len(PRUNED_TESTS)}")

(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-05",
    "note": "基线（未注入）全绿的既有用例。快照里注入后变红或名字点名答案的用例已进 visible.prune。",
    "tests": p2p_tests,
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta["visible"]["prune"] = PRUNED_TESTS
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print("meta.json 与 p2p.json 更新完成")

# --------------------------------------------------------------------------
# 八、参考解说明文档（reference/notes.md 成题版）
# --------------------------------------------------------------------------

(TASK / "reference" / "notes.md").write_text('''# T2-05 参考解说明（成题版）

> 本文件只进 `reference/`，永不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁全过（见 `calibration/` 的 gate_*.json）。

## 一、注入点清单（6 处：sync.py × 4 + service.py × 2）

| # | 位置 | 注入代码及改动 | 设计意图与症状面 |
|---|---|---|---|
| 1 | `backend/.../data/sync.py` L359 `_admit` | 拆为无锁遍历查重 + 锁内 prune/预算检查/写入；改写 docstring 删去竞态警示 | 制造 TOCTOU 竞态：并发提交相同签名任务时，两线程均未命中快照，分别启动独立 worker，造成重复抓取与数据写冲突 |
| 2 | `sync.py` L349 `_put_locked` | 删除 `running/cancelling` 分支的 `self._last_progress_at[status.job_id] = time.monotonic()` | 心跳丢失：写回进度不再刷新活跃时间，正常运行的长任务被后台巡检误判为超时僵尸并被强制按 failed 回收 |
| 3 | `sync.py` L340 `_drop_locked` | 删除 `self._signatures.pop` 与 `self._cancelled.discard`，保留注释误导为"直接命中" | 终态回收丢标记：终态记录清理后去重签名与取消标记残留在集合中，导致同签名后续任务被幽灵索引绊住、内存泄漏 |
| 4 | `sync.py` L481 `get_job` | 锁内进入后先调用 `now = time.monotonic(); self._prune_locked(now)` 再取任务 | 双重回收窗：每次查询均触发修剪与回收，制造额外回收竞态窗，让轮询查询与准入回收产生非预期的抢先清除 |
| 5 | `backend/.../service.py` L940 `GET /sync/jobs/` | `payload = job.model_dump(mode="json"); payload.pop("admission", None)` 剥除 admission | 消费方接口信息遮蔽：对外隐藏本次准入是否复用了已有在途任务的标记，调用方无法获知复用状态 |
| 6 | `service.py` L1256 `SyncCapacityError` | 409 异常响应体中删除 `"running_jobs": exc.running` 字段 | 消费方接口信息遮蔽：容量超限时只报 409 错误码，不提供当前占用的在途任务清单，前端/调用方无法显示在途详情 |

全部 6 处注入均静默且自然：不改变函数接口参数与类型签名，合成注释读起来自圆其说。

## 二、原生现状与诱饵点边界

1. **诱饵点：`run_full_market` 同步旁路不走 `_admit`**：
   - 仓库原生设计中，`run_full_market` 是阻塞执行的单次同步导入接口，其设计目标就是不排队、不占在途异步 worker 预算。
   - 试图给 `run_full_market` 加锁或塞入 `_admit` 是无用功，甚至可能造成持锁抓取死锁。
2. **原生第二场景：`_append_error` / `_append_failure` 绕过 `_put_locked`**：
   - 仓库原生代码中，`_append_error` 与 `_append_failure` 直接操作 `self._jobs[job_id] = status`，没有调用 `self._put_locked`。
   - 锚解（`fix.patch`）顺手将这两处收口为 `self._put_locked(status)`，保证错误/失败写回同样视为任务进展并刷新心跳。
3. **可见测试裁剪（`visible.prune`，共 6 条）**：
   - 注入态实测变红用例（3条）：
     - `tests/test_data_service_http.py::test_service_reports_reused_sync_admission_and_capacity_conflict`（因 409 剥除 `running_jobs` 变红）
     - `tests/test_sync_jobs.py::test_expired_terminal_job_records_are_pruned_with_their_markers`（因 `_drop_locked` 丢标记变红）
     - `tests/test_sync_jobs.py::test_pruning_protects_running_and_recently_read_jobs`（因 `get_job` 触发 prune 变红）
   - 名字点名答案与实现细节用例（3条）：
     - `tests/test_sync_jobs.py::test_identical_in_flight_sync_is_reused_without_a_second_worker`（用例名直接点名"同参数任务复用且不启第二个 worker"）
     - `tests/test_sync_jobs.py::test_service_level_budget_rejects_extra_jobs_with_running_ids`（用例名点名 running_ids）
     - `tests/test_sync_jobs.py::test_terminal_job_records_are_capped_by_count_within_retention`（点名清理与保留期规则）
   - 裁剪后快照沙箱内可见用例 0 红（全部 35 条 p2p 用例在注入态下全绿）。

## 三、锚解形态

1. **`backend/astock_backtester/data/sync.py`**：
   - 恢复 `_admit` 在单次 `with self._lock:` 内完成 prune、查重、预算检查、写入与签名登记的原子闭环，恢复警示 docstring；
   - 恢复 `_put_locked` 在 `running/cancelling` 状态下刷新 `self._last_progress_at[status.job_id] = time.monotonic()`；
   - 恢复 `_drop_locked` 清理终态记录时同步清除 `self._signatures.pop(job_id, None)` 与 `self._cancelled.discard(job_id)`；
   - 恢复 `get_job` 为轻量读取，移除进入时的 `self._prune_locked(now)`；
   - 顺手将 `_append_error` 与 `_append_failure` 的直接字典赋值改为调用 `self._put_locked(status)`。
2. **`backend/astock_backtester/service.py`**：
   - 恢复 `GET /sync/jobs/{id}` 完整序列化，不剥除 `admission` 字段；
   - 恢复 `SyncCapacityError` 409 处理分支，在返回 JSON 中保留 `"running_jobs": exc.running`。

## 四、陷阱与半成品分析

- **陷阱 A（半成品演示 `partial.patch`）**：只修复后端 `sync.py` 中的准入与心跳（①+②）。
  实测得分 33.33/100：`admission_exit` 与 `heartbeat_exit` 绿；但 `reclaim_exit`（标记未清）、`consumer_exit`（HTTP 出口缺失字段）以及 `coherence`（全周期清理失败）全红。
- **陷阱 B（只改大容量上限）**：将 `max_concurrent_jobs` 改大以避开 409。无法解决并发重复起 worker 与僵尸任务永久累积问题。
- **陷阱 C（调小失活超时）**：将 `RUNNING_JOB_STALE_SECONDS` 改小以掩盖卡死，会导致正常慢速抓取的任务被频繁误杀。
- **陷阱 D（单侧加锁）**：仅给外部入口或 `run_full_market` 加锁，未解决 `_admit` 内部检查与写入的窗口脱节。

## 五、§6.5 反过易检查清单

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"——注入采用自然口径注释重写，无历史 commit 痕迹，AGENTS/CHANGELOG 已脱敏。
- [x] ≥1 个"看似可疑但实际正确"的诱饵点——`run_full_market` 同步旁路（不走准入是正确设计）。
- [x] 每组隐藏测试有第二数据场景——准入组含串行+确定性并发竞态；心跳组含写回续命+查询续命+真正僵尸；回收组含终态标记清理+释放预算再起任务；消费方含 409 与 GET 两接口；coherence 组串联完整生命周期。
- [x] 症状与三级提示词不含任何文件/函数/常量名——提示词严格遵守零名词规范。
- [x] 只修一个端口的半成品必然 <100——`partial.patch` 实测 33.33 分（<100 严格成立）。
- [x] 出题者自评"10 分钟能一次做对"→ 退回重做——**预计 >10 分钟**：需同时厘清准入原子性、心跳写回、终态标记联动与 HTTP 消费方契约四个维度。

## 六、门禁自验结果（§5.3，packgate 实测 2026-09-30）

| 门禁 | 结果 |
|---|---|
| 锚解（`fix.patch`） | **100.0**，5 组全绿，p2p 35/35 绿 |
| 半成品（`partial.patch`） | **33.33**（<100 严格成立，p2p 35/35 绿） |
| 注入态（`injected`） | **0.0**，5 组全红，p2p 35/35 绿 |
| 注入态 ×20（`injected --repeat 20`） | 得分稳定 **0.0**，零 flaky |
| 参考解路径合规 | 仅修改 `allowed_paths` 内文件，未触碰 `forbidden_paths` |
| 沙箱可见红测试 | **0**（6 条变红/点名用例已全部裁剪入 `visible.prune`） |

## 七、校准状态（§6.4）

`calibration/results.json` 保持空表，`calibrated = false`。出题模型不参与盲测校准。
''', encoding="utf-8")
print("reference/notes.md 写入完成")

# --------------------------------------------------------------------------
# 九、自验门禁结果
# --------------------------------------------------------------------------

def summarize_gate(name: str, path: Path):
    if not path.is_file():
        print(f"[{name}] 文件不存在：{path}")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = data.get("runs", [])
    print(f"[{name}] 轮数: {len(runs)}")
    for i, r in enumerate(runs):
        score = r.get("score")
        p2p_fails = len(r.get("p2p_failures", []))
        group_summary = ", ".join(f"{g['id']}: {'绿' if g['passed'] else '红'}" for g in r.get("groups", []))
        if len(runs) <= 3 or i == 0 or i == len(runs) - 1:
            print(f"  轮 {i+1}: 得分 {score}, p2p 失败 {p2p_fails}, 组状态: [{group_summary}]")
    scores = {r.get("score") for r in runs}
    p2p_all_fails = {len(r.get("p2p_failures", [])) for r in runs}
    print(f"  --> 得分集合: {scores}, p2p 失败数集合: {p2p_all_fails}")

print("\n--- 门禁结果汇总 ---")
summarize_gate("FIXED", TASK / "calibration" / "gate_fixed.json")
summarize_gate("PARTIAL", TASK / "calibration" / "gate_partial.json")
summarize_gate("INJECTED_X20", TASK / "calibration" / "gate_injected_x20.json")




