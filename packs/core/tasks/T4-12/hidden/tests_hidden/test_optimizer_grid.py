"""T4-12 隐藏测试 · 网格校验的归宿与门槛。

口径：整数参数的小数候选由组合评估判废、计入废组合名单，其余候选照常运行；
网格入口的校验只拒绝结构性错误（未知参数、空数组、非数字、组合数超限）。
"""

from __future__ import annotations

from datetime import date

import pytest
from astock_backtester.ai.optimizer import GridTooLargeError, normalize_grid, run_optimization
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)
from astock_backtester.sample_data import sample_daily_bars


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


def _run_grid(grid):
    events: list[dict] = []
    summary = run_optimization(sample_daily_bars(), _strategy(), _settings(), grid, events.append)
    return summary, events


def test_fractional_integer_candidate_is_reported_not_fatal():
    """小数的整数候选只废掉它自己所在的组合，其余候选照常运行。

    走与请求相同的路径：先过网格入口校验，再进组合评估——入口不得把
    小数候选判死整次请求。
    """
    grid = normalize_grid({"max_positions": [2.5, 3]})
    summary, events = _run_grid(grid)

    assert summary["total"] == 2
    assert summary["evaluated"] == 1
    assert [combo["params"] for combo in summary["combinations"]] == [{"max_positions": 3}]
    assert len(summary["failures"]) == 1, "小数候选必须进废组合名单，而不是判死整次寻优"
    failure = summary["failures"][0]
    assert failure["params"] == {"max_positions": 2.5}
    assert failure["code"] == "invalid_combination"
    assert [event["type"] for event in events] == ["progress", "combination", "progress"]


def test_grid_validation_still_enforces_whitelist_and_finiteness():
    """结构性错误仍在网格入口拒绝：未知参数、超限、非数字。"""
    with pytest.raises(ValueError, match="不支持寻优的参数"):
        normalize_grid({"entry_window": [3, 5]})
    with pytest.raises(GridTooLargeError):
        normalize_grid({"fixed_holding_days": [1] * 7, "max_positions": [2] * 7})
    with pytest.raises(ValueError, match="必须是数字"):
        normalize_grid({"max_positions": ["3"]})
    with pytest.raises(ValueError, match="必须是数字"):
        normalize_grid({"max_positions": [True]})
