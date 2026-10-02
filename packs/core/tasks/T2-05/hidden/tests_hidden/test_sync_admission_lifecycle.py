"""T2-05 隐藏测试：同步任务准入、心跳与失活回收全生命周期验证。

判据纪律（2026-10-03 重写）：**只考行为，不考写法**。
所有断言都落在被测代码的公开行为上——``start_full_market`` /
``start_capital_flow_backfill`` / ``cancel_job`` / ``get_job`` 的返回值、真正起了
几个 worker（provider 侧观测）、HTTP 响应体，以及"记录消失后管理器上不再留有该
任务痕迹"这一可观测事实。用例不引用 ``SyncJobManager`` 的任何私有属性名或私有
方法名：把记账结构改名、换数据结构、换调用顺序，判分结果必须不变。

时钟纪律：失活/保留期是墙钟分钟级口径，真实等待不可行。用例装一只可控时钟
（``_Clock``，偏移式 + 可在锁内挂起），把"过了多久"变成确定性推进，既不依赖
``time.sleep`` 的边界运气，也不依赖被测代码自己的超时常量叫什么。

断言的不变量：
1. 准入原子性：同一签名在途任务在并发下只能起一个 worker，重复提交返回 admission="reused"；
2. 心跳与防失活：进度推进与查询均续期心跳，无活动僵尸任务按失活超时回收；
3. 终态回收：终态记录过期后其去重签名与取消标记一并清理，失活回收后名额即时释放；
4. 消费方出口：409 冲突带在途清单，查询接口暴露复用准入状态。
"""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd
from astock_backtester.data.sync import SyncCapacityError, SyncJobManager
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.service import create_server

_OPENER = build_opener(ProxyHandler({}))
_LOOPBACK_TIMEOUT_S = 10

# 全市场补数的任务窗口固定在 2015 年首周：串行复用与并发竞态两条用例必须用同一个
# 窗口同一批票，签名才相同。
_WINDOW = ("2015-01-01", "2015-01-05")
_BATCH_A = ["000001", "000002"]
_BATCH_B = ["000003", "000004"]
_BATCH_C = ["600519", "600520"]


# ==============================================================================
# 编排件：可控时钟 / 放行式 provider / 痕迹自省
# ==============================================================================


class _Clock:
    """偏移式可控时钟：真实时间照常走，用例要"跳过"某段时间时自己 advance。

    ``block_first`` 制造一处**故意**的挂起点：第一个读时钟的调用者停下来，等所有
    并发提交者就位后再多留一小段真实时间。判定与写入之间的缝隙于是被确定性撑开，
    而测试不需要知道被测代码内部把哪一步叫做什么。
    """

    def __init__(self, monkeypatch) -> None:
        self._real_monotonic = time.monotonic
        self._real_wall = time.time
        self._offset = 0.0
        self._block: tuple[threading.Event, float] | None = None
        monkeypatch.setattr(time, "monotonic", self.monotonic)
        monkeypatch.setattr(time, "monotonic_ns", self.monotonic_ns)
        monkeypatch.setattr(time, "perf_counter", self.monotonic)
        monkeypatch.setattr(time, "time", self.wall_clock)

    def block_first(self, all_arrived: threading.Event, grace_seconds: float = 0.3) -> None:
        self._block = (all_arrived, grace_seconds)

    def advance(self, seconds: float) -> None:
        self._offset += seconds

    def monotonic(self) -> float:
        if self._block is not None:
            all_arrived, grace = self._block
            self._block = None
            assert all_arrived.wait(timeout=10), "并发提交者没有全部就位"
            time.sleep(grace)
        return self._real_monotonic() + self._offset

    def monotonic_ns(self) -> int:
        return int(self.monotonic() * 1_000_000_000)

    def wall_clock(self) -> float:
        return self._real_wall() + self._offset


