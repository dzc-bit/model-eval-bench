"""Focused coverage for the built-in Chat Completions tool loop."""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from conftest import make_run
from harness import chat, errors, runs, util


def _ready_run(cfg, tmp_path):
    sandbox_root = tmp_path / "sandbox"
    run_dir = tmp_path / "run"
    sandbox_root.mkdir()
    run_dir.mkdir()
    run = make_run(cfg, run_dir=str(run_dir), run_id="TEST-01__chat__20260101-000000")
    run.update({"status": "ready", "sandbox": str(sandbox_root), "model": "chat"})
    cfg["models"] = [{
        "id": "chat",
        "protocol": "openai",
        "api_mode": "chat_completions",
        "base_url": "https://example.test/v1",
        "model": "test-model",
    }]
    return run, str(sandbox_root)


@pytest.mark.parametrize("reasoning_field", [None, "reasoning_content", "reasoning"])
def test_send_completes_chat_completions_tool_roundtrip(cfg, tmp_path, monkeypatch, reasoning_field):
    run, sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    util.write_text_atomic(os.path.join(sandbox_root, "README.md"), "workspace contents")
    calls = []
    responses = [
        {"choices": [{"message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-list",
                "type": "function",
                "function": {"name": "list_files", "arguments": '{"path":".","recursive":false}'},
            }],
        }}]},
        {"choices": [{"message": {"role": "assistant", "content": "I found README.md."}}]},
    ]

    if reasoning_field:
        responses[0]["choices"][0]["message"][reasoning_field] = "先检查沙箱目录。"
        responses[1]["choices"][0]["message"][reasoning_field] = "已根据工具结果确认文件。"

    def fake_post(url, payload, key, timeout):
        calls.append((url, json.loads(json.dumps(payload)), key, timeout))
        return responses.pop(0)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    result = chat.send(cfg, run, "Inspect the workspace")

    assert len(calls) == 2
    assert calls[0][0] == "https://example.test/v1/chat/completions"
    assert calls[0][2] == "test-secret"
    assert calls[0][1]["messages"][0]["role"] == "system"
    second_messages = calls[1][1]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_call_id"] == "call-list"
    assert "README.md" in second_messages[-1]["content"]

    roles = [message["role"] for message in result["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert result["messages"][1]["tool_calls"][0]["function"]["name"] == "list_files"
    assert result["message"]["content"] == "I found README.md."

    with open(os.path.join(run["run_dir"], "chat.jsonl"), "r", encoding="utf-8") as fh:
        persisted = [json.loads(line) for line in fh]
    assert [message["role"] for message in persisted] == roles
    assert chat.messages(run) == persisted
    replay = chat._history_for_api(persisted)
    if reasoning_field:
        assert persisted[1][reasoning_field] == "先检查沙箱目录。"
        assert persisted[-1][reasoning_field] == "已根据工具结果确认文件。"
        assert second_messages[-2][reasoning_field] == persisted[1][reasoning_field]
        assert replay[1][reasoning_field] == persisted[1][reasoning_field]
        assert replay[-1][reasoning_field] == persisted[-1][reasoning_field]
    else:
        assert not any(k in replay[-1] for k in ("reasoning", "reasoning_content"))
    assert "test-secret" not in json.dumps(persisted)


@pytest.mark.parametrize("path", ["../outside.txt", "..\\outside.txt"])
def test_file_tools_reject_paths_outside_workspace(cfg, tmp_path, path):
    run, sandbox_root = _ready_run(cfg, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")

    with pytest.raises(ValueError, match="沙箱"):
        chat._tool_write_file(sandbox_root, {"path": path, "content": "overwrite"})

    assert outside.read_text(encoding="utf-8") == "untouched"


def test_run_command_rejects_inline_scripts(cfg, tmp_path):
    _run, sandbox_root = _ready_run(cfg, tmp_path)

    with pytest.raises(ValueError, match="内联脚本"):
        chat._tool_run_command(sandbox_root, {"command": ["python", "-c", "print('outside')"]})


def test_run_cmd_terminates_process_when_output_limit_is_reached(tmp_path):
    result = util.run_cmd(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 10000000); sys.stdout.flush()",
        ],
        cwd=str(tmp_path),
        timeout=10,
        max_output_bytes=1024,
    )

    assert result.output_limited is True
    assert len(result.stdout.encode("utf-8")) <= 1024
    assert result.timed_out is False
    assert result.cancelled is False
    assert result.returncode != 0
    assert result.ok is False
    assert result.duration_s < 10


# ---------------------------------------------------------------------------
# 上下文窗口与压缩（NOTES.md 第五节的落地）
# ---------------------------------------------------------------------------

def _chat_run(cfg, tmp_path, rows):
    """把一批记录写进运行目录，返回 run（只造数据，不碰真实题包与运行历史）。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = make_run(cfg, run_dir=str(run_dir), run_id="TEST-01__chat__20260101-000000")
    for row in rows:
        chat._append_message(run, row)
    return run


def _tool_round(prompt, call_id, name, arguments, payload, conclusion):
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": call_id, "name": name,
         "content": json.dumps(payload, ensure_ascii=False)},
        {"role": "assistant", "content": conclusion},
    ]


def _assert_openai_sequence(items):
    """压缩后的序列必须满足服务商校验：tool 紧跟带对应 tool_calls 的 assistant。"""
    pending = set()
    for item in items:
        if item.get("role") == "tool":
            assert item["tool_call_id"] in pending, "tool 消息必须紧跟带对应 tool_calls 的 assistant"
            pending.discard(item["tool_call_id"])
            continue
        assert not pending, "assistant 的 tool_calls 必须全部等到响应"
        calls = item.get("tool_calls") or []
        pending = {call["id"] for call in calls if call.get("id")}
    assert not pending


def test_model_history_summarises_old_tools_but_keeps_write_targets(cfg, tmp_path):
    body = "同一份事实只算一次。" * 3000
    rows = _tool_round("第一轮：统一派生口径", "c1", "write_file",
                       {"path": "backend/pricing.py", "content": body},
                       {"path": "backend/pricing.py", "bytes": 12345},
                       "第一轮结论：已改写 pricing.py")
    rows[1]["reasoning_content"] = "先定位口径出处。"
    rows.append({"role": "user", "content": "第二轮：继续修 adapters"})
    run = _chat_run(cfg, tmp_path, rows)

    history, dropped = chat._model_history(cfg, chat._read_records(run))

    assert dropped == 0
    tool_texts = [str(item.get("content")) for item in history if item["role"] == "tool"]
    assert tool_texts and "write_file" in tool_texts[0]
    assert "backend/pricing.py" in tool_texts[0] and "12345" in tool_texts[0]
    assert body not in json.dumps(history, ensure_ascii=False), "历史轮的工具原文不该重发"
    # 思维链按服务商要求原样回传（带 tools 的请求必须回传，见 NOTES.md 第五节第 3 条）
    assert history[1]["reasoning_content"] == "先定位口径出处。"
    # 前端展示与落盘仍是全量：正文留在 assistant 的调用参数里
    persisted = chat.messages(run)
    assert body in persisted[1]["tool_calls"][0]["function"]["arguments"]
    assert "backend/pricing.py" in persisted[2]["content"]
    _assert_openai_sequence(chat._history_for_api(history))


def test_oversized_single_round_is_not_dropped_whole(cfg, tmp_path):
    """受测模型一轮并行几十个工具调用时，续轮不能整轮忘记上一轮。"""
    rows = [{"role": "user", "content": "题目提示词：修三个端口"},
            {"role": "assistant", "content": None, "tool_calls": []}]
    rows.pop(1)
    for index in range(150):
        rows.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c%d" % index, "type": "function",
             "function": {"name": "read_file", "arguments": json.dumps({"path": "a%d.py" % index})}}]})
        rows.append({"role": "tool", "tool_call_id": "c%d" % index, "name": "read_file",
                     "content": json.dumps({"path": "a%d.py" % index, "content": "x" * 2000})})
    rows.append({"role": "assistant", "content": "第一轮结论：已改 backend/service.py"})
    rows.append({"role": "user", "content": "第二轮：继续"})
    run = _chat_run(cfg, tmp_path, rows)

    history, dropped = chat._model_history(cfg, chat._read_records(run))
    count, _chars = chat._context_size(chat._group_rounds(history))

    assert dropped == 0, "只剩一轮时不该整轮丢弃"
    assert count <= chat._chat_option(cfg, "max_history"), "窗口必须有上界"
    assert history[0]["content"] == "题目提示词：修三个端口"
    assert "第一轮结论：已改 backend/service.py" in json.dumps(history, ensure_ascii=False)
    assert history[-1]["content"] == "第二轮：继续"
    assert any("已省略" in str(item.get("content")) for item in history)
    _assert_openai_sequence(chat._history_for_api(history))


def test_model_history_honours_configured_window(cfg, tmp_path):
    small = dict(cfg)
    small["chat"] = {"max_context_chars": 1200, "max_history": 12}
    rows = _tool_round("第一轮题目", "c1", "run_command", {"command": ["pytest", "-q"]},
                       {"exit_code": 0, "stdout": "ok " * 4000, "stderr": "", "output_limited": True},
                       "第一轮结论：p2p 全绿")
    rows += _tool_round("第二轮题目", "c2", "read_file", {"path": "a.py"},
                        {"path": "a.py", "content": "y" * 8000}, "第二轮结论：读到内容")
    rows.append({"role": "user", "content": "第三轮：收尾"})
    run = _chat_run(cfg, tmp_path, rows)

    wide, _ = chat._model_history(cfg, chat._read_records(run))
    narrow, dropped = chat._model_history(small, chat._read_records(run))

    assert len(narrow) < len(wide), "配置收紧窗口要真的生效"
    assert narrow[0]["content"] == "第一轮题目", "第一条题目提示词必须钉住"
    assert narrow[-1]["content"] == "第三轮：收尾"
    assert dropped >= 0
    _assert_openai_sequence(chat._history_for_api(narrow))


def test_model_history_bad_window_values_fall_back(cfg, tmp_path):
    broken = dict(cfg)
    broken["chat"] = {"max_history": "很多", "max_context_chars": 0}

    rows = _tool_round("题目", "c1", "list_files", {"path": "."}, {"path": ".", "entries": ["a"], "truncated": False}, "结论")
    rows.append({"role": "user", "content": "继续"})
    run = _chat_run(cfg, tmp_path, rows)

    history, dropped = chat._model_history(broken, chat._read_records(run))
    assert history and dropped == 0, "窗口数字写错不该让对话拿不到上下文"


def test_history_for_api_drops_orphans_and_unanswered_calls():
    rows = [
        {"role": "tool", "tool_call_id": "ghost", "content": "{}"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "user", "content": "接着说"},
    ]

    api = chat._history_for_api(rows)

    assert [item["role"] for item in api] == ["assistant", "user"]
    assert "tool_calls" not in api[0], "没等到响应的 tool_calls 必须摘掉，否则服务商 400"
    assert api[0]["content"] == "（工具调用及其结果已省略）"


def test_repeated_reads_elide_the_older_copy_within_one_round(cfg, tmp_path, monkeypatch):
    run, sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    util.write_text_atomic(os.path.join(sandbox_root, "a.py"), "第一版内容")
    calls = []
    first = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]}
    second = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "r2", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]}
    third = {"choices": [{"message": {"role": "assistant", "content": "读完了"}}]}

    responses = [first, second, third]

    def fake_post(url, payload, key, timeout):
        calls.append(json.loads(json.dumps(payload)))
        return responses.pop(0)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    chat.send(cfg, run, "读两次同一个文件")

    tool_contents = [m["content"] for m in calls[-1]["messages"] if m["role"] == "tool"]
    assert "已省略" in tool_contents[0], "同一路径只保留最后一次原文"
    assert "第一版内容" in tool_contents[-1]
    persisted = chat.messages(run)
    assert all("第一版内容" in str(item.get("content")) for item in persisted if item["role"] == "tool"), \
        "落盘记录仍是全量原文"


def test_run_command_blocks_git_network_operations(cfg, tmp_path):
    _run, sandbox_root = _ready_run(cfg, tmp_path)

    for command in (["git", "push", "origin"], ["git", "fetch", "origin"], ["git", "pull"]):
        with pytest.raises(ValueError, match="网络"):
            chat._tool_run_command(sandbox_root, {"command": command})


def test_start_send_acks_at_once_and_finishes_in_background(cfg, tmp_path, monkeypatch):
    """一轮对话不再占用请求：先回执，前端靠轮询接回结果。"""
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    release = threading.Event()

    def slow_post(url, payload, key, timeout):
        assert release.wait(10), "回执之后模型调用才该发生"
        return {"choices": [{"message": {"role": "assistant", "content": "后台这一轮跑完了。"}}]}

    monkeypatch.setattr(chat, "_post_json", slow_post)
    ack = chat.start_send(cfg, run, "继续处理")

    assert ack == {"accepted": True, "run_id": run["run_id"], "chat_busy": True}
    # 回执与后台线程之间不能有「看起来已空闲」的空窗，否则前端会立刻解锁输入。
    assert chat.send_active(run["run_id"]) is True

    release.set()
    deadline = time.time() + 10
    while chat.send_active(run["run_id"]) and time.time() < deadline:
        time.sleep(0.02)

    assert chat.send_active(run["run_id"]) is False
    assert [message["role"] for message in chat.messages(run)] == ["user", "assistant"]


def test_start_send_records_preflight_failure(cfg, tmp_path):
    """预检阶段就失败的发送必须留下记录。

    回执之后前端不再拿到任何 HTTP 错误，这类错误只在后台线程里抛出；
    不落盘的话界面会停在「模型处理中」然后再也没有下文。
    """
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    run["status"] = "grading"
    runs.save_run(cfg, run)

    chat.start_send(cfg, run, "继续处理")
    deadline = time.time() + 5
    while chat.send_active(run["run_id"]) and time.time() < deadline:
        time.sleep(0.02)

    rows = chat.messages(run)
    assert [row.get("status") for row in rows] == ["error"]
    assert rows[0]["error_code"] == errors.E_RUN_BUSY
    assert chat.send_active(run["run_id"]) is False


def test_send_active_keeps_busy_while_a_send_is_queued():
    """前一条收尾不能把仍在排队的后一条误报成空闲。"""
    chat._enter_active("QUEUED-RUN")
    chat._enter_active("QUEUED-RUN")
    try:
        chat._exit_active("QUEUED-RUN")
        assert chat.send_active("QUEUED-RUN") is True
    finally:
        chat._exit_active("QUEUED-RUN")
    assert chat.send_active("QUEUED-RUN") is False
