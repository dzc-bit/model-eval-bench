"""T2-04 隐藏测试：缺口在四个出口上必须得到同一个答案。

四条出口（覆盖汇总、逐股覆盖、同步补齐名单、资金流缺口名单）回答的是同一个
问题——**某一天的某只股票缺行，到底是可行动缺口，还是停牌类的天然缺口**。
答案取决于"那一天算不算市场正常日"，而这个判定是横截面统计出来的。

设计要点（对齐 §5.1 / §6.5）：

* 只断言**外部可观察的行为**：拿到的是覆盖汇总的计数、逐股覆盖的缺失日清单、
  补齐名单的成员、资金流缺口名单的成员。不约束模型必须抽共享函数、也不约束
  常量叫什么、住哪个模块。
* 每组至少两个**数据场景**，且两组场景的形状不同：长窗口（横截面有足够样本）
  与短窗口（样本不足）。任何"只按长窗口调参"或"只按短窗口调参"的特判都会漏。
* 陷阱点刻意留在场景里：一批**当天全市场都没有行**的交易日。它们是横截面的
  噪声，不是证据——把噪声算进中位数会压低判定门槛，让"只有一只票写入"的那天
  被误判成市场正常日，那天的缺行于是被当成停牌类从补齐名单里悄悄抹掉。
"""

from __future__ import annotations

import pandas as pd

from astock_backtester.data.cache import LocalCache
from astock_backtester.data.operations import build_daily_bars_coverage
from astock_backtester.data.sync import SyncJobManager
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse

# 五只票：够撑起横截面，又不会多到把中位数算散。
SYMBOLS = [f"60000{index}" for index in range(5)]
PROBE = SYMBOLS[0]


def _days(start: str, end: str) -> list[pd.Timestamp]:
    # 交易日历的返回顺序不保证有序，先排。
    return sorted(pd.Timestamp(day) for day in a_share_trade_dates(start, end))


def _bar(symbol: str, day: pd.Timestamp) -> dict:
    return {
        "symbol": symbol,
        "trade_date": day.date().isoformat(),
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 1000,
        "float_market_cap": 1_000_000.0,
        "total_market_cap": 1_200_000.0,
        "main_net_inflow": 1.0,
    }


def _write(root, layout: dict[str, list[pd.Timestamp]]) -> Warehouse:
    warehouse = Warehouse(root)
    rows = [_bar(symbol, day) for symbol, days in layout.items() for day in days]
    warehouse.write_daily_bars(pd.DataFrame(rows))
    return warehouse


# ---------------------------------------------------------------------------
# 场景夹具
# ---------------------------------------------------------------------------


def _long_window_with_a_thin_day(tmp_path) -> tuple[Warehouse, list[pd.Timestamp], pd.Timestamp]:
    """23 个交易日的窗口，中间 15 天全市场零行，其中一天只写进来 2 只票。

    这一天是本题的核心：它的横截面证据很弱（2 行 vs 常态 5 行），但它**有**行，
    所以它是横截面的合法输入；而那 15 个零行日不是——它们没有证据。

    返回 (仓, 全部交易日, 那一天)。
    """
    calendar = _days("2026-07-01", "2026-07-31")
    assert len(calendar) > 20
    healthy = [calendar[index] for index in (0, 1, 2, 3, 4, 21, 22)]
    weak_day = calendar[5]

    layout = {symbol: list(healthy) for symbol in SYMBOLS}
    layout[SYMBOLS[3]] = [*healthy, weak_day]
    layout[SYMBOLS[4]] = [*healthy, weak_day]

    return _write(tmp_path, layout), calendar, weak_day


def _long_window_with_a_normal_day_gap(tmp_path) -> tuple[Warehouse, list[pd.Timestamp], pd.Timestamp]:
    """23 个交易日的窗口，每个交易日全市场满行，只有探针标的缺其中一天。

    那天是货真价实的市场正常日：其余四只票都在，缺的那一行只能解释成停牌，
    公开渠道天然没有这种数据，补也补不出来。
    """
    calendar = _days("2026-07-01", "2026-07-31")
    normal_day = calendar[10]
    layout = {symbol: list(calendar) for symbol in SYMBOLS}
    layout[PROBE] = [day for day in calendar if day != normal_day]
    return _write(tmp_path, layout), calendar, normal_day


