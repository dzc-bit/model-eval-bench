"""T1-02 隐藏测试：跨端交易日历一致性。

只断言"事实"层面的不变量，不点名实现位置：同一份休市安排被两端各自维护时，
重叠年份的回答必须一致；缺失年份的节日在哪一端都不能变成交易日。
"""

from pathlib import Path

import pandas as pd

from backend.astock_backtester.data.trading_calendar import (
    _A_SHARE_HOLIDAY_RANGES,
    a_share_trade_dates,
)

FRONTEND_CALENDAR = Path("frontend/src/tradingCalendar.ts")

# 两端在产品上都应当覆盖的"运营年份"：覆盖窗口与默认日期都落在这个范围内
OPERATIONAL_YEARS = [2024, 2025, 2026, 2027, 2028]


def _ranges_per_year(text: str) -> dict[int, list[tuple[str, str]]]:
    """从前端源码里解析出每年节假日区间（原文逐条比对，不做语义解释）。"""
    table = text.split("A_SHARE_HOLIDAY_RANGES", 1)[1]
    result: dict[int, list[tuple[str, str]]] = {}
    current: int | None = None
    for line in table.splitlines():
        head = line.strip()
        if head.startswith("};"):
            break
        year = re.match(r"^(\d{4}): \[$", head)
        if year:
            current = int(year.group(1))
            result.setdefault(current, [])
            continue
        pair = re.match(r'^\["(\d{4}-\d{2}-\d{2})", "(\d{4}-\d{2}-\d{2})"\],?$', head)
        if pair and current is not None:
            result[current].append((pair.group(1), pair.group(2)))
    return result


import re  # noqa: E402  放在解析函数之后便于阅读


def test_spring_festival_and_other_2026_holidays_are_not_trade_dates():
    """注入年份（2026）的全部假期段都必须被排除在交易日之外。"""
    for start, end in [
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ]:
        days = a_share_trade_dates(pd.Timestamp(start), pd.Timestamp(end))
        assert not days, f"{start}~{end} 是法定节假日，不应出现交易日：{sorted(days)}"


def test_adjacent_years_keep_their_own_holidays_and_regular_weeks():
    """邻近年份的假期必须原样保留，常规工作周也不得被误伤（防"删年了事"）。"""
    for start, end in [
        ("2024-10-01", "2024-10-07"),
        ("2025-01-28", "2025-02-04"),
        ("2027-02-05", "2027-02-13"),
        ("2028-01-25", "2028-02-02"),
    ]:
        days = a_share_trade_dates(pd.Timestamp(start), pd.Timestamp(end))
        assert not days, f"{start}~{end} 应为假期，却出现了交易日：{sorted(days)}"
    regular = a_share_trade_dates(pd.Timestamp("2026-02-09"), pd.Timestamp("2026-02-13"))
    assert len(regular) == 5, f"春节前的工作周应完整：{sorted(regular)}"


def test_both_ends_cover_the_operational_years():
    """两端都必须覆盖全部运营年份——缺哪一年，这一端就该红。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))
    missing_backend = [y for y in OPERATIONAL_YEARS if y not in _A_SHARE_HOLIDAY_RANGES]
    missing_frontend = [y for y in OPERATIONAL_YEARS if y not in fe]
    assert not missing_backend, f"后端节日表缺年份：{missing_backend}"
    assert not missing_frontend, f"前端节日表缺年份：{missing_frontend}"


def test_both_ends_agree_on_every_operational_year():
    """重叠年份的节假日区间必须逐条一致（这是"收口"的最低要求）。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))
    for year in OPERATIONAL_YEARS:
        backend = sorted(_A_SHARE_HOLIDAY_RANGES.get(year, ()))
        frontend = sorted(fe.get(year, []))
        assert backend == frontend, (
            f"{year} 年两端节假日不一致：\n  仅后端有 {sorted(set(backend) - set(frontend))}"
            f"\n  仅前端有 {sorted(set(frontend) - set(backend))}"
        )


def test_trade_dates_and_frontend_table_agree_on_sampled_days():
    """抽样日上，后端交易日序列与前端表的回答必须同真同假。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))

    def frontend_says_holiday(day: str) -> bool:
        year = int(day[:4])
        return any(start <= day <= end for start, end in fe.get(year, []))

    samples = [
        "2024-02-12", "2024-10-02", "2025-01-29", "2025-10-02",
        "2026-02-16", "2026-02-18", "2026-10-02", "2027-02-08", "2028-01-26",
        "2026-02-11", "2027-03-15", "2025-06-05",
    ]
    for day in samples:
        stamp = pd.Timestamp(day)
        backend_says_holiday = stamp not in a_share_trade_dates(stamp, stamp)
        assert backend_says_holiday == frontend_says_holiday(day), (
            f"{day}：后端{'休市' if backend_says_holiday else '交易'}，"
            f"前端{'休市' if frontend_says_holiday(day) else '交易'}——两端口径分叉"
        )
