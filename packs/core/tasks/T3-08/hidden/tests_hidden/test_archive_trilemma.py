"""T3-08 隐藏测试：会话历史三机制（归档 / 配对 / 字符预算）的合取不变量。

只断言"事实"层面的不变量，不点名实现位置：
- 移出窗口的内容必须完整到达长期记忆层（归档是搬运不是裁剪）；
- 任何一次真实请求都必须协议合法（调用—结果一一配对，无孤儿结果头）；
- 整理之后真实发送体积必须回到预算内（口径含工具调用参数体）；
- 长期记忆召回的预算语义保持严格（对照锚，不许被顺手放松）。

确定性纪律：模型全部脚本化、常量全部 monkeypatch，零网络、零等待；
性能硬约束（整理动作的模型调用次数）用计数器断言，不做裸计时。
"""

from __future__ import annotations

import logging
from typing import Any

from astock_backtester.ai import agent as agent_mod
from astock_backtester.ai.agent import AgentRunner
from astock_backtester.ai.context import ContextBudget, ToolResultStore
from astock_backtester.ai.memory import FACTS_BUDGET_CHARS, MemoryRecord, MemoryStore
from astock_backtester.ai.tools.registry import AiTool, ToolRegistry


class ScriptedModel:
    """脚本化 ChatModel：按顺序弹出每轮应答，记录全部请求（零网络、零等待）。"""

    def __init__(self, script: list[list[tuple[str, Any]]]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.script:
            raise AssertionError("脚本耗尽：模型被调用的次数超出预期")
        yield from self.script.pop(0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] for t in texts]


def _final(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None):
    return ("final", {"content": content, "tool_calls": tool_calls})


def _tool_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _make_registry() -> ToolRegistry:
    """probe 工具：结果摘要携带递增标记，供断言追踪内容去向。"""
    made = {"count": 0}

    def summarize(payload: dict[str, Any]) -> str:
        made["count"] += 1
        return f"查询结果 MARK-R{made['count']} " + "数" * 250

    registry = ToolRegistry()
    registry.register(AiTool(
        name="probe_tool",
        description="probe",
        parameters={"type": "object", "properties": {}},
        executor=lambda args: {"ok": True, "value": args.get("x"), "diagnostics": []},
        summarizer=summarize,
    ))
    return registry


def _session() -> dict[str, Any]:
    return {
        "session_id": "s1", "title": "新会话", "created_at": "now", "updated_at": "now",
        "rolling_summary": "", "pending_archive": [], "messages": [], "display": [],
    }


def _wire_chars(messages: list[dict[str, Any]]) -> int:
    """真实发送体积：正文 + 工具调用参数体，与预算工具的统计口径一致。"""
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            total += len(str((call.get("function") or {}).get("arguments") or ""))
    return total


def _pairing_problems(messages: list[dict[str, Any]]) -> list[str]:
    """协议配对审查：每条 tool 结果必须能归属到前面最近一次 assistant 声明。"""
    problems: list[str] = []
    pending: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if pending.get(call_id, 0) > 0:
                pending[call_id] -= 1
            else:
                problems.append(f"第 {index} 条 tool 结果没有可归属的调用（call_id={call_id}）")
            continue
        if any(v > 0 for v in pending.values()):
            problems.append(f"第 {index} 条 {message.get('role')} 消息打断了未配对的调用")
        pending = {}
        for call in message.get("tool_calls") or []:
            key = str(call.get("id") or "")
            pending[key] = pending.get(key, 0) + 1
    dangling = sum(v for v in pending.values() if v > 0)
    if dangling:
        problems.append(f"会话尾部还有 {dangling} 个调用没有结果")
    return problems


# ---------------------------------------------------------------------------
# 组 1：archive_integrity_exit —— 归档是搬运，不是裁剪
# ---------------------------------------------------------------------------