def _five_day_window_with_a_normal_day_gap(tmp_path) -> tuple[Warehouse, list[pd.Timestamp], pd.Timestamp]:
    """恰好 5 个交易日：探针标的缺中间一天，其余标的当天均有行。

    这是分类下限的边界场景：4 天窗口仍不分类，5 天窗口应开始分类。
    """
    calendar = _days("2026-07-01", "2026-07-07")
    assert len(calendar) == 5
    normal_day = calendar[2]
    layout = {symbol: list(calendar) for symbol in SYMBOLS}
    layout[PROBE] = [day for day in calendar if day != normal_day]
    return _write(tmp_path, layout), calendar, normal_day


def _short_window(tmp_path, end: str) -> tuple[Warehouse, list[pd.Timestamp], pd.Timestamp]:
    """只有 3 个或 4 个交易日的窗口，只有探针标的缺中间那一天。

    两个刻意的选择：

    * **缺的是中间那天，不是最后一天。** 缺最后一天属于"停更尾部"，按设计永远
      可见、不参与豁免，测不出判定口径。
    * **3 天与 4 天各来一次。** 这种窗口的横截面中位数没有统计意义——三五天
      分不出"常态水位"和"异常低"。把下限调到 2~4 之间的做法能蒙对其中一条、
      在另一条上翻车；调到 1 则两条都翻车。
    """
    calendar = _days("2026-07-01", end)
    assert 3 <= len(calendar) <= 4
    gap_day = calendar[1]
    layout = {symbol: list(calendar) for symbol in SYMBOLS}
    layout[PROBE] = [day for day in calendar if day != gap_day]
    return _write(tmp_path, layout), calendar, gap_day


# ---------------------------------------------------------------------------
# 出口读取
# ---------------------------------------------------------------------------


def _daily_coverage(warehouse: Warehouse):
    for item in warehouse.coverage():
        if item.dataset == "daily_bars":
            return item
    raise AssertionError("覆盖汇总里没有日线数据集")


def _per_symbol_missing(warehouse: Warehouse, start: str, end: str, symbol: str) -> set[pd.Timestamp]:
    details = build_daily_bars_coverage(
        cache=LocalCache(warehouse.cache_root),
        warehouse=warehouse,
        symbols=[symbol],
        start_date=start,
        end_date=end,
    )
    return {pd.Timestamp(day) for day in details.items[0].missing_trade_dates}


def _sync_incomplete(warehouse: Warehouse, start: str, end: str) -> set[str]:
    return set(SyncJobManager(warehouse=warehouse, provider=None).incomplete_symbols(start, end))


def _flow_missing(warehouse: Warehouse, start: str, end: str) -> set[str]:
    return set(warehouse.read_capital_flow_missing_symbols(start, end))


# ---------------------------------------------------------------------------
# 组 1 · 覆盖汇总：内部洞的分类计数
# ---------------------------------------------------------------------------


def test_weak_cross_section_day_keeps_its_gaps_actionable_in_the_coverage_summary(tmp_path):
    """只有两只票写进来的那天，缺的三行是可行动缺口，不是停牌类。

    长窗口里另有 15 个全市场零行的交易日。零行日不是横截面证据：把它们算进
    中位数，判定门槛会塌到 1，于是"只写进来 2 只票"的那天摇身一变成了市场正常日，
    它那三行缺口被记进停牌类——补齐任务再也不会去补它，而且没有任何报错。
    """
    warehouse, calendar, weak_day = _long_window_with_a_thin_day(tmp_path)
    healthy = [calendar[index] for index in (0, 1, 2, 3, 4, 21, 22)]
    empty_days = [day for day in calendar if day not in healthy and day != weak_day]
    assert len(empty_days) > len(healthy), "零行日必须多于有行的日，门槛才会被拖下来"

    summary = _daily_coverage(warehouse)

    # 15 个零行日 × 5 只在市票 = 75，加上弱证据日的 3 行
    assert summary.suspension_rows == 0, "弱证据日的缺口被误记成停牌类"
    assert summary.missing_rows == len(empty_days) * len(SYMBOLS) + (len(SYMBOLS) - 2)


def test_full_cross_section_day_keeps_its_gap_as_a_suspension(tmp_path):
    """对照组：那天其余四只票都在，缺口只能解释成停牌，必须记停牌类。

    与上一条互为对照：只有"全记可行动"和"全记停牌"两种极端都不成立，才说明
    判定真的是按当天的横截面证据来的，而不是一刀切。
    """
    warehouse, _calendar, _normal_day = _long_window_with_a_normal_day_gap(tmp_path)

    summary = _daily_coverage(warehouse)

    assert summary.suspension_rows == 1
    assert summary.missing_rows == 0


# ---------------------------------------------------------------------------
# 组 2 · 逐股覆盖：某只票的缺失日清单
# ---------------------------------------------------------------------------


