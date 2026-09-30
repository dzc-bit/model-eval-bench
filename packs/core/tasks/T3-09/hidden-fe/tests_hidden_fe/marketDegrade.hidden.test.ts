// T3-09 隐藏测试（前端侧）：降级轮询间隔与时钟/快照代际仲裁守卫。
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