def test_archive_moves_complete_rounds_into_long_term_memory(monkeypatch):
    """长工具轮把窗口挤爆时，移出窗口的整段必须完整到达长期记忆层。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 6)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 20_000)
    model = ScriptedModel([[_final(content="综合结论")]])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].append({"role": "user", "content": "开场要求 MARK-U0"})
    for round_index in range(3):
        session["messages"].append({
            "role": "assistant", "content": None,
            "tool_calls": [_tool_call(f"c{round_index}", "probe_tool", '{"x": %d}' % round_index)],
        })
        session["messages"].append({
            "role": "tool", "tool_call_id": f"c{round_index}",
            "content": f"查询结果 MARK-R{round_index + 1} " + "数" * 180,
        })
    for index in range(4):
        session["messages"].append({"role": "user", "content": f"后续追问 {index} " + "问" * 80})
    runner.run(session=session, user_message="最新问题", system_prompt="SYS", max_steps=2, on_event=lambda event: None)

    window = session["messages"]
    assert window[0]["role"] == "user", "窗口剩余部分必须以 user 消息开头"
    assert _pairing_problems(window) == [], f"窗口内配对不完整：{_pairing_problems(window)}"
    archive = session.get("pending_archive") or []
    joined = "\n".join(archive)
    for round_index in range(1, 4):
        assert f"MARK-R{round_index}" in joined, f"移出窗口的 MARK-R{round_index} 必须仍能在长期记忆层找回"
    assert "MARK-U0" in joined, "最早的用户要求必须仍能在长期记忆层找回"


def test_archive_boundary_never_splits_a_pair(monkeypatch, caplog):
    """切点落在"调用—结果"配对中间，窗口头就是孤儿 tool 结果，请求必被上游拒绝。

    找不到安全切点（user 轮次边界）时宁可本轮不归档，也绝不产生非法窗口。
    """
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 3)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 50_000)
    runner = AgentRunner(ScriptedModel([]), _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"] = [
        {"role": "user", "content": "唯一的问题"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c0", "probe_tool", '{"x": 0}')]},
        {"role": "tool", "tool_call_id": "c0", "content": "结果 R0"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c1", "probe_tool", '{"x": 1}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果 R1"},
    ]
    with caplog.at_level(logging.WARNING, logger="astock_backtester.ai.agent"):
        runner._archive_overflow(session, lambda event: None)
    window = session["messages"]
    assert window[0]["role"] == "user", "窗口头必须仍是 user，不得切出孤儿 tool 结果"
    assert len(window) == 5, "找不到安全切点时不得归档任何条目"
    assert _pairing_problems(window) == []
    assert session.get("pending_archive") in (None, [])
    assert any("边界" in record.message for record in caplog.records), "放弃归档必须留 warning，不许静默"


# ---------------------------------------------------------------------------
# 组 2：pairing_exit —— 每次真实请求之前，协议历史必须合法
# ---------------------------------------------------------------------------

def test_dangling_calls_are_repaired_before_the_next_request(monkeypatch):
    """上次运行中断留下的悬空调用，必须在下一次真实请求前补齐配对结果。"""
    model = ScriptedModel([[_final(content="续上后的回答")]])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"] = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c0", "probe_tool", '{"x": 0}')]},
        {"role": "tool", "tool_call_id": "c0", "content": "value=0"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c1", "probe_tool", '{"x": 1}')]},
    ]
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    request = model.calls[0]["messages"]
    assert request[0]["role"] == "system"
    problems = _pairing_problems(request[1:])
    assert problems == [], f"发出的请求协议不合法：{problems}"
    tool_ids = [m.get("tool_call_id") for m in request[1:] if m.get("role") == "tool"]
    assert "c1" in tool_ids, "悬空调用 c1 必须有补齐的结果，而不是被丢给上游"


def test_archive_cut_never_ships_orphan_results(monkeypatch, caplog):
    """超限窗口在没有轮次边界可切时，不得把 tool 结果切成窗口头的孤儿。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 4)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 50_000)
    runner = AgentRunner(ScriptedModel([]), _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"] = [
        {"role": "user", "content": "开场"},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c0", "probe_tool", '{"x": 0}')]},
        {"role": "tool", "tool_call_id": "c0", "content": "结果 R0 " + "数" * 40},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c1", "probe_tool", '{"x": 1}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果 R1 " + "数" * 40},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call("c2", "probe_tool", '{"x": 2}')]},
    ]
    with caplog.at_level(logging.WARNING, logger="astock_backtester.ai.agent"):
        runner._archive_overflow(session, lambda event: None)
    window = session["messages"]
    assert window[0]["role"] == "user", f"窗口头不得是孤儿 tool 结果（实际 {window[0]['role']}）"
    assert len(window) == 6, "找不到安全切点时不得归档任何条目"
    assert session.get("pending_archive") in (None, []), "放弃归档时不得有内容被搬走"
    assert any("边界" in record.message for record in caplog.records), "放弃归档必须留 warning"