class _SymbolGate:
    """单只股票的放行闸：worker 走到这只票时 entered 亮起并等 release。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def open(self) -> None:
        self.release.set()

    def reached(self, timeout: float = 10.0) -> bool:
        assert self.entered.wait(timeout=timeout), "worker 没有走到被拦的股票"
        return True


class _GatedProvider:
    """假 provider：记录每个 (worker 线程, 股票) 抓取，并可在指定股票上卡住。

    "起了几个 worker"由此可数：任务内部的抓取线程池固定关成 1（见 ``_manager``），
    抓取线程的个数就等于 worker 的个数。
    """

    def __init__(self, gated: tuple[str, ...] = ()) -> None:
        self.gated = set(gated)
        self._calls: list[tuple[int, str]] = []
        self._gates: dict[str, _SymbolGate] = {}
        self._lock = threading.Lock()

    def gate(self, symbol: str) -> _SymbolGate:
        with self._lock:
            return self._gates.setdefault(symbol, _SymbolGate())

    def open_gate(self, symbol: str) -> None:
        self.gate(symbol).open()

    def open_all(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
        for gate in gates:
            gate.open()

    def workers(self) -> set[int]:
        with self._lock:
            return {thread_id for thread_id, _ in self._calls}

    def symbols(self) -> list[str]:
        with self._lock:
            return [symbol for _, symbol in self._calls]

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        with self._lock:
            self._calls.append((threading.get_ident(), symbol))
        if symbol in self.gated:
            gate = self.gate(symbol)
            gate.entered.set()
            assert gate.release.wait(timeout=15), f"用例没有放行 {symbol}"
        return pd.DataFrame(
            {
                "symbol": [symbol],
                "trade_date": [end_date],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "float_market_cap": [100.0],
                "total_market_cap": [120.0],
            }
        )


def _manager(tmp_path, provider, **overrides) -> SyncJobManager:
    """任务管理器：抓取线程池固定 1（worker 数 = 抓取线程数），其余按用例覆盖。"""
    settings: dict = {"full_market_workers": 1, "full_market_batch_size": 100, "max_concurrent_jobs": 4}
    settings.update(overrides)
    return SyncJobManager(warehouse=Warehouse(tmp_path), provider=provider, **settings)


def _start(manager: SyncJobManager, symbols: list[str]):
    return manager.start_full_market(symbols=symbols, start_date=_WINDOW[0], end_date=_WINDOW[1])


def _wait_terminal(manager: SyncJobManager, job_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        if job is None or job.status not in ("running", "cancelling"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"{job_id} 没有在 {timeout}s 内走到终态")


def _leftover_traces(manager: SyncJobManager, needle: str) -> list[str]:
    """管理器上任何以 ``needle`` 为键/元素的容器痕迹（**不看具体属性名**）。

    回收口径是"痕迹跟着记录一起清"：记录都查不到了，去重依据、取消标记、读取与
    心跳时间戳就不许再以这个 job_id 为键留在管理器里。这里只断言"某个容器里出现
    了这个 id"这件可观测事实，属性改名或换数据结构都不影响判分。
    """
    hits: list[str] = []

    def walk(value: object, path: str, depth: int) -> None:
        if depth > 3:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and needle in key:
                    hits.append(f"{path} 的键 {key!r}")
                walk(item, f"{path}[{key!r}]", depth + 1)
        elif isinstance(value, (set, frozenset, list, tuple)):
            for index, item in enumerate(value):
                if isinstance(item, str) and needle in item:
                    hits.append(f"{path}[{index}] 的元素 {item!r}")
                walk(item, f"{path}[{index}]", depth + 1)
        elif getattr(value, "job_id", None) == needle:
            hits.append(f"{path} 仍持有该任务的记录")

    for name, value in vars(manager).items():
        walk(value, name, 0)
    return hits


def _post(url: str, payload: dict) -> dict:
    request = Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def _post_allow_error(url: str, payload: dict) -> tuple[int, dict]:
    request = Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(url: str) -> dict:
    with _OPENER.open(Request(url, method="GET"), timeout=_LOOPBACK_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _stub_status(job_id: str, status: str, admission: str):
    from datetime import date

    from astock_backtester.models import SyncJobStatus

    return SyncJobStatus(
        job_id=job_id,
        mode="full_market_bootstrap",
        status=status,
        admission=admission,
        total_symbols=1,
        start_date=date(2015, 1, 1),
        end_date=date(2015, 1, 5),
    )


def _serve(tmp_path, sync_manager):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = sync_manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ==============================================================================
# 组 1：admission_exit（权重 1）—— 同一任务同一时刻最多一个 worker
# ==============================================================================


def test_serial_identical_submissions_reuse_in_flight_job(tmp_path):
    """串行重复提交：同批票的第二次提交并入在途任务，不起第二个 worker。

    第二次提交故意打乱股票顺序——"同一批数据"是集合口径，不是数组口径。
    """
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        first = _start(manager, _BATCH_A)
        assert first.admission == "started", "第一次提交应当新起任务"
        provider.gate("000002").reached()

        second = _start(manager, list(reversed(_BATCH_A)))
        assert second.admission == "reused", "同批票的重复提交必须并入在途任务"
        assert second.job_id == first.job_id
        assert len(provider.workers()) == 1, "重复提交起了第二个 worker"
    finally:
        provider.open_all()


def test_concurrent_submissions_admit_single_worker(tmp_path, monkeypatch):
    """并发重复提交：判定与写入之间不许有缝，多个入口同时提交也只起一个 worker。"""
    provider = _GatedProvider(gated=("000001",))
    manager = _manager(tmp_path, provider, max_concurrent_jobs=4)
    submitters = 8
    clock = _Clock(monkeypatch)
    all_arrived = threading.Event()
    arrived = 0
    arrived_lock = threading.Lock()
    results: list[tuple[str, str]] = []
    results_lock = threading.Lock()

    def submit() -> None:
        nonlocal arrived
        with arrived_lock:
            arrived += 1
            if arrived == submitters:
                all_arrived.set()
        job = _start(manager, list(_BATCH_A))
        with results_lock:
            results.append((job.job_id, job.admission))

    # 第一个进入判定的线程会在时钟里挂住，等所有提交者就位后再多留一段真实时间。
    clock.block_first(all_arrived)
    threads = [threading.Thread(target=submit, daemon=True) for _ in range(submitters)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not [thread for thread in threads if thread.is_alive()], "提交线程没有全部结束"
        assert len(results) == submitters
        assert len({job_id for job_id, _ in results}) == 1, f"同批票起出了多个任务：{results}"
        assert sum(1 for _, admission in results if admission == "started") == 1
        assert len(provider.workers()) == 1, f"起了多个 worker：{provider.symbols()}"
    finally:
        provider.open_all()


def test_identical_submission_reuses_a_job_that_is_being_cancelled(tmp_path):
    """取消中的任务仍占名额：同批票再提交并入它，而不是趁取消另起一个 worker。"""
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        first = _start(manager, _BATCH_A)
        provider.gate("000002").reached()
        cancelling = manager.cancel_job(first.job_id)
        assert cancelling is not None and cancelling.status == "cancelling"

        second = _start(manager, _BATCH_A)
        assert second.admission == "reused", "取消中的任务还在途，同批票必须并入它"
        assert second.job_id == first.job_id
        assert len(provider.workers()) == 1

        provider.open_all()
        assert _wait_terminal(manager, first.job_id).status == "cancelled"
    finally:
        provider.open_all()


# ==============================================================================
# 组 2：heartbeat_exit（权重 1）—— 写回与查询都算活着，僵尸必须回收
# ==============================================================================


def test_progress_writeback_prolongs_job_lifecycle(tmp_path, monkeypatch):
    """进度推进续命：一小时里没人查询，但作业一直在推进，就不许被当成僵尸。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002", "000003"))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, ["000001", "000002", "000003"])
        provider.gate("000002").reached()  # 000001 已抓完：此刻有一次进度写回

        clock.advance(1700)
        provider.open_gate("000002")  # 又一次进度写回，落在推进后的时刻
        provider.gate("000003").reached()

        clock.advance(1700)
        assert _start(manager, _BATCH_C).admission == "started"  # 公开入口顺带触发一次回收
        assert manager.get_job(job.job_id).status == "running", "仍在推进的任务被误判成僵尸"
    finally:
        provider.open_all()


