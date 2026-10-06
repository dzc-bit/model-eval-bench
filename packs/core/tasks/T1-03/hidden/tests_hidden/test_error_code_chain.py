"""T1-03 隐藏测试：错误分类、有限流终态与跨端契约。"""

from __future__ import annotations

import ast
import json
import re
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from astock_backtester.ai.errors import AiUpstreamError
from astock_backtester.service import LocalDataUnavailable, _stream_error_code, create_server

_OPENER = build_opener(ProxyHandler({}))
_AI_ERRORS = Path("backend/astock_backtester/ai/errors.py")
_AI_TYPES = Path("frontend/src/aiTypes.ts")


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_json_allow_error(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _request_ndjson(url: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=5) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def _start_server(tmp_path):
    warehouse = tmp_path / "本地数据仓"
    warehouse.mkdir(exist_ok=True)
    server = create_server(host="127.0.0.1", port=0, cache_dir=warehouse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def _configure(base: str) -> None:
    _request_json(
        "POST",
        f"{base}/ai/config",
        {"base_url": "http://127.0.0.1:9", "api_key": "sk-test", "model": "demo"},
    )


def _declared_ai_error_codes() -> set[str]:
    tree = ast.parse(_AI_ERRORS.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "AiError" and not any(isinstance(base, ast.Name) and base.id == "AiError" for base in node.bases):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "code" for target in statement.targets):
                if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                    codes.add(statement.value.value)
    return codes


def test_specific_stream_failures_keep_distinct_codes():
    assert _stream_error_code(LocalDataUnavailable("仓库为空")) == "no_local_data"
    assert _stream_error_code(KeyError("strategy")) == "payload_error"


def test_validation_and_unknown_stream_failures_keep_their_fallbacks():
    assert _stream_error_code(ValueError("日期不合法")) == "validation_error"
    assert _stream_error_code(RuntimeError("上游断开")) == "request_failed"


def test_unconfigured_chat_ends_with_one_typed_error(tmp_path):
    server, thread, port = _start_server(tmp_path)
    try:
        events = _request_ndjson(f"http://127.0.0.1:{port}/ai/chat/stream", {"message": "分析一下"})
        assert events == [
            {
                "type": "error",
                "code": "ai_not_configured",
                "message": "AI 服务尚未配置，请先在设置中填写 base_url、API Key 和模型名。",
            }
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_worker_failure_keeps_session_then_emits_error_terminal(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _configure(base)

    class FailingAgent:
        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event, cancel=None):
            raise AiUpstreamError("模型服务调用失败：上游暂不可用")

    monkeypatch.setattr(server.state.ai_service(), "_agent", FailingAgent())
    try:
        events = _request_ndjson(f"{base}/ai/chat/stream", {"message": "分析一下"})
        assert [event["type"] for event in events] == ["session", "error"]
        assert events[-1]["code"] == "ai_upstream_error"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_successful_chat_still_ends_with_result(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _configure(base)

    class SuccessfulAgent:
        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event, cancel=None):
            on_event({"type": "token", "text": "完成"})
            session["display"].append({"role": "assistant", "content": "完成", "tool_steps": [], "ts": "now"})
            return {}

    monkeypatch.setattr(server.state.ai_service(), "_agent", SuccessfulAgent())
    try:
        events = _request_ndjson(f"{base}/ai/chat/stream", {"message": "分析一下"})
        assert events[-1]["type"] == "result"
        assert sum(event["type"] == "result" for event in events) == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_same_ai_failure_keeps_its_code_in_json_and_stream(tmp_path, monkeypatch):
    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _configure(base)
    service = server.state.ai_service()

    def fail_oneshot(_scene, _context):
        raise AiUpstreamError("模型服务调用失败：同一上游故障")

    class FailingAgent:
        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event, cancel=None):
            raise AiUpstreamError("模型服务调用失败：同一上游故障")

    monkeypatch.setattr(service, "insight_oneshot", fail_oneshot)
    monkeypatch.setattr(service, "_agent", FailingAgent())
    try:
        status, body = _request_json_allow_error(
            "POST", f"{base}/ai/insight/oneshot", {"scene": "results_overview", "context": {}}
        )
        events = _request_ndjson(f"{base}/ai/chat/stream", {"message": "分析一下"})
        assert status == 400
        assert body["code"] == "ai_upstream_error"
        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == body["code"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_busy_session_chat_ends_with_typed_error_terminal(tmp_path, monkeypatch):
    """上一轮仍在生成时，新一轮必须在流内拿到 ai_session_busy 终态（题面症状一
    明说的第三种失败），而不是以空流收尾；在途轮次同时不许被挤坏。"""
    from astock_backtester.ai import facade as ai_facade

    server, thread, port = _start_server(tmp_path)
    base = f"http://127.0.0.1:{port}"
    _configure(base)
    monkeypatch.setattr(ai_facade, "AI_SESSION_LOCK_TIMEOUT_SECONDS", 0.05)

    entered = threading.Event()
    release = threading.Event()

    class BlockingAgent:
        def run(self, *, session, user_message, system_prompt, max_steps, context=None, on_event, cancel=None):
            entered.set()
            assert release.wait(timeout=10), "第一轮 agent 没有被释放"
            session["display"].append(
                {"role": "assistant", "content": "完成", "tool_steps": [], "ts": "now"}
            )
            return {}

    monkeypatch.setattr(server.state.ai_service(), "_agent", BlockingAgent())
    first_events: list[dict] = []

    def run_first() -> None:
        first_events.extend(_request_ndjson(f"{base}/ai/chat/stream", {"message": "先问一个"}))

    first_thread = threading.Thread(target=run_first, daemon=True)
    first_thread.start()
    try:
        assert entered.wait(timeout=5), "第一轮 worker 没有进入生成"
        session_id = (server.state.ai_service().list_sessions()[0] or {}).get("session_id")
        assert session_id
        events = _request_ndjson(
            f"{base}/ai/chat/stream", {"message": "追问", "session_id": session_id}
        )
        assert events == [
            {
                "type": "error",
                "code": "ai_session_busy",
                "message": "上一轮回答仍在生成中，请稍候再发送新消息。",
            }
        ]
    finally:
        release.set()
        first_thread.join(timeout=10)
        server.shutdown()
        thread.join(timeout=5)
    # 在途轮次必须完好收尾：占锁期间的新请求拿到 busy 终态，不等于在途轮次被挤断。
    assert first_events and first_events[-1]["type"] == "result"


def test_frontend_translator_mirrors_every_declared_ai_error_code():
    source = _AI_TYPES.read_text(encoding="utf-8")
    marker = "export function translateAiError"
    assert marker in source
    codes = _declared_ai_error_codes()
    assert codes == {
        "request_failed",
        "ai_not_configured",
        "ai_upstream_error",
        "ai_session_busy",
        "ai_session_not_found",
        "ai_memory_not_found",
    }
    # 镜像口径看整个翻译模块：逐码分支写在翻译函数里、或放在函数旁的查找表里，
    # 是行为等价的写法，守卫不耦合定义位置；单双引号均可。**裸标识符键也算**
    # （`{ ai_not_configured: "…" }` 与 `{ "ai_not_configured": "…" }` 在 TS 里
    # 完全等价，2026-10-05 T1-03 复核补上的等价形态——此前只认带引号字面量，
    # 会把行为正确、只是用了裸键查找表的实现误判成缺类别）。缺任何一个
    # 已声明类别仍然判红（基线态的既有缺口靠它兜住）。
    missing = sorted(
        code
        for code in codes
        if f'"{code}"' not in source
        and f"'{code}'" not in source
        and not re.search(r"^\s*%s\s*:" % re.escape(code), source, re.M)
    )
    assert not missing, f"页面错误翻译缺少后端已声明类别：{missing}"
