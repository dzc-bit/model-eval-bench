"""T2-07 隐藏测试：一次"停止"必须同时守住三条协议。

"停止"不是一个开关，而是一份协议，三条边缺一不可：

* 取消边界——worker 只在安全边界（模型流的分片之间、工具提交之前、模型轮次
  之间）停下来；停止之后不再消费模型流、不再开新的模型调用，已生成的内容
  必须原样保留并带停止说明。
* 消息配对——assistant(tool_calls) 一旦落盘，被取消的那一批必须当场逐条补齐
  结果；停止后的历史必须直接合法，不能指望下一次运行开始时的修复兜底。
* 会话锁释放——停止收尾完成之后，会话锁必须立即可复用；紧接着的重发请求
  不许为上一次的收尾白等。

三个取消时点（模型流中 / 工具批中 / 收尾轮中）都要落到三条协议上：只修好其中
一个时点或一条协议，coherence 组仍然红。全部用事件/闸门对齐取消时点，
monkeypatch 锁等待超时控制白等时长——无真实网络、无长 sleep。
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

from astock_backtester.ai.agent import STOP_NOTE, AgentRunner
from astock_backtester.ai.cancel import CancelToken
from astock_backtester.ai.config import AiConfig
from astock_backtester.ai.context import ContextBudget, ToolResultStore
from astock_backtester.ai.facade import AiChatRequest, AiService
from astock_backtester.ai.models import AiChatRequest as _AiChatRequest  # noqa: F401  同一模型，显式对齐
from astock_backtester.ai.tools.registry import AiTool, ToolRegistry


# ---------------------------------------------------------------------------
# 通用夹具：脚本化模型 / 脚本化流式模型 / 工具表 / 会话
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """脚本化模型：按调用次序弹出脚本轮次，用完重复最后一轮（与仓库测试同款）。"""

    def __init__(self, script: list[list[tuple[str, Any]]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []
        self._last: list[tuple[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0) if self.script else self._last
        self._last = item
        yield from item

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


def _final(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None):
    return ("final", {"content": content, "tool_calls": tool_calls})


def _tool_call(call_id: str, name: str, arguments: str = "{}") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _session() -> dict[str, Any]:
    return {
        "session_id": "s1",
        "title": "新会话",
        "created_at": "now",
        "updated_at": "now",
        "rolling_summary": "",
        "messages": [],
        "display": [],
    }


def _registry_with(names: list[str], on_call=None) -> ToolRegistry:
    """注册一批未声明 read_only 的工具（必然串行执行，取消时点因此确定）。"""
    registry = ToolRegistry()
    for name in names:
        def run(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
            if on_call is not None:
                on_call(_name)
            return {"ok": True, "value": _name}

        registry.register(
            AiTool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                executor=run,
                summarizer=lambda payload: str(payload.get("value")),
            )
        )
    return registry


class _GatedStreamModel:
    """脚本化流式模型：每个分片可挂一道闸门，闸门未开就停在 yield 上。

    闸门保证"取消发生在流中"这件事是确定的，而不是靠 sleep 碰运气：
    测试在看到目标分片后取消并开闸，消费端在闸门开之前不可能读到后面的分片。
    """

    def __init__(self, chunks: list[str], gates: list[Any], final: dict[str, Any] | None = None) -> None:
        self.chunks = chunks
        self.gates = gates
        self.final = final
        self.calls = 0
        self.closed = 0
        self.final_reached = False
        self.consumed: list[str] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        self.calls += 1
        try:
            for chunk, gate in zip(self.chunks, self.gates):
                if gate is not None:
                    assert gate.wait(timeout=5), "闸门超时：消费端没有按预期推进"
                self.consumed.append(chunk)
                yield ("text", chunk)
            self.final_reached = True
            if self.final is None:
                yield ("final", {"content": "".join(self.chunks), "tool_calls": None})
            else:
                yield ("final", self.final)
        finally:
            self.closed += 1

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


# ---------------------------------------------------------------------------
# 组 1 · 取消边界：停止后不再消费模型流，已生成内容保留
# ---------------------------------------------------------------------------


def test_stop_during_stream_halts_consumption_and_keeps_partial():
    """流中取消：必须停在分片边界上，半截回答连同停止说明一起保留。"""
    token = CancelToken()
    resume = threading.Event()
    model = _GatedStreamModel(["上游", "涨", "停"], [None, resume, resume])
    runner = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())
    session = _session()

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "token" and event["text"] == "上游":
            token.cancel()
            resume.set()

    runner.run(
        session=session, user_message="说说盘面", system_prompt="sys",
        max_steps=5, on_event=on_event, cancel=token,
    )

    assert model.final_reached is False, "停止后模型流被读到了底：分片之间的取消边界没有生效"
    assert model.calls == 1, "停止后不得开新的模型调用"
    assert model.consumed == ["上游"], f"停止后仍在消费分片：{model.consumed}"
    assert STOP_NOTE in session["display"][-1]["content"], "停止说明必须写进展示层"
    assert session["display"][-1]["content"].startswith("上游"), "停止前已生成的部分丢了"
    assistant = [m for m in session["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"] == "上游", "协议历史里的半截回答必须保留模型原文"


def test_stop_late_in_stream_keeps_only_the_generated_prefix():
    """第二场景：五个分片、第三个分片处取消——只允许保留前三个分片。"""
    token = CancelToken()
    resume = threading.Event()
    chunks = ["甲", "乙", "丙", "丁", "戊"]
    # 闸门只挂在取消点之后的分片上：挂在取消触发分片本身会造成
    # "等闸门才发分片、等分片才开闸门"的死锁。
    model = _GatedStreamModel(chunks, [None, None, None, resume, resume])
    runner = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())
    session = _session()

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "token" and event["text"] == "丙":
            token.cancel()
            resume.set()

    runner.run(
        session=session, user_message="接着讲", system_prompt="sys",
        max_steps=5, on_event=on_event, cancel=token,
    )

    assert model.final_reached is False, "停止后模型流被读到了底"
    assert model.consumed == ["甲", "乙", "丙"], f"停止后仍在消费分片：{model.consumed}"
    assert STOP_NOTE in session["display"][-1]["content"]
    assistant = [m for m in session["messages"] if m["role"] == "assistant"]
    assert assistant[-1]["content"] == "甲乙丙"


def test_stop_during_stream_never_lands_the_trailing_tool_calls():
    """第三场景：流中取消且流尾带着 tool_calls——尾包不允许再落盘。"""
    token = CancelToken()
    resume = threading.Event()
    executed: list[str] = []
    model = _GatedStreamModel(
        ["第一段", "第二段"],
        [None, resume],
        final={"content": None, "tool_calls": [_tool_call("t9", "first_tool")]},
    )
    runner = AgentRunner(model, _registry_with(["first_tool"], on_call=executed.append),
                         ToolResultStore(), ContextBudget())
    session = _session()

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "token" and event["text"] == "第一段":
            token.cancel()
            resume.set()

    runner.run(
        session=session, user_message="边讲边查", system_prompt="sys",
        max_steps=5, on_event=on_event, cancel=token,
    )

    assert model.final_reached is False, "停止后模型流被读到了底，流尾 tool_calls 不该落盘"
    assert executed == [], "停止后不得再启动任何工具"
    dangling = [m for m in session["messages"] if m.get("tool_calls")]
    assert not dangling, "流中取消后，流尾的 tool_calls 不允许写进协议历史"
    assert STOP_NOTE in session["display"][-1]["content"]


# ---------------------------------------------------------------------------
# 组 2 · 消息配对：被取消批次当场逐条补齐，历史直接合法
# ---------------------------------------------------------------------------


def _cancelled_batch_session(batch: list[tuple[str, str]], cancel_on: str):
    token = CancelToken()
    executed: list[str] = []

    def on_call(name: str) -> None:
        executed.append(name)
        if name == cancel_on:
            token.cancel()

    registry = _registry_with([name for _cid, name in batch], on_call=on_call)
    model = _ScriptedModel(
        [
            [_final(tool_calls=[_tool_call(cid, name) for cid, name in batch])],
            [_final(content="不该出现")],
        ]
    )
    runner = AgentRunner(model, registry, ToolResultStore(), ContextBudget())
    session = _session()
    runner.run(
        session=session, user_message="查一批", system_prompt="sys",
        max_steps=5, on_event=lambda event: None, cancel=token,
    )
    return session, model, executed


def test_cancelled_tool_batch_pairs_every_call_in_place():
    """批中取消：没启动的槽位必须当场补 interrupted 结果，配对不能缺。"""
    session, model, executed = _cancelled_batch_session(
        [("c1", "first_tool"), ("c2", "second_tool")], cancel_on="first_tool"
    )

    assert executed == ["first_tool"], "取消后不得再启动批内剩余工具"
    tool_ids = [m.get("tool_call_id") for m in session["messages"] if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2"], f"每个 tool_call 都要有配对结果：{tool_ids}"
    contents = {m["tool_call_id"]: m["content"] for m in session["messages"] if m["role"] == "tool"}
    assert "[code=interrupted]" in contents["c2"], "没启动的槽位必须补'已中断'标记"
    assert "[code=interrupted]" not in contents["c1"]
    assert len(model.calls) == 1, "取消后不再进入下一轮"


def test_cancelled_three_tool_batch_pairs_every_remaining_call():
    """第二场景：三连批，第一个工具里取消——后两个槽位同样必须逐条补齐。"""
    session, model, executed = _cancelled_batch_session(
        [("c1", "first_tool"), ("c2", "second_tool"), ("c3", "third_tool")],
        cancel_on="first_tool",
    )

    assert executed == ["first_tool"]
    tool_ids = [m.get("tool_call_id") for m in session["messages"] if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2", "c3"], f"每个 tool_call 都要有配对结果：{tool_ids}"
    contents = {m["tool_call_id"]: m["content"] for m in session["messages"] if m["role"] == "tool"}
    assert "[code=interrupted]" in contents["c2"] and "[code=interrupted]" in contents["c3"]
    assert "[code=interrupted]" not in contents["c1"]
    assert len(model.calls) == 1


def test_cancelled_history_needs_no_repair_before_next_turn():
    """停止后的历史必须直接合法：修复阶段应当无事可做，而不是替停止收尾补洞。"""
    session, _model, _executed = _cancelled_batch_session(
        [("c1", "first_tool"), ("c2", "second_tool")], cancel_on="first_tool"
    )
    runner = AgentRunner(_ScriptedModel([]), _registry_with(["first_tool"]),
                         ToolResultStore(), ContextBudget())

    before = [dict(m) for m in session["messages"]]
    runner._repair_interrupted_turn(session)

    assert session["messages"] == before, "停止后的历史仍有配对缺口，靠下一轮修复兜底不合格"


# ---------------------------------------------------------------------------
# 修复机制本体回归（p2p，不进计分组）
# ---------------------------------------------------------------------------
# tests/test_ai_agent.py 里点名取消机制的守卫已整文件裁剪，修复机制本体的回归
# 由这两条隐藏 p2p 条目承担：注入不触碰修复机制，锚解 / 半成品 / 注入态下都
# 必须保持绿；若模型删掉修复机制来"修"配对，这两条红 → 本轮作废。


def test_repair_synthesizes_placeholders_for_dangling_calls():
    """上一轮中断留下的悬空 tool_calls，在下一轮开始前必须补上占位结果。"""
    model = _ScriptedModel([[_final(content="续上：结论")]])
    runner = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].extend(
        [
            {"role": "user", "content": "上一轮问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("t1", "first_tool"), _tool_call("t2", "first_tool")],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "value=first_tool"},
            # t2 的结果缺失（上一轮运行在中途被打断）
        ]
    )
    events: list[dict[str, Any]] = []
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=2, on_event=events.append)

    tool_messages = [message for message in session["messages"] if message.get("role") == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[1]["tool_call_id"] == "t2"
    assert "中断" in tool_messages[1]["content"]
    # 修复后的消息紧跟在 assistant(tool_calls) 之后，顺序有效
    request_messages = model.calls[0]["messages"]
    roles = [message["role"] for message in request_messages]
    assert roles.index("tool") > roles.index("assistant")


def test_repair_keeps_intact_history_unchanged():
    """配对完整的历史不得被修复改写。"""
    model = _ScriptedModel([[_final(content="答案")]])
    runner = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())
    session = _session()
    session["messages"].extend(
        [
            {"role": "user", "content": "问题"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("t1", "first_tool")],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "value=first_tool"},
        ]
    )
    before = [dict(message) for message in session["messages"]]
    runner.run(session=session, user_message="继续", system_prompt="SYS", max_steps=2, on_event=lambda event: None)
    assert session["messages"][:3] == before


# ---------------------------------------------------------------------------
# 会话侧夹具：真 facade + 真 AgentRunner / 桩 agent，事件线程消费
# ---------------------------------------------------------------------------


def _build_service(tmp_path) -> AiService:
    service = AiService(
        cache_dir=str(tmp_path / "本地数据仓"),
        backend=SimpleNamespace(),
        log=lambda *args, **kwargs: None,
    )
    # 回环不可达端口：背景任务若有模型调用会立刻失败并被吞掉，不碰真实网络。
    service._config_store.save(AiConfig(base_url="http://127.0.0.1:9", api_key="sk-test", model="demo"))
    return service


class _CancelBlockingAgent:
    """卡在生成中的轮次：收到取消才收尾（轮询等待，与既有 HTTP 用例同款）。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.blocking = True
        self.runs = 0

    def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event, cancel=None):
        self.runs += 1
        self.entered.set()
        if not self.blocking:
            session["display"].append({"role": "assistant", "content": f"已收到：{user_message}", "tool_steps": [], "ts": "now"})
            return {}
        deadline = time.monotonic() + 5
        while not (cancel is not None and cancel.cancelled) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cancel is not None and cancel.cancelled, "取消信号没有传到 worker"
        on_event({"type": "token", "text": "已生成的部分"})
        session["display"].append({"role": "assistant", "content": "已生成的部分", "tool_steps": [], "ts": "now"})
        return {}


