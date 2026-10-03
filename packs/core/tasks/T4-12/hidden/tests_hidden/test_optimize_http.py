"""T4-12 隐藏测试 · 寻优 HTTP 出口的结果契约与错误码。

口径：结果事件必须携带废组合名单与已评估数（前端与过拟合判定的事实来源）；
网格组合数超限必须返回专用的 400 错误码，而不是与普通参数错误混用。
"""

from __future__ import annotations

import json
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from astock_backtester.sample_data import sample_daily_bars
from astock_backtester.service import create_server

# 回环流量不得走系统代理
_OPENER = build_opener(ProxyHandler({}))


def _request_json_allow_error(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        status = getattr(exc, "code", 500)
        body = getattr(exc, "read", None)
        if callable(body):
            return int(status), json.loads(body().decode("utf-8"))
        raise


def _request_ndjson(url: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=10) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def _start_server(tmp_path):
    warehouse = tmp_path / "本地数据仓"
    warehouse.mkdir(exist_ok=True)
    server = create_server(host="127.0.0.1", port=0, cache_dir=warehouse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread, server.server_address[1]


def _optimize_payload() -> dict:
    strategy = {
        "name": "寻优策略",
        "market_filters": [],
        "entry_groups": [
            {
                "id": "entry",
                "operator": "and",
                "conditions": [
                    {
                        "id": "c1",
                        "condition_id": "close_above_ma",
                        "enabled": True,
                        "params": {"window": 3},
                        "data_lag_days": 0,
                        "expression": "收盘价站上3日均线",
                    }
                ],
            }
        ],
        "exit_rules": [],
        "score_threshold": None,
    }
    settings = {
        "start_date": "2024-01-02",
        "end_date": "2024-01-08",
        "initial_cash": 1_000_000,
        "stock_pool": "all",
        "custom_symbols": [],
        "max_positions": 5,
        "max_daily_buys": 3,
        "fixed_holding_days": 2,
        "min_listing_days": 0,
    }
    return {"strategy": strategy, "settings": settings, "grid": {"max_positions": [0, 2]}}


def test_result_event_carries_failures_and_evaluated(tmp_path):
    """结果事件必须带上废组合名单与已评估数：同一次运行的三出口共享这份事实。"""
    server, thread, port = _start_server(tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    try:
        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/optimize", _optimize_payload())
        assert events[-1]["type"] == "result"
        result = events[-1]["result"]
        assert result["total"] == 2
        assert result["evaluated"] == 1
        failures = result.get("failures")
        assert failures is not None, "结果事件必须携带废组合名单，前端与判定出口都依赖它"
        assert len(failures) == 1
        assert failures[0]["code"] == "invalid_combination"
        assert failures[0]["params"] == {"max_positions": 0}
        assert result["best"]["params"] == {"max_positions": 2}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_oversized_grid_reports_grid_too_large_code(tmp_path):
    """组合数超限是结构性错误：必须以专用错误码 400 返回，提示"候选值太多"。"""
    server, thread, port = _start_server(tmp_path)
    try:
        payload = _optimize_payload()
        payload["grid"] = {"fixed_holding_days": [1] * 7, "max_positions": [2] * 7}
        status, body = _request_json_allow_error("POST", f"http://127.0.0.1:{port}/ai/optimize", payload)
        assert status == 400
        assert body["code"] == "grid_too_large", f"超限必须返回专用错误码，实际：{body}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