def test_recent_read_protects_job_from_stale_reap(tmp_path, monkeypatch):
    """近期被读也算活着：前端还在轮询的任务不会被回收。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, _BATCH_A)
        provider.gate("000002").reached()

        clock.advance(1200)
        assert manager.get_job(job.job_id).status == "running"  # 轮询续命

        clock.advance(1200)  # 距上次查询 20 分钟，仍在阈值内
        assert _start(manager, _BATCH_C).admission == "started"
        assert manager.get_job(job.job_id).status == "running", "仍在被查询的任务被误判成僵尸"
    finally:
        provider.open_all()


def test_reading_a_job_does_not_kill_it(tmp_path, monkeypatch):
    """读一次任务不该顺手把它杀掉：查询是"它还活着"的证据，不是判它死亡的触发器。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, _BATCH_A)
        provider.gate("000002").reached()

        clock.advance(3600)  # 客户端很久没看它
        seen = manager.get_job(job.job_id)
        assert seen is not None and seen.status == "running", "查询本身把正在跑的任务判成了失活"

        clock.advance(1700)  # 距这次查询 28 分钟
        assert _start(manager, _BATCH_C).admission == "started"
        assert manager.get_job(job.job_id).status == "running"
    finally:
        provider.open_all()


def test_unattended_inactive_job_is_reaped_as_failed(tmp_path, monkeypatch):
    """无人推进无人读的僵尸按失活回收：判为 failed 并给出明确原因。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, _BATCH_A)
        provider.gate("000002").reached()

        clock.advance(3600)  # 没有任何推进，也没有任何查询
        assert _start(manager, _BATCH_C).admission == "started"

        reaped = manager.get_job(job.job_id)
        assert reaped is not None and reaped.status == "failed", "失活任务没有被回收"
        assert "失活" in (reaped.last_error or ""), f"失活原因必须说清：{reaped.last_error!r}"
    finally:
        provider.open_all()


# ==============================================================================
# 组 3：reclaim_exit（权重 1）—— 痕迹与记录同生共死，失活回收让出名额
# ==============================================================================


def test_reaped_stale_job_releases_capacity_for_new_submission(tmp_path, monkeypatch):
    """失活回收释放预算：僵尸占着的名额必须让出来，否则一次 409 就堵死后续提交。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider, max_concurrent_jobs=1)
    try:
        zombie = _start(manager, _BATCH_A)
        provider.gate("000002").reached()

        clock.advance(3600)
        fresh = _start(manager, _BATCH_C)  # 名额只有 1 个：僵尸不回收就必然 409
        assert fresh.admission == "started"
        assert fresh.job_id != zombie.job_id
        assert manager.get_job(zombie.job_id).status == "failed"
    finally:
        provider.open_all()