def test_per_symbol_keeps_the_weak_cross_section_day_in_the_missing_list(tmp_path):
    """长窗口：弱证据日必须留在探针标的的缺失日清单里。"""
    warehouse, _calendar, weak_day = _long_window_with_a_thin_day(tmp_path)

    missing = _per_symbol_missing(warehouse, "2026-07-01", "2026-07-31", PROBE)

    assert weak_day in missing, "弱证据日的缺口被从逐股缺失清单里抹掉了"


def test_per_symbol_drops_full_cross_section_suspension_day_from_missing_list(tmp_path):
    """长窗口：满行市场正常日上的缺口应从逐股可行动缺失清单中剔除。"""
    warehouse, _calendar, normal_day = _long_window_with_a_normal_day_gap(tmp_path)

    missing = _per_symbol_missing(warehouse, "2026-07-01", "2026-07-31", PROBE)

    assert normal_day not in missing, "满行日上的停牌类缺口仍被列为可行动缺失"


def test_per_symbol_keeps_the_gap_on_a_three_day_window(tmp_path):
    """短窗口对照组之一：只有 3 个交易日时不做横截面判定，缺口照算可行动。"""
    warehouse, _calendar, gap_day = _short_window(tmp_path, "2026-07-03")

    missing = _per_symbol_missing(warehouse, "2026-07-01", "2026-07-03", PROBE)

    assert missing == {gap_day}


def test_per_symbol_keeps_the_gap_on_a_four_day_window(tmp_path):
    """短窗口对照组之二：4 个交易日同样不够——3 天和 4 天必须给出同一个答案。"""
    warehouse, calendar, gap_day = _short_window(tmp_path, "2026-07-06")
    assert len(calendar) == 4

    missing = _per_symbol_missing(warehouse, "2026-07-01", "2026-07-06", PROBE)

    assert missing == {gap_day}


# ---------------------------------------------------------------------------
# 组 3 · 同步补齐名单：谁还有活要干
# ---------------------------------------------------------------------------


def test_sync_keeps_the_symbol_on_a_three_day_window(tmp_path):
    """短窗口（3 天）：探针标的缺一天，必须留在补齐名单里。"""
    warehouse, _calendar, _gap_day = _short_window(tmp_path, "2026-07-03")

    assert PROBE in _sync_incomplete(warehouse, "2026-07-01", "2026-07-03")


def test_sync_keeps_the_symbol_on_a_four_day_window(tmp_path):
    """短窗口（4 天）：结论必须与 3 天时一致。

    补齐名单是"只对真正有缺口的票发起抓取"的入口，把一只还有活要干的票从这里
    拿掉，等于让它永远补不上——而且因为它已经不在名单里，没人会再来看它。
    """
    warehouse, _calendar, _gap_day = _short_window(tmp_path, "2026-07-06")

    assert PROBE in _sync_incomplete(warehouse, "2026-07-01", "2026-07-06")


def test_sync_drops_the_symbol_when_the_gap_is_a_real_suspension(tmp_path):
    """长窗口对照组：缺口落在满行的市场正常日上 → 补不出来，不该占补齐名额。"""
    warehouse, _calendar, _normal_day = _long_window_with_a_normal_day_gap(tmp_path)

    assert PROBE not in _sync_incomplete(warehouse, "2026-07-01", "2026-07-31")


# ---------------------------------------------------------------------------
# 组 4 · 资金流缺口名单
# ---------------------------------------------------------------------------


def test_flow_keeps_the_symbol_on_a_three_day_window(tmp_path):
    """短窗口（3 天）：资金流缺口名单必须仍然报出探针标的。"""
    warehouse, _calendar, _gap_day = _short_window(tmp_path, "2026-07-03")

    assert PROBE in _flow_missing(warehouse, "2026-07-01", "2026-07-03")


def test_flow_keeps_the_symbol_on_a_four_day_window(tmp_path):
    """短窗口（4 天）：与 3 天一致。

    资金流缺口名单决定要不要为一只票重抓 120 天的资金流。误豁免的代价是
    这只票的资金流从此长期缺一段，而名单已经不再提醒任何人。
    """
    warehouse, _calendar, _gap_day = _short_window(tmp_path, "2026-07-06")

    assert PROBE in _flow_missing(warehouse, "2026-07-01", "2026-07-06")


def test_flow_drops_the_symbol_when_the_gap_is_a_real_suspension(tmp_path):
    """长窗口对照组：满行市场正常日上的缺口是停牌类，资金流侧同样豁免。"""
    warehouse, _calendar, _normal_day = _long_window_with_a_normal_day_gap(tmp_path)

    assert PROBE not in _flow_missing(warehouse, "2026-07-01", "2026-07-31")


