"""T3-08 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

产出（写进 packs/core/tasks/T3-08/）：
  inject/patches/0001-agent-archive-trilemma.patch  归档三端口注入（单文件三处）
  reference/fix.patch                               锚解：三端口 + 现状口径缺陷全修齐
  reference/partial.patch                           半成品：只修预算口径/空转（演示牵制）
  hidden/tests_hidden/test_archive_trilemma.py      pytest 隐藏测试（6 组不变量）
  hidden/groups.json
  p2p.json                                          候选白名单（基线树收集，注入后经验修正）
  prompts/1.md 2.md 3.md、calibration/results.json、meta.json

三端口（互相牵制，见 reference/notes.md）：
  ① 归档丢失：_archive_overflow 搬运时丢弃 tool 结果侧正文（调用侧只留工具名）
  ② 字符预算空转：超限分支只挑"不计入统计"的空壳条目下手——利用仓库现状里
    "归档统计只数正文、预算工具却把参数体计入"的口径不一致
  ③ 孤儿 tool 头：run() 不再前置修复悬空调用；归档切点找不到 user 边界时按统计
    切点硬切（把配对切成孤儿），孤儿从此无人修复
诱饵（不许真改坏）：SHORT_TERM_WINDOW / ARCHIVE_MAX_ENTRIES 两个"调大就好"旋钮、
  _consolidate_archive 的压缩失败保留 + 条数兜底占位（正确设计）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T3-08"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

AGENT_REL = "backend/astock_backtester/ai/agent.py"
agent_src = (REPO / AGENT_REL).read_text(encoding="utf-8")

# --------------------------------------------------------------------------
# 一、变体构造（三端口注入 / 锚解 / 半成品）
# --------------------------------------------------------------------------

RUN_REPAIR_BLOCK = """        # 上一次运行可能被中断（客户端断开/进程退出/模型异常），先修复悬空的
        # tool_calls——否则下次请求会被上游 API 以协议错误拒绝，表现为“失忆”。
        self._repair_interrupted_turn(session)
