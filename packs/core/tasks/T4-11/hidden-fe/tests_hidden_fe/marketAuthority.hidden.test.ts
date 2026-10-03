// T4-11 隐藏测试（前端侧）：行情模块的刷新节奏与"最近成功时点"的单调性。
//
// 只从公开出口断言可观测行为：
//   · 部分成功（缺红绿家数）必须缩短重试间隔，全源失败必须走失败退避；
//   · 最近成功时点只进不退——迟到的旧快照、部分成功与不可用都不得让它回跳，
//     也不得把一次还没成功的刷新冒充成成功。
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

function meta(overrides: Partial<MarketRefreshMeta> = {}): MarketRefreshMeta {
  return {
    phase: "trading",
    status: "idle",
    message: "实时行情已更新",
    next_refresh_ms: 60_000,
    ...overrides
  };
}

function snapshot(overrides: Partial<RealtimeMarketSnapshot> = {}): RealtimeMarketSnapshot {
  return {
    status: "live",
    source: "ashare-sina",
    updated_at: "2026-06-05T10:00:00+08:00",
    market_phase: "trading",
    indexes: [],
    breadth: { up: 3000, down: 1800, flat: 200, total: 5000, source: "test" },
    strong_sectors: [],
    yesterday_strong_sectors: [],
    message: "实时行情已更新",
    ...overrides
  };
}

describe("行情模块刷新节奏", () => {
  it("部分成功（缺红绿家数）缩短重试间隔", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-05T10:00:00+08:00"));

    const next = nextMarketRefreshMeta(
      meta(),
      snapshot({ breadth: null, message: "红绿家数缺失" }),
      "trading"
    );

    expect(next.next_refresh_ms).toBe(DEGRADED_RETRY_MS);
    expect(next.next_refresh_ms).toBe(45_000);
  });

  it("全源失败走失败退避，且不再叠加降级间隔", () => {
    const failed = refreshIntervalForMarketResult("trading", ["所有数据源均超时"], true, false);
    expect(failed).toBe(refreshIntervalForPhase("trading", true));
    expect(failed).toBe(120_000);

    // 失败与缺字段同时成立时，退避间隔必须是失败那一档（更长的那一档）。
    const both = refreshIntervalForMarketResult("trading", ["所有数据源均超时"], true, true);
    expect(both).toBe(120_000);
  });

  it("健康的完整快照保持标准节奏，不因后台刷新被拖慢", () => {
    const next = nextMarketRefreshMeta(
      meta(),
      snapshot({ updated_at: "2026-06-05T10:05:00+08:00" }),
      "trading"
    );
    expect(next.next_refresh_ms).toBe(60_000);
    expect(next.status).toBe("idle");
  });
});

describe("最近成功时点的单调性", () => {
  it("迟到的旧快照不得让成功时点回跳", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-05T10:06:00+08:00"));

    const next = nextMarketRefreshMeta(
      meta({ last_success_at: "2026-06-05T10:05:00+08:00" }),
      snapshot({ updated_at: "2026-06-05T10:00:00+08:00", source: "late-worker" }),
      "trading"
    );

    expect(next.last_success_at).toBe("2026-06-05T10:05:00+08:00");
  });

  it("不可用与部分成功都不得推进成功时点", () => {
    const held = meta({ last_success_at: "2026-06-05T10:05:00+08:00" });

    const unavailable = nextMarketRefreshMeta(
      held,
      snapshot({ status: "unavailable", updated_at: "2026-06-05T10:20:00+08:00" }),
      "trading"
    );
    expect(unavailable.last_success_at).toBe("2026-06-05T10:05:00+08:00");

    const partial = nextMarketRefreshMeta(
      held,
      snapshot({ updated_at: "2026-06-05T10:20:00+08:00", breadth: null }),
      "trading",
      true
    );
    expect(partial.last_success_at).toBe("2026-06-05T10:05:00+08:00");
  });

  it("更新的成功快照推进成功时点，且首次成功从空值建立", () => {
    const advanced = nextMarketRefreshMeta(
      meta({ last_success_at: "2026-06-05T10:05:00+08:00" }),
      snapshot({ updated_at: "2026-06-05T10:07:00+08:00" }),
      "trading"
    );
    expect(advanced.last_success_at).toBe("2026-06-05T10:07:00+08:00");

    const first = nextMarketRefreshMeta(
      meta(),
      snapshot({ updated_at: "2026-06-05T10:07:00+08:00" }),
      "trading"
    );
    expect(first.last_success_at).toBe("2026-06-05T10:07:00+08:00");
  });
});