def _consume(stream, sink: list, errors: list) -> threading.Thread:
    def run() -> None:
        try:
            for event in stream:
                sink.append(event)
        except BaseException as exc:  # noqa: BLE001 - 交给主线程断言
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _assert_lock_released(service: AiService, session_id: str) -> None:
    entry = service._session_locks[session_id]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and entry.lock.locked():
        time.sleep(0.01)
    assert not entry.lock.locked(), "停止收尾完成后会话锁仍被占用（下一次请求将白等到底）"
    assert entry.refs == 0, f"会话锁引用计数没有归还：refs={entry.refs}"


def _assert_reusable(service: AiService, session_id: str) -> None:
    """停止之后紧跟着重发：必须立刻进入生成并拿到终态，不许白等。"""
    _assert_lock_released(service, session_id)
    agent = service._agent
    if hasattr(agent, "blocking"):
        agent.blocking = False
    sink: list = []
    errors: list = []
    thread = _consume(
        service.chat_stream(AiChatRequest(message="下一条", session_id=session_id)), sink, errors
    )
    thread.join(timeout=10)
    assert not errors, f"停止后立刻重发失败（白等后判忙）：{errors}"
    assert sink and sink[-1]["type"] == "result", f"重发请求没有拿到终态：{sink[-1:]}"


