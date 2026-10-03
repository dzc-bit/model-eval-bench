"""T4-12 隐藏测试 · 过拟合判定的样本口径与严重度下限。

口径：小样本判定只数可比较组合（被剔除的非法组合不进样本量，措辞里的组数不虚高）；
交易笔数的严重度下限维持在 5 笔（5 笔以下评 critical，5 笔及以上 warning）。
"""

from __future__ import annotations

from astock_backtester.ai.overfit import assess_overfit


def _combos(values):
    return [{"metrics": {"total_return_pct": value}} for value in values]


def _codes(result):
    return {finding["code"] for finding in result["findings"]}


def test_small_sample_counts_only_comparable_combinations():
    """被剔除的非法组合不是样本：小样本判定与措辞只数可比较组合。"""
    result = assess_overfit({"trade_count": 30}, combos=_combos([0.05, 0.02, 0.03]), rejected=3)

    assert "grid_small_sample" in _codes(result), "只有 3 组可比较结果，必须提示样本偏少"
    finding = next(item for item in result["findings"] if item["code"] == "grid_small_sample")
    assert "只有 3 组可比结果" in finding["message"], f"措辞里的组数不得包含被剔除组合：{finding['message']}"

    # 被剔除组合的可见性不受影响：剔除事实仍要单独说明
    assert "grid_partial_failures" in _codes(result)


def test_small_sample_not_triggered_when_grid_is_large_enough():
    """可比较组合充足时不提示小样本——被剔除数量再多也不改变这一点。"""
    result = assess_overfit(
        {"trade_count": 30},
        combos=_combos([0.05, 0.06, 0.07, 0.08, 0.09]),
        rejected=8,
    )

    assert "grid_small_sample" not in _codes(result), "5 组可比结果已足够，剔除数量不得触发误报"
    assert "grid_partial_failures" in _codes(result)


def test_few_trades_severity_floor_stays_at_five():
    """5 笔以下样本太少必须评 critical，5 笔及以上维持 warning。"""
    result = assess_overfit(
        {"trade_count": 4, "total_return_pct": 0.2, "win_rate_pct": 0.9, "max_drawdown_pct": -0.05}
    )
    finding = next(item for item in result["findings"] if item["code"] == "few_trades")
    assert finding["level"] == "critical", "不足 5 笔的样本统计意义不足，必须评 critical"
    assert result["level"] == "critical"

    result = assess_overfit(
        {"trade_count": 6, "total_return_pct": 0.2, "win_rate_pct": 0.9, "max_drawdown_pct": -0.05}
    )
    finding = next(item for item in result["findings"] if item["code"] == "few_trades")
    assert finding["level"] == "warning", "6 笔已过下限，维持 warning 即可"
