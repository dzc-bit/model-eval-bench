// T1-02 隐藏测试（页面侧）：默认窗口落点与节假日判定。
// 只依赖本模块导出的行为，不关心实现放在哪个文件。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isAShareTradingDay,
  recentAShareTradingDateRange,
  recentAShareTradingDateRangeEnding
} from "../tradingCalendar";

const day = (text: string) => new Date(`${text}T00:00:00`);

afterEach(() => {
  vi.useRealTimers();
});

describe("页面侧交易日判定", () => {
  it("把注入缺口年份与原生缺口年份的假期都判为休市", () => {
    expect(isAShareTradingDay(day("2025-01-29"))).toBe(false);
    expect(isAShareTradingDay(day("2025-10-02"))).toBe(false);
    expect(isAShareTradingDay(day("2026-02-17"))).toBe(false);
  });

  it("不误伤周末之外的真实交易日", () => {
    expect(isAShareTradingDay(day("2024-02-12"))).toBe(false);
    expect(isAShareTradingDay(day("2026-02-11"))).toBe(true);
    expect(isAShareTradingDay(day("2025-06-05"))).toBe(true);
  });
});

describe("默认补数窗口", () => {
  it("末端是节假日时先回退到最近交易日", () => {
    vi.useFakeTimers();
    vi.setSystemTime(day("2026-10-01"));
    const range = recentAShareTradingDateRange();
    expect(range.endDate).toBe("2026-09-30");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
  });

  it("周末当口同样回退，且不重复回退", () => {
    vi.useFakeTimers();
    vi.setSystemTime(day("2024-06-08"));
    const range = recentAShareTradingDateRange();
    expect(range.endDate).toBe("2024-06-07");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
  });

  it("给定末端落在假期内时，末端与起点都必须是交易日", () => {
    const range = recentAShareTradingDateRangeEnding("2026-02-21");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
    expect(range.endDate <= "2026-02-21").toBe(true);
  });

  it("跨年默认窗口的两端同样落在交易日上", () => {
    const range = recentAShareTradingDateRangeEnding("2027-01-02");
    expect(range.endDate).toBe("2026-12-31");
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
    expect(range.startDate.startsWith("2026-")).toBe(true);
  });
});
