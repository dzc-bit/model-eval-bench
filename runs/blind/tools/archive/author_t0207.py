"""T2-07 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

三端口（协作取消三协议，全部按 reference/notes.md 草案落点）：
  ① 取消边界不查   agent.py  模型流 token 之间的取消检查删除（长流停止后照常写完）
                   + agent.py/cancel.py 相关 docstring 同步改写成自然口径
  ② 不补配对       agent.py  _execute_tool_calls 不给被取消槽位补 interrupted 结果
                   （跳过未启动槽位，缺口"留给下一轮修复"）+ _invoke docstring 同步改写
  ③ 锁释放路径漏掉 facade.py worker finally 里被停止的轮次不再释放会话锁
                   （"锁留给下一次请求去等"——等来的却是 90 秒超时）

产出（写进 packs/core/tasks/T2-07/）：
  inject/patches/0001-agent.patch / 0002-cancel.patch / 0003-facade.patch
  reference/fix.patch     三端口全修齐（含 cancel.py docstring 还原）
  reference/partial.patch 只修配对端口（演示"只修配对 → 边界/锁两组仍红"）
  hidden/tests_hidden/test_collab_cancel.py + hidden/groups.json
  p2p.json                三文件候选 → 基线探针剔除基线红 → 注入探针剔除注入红
  prompts/1.md 2.md 3.md、calibration/results.json、meta.json、reference/notes.md
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T2-07"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

AGENT_REL = "backend/astock_backtester/ai/agent.py"
CANCEL_REL = "backend/astock_backtester/ai/cancel.py"
FACADE_REL = "backend/astock_backtester/ai/facade.py"

# 点名取消机制的可见守卫用例：无论注入后红绿都裁剪（§4.2 第 1 层）。
# tests/test_ai_agent.py 的 5 条取消守卫（:476/:502/:546/:603/:647）与修复机制
# 本体同住一个文件；harness 的按例裁剪对它不可用（文本嗅探按前 4096 字节判定，
# 该文件在字节 4094 处切中多字节字符被判为二进制而整文件跳过），因此按整文件
# 裁剪；修复机制本体的回归改由隐藏层 p2p 条目承担（见下文 hidden 夹具）。
NAME_POINTING_PRUNE = {
    "tests/test_ai_agent.py",
    "tests/test_ai_service_http.py::test_ai_chat_cancel_stops_the_worker_and_releases_the_session",
}

# 修复机制本体的 p2p 回归（镜像被整文件裁剪的既有守卫 :181/:215）：
# 注入不触碰修复机制，三条门禁状态下都必须绿；模型若删掉修复机制来"修"配对，
# 这两条会红 → 本轮作废。
REPAIR_P2P_NODES = [
    "hidden/tests_hidden/test_collab_cancel.py::test_repair_synthesizes_placeholders_for_dangling_calls",
    "hidden/tests_hidden/test_collab_cancel.py::test_repair_keeps_intact_history_unchanged",
]


def must_replace(text: str, old: str, new: str, tag: str) -> str:
    assert old in text, f"{tag}: 原文未命中\n---\n{old}\n---"
    assert text.count(old) == 1, f"{tag}: 原文命中多次"
    return text.replace(old, new, 1)


# --------------------------------------------------------------------------
# 一、注入变体（端口① 取消边界 + 端口② 不补配对 + docstring 同步改写）
# --------------------------------------------------------------------------

agent_src = (REPO / AGENT_REL).read_text(encoding="utf-8")
cancel_src = (REPO / CANCEL_REL).read_text(encoding="utf-8")
facade_src = (REPO / FACADE_REL).read_text(encoding="utf-8")

# --- 端口①：删掉模型流 token 之间的取消检查 ---
agent_injected = must_replace(
    agent_src,
    """                for event in stream:
                    if event[0] == "text":
                        content_parts.append(event[1])
                        on_event({"type": "token", "text": event[1]})
                        if token.cancelled:
                            return self._finish_cancelled(
                                session, artifacts, on_event, partial="".join(content_parts)
                            )
                        continue
                    turn = event[1]""",
    """                for event in stream:
                    if event[0] == "text":
                        content_parts.append(event[1])
                        on_event({"type": "token", "text": event[1]})
                        continue
                    turn = event[1]""",
    "端口①取消边界",
)
# --- 端口①：run() docstring 同步改写（不再声称检查 token 流之间） ---
agent_injected = must_replace(
    agent_injected,
    """        ``cancel`` 是协作停止信号：只在安全边界查询（轮次之间、模型流的 token
        之间、每个工具启动之前），已经开始的工具会跑完它自己那一步。""",
    """        ``cancel`` 是协作停止信号：只在安全边界查询（轮次之间、每个工具
        启动之前），已经开始的工具会跑完它自己那一步。""",
    "端口①docstring",
)

# --- 端口②：被取消槽位不再补 interrupted 结果（跳过，缺口"留给下一轮修复"） ---
agent_injected = must_replace(
    agent_injected,
    """        for plan, execution in zip(plans, executions, strict=True):
            if execution is None:
                # 取消后未启动的工具：立刻补一条 interrupted 结果，配对不能留到下次修复。
                execution = ToolExecution(
                    ok=False,
                    payload={"ok": False, "error_code": CODE_INTERRUPTED, "error": "用户已停止，未执行该工具。"},
                    summary="用户已停止，本次未执行该工具。",
                    duration_ms=0,
                    code=CODE_INTERRUPTED,
                )
            self._result_store.put(""",
    """        for plan, execution in zip(plans, executions, strict=True):
            if execution is None:
                # 被停止而没启动的槽位拿不到结果：跳过即可，缺口留给下一轮
                # 运行开始时的修复逻辑补齐。
                continue
            self._result_store.put(""",
    "端口②不补配对",
)
# --- 端口②：_invoke docstring 同步改写 ---
agent_injected = must_replace(
    agent_injected,
    """        取消后**不再启动**新工具（槽位返回 ``None``，由调用方写成 interrupted 结
        果）；已经提交的任务不杀线程，让它跑完自己的安全边界——半途杀线程会
        留下写到一半的数据分区。""",
    """        取消后**不再启动**新工具（槽位返回 ``None``）；已经提交的任务不杀线程，
        让它跑完自己的安全边界——半途杀线程会留下写到一半的数据分区。未启动的
        槽位不写结果，缺口留给下一轮运行开始时的修复兜底。""",
    "端口②docstring",
)
# --- 端口②：两个只为补结果服务的名字不再被使用，导入行同步收窄 ---
agent_injected = must_replace(
    agent_injected,
    "from astock_backtester.ai.tools.registry import CODE_INTERRUPTED, ToolExecution, ToolRegistry",
    "from astock_backtester.ai.tools.registry import ToolRegistry",
    "端口②导入行",
)

# --- 端口③：被停止的轮次不释放会话锁（"留给下一次请求去等"） ---
facade_injected = must_replace(
    facade_src,
    """                        release_turn()
                        events.put(None)
                        # 会话锁由 worker 释放：客户端断开（停止按钮）后 worker 仍在跑，
                        # 提前释放会让下一个请求与它并发写同一会话。
                        release_session_lock()""",
    """                        release_turn()
                        events.put(None)
                        # 会话锁由 worker 释放：客户端断开（停止按钮）后 worker 仍在跑，
                        # 提前释放会让下一个请求与它并发写同一会话。被停止的轮次
                        # 例外：锁留在原地，让紧跟着的重发请求一定排在这轮收尾后面。
                        if not cancel_token.cancelled:
                            release_session_lock()""",
    "端口③锁释放",
)

# --- 端口①连带：cancel.py 模块 docstring 同步改写（安全边界清单里去掉 token 流之间） ---
cancel_injected = must_replace(
    cancel_src,
    """停止不是杀线程、也不是只断开前端。worker 只在**安全边界**查询令牌——模型轮次
之间、模型流的 token 之间、工具批次里每个工具启动之前、寻优的每个组合之间——
已经开始的写入跑到边界再退出，会话保存与会话锁释放都发生在退出之后。因此令牌
必须比 worker 活得久一点，也不能被两个轮次共用。""",
    """停止不是杀线程、也不是只断开前端。worker 只在**安全边界**查询令牌——模型轮次
之间、工具批次里每个工具启动之前、寻优的每个组合之间——已经开始的写入跑到
边界再退出，会话保存与会话锁释放都发生在退出之后。因此令牌必须比 worker 活得
久一点，也不能被两个轮次共用。""",
    "端口①cancel.py docstring",
)

# --------------------------------------------------------------------------
# 二、锚解变体与半成品变体
# --------------------------------------------------------------------------

# 锚解 = 三端口全部还原（即受测仓库原状）
agent_fixed = agent_src
cancel_fixed = cancel_src
facade_fixed = facade_src

# 半成品 = 只修配对端口（端口②还原，端口①③保持注入态）
agent_partial = must_replace(
    agent_injected,
    """        for plan, execution in zip(plans, executions, strict=True):
            if execution is None:
                # 被停止而没启动的槽位拿不到结果：跳过即可，缺口留给下一轮
                # 运行开始时的修复逻辑补齐。
                continue
            self._result_store.put(""",
    """        for plan, execution in zip(plans, executions, strict=True):
            if execution is None:
                # 取消后未启动的工具：立刻补一条 interrupted 结果，配对不能留到下次修复。
                execution = ToolExecution(
                    ok=False,
                    payload={"ok": False, "error_code": CODE_INTERRUPTED, "error": "用户已停止，未执行该工具。"},
                    summary="用户已停止，本次未执行该工具。",
                    duration_ms=0,
                    code=CODE_INTERRUPTED,
                )
            self._result_store.put(""",
    "半成品·还原配对",
)
agent_partial = must_replace(
    agent_partial,
    """        取消后**不再启动**新工具（槽位返回 ``None``）；已经提交的任务不杀线程，
        让它跑完自己的安全边界——半途杀线程会留下写到一半的数据分区。未启动的
        槽位不写结果，缺口留给下一轮运行开始时的修复兜底。""",
    """        取消后**不再启动**新工具（槽位返回 ``None``，由调用方写成 interrupted 结
        果）；已经提交的任务不杀线程，让它跑完自己的安全边界——半途杀线程会
        留下写到一半的数据分区。""",
    "半成品·还原docstring",
)
agent_partial = must_replace(
    agent_partial,
    "from astock_backtester.ai.tools.registry import ToolRegistry",
    "from astock_backtester.ai.tools.registry import CODE_INTERRUPTED, ToolExecution, ToolRegistry",
    "半成品·还原导入行",
)

# --------------------------------------------------------------------------
# 三、补丁产出（每文件一个 patch）
# --------------------------------------------------------------------------


def lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE.mkdir(parents=True, exist_ok=True)

(INJECT_DIR / "0001-agent.patch").write_text(
    build_patch(AGENT_REL, lines(agent_src), lines(agent_injected)), encoding="utf-8"
)
(INJECT_DIR / "0002-cancel.patch").write_text(
    build_patch(CANCEL_REL, lines(cancel_src), lines(cancel_injected)), encoding="utf-8"
)
(INJECT_DIR / "0003-facade.patch").write_text(
    build_patch(FACADE_REL, lines(facade_src), lines(facade_injected)), encoding="utf-8"
)

fix_parts = []
if agent_injected != agent_fixed:
    fix_parts.append(build_patch(AGENT_REL, lines(agent_injected), lines(agent_fixed)))
if cancel_injected != cancel_fixed:
    fix_parts.append(build_patch(CANCEL_REL, lines(cancel_injected), lines(cancel_fixed)))
if facade_injected != facade_fixed:
    fix_parts.append(build_patch(FACADE_REL, lines(facade_injected), lines(facade_fixed)))
(REFERENCE / "fix.patch").write_text("".join(fix_parts), encoding="utf-8")

(REFERENCE / "partial.patch").write_text(
    build_patch(AGENT_REL, lines(agent_injected), lines(agent_partial)), encoding="utf-8"
)
print("补丁已生成")

# 注入点行号（notes.md 注入点表用）
def line_of(text: str, needle: str) -> int:
    index = text.find(needle)
    assert index >= 0, f"行号定位失败：{needle[:40]}"
    return text.count("\n", 0, index) + 1


INJECT_LINES = {
    "agent_stream_check": line_of(agent_src, 'on_event({"type": "token", "text": event[1]})'),
    "agent_backfill": line_of(agent_src, "if execution is None:"),
    "agent_invoke_doc": line_of(agent_src, "由调用方写成 interrupted 结"),
    "agent_import": line_of(agent_src, "from astock_backtester.ai.tools.registry import CODE_INTERRUPTED"),
    "agent_run_doc": line_of(agent_src, "只在安全边界查询（轮次之间、模型流的 token"),
    "cancel_doc": line_of(cancel_src, "之间、模型流的 token 之间、工具批次里每个工具启动之前"),
    "facade_release": line_of(facade_src, "release_session_lock()\n                if error_holder:"),
}

# --------------------------------------------------------------------------
# 四、隐藏测试（三取消时点 × 三协议矩阵）
# --------------------------------------------------------------------------

HIDDEN = TASK / "hidden" / "tests_hidden"
HIDDEN.mkdir(parents=True, exist_ok=True)

HIDDEN_TEST_PY = '''"""T2-07 隐藏测试：一次"停止"必须同时守住三条协议。

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

    model = _GatedScriptedModel(
        [[_final_event(None, tool_calls=[_tc := {"id": "c1", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}},
                                   {"id": "c2", "type": "function", "function": {"name": "second_tool", "arguments": "{}"}}])]]
    )
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
'''

# 上面写死了一个海象表达式，为了可读性改回普通结构：
HIDDEN_TEST_PY = HIDDEN_TEST_PY.replace(
    """    model = _GatedScriptedModel(
        [[_final_event(None, tool_calls=[_tc := {"id": "c1", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}},
                                   {"id": "c2", "type": "function", "function": {"name": "second_tool", "arguments": "{}"}}])]]
    )""",
    """    batch_calls = [
        {"id": "c1", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "second_tool", "arguments": "{}"}},
    ]
    model = _GatedScriptedModel([[_final_event(None, tool_calls=batch_calls)]])""",
)

(HIDDEN / "test_collab_cancel.py").write_text(HIDDEN_TEST_PY, encoding="utf-8")

(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-07",
    "note": "pytest 侧分组。组 = 一条取消协议的独立出口；coherence 组权重最高，把三个取消时点（模型流中 / 工具批中 / 收尾轮中）各自压过全部三条协议——只修一个时点或一条协议，它仍然红。",
    "groups": [
        {
            "id": "cancel_boundary_exit",
            "weight": 1,
            "port": "取消边界：停止后不再消费模型流、不再开新的模型调用，已生成内容保留并带停止说明",
            "tests": [
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_during_stream_halts_consumption_and_keeps_partial",
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_late_in_stream_keeps_only_the_generated_prefix",
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_during_stream_never_lands_the_trailing_tool_calls",
            ],
        },
        {
            "id": "pairing_exit",
            "weight": 1,
            "port": "配对协议：被取消批次当场逐条补齐结果，停止后的历史直接合法（不靠下一轮修复兜底）",
            "tests": [
                "hidden/tests_hidden/test_collab_cancel.py::test_cancelled_tool_batch_pairs_every_call_in_place",
                "hidden/tests_hidden/test_collab_cancel.py::test_cancelled_three_tool_batch_pairs_every_remaining_call",
                "hidden/tests_hidden/test_collab_cancel.py::test_cancelled_history_needs_no_repair_before_next_turn",
            ],
        },
        {
            "id": "lock_release_exit",
            "weight": 1,
            "port": "锁释放：停止收尾完成后会话锁立即可复用，重发请求不白等（连续停止也不累积）",
            "tests": [
                "hidden/tests_hidden/test_collab_cancel.py::test_session_lock_reusable_right_after_a_cancelled_turn",
                "hidden/tests_hidden/test_collab_cancel.py::test_repeated_cancels_never_stack_lock_waits",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "跨时点一致性：流中 / 工具批中 / 收尾轮中三个时点停止后，会话都处于一致且立即可用的状态",
            "tests": [
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_mid_stream_leaves_session_valid_and_reusable",
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_in_tool_batch_leaves_session_valid_and_reusable",
                "hidden/tests_hidden/test_collab_cancel.py::test_stop_between_rounds_leaves_session_valid_and_reusable",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "既有用例白名单见任务根 p2p.json。任一条红 → 本轮作废（0 分）。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------------------------------------------------------
# 五、提示词 / 校准 / meta
# --------------------------------------------------------------------------

PROMPTS = TASK / "prompts"
PROMPTS.mkdir(exist_ok=True)

(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

这个助手的"停止"按钮最近变得很不对劲，三种表现：

1. **停不下来**。回答生成到一半点停止，字还在继续往外蹦，该调的工具也接着调，
   要等它自己把整轮跑完才安静下来；界面上也不再出现"本轮已被停止"的说明。
2. **残缺的记录**。有时停止之后，会话里会留下"发起了一个工具调用、却没有结果"
   的残缺记录；下一次提问时助手的行为变得不可预测，甚至像忘了之前聊到哪。
3. **再问白等**。停止之后紧接着再提问，要干等一分多钟才有反应，期间没有任何
   "正在收尾"的提示。停止用得越频繁，白等越常见。

## 验收要求

修好之后，下面几条必须同时成立：

- 停止是**协作式**的：点停止后，生成必须在安全边界停下来，停止前已经生成的
  内容要原样保留在对话记录里（带停止说明），不能再继续往外生成；
- 已发出的工具调用要么有结果、要么有明确的"已中断"标记——停止之后的历史必须
  直接就是完整的，不能指望下一次提问时再偷偷修补；
- 停止的收尾一旦完成，会话马上就能再用：紧接着的重发请求不许为上一次的收尾
  付出任何等待，连续多次停止也一样。

不要通过丢弃已生成的内容、或者把"停止"改成同步等收尾来让症状消失。请把根因
修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把"停止"从按下到下一次提问走一遍，会发现它被拆成了三段，各自为政：

1. **生成侧的停**：停止信号只在某些环节被查看。模型正在往外吐字的时候没有人
   看这个信号——整段流被读到底，停止说明也不会写进记录；
2. **记录侧的补**：一轮回答里助手可以发起一批工具调用。停止打断这一批时，
   已落盘的调用清单和它们的结果之间没有人对账——残缺的配对就那么存进会话，
   等到下一次提问才被偷偷补上（或者根本补不上）；
3. **占用侧的放**：每一轮生成期间会占住一把"会话正在使用"的锁。正常跑完的
   轮次会把锁还掉；被停止的轮次却把锁留在了原地——持有它的收尾早已结束，
   再也没有人来还，下一次提问只能干等到底。

三段单独看都"各有一套做法"，合在一起就是：停止之后，会话既不完整、也不合法、
还不可用。
''', encoding="utf-8")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时成立的表述：

1. 停止信号必须在每一个安全边界都被查看：模型流的分片之间、一批工具调用提交
   之前、模型轮次之间。停止之后不允许再消费任何模型流、不允许再开新的模型
   调用或新工具；
2. 停止那一刻已经生成的内容（含半截回答）必须原样保留在协议历史与展示层，
   并带停止说明；允许收尾，不允许覆盖或丢弃；
3. 工具调用清单一旦落盘，被停止的那一批必须当场逐条补上结果（没启动的补
   "已中断"标记）。停止之后的历史必须**直接完整**——"等下一次提问时再修复"
   不合格；
4. 停止的收尾（保存记录、补标记）完成时，会话锁必须已被释放、引用计数归零。
   判定标准是行为：紧跟着的重发请求必须立刻进入生成，与正常轮次之后的重发
   完全一致；连续多次停止也一样。

已被否决的思路（不要重提）：

- "把停止改成同步等收尾"——停止请求会被收尾耗时挂住，界面卡死还可能互相等
  锁。停止只发信号、收尾由后台自己完成，这个分工是对的，不要动；
- "把锁的等待上限调小，白等就不明显了"——等待本身不该存在；调小上限只是把
  白等换成报错；
- "停止时直接丢弃整轮消息，眼不见为净"——用户已经看到的内容必须留下，丢记录
  是比残缺更严重的退化。
''', encoding="utf-8")

CALIB = TASK / "calibration"
CALIB.mkdir(exist_ok=True)
(CALIB / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-07",
    "calibrated": False,
    "target_band": [0.25, 0.55],
    "owner": "author",
    "policy": "§6.4 硬纪律：出题模型不得给自己出的题做校准。本表在盲测完成前保持空表，calibrated 恒为 false。",
    "gate": {
        "note": "出题侧门禁（§5.3）由 runs/blind/tools/packgate.py 跑，原始输出见 gate_*.json。这些不是校准数据，不参与 pass@1 统计。",
        "anchor_solution": "gate_fixed.json",
        "partial_solution": "gate_partial.json",
        "injected_state": "gate_injected_x20.json",
    },
    "blind_runs": {
        "note": "每一行 = 一次『只给第 1 级提示词』的完整作答。由非出题模型填写。",
        "columns": ["run_id", "model", "tier", "prompt_level", "pass@1", "score",
                    "failed_groups", "p2p_broken", "notes"],
        "rows": [],
    },
    "summary": {"runs": 0, "pass_at_1": None, "confidence_interval": None,
                "in_band": None, "conclusion": None},
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
meta.pop("status", None)
meta["redactions"] = [
    {"file": "AGENTS.md", "sections": ["15", "16"]},
    {"file": "CHANGELOG.md", "versions": ["1.5.2", "1.6.0", "1.6.1"]},
]
meta["checks"] = [
    {"kind": "pytest", "hidden": "hidden/tests_hidden", "groups": "hidden/groups.json", "p2p": "p2p.json"},
]
meta["visible"] = {"prune": list(NAME_POINTING_PRUNE)}
# 探针树构建前先落盘 meta：让"点名裁剪"在收集阶段就生效。
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

# --------------------------------------------------------------------------
# 六、p2p 白名单：基线收集 → 基线探针剔除 → 注入探针剔除（红名单进 visible.prune）
# --------------------------------------------------------------------------

CANDIDATE_FILES = [
    "tests/test_ai_agent.py",
    "tests/test_ai_sessions.py",
    "tests/test_ai_service_http.py",
]


def _build_tree(dest: Path, patches: list[Path]) -> None:
    packgate.build_tree("T2-07", json.loads((TASK / "meta.json").read_text(encoding="utf-8")),
                        dest, patches)


def _collect(tree: Path) -> list[str]:
    existing = [f for f in CANDIDATE_FILES if (tree / f).is_file()]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *existing, "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    return sorted({
        line.strip() for line in done.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line.strip()
    })


def _probe(tree: Path, node_ids: list[str]) -> set[str]:
    """跑一遍候选用例，返回红掉的用例名（junit 解析；名字跨这三个文件唯一）。"""
    report = tree / "probe-report.xml"
    if report.exists():
        report.unlink()
    env = packgate.build_env(packgate.CFG, str(tree))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *node_ids, "-q", "-p", "no:cacheprovider",
         f"--junitxml={report}", f"--basetemp={tree / '_probe-tmp'}"],
        cwd=tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=600,
    )
    red: set[str] = set()
    if not report.exists():
        raise RuntimeError(f"探针没有产出报告：{done.stdout[-2000:]}")
    for suite in ET.parse(report).getroot().iter("testsuite"):
        for case in suite.findall("testcase"):
            if any(case.find(tag) is not None for tag in ("failure", "error")):
                red.add(str(case.get("name") or ""))
    return red


INJECT_PATCHES = sorted(INJECT_DIR.glob("*.patch"))
baseline_tree = packgate.GATES / "T2-07-collect-baseline"
injected_tree = packgate.GATES / "T2-07-collect-injected"
_build_tree(baseline_tree, [])
candidates = _collect(baseline_tree)
print(f"p2p 候选 {len(candidates)} 条（基线树收集）")

baseline_red = _probe(baseline_tree, candidates)
print(f"基线红 {len(baseline_red)} 条：{sorted(baseline_red)}")

_build_tree(injected_tree, INJECT_PATCHES)
injected_red_names = _probe(injected_tree, candidates)
# junit 里拿到的只是用例名；换算回候选节点（用例名在这三个文件里唯一）。
injected_red = {node for node in candidates if node.split("::")[-1] in injected_red_names}
print(f"注入红 {len(injected_red)} 条：{sorted(injected_red)}")

prune_final = sorted(NAME_POINTING_PRUNE | injected_red)
p2p_final = [node for node in candidates
             if node not in baseline_red and node not in prune_final]
p2p_final = sorted(set(p2p_final) | set(REPAIR_P2P_NODES))

(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-07",
    "note": "基线（未注入）全绿的既有用例。候选 = test_ai_agent / test_ai_sessions / test_ai_service_http 三文件全量收集；基线态就红的剔除；注入后变红、或名字/断言点名取消机制的用例进 visible.prune（见 reference/notes.md）。",
    "tests": p2p_final,
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta["visible"] = {"prune": prune_final}
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"visible.prune {len(prune_final)} 条；p2p {len(p2p_final)} 条")

# --------------------------------------------------------------------------
# 七、notes.md（门禁结果若已存在则一并写入）
# --------------------------------------------------------------------------


def _gate_summary(name: str) -> dict:
    path = CALIB / name
    if not path.is_file():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {
        "score_min": doc.get("score_min"),
        "score_max": doc.get("score_max"),
        "stable": doc.get("stable"),
        "repeat": doc.get("repeat", 1),
        "p2p_total": doc.get("runs", [{}])[0].get("p2p_total") if doc.get("runs") else None,
        "p2p_failures": sum(len(run.get("p2p_failures") or []) for run in doc.get("runs", [])),
        "groups": {
            group["id"]: ("绿" if group["passed"] else "红")
            for group in (doc.get("runs", [{}])[-1].get("groups") or [])
        },
    }


fixed = _gate_summary("gate_fixed.json")
partial = _gate_summary("gate_partial.json")
injected = _gate_summary("gate_injected_x20.json")


def _fmt(state: dict, label: str) -> str:
    if not state:
        return f"| {label} | 待跑（`packgate --state …` 后重跑本脚本回填） |"
    detail = "、".join(f"{gid} {mark}" for gid, mark in state["groups"].items())
    return (
        f"| {label} | 得分 **{state['score_min']}**（{state['repeat']} 次，"
        f"稳定={state['stable']}），{detail}，p2p 破坏 {state['p2p_failures']} 条 |"
    )


notes = f"""# T2-07 参考解说明（成题版）

> 本文件**只进 `reference/`**，永远不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁见 `calibration/gate_*.json`。

## 一、锚解的形态：把"停止"的三条协议接回去

§6.3 对中级题的硬性规格是"修复需设计机制或协议"。本题的机制是一句话：

> **"停止"不是开关，而是一份协议**：取消令牌在哪些边界被检查、悬空的工具调用
> 怎么补、会话锁怎么释放——三条边互相咬合，缺一条"停止"就退化成一种灾难。

锚解把三条协议逐条接回（无新增文件、无新增公共接口）：

| 协议 | 锚解动作 |
| --- | --- |
| 取消边界 | 恢复模型流**分片之间**的取消检查：发现已取消立即带 partial 收尾（协议历史留模型原文、展示层留停止说明），`finally` 关断上游流 |
| 消息配对 | 恢复被取消槽位的 interrupted 结果回填：assistant(tool_calls) 落盘的每一个 call 都当场有配对结果，下一轮修复无事可做 |
| 锁释放 | 恢复 worker `finally` 的无条件释放：被停止的轮次同样在保存后释放锁并归还引用，重发请求零等待 |

## 二、注入态的改动（三端口，落盘 3 文件 / 6 处编辑）

| # | 位置（原始行号） | 注入内容 | 症状面 |
| --- | --- | --- | --- |
| 1 | `agent.py` 模型流循环（约 L{INJECT_LINES['agent_stream_check']}） | 删除 token 分片之间的 `token.cancelled` 检查（`_finish_cancelled(partial=…)` 的唯一调用点随之消失） | 停止后长流照常写完、停止说明再也不出现 |
| 2 | `agent.py` `run()` docstring（约 L{INJECT_LINES['agent_run_doc']}） | 安全边界清单里同步去掉"模型流的 token 之间" | 注释与代码保持"自洽"，不残留线索 |
| 3 | `agent.py` `_execute_tool_calls`（约 L{INJECT_LINES['agent_backfill']}） | 被取消槽位不再补 interrupted 结果，改为跳过（"缺口留给下一轮修复兜底"） | 停止后协议历史留下孤儿工具头；下一次提问先靠修复偷偷补洞 |
| 4 | `agent.py` `_invoke` docstring（约 L{INJECT_LINES['agent_invoke_doc']}）+ 导入行 | 同步改写；只为补结果服务的两个名字从导入中收窄 | 同上 |
| 5 | `facade.py` worker `finally`（原始 L{INJECT_LINES['facade_release']} 附近） | 被停止的轮次跳过 `release_session_lock()`（"锁留在原地，让重发请求排在这轮收尾后面"——但收尾早已结束，没人再还锁） | 停止后锁被永久占用，重发白等 90 秒后判忙 |
| 6 | `cancel.py` 模块 docstring（约 L{INJECT_LINES['cancel_doc']}） | 安全边界清单同步去掉"模型流的 token 之间" | 同 2 |

三处端口全部静默：不抛错、不改签名、不产生任何告警差异；注释与 docstring
同步改写成"看起来是有意的设计"。

**为什么不是回滚历史修复（相似度 < 0.6）**：注入不是删代码了事——每个删除点
都补了自洽的新注释给出一条"貌似合理的错误理由"（跳过的槽位"留给下一轮修复"、
被停止轮次的锁"留给重发请求排队"）；端口③还是一条新增的条件分支而非删除。
这组形态在任何历史提交里都不会同时出现。

## 三、陷阱

- **陷阱 A（半成品演示）**：只修配对端口。`partial.patch` 实测得分见下表——
  `pairing_exit` 绿，取消边界与会话锁两组全红，coherence（权重 2）因流中与
  工具批中两个时点仍不可用而红。
- **陷阱 B**：把锁等待上限调小（90 → 1）——"白等"看起来消失了，但锁的真正
  问题（被停止轮次漏释放）还在：每一次停止后的重发都会白等满上限然后判忙，
  `lock_release_exit` 两条用例红。
- **陷阱 C**：取消时直接丢弃整轮消息（"干净但暴力"）——用户已看到的答案没了，
  `cancel_boundary_exit` 的"已生成内容保留"断言红。
- **陷阱 D**：只在某一个取消时点做对（比如只处理流中取消）——coherence 组在
  另外两个时点各有一条用例（工具批中看配对、收尾轮中看边界+锁），必然红。
- **诱饵点**：①`cancel_turn` 只置位不阻塞是**正确**的协作式设计（第 3 级提示词
  已否决"改成同步等收尾"）；②`AI_STREAM_HEARTBEAT_SECONDS = 15` 看似与"停止
  无响应"有关，实际是前端保活机制，与本题无关；③`AI_SESSION_LOCK_TIMEOUT_SECONDS
  = 90` 是症状来源，但它是**上限**不是缺陷——把锅扣在常量上就是陷阱 B。

## 四、§6.5 反过易检查清单（逐条打勾）

- [x] **grep / 读文档 / git log 找不到"该修哪里、改成什么"。** AGENTS.md §15/§16
  与 CHANGELOG 相关条目已在 `redactions` 里划掉；三个注入点的周边注释同步改写
  成自然口径，不残留"这里曾经有检查"的痕迹。
- [x] **≥1 个"看似可疑但实际正确"的诱饵点。** 三个，见陷阱后的诱饵清单。
- [x] **每组隐藏测试有第二数据场景，硬编码 / 特判必挂。** 边界组三个场景
  （流首取消 / 流末前取消 / 流尾带 tool_calls）；配对组两批 + 修复不变量；
  锁组单轮与连续两轮停止；coherence 三个时点各一条。
- [x] **症状与三级提示词不含任何文件 / 函数 / 常量名。** 通篇只有"停止 /
  生成 / 工具调用 / 会话锁 / 白等"这类业务词汇。
- [x] **只修一个端口的半成品必然 < 100。** `partial.patch` 实测见下表。
- [x] **出题者自评"我 10 分钟能一次做对" → 退回重做。** 自评结论：**超过
  10 分钟**。三个注入点分属两条取消链路（agent 消息协议 / facade 锁协议），
  症状互相掩护（"停不下来"会掩盖"配对残缺"，"白等"又像性能问题）；修复
  需要同时理解 TurnRegistry 的登记/摘除、会话锁的引用计数与 worker 的
  finally 顺序，再写出三种时点都对齐的收尾。

## 五、visible.prune 与 p2p

| 条目 | 理由 |
| --- | --- |
| `tests/test_ai_agent.py`（整文件） | 该文件的 5 条取消守卫（`test_session_pairs_stay_valid_after_cancel`、`test_cancel_between_rounds_starts_no_further_model_call`、`test_cancel_after_tool_calls_landed_still_pairs_every_call`、`test_cancel_mid_stream_keeps_partial_text_verbatim_in_protocol_history`、`test_cancel_skips_remaining_tools_but_keeps_call_pairing`）名字+docstring 把三条协议在三个时点上的口径全部点名，留在沙箱等于发答案；其中两条在注入态还会红。按例裁剪对本文件不可用（harness 文本嗅探按前 4096 字节判定，该文件在字节 4094 处切中多字节字符被当作二进制跳过），故整文件裁剪。 |
| `tests/test_ai_service_http.py::test_ai_chat_cancel_stops_the_worker_and_releases_the_session` | 注入态变红 + docstring 直接点名"停止后释放会话锁"。 |

`test_ai_chat_cancel_requires_session_id` **保留**在 p2p：它只断言空参 400 校验，
不点名取消协议，注入后仍绿（与任务书草案预计的"也 prune"不同，按实测与
"点名才裁"的准则保留，减少无谓的回归覆盖损失）。

修复机制本体的回归随整文件裁剪一起离开沙箱，改由**两条隐藏 p2p 条目**承担
（`hidden/tests_hidden/test_collab_cancel.py::test_repair_synthesizes_placeholders_for_dangling_calls`
与 `::test_repair_keeps_intact_history_unchanged`，镜像被裁的既有守卫）：注入
不触碰修复机制，三条门禁状态下都必须绿；模型若以"删掉修复机制"的方式"修"
配对，这两条红 → 本轮作废。

p2p 白名单：{len(p2p_final)} 条 = 会话锁/回收用例（tests/test_ai_sessions.py）+
非取消 HTTP 用例（tests/test_ai_service_http.py，剔除上面裁掉的一条）+ 修复机制
隐藏回归 2 条；基线探针剔除 {len(baseline_red)} 条基线红，注入探针剔除
{len(injected_red)} 条注入红。

## 六、门禁自验结果（§5.3，packgate 实测）

| 门禁 | 结果 |
| --- | --- |
{_fmt(fixed, "锚解（fix.patch）")}
{_fmt(partial, "半成品（partial.patch，只修配对）")}
{_fmt(injected, "注入态 ×" + str((injected or {}).get("repeat", 20)))}
| 参考解不触碰 forbidden_paths | 通过（fix.patch 仅 `ai/agent.py`、`ai/facade.py`、`ai/cancel.py`，partial.patch 仅 `ai/agent.py`，均在 allowed_paths） |
| 沙箱可见红测试 | 0（点名守卫整文件裁剪 1 + 点名单例裁剪 1，注入态变红用例全部离开沙箱） |

## 七、校准状态（§6.4）

`calibration/results.json` 盲测表待填，`calibrated = false`。出题模型不做盲测。
"""
(REFERENCE / "notes.md").write_text(notes, encoding="utf-8")

print("T2-07 成题完成")
