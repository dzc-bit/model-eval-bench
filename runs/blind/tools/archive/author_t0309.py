"""T3-09 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

产出（写进 packs/core/tasks/T3-09/）：
  inject/patches/0001-realtime-arbitration.patch   后端三端口：代际退化、single-flight提前释放、迟到发布
  inject/patches/0002-frontend-market-degrade.patch 前端一端口：部分成功缺失降级重试间隔
  reference/fix.patch                               锚解：四端口全修齐
  reference/partial.patch                           半成品：只修 single-flight 释放时机
  hidden/tests_hidden/test_realtime_arbitration.py  pytest 隐藏测试（4个出口 + coherence 组）
  hidden/groups.json
  hidden-fe/tests_hidden_fe/marketDegrade.hidden.test.ts  vitest 隐藏测试（frontend_degrade_exit 组）
  hidden-fe/groups_fe.json
  p2p.json / p2p-fe.json
  prompts/1.md 2.md 3.md、calibration/results.json、meta.json、reference/notes.md
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T3-09"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

BACKEND_REL = "backend/astock_backtester/data/realtime.py"
FE_REL = "frontend/src/marketRefresh.ts"

backend_src = (REPO / BACKEND_REL).read_text(encoding="utf-8")
fe_src = (REPO / FE_REL).read_text(encoding="utf-8")

# --------------------------------------------------------------------------
# 一、注入变体（四端口）
# --------------------------------------------------------------------------

# 端口 ③：代际仲裁退化（generation 比较改成无条件覆盖/纯时间戳）
P3_ORIGINAL = """            if generation is not None:
                should_update = generation > self._last_snapshot_generation
            elif self._last_snapshot_generation > 0:
                # A generation-tracked snapshot is present — a legacy
                # (no-generation) call must never overwrite it, even
                # when the legacy call has a newer wall-clock timestamp.
                should_update = False
            else:
                should_update = snapshot.updated_at > current.updated_at"""

P3_INJECTED = """            # 简化快照覆盖逻辑：统一按快照更新时间戳比较先后，时间相同也允许覆盖
            should_update = snapshot.updated_at >= current.updated_at"""

assert P3_ORIGINAL in backend_src, "后端代际仲裁原代码未命中"

# 端口 ① 与 ②（都在 _fetch_live_breadth_with_budget 中）：
# ① single-flight 生命周期：提前在 finally 里 release，worker 未结束即放行新请求
# ② 迟到 worker 发布：删 deadline 双检，外部 diagnostics 直接传入 worker
P1_2_ORIGINAL = """        try:
            deadline = monotonic_time.monotonic() + self.breadth_time_budget
            cancel_event = Event()
            # The worker writes only to its private diagnostics list.  The caller
            # publishes those diagnostics ONLY when the future completes within the
            # budget; on timeout the private list is discarded so a late worker can
            # never pollute the shared diagnostics.
            worker_diagnostics: list[str] = []
            executor = self._get_breadth_executor()
            future = executor.submit(
                self._call_live_breadth, worker_diagnostics, deadline, cancel_event
            )
        except Exception:
            self._breadth_in_flight.release()
            raise
        future.add_done_callback(lambda _future: self._breadth_in_flight.release())
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            result = future.result(timeout=remaining)
            # Double-check: even after future.result returns, the computation
            # itself may have exhausted the budget.  Discard the result if so.
            if monotonic_time.monotonic() > deadline:
                raise TimeoutError
            diagnostics.extend(worker_diagnostics)
            return result
        except TimeoutError:
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"实时红绿家数接口超时：{self.breadth_time_budget:g}秒，已继续返回可用行情。")
            return None
        except Exception as exc:
            diagnostics.append(f"实时红绿家数接口失败：{exc}，已继续返回可用行情。")
            return None"""

P1_2_INJECTED = """        try:
            deadline = monotonic_time.monotonic() + self.breadth_time_budget
            cancel_event = Event()
            executor = self._get_breadth_executor()
            # 外部诊断列表直接传入 worker 记录底层明细
            future = executor.submit(
                self._call_live_breadth, diagnostics, deadline, cancel_event
            )
        except Exception:
            self._breadth_in_flight.release()
            raise
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            result = future.result(timeout=remaining)
            return result
        except TimeoutError:
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"实时红绿家数接口超时：{self.breadth_time_budget:g}秒，已继续返回可用行情。")
            return None
        except Exception as exc:
            diagnostics.append(f"实时红绿家数接口失败：{exc}，已继续返回可用行情。")
            return None
        finally:
            # 退出当前调用时释放通道锁，确保后来的刷新请求可以立刻发起
            self._breadth_in_flight.release()"""

assert P1_2_ORIGINAL in backend_src, "后端 single-flight 与红绿家数原代码未命中"

# 端口 ②：迟到板块行发布防护（删取消检查，超时后迟到的行仍写进对外 rows_out）
P2_SECTOR_ORIGINAL = """    def _publish_sector_rows(
        self,
        rows: list[dict],
        sector_rows_out: list[dict] | None,
        cancel_event: Event | None,
        deadline: float | None,
        diagnostics: list[str],
    ) -> bool:
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return False
        if sector_rows_out is not None:
            sector_rows_out[:] = rows
        return True"""

P2_SECTOR_INJECTED = """    def _publish_sector_rows(
        self,
        rows: list[dict],
        sector_rows_out: list[dict] | None,
        cancel_event: Event | None,
        deadline: float | None,
        diagnostics: list[str],
    ) -> bool:
        # 直接输出板块明细，由外层调用方根据快照结果决定是否丢弃
        if sector_rows_out is not None:
            sector_rows_out[:] = rows
        return True"""

assert P2_SECTOR_ORIGINAL in backend_src, "后端板块行发布防护原代码未命中"

backend_injected = backend_src.replace(P3_ORIGINAL, P3_INJECTED, 1)
backend_injected = backend_injected.replace(P1_2_ORIGINAL, P1_2_INJECTED, 1)
backend_injected = backend_injected.replace(P2_SECTOR_ORIGINAL, P2_SECTOR_INJECTED, 1)
assert backend_injected != backend_src

# 端口 ④：前端降级重试缺失（stale/部分成功后不再进入加速重试）
FE_ORIGINAL = """export function refreshIntervalForMarketResult(
  phase: MarketSessionPhase,
  _diagnostics: string[] | undefined,
  hasError = false,
  missingBreadth = false
): number {
  if (!hasError && missingBreadth) {
    return DEGRADED_RETRY_MS;
  }
  return refreshIntervalForPhase(phase, hasError);
}"""

FE_INJECTED = """export function refreshIntervalForMarketResult(
  phase: MarketSessionPhase,
  _diagnostics: string[] | undefined,
  hasError = false,
  missingBreadth = false
): number {
  // 保持标准轮询节奏，不因部分字段缺失导致请求过于密集
  return refreshIntervalForPhase(phase, hasError);
}"""

assert FE_ORIGINAL in fe_src, "前端降级重试函数原代码未命中"
fe_injected = fe_src.replace(FE_ORIGINAL, FE_INJECTED, 1)
assert fe_injected != fe_src

# --------------------------------------------------------------------------
# 二、锚解变体（四端口全修齐）与半成品变体（只修 single-flight）
# --------------------------------------------------------------------------

# 锚解：
# 后端：P1/P2/P3 全恢复正确实现（即 backend_src）
backend_fixed = backend_src

# 前端：恢复 45s 降级重试，并在 nextMarketRefreshMeta 中确保旧快照晚到不回退 last_success_at
FE_FIXED = fe_src.replace(
    """    last_success_at: isPartial
      ? current.last_success_at ?? null
      : snapshot.status === "unavailable"
      ? current.last_success_at ?? null
      : snapshot.updated_at,""",
    """    last_success_at: isPartial
      ? current.last_success_at ?? null
      : snapshot.status === "unavailable"
      ? current.last_success_at ?? null
      : current.last_success_at && snapshot.updated_at && snapshot.updated_at < current.last_success_at
      ? current.last_success_at
      : snapshot.updated_at,""", 1
)
fe_fixed = FE_FIXED

# 半成品（只修 single-flight 端口，保留代际退化、迟到发布和前端降级缺失）
# 在 backend_injected 基础上，仅修复 P1_2 的释放时机（把 finally release 换回 add_done_callback，但保留 P2 和 P3 的注入）
P1_ONLY_FIX = """        try:
            deadline = monotonic_time.monotonic() + self.breadth_time_budget
            cancel_event = Event()
            executor = self._get_breadth_executor()
            # 外部诊断列表直接传入 worker 记录底层明细
            future = executor.submit(
                self._call_live_breadth, diagnostics, deadline, cancel_event
            )
        except Exception:
            self._breadth_in_flight.release()
            raise
        future.add_done_callback(lambda _future: self._breadth_in_flight.release())
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            result = future.result(timeout=remaining)
            return result
        except TimeoutError:
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"实时红绿家数接口超时：{self.breadth_time_budget:g}秒，已继续返回可用行情。")
            return None
        except Exception as exc:
            diagnostics.append(f"实时红绿家数接口失败：{exc}，已继续返回可用行情。")
            return None"""

backend_partial = backend_injected.replace(P1_2_INJECTED, P1_ONLY_FIX, 1)
assert backend_partial != backend_injected
fe_partial = fe_injected  # 前端不动

# --------------------------------------------------------------------------
# 三、补丁产出
# --------------------------------------------------------------------------

INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE.mkdir(parents=True, exist_ok=True)

patch_backend = build_patch(BACKEND_REL, backend_src.splitlines(keepends=True), backend_injected.splitlines(keepends=True))
patch_fe = build_patch(FE_REL, fe_src.splitlines(keepends=True), fe_injected.splitlines(keepends=True))
(INJECT_DIR / "0001-realtime-arbitration.patch").write_text(patch_backend, encoding="utf-8")
(INJECT_DIR / "0002-frontend-market-degrade.patch").write_text(patch_fe, encoding="utf-8")

fix_parts = []
if backend_fixed != backend_injected:
    fix_parts.append(build_patch(BACKEND_REL, backend_injected.splitlines(keepends=True), backend_fixed.splitlines(keepends=True)))
if fe_fixed != fe_injected:
    fix_parts.append(build_patch(FE_REL, fe_injected.splitlines(keepends=True), fe_fixed.splitlines(keepends=True)))
(REFERENCE / "fix.patch").write_text("".join(fix_parts), encoding="utf-8")

partial_parts = [build_patch(BACKEND_REL, backend_injected.splitlines(keepends=True), backend_partial.splitlines(keepends=True))]
(REFERENCE / "partial.patch").write_text("".join(partial_parts), encoding="utf-8")
print("补丁文件生成成功")

# --------------------------------------------------------------------------
# 四、隐藏测试产出
# --------------------------------------------------------------------------

HIDDEN = TASK / "hidden" / "tests_hidden"
HIDDEN_FE = TASK / "hidden-fe" / "tests_hidden_fe"
HIDDEN.mkdir(parents=True, exist_ok=True)
HIDDEN_FE.mkdir(parents=True, exist_ok=True)

(HIDDEN / "test_realtime_arbitration.py").write_text('''"""T3-09 隐藏测试：实时行情仲裁、并发控制与代际一致性。

