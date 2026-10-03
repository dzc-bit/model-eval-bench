"""T4-12 隐藏测试 · 排名口径与网格级成本。

口径：零成交的组合不得参与最优评选，全部零成交时最优为空；指标增强整个网格
只算一次，结果与逐组合各算一次完全一致。
"""

from __future__ import annotations

from datetime import date

from astock_backtester.ai.optimizer import rank_combinations, run_optimization
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)
from astock_backtester.sample_data import sample_daily_bars


def _combos():
    return [
        {
            "index": 1,
            "params": {"fixed_holding_days": 3},
            "metrics": {"trade_count": 12, "total_return_pct": -0.02, "max_drawdown_pct": -0.05},
        },
        {
            "index": 2,
            "params": {"fixed_holding_days": 5},
            "metrics": {"trade_count": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0},
        },
    ]


def test_zero_trade_combination_is_never_the_best():
    """零成交组合收益恒为零，不得在最优评选里盖过真正交易过的组合。"""
    best = rank_combinations(_combos())
    assert best is not None, "有交易过的组合在，最优不得为空"
    assert best["index"] == 1, "最优必须来自有成交的组合"


def test_all_tradeless_grid_has_no_best():
    """全部零成交时最优为空：没有交易就没有"最优参数"。"""
    combos = [
        {"index": 1, "params": {"fixed_holding_days": 3}, "metrics": {"trade_count": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0}},
        {"index": 2, "params": {"fixed_holding_days": 5}, "metrics": {"trade_count": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0}},
    ]
    assert rank_combinations(combos) is None


def _settings() -> BacktestSettings:
    return BacktestSettings(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 8),
        initial_cash=1_000_000,
        max_positions=5,
        max_daily_buys=3,
        fixed_holding_days=2,
        min_listing_days=0,
    )


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        name="寻优策略",
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[ConditionNode(id="c1", condition_id="close_above_ma", params={"window": 3})],
            )
        ],
    )


def test_indicator_enrichment_runs_once_per_grid(monkeypatch):
    """指标增强是整个网格共用的一份公共计算：组合再多也只算一次。"""
    import astock_backtester.ai.optimizer as optimizer_module

    calls: list[int] = []
    real_enrich = optimizer_module.enrich_for_strategy

    def counting_enrich(frame, strategy):
        calls.append(1)
        return real_enrich(frame, strategy)

    monkeypatch.setattr(optimizer_module, "enrich_for_strategy", counting_enrich)
    grid = {"fixed_holding_days": [1, 2, 3], "max_positions": [2, 3]}
    events: list[dict] = []
    summary = run_optimization(sample_daily_bars(), _strategy(), _settings(), grid, events.append)

    assert summary["evaluated"] == 6
    assert len(calls) == 1, f"指标增强必须整个网格共用一份，实际计算了 {len(calls)} 次"
