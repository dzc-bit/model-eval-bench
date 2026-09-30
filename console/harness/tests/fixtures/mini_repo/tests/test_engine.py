"""引擎层的可见测试。"""

from miniapp import engine

ROW_A = {"symbol": "SH600000", "close": 10.0, "volume": 4_000_000.0, "shares": 200_000_000.0}
ROW_B = {"symbol": "SH600001", "close": 10.0, "volume": 1_000_000.0, "shares": 200_000_000.0}


def test_board_pass_rejects_low_turnover():
    assert engine.board_pass(ROW_B) is False


def test_board_pass_accepts_high_turnover():
    assert engine.board_pass(ROW_A) is True


def test_summary_counts_candidates():
    summary = engine.summary_of([ROW_A, ROW_B])
    assert summary["total"] == 2
    assert summary["candidates"] == ["SH600000"]