def test_terminal_job_prunes_signatures_and_cancellation_markers(tmp_path, monkeypatch):
    """终态记录与标记同生共死：记录过期回收后不留任何以它为键的痕迹。

    取消标记是"用户已经按下停止"的唯一载体，记录没了还留着它，同一个任务号再被
    写入时就会一出生就被判死；去重签名留着则让同批票的再提交被一条不存在的记录
    绊住。两条痕迹都必须随记录一起消失。
    """
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, _BATCH_A)
        provider.gate("000002").reached()
        manager.cancel_job(job.job_id)
        provider.open_all()
        assert _wait_terminal(manager, job.job_id).status == "cancelled"

        clock.advance(3600)  # 超过保留期，也超过"最近被读"的保护期
        assert _start(manager, _BATCH_C).admission == "started"  # 公开入口顺带触发一次回收

        assert manager.get_job(job.job_id) is None, "过期终态记录没有被回收"
        assert _leftover_traces(manager, job.job_id) == [], "记录没了，去重依据/取消标记却还留着"

        again = _start(manager, _BATCH_A)
        assert again.admission == "started", "同批票的再提交被幽灵记录绊住了"
        assert again.job_id != job.job_id
    finally:
        provider.open_all()


# ==============================================================================
# 组 4：consumer_exit（权重 1）—— 409 带在途清单，查询接口暴露复用状态
# ==============================================================================


