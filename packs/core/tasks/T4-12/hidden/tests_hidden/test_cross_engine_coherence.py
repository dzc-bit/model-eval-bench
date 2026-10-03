"""T4-12 联合 coherence 组 · 同一次运行在所有出口只有一个口径。

两条联合用例断言真实相互作用（不是合取记账）：
1. 同一次寻优运行的汇总、过拟合判定与解读上下文必须共享同一份事实——
   废组合数量、样本措辞、已评估数、最优的成交状态任意一处对不上即失败。
2. 同一次简报运行的库存、事件流与推送上限必须共享同一份"新增"事实——
   库存收全部、推送封顶两条、标签口径一致。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.digest import DigestEngine, DigestStore
from astock_backtester.ai.insights import EventBroker
from astock_backtester.ai.optimizer import build_optimize_insight_context, run_optimization
from astock_backtester.ai.overfit import assess_overfit
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    MarketBreadth,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
    StrategyConfig,
)
from astock_backtester.sample_data import sample_daily_bars


def _settings() -> BacktestSettings:
    return BacktestSettings(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 8),
        initial_cash=1_000_000,
        max_positions=5,
        max_daily_buys=3,
        fixed_holding_days=2,
        min_listing_days=0,
    )


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        name="寻优策略",
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[ConditionNode(id="c1", condition_id="close_above_ma", params={"window": 3})],
            )
        ],
    )


def test_one_optimize_run_reports_one_truth_to_overfit_and_insight_context():
    """汇总 / 过拟合判定 / 解读上下文对同一次运行只能有一个口径。"""
    events: list[dict] = []
    summary = run_optimization(
        sample_daily_bars(), _strategy(), _settings(), {"max_positions": [0, 2]}, events.append
    )

    # 汇总层：废组合只进 failures，不进排名
    assert summary["total"] == 2
    assert summary["evaluated"] == 1
    assert len(summary["failures"]) == 1
    assert summary["failures"][0]["params"] == {"max_positions": 0}
    assert summary["best"] is None or summary["best"]["metrics"]["trade_count"] > 0

    # 过拟合层：用本次运行的真实产物判定，rejected 数量与样本措辞必须一致
    verdict = assess_overfit(
        (summary["best"] or summary["combinations"][0])["metrics"],
        combos=summary["combinations"],
        rejected=len(summary["failures"]),
    )
    partial = next(
        (item for item in verdict["findings"] if item["code"] == "grid_partial_failures"), None
    )
    assert partial is not None, "废组合的存在必须在判定里可见"
    assert "1 个参数组合不合法" in partial["message"], f"剔除数量措辞必须与本次运行一致：{partial['message']}"
    small = next((item for item in verdict["findings"] if item["code"] == "grid_small_sample"), None)
    if small is not None:
        assert "只有 1 组可比结果" in small["message"], f"样本措辞不得把废组合计入：{small['message']}"

    # 解读上下文层：与汇总同一份事实
    context = build_optimize_insight_context(summary)
    assert context["evaluated"] == 1
    assert [failure["params"] for failure in context["failures"]] == [{"max_positions": 0}]
    assert len(context["combinations"]) == 1


FOUR_ITEMS = (
    "["
    + ",".join(
        '{"title": "要点%d", "summary": "第%d条要点的内容。", "tags": [], "symbols": []}' % (index, index)
        for index in range(1, 5)
    )
    + "]"
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


def test_one_digest_run_reaches_store_and_event_stream_with_the_same_fact(tmp_path):
    """同一次简报运行：库存、事件流、推送上限共享同一份新增事实。"""
    from astock_backtester.ai.tools import astock_data_tools

    original_fetch = astock_data_tools.fetch_limit_up_rows
    astock_data_tools.fetch_limit_up_rows = lambda kind: []
    try:
        backend = FakeBackend()
        backend.news_provider = SimpleNamespace(latest_news=backend.latest_news)
        backend.realtime_provider = SimpleNamespace(market_snapshot=backend.snapshot)
        backend.briefing_provider = SimpleNamespace(
            latest_fupan=lambda: SimpleNamespace(summary="复盘要点文字"),
            latest_zaopan=lambda: SimpleNamespace(summary=""),
        )
        broker = EventBroker()
        stream = broker.subscribe()
        engine = DigestEngine(
            broker=broker,
            backend=backend,
            model_provider=lambda: ScriptedModel(FOUR_ITEMS),
            config_provider=lambda: AiConfig(base_url="http://x", api_key="k", model="m"),
            store=DigestStore(tmp_path),
        )

        result = engine.run_once()
        assert result.get("ok") is True

        events = []
        while not stream.empty():
            events.append(stream.get_nowait())
        data_fresh = [event for event in events if event["type"] == "data_fresh"]
        insights = [event for event in events if event["type"] == "insight"]

        # 同一份事实的两个出口：库存收全部，推送封顶两条且指向库存条目
        assert len(data_fresh) == 1 and data_fresh[0]["module"] == "ai_news"
        assert engine.view()["count"] == 4, "四条新要点都应入库"
        assert len(insights) == 2, f"推送必须与库存的新增事实对齐且封顶两条，实际 {len(insights)} 条"
        store_titles = {item["title"] for item in engine.view()["items"]}
        assert {event["insight"]["title"] for event in insights} <= store_titles
        assert all(event["insight"].get("disclaimer") for event in insights)
    finally:
        astock_data_tools.fetch_limit_up_rows = original_fetch
