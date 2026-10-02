"""T4-11 联合一致性隐藏用例：两条链之间的真实相互作用。

与本包另外两份隐藏用例的分工：test_realtime_arbitration.py 与
test_write_chain.py 各自守一条链的内部不变量；本文件守的是**跨链**不变量——
两条链共享同一个本地数据仓，写入侧的失效协议与健康出口、行情侧的兜底快照
与宽度校验，必须对同一份盘上事实给出同一个答案。

* 用例一（写入 → 行情）：写入侧的股票池失效协议是行情侧宽度完整性校验的
  事实来源。写入让"本地有多少只股票"变了，行情侧下一次校验必须基于写后
  事实——要么拿到写后新值，要么诚实报告"计数未热"，绝不允许继续引用写前
  的旧池子做校验。
* 用例二（写入 → 健康 × 行情）：同一次外部程序写入，必须同时穿透健康覆盖
  出口与行情兜底出口；任一出口停在写前状态，联合一致性即告失败。

确定性纪律与邻题一致：零网络（板块主题抓取显式跳过）、零裸计时（同步等待
与轮询落定都以状态为准）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd

try:
    from astock_backtester.data.realtime import RealtimeMarketProvider
    from astock_backtester.data.warehouse import Warehouse
except ModuleNotFoundError:  # pragma: no cover - 兼容带 backend/ 前缀的布局
    from backend.astock_backtester.data.realtime import RealtimeMarketProvider
    from backend.astock_backtester.data.warehouse import Warehouse

UTC = timezone.utc
_OPENER = build_opener(ProxyHandler({}))


def _bars(symbols, dates, base: float = 10.0) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for day in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": base,
                    "high": base + 0.5,
                    "low": base - 0.5,
                    "close": base + 0.2,
                    "volume": 1000,
                }
            )
    return pd.DataFrame(rows)


def _get_json(port: int, path: str) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with _OPENER.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _ServerHarness:
    """起一个本地数据服务（回环、临时缓存目录），供跨链用例复用。"""

    def __init__(self, cache_dir: Path) -> None:
        from astock_backtester.service import create_server

        self.server = create_server(host="127.0.0.1", port=0, cache_dir=cache_dir)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=20)

    def health(self, attempts: int = 60, delay: float = 0.1) -> dict:
        """取一次 /health；若覆盖刷新还在跑，继续轮询到它落地。"""
        payload = _get_json(self.port, "/health")
        for _ in range(attempts):
            if not payload.get("coverage_refreshing"):
                return payload
            time.sleep(delay)
            payload = _get_json(self.port, "/health")
        return payload


# ===========================================================================
# 联合场景一：写入侧失效协议守卫行情侧宽度校验的事实源
# ===========================================================================


def test_write_invalidates_the_pool_the_realtime_guard_checks_against(tmp_path):
    """股票池只数在任一时刻只能有一个答案：写入改变它之后，行情侧不得再拿写前的旧值做校验。

    行情兜底快照在判断"红绿家数是否覆盖全市场"时要引用本地股票池规模；
    这个数字由写入侧的失效协议负责保鲜。协议若失灵，行情侧会拿写前的
    旧池子去校验写后的新数据——两边对"全市场有多大"各执一词，且谁都不报错。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(
        _bars(["600519", "000001", "300750", "688981"], ["2026-06-01", "2026-06-02", "2026-06-03"])
    )
    assert warehouse.refresh_symbol_count() == 4, "夹具自身校验：预热后的股票池应为 4"
    provider = RealtimeMarketProvider(warehouse)
    now = datetime.now(UTC)

    snap_before = provider._snapshot_from_local(now, skip_topic_fetch=True)
    text_before = "\n".join(str(item) for item in (snap_before.diagnostics or []))
    assert "本地股票池=4" in text_before, (
        "夹具自身校验：写前快照应按池子 4 做校验，实际诊断：%s" % text_before
    )

    # 外部写入第 5 只股票（同一最新交易日）：盘上事实变了。
    warehouse.write_daily_bars(_bars(["600603"], ["2026-06-03"]))

    snap_after = provider._snapshot_from_local(now, skip_topic_fetch=True)
    text_after = "\n".join(str(item) for item in (snap_after.diagnostics or []))
    assert "本地股票池=4" not in text_after, (
        "写入之后行情侧仍引用写前的旧股票池（4）做宽度校验：写入侧的失效协议没有打穿到行情侧"
    )
    assert ("未热" in text_after) or ("本地股票池=5" in text_after), (
        "写入之后行情侧必须要么诚实报告计数未热、要么已拿到写后新值 5，实际诊断：%s" % text_after
    )

    # 同步落定（等价于等后台预热完成，但不依赖线程时序）：
    # 重算之后行情侧引用的池子必须等于盘上真实只数。
    assert warehouse.refresh_symbol_count() == 5, "写入后股票池必须重算为盘上真实只数"
    snap_settled = provider._snapshot_from_local(now, skip_topic_fetch=True)
    text_settled = "\n".join(str(item) for item in (snap_settled.diagnostics or []))
    assert "本地股票池=5" in text_settled, (
        "落定之后行情侧校验必须基于写后事实（5 只），实际诊断：%s" % text_settled
    )


# ===========================================================================
# 联合场景二：同一次外部写入同时穿透健康出口与行情出口
# ===========================================================================


def test_external_write_reaches_health_and_market_exits_with_the_same_fact(tmp_path):
    """一次外部程序写入之后，健康覆盖口径与行情兜底口径必须报同一个最新事实。

    两条链共享同一个本地数据仓：写入发生之后，健康出口报告的覆盖范围与
    行情兜底快照引用的最新交易日，必须同时等于盘上真实状态；任何一个出口
    停在写前状态（或根本没人去刷新它），用户看到的两套口径就互相矛盾。
    """
    harness = _ServerHarness(tmp_path)
    try:
        harness.health()  # 空仓时的初始刷新：把覆盖快照置于已知的"未热"起点

        # 另一个程序写入：不经 HTTP 路由、不经服务进程内的仓库句柄。
        external = Warehouse(tmp_path)
        external.write_daily_bars(
            _bars(["600519", "000001", "300750", "688981"], ["2026-06-01", "2026-06-02", "2026-06-03"])
        )

        payload = harness.health()
        datasets = {item["dataset"]: item for item in payload["coverage"]}
        assert "daily_bars" in datasets, f"健康快照缺少日线条目：{payload['coverage']}"
        assert datasets["daily_bars"]["symbols"] == 4, (
            "外部写入之后健康口径报的只数必须等于盘上真实只数，实际：%s" % datasets["daily_bars"]
        )
        assert str(datasets["daily_bars"]["end_date"]) == "2026-06-03", (
            "外部写入之后健康口径仍停在写前的覆盖范围，实际：%s" % datasets["daily_bars"]
        )

        # 行情出口：与服务同一个行情提供者实例（共享同一数据仓）的本地兜底快照。
        provider = harness.server.state.realtime_provider
        snap = provider._snapshot_from_local(datetime.now(UTC), skip_topic_fetch=True)
        assert snap.status != "unavailable", "盘上有数据之后行情兜底快照不得再报不可用"
        text = "\n".join([str(snap.message or ""), *(str(item) for item in (snap.diagnostics or []))])
        assert "2026-06-03" in text, (
            "健康口径已经报到 2026-06-03，行情兜底口径也必须看到同一天，实际：%s" % text
        )
    finally:
        harness.close()