# ---------------------------------------------------------------------------
# 组 3：budget_effective_exit —— 整理之后，真实发送体积必须真的受控
# ---------------------------------------------------------------------------

def test_tidy_brings_real_payload_back_under_budget(monkeypatch):
    """"整理在跑、体积不减"的空转必须消失：整理后真实发送体积回到预算内。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 5)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 800)
    model = ScriptedModel([[_final(content="结论")]])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(6):
        session["messages"].append({"role": "user", "content": f"第 {index} 轮的长问题 " + "题" * 290})
    events: list[dict[str, Any]] = []
    runner.run(session=session, user_message="最新一问", system_prompt="SYS", max_steps=2, on_event=events.append)
    assert any(event.get("type") == "phase" and "归档" in str(event.get("phase", "")) for event in events), \
        "整理动作必须真的发生过（不能靠不整理过关）"
    request_window = model.calls[-1]["messages"][1:]
    size = _wire_chars(request_window)
    assert size <= 800, f"整理后真实发送体积 {size} 仍超预算（口径 = 正文 + 调用参数体）"


def test_budget_counts_tool_call_arguments(monkeypatch):
    """"看起来不大、每轮必败"的会话：调用参数体不进统计时，超限永远无人处理。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 8)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 600)
    model = ScriptedModel([[_final(content="结论")]])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].append({"role": "user", "content": "开场"})
    for round_index in range(2):
        session["messages"].append({
            "role": "assistant", "content": None,
            "tool_calls": [_tool_call(
                f"c{round_index}", "probe_tool",
                '{"x": %d, "filter": "%s"}' % (round_index, "参" * 400),
            )],
        })
        session["messages"].append({
            "role": "tool", "tool_call_id": f"c{round_index}",
            "content": "结果 " + "数" * 40,
        })
    session["messages"].append({"role": "user", "content": "中间一问"})
    runner.run(session=session, user_message="最新一问", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    request_window = model.calls[-1]["messages"][1:]
    size = _wire_chars(request_window)
    assert size <= 600, f"参数体必须计入预算口径：整理后真实体积 {size} 仍超限"


def test_tidy_keeps_model_calls_batched_per_turn(monkeypatch):
    """整理动作的压缩调用每轮至多一次（攒批语义），不许按条数放大模型调用。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 10)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 100_000)
    model = ScriptedModel([[_final(content="纪要内容")], [_final(content="结论")]])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    for index in range(60):
        session["messages"].append({"role": "user", "content": f"历史 {index} " + "史" * 40})
    runner.run(session=session, user_message="最新一问", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    compaction_calls = [c for c in model.calls if c["tools"] is None and len(c["messages"]) == 1]
    assert len(compaction_calls) == 1, f"压缩调用必须攒批成一次，实际 {len(compaction_calls)} 次"
    assert len(model.calls) <= 3, f"单轮模型调用总数异常：{len(model.calls)}"
    assert session["pending_archive"] == []
    assert session["rolling_summary"] == "纪要内容"


# ---------------------------------------------------------------------------
# 组 4：no_data_loss_exit —— 数据零丢失
# ---------------------------------------------------------------------------

def test_no_tool_result_is_lost_when_archiving(monkeypatch):
    """结果侧正文是数据本体：移出窗口时必须原文保留，不许只留调用名。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 4)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 50_000)
    runner = AgentRunner(ScriptedModel([]), _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    payloads = []
    messages: list[dict[str, Any]] = [{"role": "user", "content": "开场要求 MARK-U0"}]
    for round_index in range(3):
        call_id = f"c{round_index}"
        body = f"独家结果体 MARK-R{round_index + 1}-" + "据" * 120
        payloads.append(body)
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [_tool_call(call_id, "probe_tool", '{"x": %d}' % round_index)]})
        messages.append({"role": "tool", "tool_call_id": call_id, "content": body})
    messages.append({"role": "user", "content": "收尾追问"})
    session["messages"] = messages
    runner._archive_overflow(session, lambda event: None)
    archive = session.get("pending_archive") or []
    joined = "\n".join(archive)
    for body in payloads:
        assert body in joined, "结果正文必须原文进入长期记忆层（归档是搬运不是裁剪）"
    assert "MARK-U0" in joined