def _wait_for(sink: list, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not any(event.get("type") and predicate(event) for event in sink):
        time.sleep(0.005)
    assert any(predicate(event) for event in sink), f"等待事件超时：{sink[-3:]}"


class _GatedScriptedModel:
    """脚本化模型（facade 用）：事件可挂闸门；脚本用完后补一轮普通收尾回答。"""

    def __init__(self, script: list[list[tuple[str, Any]]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []
        # 每次模型调用对应一项：这一轮是否被完整消费（在某个 yield 上被关断则为 False）
        self.consumed_fully: list[bool] = []
        self._last: list[tuple[str, Any]] = []

    def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None):
        self.calls.append({"tools": tools})
        item = self.script.pop(0) if self.script else self._last
        self._last = item
        self.consumed_fully.append(False)
        try:
            for event in item:
                gate = event[2] if len(event) > 2 else None
                if gate is not None:
                    assert gate.wait(timeout=5), "闸门超时：消费端没有按预期推进"
                yield (event[0], event[1])
            # 走到这里说明整轮被消费完（被关断时 GeneratorExit 会跳过这一步）
            self.consumed_fully[-1] = True
        finally:
            pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


def _text_event(text: str, gate=None):
    return ("text", text, gate)


def _final_event(content=None, tool_calls=None, gate=None):
    return ("final", {"content": content, "tool_calls": tool_calls}, gate)


# ---------------------------------------------------------------------------
# 组 3 · 会话锁释放：停止收尾完成后锁立即可复用
# ---------------------------------------------------------------------------


def test_session_lock_reusable_right_after_a_cancelled_turn(tmp_path, monkeypatch):
    import astock_backtester.ai.facade as facade_module

    # 把锁等待上限压到 0.2 秒：注入态下白等会立刻显形成 AiSessionBusy。
    monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.2)
    service = _build_service(tmp_path)
    agent = _CancelBlockingAgent()
    service._agent = agent

    sink: list = []
    errors: list = []
    thread = _consume(service.chat_stream(AiChatRequest(message="长任务")), sink, errors)
    assert agent.entered.wait(timeout=5), "worker 没有进入生成"
    session_id = service.list_sessions()[0]["session_id"]
    assert service.cancel_turn(session_id) == {"ok": True, "cancelling": True}
    thread.join(timeout=5)
    assert not thread.is_alive(), "停止后事件流没有收尾"
    assert not errors, errors
    assert sink[-1]["type"] == "result", "协作停止也要送出终态"

    _assert_reusable(service, session_id)
    assert agent.runs == 2, "重发请求没有进入第二次生成"


def test_repeated_cancels_never_stack_lock_waits(tmp_path, monkeypatch):
    """第二场景：连续两轮"停止 → 重发"，白等不许出现、更不许累积。"""
    import astock_backtester.ai.facade as facade_module

    monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.2)
    service = _build_service(tmp_path)
    agent = _CancelBlockingAgent()
    service._agent = agent
    session_id = None

    for round_index in range(2):
        agent.blocking = True
        agent.entered.clear()
        request_id = None if round_index == 0 else session_id
        sink: list = []
        errors: list = []
        thread = _consume(
            service.chat_stream(AiChatRequest(message=f"长任务 {round_index}", session_id=request_id)),
            sink, errors,
        )
        assert agent.entered.wait(timeout=5), "worker 没有进入生成"
        if session_id is None:
            session_id = sink[0]["session_id"]
        assert service.cancel_turn(session_id)["cancelling"] is True
        thread.join(timeout=5)
        assert not thread.is_alive() and not errors, errors
        assert sink[-1]["type"] == "result"
        _assert_reusable(service, session_id)

    entry = service._session_locks[session_id]
    assert not entry.lock.locked() and entry.refs == 0


