"""T4-12 隐藏测试 · 快讯节流窗口与档位去重。

口径：快讯数量上限的滑动窗口是一小时；同一触发原因的去重键必须保留档位划分，
档位之间的移动是新的触发。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.insights import EventBroker, InsightEngine
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
    RiskAlertsResponse,
)


class ScriptedModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompts: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools=None):
        self.prompts.append(messages)
        yield ("final", {"content": self.content, "tool_calls": None})

    def embed(self, texts):
        return [[0.0]]


def _news(titles: list[str]) -> MarketNewsResponse:
    return MarketNewsResponse(
        updated_at=datetime.now(UTC),
        source="fake",
        items=[MarketNewsItem(title=title, source="财联社") for title in titles],
    )


def _snapshot(up: int, total: int) -> RealtimeMarketSnapshot:
    return RealtimeMarketSnapshot(
        status="live",
        source="fake",
        updated_at=datetime.now(UTC),
        indexes=[MarketIndexQuote(symbol="sh000001", name="上证指数", last=3100.0, source="fake")],
        breadth=MarketBreadth(up=up, down=total - up, flat=0, total=total, source="fake"),
        message="ok",
    )


class FakeBackend:
    def __init__(self) -> None:
        self.news_titles: list[str] = ["第一条新闻"]
        self.breadth_up = 2600
        self.breadth_total = 5200
        self.risk_count = 3

    news_provider = SimpleNamespace()
    realtime_provider = SimpleNamespace()
    risk_provider = SimpleNamespace()

    def latest_news(self):
        return _news(self.news_titles)

    def snapshot(self):
        return _snapshot(self.breadth_up, self.breadth_total)

    def alerts(self):
        return RiskAlertsResponse(updated_at=datetime.now(UTC), source="fake", items=[])

    def log(self, level, message):
        pass


def _wire(backend: FakeBackend) -> None:
    backend.news_provider = SimpleNamespace(latest_news=backend.latest_news)
    backend.realtime_provider = SimpleNamespace(market_snapshot=backend.snapshot)
    backend.risk_provider = SimpleNamespace(current_alerts=backend.alerts)


def _engine(backend: FakeBackend, broker: EventBroker, config: AiConfig):
    model = ScriptedModel("市场宽度异常，注意情绪退潮风险")
    engine = InsightEngine(
        broker,
        backend,
        model_provider=lambda: model,
        config_provider=lambda: config,
    )
    return engine, model


def _drain(stream) -> list[dict[str, Any]]:
    events = []
    while not stream.empty():
        events.append(stream.get_nowait())
    return events


def test_insight_cap_window_recovers_within_the_hour(monkeypatch):
    """数量上限按一小时滑动窗口记账：窗口滑走后必须立刻恢复生成。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("astock_backtester.ai.insights.time", SimpleNamespace(monotonic=lambda: clock["now"]))
    broker = EventBroker()
    stream = broker.subscribe()
    backend = FakeBackend()
    _wire(backend)
    config = AiConfig(base_url="http://x", api_key="k", model="m", insight_max_per_hour=2)
    engine, _model = _engine(backend, broker, config)

    drained: list[dict[str, Any]] = []

    backend.breadth_up = 500  # ≈9.6%，低档极端
    engine.tick()
    backend.breadth_up = 60  # ≈1.2%，同向但档位不同 → 允许第二条
    engine.tick()
    drained.extend(_drain(stream))
    assert len([e for e in drained if e["type"] == "insight"]) == 2

    clock["now"] += 2 * 3600  # 两小时后：小时窗口早已滑走
    backend.breadth_up = 640  # ≈12.3%，仍低档极端
    engine.tick()

    drained.extend(_drain(stream))
    insights = [e for e in drained if e["type"] == "insight"]
    assert len(insights) == 3, "小时窗口滑走后不得继续按旧配额压制快讯"


def test_width_leaving_one_extreme_bucket_realerts():
    """宽度在两个极端档位之间移动是新的触发，不得被同一档冷却吞掉。"""
    broker = EventBroker()
    stream = broker.subscribe()
    backend = FakeBackend()
    _wire(backend)
    config = AiConfig(base_url="http://x", api_key="k", model="m", insight_max_per_hour=60)
    engine, model = _engine(backend, broker, config)

    backend.breadth_up = 500  # ≈9.6%
    engine.tick()
    backend.breadth_up = 640  # ≈12.3%，同一档 → 冷却，不调模型
    engine.tick()
    backend.breadth_up = 1200  # ≈23.1%，档位移动 → 必须再次提示
    engine.tick()

    insights = [e for e in _drain(stream) if e["type"] == "insight"]
    assert len(insights) == 2, "档位之间的移动不得被旧档位的冷却吞掉"
    assert len(model.prompts) == 2