def test_compaction_failure_keeps_archive_and_cap_leaves_placeholder(monkeypatch):
    """压缩失败不得丢归档；归档条数兜底裁剪必须留下"已丢弃 N 条"占位说明。"""

    class ExplodingModel(ScriptedModel):
        def chat(self, messages, *, tools=None):
            self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
            raise RuntimeError("上游不可用")

    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_ENTRIES", 2)
    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_CHARS", 10)
    monkeypatch.setattr(agent_mod, "ARCHIVE_MAX_ENTRIES", 6)
    runner = AgentRunner(ExplodingModel([]), _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["pending_archive"] = ["归档条目甲", "归档条目乙"]
    runner._consolidate_archive(session, lambda event: None)
    assert session["pending_archive"] == ["归档条目甲", "归档条目乙"], "压缩失败时归档必须原样保留"
    assert session["rolling_summary"] == ""

    runner2 = AgentRunner(ScriptedModel([]), _make_registry(), ToolResultStore(), ContextBudget())
    session2 = _session()
    session2["messages"] = [
        {"role": "user", "content": f"第 {i} 轮 " + "z" * 60} for i in range(20)
    ]
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 2)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 50_000)
    runner2._archive_overflow(session2, lambda event: None)
    archive = session2["pending_archive"]
    assert len(archive) <= 6, "归档条数兜底必须生效"
    assert "已丢弃" in archive[0], "裁剪必须留下占位说明，不许静默丢弃"


# ---------------------------------------------------------------------------
# 组 5：recall_reference_exit —— 长期记忆召回语义保持严格
# ---------------------------------------------------------------------------

def test_recall_budget_stays_strict(tmp_path):
    """长期记忆召回的预算语义是硬的：整行累加、超限即停（对照锚，不许被放松）。"""
    store = MemoryStore(tmp_path)
    stamp = "2026-09-01T00:00:00+00:00"
    store.save([
        MemoryRecord(id=f"f{i:02d}", content="事实" * 100 + str(i), category="fact",
                     created_at=stamp, updated_at=stamp)
        for i in range(30)
    ])
    _profile, facts, injected_ids = store.recall()
    assert len(facts) <= FACTS_BUDGET_CHARS, f"事实段 {len(facts)} 字符超出预算 {FACTS_BUDGET_CHARS}"
    assert facts, "预算内至少要装下一条事实"
    rendered = len([line for line in facts.splitlines() if line.startswith("- ")])
    assert len(injected_ids) == rendered, "hit-boost 只能记真正渲染进上下文的记录"

    # 第二数据场景：小记录必须全部装下，不许"一刀切不截断"或"一刀切全丢"
    store.save([
        MemoryRecord(id=f"g{i:02d}", content=f"短事实{i}", category="fact",
                     created_at=stamp, updated_at=stamp)
        for i in range(8)
    ])
    _profile2, facts2, ids2 = store.recall()
    assert len(ids2) == 8, "预算内的小记录必须全部注入"
    assert all(f"短事实{i}" in facts2 for i in range(8))