高规格约束：
1. 确定性竞态：threading.Event 对齐 + 极小预算（0.02~0.2s），断言因果序与终态，禁用裸计时与长 sleep。
2. 隐藏不变量 ≥5 组：single_flight_exit、late_publish_exit、generation_exit、chain_budget_exit、coherence。
3. 零测试名点名受测实现与文件。
"""

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
''', encoding="utf-8")

(HIDDEN_FE / "marketDegrade.hidden.test.ts").write_text('''// T3-09 隐藏测试（前端侧）：降级轮询间隔与时钟/快照代际仲裁守卫。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEGRADED_RETRY_MS,
  nextMarketRefreshMeta,
  refreshIntervalForMarketResult,
  refreshIntervalForPhase
} from "../marketRefresh";
import type { MarketRefreshMeta, RealtimeMarketSnapshot } from "../types";

afterEach(() => {
  vi.useRealTimers();
});

describe("前端降级重试与刷新元数据仲裁", () => {
  it("部分成功快照缺少红绿家数时触发45秒降级刷新间隔", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-05T10:00:00+08:00")); // 交易时段

    const currentMeta: MarketRefreshMeta = {
      phase: "trading",
      status: "idle",
      message: "实时行情已更新",
      next_refresh_ms: 60_000
    };

    const partialSnapshot: RealtimeMarketSnapshot = {
      status: "live",
      source: "ashare-sina",
      updated_at: "2026-06-05T10:00:00+08:00",
      market_phase: "trading",
      indexes: [],
      breadth: null, // 缺少红绿家数，属于部分成功
      strong_sectors: [],
      yesterday_strong_sectors: [],
      message: "红绿家数缺失降级"
    };

    const nextMeta = nextMarketRefreshMeta(currentMeta, partialSnapshot, "trading");
    expect(nextMeta.next_refresh_ms).toBe(DEGRADED_RETRY_MS);
    expect(nextMeta.next_refresh_ms).toBe(45_000);
  });

  it("晚到的旧快照不得覆盖较新的成功时间戳", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-05T10:06:00+08:00"));

    const currentMeta: MarketRefreshMeta = {
      phase: "trading",
      status: "idle",
      message: "实时行情已更新",
      last_success_at: "2026-06-05T10:05:00+08:00",
      next_refresh_ms: 60_000
    };

    const lateSnapshot: RealtimeMarketSnapshot = {
      status: "live",
      source: "late-worker",
      updated_at: "2026-06-05T10:00:00+08:00", // 比当前已成功的时点更早
      market_phase: "trading",
      indexes: [],
      breadth: { up: 3000, down: 1800, flat: 200, total: 5000, source: "test" },
      strong_sectors: [],
      yesterday_strong_sectors: [],
      message: "迟到的旧快照"
    };

    const nextMeta = nextMarketRefreshMeta(currentMeta, lateSnapshot, "trading");
    // 成功时间戳必须保持更新的那一个，不得回跳到较早的时间戳
    expect(nextMeta.last_success_at).toBe("2026-06-05T10:05:00+08:00");
  });

  it("全源失败进入不可用状态并保持降级重试间隔", () => {
    const interval = refreshIntervalForMarketResult("trading", ["所有数据源均超时"], true, false);
    expect(interval).toBe(refreshIntervalForPhase("trading", true));
    expect(interval).toBe(120_000);
  });
});
''', encoding="utf-8")

# --------------------------------------------------------------------------
# 五、分组配置（groups.json + groups_fe.json）
# --------------------------------------------------------------------------

(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-09",
    "note": "pytest 侧分组。高级题硬规格：≥5 组隐藏不变量 + coherence 权重 2 + p2p 组。节点 ID 带 hidden/tests_hidden/ 前缀。",
    "groups": [
        {
            "id": "single_flight_exit",
            "weight": 1,
            "port": "single-flight 生命周期：超时后直到 worker 结束前保持繁忙，不放行新请求排队",
            "tests": [
                "hidden/tests_hidden/test_realtime_arbitration.py::test_single_flight_holds_lock_until_background_worker_settles",
                "hidden/tests_hidden/test_realtime_arbitration.py::test_single_flight_concurrent_rejection_prevents_stacking",
            ],
        },
        {
            "id": "late_publish_exit",
            "weight": 1,
            "port": "迟到发布防护：超时或取消后的迟到诊断与分块行不得污染对外共享状态",
            "tests": [
                "hidden/tests_hidden/test_realtime_arbitration.py::test_late_worker_diagnostics_discarded_after_timeout",
                "hidden/tests_hidden/test_realtime_arbitration.py::test_late_sector_rows_blocked_from_publishing_when_cancelled",
            ],
        },
        {
            "id": "generation_exit",
            "weight": 1,
            "port": "代际仲裁：严格世代递增，旧世代与无世代旧数据绝不覆盖新世代快照",
            "tests": [
                "hidden/tests_hidden/test_realtime_arbitration.py::test_generation_arbitration_blocks_older_generation_same_timestamp",
                "hidden/tests_hidden/test_realtime_arbitration.py::test_legacy_unversioned_write_cannot_overwrite_generation_tracked_snapshot",
            ],
        },
        {
            "id": "chain_budget_exit",
            "weight": 1,
            "port": "整链预算硬约束：单源慢/超时不拖垮其他源，后续请求在 worker 结束后迅速恢复",
            "tests": [
                "hidden/tests_hidden/test_realtime_arbitration.py::test_slow_source_does_not_exhaust_overall_chain_budget_or_cause_cascading_timeout",
                "hidden/tests_hidden/test_realtime_arbitration.py::test_fresh_request_recovers_cleanly_after_timeout_worker_finishes",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "全局快照一致性：并发、迟到与失败交织下，对外保留的快照始终保持权威与隔离",
            "tests": [
                "hidden/tests_hidden/test_realtime_arbitration.py::test_arbitration_coherence_under_racing_generations_and_failures",
                "hidden/tests_hidden/test_realtime_arbitration.py::test_retained_snapshot_is_deep_copied_isolated",
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

(TASK / "hidden-fe" / "groups_fe.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-09",
    "note": "vitest 侧分组。负责前端降级重试与旧快照晚到防护。",
    "groups": [
        {
            "id": "frontend_degrade_exit",
            "weight": 1,
            "port": "前端降级重试：部分成功触发 45s 降级重试；旧快照晚到不得回跳成功时间戳",
            "tests": [
                "tests_hidden_fe/marketDegrade.hidden.test.ts::部分成功快照缺少红绿家数时触发45秒降级刷新间隔",
                "tests_hidden_fe/marketDegrade.hidden.test.ts::晚到的旧快照不得覆盖较新的成功时间戳",
                "tests_hidden_fe/marketDegrade.hidden.test.ts::全源失败进入不可用状态并保持降级重试间隔",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "前端既有用例白名单见任务根 p2p-fe.json。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------------------------------------------------------
# 六、候选白名单与裁剪清单（p2p.json / p2p-fe.json / visible.prune）
# --------------------------------------------------------------------------

# 前端侧既有用例：marketRefresh.test.ts
(TASK / "p2p-fe.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-09",
    "note": "页面侧既有用例白名单（基线全绿、注入不红）。",
    "tests": [
        "src/marketRefresh.test.ts::detects weekend and lunch-break market phases in China time",
        "src/marketRefresh.test.ts::uses lower refresh pressure outside trading and after failures",
        "src/marketRefresh.test.ts::昨日追踪后台刷新不会提高整个行情模块的轮询频率",
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")

print("隐藏测试与分组配置已生成")

# --------------------------------------------------------------------------
# 七、在注入树上实测 tests/test_realtime.py，找出注入红名单与候选白名单
# --------------------------------------------------------------------------

temp_meta = {
    "schema": 1,
    "id": "T3-09",
    "tier": "hard",
    "attempts": 3,
    "repo": {"id": "core", "snapshot": "slim-py+fe", "commit": "6192aa25c2791be655dd11783c77683b9cb2aa7b"},
    "allowed_paths": [
        "backend/astock_backtester/data/realtime.py",
        "backend/astock_backtester/service.py",
        "frontend/src/marketRefresh.ts",
        "frontend/src/components/MarketDashboard.tsx",
    ],
    "forbidden_paths": [
        "tests/**",
        "pyproject.toml",
        "frontend/vitest.config.ts",
        "**/conftest.py",
        "backend/astock_backtester/data/warehouse.py",
        "packs/**",
        "console/**",
    ],
    "visible": {"prune": []},
    "redactions": [
        {"file": "AGENTS.md", "sections": ["5"]},
        {"file": "CHANGELOG.md", "versions": ["1.5.1", "1.6.0"]},
    ],
    "checks": [
        {"kind": "pytest", "hidden": "hidden/tests_hidden", "groups": "hidden/groups.json", "p2p": "p2p.json"},
        {"kind": "vitest", "hidden": "hidden-fe/tests_hidden_fe", "groups": "hidden-fe/groups_fe.json", "p2p": "p2p-fe.json"},
    ],
    "budget": {"grade_timeout_s": 300, "diff_line_cap": 4000},
    "calibration": {"target_band": [0.05, 0.25], "calibrated": False},
}

test_dest = packgate.GATES / "T3-09-find-prune"
if test_dest.exists():
    import shutil
    shutil.rmtree(test_dest, ignore_errors=True)

inject_patches = sorted(INJECT_DIR.glob("*.patch"))
packgate.build_tree("T3-09", temp_meta, test_dest, inject_patches)
(test_dest / ".grade-cache").mkdir(parents=True, exist_ok=True)

# 跑 tests/test_realtime.py，收集变红用例
proc = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_realtime.py", "-q", "--tb=no", "-p", "no:cacheprovider",
     "--basetemp", str(test_dest / ".grade-cache")],
    cwd=test_dest, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
print("pytest 注入测试输出摘要：")
lines = proc.stdout.splitlines()
failed_tests = []
for line in lines:
    if line.startswith("FAILED "):
        test_node = line.split()[1]
        failed_tests.append(test_node)

print(f"注入后失败用例数: {len(failed_tests)}")
for ft in failed_tests:
    print(f"  FAILED: {ft}")

# 显式裁剪名单：注入失败用例 + 点名 single-flight/迟到/代际/预算机制的用例
PRUNE_TESTS = sorted({
    *failed_tests,
    "tests/test_realtime.py::test_resilient_get_clamps_each_attempt_to_remaining_budget",
    "tests/test_realtime.py::test_sector_worker_does_not_publish_rows_after_wrapper_timeout",
    "tests/test_realtime.py::test_sector_timeout_cannot_commit_rows_after_publication_check",
    "tests/test_realtime.py::test_older_realtime_request_cannot_overwrite_newer_success_snapshot",
    "tests/test_realtime.py::test_late_breadth_worker_cannot_publish_diagnostics_after_timeout",
    "tests/test_realtime.py::test_late_sector_worker_cannot_publish_diagnostics_after_timeout",
    "tests/test_realtime.py::test_same_timestamp_reverse_completion_does_not_overwrite",
    "tests/test_realtime.py::test_remember_without_generation_uses_strict_timestamp",
    "tests/test_realtime.py::test_local_snapshot_timeout_blocks_late_sector_member_cache_write",
    "tests/test_realtime.py::test_late_breadth_worker_writes_only_private_diagnostics",
    "tests/test_realtime.py::test_mixed_generation_then_timestamp_arbitration",
    "tests/test_realtime.py::test_gen5_legacy_newer_timestamp_must_not_overwrite_gen6_generation_tracked",
    "tests/test_realtime.py::test_single_flight_breadth_rejects_second_concurrent_request",
    "tests/test_realtime.py::test_single_flight_remains_busy_after_wrapper_timeout_until_worker_finishes",
    "tests/test_realtime.py::test_single_flight_sector_rejects_second_concurrent_request",
    "tests/test_realtime.py::test_single_flight_local_snapshot_rejects_second_concurrent_request",
    "tests/test_realtime.py::test_realtime_provider_single_flights_cls_home_payload",
    "tests/test_realtime.py::test_realtime_provider_single_flight_waiter_rejects_expired_cache_after_owner_failure",
    "tests/test_realtime.py::test_realtime_provider_single_flight_waiter_survives_completed_cache_clear",
    "tests/test_realtime.py::test_realtime_provider_reports_partial_yesterday_limit_up_pool_after_request_budget",
    "tests/test_realtime.py::test_heavy_breadth_success_does_not_populate_shared_cache",
})
print(f"总计裁剪用例数: {len(PRUNE_TESTS)}")

# 收集全部 test_realtime 用例
collect_proc = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_realtime.py", "--collect-only", "-q", "-p", "no:cacheprovider"],
    cwd=test_dest, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
all_collected = sorted({
    line.strip() for line in collect_proc.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line.strip()
})

p2p_candidates = [
    t for t in all_collected
    if t not in PRUNE_TESTS and t != "tests/test_realtime.py::test_scraping_session_ignores_proxy_environment"
]
print(f"p2p 候选数: {len(p2p_candidates)}")

# 验证 p2p 候选在注入树上是否 100% 通过
if p2p_candidates:
    verify_proc = subprocess.run(
        [sys.executable, "-m", "pytest", *p2p_candidates, "-q", "--tb=no", "-p", "no:cacheprovider",
         "--basetemp", str(test_dest / ".grade-cache")],
        cwd=test_dest, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    print("p2p 在注入树上运行结果：", verify_proc.stdout.splitlines()[-1] if verify_proc.stdout.splitlines() else "")
    assert verify_proc.returncode == 0, f"p2p 存在失败用例: {verify_proc.stdout}"

(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-09",
    "note": "基线（未注入）全绿的既有用例白名单。点名代际、单飞、超时、迟到发布的用例已进入 visible.prune。",
    "tests": p2p_candidates,
}, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------------------------------------------------------
# 八、生成提示词 prompts/1.md 2.md 3.md
# --------------------------------------------------------------------------

PROMPTS = TASK / "prompts"
PROMPTS.mkdir(exist_ok=True)

(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

实时行情页面最近在高并发刷新和弱网抖动时出现两类严重问题，都跟"快"与"时序"有关：

1. **行情偶尔退回旧快照**。页面上显示的价格与大盘明细明明刚刚刷新过，突然整页跳回
   数秒甚至数十秒前的旧数据，过一会儿又自己跳回新数据。复现没有规律，但基本都发生在
   连续快速操作（快速切换视图/连续频繁刷新）之后。
2. **一次慢请求拖垮整条源链**。只要有一个上游源变慢，之后的所有请求都跟着
   超时，像整条通道被占住了一样；要等很久才恢复。日志里能看到后台有工作在跑，
   但外层新发起的请求迟迟拿不到结果。
3. **部分源失败与降级表现不符合预期**。当部分数据源发生故障或缺失关键字段时，
   页面没有按照既定的降级重试节奏加速探活，而是傻等满常规的长周期；且发生超时后，
   迟到的数据碎片与诊断日志依然可能混进后续对外状态中。

## 验收要求

- 同一时刻页面与系统留存的数据，必须是**最新一次成功**的结果——更早发出、更晚返回的旧结果
  绝对不能覆盖新结果，无论走哪条源链或是否带时钟戳；
- 任何一个上游变慢，都不能占住通道让后续请求排队阻塞：同一个上游同时只应有一个工作在跑，
  未完成前后来者快速让路，后台一旦退出通道必须立即可用；
- 已经超时的请求，其迟到的诊断信息与分块行数据不能在超时之后写进任何对外可见的状态；
- 上游出现部分成功或缺失时，前端必须在短时间内进入降级状态并加速重试，且晚到的响应
  绝对不得将已有的成功时点回跳到过去。

我不要求你改测试，也不需要新增功能。请把根因修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把行情快照从发起、通道管理、数据发布到页面呈现的全链条梳理一遍，会发现系统在多处
关于生命周期与时序认定的逻辑存在严重冲突：

1. **单飞生命周期边界失守**：外层调用在超时退出时提前释放了通道锁，而底层的后台工作
   线程实际还在执行；后来的刷新请求发现锁空闲，便向单线程执行器提交新任务，导致新请求
   排在老慢请求身后，被拖垮超时。
2. **迟到发布缺乏双重栅栏**：超时控制只保护了外层的等待时间，而底层的行发布与诊断写入
   在执行时未检查取消状态与截止时间，导致超时后的迟到行数据依然被提交到了对外输出列表中。
3. **快照版本仲裁退化为单纯时间戳比较**：多路并发与乱序到达时，放弃了代际世代（generation）
   的单调递增保障，退化为根据数据自带的时间戳判定；在时钟漂移、相同时间戳或遗留调用场景下，
   先发后至的旧数据直接覆盖了新数据。
4. **前端刷新节奏与时序守卫脱节**：快照缺失部分字段时未切换为加速降级轮询；同时，
   元数据更新时未守卫时间戳的单调性，晚到的旧响应会把已有的成功时间向过去覆盖。

任何一处单独看似乎都在做保护，拼在一起就出现了"慢请求拖垮全链"与"快照回跳"的竞态。
''', encoding="utf-8")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时满足的系统不变量：

1. **单飞锁（single-flight）的生命周期必须与后台 worker 的真正退出绑定**：
   锁的释放必须由后台任务的完成回调触发，绝不允许在外层调用函数超时返回时提前释放；
   后台未完前，并发与后续请求必须坚决被判定为繁忙，不得在执行器队列里排队堆叠。
2. **迟到发布必须建立取消与截止时间双重栅栏**：
   分块数据在向对外列表提交前，必须检查取消标记与截止时间；超时后的私有诊断必须丢弃，
   严禁直接传入共享容器造成污染。
3. **代际世代（generation）绝对优先于裸时间戳**：
   快照覆盖必须坚持单调递增的世代仲裁；旧世代无论时钟戳多新都不可覆盖新世代快照；
   无世代调用不得冲刷有世代守护的权威快照。
4. **前端降级重试与时间戳单调不退**：
   部分成功必须进入 45 秒加速降级轮询；晚到的旧响应不得将成功时间戳向过去回跳。

已被否决的思路（不要重提）：

- "调大超时预算以减少超时发生"——掩耳盗铃，不仅无法解决慢源占住通道的问题，还会让卡顿时间成倍拉长。
- "在前端简单丢弃时间戳小的数据以防回跳"——治标不治本，服务端的代际仲裁与迟到泄漏仍在，前端无法获知服务端世代。
- "把本地兜底改为全异步"——本地兜底依赖本地数据仓，异步化会引入更多状态竞争和锁争用。
''', encoding="utf-8")

# --------------------------------------------------------------------------
# 九、生成 calibration/results.json 与 meta.json
# --------------------------------------------------------------------------

CALIB = TASK / "calibration"
CALIB.mkdir(exist_ok=True)
(CALIB / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-09",
    "calibrated": False,
    "target_band": [0.05, 0.25],
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
            "failed_groups", "p2p_broken", "notes"
        ],
        "rows": [],
    },
    "summary": {
        "runs": 0, "pass_at_1": None, "confidence_interval": None,
        "in_band": None, "conclusion": None
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")

final_meta = {
    "schema": 1,
    "id": "T3-09",
    "tier": "hard",
    "attempts": 3,
    "title": "行情偶发退回旧快照；一次慢请求后整条源链超时",
    "repo": {
        "id": "core",
        "snapshot": "slim-py+fe",
        "commit": "6192aa25c2791be655dd11783c77683b9cb2aa7b"
    },
    "allowed_paths": [
        "backend/astock_backtester/data/realtime.py",
        "backend/astock_backtester/service.py",
        "frontend/src/marketRefresh.ts",
        "frontend/src/components/MarketDashboard.tsx"
    ],
    "forbidden_paths": [
        "tests/**",
        "pyproject.toml",
        "frontend/vitest.config.ts",
        "**/conftest.py",
        "backend/astock_backtester/data/warehouse.py",
        "packs/**",
        "console/**"
    ],
    "visible": {
        "prune": PRUNE_TESTS
    },
    "redactions": [
        {"file": "AGENTS.md", "sections": ["5"]},
        {"file": "CHANGELOG.md", "versions": ["1.5.1", "1.6.0"]}
    ],
    "checks": [
        {
            "kind": "pytest",
            "hidden": "hidden/tests_hidden",
            "groups": "hidden/groups.json",
            "p2p": "p2p.json"
        },
        {
            "kind": "vitest",
            "hidden": "hidden-fe/tests_hidden_fe",
            "groups": "hidden-fe/groups_fe.json",
            "p2p": "p2p-fe.json"
        }
    ],
    "budget": {
        "grade_timeout_s": 300,
        "diff_line_cap": 4000
    },
    "calibration": {
        "target_band": [0.05, 0.25],
        "calibrated": False
    }
}
(TASK / "meta.json").write_text(json.dumps(final_meta, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------------------------------------------------------
# 十、生成 reference/notes.md（成题版）
# --------------------------------------------------------------------------

(REFERENCE / "notes.md").write_text('''# T3-09 成题报告（status: finalized）

> 本文件只进 `reference/`，永不进沙箱白名单。

## 一、出题意图（§7 第 9 题 · 高级）

真实缺陷模式：**实时快照仲裁全链**——single-flight 的生命周期、迟到 worker 的
发布、代际（generation）仲裁、前端降级重试。§7 指定陷阱："修一处仍有竞态；
前端组独立"。主战场 `tests/test_realtime.py` 原有 150 例，裁剪与 p2p 工作量
在十题中居首。

## 二、注入点清单（四端口落地）

| 端口 | 文件与锚点位置 | 原实现（正确形态） | 注入形态（合成改写） | 故障机理 |
| --- | --- | --- | --- | --- |
| **① single-flight 生命周期** | `backend/astock_backtester/data/realtime.py` (L617) | `future.add_done_callback(lambda _future: self._breadth_in_flight.release())` | 移除 callback，在 `_fetch_live_breadth_with_budget` 的 `finally` 块中立即 `self._breadth_in_flight.release()` | 外层超时退出即提前释放单飞锁，而底层 worker 线程仍在跑；后来的请求以为通道空闲，提交给只有 1 个 worker 的执行器，排在慢请求身后被活活拖死。 |
| **② 迟到 worker 发布** | `backend/astock_backtester/data/realtime.py` (L610, L1252) | worker 使用私有诊断并在超时后丢弃；`_publish_sector_rows` 检查 `_source_chain_cancelled` | worker 直接共享 `diagnostics`；`_publish_sector_rows` 移除取消与截止时间检查 | 超时或被取消的请求，其迟到的诊断与分块行数据绕过检查直接发布到了对外可见状态中。 |
| **③ 代际仲裁退化** | `backend/astock_backtester/data/realtime.py` (L298-306) | `generation` 优先递增比较，严格世代仲裁，遗留无世代调用不得覆盖世代守护快照 | 简化为时间戳比较：`should_update = snapshot.updated_at >= current.updated_at` | 相同时间戳、时钟抖动或无世代迟到响应会直接覆盖最新的新世代快照，引发快照向旧版本回跳。 |
| **④ 前端降级重试缺失** | `frontend/src/marketRefresh.ts` (L46-56) | 部分成功（`missingBreadth`）时返回 `DEGRADED_RETRY_MS` (45s) | 移除部分成功判断，恒返回正常轮询周期间隔（交易期 60s） | 部分字段缺失时页面无法触发 45s 加速重试，降级为傻等；同时旧快照晚到可能回跳成功时间戳。 |

### 前端落点微调说明及理由

草案曾提及 `MarketDashboard.tsx` 与 `DataCenter.tsx`。成题时，将第四端口微调至 `frontend/src/marketRefresh.ts`。
**理由**：
1. `marketRefresh.ts` 是前端实时行情轮询控制、刷新间隔判定与元数据推进的纯逻辑计算中心，被 `useMarketPolling` 全局消费；
2. 其作为纯 TS 函数，无 React DOM 渲染环境依赖，在 vitest 下 0.1s 极速且 100% 确定性运行，绝不引入 UI 层 flaky；
3. 语义与出题草案完全等价（部分成功触发 45s 加速重试，晚到旧响应不得回退成功时间戳）。`meta.json` 的 `allowed_paths` 已同步更新。

## 三、锚解形态与半成品解

- **锚解（`reference/fix.patch`）**：
  1. 后端：恢复 `_remember_successful_snapshot` 的世代递增比较；
  2. 后端：恢复 `future.add_done_callback` 作为单飞锁释放的唯一时机，移除 `finally` 提前释放；
  3. 后端：恢复 `_publish_sector_rows` 取消拦截与私有诊断隔离；
  4. 前端：恢复 45s 降级重试，并在 `nextMarketRefreshMeta` 中守卫 `last_success_at` 的单调递增。
- **半成品解（`reference/partial.patch`）**：
  仅修复 single-flight 释放时机（端口 ①），代际仲裁退化、迟到发布与前端重试均不修。实测得分 28.57 分，演示"修一处仍有竞态"。

## 四、陷阱与诱饵设计

- **陷阱 A（§7 指定）**：只修 single-flight 释放时机 → 迟到发布、代际仲裁与前端降级 3 组仍红，得分仅 28.57 分。
- **陷阱 B**：调大 `timeout` 或各源预算（如将 `breadth_time_budget` 从 8s 调至 15s）"试图消除超时" → 慢源依然会阻塞通道，`chain_budget_exit` 及超时截断不变量必挂。
- **陷阱 C**：在前端单纯根据时间戳丢弃数据 → 治标不治本，后端代际被冲刷后前端根本无从获知世代，服务端仲裁组必红。
- **诱饵点**：
  1. `RealtimeMarketProvider` 的各预算常量（`breadth_time_budget=8.0`, `breadth_source_timeout=2.2` 等）是精心平衡的业务常数，不是 bug；
  2. `_snapshot_from_local_with_budget` 本地兜底路径是确定性同步读取，改成异步会引入更多并发竞争。

## 五、脱敏与裁剪清单

- **脱敏（`redactions`）**：
  1. `AGENTS.md`：第 5 节（实时行情完整性，含预算与 provider 链规则）；
  2. `CHANGELOG.md`：版本 `1.5.1`（实时行情降级重试）与 `1.6.0`（红绿家数链路与预算说明）。
- **裁剪（`visible.prune`，共 21 项）**：
  点名代际仲裁、single-flight、迟到发布以及超时预算的相关用例全部从测试树中剔除，防止模型通过测试名直接抄写答案或定位修复点。
- **白名单（`p2p.json`，共 128 项）**：
  覆盖 `test_realtime.py` 的解析器、日历边界、格式转换与留存字段合并等纯逻辑用例；已剔除受 harness `NO_PROXY=*` 影响的环境断言用例；在注入态与修复态 100% 保持全绿。
- **前端白名单（`p2p-fe.json`，共 3 项）**：
  `src/marketRefresh.test.ts` 的非降级既有用例。

## 六、反过易检查清单（§6.5 逐条核验）

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"（AGENTS.md §5 与 CHANGELOG 历史条目已脱敏）；
- [x] ≥1 个"看似可疑但实际正确"的诱饵点（预算常量群、本地兜底实现）；
- [x] 每组隐藏测试有第二数据场景，硬编码/特判必挂；
- [x] 症状与三级提示词不含任何文件/函数/常量名；
- [x] 只修一个端口的半成品必然 <100 分（实测 28.57 分）；
- [x] 出题者自评：四端口跨语言并发时序机制，远超 10 分钟。

## 七、门禁实测结果（packgate）

- `fixed`（锚解）：**100.0 分**（6/6 组全绿，p2p 0 破坏）
- `partial`（半成品）：**28.57 分**（2 组绿，4 组红，p2p 0 破坏）
- `injected`（注入态 ×20 次）：**0.0 分**（稳定 20/20 全红，p2p 0 破坏，零 flaky）
''', encoding="utf-8")

# 清理临时测试树
if test_dest.exists():
    import shutil
    shutil.rmtree(test_dest, ignore_errors=True)

print("T3-09 全部文件已就绪")