# ---------------------------------------------------------------------------
# 组 4 · coherence：三个取消时点 × 三条协议（流中 / 工具批中 / 收尾轮中）
# ---------------------------------------------------------------------------


def test_stop_mid_stream_leaves_session_valid_and_reusable(tmp_path, monkeypatch):
    """时点一（模型流中）：停止 → 内容保留、说明落盘、锁立即可复用。"""
    import astock_backtester.ai.facade as facade_module

    monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.2)
    service = _build_service(tmp_path)
    resume = threading.Event()
    model = _GatedScriptedModel(
        [
            [
                _text_event("第一段"),
                _text_event("第二段", resume),
                _text_event("第三段", resume),
                _final_event("第一段第二段第三段"),
            ]
        ]
    )
    service._agent = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())

    sink: list = []
    errors: list = []
    thread = _consume(service.chat_stream(AiChatRequest(message="讲讲盘面")), sink, errors)
    _wait_for(sink, lambda event: event.get("type") == "token")
    session_id = sink[0]["session_id"]
    assert service.cancel_turn(session_id)["cancelling"] is True
    resume.set()
    thread.join(timeout=10)
    assert not errors, errors
    assert sink[-1]["type"] == "result", "协作停止也要送出终态"

    stored = service._sessions.get(session_id)
    tail = str(stored["display"][-1].get("content") or "")
    assert STOP_NOTE in tail, "停止说明丢了"
    assert tail.startswith("第一段"), "停止前已生成的部分丢了"
    assert "第三段" not in json.dumps(stored["display"], ensure_ascii=False), "停止后仍在继续生成"
    assert model.consumed_fully == [False], (
        f"模型流被读到了底：分片之间的取消边界没有生效（{model.consumed_fully}）"
    )
    protocol_tail = [m for m in stored["messages"] if m.get("role") == "assistant"][-1]
    assert str(protocol_tail.get("content") or "").startswith("第一段"), "协议历史里的半截回答丢了"

    _assert_reusable(service, session_id)


