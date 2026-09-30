"""T1-01 隐藏测试：换手率 / 流通市值 / 涨跌停 三处派生口径。

设计要点（对齐 §5.1 / §6.5）：

* 只断言**外部可观察的行为**，不约束模型必须把共享口径抽在哪个模块、叫什么名字。
  只要各出口对同一事实给出同一结论就算通过。
* 每个分组至少两个**数据场景**（不同的取值集合 / 不同的标的 / 不同的阈值带），
  硬编码某一组常量或对输入做特判的解法必然在第二个场景上挂掉。
* 场景数据刻意跨过"量纲分界"（0 < v ≤ 1 与 v > 1 两侧都有取值），
  任何"按数值大小猜量纲"的实现都会在其中一侧翻车。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from astock_backtester import engine
from astock_backtester.conditions import MASK_BUILDERS, evaluate_condition
from astock_backtester.data.astock_adapter import HttpAStockFetcher
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.engine import run_backtest
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)

# ---------------------------------------------------------------------------
# 桩：公开日 K + 腾讯报价
# ---------------------------------------------------------------------------

_QUOTE_FIELDS = 53


def _quote_line(name: str, price: float, float_mcap_yi: float) -> str:
    fields = ["0"] * _QUOTE_FIELDS
    fields[1] = name
    fields[3] = f"{price:.2f}"
    fields[44] = f"{float_mcap_yi:.2f}"
    return 'v_sh600519="' + "~".join(fields) + '"'


def _tencent_day_payload(rows: list[list[str]]) -> dict:
    return {"data": {"sh600519": {"day": rows}}}


# 一天成交量 1000 手（入库 ×100 = 100_000 股），流通市值 1 亿元、现价 10.00
# → 流通股 1000 万股 → 换手率 100_000 / 10_000_000 × 100 = 1.0（百分数）。
_ONE_DAY_ROW = ["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]


def _public_fetcher(day_rows, quote: str) -> HttpAStockFetcher:
    def fake_public_json_get(url, params, headers, timeout):
        return _tencent_day_payload(day_rows)

    def fake_public_text_get(url, headers, timeout):
        return quote

    return HttpAStockFetcher(
        public_json_get=fake_public_json_get,
        public_text_get=fake_public_text_get,
    )


def _baidu_fetcher(market_data: str, quote: str) -> HttpAStockFetcher:
    keys = ["time", "open", "close", "high", "low", "volume", "amount", "range", "ratio", "turnoverratio", "preClose"]

    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {"Result": {"newMarketData": {"keys": keys, "marketData": market_data}}}
        raise OSError("报价源不可达")

    def fake_public_text_get(url, headers, timeout):
        return quote

    return HttpAStockFetcher(json_get=fake_json_get, public_text_get=fake_public_text_get)


# ---------------------------------------------------------------------------
# 组 1 · 换手率这一列的出口：未知必须还是未知
# ---------------------------------------------------------------------------


def test_unknown_turnover_stays_unknown_when_float_shares_are_not_derivable():
    """报价里没有市值也没有现价 → 流通股推不出来 → 换手率必须留空。

    0 是合法换手率。一旦把"推不出来"写成 0，条件与打分都无法把它和"当日真的没换手"
    区分开，而且不会有任何报错。
    """
    fetcher = _public_fetcher(
        [_ONE_DAY_ROW],
        _quote_line("示例股份", 0.0, 0.0),
    )

    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    assert len(result) == 1
    assert pd.isna(result.loc[0, "turnover_rate"]), "推不出流通股时换手率必须留空，不能是 0"


def test_turnover_column_is_percentage_scale_across_a_multi_day_window():
    """能推出流通股时逐行补换手率，量纲是百分数（2% 写成 2.0，不是 0.02）。

    两天分别取 1% 与 4%：任何"按数值大小猜量纲"的实现都会在 1% 那一行翻车。
    """
    two_days = [
        ["2026-09-23", "10.00", "10.50", "11.00", "9.80", "1000"],
        ["2026-09-24", "10.00", "10.50", "11.00", "9.80", "4000"],
    ]
    fetcher = _public_fetcher(two_days, _quote_line("示例股份", 10.00, 1.00))

    result = fetcher.fetch_daily_bars(["600519"], "2026-09-23", "2026-09-24")

    assert result["turnover_rate"].tolist() == [1.0, 4.0]


def test_frame_without_turnover_column_normalizes_to_unknown_not_zero():
    """从外部文件（CSV / 第三方帧）进来的数据没有换手率列时，也必须留空。

    采集侧已经用"留空"表达未知；归一化侧若在这一步填 0，同一只股票会因为走了
    不同入口而在两处得到不同的换手率。
    """
    frame = pd.DataFrame(
        [
            {
                "symbol": "600519",
                "trade_date": "2024-01-02",
                "open": 10.0,
                "high": 11.0,
                "low": 9.8,
                "close": 10.5,
                "volume": 1000,
            },
            {
                "symbol": "600001",
                "trade_date": "2024-01-02",
                "open": 8.0,
                "high": 8.4,
                "low": 7.9,
                "close": 8.2,
                "volume": 2500,
            },
        ]
    )

    out = normalize_daily_bars(frame)

    assert out["turnover_rate"].isna().all(), "缺列的换手率必须留空（未知），不能补 0"


# ---------------------------------------------------------------------------
# 组 2 · 流通市值这一列的出口：报价只补空缺，不得改写逐日值
# ---------------------------------------------------------------------------


def test_quote_top_up_preserves_the_per_date_market_cap_the_source_channel_computed():
    """带 turnoverratio 的通道已经按"当日股本"算出了流通市值，报价不得改写它。

    报价给的是**今天**的流通股本；拿它回填历史行，等于把后来的增发/解禁算进更早的
    市值里。数字看上去仍然是合理量级，不会有任何报错。
    """
    # 2024-01-02：换手率 2%、量 1000 手、收 10.5 → 该通道算出的流通市值 525_000
    market_data = "2024-01-02,10,10.5,11,9.8,1000,10500,+0.5,+5.0,2.0,10"
    # 报价：现价 10.00、流通市值 8 亿元 → 流通股 8000 万股，派生值 8.4 亿
    fetcher = _baidu_fetcher(market_data, _quote_line("示例股份", 10.00, 8.00))

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert result.loc[0, "float_market_cap"] == pytest.approx(525_000.0)


def test_quote_top_up_fills_only_the_rows_that_have_no_market_cap():
    """同一只股票两天：一天有逐日市值、一天没有 → 有值的那天保持原值。

    这条把"只补空缺"和"整窗覆盖"两种做法拉开：整窗覆盖会让第一天的值变成派生值。
    """
    # 第一天有 turnoverratio=2.0（逐日市值 525_000）；第二天 turnoverratio=0（无值），
    # 只能由报价补：8 亿 / 10.00 = 8000 万股 × 当日收盘 10.6 = 848_000_000
    market_data = (
        "2024-01-02,10,10.5,11,9.8,1000,10500,+0.5,+5.0,2.0,10;"
        "2024-01-03,10,10.6,11,9.9,1000,10600,+1.0,+9.5,0,10.5"
    )
    fetcher = _baidu_fetcher(market_data, _quote_line("示例股份", 10.00, 8.00))

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-03")

    assert result["float_market_cap"].tolist() == pytest.approx([525_000.0, 848_000_000.0])


# ---------------------------------------------------------------------------
# 组 3 / 组 4 · 条件注册表的两条实现：行级与向量化
# ---------------------------------------------------------------------------


def _node(minimum: float, maximum: float) -> ConditionNode:
    return ConditionNode(
        id="turnover",
        condition_id="turnover_between",
        params={"min": minimum, "max": maximum},
    )


def _expected_percent_scale(values: list[float], minimum: float, maximum: float) -> list[bool]:
    """用户口语的换手率区间（分数）对照仓库的百分数列。"""
    return [bool(minimum <= value / 100.0 <= maximum) for value in values]


def test_row_evaluator_reads_low_turnover_band_as_percentage():
    """行级判定：0.2%~0.8% 的低换手策略必须能筛出 0.5% 与 0.3% 的股票。

    低换手区间的取值（0.05 ~ 1.0）与分数区间高度重合，正是"按数值大小猜量纲"
    最容易翻车的地方。
    """
    values = [0.5, 0.05, 0.3, 9.0, 0.8, 0.2]
    frame = pd.DataFrame({"turnover_rate": values})
    node = _node(0.002, 0.008)

    actual = [evaluate_condition(node, pd.Series({"turnover_rate": value}), frame).passed for value in values]

    assert actual == _expected_percent_scale(values, 0.002, 0.008)


def test_row_evaluator_reads_wide_band_as_percentage():
    """行级判定的第二个数据场景：1%~20% 的宽区间，12% 必须判不通过。

    与上一条刻意用了不同的阈值带与不同的取值集合：任何对某组常量特判的实现都会
    在其中一条上挂掉。
    """
    values = [2.0, 9.0, 0.5, 12.0, 20.0, 1.0]
    frame = pd.DataFrame({"turnover_rate": values})
    node = _node(0.01, 0.20)

    actual = [evaluate_condition(node, pd.Series({"turnover_rate": value}), frame).passed for value in values]

    assert actual == _expected_percent_scale(values, 0.01, 0.20)


def test_prefilter_mask_selects_the_documented_turnover_band():
    """向量化预筛：2%~8% 的推荐策略必须能留下 2% 与 5% 的股票。

    预筛跑在行级判定之前，两条实现给出相反结论时，引擎会把行级判定为通过的股票
    全部提前丢掉，表现为"一个候选都筛不出"。
    """
    values = [2.0, 5.0, 9.0, 0.5, 0.05, 1.0, 8.0]
    frame = pd.DataFrame({"turnover_rate": values})
    node = _node(0.02, 0.08)

    mask = MASK_BUILDERS["turnover_between"](node, frame)

    assert mask.tolist() == _expected_percent_scale(values, 0.02, 0.08)


def test_prefilter_mask_agrees_on_a_second_band_with_nan_and_boundary_rows():
    """向量化预筛的第二个数据场景：0.5%~5%，并混入 0.0、NaN 与两个边界值。"""
    values = [0.7, 1.2, 3.3, 8.0, 0.0, float("nan"), 0.5, 5.0]
    frame = pd.DataFrame({"turnover_rate": values})
    node = _node(0.005, 0.05)

    mask = MASK_BUILDERS["turnover_between"](node, frame)

    expected = [False if pd.isna(value) else bool(0.005 <= value / 100.0 <= 0.05) for value in values]
    assert mask.tolist() == expected


# ---------------------------------------------------------------------------
# 组 5 · 涨跌停：先看板块，再看 ST
# ---------------------------------------------------------------------------


def _limit_up_row(
    trade_date: str,
    *,
    symbol: str,
    is_st: bool,
    open_price: float = 10.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    pre_close: float | None = None,
) -> dict:
    close_price = close if close is not None else open_price
    row = {
        "symbol": symbol,
        "trade_date": pd.Timestamp(trade_date),
        "open": open_price,
        "high": high if high is not None else max(open_price, close_price),
        "low": low if low is not None else min(open_price, close_price),
        "close": close_price,
        "volume": 1000,
        "is_suspended": False,
        "listing_days": 500,
        "float_market_cap": 2_000_000_000,
        "main_net_inflow": 0.0,
        "is_st": is_st,
    }
    if pre_close is not None:
        row["pre_close"] = pre_close
    return row


def _cap_strategy() -> StrategyConfig:
    return StrategyConfig(
        name="simple",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[],
    )


def _limit_settings() -> BacktestSettings:
    return BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-03").date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_up_blocks_buy=True,
        fixed_holding_days=1,
        exclude_st=False,
    )


@pytest.mark.parametrize(
    "symbol",
    ["300001", "301001", "688001", "430001"],
)
def test_st_flagged_growth_board_names_are_not_treated_as_five_percent(symbol):
    """风险警示标记落在创业板/科创板/北交所上时，涨跌幅限制仍按各自板块取。

    这几个板块的涨跌幅限制分别是 20%/20%/30%；把它们一律按 5% 判，涨停价附近的
    正常成交会被当成"封板"而错误拦截。
    """
    frame = pd.DataFrame(
        [
            _limit_up_row("2024-01-02", symbol=symbol, is_st=True, close=10.0),
            _limit_up_row("2024-01-03", symbol=symbol, is_st=True, open_price=11.5, pre_close=10.0),
        ]
    )

    result = run_backtest(frame, _cap_strategy(), _limit_settings())

    assert result.trades, f"{symbol} 属于高涨跌幅板块，11.5% 的开盘不应被当成封板拦掉"


def test_main_board_risk_flagged_name_still_uses_five_percent_and_is_blocked():
    """主板的风险警示标的仍是 5%：涨 6% 属于封板，必须被拦下。

    与上一条互为对照组：只有"按板块"与"一律 5%"两种极端都不成立时才说明判定顺序
    真的被拆对了。
    """
    frame = pd.DataFrame(
        [
            _limit_up_row("2024-01-02", symbol="600001", is_st=True, close=10.0),
            _limit_up_row("2024-01-03", symbol="600001", is_st=True, open_price=10.6, pre_close=10.0),
        ]
    )

    result = run_backtest(frame, _cap_strategy(), _limit_settings())

    assert not result.trades, "主板风险警示标的涨 6% 已封板，不应成交"


# ---------------------------------------------------------------------------
# 组 6 · 三个出口对同一事实必须给同一结论
# ---------------------------------------------------------------------------


def test_collection_row_and_prefilter_agree_on_the_same_turnover_fact():
    """端到端：采集侧产出的换手率列，喂给行级判定与向量化预筛，两者必须同时正确。

    这条同时约束"写出去的口径"与"读进来的口径"：只把其中一边改对，另一边仍会
    与用户口语的换手率区间对不上。
    """
    # 流通股 1000 万股；两天分别换手 2% 与 5%（成交量 2000 手 / 5000 手）
    two_days = [
        ["2026-09-23", "10.00", "10.50", "11.00", "9.80", "2000"],
        ["2026-09-24", "10.00", "10.50", "11.00", "9.80", "5000"],
    ]
    fetcher = _public_fetcher(two_days, _quote_line("示例股份", 10.00, 1.00))
    collected = fetcher.fetch_daily_bars(["600519"], "2026-09-23", "2026-09-24")

    values = collected["turnover_rate"].tolist()
    frame = collected[["symbol", "trade_date", "turnover_rate"]].copy()
    node = _node(0.02, 0.08)
    mask = MASK_BUILDERS["turnover_between"](node, frame)
    row_results = [
        evaluate_condition(node, pd.Series({"symbol": "600519", "turnover_rate": value}), frame).passed
        for value in values
    ]

    expected = [True, True]
    assert row_results == expected, f"行级判定与用户口语不符：{list(zip(values, row_results))}"
    assert mask.tolist() == expected, f"向量化预筛与用户口语不符：{list(zip(values, mask.tolist()))}"


def test_collection_and_market_cap_condition_see_the_same_capitalisation():
    """同一份采集数据，市值条件看到的就是入库那一列，不允许在条件侧再换算一次。

    行情通道与条件通道对"流通市值"的理解必须一致：条件侧若自己按现价重算一遍，
    同一只股票在两个出口会被判成不同的规模。这里用**通道自己算出的那一版**
    （当日股本口径）作为参照：两个出口必须同时看到这个值。
    """
    # 当日换手 2%、量 1000 手、收 10.5 → 该通道算出的流通市值 525_000；
    # 报价给的是今天的股本（8 亿），拿它回填这一行会得到 840_000_000。
    per_date_cap = 525_000.0
    market_data = "2024-01-02,10,10.5,11,9.8,1000,10500,+0.5,+5.0,2.0,10"
    fetcher = _baidu_fetcher(market_data, _quote_line("示例股份", 10.00, 8.00))
    collected = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    stored = float(collected.loc[0, "float_market_cap"])
    node = ConditionNode(
        id="cap",
        condition_id="market_cap_between",
        params={"min": per_date_cap * 0.99, "max": per_date_cap * 1.01},
    )
    frame = collected[["float_market_cap"]].copy()
    mask = MASK_BUILDERS["market_cap_between"](node, frame)
    row_result = evaluate_condition(node, pd.Series({"float_market_cap": stored}), frame).passed

    assert bool(mask.iloc[0]) is True, "向量化预筛看到的市值与入库的值不是同一个"
    assert row_result is True, "行级判定看到的市值与入库的值不是同一个"
    assert np.isfinite(stored)