"""
assert RUN_REPAIR_BLOCK in agent_src, "run() 修复块锚点未命中"

ARCHIVE_START = "    def _archive_overflow(self, session: dict[str, Any], on_event: EventHandler) -> None:"
ARCHIVE_END = "    def _consolidate_archive(self, session: dict[str, Any], on_event: EventHandler) -> None:"
assert ARCHIVE_START in agent_src and ARCHIVE_END in agent_src, "_archive_overflow 锚点未命中"


def replace_span(text: str, start: str, end: str, replacement: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[:i] + replacement + text[j:]


INJECTED_ARCHIVE = '''    def _archive_overflow(self, session: dict[str, Any], on_event: EventHandler) -> None:
        """Keep the live window within both the message-count and char budget.

        窗口整理是“搬运”：把放不下的条目移出窗口、登记进待归档区，等攒批
        压缩统一折进滚动纪要。搬运途中不做摘要、不改写正文；悬空调用的补齐
        有专门的修复阶段负责，搬运这里不重复检查配对关系。
        """
        messages = session["messages"]
        overflow = max(len(messages) - SHORT_TERM_WINDOW, 0)
        total_chars = sum(len(str(message.get("content") or "")) for message in messages)
        if total_chars > SHORT_TERM_MAX_CHARS:
            # 字符超限：优先搬运不占内容统计的空壳条目（没有正文的调用声明、
            # 占位消息）。它们在统计里体积为零，搬走属于零成本清理；带正文的
            # 条目是对话本体，留给攒批压缩统一处理，这里一律不动。
            for index in range(len(messages)):
                if str(messages[index].get("content") or ""):
                    continue
                overflow = max(overflow, index + 1)
        if overflow <= 0:
            return
        # user 消息是天然的轮次边界，优先按边界切；找不到边界就按统计切点
        # 直接切，超限的窗口不能因为找不到边界就一轮轮放着不管。
        boundary = next(
            (index for index in range(overflow, len(messages)) if messages[index].get("role") == "user"),
            overflow,
        )
        overflow = boundary
        dropped = messages[:overflow]
        session["messages"] = messages[overflow:]
        archive = session.setdefault("pending_archive", [])
        for message in dropped:
            text = str(message.get("content") or "")
            calls = message.get("tool_calls") or []
            if calls:
                # 调用侧只登记工具名：调用随时可以按原参数重发拿回同样的结果，
                # 归档里再留一份调用载荷只会把长期记忆撑爆。
                text += " " + ", ".join(call.get("function", {}).get("name", "") for call in calls)
                archive.append(f"{message.get('role')}: {text}")
                continue
            if message.get("role") == "tool":
                # 结果侧正文不进归档：结果仍按 call_id 留在内存结果仓里可取回，
                # 归档只保留对话主线，防止同一段数据在窗口和归档里存两份。
                continue
            archive.append(f"{message.get('role')}: {text}")
        # 压缩持续失败时归档只增不减，必须按条数兜底裁剪，否则会话文件会无限膨胀。
        if len(archive) > ARCHIVE_MAX_ENTRIES:
            # 丢最旧的条目，但保留一条“已丢弃 N 条”的占位说明，避免静默失忆。
            # 说明本身占 1 条，所以保留 ARCHIVE_MAX_ENTRIES - 1 条正文。
            excess = len(archive) - (ARCHIVE_MAX_ENTRIES - 1)
            archive[:] = [f"（更早的 {excess} 条对话因压缩失败已丢弃）", *archive[excess:]]
        on_event({"type": "phase", "phase": "归档短期窗口之外的对话"})

'''

FIXED_ARCHIVE = '''    def _archive_overflow(self, session: dict[str, Any], on_event: EventHandler) -> None:
        """Keep the live window within both the message-count and char budget.

        归档是“搬运”而不是“摘要”，三条不变量必须同时成立：

        1. 体积口径与真实发送一致：assistant(tool_calls) 的参数体和消息正文
           一起计入字符预算——只数正文会系统性低估体积，超限会话看着不大，
           实际每轮请求都超限。口径必须与 ``ContextBudget.count_tokens`` 一致：
           一个出口数正文、一个出口数调用体，两个出口对同一会话必然各执一词。
        2. 切点只能落在轮次边界：归档后的剩余窗口必须以 user 消息开头——
           assistant(tool_calls)/tool 配对因此天然完整，不会在配对中间切出
           孤儿 tool 头（上游都拒绝首条为 tool 的请求，孤儿一旦进窗口，
           该会话此后每轮必败）。找不到安全切点就归档 0 条并留 warning：
           宁可一轮超预算，绝不产生非法窗口。
        3. 被移走的每一条都完整进 pending_archive（含 tool 结果正文）：
           归档是长期记忆的唯一入口，搬运途中丢内容就是失忆。
        """
        messages = session["messages"]
        overflow = max(len(messages) - SHORT_TERM_WINDOW, 0)
        total_chars = sum(self._message_wire_chars(message) for message in messages)
        if total_chars > SHORT_TERM_MAX_CHARS:
            # 字符超限：从最旧处开始按同一口径累计，直到剩余体积回到阈值内。
            dropped_chars = 0
            for index in range(len(messages)):
                if total_chars - dropped_chars <= SHORT_TERM_MAX_CHARS:
                    break
                dropped_chars += self._message_wire_chars(messages[index])
                overflow = max(overflow, index + 1)
        if overflow <= 0:
            return
        boundary = next(
            (index for index in range(overflow, len(messages)) if messages[index].get("role") == "user"),
            None,
        )
        if boundary is None:
            logger.warning(
                "短期窗口 %s 条超出阈值但找不到 user 边界（长工具轮中段），本轮不归档以保持协议合法",
                len(messages),
            )
            return
        overflow = boundary
        dropped = messages[:overflow]
        session["messages"] = messages[overflow:]
        archive = session.setdefault("pending_archive", [])
        for message in dropped:
            text = str(message.get("content") or "")
            calls = message.get("tool_calls") or []
            if calls:
                text += " " + ", ".join(call.get("function", {}).get("name", "") for call in calls)
            archive.append(f"{message.get('role')}: {text}")
        # 压缩持续失败时归档只增不减，必须按条数兜底裁剪，否则会话文件会无限膨胀。
        if len(archive) > ARCHIVE_MAX_ENTRIES:
            # 丢最旧的条目，但保留一条“已丢弃 N 条”的占位说明，避免静默失忆。
            # 说明本身占 1 条，所以保留 ARCHIVE_MAX_ENTRIES - 1 条正文。
            excess = len(archive) - (ARCHIVE_MAX_ENTRIES - 1)
            archive[:] = [f"（更早的 {excess} 条对话因压缩失败已丢弃）", *archive[excess:]]
        on_event({"type": "phase", "phase": "归档短期窗口之外的对话"})

    @staticmethod
    def _message_wire_chars(message: dict[str, Any]) -> int:
        """单条消息的真实发送体积：正文 + 工具调用参数体（与 count_tokens 同口径）。"""
        total = len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(str(function.get("arguments") or ""))
        return total

'''

PARTIAL_ARCHIVE = '''    def _archive_overflow(self, session: dict[str, Any], on_event: EventHandler) -> None:
        """Keep the live window within both the message-count and char budget.

        窗口整理是“搬运”：把放不下的条目移出窗口、登记进待归档区，等攒批
        压缩统一折进滚动纪要。体积统计与真实发送同口径：assistant(tool_calls)
        的参数体和消息正文一起计入，只数正文会系统性低估体积。
        """
        messages = session["messages"]
        overflow = max(len(messages) - SHORT_TERM_WINDOW, 0)
        total_chars = sum(self._message_wire_chars(message) for message in messages)
        if total_chars > SHORT_TERM_MAX_CHARS:
            # 字符超限：从最旧处开始按同一口径累计，直到剩余体积回到阈值内。
            dropped_chars = 0
            for index in range(len(messages)):
                if total_chars - dropped_chars <= SHORT_TERM_MAX_CHARS:
                    break
                dropped_chars += self._message_wire_chars(messages[index])
                overflow = max(overflow, index + 1)
        if overflow <= 0:
            return
        # user 消息是天然的轮次边界，优先按边界切；找不到边界就按统计切点
        # 直接切，超限的窗口不能因为找不到边界就一轮轮放着不管。
        boundary = next(
            (index for index in range(overflow, len(messages)) if messages[index].get("role") == "user"),
            overflow,
        )
        overflow = boundary
        dropped = messages[:overflow]
        session["messages"] = messages[overflow:]
        archive = session.setdefault("pending_archive", [])
        for message in dropped:
            text = str(message.get("content") or "")
            calls = message.get("tool_calls") or []
            if calls:
                # 调用侧只登记工具名：调用随时可以按原参数重发拿回同样的结果，
                # 归档里再留一份调用载荷只会把长期记忆撑爆。
                text += " " + ", ".join(call.get("function", {}).get("name", "") for call in calls)
                archive.append(f"{message.get('role')}: {text}")
                continue
            if message.get("role") == "tool":
                # 结果侧正文不进归档：结果仍按 call_id 留在内存结果仓里可取回，
                # 归档只保留对话主线，防止同一段数据在窗口和归档里存两份。
                continue
            archive.append(f"{message.get('role')}: {text}")
        # 压缩持续失败时归档只增不减，必须按条数兜底裁剪，否则会话文件会无限膨胀。
        if len(archive) > ARCHIVE_MAX_ENTRIES:
            # 丢最旧的条目，但保留一条“已丢弃 N 条”的占位说明，避免静默失忆。
            # 说明本身占 1 条，所以保留 ARCHIVE_MAX_ENTRIES - 1 条正文。
            excess = len(archive) - (ARCHIVE_MAX_ENTRIES - 1)
            archive[:] = [f"（更早的 {excess} 条对话因压缩失败已丢弃）", *archive[excess:]]
        on_event({"type": "phase", "phase": "归档短期窗口之外的对话"})

    @staticmethod
    def _message_wire_chars(message: dict[str, Any]) -> int:
        """单条消息的真实发送体积：正文 + 工具调用参数体（与 count_tokens 同口径）。"""
        total = len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(str(function.get("arguments") or ""))
        return total

'''


def make_variant(state: str) -> str:
    """baseline / injected / fixed / partial 四态。fixed = 原始正确行为。"""
    if state == "baseline":
        return agent_src
    text = agent_src
    if state in ("injected", "partial"):
        # 端口 3：归档/修复顺序对调——run() 不再前置修复（fixed 必须保留修复）
        text = text.replace(RUN_REPAIR_BLOCK, "", 1)
    if state in ("injected", "partial"):
        text = replace_span(text, ARCHIVE_START, ARCHIVE_END, INJECTED_ARCHIVE)
    elif state == "fixed":
        text = replace_span(text, ARCHIVE_START, ARCHIVE_END, FIXED_ARCHIVE)
    if state == "partial":
        # 半成品 = 只修预算口径/空转：在注入版基础上把字符分支与口径修齐
        text = replace_span(text, ARCHIVE_START, ARCHIVE_END, PARTIAL_ARCHIVE)
    return text


injected_src = make_variant("injected")
fixed_src = make_variant("fixed")
partial_src = make_variant("partial")
for name, text in (("injected", injected_src), ("fixed", fixed_src), ("partial", partial_src)):
    assert text != agent_src, f"{name} 变体与原始相同"
    compile(text, f"{name}.py", "exec")  # 语法自检

# --------------------------------------------------------------------------
# 二、补丁产出
# --------------------------------------------------------------------------

original_lines = agent_src.splitlines(keepends=True)
injected_lines = injected_src.splitlines(keepends=True)

INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE.mkdir(parents=True, exist_ok=True)

(INJECT_DIR / "0001-agent-archive-trilemma.patch").write_text(
    build_patch(AGENT_REL, original_lines, injected_lines), encoding="utf-8")
(REFERENCE / "fix.patch").write_text(
    build_patch(AGENT_REL, injected_lines, fixed_src.splitlines(keepends=True)), encoding="utf-8")
(REFERENCE / "partial.patch").write_text(
    build_patch(AGENT_REL, injected_lines, partial_src.splitlines(keepends=True)), encoding="utf-8")
print("补丁已生成（注入 1 文件 + fix + partial）")

# --------------------------------------------------------------------------
# 三、隐藏测试与分组
# --------------------------------------------------------------------------

HIDDEN = TASK / "hidden" / "tests_hidden"
HIDDEN.mkdir(parents=True, exist_ok=True)

(HIDDEN / "test_archive_trilemma.py").write_text(r'''"""T3-08 隐藏测试：会话历史三机制（归档 / 配对 / 字符预算）的合取不变量。

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
''', encoding="utf-8")

(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-08",
    "note": "pytest 侧分组。组 = 一个出口/一条独立事实。coherence 组权重最高，断言三条承诺在同一组长会话里同时成立；它红，说明归档/配对/预算三机制仍有至少一处没接上。每个记分测试都有第二数据场景，硬编码或特判必然挂。",
    "groups": [
        {
            "id": "archive_integrity_exit",
            "weight": 1,
            "port": "归档完整性：移出窗口的内容必须原文到达长期记忆层；切点永不落在配对中间，找不到安全切点就本轮不归档并留告警",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_archive_moves_complete_rounds_into_long_term_memory",
                "hidden/tests_hidden/test_archive_trilemma.py::test_archive_boundary_never_splits_a_pair",
            ],
        },
        {
            "id": "pairing_exit",
            "weight": 1,
            "port": "协议配对：每次真实请求发出前，悬空调用必须补齐结果、窗口头不得是孤儿 tool 结果",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_dangling_calls_are_repaired_before_the_next_request",
                "hidden/tests_hidden/test_archive_trilemma.py::test_archive_cut_never_ships_orphan_results",
            ],
        },
        {
            "id": "budget_effective_exit",
            "weight": 1,
            "port": "预算有效：整理之后真实发送体积（正文 + 调用参数体）必须回到预算内；整理动作的压缩调用每轮至多一次",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_tidy_brings_real_payload_back_under_budget",
                "hidden/tests_hidden/test_archive_trilemma.py::test_budget_counts_tool_call_arguments",
                "hidden/tests_hidden/test_archive_trilemma.py::test_tidy_keeps_model_calls_batched_per_turn",
            ],
        },
        {
            "id": "no_data_loss_exit",
            "weight": 1,
            "port": "数据零丢失：工具结果原文必须随归档保留；压缩失败保留归档；条数兜底裁剪留占位说明",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_no_tool_result_is_lost_when_archiving",
                "hidden/tests_hidden/test_archive_trilemma.py::test_compaction_failure_keeps_archive_and_cap_leaves_placeholder",
            ],
        },
        {
            "id": "recall_reference_exit",
            "weight": 1,
            "port": "召回参照：长期记忆召回的预算语义保持严格（对照锚）；归档原料必须完整进入压缩通道",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_recall_budget_stays_strict",
                "hidden/tests_hidden/test_archive_trilemma.py::test_archived_findings_still_reach_the_summary_channel",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "合取：记得的只多不少 + 发送体积受控 + 配对完整，在同一组长会话里同时成立",
            "tests": [
                "hidden/tests_hidden/test_archive_trilemma.py::test_long_session_keeps_all_three_promises_at_once",
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
print("隐藏测试与分组已写入")

# --------------------------------------------------------------------------
# 四、p2p：基线树收集 → 注入探测 → 经验修正
# --------------------------------------------------------------------------

meta = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
meta.pop("status", None)
meta["visible"]["prune"] = []  # 先收集，再按注入红名单回填

CANDIDATE_FILES = [
    "tests/test_ai_context.py",   # 预算工具本体（注入不动，全文件候选）
    "tests/test_ai_agent.py",     # 本题主战场：攒批/阈值下等注入不动用例保留，点名答案的裁剪
]

baseline_tree = packgate.GATES / "T3-08-collect"
packgate.build_tree("T3-08", meta, baseline_tree, [])
done = subprocess.run(
    [sys.executable, "-m", "pytest", *CANDIDATE_FILES, "--collect-only", "-q",
     "-p", "no:cacheprovider"],
    cwd=baseline_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
candidates = sorted({
    line.strip() for line in done.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line.strip()
})
print(f"p2p 候选 {len(candidates)} 条（基线收集）")
assert candidates, "基线树没有收集到任何候选用例"
# 基线红的剔除（本仓库当前应无；保险起见按声明剔除）
baseline_tree.mkdir(parents=True, exist_ok=True)
(baseline_tree / ".grade-cache").mkdir(parents=True, exist_ok=True)
baseline_run = subprocess.run(
    [sys.executable, "-m", "pytest", *candidates, "-q", "-p", "no:cacheprovider",
     "--basetemp", str(baseline_tree / ".grade-cache" / "p2p-base")],
    cwd=baseline_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=600,
)
baseline_red = {
    line.split(" ", 1)[1].strip()
    for line in baseline_run.stdout.splitlines() if line.startswith("FAILED ")
}
if baseline_red:
    print("基线红（剔除）:", sorted(baseline_red))
candidates = [c for c in candidates if c not in baseline_red]

# 注入探测：注入后变红的用例 = 点名答案的守卫 → visible.prune，同时从 p2p 剔除
probe_tree = packgate.GATES / "T3-08-probe"
inject_patches = sorted((TASK / "inject" / "patches").glob("*.patch"))
probe_meta = json.loads(json.dumps(meta))
probe_meta["visible"]["prune"] = []
packgate.build_tree("T3-08", probe_meta, probe_tree, inject_patches)
(probe_tree / ".grade-cache").mkdir(parents=True, exist_ok=True)
probe_run = subprocess.run(
    [sys.executable, "-m", "pytest", *candidates, "-q", "-p", "no:cacheprovider",
     "--basetemp", str(probe_tree / ".grade-cache" / "p2p-probe")],
    cwd=probe_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=600,
)
injected_red = sorted(
    line.split(" ", 1)[1].strip()
    for line in probe_run.stdout.splitlines() if line.startswith("FAILED ")
)
print(f"注入后变红 {len(injected_red)} 条（进 visible.prune 并从 p2p 剔除）:")
for item in injected_red:
    print("  ", item)
p2p_tests = [c for c in candidates if c not in set(injected_red)]

(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-08",
    "note": "基线（未注入）全绿的既有用例。候选 = tests/test_ai_context.py 全文件（预算工具本体）+ tests/test_ai_agent.py（本题主战场）。注入后变红、点名答案的守卫用例已进 visible.prune 并从本白名单剔除（见 reference/notes.md 的记录）。",
    "tests": p2p_tests,
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta["visible"]["prune"] = injected_red
meta["redactions"] = [
    # §15 架构不变量（分层记忆/短期窗口/归档条目）、§16 AI 子系统——点名本题主战场机制
    {"file": "AGENTS.md", "sections": ["15", "16"]},
    # 1.6.1（归档窗口孤儿 tool 消息条目）、1.4.0（上下文工程与分层记忆条目）——点名答案
    {"file": "CHANGELOG.md", "versions": ["1.6.1", "1.4.0"]},
]

# --------------------------------------------------------------------------
# 五、提示词 / 校准表 / meta 定稿
# --------------------------------------------------------------------------

PROMPTS = TASK / "prompts"
PROMPTS.mkdir(exist_ok=True)

(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

这套系统的 AI 助手在长会话上有三种表现，像三个不同的病，但总是在同一些会话身上一起发作：

1. **长工具轮之后失忆**。凡是哪一轮连续调用了比较多的工具，那一轮结束之后助手就把
   更早的上下文忘得干干净净——连用户最开始给的要求都丢了。短会话一切正常，
   越长、工具用得越多的对话越明显。
2. **个别会话每轮必败**。某些会话从某一刻起，每一轮请求都被上游以"超过长度上限"
   拒绝，怎么重试都没用。奇怪的是这些会话"看起来"不大，甚至比正常会话还小。
3. **清理像是没发生过，偶尔还帮倒忙**。日志里能看到"正在整理历史"的动作在跑，
   整理完之后会话体积却一点没变；偶尔反过来——整理完，助手连刚刚答应过的事都不认账了，
   只能把刚才那轮重新来一遍。

## 验收要求

修好之后，下面几条必须**同时**成立（我们在多组不同的长会话上验收，只满足其中几条的改法，
在这套数据上一定会在别处翻车）：

- 长会话必须能自动把更早的历史**完整地**移进长期记忆区——移出窗口的每一段内容，
  之后都必须还能从长期记忆里找回，包括工具查回来的结果原文；
- 任何一轮请求发出前，实际发送的内容必须真的在预算内——"看起来整理过"不算数，
  会话"看起来不大"也不算数，发出去超限就是失败；
- 记录里出现的每一次工具调用都必须有对应的结果（或明确的"已中断"说明），
  整理动作不能制造新的残缺配对，也不能把配对的一半当成体积裁掉。

我不要求你改测试，也不需要新增功能。请把根因修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把"这个会话有多大、哪些内容还留着、每次调用是否有着落"这三件事在系统里走一遍，
会发现它们被好几处各自计算、而且互相矛盾：

1. 会话体积有两本账：一处只数"消息正文"的字符数，另一处把"工具调用的参数体"也算
   进去。同一场会话，一本账说"远没超限"，另一本账说"早就爆了"。
2. 超限判定与整理动作挑体积的口径对不上：判定那边喊超限，整理那边却专挑"统计里
   根本不算数"的部分下手——动作再多，账面体积一动不动。
3. "移出窗口"和"进入长期记忆"对不上账：有些条目明明被移出了窗口，长期记忆里却
   找不到它的正文——搬运途中丢东西，而且丢的恰好是工具结果那一半。
4. "补齐残缺调用"和"整理窗口"的先后没有约定：整理可能在配对中间落刀，把完整的
   "调用—结果"从中间切开，切出来的碎片再也没有人来认领。
5. 对照：长期记忆召回那一侧的预算是严格生效的——整行累加、超限即停、被预算挡下的
   记录绝不记入"已注入"。同一套系统里两种预算语义一严一松，松的那侧就是重灾区。

任何一处单独看都"有自己的道理"，合在一起就是：同一个事实，多本账、多种答案，
而且谁也不肯为"发出去的东西到底有多大"负责。
''', encoding="utf-8")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时成立的表述（缺一条就是没修完）：

