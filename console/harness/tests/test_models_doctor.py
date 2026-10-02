"""模型档案的「可用性」出口：GET /api/models 的诊断字段 + POST /api/models/test。

两条红线：
1. 只回环境变量**名字**与「有没有取到值」，密钥取值一律不出服务端；
2. 体检只认已保存的档案 id，请求体里的 base_url 之类的地址不参与寻址（SSRF）。

所有网络都走 monkeypatch 掉的 ``chat._get_json``，测试绝不打真接口。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if CONSOLE_DIR not in sys.path:
    sys.path.insert(0, CONSOLE_DIR)

import server  # noqa: E402
from harness import chat, config, errors, keyring, runs  # noqa: E402

from test_api import _client, _serve, _stop  # noqa: E402

SECRET = "sk-viewtest-NEVER-LEAK"
#: 2026-10-01 重构：去掉 key_env（密钥改按供应商存），加入 provider / 容量字段。
VIEW_FIELDS = ("id", "name", "provider_id", "provider_name", "qualified_id",
               "protocol", "api_mode", "base_url", "model",
               "context_window", "max_tokens", "key_masked", "note")
DIAGNOSTIC_FIELDS = ("key_candidates", "key_env_effective", "key_present", "ready")


def _profile(**overrides):
    model = {"id": "doc", "name": "doc-model", "provider_id": "DOC",
             "provider_name": "DOC", "qualified_id": "DOC::doc",
             "protocol": "openai", "api_mode": "chat_completions",
             "base_url": "https://doctor.test/v1", "model": "doc-model",
             "context_window": 262144, "max_tokens": 32768,
             "key_masked": "sk-****3f9a", "note": "主力档案"}
    model.update(overrides)
    return model


def _provider(pid="DOC"):
    """一个供应商条目（含模型清单），用于写回路径的测试。"""
    return {"id": pid, "display_name": pid, "protocol": "openai",
            "api_mode": "chat_completions", "base_url": "https://doctor.test/v1",
            "default_context_window": 262144, "default_max_tokens": 32768,
            "note": "", "models": [{"id": "doc-model", "name": "doc-model",
                                    "context_window": 262144, "max_tokens": 32768, "note": ""}]}


@pytest.fixture
def clean_env(monkeypatch):
    """把档案会读到的环境变量先清干净，避免宿主机的真实密钥左右结论。"""
    for name in ("DOC_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY", "MODEL_OTHER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def probe(monkeypatch):
    """假探测：记录调用，返回一份可用列表。"""
    calls = []

    def install(result=None):
        def fake_get(url, key="", timeout=None):
            calls.append({"url": url, "key": key, "timeout": timeout})
            return dict(result if result is not None else
                        {"status": 200, "value": {"data": [{"id": "doc-model"}]}, "error": ""})
        monkeypatch.setattr(chat, "_get_json", fake_get)
        return calls

    return install


# ---------------------------------------------------------------- 档案视图

def test_model_view_adds_key_diagnostics_without_the_value(clean_env):
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    view = runs._model_view(_profile())

    assert view["key_candidates"] == ["DOC_API_KEY", "MODEL_DOC_API_KEY", "OPENAI_API_KEY"]
    # 本用例只设了模型级变量，供应商级没设 → 生效的是模型级那个
    assert view["key_env_effective"] == "MODEL_DOC_API_KEY"
    assert view["key_present"] is True
    assert view["ready"] is True
    # 原有脱敏行为不变：档案里只有 key_masked，且取值不回显
    assert view["key_masked"] == "sk-****3f9a"
    assert SECRET not in json.dumps(view, ensure_ascii=False)
    for field in VIEW_FIELDS + DIAGNOSTIC_FIELDS:
        assert field in view


def test_model_view_effective_falls_back_to_the_variable_to_set(clean_env):
    """没配密钥时也要给出「该往哪个变量写」，UI 才能直接显示变量名。"""
    view = runs._model_view(_profile())

    assert view["key_present"] is False
    assert view["key_env_effective"] == "DOC_API_KEY"
    assert view["ready"] is False


def test_model_view_honours_the_provider_env_var(clean_env):
    """供应商级变量名优先于模型级——同一中转站多个模型共用一把密钥。"""
    clean_env.setenv("DOC_API_KEY", SECRET)
    view = runs._model_view(_profile())

    assert view["key_candidates"][0] == "DOC_API_KEY"
    assert view["key_env_effective"] == "DOC_API_KEY"
    assert view["key_present"] is True


@pytest.mark.parametrize("field,value,ready", [
    ("protocol", "anthropic", False),      # 内置对话只接 OpenAI 兼容
    ("api_mode", "responses", False),      # 协议接得住但形态不支持工具闭环
    ("base_url", "", False),               # 没填地址
    ("base_url", "ftp://doctor.test/v1", False),
])
def test_ready_is_the_and_of_protocol_base_url_and_key(clean_env, field, value, ready):
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    assert runs._model_view(_profile(**{field: value}))["ready"] is ready


def test_ready_is_false_when_the_key_is_missing_even_otherwise_clean(clean_env):
    assert runs._model_view(_profile())["ready"] is False


def test_list_models_covers_every_profile(cfg, clean_env):
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    cfg["models"] = [_profile(), _profile(id="other", key_masked="", model="")]
    views = runs.list_models(cfg)

    assert [v["id"] for v in views] == ["doc", "other"]
    assert [v["ready"] for v in views] == [True, False]
    assert SECRET not in json.dumps(views, ensure_ascii=False)


def test_diagnostic_fields_are_never_persisted(cfg, clean_env, monkeypatch, tmp_path):
    """诊断字段是「服务端此刻的环境」，写进 config.json 就成了过期事实。"""
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    cfg["providers"] = [_provider("DOC"), _provider("other")]
    saved = []
    monkeypatch.setattr(config, "update_providers",
                        lambda providers: saved.append(json.loads(json.dumps(providers))))
    # 密钥写路径也要挡住：本用例会带 api_key，keyring 会写真实密钥文件。
    # conftest 的保险丝会拦，但用例应该自己指到临时文件，而不是靠兜底报错。
    monkeypatch.setattr(keyring, "path", lambda: str(tmp_path / "keys.local.json"))

    runs.upsert_provider(cfg, {"id": "DOC", "protocol": "openai", "api_mode": "chat_completions",
                               "base_url": "https://doctor.test/v1",
                               "models": [{"id": "doc-model"}],
                               "key_masked": "sk-****3f9a"})
    runs.delete_provider(cfg, "other")

    assert len(saved) == 2
    # 落盘的 provider 记录只含可持久化字段：key_masked 在，诊断字段不在
    for record in saved[0]:
        assert "key_present" not in record and "ready" not in record
    blob = json.dumps(saved, ensure_ascii=False)
    for leaked in DIAGNOSTIC_FIELDS + ("key_present", "ready", SECRET):
        assert leaked not in blob


# ---------------------------------------------------------------- 体检路由

def test_route_is_registered_on_the_live_server(cfg, clean_env, probe, monkeypatch):
    """真起一次本地服务，确认 POST /api/models/test 确实可达。"""
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    cfg["models"] = [_profile()]
    calls = probe()

    httpd, port, thread = _serve(monkeypatch, lambda: cfg)
    live = _client(port)
    try:
        status, body, headers = live("/api/models/test", method="POST", body={"id": "doc"})
        assert status == 200, body
        doc = json.loads(body)
        assert doc["ok"] is True
        assert [s["id"] for s in doc["stages"]] == ["key", "base_url", "reach", "model"]
        assert doc["stages"][0]["detail"] == "MODEL_DOC_API_KEY"
        assert calls[0]["url"] == "https://doctor.test/v1/models"
        assert SECRET not in body

        listed = json.loads(live("/api/models")[1])["models"][0]
        assert listed["ready"] is True and listed["key_present"] is True
    finally:
        _stop(httpd, thread)


def _call_router(cfg, body, method="POST", path="/api/models/test"):
    handler, params = server.ROUTER.match(method, path)
    return handler({"cfg": cfg, "body": body, "query": {}, "method": method, **params})


def test_router_matches_the_test_endpoint_exactly(clean_env):
    """路由是 ^…$ 精确匹配：/api/models 不会被 /api/models/test 抢走，反之亦然。"""
    for method, path in (("POST", "/api/models/test"), ("GET", "/api/models"),
                         ("POST", "/api/models"), ("DELETE", "/api/models")):
        handler, params = server.ROUTER.match(method, path)
        assert handler is not None
        assert params == {}
    with pytest.raises(errors.HarnessError) as caught:
        server.ROUTER.match("GET", "/api/models/test")
    assert caught.value.code == errors.E_METHOD_NOT_ALLOWED


def test_doctor_endpoint_ignores_addresses_from_the_request_body(cfg, clean_env, probe):
    """SSRF 闸门：档案没存的 base_url 不能借这个接口让本机去访问。"""
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    cfg["models"] = [_profile()]
    calls = probe()

    payload, content_type = _call_router(cfg, {
        "id": "doc",
        "base_url": "http://169.254.169.254/latest/meta-data/",
        "key_env": "STOLEN_KEY",
    })

    assert content_type == "application/json; charset=utf-8"
    assert calls[0]["url"] == "https://doctor.test/v1/models"      # 服务端存的地址
    assert payload["stages"][1]["detail"] == "https://doctor.test/v1"
    assert "169.254" not in json.dumps(payload, ensure_ascii=False)


def test_doctor_endpoint_requires_a_saved_profile_id(cfg, clean_env, probe):
    clean_env.setenv("MODEL_DOC_API_KEY", SECRET)
    cfg["models"] = [_profile()]
    probe()

    with pytest.raises(errors.HarnessError) as missing_id:
        _call_router(cfg, {})
    assert missing_id.value.code == errors.E_BAD_REQUEST

    with pytest.raises(errors.HarnessError) as unknown:
        _call_router(cfg, {"id": "never-saved"})
    assert unknown.value.code == errors.E_MODEL_NOT_FOUND
    assert unknown.value.http_status == 404


def test_doctor_endpoint_reports_a_broken_profile_as_data(cfg, clean_env, probe):
    """档案坏了也是 200 + 业务结论：前端按档位渲染，不靠 HTTP 状态码分支。"""
    cfg["models"] = [_profile(base_url="")]
    calls = probe({"status": 200, "value": {"data": [{"id": "doc-model"}]}, "error": ""})

    payload, _ = _call_router(cfg, {"id": "doc"})

    assert calls == []
    assert payload["ok"] is False
    stages = {s["id"]: s for s in payload["stages"]}
    assert stages["key"]["ok"] is False
    assert stages["base_url"]["ok"] is False
    assert stages["reach"]["ok"] is None and stages["model"]["ok"] is None
