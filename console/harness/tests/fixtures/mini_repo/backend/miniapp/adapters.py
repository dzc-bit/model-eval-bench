"""行情行适配层：把一行行情变成派生列。

派生列的口径必须与 pricing 一致：这里只做搬运与组装，不重新定义公式。
"""

from . import pricing


def row_to_derived(row):
    """一行行情 → 派生列（换手率、市值、是否 ST）。"""
    code = pricing.normalize_symbol(row["symbol"])
    return {
        "symbol": code,
        "close": float(row["close"]),
        "turnover": pricing.turnover_rate(
            float(row["close"]), float(row["volume"]), float(row["shares"])
        ),
        "market_cap": pricing.market_cap(float(row["close"]), float(row["shares"])),
        "special_treated": pricing.is_special_treated(code),
    }


def rows_to_derived(rows):
    """批量转换。"""
    return [row_to_derived(row) for row in rows]
