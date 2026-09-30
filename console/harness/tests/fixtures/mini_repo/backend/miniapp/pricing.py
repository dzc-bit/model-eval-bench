"""迷你派生指标：换手率与市值的唯一口径出处。

同一份事实只在这里算一次，adapters 与 engine 都从这里取，
避免"同一个事实被几处各自计算"（这道验收题要考的就是这个）。
"""

SPECIAL_SUFFIXES = ("ST", "退")


def normalize_symbol(symbol):
    """归一化股票代码：去掉市场后缀与空白。"""
    return symbol.strip().split(".")[0].upper()


def is_special_treated(symbol):
    """是否 ST / *ST / 退市整理。"""
    code = normalize_symbol(symbol)
    return any(code.endswith(suffix) for suffix in SPECIAL_SUFFIXES)


def turnover_rate(close, volume, shares):
    """换手率 = 成交量 / 流通股本。"""
    if shares <= 0:
        return 0.0
    return round(volume / shares, 6)


def market_cap(close, shares):
    """总市值（亿元）= 收盘价 × 总股本。"""
    return round(close * shares / 1e8, 6)
