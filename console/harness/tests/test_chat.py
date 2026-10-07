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


def test_system_prompt_discloses_the_edit_boundary(cfg, tmp_path, monkeypatch):
    """越界按路径判整轮作废，就必须先把这条规则告诉模型。"""
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    calls = []
    responses = [{"choices": [{"message": {"role": "assistant", "content": "改好了。"}}]}]

    def fake_post(url, payload, key, timeout):
        calls.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    chat.send(cfg, run, "把逻辑修好")

    system = calls[0]["messages"][0]["content"]
    assert "backend/miniapp/**" in system, "题包允许路径要出现在系统提示词里"
    assert "只允许修改" in system


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
    """<供应商 ID>_API_KEY → MODEL_<模型 ID>_API_KEY → OPENAI_API_KEY，且只回变量名。

    前端与 doctor 都从这里取顺序，不再各自复刻一遍。
    2026-10-01 重构：档案自带的 key_env 字段已去掉——密钥现在有两条路
    （本机密钥文件按供应商存 / 环境变量），再多一个自定义变量名字段
    只会让人不知道该填哪个。供应商在前，因为同一中转站的多个模型共用一把密钥。
    """
    assert chat.key_candidates(
        {"id": "hy4-preview", "provider_id": "cbcn", "protocol": "openai"}) == [
        "CBCN_API_KEY", "MODEL_HY4_PREVIEW_API_KEY", "OPENAI_API_KEY"]
    # 非 openai 协议不该建议去读 OPENAI_API_KEY
    assert chat.key_candidates(
        {"id": "claude", "provider_id": "anthropic", "protocol": "anthropic"}) == [
        "ANTHROPIC_API_KEY", "MODEL_CLAUDE_API_KEY"]
    # 没有供应商时退化成只按模型 id 推（历史档案形态）
    assert chat.key_candidates({"id": "legacy", "protocol": "anthropic"}) == ["MODEL_LEGACY_API_KEY"]
    # 供应商 id 与模型 id 推成同一个变量名时去重，保持首次出现顺序
    assert chat.key_candidates(
        {"id": "same", "provider_id": "same", "protocol": "openai"}) == [
        "SAME_API_KEY", "MODEL_SAME_API_KEY", "OPENAI_API_KEY"]
    # 非法字符会被折成下划线，不会变成「查一个不存在的环境变量」
    assert chat.key_candidates(
        {"id": "x", "provider_id": "my relay", "protocol": "openai"}) == [
        "MY_RELAY_API_KEY", "MODEL_X_API_KEY", "OPENAI_API_KEY"]


def test_model_key_returns_first_candidate_with_a_value(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEAM_KEY", "   ")          # 只有空白视同没配
    monkeypatch.setenv("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-lower-priority")
    model = {"id": DOCTOR_ID, "provider_id": "TEAM", "protocol": "openai"}

    assert chat._model_key(model) == DOCTOR_SECRET
    assert chat.resolve_key(model) == ("MODEL_DOC_API_KEY", DOCTOR_SECRET)
    status = chat.key_status(model)
    assert status == {"candidates": ["TEAM_API_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY"],
                      "effective": "MODEL_DOC_API_KEY", "present": True}
    # 只读诊断里绝不出现取值本身
    assert DOCTOR_SECRET not in json.dumps(status, ensure_ascii=False)


def test_model_key_failure_names_variables_but_never_values(monkeypatch):
    for name in ("TEAM_API_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    model = {"id": DOCTOR_ID, "provider_id": "TEAM", "protocol": "openai"}

    with pytest.raises(errors.HarnessError) as caught:
        chat._model_key(model)
    assert caught.value.code == errors.E_MODEL_INVALID
    dumped = json.dumps([caught.value.message, caught.value.detail], ensure_ascii=False)
    assert "TEAM_API_KEY" in dumped and "OPENAI_API_KEY" in dumped
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


# ---------------------------------------------------------------------------
# 空响应与 finish_reason（2026-10-07：T2-05 空收束被当成自然结束，任务没完成
# 却要用户手动发「继续」——chat_completions 分支必须与另两个分支同样报错）
# ---------------------------------------------------------------------------

def test_empty_response_retries_once_then_reports(cfg, tmp_path, monkeypatch):
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    empty = {"choices": [{"finish_reason": "length",
                          "message": {"role": "assistant", "content": ""}}]}
    calls = []

    def fake_post(url, payload, key, timeout):
        calls.append(json.loads(json.dumps(payload)))
        return dict(empty)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    with pytest.raises(errors.HarnessError) as caught:
        chat.send(cfg, run, "把这个跑完")
    assert caught.value.code == errors.E_CHAT_FAILED
    assert len(calls) == 2, "空响应先原样重试一次再报错"
    assert "length" in caught.value.detail, "finish_reason 要进 detail，能区分截断与抽风"
    rows = chat.messages(run)
    assert rows[-1]["status"] == "error"
    assert not any(row.get("role") == "assistant" and not str(row.get("content") or "").strip()
                   and row.get("status") != "error" for row in rows), \
        "空响应绝不能以正常 assistant 消息的形态落盘收束"


def test_empty_response_recovers_on_retry(cfg, tmp_path, monkeypatch):
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    responses = [
        {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": ""}}]},
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "继续做完了。"}}]},
    ]

    def fake_post(url, payload, key, timeout):
        return responses.pop(0)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    result = chat.send(cfg, run, "继续")

    assert result["message"]["content"] == "继续做完了。"
    assert chat.messages(run)[-1]["finish_reason"] == "stop"