def test_archived_findings_still_reach_the_summary_channel(monkeypatch):
    """被移出窗口的查询结果必须到达压缩通道（长期记忆的召回原料不能缺斤短两）。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 6)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 50_000)
    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_ENTRIES", 2)
    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_CHARS", 10)
    model = ScriptedModel([
        [_final(content="纪要：两轮查询的结论")],
        [_final(content="最终回答")],
    ])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].append({"role": "user", "content": "开场 MARK-U0"})
    for round_index in range(2):
        session["messages"].append({
            "role": "assistant", "content": None,
            "tool_calls": [_tool_call(f"c{round_index}", "probe_tool", '{"x": %d}' % round_index)],
        })
        session["messages"].append({
            "role": "tool", "tool_call_id": f"c{round_index}",
            "content": f"查询结果 MARK-R{round_index + 1}：" + "数" * 100,
        })
    session["messages"].append({"role": "user", "content": "收尾追问"})
    runner.run(session=session, user_message="最新一问", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    compaction = model.calls[0]
    assert compaction["tools"] is None, "第一笔模型调用必须是压缩调用"
    compaction_text = "".join(str(m.get("content") or "") for m in compaction["messages"])
    for marker in ("MARK-U0", "MARK-R1", "MARK-R2"):
        assert marker in compaction_text, f"{marker} 必须随归档进入压缩通道"
    assert session["rolling_summary"] == "纪要：两轮查询的结论"


# ---------------------------------------------------------------------------
# 组 6：coherence —— 三条承诺在同一组长会话里同时成立
# ---------------------------------------------------------------------------

def test_long_session_keeps_all_three_promises_at_once(monkeypatch):
    """三轮长会话端到端：记得的只多不少、发送体积受控、配对完整，缺一即红。"""
    monkeypatch.setattr(agent_mod, "SHORT_TERM_WINDOW", 8)
    monkeypatch.setattr(agent_mod, "SHORT_TERM_MAX_CHARS", 1_200)
    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_ENTRIES", 4)
    monkeypatch.setattr(agent_mod, "CONSOLIDATE_MIN_CHARS", 10)
    model = ScriptedModel([
        [_final(tool_calls=[_tool_call("c1", "probe_tool", '{"x": 1}')])],
        [_final(content="第一轮结论 MARK-A1 " + "论" * 40)],
        [_final(tool_calls=[_tool_call("c2", "probe_tool", '{"x": 2}')])],
        [_final(content="第二轮结论 MARK-A2 " + "论" * 40)],
        [_final(content="纪要：更早对话的关键结论")],
        [_final(tool_calls=[_tool_call("c3", "probe_tool", '{"x": 3}')])],
        [_final(content="第三轮结论 MARK-A3")],
    ])
    runner = AgentRunner(model, _make_registry(), ToolResultStore(), ContextBudget())
    session = _session()
    markers_emitted: list[str] = []
    compaction_inputs: list[str] = []

    for turn in range(1, 4):
        markers_emitted.append(f"MARK-Q{turn}")
        start = len(model.calls)
        runner.run(session=session, user_message=f"第 {turn} 轮问题 MARK-Q{turn}",
                   system_prompt="SYS", max_steps=2, on_event=lambda event: None)
        markers_emitted.extend([f"MARK-R{turn}", f"MARK-A{turn}"])
        for call in model.calls[start:]:
            if call["tools"] is None and len(call["messages"]) == 1:
                compaction_inputs.append("".join(str(m.get("content") or "") for m in call["messages"]))
        # 本轮的第一笔"带窗口请求"：压缩调用（单条 user）不算，找第一条带 system 的
        first_request = None
        for call in model.calls[start:]:
            if call["messages"] and call["messages"][0]["role"] == "system":
                first_request = call["messages"][1:]
                break
        assert first_request is not None, f"第 {turn} 轮没有发出带窗口的模型请求"
        assert first_request[0]["role"] == "user", f"第 {turn} 轮请求窗口必须以 user 开头"
        problems = _pairing_problems(first_request)
        assert problems == [], f"第 {turn} 轮请求协议不合法：{problems}"
        size = _wire_chars(first_request)
        assert size <= 1_200, f"第 {turn} 轮请求真实体积 {size} 超出预算"

    # 记得的只多不少：任何一个出现过的标记都必须仍能在
    # 窗口 ∪ 待归档区 ∪ 压缩通道输入里找回——不许凭空消失。
    kept = "\n".join(
        [*(str(m.get("content") or "") for m in session["messages"]),
         *compaction_inputs,
         *(str(item) for item in (session.get("pending_archive") or []))]
    )
    for marker in markers_emitted:
        assert marker in kept, f"{marker} 从会话里凭空消失了"
    # 整理动作的模型调用 O(1)/轮：压缩调用次数不得超过轮数。
    compactions = [c for c in model.calls if c["tools"] is None and len(c["messages"]) == 1]
    assert len(compactions) <= 3, f"压缩调用 {len(compactions)} 次超过轮数，攒批语义被破坏"
