"""Focused coverage for the built-in Chat Completions tool loop."""

from __future__ import annotations

import json
import os
import sys

import pytest

from conftest import make_run
from harness import chat, errors, util


#: doctor 用例的档案 id 与「密钥」——一切网络都走 monkeypatch 的假探测，
#: 绝不打真接口；密钥值只用来断言它没有出现在返回值里。
DOCTOR_ID = "doc"
DOCTOR_SECRET = "sk-doctest-NEVER-LEAK"
DOCTOR_STAGE_KEYS = {"id", "ok", "detail", "http_status", "hint"}


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


# ---------------------------------------------------------------- 密钥优先级

def test_key_candidates_is_the_single_source_of_env_var_precedence():
    """key → MODEL_<ID>_API_KEY → OPENAI_API_KEY，且只回变量名。

    前端与 doctor 都从这里取顺序，不再各自复刻一遍。
    """
    assert chat.key_candidates({"id": "gpt-5", "protocol": "openai", "key_env": "TEAM_KEY"}) == [
        "TEAM_KEY", "MODEL_GPT_5_API_KEY", "OPENAI_API_KEY"]
    # 非 openai 协议不该建议去读 OPENAI_API_KEY
    assert chat.key_candidates({"id": "claude", "protocol": "anthropic"}) == ["MODEL_CLAUDE_API_KEY"]
    # 非法变量名（有空格/以数字开头）直接丢弃，不会变成「查一个不存在的环境变量」
    assert chat.key_candidates({"id": "claude-opus", "protocol": "openai", "key_env": "bad name"}) == [
        "MODEL_CLAUDE_OPUS_API_KEY", "OPENAI_API_KEY"]
    # key_env 与 OPENAI_API_KEY 重合时去重，保持首次出现顺序
    assert chat.key_candidates({"id": "x", "protocol": "openai", "key_env": "OPENAI_API_KEY"}) == [
        "OPENAI_API_KEY", "MODEL_X_API_KEY"]
    # 旧字段名 api_key_env 仍然认（历史档案）
    assert chat.key_candidates({"id": "y", "protocol": "openai", "api_key_env": "LEGACY_KEY"})[0] == "LEGACY_KEY"


