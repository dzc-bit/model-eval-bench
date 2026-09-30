"""隐藏测试：评分时才叠进评分树，模型全程看不到。

分组见 hidden/groups.json：一个出口一组，coherence 权重最高。
这些断言只描述"必须成立的事实"，不描述"该写成什么样"，所以
换一种正确写法也能过——只认结果，不认具体代码。
"""

from miniapp import adapters, engine, pricing

ROW_A = {"symbol": "SH600000", "close": 10.0, "volume": 4_000_000.0, "shares": 200_000_000.0}
ROW_B = {"symbol": "SH600001", "close": 10.0, "volume": 1_000_000.0, "shares": 200_000_000.0}
ROW_ST = {"symbol": "SH600002ST", "close": 50.0, "volume": 90_000_000.0, "shares": 200_000_000.0}


# ---------------------------------------------------------------- turnover_exit
def test_turnover_rate_uses_shares():
    """换手率要以流通股本为分母：400 万股换手于 2 亿流通股 = 2%。"""
    assert pricing.turnover_rate(10.0, 4_000_000.0, 200_000_000.0) == 0.02
    assert pricing.turnover_rate(10.0, 1_000_000.0, 200_000_000.0) == 0.005
    # 换收盘价不该影响换手率
    assert pricing.turnover_rate(88.0, 4_000_000.0, 200_000_000.0) == 0.02


# ----------------------------------------------------------------- adapter_exit
def test_adapter_delegates_turnover_to_pricing():
    """adapter 的派生列必须与 pricing 同口径。"""
    derived = adapters.row_to_derived(ROW_A)
    assert derived["turnover"] == pricing.turnover_rate(
        ROW_A["close"], ROW_A["volume"], ROW_A["shares"]
    )
    assert derived["turnover"] == 0.02


# ------------------------------------------------------------------ engine_exit
def test_engine_rejects_low_turnover_row():
    """换手 0.5% 的票不能进候选池（ST 票同样不能）。"""
    assert engine.board_pass(ROW_B) is False
    assert engine.board_pass(ROW_ST) is False
    assert engine.board_pass(ROW_A) is True


# ------------------------------------------------------------- market_cap_exit
def test_market_cap_formula_unchanged():
    """市值口径没被动过，这一组必须始终是绿的。"""
    assert pricing.market_cap(10.0, 200_000_000.0) == 20.0
    assert pricing.market_cap(3.5, 10_000_000.0) == 0.35


# -------------------------------------------------------------------- coherence
def test_all_exits_agree_on_same_fact():
    """同一个事实，三处出口必须给同一个答案。"""
    for row in (ROW_A, ROW_B, ROW_ST):
        derived = adapters.row_to_derived(row)
        direct = pricing.turnover_rate(row["close"], row["volume"], row["shares"])
        assert derived["turnover"] == direct, "adapter 与 pricing 口径不一致：%s" % row["symbol"]
        assert derived["special_treated"] == pricing.is_special_treated(row["symbol"])
        # 引擎的判定也必须由这两个派生列推出，而不是自己再算一遍
        expected = (not derived["special_treated"]
                    and derived["turnover"] > engine.TURNOVER_FLOOR
                    and derived["market_cap"] > engine.MARKET_CAP_FLOOR)
        assert engine.board_pass(row) is expected, "engine 与派生列口径不一致：%s" % row["symbol"]


def test_summary_counts_candidates():
    """看板概览要只留下真正过闸的票。"""
    summary = engine.summary_of([ROW_A, ROW_B, ROW_ST])
    assert summary["total"] == 3
    assert summary["candidates"] == ["SH600000"]
    assert summary["excluded_special"] == 1
