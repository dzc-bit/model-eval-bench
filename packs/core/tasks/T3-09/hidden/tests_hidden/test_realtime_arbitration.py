"""T3-09 隐藏测试：实时行情仲裁、并发控制与代际一致性。

高规格约束：
1. 确定性竞态：threading.Event 对齐 + 极小预算（0.02~0.2s），断言因果序与终态，禁用裸计时与长 sleep。
2. 隐藏不变量 ≥5 组：single_flight_exit、late_publish_exit、generation_exit、chain_budget_exit、
   coherence、background_refresh_exit、cls_home_waiter_exit。
3. 零测试名点名受测实现与文件。
"""

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Event, Thread
from unittest.mock import MagicMock

import pytest

try:
    from astock_backtester.models import (
        MarketBreadth,
        MarketIndexQuote,
        RealtimeMarketSnapshot,
        SectorMover,
    )
    from astock_backtester.data.realtime import RealtimeMarketProvider
    from astock_backtester.data.warehouse import Warehouse
except ModuleNotFoundError:
    from backend.astock_backtester.models import (
        MarketBreadth,
        MarketIndexQuote,
        RealtimeMarketSnapshot,
        SectorMover,
    )
    from backend.astock_backtester.data.realtime import RealtimeMarketProvider
    from backend.astock_backtester.data.warehouse import Warehouse

UTC = timezone.utc


# ===========================================================================
# 组 1：single_flight_exit（单飞通道生命周期：超时后直到 worker 结束前保持繁忙）
# ===========================================================================

def test_single_flight_holds_lock_until_background_worker_settles(tmp_path):
    """场景一：超时返回时不得提前 release 单飞通道，未结束的 worker 仍在跑时后续请求必须快速让路。"""
    worker_started = Event()
    release_worker = Event()
    worker_executions = 0
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.02)

    def slow_worker(diagnostics, deadline=None, cancel_event=None):
        nonlocal worker_executions
        worker_executions += 1
        worker_started.set()
        release_worker.wait(timeout=2.0)
        return MarketBreadth(up=2500, down=2000, flat=500, total=5000, source="mock-breadth")

    provider._call_live_breadth = slow_worker

    first_diag: list[str] = []
    second_diag: list[str] = []

    # 第一轮请求：必定超时返回 None
    res1 = provider._fetch_live_breadth_with_budget(first_diag)
    assert res1 is None
    assert worker_started.is_set()
    assert any("超时" in item for item in first_diag)

    # 第二轮请求：第一轮 worker 尚未结束，单飞通道必须保持繁忙拒绝，不得堆叠新的执行
    res2 = provider._fetch_live_breadth_with_budget(second_diag)
    assert res2 is None
    assert any("繁忙" in item for item in second_diag)
    assert worker_executions == 1, "超时后提前释放了单飞通道，导致后序请求排队或重复提交工作"

    # 放行后台 worker 结束
    release_worker.set()