# ---------------------------------------------------------------------------
# 组 5 · 跨出口一致性：同一天、同一个事实、四个出口、只能有一个答案
# ---------------------------------------------------------------------------


def test_all_four_exits_agree_on_the_weak_cross_section_day(tmp_path):
    """四个出口必须对"弱证据日的那行缺口算不算可行动"给出同一个答案。"""
    warehouse, calendar, weak_day = _long_window_with_a_thin_day(tmp_path)
    healthy = [calendar[index] for index in (0, 1, 2, 3, 4, 21, 22)]
    empty_days = [day for day in calendar if day not in healthy and day != weak_day]

    summary = _daily_coverage(warehouse)
    coverage_says_actionable = (
        summary.suspension_rows == 0
        and summary.missing_rows == len(empty_days) * len(SYMBOLS) + (len(SYMBOLS) - 2)
    )
    per_symbol_says_actionable = weak_day in _per_symbol_missing(warehouse, "2026-07-01", "2026-07-31", PROBE)
    sync_says_actionable = PROBE in _sync_incomplete(warehouse, "2026-07-01", "2026-07-31")
    flow_says_actionable = PROBE in _flow_missing(warehouse, "2026-07-01", "2026-07-31")

    assert coverage_says_actionable, "覆盖汇总把弱证据日当成了停牌类"
    assert per_symbol_says_actionable, "逐股覆盖把弱证据日从缺失清单里抹掉了"
    assert sync_says_actionable, "补齐名单不再包含这只票"
    assert flow_says_actionable, "资金流缺口名单不再报出这只票"
    assert len(
        {coverage_says_actionable, per_symbol_says_actionable, sync_says_actionable, flow_says_actionable}
    ) == 1


def test_all_four_exits_agree_on_a_full_cross_section_suspension(tmp_path):
    """对照组：满行市场正常日上的缺口，四个出口必须一致地判成"不可行动"。"""
    warehouse, _calendar, normal_day = _long_window_with_a_normal_day_gap(tmp_path)

    summary = _daily_coverage(warehouse)
    coverage_says_actionable = summary.suspension_rows == 0 and summary.missing_rows == 0
    per_symbol_says_actionable = normal_day in _per_symbol_missing(
        warehouse, "2026-07-01", "2026-07-31", PROBE
    )
    sync_says_actionable = PROBE in _sync_incomplete(warehouse, "2026-07-01", "2026-07-31")
    flow_says_actionable = PROBE in _flow_missing(warehouse, "2026-07-01", "2026-07-31")

    assert not coverage_says_actionable, "覆盖汇总没把停牌类缺口记进停牌类"
    assert not per_symbol_says_actionable, "逐股覆盖仍把停牌类缺口列成可行动"
    assert not sync_says_actionable, "补齐名单把补不出来的票留在了名单里"
    assert not flow_says_actionable, "资金流缺口名单豁免规则没有与另外三个出口对齐"
    assert len(
        {coverage_says_actionable, per_symbol_says_actionable, sync_says_actionable, flow_says_actionable}
    ) == 1


def test_all_four_exits_agree_at_the_five_day_window_boundary(tmp_path):
    """恰好 5 个交易日时，四个出口都应开始把满行日缺口判为停牌类。"""
    warehouse, calendar, normal_day = _five_day_window_with_a_normal_day_gap(tmp_path)
    assert len(calendar) == 5

    summary = _daily_coverage(warehouse)
    coverage_says_actionable = summary.suspension_rows == 0 and summary.missing_rows > 0
    assert summary.suspension_rows == 1
    assert summary.missing_rows == 0
    per_symbol_says_actionable = normal_day in _per_symbol_missing(
        warehouse, "2026-07-01", "2026-07-07", PROBE
    )
    sync_says_actionable = PROBE in _sync_incomplete(warehouse, "2026-07-01", "2026-07-07")
    flow_says_actionable = PROBE in _flow_missing(warehouse, "2026-07-01", "2026-07-07")

    assert not coverage_says_actionable, "5 日边界的覆盖汇总仍把停牌类缺口算作可行动"
    assert not per_symbol_says_actionable, "5 日边界的逐股清单仍包含停牌类缺口"
    assert not sync_says_actionable, "5 日边界的补齐名单仍包含这只票"
    assert not flow_says_actionable, "5 日边界的资金流缺口名单仍包含这只票"
    assert len(
        {coverage_says_actionable, per_symbol_says_actionable, sync_says_actionable, flow_says_actionable}
    ) == 1
