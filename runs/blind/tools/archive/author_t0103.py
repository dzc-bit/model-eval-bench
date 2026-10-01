"""T1-03 成题脚本：只读受测仓库，在题包目录内生成注入、锚解与门禁材料。"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T1-03"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

SERVICE_REL = "backend/astock_backtester/service.py"
FACADE_REL = "backend/astock_backtester/ai/facade.py"
AI_ERRORS_REL = "backend/astock_backtester/ai/errors.py"
AI_TYPES_REL = "frontend/src/aiTypes.ts"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    assert count == 1, f"{label} 锚点数量异常：{count}"
    return text.replace(old, new, 1)


def patch_for(rel_path: str, before: str, after: str) -> str:
    patch = build_patch(
        rel_path,
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
    )
    assert patch, f"{rel_path} 没有生成差异"
    return patch


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


service_src = (REPO / SERVICE_REL).read_text(encoding="utf-8")
facade_src = (REPO / FACADE_REL).read_text(encoding="utf-8")
ai_types_src = (REPO / AI_TYPES_REL).read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# 一、三端口注入：分类退化、有限流错误终态缺失、页面按码文案退化
# ---------------------------------------------------------------------------

service_injected = replace_once(
    service_src,
    '''def _stream_error_code(exc: Exception) -> str:
    """Stable machine-readable error codes consumed by the frontend."""
    if isinstance(exc, LocalDataUnavailable):
        return "no_local_data"
    if isinstance(exc, KeyError):
        return "payload_error"
    if isinstance(exc, ValueError):
        return "validation_error"
    return "request_failed"
''',
    '''def _stream_error_code(exc: Exception) -> str:
    """流式失败只区分输入问题与其余异常，具体原因继续放在说明文本里。"""
    if isinstance(exc, ValueError):
        return "validation_error"
    return "request_failed"
''',
    "服务层错误分类",
)

service_injected = replace_once(
    service_injected,
    '''        except AiError as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            self._write_ai_error_event(generator, exc.code, str(exc))
        except Exception as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            self._write_ai_error_event(generator, "request_failed", str(exc))

    def _write_ai_error_event(self, generator: Any, code: str, message: str) -> None:
        generator.close()
        try:
            self._write_ndjson({"type": "error", "code": code, "message": message})
        except ClientDisconnected:
            return
''',
    '''        except AiError as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            # 响应已经开始后只释放生成器，连接关闭由消费端统一识别。
            generator.close()
        except Exception as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            generator.close()
''',
    "服务层异常终态",
)

service_fixed = replace_once(
    service_src,
    '    """Stable machine-readable error codes consumed by the frontend."""',
    '    """把流式异常转换成供页面稳定分支的机器可读类别。"""',
    "锚解错误分类注释",
)

facade_injected = replace_once(
    facade_src,
    '''            if error_holder:
                yield error_holder[0]
''',
    '''            # 哨兵表示本轮消费已经收口，之后不再追加队列外事件。
''',
    "facade 异常终态",
)

ai_types_original_function = '''export function translateAiError(error: unknown): string {
  if (error instanceof Error) {
    // 先按后端稳定码分支：码是契约，message 是给人看的文案，会随措辞变化。
    const code = (error as Error & { code?: unknown }).code;
    const message = error.message;
    if (code === "ai_not_configured" || message.includes("ai_not_configured") || message.includes("尚未配置")) {
      return "AI 服务尚未配置，请点击右上角设置填写 base_url、API Key 和模型名。";
    }
    if (code === "ai_session_busy" || message.includes("ai_session_busy") || message.includes("仍在生成中")) {
      return "上一轮回答还在生成中，请等它结束（或点击停止）后再发送。";
    }
    if (code === "ai_upstream_error" || message.includes("ai_upstream_error") || message.includes("模型服务调用失败")) {
      // 后端 detail 里带着真正的病因（上下文超长 / 401 / 429 / 超时），
      // 整句换成固定文案等于让用户照着“检查网络”盲猜。
      const detail = error.message
        .replace(/^模型服务调用失败/u, "")
        .replace(/^[\\s（）:：-]+/u, "")
        .trim();
      const tail = detail.length > 160 ? `${detail.slice(0, 160)}…` : detail;
      return `模型服务调用失败，请检查网络、API Key 与服务商状态后重试。${
        tail ? `服务商返回：${tail}。` : ""
      }若是回答过长或上下文超限，请新建对话后拆小问题再问。`;
    }
    return error.message;
  }
  return "AI 请求失败，请稍后重试。";
}
'''

ai_types_injected_function = '''export function translateAiError(error: unknown): string {
  if (error instanceof Error) {
    const code = (error as Error & { code?: unknown }).code;
    // 已归类的助手故障统一收口，未识别异常仍沿用服务端说明。
    if (code === "ai_not_configured" || code === "ai_session_busy" || code === "ai_upstream_error") {
      return "AI 请求失败，请稍后重试。";
    }
    return error.message;
  }
  return "AI 请求失败，请稍后重试。";
}
'''

ai_types_fixed_function = '''export function translateAiError(error: unknown): string {
  if (!(error instanceof Error)) {
    return "AI 请求失败，请稍后重试。";
  }

  // 稳定类别决定处理建议；说明文本只承载诊断细节和旧版本兼容。
  const code = (error as Error & { code?: unknown }).code;
  const message = error.message;
  if (code === "ai_not_configured") {
    return "AI 服务尚未配置，请点击右上角设置填写 base_url、API Key 和模型名。";
  }
  if (code === "ai_session_busy") {
    return "上一轮回答还在生成中，请等它结束（或点击停止）后再发送。";
  }
  if (code === "ai_upstream_error") {
    const detail = message
      .replace(/^模型服务调用失败/u, "")
      .replace(/^[\\s（）:：-]+/u, "")
      .trim();
    const tail = detail.length > 160 ? `${detail.slice(0, 160)}…` : detail;
    return `模型服务调用失败，请检查网络、API Key 与服务商状态后重试。${
      tail ? `服务商返回：${tail}。` : ""
    }若是回答过长或上下文超限，请新建对话后拆小问题再问。`;
  }
  if (code === "ai_session_not_found") {
    return "没有找到这段对话，它可能已被删除。请刷新历史记录后重试。";
  }
  if (code === "ai_memory_not_found") {
    return "没有找到这条长期记忆，它可能已被删除。请刷新记忆列表后重试。";
  }
  if (code === "request_failed") {
    return "AI 请求失败，请稍后重试。";
  }

  if (message.includes("ai_not_configured") || message.includes("尚未配置")) {
    return "AI 服务尚未配置，请点击右上角设置填写 base_url、API Key 和模型名。";
  }
  if (message.includes("ai_session_busy") || message.includes("仍在生成中")) {
    return "上一轮回答还在生成中，请等它结束（或点击停止）后再发送。";
  }
  if (message.includes("ai_upstream_error") || message.includes("模型服务调用失败")) {
    const detail = message
      .replace(/^模型服务调用失败/u, "")
      .replace(/^[\\s（）:：-]+/u, "")
      .trim();
    const tail = detail.length > 160 ? `${detail.slice(0, 160)}…` : detail;
    return `模型服务调用失败，请检查网络、API Key 与服务商状态后重试。${
      tail ? `服务商返回：${tail}。` : ""
    }若是回答过长或上下文超限，请新建对话后拆小问题再问。`;
  }
  return message || "AI 请求失败，请稍后重试。";
}
'''

ai_types_injected = replace_once(
    ai_types_src,
    ai_types_original_function,
    ai_types_injected_function,
    "前端错误文案",
)
ai_types_fixed = replace_once(
    ai_types_src,
    ai_types_original_function,
    ai_types_fixed_function,
    "前端锚解文案",
)

# ---------------------------------------------------------------------------
# 二、注入补丁与参考解
# ---------------------------------------------------------------------------

inject_dir = TASK / "inject" / "patches"
inject_dir.mkdir(parents=True, exist_ok=True)
for old in inject_dir.glob("*.patch"):
    old.unlink()
write_text(inject_dir / "0001-service-error-contract.patch", patch_for(SERVICE_REL, service_src, service_injected))
write_text(inject_dir / "0002-ai-facade-terminal.patch", patch_for(FACADE_REL, facade_src, facade_injected))
write_text(inject_dir / "0003-frontend-error-copy.patch", patch_for(AI_TYPES_REL, ai_types_src, ai_types_injected))

fix_patch = "".join(
    [
        patch_for(SERVICE_REL, service_injected, service_fixed),
        patch_for(FACADE_REL, facade_injected, facade_src),
        patch_for(AI_TYPES_REL, ai_types_injected, ai_types_fixed),
    ]
)
partial_patch = "".join(
    [
        patch_for(SERVICE_REL, service_injected, service_fixed),
        patch_for(FACADE_REL, facade_injected, facade_src),
    ]
)
write_text(TASK / "reference" / "fix.patch", fix_patch)
write_text(TASK / "reference" / "partial.patch", partial_patch)

# ---------------------------------------------------------------------------
# 三、pytest 隐藏测试
# ---------------------------------------------------------------------------

hidden_test = '''"""T1-03 隐藏测试：错误分类、有限流终态与跨端契约。"""

from __future__ import annotations

import ast
import json
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


def test_frontend_translator_mirrors_every_declared_ai_error_code():
    source = _AI_TYPES.read_text(encoding="utf-8")
    marker = "export function translateAiError"
    assert marker in source
    translator = source.split(marker, 1)[1]
    codes = _declared_ai_error_codes()
    assert codes == {
        "request_failed",
        "ai_not_configured",
        "ai_upstream_error",
        "ai_session_busy",
        "ai_session_not_found",
        "ai_memory_not_found",
    }
    missing = sorted(code for code in codes if f'"{code}"' not in translator)
    assert not missing, f"页面错误翻译缺少后端已声明类别：{missing}"
'''
write_text(TASK / "hidden" / "tests_hidden" / "test_error_code_chain.py", hidden_test)

write_json(
    TASK / "hidden" / "groups.json",
    {
        "schema": 1,
        "task": "T1-03",
        "note": "pytest 侧分组：稳定分类、有限流终态和跨出口一致性。coherence 权重最高，并包含前端翻译对后端 AiError 全码集合的文本级镜像守卫。",
        "groups": [
            {
                "id": "error_code_exit",
                "weight": 1,
                "port": "服务层流式失败按异常语义保留稳定分类",
                "tests": [
                    "hidden/tests_hidden/test_error_code_chain.py::test_specific_stream_failures_keep_distinct_codes",
                    "hidden/tests_hidden/test_error_code_chain.py::test_validation_and_unknown_stream_failures_keep_their_fallbacks",
                ],
            },
            {
                "id": "stream_terminal_exit",
                "weight": 1,
                "port": "有限对话流在失败和成功时都必须给出明确终态",
                "tests": [
                    "hidden/tests_hidden/test_error_code_chain.py::test_unconfigured_chat_ends_with_one_typed_error",
                    "hidden/tests_hidden/test_error_code_chain.py::test_worker_failure_keeps_session_then_emits_error_terminal",
                    "hidden/tests_hidden/test_error_code_chain.py::test_successful_chat_still_ends_with_result",
                ],
            },
            {
                "id": "coherence",
                "weight": 2,
                "port": "同一故障跨 JSON/流式出口同码，页面镜像后端全部 AI 类别",
                "tests": [
                    "hidden/tests_hidden/test_error_code_chain.py::test_same_ai_failure_keeps_its_code_in_json_and_stream",
                    "hidden/tests_hidden/test_error_code_chain.py::test_frontend_translator_mirrors_every_declared_ai_error_code",
                ],
            },
            {
                "id": "p2p",
                "weight": 0,
                "mode": "regression",
                "note": "既有 pytest 白名单见任务根 p2p.json；任一条失败，本轮记 0 分。",
            },
        ],
    },
)

# ---------------------------------------------------------------------------
# 四、vitest 隐藏测试
# ---------------------------------------------------------------------------

hidden_fe_test = '''// T1-03 隐藏测试（页面侧）：稳定类别必须产生可区分的处理建议。
import { describe, expect, it } from "vitest";
import { translateAiError } from "../aiTypes";

function codedError(code: string, message: string): Error & { code: string } {
  return Object.assign(new Error(message), { code });
}

describe("AI 错误提示", () => {
  it("按稳定类别给出三种不同的处理建议", () => {
    const notConfigured = translateAiError(codedError("ai_not_configured", "原始未配置说明"));
    const busy = translateAiError(codedError("ai_session_busy", "原始占用说明"));
    const upstream = translateAiError(codedError("ai_upstream_error", "模型服务调用失败：429 quota"));

    expect(notConfigured).toContain("设置");
    expect(busy).toMatch(/上一轮|停止/u);
    expect(upstream).toMatch(/服务商|API Key/u);
    expect(new Set([notConfigured, busy, upstream]).size).toBe(3);
  });

  it("上游失败保留诊断细节而不是只给通用提示", () => {
    const message = translateAiError(codedError("ai_upstream_error", "模型服务调用失败：上下文超长"));
    expect(message).toContain("上下文超长");
    expect(message).toContain("重试");
  });

  it("未知类别保留原始说明并为非异常值兜底", () => {
    expect(translateAiError(codedError("future_ai_error", "新的服务端说明"))).toBe("新的服务端说明");
    expect(translateAiError({ code: "future_ai_error" })).toBe("AI 请求失败，请稍后重试。");
  });
});
'''
write_text(TASK / "hidden-fe" / "tests_hidden_fe" / "errorCopy.hidden.test.ts", hidden_fe_test)

write_json(
    TASK / "hidden-fe" / "groups_fe.json",
    {
        "schema": 1,
        "task": "T1-03",
        "note": "页面侧分组：三类稳定码必须给出三种可执行文案，未知类别仍有安全兜底。",
        "groups": [
            {
                "id": "frontend_branch_exit",
                "weight": 1,
                "port": "页面按稳定类别选择提示，而不是把已知故障压成一句通用报错",
                "tests": [
                    "tests_hidden_fe/errorCopy.hidden.test.ts::按稳定类别给出三种不同的处理建议",
                    "tests_hidden_fe/errorCopy.hidden.test.ts::上游失败保留诊断细节而不是只给通用提示",
                    "tests_hidden_fe/errorCopy.hidden.test.ts::未知类别保留原始说明并为非异常值兜底",
                ],
            },
            {
                "id": "p2p",
                "weight": 0,
                "mode": "regression",
                "note": "页面既有用例白名单见任务根 p2p-fe.json。",
            },
        ],
    },
)

# ---------------------------------------------------------------------------
# 五、p2p：pytest 基线 collect-only + vitest 标题清单
# ---------------------------------------------------------------------------

collect_env = dict(os.environ)
collect_env["PYTHONDONTWRITEBYTECODE"] = "1"
baseline_tree = packgate.GATES / "T1-03-collect"
packgate.hutil.remove_tree(str(baseline_tree))
collect_meta = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
collect_meta["repo"]["snapshot"] = "slim-py"
collect_meta["visible"] = {
    "prune": [
        "tests/test_ai_service_http.py::test_ai_chat_stream_without_config_returns_error_event",
        "tests/test_ai_service_http.py::test_ai_chat_stream_bad_session_id_isolated",
    ]
}
collect_meta["redactions"] = [
    {"file": "AGENTS.md", "sections": ["15", "18"]},
    {"file": "CHANGELOG.md", "versions": ["1.5.2", "1.6.1"]},
]
packgate.build_tree("T1-03", collect_meta, baseline_tree, [])
try:
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_ai_service_http.py",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=baseline_tree,
        env=collect_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
finally:
    packgate.hutil.remove_tree(str(baseline_tree))
assert collected.returncode == 0, collected.stdout + collected.stderr
collected_nodes = {
    line.strip()
    for line in collected.stdout.splitlines()
    if line.strip().startswith("tests/test_ai_service_http.py::")
}
non_stream_names = {
    "test_ai_status_reports_unconfigured_by_default",
    "test_ai_config_roundtrip_masks_key",
    "test_ai_config_embedding_endpoint_and_schedule_roundtrip",
    "test_ai_reports_list_and_file_roundtrip",
    "test_ai_overfit_check_endpoint",
    "test_ai_config_reveal_returns_full_key",
    "test_ai_chat_cancel_requires_session_id",
    "test_ai_config_store_keeps_embedding_base_url_when_blank",
    "test_ai_session_locks_are_bounded_and_reusable",
    "test_ai_session_lock_not_evicted_between_fetch_and_acquire",
}
pytest_p2p = sorted(
    node for node in collected_nodes if node.split("::", 1)[1] in non_stream_names
)
assert len(pytest_p2p) == len(non_stream_names), (
    "基线收集缺少非流式候选：" + str(sorted(non_stream_names - {node.split("::", 1)[1] for node in pytest_p2p}))
)
write_json(
    TASK / "p2p.json",
    {
        "schema": 1,
        "task": "T1-03",
        "note": "在未注入基线树上从 tests/test_ai_service_http.py --collect-only 收集的非 AI 流式用例；目标流式守卫不混入回归白名单。",
        "tests": pytest_p2p,
    },
)

frontend_p2p = [
    "src/api.stream.test.ts::forwards blocked trade events to the trade handler",
    "src/api.stream.test.ts::emits renderable partial snapshots before the final realtime result",
    "src/api.stream.test.ts::aborts a stalled realtime stream after its idle timeout",
    "src/api.stream.test.ts::aborts a stalled backtest stream after its idle timeout",
    "src/api.stream.test.ts::cancels an active realtime stream from the caller signal",
    "src/api.stream.test.ts::cancels an active backtest stream from the caller signal",
    "src/api.stream.test.ts::keeps the backend business code from a non-2xx stream response",
    "src/api.stream.test.ts::falls back to http_error when the error body is not JSON",
    "src/api.stream.test.ts::reports an unfinished backtest stream instead of resolving with nothing",
    "src/api.stream.test.ts::cancels the response body when an NDJSON event cannot be parsed",
    "src/api.stream.test.ts::reports the stream idle timeout when a non-success response body stalls",
    "src/aiApi.chat.test.ts::reports interruption after tokens when no result event arrives",
    "src/aiApi.chat.test.ts::resolves once the result event has been unwrapped",
    "src/aiApi.chat.test.ts::keeps the backend stable code carried by an error event",
    "src/aiApi.chat.test.ts::passes a caller abort through as a cancellation, not a success",
]
write_json(
    TASK / "p2p-fe.json",
    {
        "schema": 1,
        "task": "T1-03",
        "note": "vitest 不支持 pytest --collect-only；按基线源码手写 api.stream.test.ts 的 11 条与 aiApi.chat.test.ts 的 4 条标题，并由 packgate 实跑确认。",
        "tests": frontend_p2p,
    },
)

# ---------------------------------------------------------------------------
# 六、提示词、meta、校准与参考说明
# ---------------------------------------------------------------------------

wiring = '''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。
'''

prompt1 = wiring + '''
## 我遇到的问题

桌面端最近把好几种本来能分别处理的失败都显示成了“AI 请求失败，请稍后重试”。没配好服务、上游调用失败、上一轮还没结束时，用户看到的都是同一句话，既不知道该去设置、等待，还是稍后重试。

另一个现象更难判断：有些回答已经在后台失败，页面却只看到连接结束，没有收到明确的失败收尾。已经生成的片段停在那里，界面最后只能把它当作普通中断。

## 验收要求

- 可恢复的失败要保持各自稳定的类别，不能在传递途中被合并成通用失败；
- 有明确结束语义的流，无论成功还是失败，都必须给出一个且仅一个终态；
- 页面要按类别给出不同的中文处理建议，未知类别仍应保留有用的原始说明；
- 同一种故障走普通请求或流式请求时，分类必须一致。

请修根因，不要改测试，也不要用文案关键字猜故障类别。
'''

prompt2 = wiring + '''
## 已确认的不一致

同一套失败信息经过系统时，被三个彼此独立的环节重复解释，现在口径已经分叉：

1. 最先接住异常的一层原本能区分“本地数据不存在”“请求字段缺失”“参数不合法”和无法识别的失败；现在较具体的两类被较宽的判断吞掉，调用方只能收到更笼统的类别。
2. 有限的对话流有两条失败路径：一种在开始迭代时就失败，另一种在后台工作线程启动后失败。前者由传输层补收尾，后者由事件生产层在哨兵之后补收尾；现在两条路都只关闭连接，不再交付失败终态。正常成功流仍会返回结果，所以问题只在异常分支出现。
3. 页面拿到稳定类别后，本应给未配置、上一轮占用、上游服务异常分别提供设置、等待/停止、检查服务商等不同建议；现在已识别类别被统一压成同一句话，而未识别异常反而还会直出原始说明。

还要注意两处看似相关但实际正确的边界：未知异常退成通用类别是必要兜底；普通请求的异常捕获必须让更具体的冲突与业务异常排在宽泛异常之前。不要为了让某个断言变绿而破坏这两处。

修复后请同时核对：具体分类的先后关系、有限流的失败收尾、页面类别与文案的对应，以及普通请求和流式请求对同一故障是否同码。
'''

prompt3 = wiring + '''
## 必须同时成立的不变量

1. **分类只能在语义最完整的位置决定一次。** 更具体的异常必须先于它的父类判断；缺数据、缺字段、参数非法与未知失败不能互相冒充。未知异常可以进入通用兜底，但已知异常不得提前丢失类别。
2. **有限流必须显式终结。** 成功以结果终态收尾，失败以携带稳定类别和说明的失败终态收尾；开始迭代前失败与后台线程失败都要经过同一收尾机制。连接自然关闭不能替代终态，失败也不能伪装成成功结果。
3. **页面只把稳定类别当分支依据。** 未配置要引导完成设置，上一轮占用要提示等待或停止，上游失败要保留诊断并给重试建议；这些文案必须彼此可区分。说明文本匹配只能兼容旧数据，不能成为主要分类方法。
4. **两种传输形态保持一致。** 同一个上游故障经普通请求和有限流返回时，机器可读类别必须相同；页面能够识别后端声明的每一种助手故障类别，新增类别不能悄悄落入错误的旧分支。
5. **常驻通知与有限任务分开看。** 常驻通知通道本来就依赖重连，没有“最终结果”；不要把有限任务的终态规则机械套到它上面。

## 已被否决的做法

- 不要在页面根据中文句子包含什么字来重新猜类别。文案会变化，同一故障在不同出口也可能有不同说明，这种修法会再次漂移。
- 不要在连接关闭时无条件补一个“成功”结果。那会吞掉真实失败，让调用方把残缺内容当完整回答。
- 不要改通用兜底来迁就已知类别，也不要交换普通请求最外层的异常捕获顺序；前者会让未知异常冒充业务错误，后者会把本应保留的冲突状态降成普通失败。

验收时既要覆盖每个异常出口，也要保留一个正常成功场景，防止用“一律报错”或“一律补终态”蒙混通过。
'''

assert len(prompt1) < len(prompt2) < len(prompt3), (len(prompt1), len(prompt2), len(prompt3))
for prompt in (prompt1, prompt2, prompt3):
    for forbidden in (
        "service.py",
        "facade.py",
        "aiTypes.ts",
        "App.tsx",
        "_stream_error_code",
        "translateAiError",
        "ai_not_configured",
        "stream_incomplete",
    ):
        assert forbidden not in prompt, f"提示词泄漏实现名：{forbidden}"

write_text(TASK / "prompts" / "1.md", prompt1)
write_text(TASK / "prompts" / "2.md", prompt2)
write_text(TASK / "prompts" / "3.md", prompt3)

meta = {
    "schema": 1,
    "id": "T1-03",
    "tier": "primary",
    "attempts": 1,
    "title": "同一种失败，三段链路各说各话",
    "repo": {
        "id": "core",
        "snapshot": "slim-py+fe",
        "commit": "6192aa25c2791be655dd11783c77683b9cb2aa7b",
    },
    "allowed_paths": [
        SERVICE_REL,
        FACADE_REL,
        AI_ERRORS_REL,
        "frontend/src/api.ts",
        AI_TYPES_REL,
        "frontend/src/App.tsx",
    ],
    "forbidden_paths": [
        "tests/**",
        "pyproject.toml",
        "frontend/vitest.config.ts",
        "**/conftest.py",
        "packs/**",
        "console/**",
    ],
    "visible": {
        "prune": [
            "tests/test_ai_service_http.py::test_ai_chat_stream_without_config_returns_error_event",
            "tests/test_ai_service_http.py::test_ai_chat_stream_bad_session_id_isolated",
        ]
    },
    "redactions": [
        {"file": "AGENTS.md", "sections": ["15", "18"]},
        {"file": "CHANGELOG.md", "versions": ["1.5.2", "1.6.1"]},
    ],
    "checks": [
        {
            "kind": "pytest",
            "hidden": "hidden/tests_hidden",
            "groups": "hidden/groups.json",
            "p2p": "p2p.json",
        },
        {
            "kind": "vitest",
            "hidden": "hidden-fe/tests_hidden_fe",
            "groups": "hidden-fe/groups_fe.json",
            "p2p": "p2p-fe.json",
        },
    ],
    "budget": {"grade_timeout_s": 240, "diff_line_cap": 4000},
    "calibration": {"target_band": [0.6, 0.85], "calibrated": False},
}
write_json(TASK / "meta.json", meta)

write_json(
    TASK / "calibration" / "results.json",
    {
        "schema": 1,
        "task": "T1-03",
        "calibrated": False,
        "target_band": [0.6, 0.85],
        "owner": "author",
        "policy": "§6.4 硬纪律：出题模型不得给自己出的题做校准。本表在盲测完成前保持空表，calibrated 恒为 false。",
        "gate": {
            "note": "出题侧门禁由 runs/blind/tools/packgate.py 执行；原始结果见同目录 gate_*.json，不计入 pass@1。",
            "anchor_solution": "gate_fixed.json",
            "partial_solution": "gate_partial.json",
            "injected_state": "gate_injected_x20.json",
        },
        "blind_runs": {
            "note": "每一行代表一次只给第 1 级提示词的完整作答，由非出题模型填写。",
            "columns": [
                "run_id",
                "model",
                "tier",
                "prompt_level",
                "pass@1",
                "score",
                "failed_groups",
                "p2p_broken",
                "notes",
            ],
            "rows": [],
        },
        "summary": {
            "runs": 0,
            "pass_at_1": None,
            "confidence_interval": None,
            "in_band": None,
            "conclusion": None,
        },
    },
)

notes = f'''# T1-03 参考解说明（成题版）

> 本文件只进 `reference/`，不进入沙箱快照白名单。
> 门禁结果由 `calibration/gate_*.json` 留档；盲测校准仍按 §6.4 由非出题模型执行。

## 一、注入端口（3 类，落盘 3 文件）

| # | 位置（原始快照行号） | 注入内容 | 可见症状 |
| --- | --- | --- | --- |
| 1 | `backend/astock_backtester/service.py:91-99` | 服务层流式分类只剩“参数非法/请求失败”两档；`LocalDataUnavailable` 被父类判断吞成校验失败，`KeyError` 被并入通用失败 | 调用方失去“无本地数据/缺字段”的稳定类别 |
| 2a | `backend/astock_backtester/service.py:629-640` | chat 在开始迭代时抛错后只关闭生成器，不再经共享助手写 error 终态 | 未配置等早期失败以空流结束 |
| 2b | `backend/astock_backtester/ai/facade.py:393-436` | worker 仍保存异常与投递哨兵，但消费端越过哨兵后不再 yield error 帧 | 上游失败先出现 session，随后无失败终态地断流 |
| 3 | `frontend/src/aiTypes.ts:307-333` | 三种已识别类别统一显示“AI 请求失败，请稍后重试”，未知错误的 message 兜底仍保留 | 用户无法判断应去设置、等待/停止还是检查上游 |

三处注入均不改签名，不影响正常成功路径。注入注释改成了“分类收成两档/连接关闭统一识别/哨兵后不追加”的自然维护口径，与原守卫注释不复刻。

## 二、锚解形态

1. 恢复具体异常优先于宽泛父类的服务层映射，四个基础类别各守其责。
2. 恢复 `_write_ai_error_event` 这一共享终态出口，让 `AiError` 与未知异常两个 catch 分支都调用它；worker 内失败则由 facade 在哨兵后送出保存的 error 帧。两类失败路径因此都以明确 error 终态收尾，成功路径仍只有 result。
3. 页面翻译以稳定类别为主分支，三个高频故障分别给设置、等待/停止、检查服务商的建议；并显式镜像后端 `AiError` 声明的全部类别。说明文本只做旧版本兼容与未知类别兜底。

`partial.patch` 只完成第 1、2 点（后端两个端口），故后端分类与终态组转绿，但页面组和包含全码镜像的 coherence 组保持红。

## 三、隐藏分组

| 组 | 权重 | 第二场景与防特判设计 |
| --- | ---: | --- |
| `error_code_exit` | 1 | 缺数据+缺字段；另以参数非法+未知异常作对照 |
| `stream_terminal_exit` | 1 | 未配置的迭代前失败、stub agent 的 worker 失败；另守正常 result |
| `frontend_branch_exit` | 1 | 三类码三种文案；另测上游 detail、未知码与非 Error 兜底 |
| `coherence` | 2 | 同一上游故障走 JSON/流式同码；文本/AST 扫描前端翻译镜像后端全部 `AiError` 码 |
| `p2p` | 0 | pytest 与 vitest 既有行为白名单，任一回归即整轮 0 分 |

## 四、陷阱与诱饵

- **半成品陷阱**：只修服务层/worker 终态，页面仍把已知类别压成通用文案；预计 40/100。
- **字符串猜码陷阱**：只按 message 片段分类，JSON 与流式的说明稍有不同就漂移，且全码镜像守卫仍红。
- **伪终态陷阱**：对任意 EOF 补成功 result 会吞掉真实失败；正常/失败终态守卫会区分。
- **正确诱饵 1**：`backend/astock_backtester/ai/errors.py:52-55` 对非 `AiError` 返回通用码是未知异常的正确兜底，不应改坏。
- **正确诱饵 2**：`service.py:1256-1281` 的普通请求捕获顺序保证准入冲突先于宽泛异常；调整会破坏 HTTP 409 语义。
- **正确诱饵 3**：`frontend/src/api.ts` 的非 2xx 解析和 `App.tsx` 的回测文案分支都不是本题退化点。

## 五、原生场景取舍

`service.py:745-755` 的常驻 AI 通知流在异常时只关闭生成器、不发送有限任务终态，这是原生行为，保持原样且隐藏测试不作断言。该通道由调用方重连，没有“最终结果”；把 chat 的有限流终态契约套过去会扩大题目范围并引入错误修复。

## 六、裁剪、p2p 与脱敏

- `visible.prune` 共 2 条：未配置 chat 流的具体 error 事件守卫，以及非法 session id 下仍返回同一错误码的守卫。两条都直接点名注入后缺失的终态/码；其余正常 chat、取消、锁与心跳用例保留可见。
- pytest p2p 共 {len(pytest_p2p)} 条：在基线对 `tests/test_ai_service_http.py --collect-only` 后选出的非 AI 流式用例。
- vitest p2p 共 {len(frontend_p2p)} 条：`api.stream.test.ts` 11 条 + `aiApi.chat.test.ts` 4 条。vitest 没有 pytest 的 collect-only 接口，故按标题手写，再由 packgate 实跑确认。
- redactions 使用 T2-04 同款结构，移除会直接给出错误码/终态答案的 AGENTS 与 CHANGELOG 章节；当前 slim 快照若未复制这些文档，规则仍作为未来白名单扩展的防泄漏声明。

## 七、§6.5 反过易检查清单

- [x] grep/读沙箱文档不能直接得到三处改法；答案型架构/更新记录已声明脱敏。
- [x] 至少一个看似可疑但实际正确的诱饵：AI 通用兜底、普通请求捕获顺序、前端传输兜底。
- [x] 每个计分组都有第二数据场景，单值硬编码或一刀切会失败。
- [x] 症状与三级提示词不含文件名、函数名、常量名或错误码字面量。
- [x] 只修后端端口的 `partial.patch` 必然低于 100；页面组与 coherence 独立保持红。
- [x] 出题者自评无法在 10 分钟内一次做对：需跨 Python/TypeScript 追踪分类、两类流式异常时机与页面消费，并避开三个正确诱饵。

## 八、门禁结果（packgate，2026-09-30）

| 状态 | 实测结果 |
| --- | --- |
| `fixed` | **100.0**；4 个计分组全绿；p2p {len(pytest_p2p) + len(frontend_p2p)}/{len(pytest_p2p) + len(frontend_p2p)}，0 失败 |
| `partial` | **40.0**；后端分类与终态两组绿，页面与 coherence 两组红；p2p 0 失败 |
| `injected ×20` | **20/20 均为 0.0**；每轮 4 个计分组全红；score_min=score_max=0.0，stable=true；p2p 0 失败 |

三个门禁原始 JSON 位于 `calibration/`。结果符合预设，无分数组成偏差。

## 九、校准状态

`calibration/results.json` 仍为 `calibrated=false`，目标带 `[0.6, 0.85]`，盲测表保持空白；作者不进行 pass@1 校准。
'''
write_text(TASK / "reference" / "notes.md", notes)

print(f"T1-03 成题材料已生成：pytest p2p {len(pytest_p2p)} 条，vitest p2p {len(frontend_p2p)} 条")
