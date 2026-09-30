"""服务端模型代理与受限工作区工具。

浏览器只把用户消息交给本地服务。服务端从模型档案解析 OpenAI 兼容 endpoint，
把沙箱绝对路径放入 system 消息，并在 Chat Completions 的 tool call 闭环中执行
受限文件/命令工具。API key 只从服务端进程环境读取，永不回传给浏览器或落盘。
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import shlex
import threading
import uuid
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional
from urllib import error as url_error
from urllib import request as url_request

from . import config, errors, keyring, util

MAX_HISTORY = 100
MAX_FILE_CHARS = 200_000
MAX_COMMAND_OUTPUT = 24_000
MAX_COMMAND_SECONDS = 120
_CHAT_LOCKS: Dict[str, threading.RLock] = {}
_CHAT_LOCKS_GUARD = threading.Lock()
#: 正在执行 send 的运行（浏览器关掉/刷新后服务端线程还在跑，前端靠这个感知）
_ACTIVE_SENDS: set = set()
_ACTIVE_SENDS_GUARD = threading.Lock()
_CHAT_ENV_KEYS = {
    "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
}
ALLOWED_COMMANDS = {
    "git", "git.exe",
    "node", "node.exe",
    "npm", "npm.cmd", "npx", "npx.cmd",
    "py", "py.exe", "python", "python.exe", "pytest", "pytest.exe",
}
BLOCKED_COMMAND_FLAGS = {"-c", "--command", "-e", "--eval", "--exec"}


def _chat_path(run: dict) -> str:
    run_dir = str(run.get("run_dir") or "")
    return os.path.join(run_dir, "chat.jsonl") if run_dir else ""


def _refresh_run(run: dict) -> dict:
    """在会话锁内读取最新运行状态，避免取消/评分使用过期快照。"""
    run_dir = str(run.get("run_dir") or "")
    if not run_dir:
        return run
    current = util.read_json(os.path.join(run_dir, "run.json"), default=None)
    if isinstance(current, dict) and current.get("run_id") == run.get("run_id"):
        current["run_dir"] = run_dir
        return current
    return run


def lock_for(run_id: str) -> threading.RLock:
    """返回某次运行的会话锁，串行化聊天、评分启动与记录写入。"""
    key = str(run_id or "")
    with _CHAT_LOCKS_GUARD:
        lock = _CHAT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _CHAT_LOCKS[key] = lock
        return lock


@contextmanager
def exclusive(run_id: str, *, blocking: bool = True) -> Iterator[bool]:
    """以非阻塞方式预留一次运行的独占操作。"""
    lock = lock_for(run_id)
    acquired = lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def is_supported_model(model: dict) -> bool:
    """当前内置工具闭环只接受 OpenAI-compatible Chat Completions。"""
    try:
        mode, _url = _endpoint(model)
    except errors.HarnessError:
        return False
    return mode == "chat_completions"


def _read_messages(run: dict) -> List[dict]:
    path = _chat_path(run)
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            out = []
            for line in fh:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict) and value.get("role") in {"user", "assistant", "tool"}:
                    out.append(value)
            # 按完整的 user -> assistant/tool 轮次裁剪，不能从 tool 响应中间截断。
            groups = []
            current = []
            for item in out:
                if item.get("role") == "user" and current:
                    groups.append(current)
                    current = []
                current.append(item)
            if current:
                groups.append(current)
            selected = []
            count = 0
            for group in reversed(groups):
                if selected and count + len(group) > MAX_HISTORY:
                    break
                selected[0:0] = [group]
                count += len(group)
            return [item for group in selected for item in group]
    except OSError:
        return []


def _append_message(run: dict, message: dict) -> dict:
    path = _chat_path(run)
    if not path:
        raise errors.HarnessError(errors.E_STORE_FAILED, "运行记录目录不可写，无法保存对话。")
    item = dict(message)
    item.setdefault("id", "msg-%s" % uuid.uuid4().hex[:16])
    item.setdefault("created_at", util.iso_now())
    util.ensure_dir(os.path.dirname(path))
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise errors.HarnessError(errors.E_STORE_FAILED, "对话记录保存失败，请检查运行目录权限。", str(exc))
    return item


def messages(run: dict) -> List[dict]:
    """返回不含内部认证信息的消息记录。"""
    return _read_messages(run)


def _model_key(model: dict) -> str:
    """按本机密钥文件（页面粘贴）→ 显式 key_env → 模型专属环境变量 → OPENAI_API_KEY 读取密钥。"""
    stored = keyring.get_key(str(model.get("id") or ""))
    if stored:
        return stored
    configured = str(model.get("key_env") or model.get("api_key_env") or "").strip()
    candidates = []
    if configured and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", configured):
        candidates.append(configured)
    safe_id = re.sub(r"[^A-Za-z0-9]+", "_", str(model.get("id") or "MODEL")).strip("_").upper()
    if safe_id:
        candidates.append("MODEL_%s_API_KEY" % safe_id)
    if str(model.get("protocol") or "").lower() == "openai":
        candidates.append("OPENAI_API_KEY")
    for name in candidates:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise errors.HarnessError(
        errors.E_MODEL_INVALID,
        "模型档案还没有可用密钥。到「模型档案」页编辑该档案并粘贴 API 密钥，或在服务端环境变量中设置后重试。",
        "可用变量：%s" % "、".join(candidates),
    )


def _base_url(model: dict) -> str:
    base = str(model.get("base_url") or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com/v1"
    if not re.match(r"^https?://", base, re.I):
        raise errors.HarnessError(errors.E_MODEL_INVALID, "模型档案的 base_url 必须是 http 或 https 地址。")
    return base


def _endpoint(model: dict) -> tuple:
    protocol = str(model.get("protocol") or "").lower()
    if protocol != "openai":
        raise errors.HarnessError(
            errors.E_CHAT_UNSUPPORTED,
            "当前内置聊天代理只支持 OpenAI-compatible 模型档案，其他协议暂未接入。",
            "protocol=%s" % protocol,
        )
    mode = str(model.get("api_mode") or config.DEFAULT_OPENAI_API_MODE).lower()
    mode = {"chat/completions": "chat_completions", "completion": "completions"}.get(mode, mode)
    if mode not in config.OPENAI_API_MODES:
        raise errors.HarnessError(errors.E_CHAT_UNSUPPORTED, "模型档案的 api_mode 不受支持。", mode)
    if mode != "chat_completions":
        raise errors.HarnessError(
            errors.E_CHAT_UNSUPPORTED,
            "当前工作区对话需要 Chat Completions，以便让模型通过受限工具操作沙箱。",
            "api_mode=%s" % mode,
        )
    suffix = {"responses": "/responses", "chat_completions": "/chat/completions", "completions": "/completions"}[mode]
    return mode, _base_url(model) + suffix


def _post_json(url: str, payload: dict, key: str, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = url_request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % key,
        "Accept": "application/json",
    })
    try:
        with url_request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except url_error.HTTPError as exc:
        try:
            detail = util.decode_output(exc.read(4096))
        except OSError:
            detail = ""
        detail = re.sub(r"(?i)(authorization|api[-_ ]?key)\s*[:=]\s*[^,\s}]+", r"\1: [redacted]", detail)
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回了 HTTP %s，请检查模型档案和服务端密钥。" % exc.code, detail)
    except (url_error.URLError, TimeoutError, OSError) as exc:
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口连接失败，请检查 base_url、网络和服务端密钥。", str(exc))
    try:
        value = json.loads(util.decode_output(raw))
    except (TypeError, ValueError) as exc:
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回的不是合法 JSON。", str(exc))
    if not isinstance(value, dict):
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回格式不受支持。")
    return value


def _workspace_root(run: dict) -> str:
    raw = str(run.get("sandbox") or "").strip()
    if not raw:
        raise errors.HarnessError(errors.E_SANDBOX_MISSING, "沙箱目录不存在，请先准备或重建沙箱。")
    path = util.norm(raw)
    if not os.path.isdir(path):
        raise errors.HarnessError(errors.E_SANDBOX_MISSING, "沙箱目录不存在，请先准备或重建沙箱。")
    return path


def _safe_path(root: str, relative: str, *, allow_root: bool = True) -> str:
    relative = str(relative or "").replace("\\", "/")
    if not relative and allow_root:
        return root
    if os.path.isabs(relative):
        candidate = util.norm(relative)
    else:
        candidate = util.norm(os.path.join(root, relative))
    if not util.path_within(root, candidate):
        raise ValueError("路径必须位于当前沙箱内")
    # Existing junction/symlink targets must also remain inside the workspace.
    real_root = os.path.realpath(root)
    probe = candidate if os.path.exists(candidate) else os.path.dirname(candidate)
    if not util.path_within(real_root, os.path.realpath(probe)):
        raise ValueError("路径解析后越过沙箱边界")
    if util.rel_posix(candidate, root).split("/")[0] == ".git":
        raise ValueError("工具不允许直接修改 .git")
    return candidate


def _tool_list_files(root: str, args: dict) -> dict:
    path = _safe_path(root, args.get("path", ""))
    if not os.path.isdir(path):
        raise ValueError("目录不存在")
    recursive = bool(args.get("recursive", True))
    result = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames[:] = [n for n in dirnames if n != ".git" and not util.is_junction(os.path.join(dirpath, n))]
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                result.append(util.rel_posix(full, root))
                if len(result) >= 500:
                    break
            if len(result) >= 500:
                break
    else:
        for name in sorted(os.listdir(path))[:500]:
            full = os.path.join(path, name)
            result.append(util.rel_posix(full, root) + ("/" if os.path.isdir(full) else ""))
    return {"path": util.rel_posix(path, root) or ".", "entries": result, "truncated": len(result) >= 500}


def _tool_read_file(root: str, args: dict) -> dict:
    path = _safe_path(root, args.get("path", ""), allow_root=False)
    if not os.path.isfile(path):
        raise ValueError("文件不存在")
    max_chars = min(MAX_FILE_CHARS, max(1, int(args.get("max_chars") or MAX_FILE_CHARS)))
    with open(path, "rb") as fh:
        text = util.decode_output(fh.read(max_chars + 1))
    return {"path": util.rel_posix(path, root), "content": text[:max_chars], "truncated": len(text) > max_chars}


def _tool_write_file(root: str, args: dict) -> dict:
    path = _safe_path(root, args.get("path", ""), allow_root=False)
    content = str(args.get("content") or "")
    if len(content) > 2 * MAX_FILE_CHARS:
        raise ValueError("单次写入内容超过 400000 字符上限")
    util.ensure_dir(os.path.dirname(path))
    util.write_text_atomic(path, content)
    return {"path": util.rel_posix(path, root), "bytes": len(content.encode("utf-8"))}


def _tool_run_command(root: str, args: dict) -> dict:
    command = args.get("command")
    if isinstance(command, list):
        argv = [str(item) for item in command]
    else:
        text = str(command or "").strip()
        if not text:
            raise ValueError("缺少 command")
        if any(mark in text for mark in ("&", "|", ";", ">", "<", "`", "$(", "\n", "\r")):
            raise ValueError("命令只支持不含 shell 重定向或串联的 argv 形式")
        argv = shlex.split(text, posix=False)
    if not argv or any("\x00" in item for item in argv):
        raise ValueError("command 为空或包含非法字符")
    executable = ntpath.basename(argv[0]).lower()
    if executable not in ALLOWED_COMMANDS:
        raise ValueError("只允许运行 git、python/pytest、node/npm 相关检查命令")
    lowered = [item.lower() for item in argv[1:]]
    if any(flag in BLOCKED_COMMAND_FLAGS for flag in lowered):
        raise ValueError("不允许执行内联脚本；请把修改写入沙箱文件后再运行检查")
    if executable.startswith("git"):
        operation = next((item for item in lowered if not item.startswith("-")), "")
        if operation in {"clone", "init", "remote", "config", "worktree", "submodule"}:
            raise ValueError("不允许改变仓库来源或 git 配置")
    if executable in {"npm", "npm.cmd", "npx", "npx.cmd"}:
        operation = next((item for item in lowered if not item.startswith("-")), "")
        if operation in {"install", "i", "ci", "update", "uninstall", "link", "root"}:
            raise ValueError("不允许通过 npm 修改依赖或访问工作区之外的目录")
    for item in argv[1:]:
        normalized = str(item).replace("\\", "/")
        if (ntpath.isabs(str(item)) or normalized.startswith(("//", "~/"))
                or re.search(r"(^|/)\.\.(/|$)", normalized)
                or re.match(r"^[A-Za-z]:", str(item))):
            raise ValueError("命令参数不能使用沙箱之外的绝对路径或 ..")
    for index, item in enumerate(lowered[:-1]):
        if item in {"-c", "--cwd", "--prefix", "-c=", "--directory"}:
            raise ValueError("命令不能切换到沙箱之外的工作目录")
    timeout = min(MAX_COMMAND_SECONDS, max(1, int(args.get("timeout_s") or 60)))
    env = {key: value for key, value in os.environ.items() if key in _CHAT_ENV_KEYS}
    env["PYTHONUNBUFFERED"] = "1"
    result = util.run_cmd(
        argv, cwd=root, env=env, timeout=timeout,
        max_output_bytes=MAX_COMMAND_OUTPUT * 2,
    )
    return {
        "exit_code": None if result.timed_out else result.returncode,
        "timed_out": bool(result.timed_out),
        "cancelled": bool(result.cancelled),
        "stdout": (result.stdout or "")[-MAX_COMMAND_OUTPUT:],
        "stderr": (result.stderr or "")[-MAX_COMMAND_OUTPUT:],
        "output_limited": bool(getattr(result, "output_limited", False)),
    }


TOOL_HANDLERS: Dict[str, Callable[[str, dict], dict]] = {
    "list_files": _tool_list_files,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "run_command": _tool_run_command,
}

TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "列出沙箱内文件。",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取沙箱内文本文件。",
     "parameters": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "write_file", "description": "写入沙箱内文件。",
     "parameters": {"type": "object", "required": ["path", "content"], "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run_command", "description": "在沙箱根目录运行一个受限命令。",
     "parameters": {"type": "object", "required": ["command"], "properties": {"command": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}, "timeout_s": {"type": "integer"}}}}},
]


def _system_prompt(run: dict, tool_enabled: bool = True) -> str:
    root = util.norm(str(run.get("sandbox") or ""))
    prompt = (
        "你正在一个代码评测 harness 中工作。当前唯一允许读写和运行命令的工作区是：%s。"
        "所有相对路径都相对于该目录；不要访问工作区之外的文件、环境变量、密钥或网络，"
        "不要执行 git commit。请先检查现状，再用工具完成用户要求，最后简要说明改动。" % root
    )
    if tool_enabled:
        prompt += " 可用工具只能操作该工作区：list_files、read_file、write_file、run_command。"
    return prompt


def _extract_chat_message(response: dict) -> dict:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口没有返回可用的 assistant 消息。")
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {}


def _responses_text(response: dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    outputs = response.get("output") or []
    parts = []
    for item in outputs if isinstance(outputs, list) else []:
        for content in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(parts)


def _history_for_api(history: List[dict]) -> List[dict]:
    out = []
    for item in history:
        if item.get("status") == "error":
            continue
        role = item.get("role")
        if role == "tool":
            if not out or out[-1].get("role") not in {"assistant", "tool"}:
                continue
            out.append({k: item[k] for k in ("role", "content", "tool_call_id") if k in item})
        elif role in {"user", "assistant"}:
            fields = ("role", "content", "tool_calls")
            if role == "assistant":
                fields += ("reasoning_content", "reasoning")
            out.append({k: item[k] for k in fields if k in item})
    return out


def send_active(run_id: str) -> bool:
    """该运行的模型发送线程是否仍在服务端执行；run_view / 对话记录用它告知前端。"""
    return str(run_id or "") in _ACTIVE_SENDS


def send(cfg: dict, run: dict, text: str) -> dict:
    """发送一条用户消息并返回最终 assistant 消息及最新历史。

    不限制工具轮数：模型自己停止调用工具才算本轮结束。每轮模型请求
    仍受 ``timeouts.chat_s`` 网络超时约束，工具执行受沙箱各项上限约束。
    发送期间即使浏览器断开，服务端线程也会继续跑完；期间
    ``send_active()`` 为真，前端据此显示「模型仍在处理」并阻止并发校验。
    """
    text = str(text or "").strip()
    if not text:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "消息不能为空。")
    run_id = str(run.get("run_id") or "")
    with _ACTIVE_SENDS_GUARD:
        _ACTIVE_SENDS.add(run_id)
    try:
        return _send_locked(cfg, run, text, run_id)
    finally:
        with _ACTIVE_SENDS_GUARD:
            _ACTIVE_SENDS.discard(run_id)


def _send_locked(cfg: dict, run: dict, text: str, run_id: str) -> dict:
    with lock_for(run_id):
        run = _refresh_run(run)
        if run.get("status") != "ready":
            if run.get("status") == "cancelled" or run.get("cancel_requested"):
                raise errors.HarnessError(errors.E_RUN_CANCELLED, "这一轮已被取消，不能继续对话。", run_id)
            raise errors.HarnessError(
                errors.E_RUN_BUSY,
                "只有沙箱就绪时才能对话；请等待当前操作结束后再试。",
                "status=%s" % run.get("status"),
            )
        root = _workspace_root(run)
        model = config.find_model(cfg, str(run.get("model") or ""))
        mode, url = _endpoint(model)
        key = _model_key(model)
        _append_message(run, {"role": "user", "content": text})
        history = _read_messages(run)
        timeout = float((cfg.get("timeouts") or {}).get("chat_s", 180) or 180)

        try:
            if mode == "chat_completions":
                api_messages = [{"role": "system", "content": _system_prompt(run, True)}] + _history_for_api(history)
                # 不限工具轮数：模型不再发起工具调用时自然收束；单轮请求有超时兜底
                while True:
                    response = _post_json(url, {"model": model.get("model") or model.get("id"), "messages": api_messages,
                                                "tools": TOOLS, "tool_choice": "auto"}, key, timeout)
                    assistant = _extract_chat_message(response)
                    tool_calls = assistant.get("tool_calls") or []
                    # Preserve the provider's text and field names for display and
                    # tool continuation. Do not stringify opaque/structured data.
                    reasoning = {field: assistant[field]
                                 for field in ("reasoning_content", "reasoning")
                                 if isinstance(assistant.get(field), str)}
                    if not tool_calls:
                        content = assistant.get("content") or ""
                        saved_data = {"role": "assistant", "content": str(content)}
                        saved_data.update(reasoning)
                        saved = _append_message(run, saved_data)
                        return {"message": saved, "messages": _read_messages(run), "model": {"id": model.get("id"), "model": model.get("model"), "api_mode": mode}}
                    assistant_saved = {
                        "role": "assistant",
                        "content": assistant.get("content"),
                        "tool_calls": tool_calls,
                    }
                    assistant_saved.update(reasoning)
                    _append_message(run, assistant_saved)
                    api_messages.append(dict(assistant_saved))
                    for call in tool_calls:
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        try:
                            arguments = json.loads(function.get("arguments") or "{}")
                            if not isinstance(arguments, dict):
                                raise ValueError("工具参数必须是对象")
                            handler = TOOL_HANDLERS.get(name)
                            if not handler:
                                raise ValueError("未知工具")
                            result = handler(root, arguments)
                        except (ValueError, TypeError, OSError) as exc:
                            result = {"error": str(exc)}
                        tool_id = str(call.get("id") or "tool-%s" % uuid.uuid4().hex[:8])
                        serialized = json.dumps(result, ensure_ascii=False)
                        api_messages.append({"role": "tool", "tool_call_id": tool_id, "content": serialized})
                        _append_message(run, {"role": "tool", "tool_call_id": tool_id, "name": name, "content": serialized})

            if mode == "responses":
                response = _post_json(url, {"model": model.get("model") or model.get("id"),
                                            "instructions": _system_prompt(run, False),
                                            "input": [{"role": "user", "content": text}]}, key, timeout)
                content = _responses_text(response)
            else:
                prompt = _system_prompt(run, False) + "\n\n" + "\n\n".join(
                    "%s: %s" % (item.get("role"), item.get("content", "")) for item in history)
                response = _post_json(url, {"model": model.get("model") or model.get("id"), "prompt": prompt,
                                            "max_tokens": 4096}, key, timeout)
                choices = response.get("choices") or []
                content = str(choices[0].get("text") or "") if choices and isinstance(choices[0], dict) else ""
            if not content:
                raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回了空消息。")
            saved = _append_message(run, {"role": "assistant", "content": content})
            return {"message": saved, "messages": _read_messages(run), "model": {"id": model.get("id"), "model": model.get("model"), "api_mode": mode}}
        except errors.HarnessError as exc:
            # 将失败放入可见记录，但不把它重新送回模型上下文。
            try:
                _append_message(run, {"role": "assistant", "content": exc.message,
                                      "status": "error", "error_code": exc.code})
            except errors.HarnessError:
                pass
            raise