1. 单一口径：任何"会话是否超预算"的判定、任何整理动作挑体积的口径，必须与"这次
   请求真实要发送的体积"同源——正文和工具调用参数体一起算，不许两本账。
2. 发送前硬校验：每一轮请求发出前，按同一口径计的体积必须回到预算内。整理动作只准
   挑真正计入统计的体积下手；"整理了但账面不动"的路径等于没有整理。
3. 搬运完整：移出窗口的每一条都必须原文进入长期记忆区；归档切点只能落在完整的轮次
   之间（切完的窗口以用户消息开头），永不落在"调用—结果"配对中间；找不到这样的
   安全切点，就本轮不归档并留下告警——宁可一轮超预算，也绝不产生协议非法的窗口。
4. 配对自愈先于一切发送：每次整理之后、每次请求之前，协议历史必须合法——悬空的
   调用要补上"已中断"的结果，无主的结果不允许留在窗口里；这个修复不能依赖
   "下一次碰巧想起来"。
5. 整理动作对模型的调用每轮至多一次（攒批压缩语义保留）；压缩失败时已归档内容必须
   原样保留，条数兜底裁剪留下的"已丢弃 N 条"占位说明不许去掉。
6. 长期记忆召回的预算语义保持严格（整行累加、超限即停）：修预算不许顺手把严格语义
   放松成"永不截断"来"一举解决"。

已被否决的思路（不要重提）：

- "把窗口条数/字符上限调大就好"——调大只是推迟，更长的会话照样空转；而且
  "必败会话看起来更小"恰恰说明问题不在上限数值。
- "压缩失败就清空归档，免得越攒越多"——失败保留是正确设计；清空会把已经移出窗口的
  内容永久丢掉，那是把"内存膨胀"修成"失忆"。
- "把配对残缺的消息整条删掉"——删掉孤儿结果并不能补回它的调用，删掉悬空调用则连
  修复机会一起删了；删除不能替代补齐。
- "发送前把超长内容静默截断一刀"——那是把深层问题的答案糊在表层症状上，截掉的
  仍然是用户已经得到的结论。
''', encoding="utf-8")

CALIB = TASK / "calibration"
CALIB.mkdir(exist_ok=True)
(CALIB / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T3-08",
    "calibrated": False,
    "target_band": [0.05, 0.25],
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

(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print("T3-08 成题文件已生成（prompts / calibration / meta 定稿）")
print(f"visible.prune {len(injected_red)} 条；p2p {len(p2p_tests)} 条")
