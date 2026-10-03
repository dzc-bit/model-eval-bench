"""T4-12 隐藏测试 · 简报新鲜度记账。

口径：只有真的产出过一次简报才推进"上次运行"的记账；未配置、缺模型、无素材
这类跳过一律不记账，配置/模型就绪后的第一次运行必须立刻可以发生。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.digest import DigestEngine, DigestStore
from astock_backtester.ai.insights import EventBroker
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
)

DIGEST_JSON = (
    '[{"title": "光量子计算取得突破", "summary": "图灵量子发布第三代光量子计算机。", '
    '"tags": ["行业"], "symbols": ["688047"]},'
    '{"title": "市场宽度回暖", "summary": "红盘占比回升至六成。", "tags": ["情绪"], "symbols": []}]'
)


class ScriptedModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.prompts: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None):
        self.prompts.append(messages)
        yield ("final", {"content": self.content, "tool_calls": None})

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class FakeBackend:
    news_provider = SimpleNamespace()
    realtime_provider = SimpleNamespace()
    briefing_provider = SimpleNamespace()

    def latest_news(self) -> MarketNewsResponse:
        return MarketNewsResponse(
            updated_at=datetime.now(UTC),
            source="fake",
            items=[MarketNewsItem(title="某条新闻标题", source="财联社电报")],
        )

    def snapshot(self) -> RealtimeMarketSnapshot:
        return RealtimeMarketSnapshot(
            status="live",
            source="fake",
            updated_at=datetime.now(UTC),
            indexes=[MarketIndexQuote(symbol="sh000001", name="上证指数", last=3100.0, change_pct=0.5, source="fake")],
            breadth=MarketBreadth(up=3200, down=1920, flat=0, total=5120, source="fake"),
            message="ok",
        )

    def log(self, level: str, message: str) -> None:
        pass


def _wire(backend: FakeBackend) -> None:
    backend.news_provider = SimpleNamespace(latest_news=backend.latest_news)
    backend.realtime_provider = SimpleNamespace(market_snapshot=backend.snapshot)
    backend.briefing_provider = SimpleNamespace(
        latest_fupan=lambda: SimpleNamespace(summary="复盘要点文字"),
        latest_zaopan=lambda: SimpleNamespace(summary=""),
    )


def _engine(tmp_path, backend: FakeBackend, *, config, model_provider, store_path=None) -> DigestEngine:
    _wire(backend)
    broker = EventBroker()
    broker.subscribe()  # 有订阅者时 run_once 才会发布
    return DigestEngine(
        broker=broker,
        backend=backend,
        model_provider=model_provider,
        config_provider=config,
        store=DigestStore(store_path or tmp_path),
    )


def test_unconfigured_period_does_not_delay_the_first_real_run(tmp_path, monkeypatch):
    """配置就绪前的跳过不得记账：配置就绪后的第一次简报必须立刻可跑。"""
    monkeypatch.setattr(
        "astock_backtester.ai.tools.astock_data_tools.fetch_limit_up_rows",
        lambda kind: [],
    )
    backend = FakeBackend()
    holder = {"config": AiConfig()}  # 尚未配置
    engine = _engine(
        tmp_path,
        backend,
        config=lambda: holder["config"],
        model_provider=lambda: ScriptedModel(DIGEST_JSON),
    )

    first = engine.run_once()
    assert first["skipped"] == "not_configured"

    holder["config"] = AiConfig(base_url="http://x", api_key="k", model="m")
    second = engine.run_once()
    assert second.get("ok") is True, f"配置就绪后的第一次运行不得被当作刚跑过：{second}"


def test_missing_model_period_does_not_delay_the_first_real_run(tmp_path, monkeypatch):
    """模型缺席的跳过同样不得记账：模型就绪后第一次运行必须立刻发生。"""
    monkeypatch.setattr(
        "astock_backtester.ai.tools.astock_data_tools.fetch_limit_up_rows",
        lambda kind: [],
    )
    backend = FakeBackend()
    config = lambda: AiConfig(base_url="http://x", api_key="k", model="m")  # noqa: E731
    holder = {"model": None}
    engine = _engine(
        tmp_path / "b",
        backend,
        config=config,
        model_provider=lambda: holder["model"],
        store_path=tmp_path / "b",
    )

    first = engine.run_once()
    assert first["skipped"] == "no_model"

    holder["model"] = ScriptedModel(DIGEST_JSON)
    second = engine.run_once()
    assert second.get("ok") is True, f"模型就绪后的第一次运行不得被当作刚跑过：{second}"
