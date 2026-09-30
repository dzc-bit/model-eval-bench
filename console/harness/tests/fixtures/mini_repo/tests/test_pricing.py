"""可见的守卫测试：模型能看到，但点名不变量的那几条会被 visible.prune 裁掉。"""

from miniapp import pricing


def test_normalize_symbol_strips_market_suffix():
    assert pricing.normalize_symbol("sh600000") == "SH600000"
    assert pricing.normalize_symbol("  sz000001.sz ") == "SZ000001"


def test_market_cap_is_close_times_shares():
    assert pricing.market_cap(10.0, 200_000_000.0) == 20.0


def test_turnover_rate_is_volume_over_shares():
    # 这条会点名"换手率=成交量/流通股本"，出题时应被 prune 掉
    assert pricing.turnover_rate(10.0, 4_000_000.0, 200_000_000.0) == 0.02


def test_turnover_rate_zero_shares_is_zero():
    assert pricing.turnover_rate(10.0, 4_000_000.0, 0.0) == 0.0


def test_special_treated_detection():
    assert pricing.is_special_treated("SH600001ST") is True
    assert pricing.is_special_treated("SH600002") is False