def test_single_flight_concurrent_rejection_prevents_stacking(tmp_path):
    """场景二：并发请求同一上游时，只有首个请求进入后台执行，并发者立即收到繁忙标识。"""
    worker_started = Event()
    release_worker = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=2.0)

    def blocking_worker(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_worker.wait(timeout=2.0)
        return MarketBreadth(up=2800, down=1900, flat=300, total=5000, source="mock-breadth")

    provider._call_live_breadth = blocking_worker

    first_returned = Event()
    diag1: list[str] = []
    diag2: list[str] = []

    def run_first():
        provider._fetch_live_breadth_with_budget(diag1)
        first_returned.set()

    t1 = Thread(target=run_first)
    t1.start()
    assert worker_started.wait(timeout=2.0)

    # 并发第二请求：通道正被占用
    res2 = provider._fetch_live_breadth_with_budget(diag2)
    assert res2 is None
    assert any("繁忙" in item for item in diag2)

    release_worker.set()
    assert first_returned.wait(timeout=2.0)
    t1.join(timeout=2.0)


# ===========================================================================
# 组 2：late_publish_exit（迟到 worker 结果与诊断不得污染对外状态）
# ===========================================================================

def test_late_worker_diagnostics_discarded_after_timeout(tmp_path):
    """场景一：超时后返回的私有 worker 诊断信息，不得迟到写进对外诊断列表。"""
    worker_started = Event()
    wrapper_finished = Event()
    release_worker = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.03)

    def slow_diagnostics_worker(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_worker.wait(timeout=2.0)
        diagnostics.append("late-private-diagnostic-probe")
        return None

    provider._call_live_breadth = slow_diagnostics_worker

    shared_diag: list[str] = []

    def run_call():
        provider._fetch_live_breadth_with_budget(shared_diag)
        wrapper_finished.set()

    t = Thread(target=run_call)
    t.start()
    assert worker_started.wait(timeout=2.0)
    assert wrapper_finished.wait(timeout=2.0)

    # 包装函数已超时退出，现在放行后台迟到写入
    release_worker.set()
    t.join(timeout=2.0)

    assert "late-private-diagnostic-probe" not in shared_diag, "迟到 worker 诊断泄漏进了对外共享状态"
    assert any("超时" in item for item in shared_diag)


def test_late_sector_rows_blocked_from_publishing_when_cancelled(tmp_path):
    """场景二：当取消标记被设置或截止时间耗尽，_publish_sector_rows 必须拦截数据落入外部输出。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path), sector_time_budget=0.05)
    rows_out: list[dict] = []
    diagnostics: list[str] = []

    cancel_evt = Event()
    cancel_evt.set()  # 已被取消

    published = provider._publish_sector_rows(
        rows=[{"name": "迟到板块", "change_pct": 0.05}],
        sector_rows_out=rows_out,
        cancel_event=cancel_evt,
        deadline=None,
        diagnostics=diagnostics,
    )
    assert not published, "请求取消后 _publish_sector_rows 仍返回成功"
    assert len(rows_out) == 0, "取消状态下的迟到板块数据被发布到了对外输出列表中"


# ===========================================================================
# 组 3：generation_exit（代际仲裁：严格世代递增，旧世代与无世代旧数据不得覆盖）
# ===========================================================================

def test_generation_arbitration_blocks_older_generation_same_timestamp(tmp_path):
    """场景一：同一时间戳下，晚返回的旧世代快照不得覆盖已落库的新世代快照。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    ts = datetime(2026, 9, 30, 9, 35, 0, tzinfo=UTC)

    snap_gen1 = RealtimeMarketSnapshot(status="live", source="gen-1", updated_at=ts, message="gen1")
    snap_gen2 = RealtimeMarketSnapshot(status="live", source="gen-2", updated_at=ts, message="gen2")
    snap_gen1_late = RealtimeMarketSnapshot(status="live", source="gen-1-late", updated_at=ts, message="gen1_late")

    provider._remember_successful_snapshot(snap_gen1, generation=1)
    assert provider.retained_successful_snapshot().source == "gen-1"

    # 世代 2 到达
    provider._remember_successful_snapshot(snap_gen2, generation=2)
    assert provider.retained_successful_snapshot().source == "gen-2"

    # 世代 1 迟到响应到达：时间戳相同，但世代落后，绝对不得覆盖
    provider._remember_successful_snapshot(snap_gen1_late, generation=1)
    retained = provider.retained_successful_snapshot()
    assert retained.source == "gen-2", f"旧世代快照覆盖了新世代快照：当前源为 {retained.source}"


def test_legacy_unversioned_write_cannot_overwrite_generation_tracked_snapshot(tmp_path):
    """场景二：未携带代际的遗留调用，无论其时钟如何，都不得覆盖已经由代际守卫保护的有效快照。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    t_old = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
    t_new = datetime(2026, 9, 30, 9, 35, 0, tzinfo=UTC)

    # 世代 5 的快照先行入库
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(status="live", source="gen-5-authoritative", updated_at=t_old, message="g5"),
        generation=5,
    )

    # 遗留无世代调用，即使声称时间戳更新，也不得覆盖
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(status="live", source="legacy-unversioned", updated_at=t_new, message="legacy"),
        generation=None,
    )
    retained = provider.retained_successful_snapshot()
    assert retained.source == "gen-5-authoritative", f"无世代遗留快照覆盖了权威世代快照：当前源为 {retained.source}"


# ===========================================================================
# 组 4：chain_budget_exit（整链预算：单源超时不阻塞整链，后续请求快速恢复）
# ===========================================================================

def test_slow_source_does_not_exhaust_overall_chain_budget_or_cause_cascading_timeout(tmp_path):
    """场景一：慢源执行期间，后续发起的请求不得排队并发生级联超时，必须快速返回繁忙避让。"""
    worker_started = Event()
    release_slow = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.03)

    def slow_worker(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_slow.wait(timeout=2.0)
        return MarketBreadth(up=2000, down=2000, flat=1000, total=5000, source="slow")

    provider._call_live_breadth = slow_worker

    # 第一次请求：超时
    d1: list[str] = []
    res1 = provider._fetch_live_breadth_with_budget(d1)
    assert res1 is None
    assert worker_started.is_set()

    # 第二次请求在 slow worker 仍在执行时发起：
    # 锚解：通道繁忙，立即返回 None + 诊断中含“繁忙”，耗时几乎为 0，绝对不超时！
    # 注入缺陷：提前释放了锁，新请求被丢进单线程执行器排队，等待耗尽预算，抛出“超时”！
    d2: list[str] = []
    res2 = provider._fetch_live_breadth_with_budget(d2)
    assert res2 is None
    assert any("繁忙" in item for item in d2)
    assert not any("超时" in item for item in d2), "慢请求未完成时，后序请求排队等待并超时，整链被慢源占死拖垮"

    release_slow.set()


def test_fresh_request_recovers_cleanly_after_timeout_worker_finishes(tmp_path):
    """场景二：上一轮超时 worker 彻底完成后，后续的新请求能够重新成功获取数据，通道恢复畅通。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.02)
    release_first = Event()

    def worker_stub(diagnostics, deadline=None, cancel_event=None):
        if not release_first.is_set():
            release_first.wait(timeout=1.0)
            return None
        return MarketBreadth(up=3100, down=1600, flat=300, total=5000, source="recovered-breadth")

    provider._call_live_breadth = worker_stub

    diag1: list[str] = []
    res1 = provider._fetch_live_breadth_with_budget(diag1)
    assert res1 is None
    assert any("超时" in item for item in diag1)

    # 放行首个 worker 结束
    release_first.set()

    # 等待该后台 worker 完成回调释放通道
    provider._get_breadth_executor().shutdown(wait=True)
    provider._breadth_executor = None

    # 后续新鲜请求发起：成功拿到最新数据
    diag2: list[str] = []
    res2 = provider._fetch_live_breadth_with_budget(diag2)
    assert res2 is not None
    assert res2.source == "recovered-breadth"
    assert res2.up == 3100