def test_http_capacity_conflict_reports_running_jobs(tmp_path):
    """409 响应必须能查出占用名额的任务编号（字段名与形态自定）。"""

    class StubSyncManager:
        def __init__(self):
            self.calls = 0

        def start_full_market(self, symbols, start_date, end_date):
            self.calls += 1
            if self.calls == 3:
                raise SyncCapacityError(["job-1", "job-2"], 2)
            return _stub_status("job-1", "running", "started" if self.calls == 1 else "reused")

    server, thread = _serve(tmp_path, StubSyncManager())
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/sync/full-market"
        payload = {"symbols": ["000001"], "start_date": _WINDOW[0], "end_date": _WINDOW[1]}
        _post(url, payload)
        _post(url, payload)
        status, body = _post_allow_error(url, payload)

        assert status == HTTPStatus.CONFLICT
        assert body["code"] == "sync_capacity"
        # 契约只要求"占用名额的任务编号可查"，不规定字段名：断言 body["running_jobs"]
        # 等于把锚解自己起的名字当成题目要求，模型换个字段名或写进文案就判错。
        blob = json.dumps(body, ensure_ascii=False)
        assert "job-1" in blob and "job-2" in blob, f"409 必须暴露在途任务清单：{body}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_sync_job_query_exposes_admission_status(tmp_path):
    """GET /sync/jobs/{id} 必须暴露 admission：复用准入状态对前端/调用方可见。"""

    class StubSyncManager:
        def get_job(self, job_id: str):
            if job_id == "job-reused":
                return _stub_status("job-reused", "running", "reused")
            return None

    server, thread = _serve(tmp_path, StubSyncManager())
    try:
        data = _get(f"http://127.0.0.1:{server.server_address[1]}/sync/jobs/job-reused")
        assert "job" in data
        assert data["job"].get("admission") == "reused"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_http_capacity_conflict_lists_the_really_running_job(tmp_path):
    """端到端：真在跑的任务占满名额时，409 报出的是那个真任务的编号。

    用真管理器走一遍 HTTP 入口（不是打桩抛异常），确认"在途清单"来自实际占位的
    任务，而不是调用方自己编的字符串。
    """
    provider = _GatedProvider(gated=("000003",))
    manager = _manager(tmp_path, provider, max_concurrent_jobs=1)
    server, thread = _serve(tmp_path, manager)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/sync/full-market"
        payload = {"symbols": _BATCH_B, "start_date": _WINDOW[0], "end_date": _WINDOW[1]}
        started = _post(url, payload)["job"]
        assert started["admission"] == "started"
        provider.gate("000003").reached()

        # 换一批票：同批票会被并入在途任务（200），只有新批次才撞得上容量上限。
        other = {"symbols": _BATCH_A, "start_date": _WINDOW[0], "end_date": _WINDOW[1]}
        status, body = _post_allow_error(url, other)
        assert status == HTTPStatus.CONFLICT, f"预算已满却给出了 {status}：{body}"
        assert body["code"] == "sync_capacity"
        assert started["job_id"] in json.dumps(body, ensure_ascii=False), f"409 没有报出在途任务：{body}"
    finally:
        provider.open_all()
        server.shutdown()
        thread.join(timeout=5)


# ==============================================================================
# 组 5：coherence（权重 2）—— 完成与取消两条路径各一场景，全生命周期一致
# ==============================================================================


