"""选股引擎：按派生口径判定一只票能不能进候选池。

阈值与口径都来自 pricing / adapters，本模块只做判定，不再重算派生值。
"""

from . import adapters, pricing

TURNOVER_FLOOR = 0.01
MARKET_CAP_FLOOR = 10.0


def board_pass(row):
    """进候选池的条件：非 ST，且换手率与市值同时过线。"""
    derived = adapters.row_to_derived(row)
    if derived["special_treated"]:
        return False
    return (
        derived["turnover"] > TURNOVER_FLOOR
        and derived["market_cap"] > MARKET_CAP_FLOOR
    )


def screen_candidates(rows):
    """筛出候选票（去重后按代码排序）。"""
    picked = []
    for row in rows:
        if board_pass(row) and row["symbol"] not in picked:
            picked.append(row["symbol"])
    return sorted(picked)


def summary_of(rows):
    """候选池概览：给上层看板用。"""
    return {
        "total": len(rows),
        "candidates": screen_candidates(rows),
        "excluded_special": sum(
            1 for r in rows if pricing.is_special_treated(r["symbol"])
        ),
    }