def test_truncated_answer_is_persisted_with_finish_reason(cfg, tmp_path, monkeypatch):
    """finish_reason=length 且正文非空：内容有信息量照常收束，但截断要留档。"""
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    monkeypatch.setattr(chat, "_post_json", lambda url, payload, key, timeout:
                        {"choices": [{"finish_reason": "length",
                                      "message": {"role": "assistant", "content": "改了一半的话…"}}]})
    result = chat.send(cfg, run, "继续")

    assert result["message"]["finish_reason"] == "length"
    assert chat.messages(run)[-1]["finish_reason"] == "length"


def test_provider_output_limit_is_sent_when_profile_has_one(cfg, tmp_path, monkeypatch):
    """不传 max_tokens 时不少服务商按小默认截断输出——思维链吃满额度正文就空了。"""
    run, _sandbox_root = _ready_run(cfg, tmp_path)
    cfg["models"][0]["max_tokens"] = 32768
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    seen = []

    def fake_post(url, payload, key, timeout):
        seen.append(json.loads(json.dumps(payload)))
        return {"choices": [{"message": {"role": "assistant", "content": "完成。"}}]}

    monkeypatch.setattr(chat, "_post_json", fake_post)
    chat.send(cfg, run, "继续")

    assert seen[0]["max_tokens"] == 32768
    # finish_reason 是我们自己的留档字段，不属于服务商消息序列，不能外发
    assert not any("finish_reason" in message for message in seen[0]["messages"])


# ---------------------------------------------------------------------------
# 单轮内守预算与写入清单留存（用户口径：压缩不能等下一轮才做、更不能压到
# 只剩省略号——模型得记得自己写过哪些文件，否则续轮重复劳动）
# ---------------------------------------------------------------------------

def _big_round_rows(rounds=6, body_size=6000):
    rows = [{"role": "user", "content": "题目：把三处失忆都修掉"}]
    for index in range(rounds):
        rows.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": "c%d" % index, "type": "function",
             "function": {"name": "read_file",
                          "arguments": json.dumps({"path": "a%d.py" % index})}}]})
        rows.append({"role": "tool", "tool_call_id": "c%d" % index, "name": "read_file",
                     "content": json.dumps({"path": "a%d.py" % index,
                                            "content": "x" * body_size}, ensure_ascii=False)})
    rows.append({"role": "assistant", "content": "结论：都改完了。"})
    return rows


def test_shrink_plain_window_keeps_head_tail_pairing_and_is_idempotent():
    rows = _big_round_rows()
    small = {"max_context_chars": 8000, "tool_summary_chars": 800, "max_history": 100}

    shrunk, changed = chat._shrink_plain_window(
        [dict(row) for row in rows], small["max_context_chars"],
        small["tool_summary_chars"], keep_head=1)
    again, _ = chat._shrink_plain_window(
        [dict(row) for row in shrunk], small["max_context_chars"],
        small["tool_summary_chars"], keep_head=1)

    assert changed
    _assert_openai_sequence(chat._history_for_api(shrunk))
    assert shrunk[0]["content"] == "题目：把三处失忆都修掉", "组首 user 钉住"
    assert shrunk[-1]["content"] == "结论：都改完了。", "最新正文不降级"
    summaries = [item for item in shrunk if item.get("role") == "tool"
                 and len(str(item.get("content"))) <= 1000]
    assert summaries and "a0.py" in json.dumps(summaries, ensure_ascii=False), \
        "早期工具结果降级成摘要"
    originals = [item for item in shrunk if item.get("role") == "tool"
                 and len(str(item.get("content"))) > 1000]
    assert originals, "尾部保留区里还有全量原文供模型当下使用"
    assert chat._plain_size(again) == chat._plain_size(shrunk), "重复触发不得层层套娃"