def test_stop_in_tool_batch_leaves_session_valid_and_reusable(tmp_path, monkeypatch):
    """时点二（工具批中）：停止 → 配对当场补齐、不再开新轮次、锁立即可复用。"""
    import astock_backtester.ai.facade as facade_module

    monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.2)
    service = _build_service(tmp_path)
    executed: list[str] = []

    def on_call(name: str) -> None:
        executed.append(name)
        if name == "first_tool":
            keys = service._turns.active_keys()
            assert len(keys) == 1, "同一时刻只应有一个在途轮次"
            service.cancel_turn(keys[0])

    batch_calls = [
        {"id": "c1", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "second_tool", "arguments": "{}"}},
    ]
    model = _GatedScriptedModel([[_final_event(None, tool_calls=batch_calls)]])
    service._agent = AgentRunner(
        model, _registry_with(["first_tool", "second_tool"], on_call), ToolResultStore(), ContextBudget()
    )

    sink: list = []
    errors: list = []
    thread = _consume(service.chat_stream(AiChatRequest(message="跑一批工具")), sink, errors)
    thread.join(timeout=10)
    assert not errors, errors
    session_id = sink[0]["session_id"]
    assert sink[-1]["type"] == "result", "协作停止也要送出终态"

    stored = service._sessions.get(session_id)
    tool_ids = [m["tool_call_id"] for m in stored["messages"] if m.get("role") == "tool"]
    assert tool_ids == ["c1", "c2"], f"被取消批次的每个调用都必须当场配对：{tool_ids}"
    contents = {m["tool_call_id"]: m["content"] for m in stored["messages"] if m.get("role") == "tool"}
    assert "[code=interrupted]" in contents["c2"], "没启动的槽位必须补'已中断'标记"
    assert "[code=interrupted]" not in contents["c1"]
    assert executed == ["first_tool"], "取消后不得再启动批内剩余工具"
    assert len(model.calls) == 1, "取消后不得再开新的模型轮次"
    assert STOP_NOTE in str(stored["display"][-1].get("content") or ""), "停止说明必须写进展示层"

    _assert_reusable(service, session_id)


