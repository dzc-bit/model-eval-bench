"""Focused coverage for the built-in Chat Completions tool loop."""

from __future__ import annotations

import json
import os
import sys

import pytest

from conftest import make_run
from harness import chat, util


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