def test_full_lifecycle_coherence_across_concurrency_and_cleanup(tmp_path, monkeypatch):
    """全周期链：串行复用 → 并发只起一个 → 跑完清理干净 → 同批票再进起全新任务。"""
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002", "000004"))
    manager = _manager(tmp_path, provider, max_concurrent_jobs=3)
    try:
        # 1. 串行复用
        first = _start(manager, _BATCH_A)
        assert first.admission == "started"
        provider.gate("000002").reached()
        reused = _start(manager, _BATCH_A)
        assert reused.admission == "reused" and reused.job_id == first.job_id

        # 2. 并发竞态：另一批票同样只允许一个 worker
        submitters = 6
        all_arrived = threading.Event()
        arrived = 0
        arrived_lock = threading.Lock()
        concurrent: list[tuple[str, str]] = []
        concurrent_lock = threading.Lock()

        def submit() -> None:
            nonlocal arrived
            with arrived_lock:
                arrived += 1
                if arrived == submitters:
                    all_arrived.set()
            job = _start(manager, _BATCH_B)
            with concurrent_lock:
                concurrent.append((job.job_id, job.admission))

        clock.block_first(all_arrived)
        threads = [threading.Thread(target=submit, daemon=True) for _ in range(submitters)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=30)
        assert not [item for item in threads if item.is_alive()], "提交线程没有全部结束"
        assert len({job_id for job_id, _ in concurrent}) == 1, f"并发提交起出了多个任务：{concurrent}"
        assert sum(1 for _, admission in concurrent if admission == "started") == 1
        provider.gate("000004").reached()
        assert len(provider.workers()) == 2, f"两批票各一个 worker，实际：{provider.symbols()}"

        # 3. 跑完并清理
        provider.open_all()
        finished = [_wait_terminal(manager, job_id) for job_id, _ in concurrent]
        finished.append(_wait_terminal(manager, first.job_id))
        assert all(job is not None and job.status in ("completed", "completed_with_errors") for job in finished)

        clock.advance(3600)
        assert _start(manager, ["000009"]).admission == "started"  # 公开入口顺带触发一次回收
        for job_id, _ in concurrent:
            assert manager.get_job(job_id) is None, "过期终态记录没有被回收"
            assert _leftover_traces(manager, job_id) == [], "记录没了却还留着以它为键的痕迹"
        assert manager.get_job(first.job_id) is None
        assert _leftover_traces(manager, first.job_id) == []

        # 4. 同批票再进来是全新任务
        final = _start(manager, _BATCH_A)
        assert final.admission == "started" and final.job_id != first.job_id
    finally:
        provider.open_all()


def test_cancel_then_prune_then_resubmit_starts_fresh(tmp_path, monkeypatch):
    """第二数据场景（取消路径）：取消 → 终态清理 → 同批票重提必须是全新 started。

    与上一条"完成路径"互补：清理动作必须连去重签名与取消标记一起带走，留下任何
    一个都会把"停止过的任务"变成幽灵——签名残留让重提被旧记录绊住，取消标记残留
    让新任务一出生就背着别人的停止令。
    """
    clock = _Clock(monkeypatch)
    provider = _GatedProvider(gated=("000002",))
    manager = _manager(tmp_path, provider)
    try:
        job = _start(manager, _BATCH_A)
        assert job.admission == "started"
        provider.gate("000002").reached()
        assert manager.cancel_job(job.job_id).status == "cancelling"  # 用户按下停止
        provider.open_all()
        assert _wait_terminal(manager, job.job_id).status == "cancelled"

        clock.advance(3600)
        assert _start(manager, ["000009"]).admission == "started"  # 公开入口顺带触发一次回收
        assert manager.get_job(job.job_id) is None
        assert _leftover_traces(manager, job.job_id) == [], "取消标记/去重签名没有跟着记录一起清"

        again = _start(manager, _BATCH_A)
        assert again.admission == "started", "同批票的重提被幽灵记录绊住了"
        assert again.job_id != job.job_id
        provider.gate("000002").reached()
        assert manager.get_job(again.job_id).status == "running", "新任务一出生就背着旧任务的取消令"
    finally:
        provider.open_all()