def test_stop_between_rounds_leaves_session_valid_and_reusable(tmp_path, monkeypatch):
    """时点三（收尾轮中）：工具结果已回、下一轮刚开始时停止——同样全协议成立。"""
    import astock_backtester.ai.facade as facade_module

    monkeypatch.setattr(facade_module, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.2)
    service = _build_service(tmp_path)
    resume = threading.Event()
    model = _GatedScriptedModel(
        [
            [_final_event(None, tool_calls=[{"id": "c1", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}}])],
            [
                _text_event("收尾段", resume),
                _text_event("收尾尾", resume),
                _final_event("收尾段收尾尾"),
            ],
        ]
    )
    service._agent = AgentRunner(model, _registry_with(["first_tool"]), ToolResultStore(), ContextBudget())

    sink: list = []
    errors: list = []
    thread = _consume(service.chat_stream(AiChatRequest(message="先查一下再总结")), sink, errors)
    _wait_for(sink, lambda event: event.get("type") == "tool_result" and event.get("id") == "c1")
    session_id = sink[0]["session_id"]
    assert service.cancel_turn(session_id)["cancelling"] is True
    resume.set()
    thread.join(timeout=10)
    assert not errors, errors
    assert sink[-1]["type"] == "result", "协作停止也要送出终态"

    stored = service._sessions.get(session_id)
    tool_ids = [m["tool_call_id"] for m in stored["messages"] if m.get("role") == "tool"]
    assert tool_ids == ["c1"], f"已执行批次的配对必须完整：{tool_ids}"
    tail = str(stored["display"][-1].get("content") or "")
    assert STOP_NOTE in tail, "停止说明丢了"
    assert "收尾尾" not in json.dumps(stored["display"], ensure_ascii=False), "停止后仍在继续生成"
    # 要么取消落在轮间（根本没有第二轮调用），要么收尾轮在某个分片上被关断，
    # 二者必居其一；把收尾轮读到底就是没有命中任何安全边界。
    assert len(model.calls) == 1 or model.consumed_fully[1] is False, (
        f"收尾轮被读到了底：停止没有命中任何安全边界（{model.consumed_fully}）"
    )

    _assert_reusable(service, session_id)