def test_send_loop_shrinks_before_requests_and_keeps_full_persisted_log(cfg, tmp_path, monkeypatch):
    small = dict(cfg)
    small["chat"] = {"max_context_chars": 8000, "max_history": 100, "tool_summary_chars": 800}
    run, sandbox_root = _ready_run(small, tmp_path)
    util.write_text_atomic(os.path.join(sandbox_root, "a.py"), "x" * 6000)
    util.write_text_atomic(os.path.join(sandbox_root, "b.py"), "y" * 6000)
    monkeypatch.setenv("MODEL_CHAT_API_KEY", "test-secret")
    calls = []
    # 三跳：前两跳各读一个大文件，第三跳收束。窗口 8000 字符必然撑爆。
    responses = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "r1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "r2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"b.py"}'}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "两份都看完了。"}}]},
    ]

    def fake_post(url, payload, key, timeout):
        calls.append(json.loads(json.dumps(payload)))
        return responses.pop(0)

    monkeypatch.setattr(chat, "_post_json", fake_post)
    chat.send(small, run, "读两个大文件")

    assert len(calls) == 3
    first_round_tools = [m for m in calls[-1]["messages"]
                         if m.get("role") == "tool" and m.get("tool_call_id") == "r1"]
    assert first_round_tools and "x" * 6000 not in first_round_tools[0]["content"], \
        "循环进行中就要把早期工具往返降级，不等下一轮 send"
    persisted = [row for row in chat.messages(run) if row.get("role") == "tool"]
    assert any("x" * 6000 in row["content"] for row in persisted), "落盘记录必须仍是全量"
    _assert_openai_sequence(calls[-1]["messages"])


def test_call_listing_and_gutted_note_keep_write_targets(cfg, tmp_path):
    """塌缩与 gutted 之后，模型对「自己写过哪些文件」的记忆必须还在。"""
    arguments = json.dumps({"path": "backend/sync.py", "content": "正文" * 2000},
                           ensure_ascii=False)
    listing = chat._call_listing([
        {"id": "w", "type": "function",
         "function": {"name": "write_file", "arguments": arguments}},
        {"id": "r", "type": "function",
         "function": {"name": "run_command", "arguments": '{"command":["git","status"]}'}}])
    assert "backend/sync.py" in listing and "写入过" in listing

    # 构造一段会触发 gutted 的历史：塌缩后该轮仍有 4 条消息（>3 门槛）且
    # 收尾正文很长，pop 完可丢的轮次后仍超预算——唯一写入发生在第一轮
    write_round = [
        {"role": "user", "content": "第一轮题目"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w1", "type": "function",
             "function": {"name": "write_file", "arguments": arguments}}]},
        {"role": "tool", "tool_call_id": "w1", "name": "write_file",
         "content": json.dumps({"path": "backend/sync.py", "bytes": 12000}, ensure_ascii=False)},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "w2", "type": "function",
             "function": {"name": "write_file", "arguments": arguments}}]},
        {"role": "tool", "tool_call_id": "w2", "name": "write_file",
         "content": json.dumps({"path": "backend/sync.py", "bytes": 14000}, ensure_ascii=False)},
        {"role": "assistant", "content": "第一轮结论：%s" % ("改完了，" * 300)}]
    rows = list(write_round)
    rows += _big_round_rows(rounds=2, body_size=4000)
    rows.append({"role": "user", "content": "第二轮：继续"})
    run = _chat_run(cfg, tmp_path, rows)

    small = dict(cfg)
    small["chat"] = {"max_context_chars": 1200, "max_history": 100}
    history, _dropped = chat._model_history(small, chat._read_records(run))
    notes = [str(item.get("content")) for item in history if "已省略" in str(item.get("content"))]
    assert notes and "写入过" in notes[-1] and "backend/sync.py" in notes[-1], \
        "gutted 省略行必须带走写入清单（NOTES.md 第五节第 2/4 条）"
    _assert_openai_sequence(chat._history_for_api(history))


def test_record_failure_persists_detail(cfg, tmp_path):
    """只落一句「连接失败」无法归因：detail（已脱敏）要一起进对话记录。"""
    run = _chat_run(cfg, tmp_path, [])
    chat._record_failure(run, errors.E_CHAT_FAILED, "模型接口连接失败，请检查 base_url、网络和服务端密钥。",
                         "URLError: <urlopen error timed out>")

    rows = chat.messages(run)
    assert rows[0]["error_code"] == errors.E_CHAT_FAILED
    assert "timed out" in rows[0]["detail"]
    assert rows[0]["detail"] == chat._clip(rows[0]["detail"], 600)