# ===========================================================================
# 组 5：coherence（跨端与并发交织场景下，系统对外始终保持最新有效快照）
# ===========================================================================

def test_arbitration_coherence_under_racing_generations_and_failures(tmp_path):
    """场景一：并发乱序交织下（早世代慢请求、新世代成功、失败快照），对外保留的快照严格保持权威性。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    t1 = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 30, 9, 31, 0, tzinfo=UTC)
    t3 = datetime(2026, 9, 30, 9, 32, 0, tzinfo=UTC)

    # 1. 世代 2 请求先成功返回
    snap2 = RealtimeMarketSnapshot(status="live", source="gen2-authoritative", updated_at=t2, message="s2")
    provider._remember_successful_snapshot(snap2, generation=2)

    # 2. 世代 3 请求失败（不可用快照），不应冲掉已有的成功快照
    # （retained_successful_snapshot 始终为 snap2）
    assert provider.retained_successful_snapshot().source == "gen2-authoritative"

    # 3. 世代 1 请求迟到到达并尝试发布（同一秒生成，携带旧代际数据）
    snap1_late = RealtimeMarketSnapshot(status="live", source="gen1-stale", updated_at=t2, message="s1")
    provider._remember_successful_snapshot(snap1_late, generation=1)
    assert provider.retained_successful_snapshot().source == "gen2-authoritative"

    # 4. 世代 4 正确到达并顺利更新
    snap4 = RealtimeMarketSnapshot(status="live", source="gen4-newest", updated_at=t3, message="s4")
    provider._remember_successful_snapshot(snap4, generation=4)
    assert provider.retained_successful_snapshot().source == "gen4-newest"


def test_retained_snapshot_is_deep_copied_isolated(tmp_path):
    """场景二：对外返回的快照必须是 deepcopy 隔离对象，外部原地篡改不得污染内部缓存。"""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    snap = RealtimeMarketSnapshot(
        status="live",
        source="immutability-check",
        updated_at=datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC),
        diagnostics=["diag-original"],
        message="immutability-check",
    )
    provider._remember_successful_snapshot(snap, generation=1)

    fetched = provider.retained_successful_snapshot()
    assert fetched is not None
    fetched.diagnostics.append("external-tamper")

    refetched = provider.retained_successful_snapshot()
    assert "external-tamper" not in refetched.diagnostics, "外部就地修改污染了 provider 内部留存快照"


# ===========================================================================
# 组 6：background_refresh_exit
#        （后台刷新闸门：失败必须能被再次调度，在途期间不得重复起任务）
# ===========================================================================

def _await_background_gate_release(provider, attempts: int = 400) -> bool:
    """等待后台刷新闸门自行复位（release 回调在 worker 线程里跑）。"""
    for _ in range(attempts):
        with provider._yesterday_sector_lock:
            if not provider._yesterday_sector_in_flight:
                return True
        time.sleep(0.01)
    return False


def test_background_refresh_gate_reopens_after_failed_refresh(tmp_path):
    """场景一：首次后台刷新抛错后，闸门必须复位，后续请求要能重新调度刷新。"""
    worker_entered = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    submissions: list[int] = []

    def flaky_refresh(_diagnostics):
        submissions.append(len(submissions) + 1)
        if len(submissions) == 1:
            worker_entered.set()
            raise RuntimeError("昨日池上游首次刷新失败")
        return []

    provider._fetch_yesterday_strong_sectors = flaky_refresh

    provider._yesterday_sector_snapshot_or_schedule([])
    assert worker_entered.wait(timeout=2.0), "首次后台刷新根本没有跑起来"
    assert _await_background_gate_release(provider), (
        "首次后台刷新失败后闸门没有复位，后续刷新被永久挡住"
    )

    provider._yesterday_sector_snapshot_or_schedule([])
    for _ in range(400):
        if len(submissions) >= 2:
            break
        time.sleep(0.01)
    assert len(submissions) >= 2, (
        f"首次刷新失败后无法再次调度后台刷新：累计只提交了 {len(submissions)} 次"
    )
    assert _await_background_gate_release(provider)


def test_background_refresh_in_flight_is_not_restacked(tmp_path):
    """场景二：刷新在途时连续请求都走陈旧兜底，且后台任务只允许被调度一次。"""
    worker_entered = Event()
    release_worker = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"
    _stale_yesterday_cache(provider)

    submissions: list[int] = []

    def blocking_refresh(_diagnostics):
        submissions.append(len(submissions) + 1)
        worker_entered.set()
        release_worker.wait(timeout=5.0)
        return []

    provider._fetch_yesterday_strong_sectors = blocking_refresh

    try:
        first = provider._yesterday_sector_snapshot_or_schedule([])
        assert worker_entered.wait(timeout=2.0), "后台刷新没有启动"

        follow_up: list[list[SectorMover]] = []
        for _ in range(3):
            follow_up.append(provider._yesterday_sector_snapshot_or_schedule([]))

        assert len(submissions) == 1, (
            f"刷新在途时又重复调度了后台任务：累计提交 {len(submissions)} 次"
        )
        for index, sectors in enumerate(follow_up, start=1):
            assert [sector.name for sector in sectors] == ["陈旧的昨日强势板块"], (
                f"在途期间第 {index} 个请求没有拿到陈旧兜底数据：{sectors}"
            )
        assert [sector.name for sector in first] == ["陈旧的昨日强势板块"]
    finally:
        release_worker.set()
        _await_background_gate_release(provider)


# ===========================================================================
# 组 7：cls_home_waiter_exit
#        （单飞等待者：owner 失败时不得回退陈旧缓存，成功时不得重复发请求）
# ===========================================================================

def test_cls_home_waiter_rejects_stale_cache_after_owner_failure(tmp_path):
    """场景一：owner 刷新失败且缓存已过有效期时，等待者必须一并失败，不得静默吃陈旧缓存。"""
    owner_entered = Event()
    release_owner = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), timeout=2.0)
    _stale_cls_home_cache(provider)

    request_count: list[int] = []

    def failing_requester(_url, **_kwargs):
        request_count.append(len(request_count) + 1)
        owner_entered.set()
        release_owner.wait(timeout=2.0)
        raise RuntimeError("CLS home 上游刷新失败")

    provider.requester = failing_requester

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(provider._fetch_cls_home_payload)
        assert owner_entered.wait(timeout=2.0)
        waiter = executor.submit(provider._fetch_cls_home_payload)
        time.sleep(0.15)  # 让等待者确实进入等待分支
        release_owner.set()

        with pytest.raises(Exception):
            owner.result(timeout=5.0)
        with pytest.raises(Exception) as waiter_error:
            waiter.result(timeout=5.0)

    assert "single-flight request failed" in str(waiter_error.value), (
        f"等待者没有如实报告失败，而是拿到了别的东西：{waiter_error.value!r}"
    )
    assert len(request_count) == 1, "owner 失败后等待者自己又发了一次请求"


def test_cls_home_waiters_share_one_upstream_request(tmp_path):
    """场景二：owner 成功时两个并发等待者拿到一致结果，且真实请求只发一次。"""
    owner_entered = Event()
    release_owner = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), timeout=2.0)
    payload = {
        "code": 200,
        "data": {"index_quote": [], "up_down_dis": {"rise_num": 4, "fall_num": 0}},
    }
    request_count: list[int] = []

    def requester(_url, **_kwargs):
        request_count.append(len(request_count) + 1)
        owner_entered.set()
        release_owner.wait(timeout=2.0)
        return _StubResponse(payload)

    provider.requester = requester

    with ThreadPoolExecutor(max_workers=3) as executor:
        owner = executor.submit(provider._fetch_cls_home_payload)
        assert owner_entered.wait(timeout=2.0)
        waiters = [executor.submit(provider._fetch_cls_home_payload) for _ in range(2)]
        time.sleep(0.15)
        release_owner.set()
        owner_payload = owner.result(timeout=5.0)
        waiter_payloads = [item.result(timeout=5.0) for item in waiters]

    assert owner_payload == payload
    for index, value in enumerate(waiter_payloads, start=1):
        assert value == payload, f"第 {index} 个等待者拿到的结果与 owner 不一致：{value!r}"
    assert len(request_count) == 1, (
        f"单飞失效：{len(request_count)} 个并发调用各发了一次上游请求"
    )


class _StubResponse:
    """最小 HTTP 响应替身（隐藏用例自带，不依赖可见测试的任何符号）。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _stale_cls_home_cache(provider) -> None:
    """给 provider 预置一份「已过有效期」的 CLS home 缓存。"""
    provider._cls_home_cache = {"code": 200, "data": {"up_down_dis": {"rise_num": 1, "fall_num": 1}}}
    provider._cls_home_cached_at = time.monotonic() - provider.cls_home_cache_ttl - 1


def _stale_yesterday_cache(provider) -> None:
    """给 provider 预置一份「已过有效期」的昨日强势板块缓存。"""
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cache = [
        SectorMover(name="陈旧的昨日强势板块", change_pct=0.05, source="eastmoney-yesterday-limit-up")
    ]
    provider._yesterday_sector_cached_at = (
        time.monotonic() - provider.yesterday_sector_cache_ttl - 1
    )
