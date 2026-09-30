"""回归基线用例：这道题不该影响它们，p2p.json 会点名这几条。"""

from miniapp import pricing


def test_normalize_symbol_stable():
    assert pricing.normalize_symbol("sh600000") == "SH600000"
    assert pricing.normalize_symbol("sz000001.SZ") == "SZ000001"


def test_market_cap_stable():
    assert pricing.market_cap(10.0, 200_000_000.0) == 20.0
    assert pricing.market_cap(3.5, 10_000_000.0) == 0.35
