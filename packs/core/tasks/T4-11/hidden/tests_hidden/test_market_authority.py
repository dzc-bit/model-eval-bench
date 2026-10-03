"""T4-11 隐藏测试（行情侧）：对外权威快照、通道生命周期与迟到发布防护。

本题的"唯一事实"由五个出口共同守：

* ``single_flight_exit``    —— 通道在一次成功读取后必须回到可用状态；并发时不得堆叠；
* ``late_publish_exit``     —— 超时/取消之后，worker 的诊断与分块行不得再落到对外状态；
* ``generation_exit``       —— 世代水位只进不退，旧世代与无世代数据都不得覆盖新世代；
* ``chain_budget_exit``     —— 慢源占用期间后续请求立即让路，worker 结束后通道立刻恢复；
* ``coherence_realtime``    —— 乱序到达与外部就地修改交织下，留存快照始终权威且隔离。

写法纪律：确定性竞态只用 ``threading.Event`` 对齐 + 极小预算（0.02~0.2s），
断言因果序与终态，不写裸计时；零网络（上游 fetcher 全部换成进程内 stub）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread

try:
    from astock_backtester.models import (
        MarketBreadth,
        RealtimeMarketSnapshot,
        SectorMover,
    )
    from astock_backtester.data.realtime import RealtimeMarketProvider
    from astock_backtester.data.warehouse import Warehouse
except ModuleNotFoundError:
    from backend.astock_backtester.models import (
        MarketBreadth,
        RealtimeMarketSnapshot,
        SectorMover,
    )
    from backend.astock_backtester.data.realtime import RealtimeMarketProvider
    from backend.astock_backtester.data.warehouse import Warehouse

UTC = timezone.utc


def _provider(tmp_path, **overrides) -> RealtimeMarketProvider:
    return RealtimeMarketProvider(Warehouse(tmp_path), **overrides)


def _breadth(total: int = 5000, source: str = "stub-breadth") -> MarketBreadth:
    up = int(total * 0.5)
    down = int(total * 0.36)
    return MarketBreadth(up=up, down=down, flat=total - up - down, total=total, source=source)


def _snapshot(source: str, updated_at: datetime, diagnostics=None) -> RealtimeMarketSnapshot:
    return RealtimeMarketSnapshot(
        status="live",
        source=source,
        updated_at=updated_at,
        message=source,
        diagnostics=list(diagnostics or []),
    )


# ===========================================================================
# 出口一：single_flight_exit —— 通道生命周期（成功要交还，忙时要让路）
# ===========================================================================


def test_breadth_channel_is_reusable_after_a_successful_read(tmp_path):
    """一次成功读取之后通道必须交还：紧随其后的请求拿到的是数据，不是"繁忙"。"""
    provider = _provider(tmp_path, breadth_time_budget=2.0)
    provider._call_live_breadth = lambda diagnostics, deadline=None, cancel_event=None: _breadth(
        5000, "first-read"
    )

    first: list[str] = []
    result_first = provider._fetch_live_breadth_with_budget(first)
    assert result_first is not None, f"首次读取没有拿到数据：{first}"
    assert result_first.source == "first-read"

    # 等后台 worker 线程彻底收尾（通道交还由它触发），把"交还"与"下次请求"
    # 之间的时序钉死，不让断言去赌回调线程的调度。
    provider._get_breadth_executor().shutdown(wait=True)
    provider._breadth_executor = None

    second: list[str] = []
    result_second = provider._fetch_live_breadth_with_budget(second)
    assert result_second is not None, (
        f"成功读取之后通道没有交还，下一次请求被拒：{second}"
    )
    assert not any("繁忙" in item for item in second), (
        f"成功读取之后通道仍被判为繁忙：{second}"
    )


def test_breadth_channel_rejects_a_second_worker_while_one_is_running(tmp_path):
    """同一上游只允许一个 worker 在跑：并发第二个请求立即收到繁忙标识，执行次数保持 1。"""
    worker_started = Event()
    release_worker = Event()
    executions: list[int] = []
    provider = _provider(tmp_path, breadth_time_budget=2.0)

    def blocking_worker(diagnostics, deadline=None, cancel_event=None):
        executions.append(len(executions) + 1)
        worker_started.set()
        release_worker.wait(timeout=5.0)
        return _breadth(5000, "blocking")

    provider._call_live_breadth = blocking_worker

    finished = Event()
    first_diagnostics: list[str] = []

    def run_first():
        provider._fetch_live_breadth_with_budget(first_diagnostics)
        finished.set()

    thread = Thread(target=run_first)
    thread.start()
    assert worker_started.wait(timeout=5.0), "首个 worker 没有启动"

    second_diagnostics: list[str] = []
    result = provider._fetch_live_breadth_with_budget(second_diagnostics)
    assert result is None
    assert any("繁忙" in item for item in second_diagnostics), (
        f"并发请求没有收到繁忙标识：{second_diagnostics}"
    )
    assert executions == [1], f"并发请求堆叠出了第二个 worker：{executions}"

    release_worker.set()
    assert finished.wait(timeout=5.0)
    thread.join(timeout=5.0)


# ===========================================================================
# 出口二：late_publish_exit —— 迟到诊断与分块行不得穿透共享状态
# ===========================================================================


def test_timed_out_worker_diagnostics_never_reach_the_caller(tmp_path):
    """超时退出之后，后台 worker 后续写入的诊断属于私有暂存区，不得再并回调用方。"""
    worker_started = Event()
    wrapper_returned = Event()
    release_worker = Event()
    provider = _provider(tmp_path, breadth_time_budget=0.03)

    def slow_worker(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_worker.wait(timeout=5.0)
        diagnostics.append("late-worker-probe")
        return None

    provider._call_live_breadth = slow_worker
    shared: list[str] = []

    def run_call():
        provider._fetch_live_breadth_with_budget(shared)
        wrapper_returned.set()

    thread = Thread(target=run_call)
    thread.start()
    assert worker_started.wait(timeout=5.0), "worker 没有启动"
    assert wrapper_returned.wait(timeout=5.0), "包装函数没有在预算内退出"

    release_worker.set()  # 放行迟到写入
    thread.join(timeout=5.0)
    # 等 worker 线程真正结束：迟到写入若存在，一定在它收尾之前落到共享列表里。
    provider._get_breadth_executor().shutdown(wait=True)
    provider._breadth_executor = None

    assert any("超时" in item for item in shared), f"缺少超时诊断：{shared}"
    assert "late-worker-probe" not in shared, (
        f"迟到 worker 的诊断穿透进了对外共享状态：{shared}"
    )


def test_cancelled_sector_rows_are_not_published_to_the_caller(tmp_path):
    """取消标记已置位时，分块行不得落进调用方的输出列表（先判定、后发布）。"""
    provider = _provider(tmp_path, sector_time_budget=0.05)
    rows_out: list[dict] = []
    diagnostics: list[str] = []

    cancelled = Event()
    cancelled.set()

    published = provider._publish_sector_rows(
        rows=[{"name": "迟到板块", "change_pct": 0.05}],
        sector_rows_out=rows_out,
        cancel_event=cancelled,
        deadline=None,
        diagnostics=diagnostics,
    )

    assert not published, "请求已取消，发布动作仍报成功"
    assert rows_out == [], f"取消之后的迟到分块行被发布到了对外输出：{rows_out}"


# ===========================================================================
# 出口三：generation_exit —— 世代水位只进不退
# ===========================================================================


def test_older_generation_cannot_replace_a_newer_one_on_equal_timestamp(tmp_path):
    """同一时间戳下，晚到的旧世代不得覆盖已经留存的新世代快照。"""
    provider = _provider(tmp_path)
    stamp = datetime(2026, 9, 30, 9, 35, 0, tzinfo=UTC)

    provider._remember_successful_snapshot(_snapshot("gen-1", stamp), generation=1)
    assert provider.retained_successful_snapshot().source == "gen-1"

    provider._remember_successful_snapshot(_snapshot("gen-2", stamp), generation=2)
    assert provider.retained_successful_snapshot().source == "gen-2"

    provider._remember_successful_snapshot(_snapshot("gen-1-late", stamp), generation=1)
    retained = provider.retained_successful_snapshot()
    assert retained.source == "gen-2", f"旧世代覆盖了新世代：当前留存源为 {retained.source}"


def test_unversioned_write_cannot_replace_a_generation_tracked_snapshot(tmp_path):
    """不带世代的遗留调用，即使时钟更新，也不得覆盖已由世代守卫保护的快照。"""
    provider = _provider(tmp_path)
    older = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 9, 45, 0, tzinfo=UTC)

    provider._remember_successful_snapshot(_snapshot("gen-5-authoritative", older), generation=5)

    provider._remember_successful_snapshot(_snapshot("legacy-unversioned", newer), generation=None)
    retained = provider.retained_successful_snapshot()
    assert retained.source == "gen-5-authoritative", (
        f"无世代遗留快照覆盖了权威世代快照：当前留存源为 {retained.source}"
    )


# ===========================================================================
# 出口四：chain_budget_exit —— 慢源不得占死整链
# ===========================================================================


def test_request_behind_a_slow_source_is_answered_immediately_not_queued(tmp_path):
    """慢源仍在跑时，后续请求必须立即让路（繁忙），而不是排队等成一次级联超时。"""
    worker_started = Event()
    release_slow = Event()
    provider = _provider(tmp_path, breadth_time_budget=0.03)

    def slow_worker(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_slow.wait(timeout=5.0)
        return _breadth(5000, "slow")

    provider._call_live_breadth = slow_worker

    first: list[str] = []
    assert provider._fetch_live_breadth_with_budget(first) is None
    assert worker_started.is_set(), "慢 worker 没有启动"

    second: list[str] = []
    assert provider._fetch_live_breadth_with_budget(second) is None
    assert any("繁忙" in item for item in second), f"后续请求没有立即让路：{second}"
    assert not any("超时" in item for item in second), (
        f"后续请求被排到慢源后面并耗尽预算：{second}"
    )

    release_slow.set()


def test_channel_recovers_for_a_fresh_request_once_the_worker_finishes(tmp_path):
    """上一轮 worker 彻底结束之后，新的请求必须能重新拿到数据。"""
    release_first = Event()
    provider = _provider(tmp_path, breadth_time_budget=0.02)

    def worker_stub(diagnostics, deadline=None, cancel_event=None):
        if not release_first.is_set():
            release_first.wait(timeout=5.0)
            return None
        return _breadth(5000, "recovered-breadth")

    provider._call_live_breadth = worker_stub

    first: list[str] = []
    assert provider._fetch_live_breadth_with_budget(first) is None
    assert any("超时" in item for item in first)

    release_first.set()
    provider._get_breadth_executor().shutdown(wait=True)
    provider._breadth_executor = None

    second: list[str] = []
    result = provider._fetch_live_breadth_with_budget(second)
    assert result is not None, f"worker 结束之后通道没有恢复：{second}"
    assert result.source == "recovered-breadth"


# ===========================================================================
# 出口五：coherence_realtime —— 留存快照的权威性与隔离（权重最高）
# ===========================================================================


def test_retained_snapshot_is_isolated_from_later_caller_mutation(tmp_path):
    """留存动作必须与调用方持有的对象隔离：调用方之后就地改它，不得污染留存快照。"""
    provider = _provider(tmp_path)
    stamp = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
    submitted = _snapshot("authority-holder", stamp, diagnostics=["原始诊断"])

    provider._remember_successful_snapshot(submitted, generation=1)
    submitted.diagnostics.append("调用方事后追加")

    retained = provider.retained_successful_snapshot()
    assert "调用方事后追加" not in retained.diagnostics, (
        f"调用方事后修改污染了留存快照：{retained.diagnostics}"
    )
    assert retained.diagnostics == ["原始诊断"]


def test_retained_snapshot_keeps_the_newest_generation_under_out_of_order_arrival(tmp_path):
    """乱序到达（旧世代迟到 + 无世代更新时钟 + 新世代正常）之后，留存的必须是最新世代。"""
    provider = _provider(tmp_path)
    t_early = datetime(2026, 9, 30, 9, 30, 0, tzinfo=UTC)
    t_mid = datetime(2026, 9, 30, 9, 31, 0, tzinfo=UTC)
    t_late = datetime(2026, 9, 30, 9, 32, 0, tzinfo=UTC)

    provider._remember_successful_snapshot(_snapshot("gen-2-authoritative", t_mid), generation=2)
    provider._remember_successful_snapshot(_snapshot("gen-1-late", t_mid), generation=1)
    assert provider.retained_successful_snapshot().source == "gen-2-authoritative", (
        "迟到的旧世代顶掉了权威快照"
    )

    provider._remember_successful_snapshot(_snapshot("legacy-newer-clock", t_late), generation=None)
    assert provider.retained_successful_snapshot().source == "gen-2-authoritative", (
        "无世代调用仅凭更新的时钟顶掉了权威快照"
    )

    provider._remember_successful_snapshot(_snapshot("gen-4-newest", t_late), generation=4)
    assert provider.retained_successful_snapshot().source == "gen-4-newest", (
        "更新的世代没有被采纳"
    )

    provider._remember_successful_snapshot(_snapshot("gen-3-stale", t_early), generation=3)
    assert provider.retained_successful_snapshot().source == "gen-4-newest", (
        "落后的世代把留存快照拉了回去"
    )