def test_model_key_returns_first_candidate_with_a_value(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEAM_KEY", "   ")          # 只有空白视同没配
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-lower-priority")
    model = {"id": DOCTOR_ID, "protocol": "openai", "key_env": "TEAM_KEY"}

    assert chat._model_key(model) == DOCTOR_SECRET
    assert chat.resolve_key(model) == ("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    status = chat.key_status(model)
    assert status == {"candidates": ["TEAM_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY"],
                      "effective": "MODEL_DOC_API_KEY", "present": True}
    # 只读诊断里绝不出现取值本身
    assert DOCTOR_SECRET not in json.dumps(status, ensure_ascii=False)


def test_model_key_failure_names_variables_but_never_values(monkeypatch):
    for name in ("TEAM_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    model = {"id": DOCTOR_ID, "protocol": "openai", "key_env": "TEAM_KEY"}

    with pytest.raises(errors.HarnessError) as caught:
        chat._model_key(model)
    assert caught.value.code == errors.E_MODEL_INVALID
    dumped = json.dumps([caught.value.message, caught.value.detail], ensure_ascii=False)
    assert "TEAM_KEY" in dumped and "OPENAI_API_KEY" in dumped
    assert "sk-" not in dumped


def test_has_usable_base_url_requires_an_explicit_http_base_url():
    assert chat.has_usable_base_url({"base_url": "https://doctor.test/v1"}) is True
    assert chat.has_usable_base_url({"base_url": ""}) is False
    assert chat.has_usable_base_url({"base_url": "ftp://doctor.test/v1"}) is False


# ------------------------------------------------------------------ doctor

def _doctor_profile(cfg, monkeypatch, **overrides):
    """一个可诊断的档案，并把它会读的环境变量全部清空。"""
    model = {"id": DOCTOR_ID, "protocol": "openai", "api_mode": "chat_completions",
             "base_url": "https://doctor.test/v1", "model": "doc-model"}
    model.update(overrides)
    cfg["models"] = [model]
    for name in chat.key_candidates(model):
        monkeypatch.delenv(name, raising=False)
    return model


def _fake_probe(monkeypatch, result):
    """替换 doctor 唯一的网络出口，记录调用参数（测试绝不出网）。"""
    calls = []

    def fake_get(url, key="", timeout=None):
        calls.append({"url": url, "key": key, "timeout": timeout})
        return dict(result)

    monkeypatch.setattr(chat, "_get_json", fake_get)
    return calls


def _stages(result):
    return {stage["id"]: stage for stage in result["stages"]}


def test_doctor_passes_every_stage(cfg, monkeypatch):
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    calls = _fake_probe(monkeypatch, {
        "status": 200,
        "value": {"object": "list", "data": [{"id": "doc-model"}, {"id": "other-model"}]},
        "error": ""})

    result = chat.doctor(cfg, DOCTOR_ID)

    assert result["id"] == DOCTOR_ID
    assert result["ok"] is True
    assert result["endpoint"] == "https://doctor.test/v1/chat/completions"
    assert result["checked_at"]
    assert [stage["id"] for stage in result["stages"]] == list(chat.DOCTOR_STAGES)
    assert [stage["ok"] for stage in result["stages"]] == [True, True, True, True]
    assert all(set(stage) == DOCTOR_STAGE_KEYS for stage in result["stages"])
    stages = _stages(result)
    assert stages["key"]["detail"] == "MODEL_DOC_API_KEY"          # 只有变量名
    assert stages["base_url"]["detail"] == "https://doctor.test/v1"
    assert stages["reach"]["http_status"] == 200
    assert stages["model"]["http_status"] is None

    assert calls == [{"url": "https://doctor.test/v1/models", "key": DOCTOR_SECRET,
                      "timeout": chat.DOCTOR_TIMEOUT_S}]
    assert DOCTOR_SECRET not in json.dumps(result, ensure_ascii=False)


def test_doctor_missing_key_is_a_result_not_an_exception(cfg, monkeypatch):
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    calls = _fake_probe(monkeypatch, {"status": 401, "value": None, "error": "Unauthorized"})

    result = chat.doctor(cfg, DOCTOR_ID)
    stages = _stages(result)

    assert result["ok"] is False
    assert stages["key"]["ok"] is False
    assert stages["key"]["detail"] == "MODEL_DOC_API_KEY"          # 告诉你该往哪个变量写
    assert "MODEL_DOC_API_KEY" in stages["key"]["hint"]
    assert calls[0]["key"] == ""                                    # 没密钥就不发 Authorization 头
    assert stages["reach"]["ok"] is False and stages["reach"]["http_status"] == 401
    assert "未认证" in stages["reach"]["hint"]
    assert stages["model"]["ok"] is None                            # 列表没拿到，不许臆断


@pytest.mark.parametrize("status,value", [
    (404, None),
    (405, None),
    (200, {"object": "list"}),        # 有响应但读不出一段列表
    (200, None),                      # 非 JSON
])
def test_doctor_treats_absent_model_list_as_unknown(cfg, monkeypatch, status, value):
    """没公开 /models 是服务商的常态，不能报成档案坏了。"""
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    _fake_probe(monkeypatch, {"status": status, "value": value, "error": ""})

    result = chat.doctor(cfg, DOCTOR_ID)
    stages = _stages(result)

    assert stages["reach"]["ok"] is None
    assert "模型列表" in stages["reach"]["hint"]
    assert stages["reach"]["http_status"] == status
    assert stages["model"]["ok"] is None
    assert result["ok"] is True        # 被跳过的档位不参与总判定


def test_doctor_flags_a_model_name_the_provider_does_not_offer(cfg, monkeypatch):
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    _fake_probe(monkeypatch, {"status": 200, "value": {"data": [{"id": "a"}, "b"]}, "error": ""})

    result = chat.doctor(cfg, DOCTOR_ID)
    stages = _stages(result)

    assert stages["reach"]["ok"] is True
    assert stages["model"]["ok"] is False
    assert "doc-model" in stages["model"]["detail"]
    assert "a" in stages["model"]["hint"]
    assert result["ok"] is False


@pytest.mark.parametrize("base_url", ["", "ftp://doctor.test/v1"])
def test_doctor_refuses_to_probe_without_a_usable_base_url(cfg, monkeypatch, base_url):
    _doctor_profile(cfg, monkeypatch, base_url=base_url)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    calls = _fake_probe(monkeypatch, {"status": 200, "value": {"data": [{"id": "doc-model"}]}, "error": ""})

    result = chat.doctor(cfg, DOCTOR_ID)
    stages = _stages(result)

    assert calls == []                                   # 一个字节都不许发出去
    assert stages["base_url"]["ok"] is False
    assert stages["reach"]["ok"] is None and stages["model"]["ok"] is None
    assert result["ok"] is False
    assert result["endpoint"] is None


def test_doctor_reports_connection_failures_without_raising(cfg, monkeypatch):
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    _fake_probe(monkeypatch, {"status": None, "value": None, "error": "timed out"})

    result = chat.doctor(cfg, DOCTOR_ID)
    stages = _stages(result)

    assert stages["reach"]["ok"] is False and stages["reach"]["http_status"] is None
    assert "timed out" in stages["reach"]["detail"]
    assert stages["model"]["ok"] is None
    assert result["ok"] is False


def test_doctor_survives_a_probe_that_explodes(cfg, monkeypatch):
    """体检接口的契约是「永远返回业务结果」，连内部异常也要摊成档位。"""
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)

    def boom(url, key="", timeout=None):
        raise RuntimeError("provider said: %s" % DOCTOR_SECRET)

    monkeypatch.setattr(chat, "_get_json", boom)
    result = chat.doctor(cfg, DOCTOR_ID)

    assert _stages(result)["reach"]["ok"] is False
    assert DOCTOR_SECRET not in json.dumps(result, ensure_ascii=False)


def test_doctor_never_leaks_the_secret_into_any_field(cfg, monkeypatch):
    """服务商把密钥原文嵌进错误信息也要擦掉（标签式与裸值两种形态）。"""
    _doctor_profile(cfg, monkeypatch)
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    _fake_probe(monkeypatch, {
        "status": 400, "value": None,
        "error": "invalid api key: %s; Authorization: Bearer %s; raw %s"
                 % (DOCTOR_SECRET, DOCTOR_SECRET, DOCTOR_SECRET)})

    result = chat.doctor(cfg, DOCTOR_ID)
    dumped = json.dumps(result, ensure_ascii=False)

    assert DOCTOR_SECRET not in dumped
    assert "[redacted]" in dumped
    assert "MODEL_DOC_API_KEY" in dumped        # 变量名可以出现，取值不行


def test_doctor_needs_an_already_saved_profile(cfg, monkeypatch):
    _doctor_profile(cfg, monkeypatch)
    with pytest.raises(errors.HarnessError) as caught:
        chat.doctor(cfg, "never-saved")
    assert caught.value.code == errors.E_MODEL_NOT_FOUND
    assert caught.value.http_status == 404
