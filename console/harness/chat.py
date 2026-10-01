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

from . import config, errors, util

MAX_HISTORY = 100
MAX_TOOL_ROUNDS = 8
MAX_FILE_CHARS = 200_000
MAX_COMMAND_OUTPUT = 24_000
MAX_COMMAND_SECONDS = 120
#: doctor 的连通性档位上限（秒）：服务商的 /models 通常百毫秒返回，
#: 8 秒还连不上就是域名/网络问题，不能让浏览器一直转圈。
DOCTOR_TIMEOUT_S = 8.0
#: doctor 单次读取的响应体上限；诊断不需要完整模型清单
DOCTOR_MAX_BYTES = 512 * 1024
#: doctor 档位 id（顺序即检查顺序，前端按这个顺序渲染）
DOCTOR_STAGES = ("key", "base_url", "reach", "model")
#: 诊断详情/提示的字符上限，避免把整份响应体搬进 API 与日志
DOCTOR_DETAIL_CHARS = 180
#: 服务商未公开模型列表时的统一提示（不是失败，只是无法核对模型名）
DOCTOR_NO_LIST_HINT = (
    "这个 endpoint 没有公开模型列表，无法核对模型名；"
    "对话调用不受影响，可继续用「发一条消息」验证。"
)
_CHAT_LOCKS: Dict[str, threading.RLock] = {}
_CHAT_LOCKS_GUARD = threading.Lock()
_CHAT_ENV_KEYS = {
    "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
}
ALLOWED_COMMANDS = {
    "git", "git.exe",
    "node", "node.exe",
    "npm", "npm.cmd",
    "py", "py.exe", "python", "python.exe", "pytest", "pytest.exe",
}
#: 所有可执行文件都禁止的内联脚本 flag；比较前先把 `--flag=value` 切成 `--flag`
BLOCKED_COMMAND_FLAGS = {"-c", "--command", "-e", "--eval", "--exec"}
#: node 的内联执行变体（`node -p "js"` 与 `node --eval=<js>` 旧黑名单拦不住）
_NODE_INLINE_FLAGS = ("-p", "--print", "--eval", "-e", "--exec", "--input-type")
#: python -m 里允许直接装/改环境的模块一律拒绝（写进解释器自身 = 沙箱外落盘）
_PYTHON_BLOCKED_MODULES = {"pip", "pip3", "easy_install", "ensurepip", "venv", "setuptools", "ez_setup"}
#: npm 只放行跑本地脚本的操作；npx/npm exec/npm init 会从 registry 拉任意代码执行
_NPM_ALLOWED_OPS = {"run", "test", "start", "stop", "restart"}
#: git 黑名单补充：difftool/mergetool 的 --extcmd/--tool 经 shell 执行任意命令，
#: fetch/push/pull 是出网通道（旧黑名单只拦了改配置的五个子命令）
_GIT_BLOCKED_OPS = {"clone", "init", "remote", "config", "worktree", "submodule",
                    "difftool", "mergetool", "fetch", "push", "pull", "daemon",
                    "filter-branch", "filter-repo", "p4", "svn"}


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


def key_candidates(model: dict) -> List[str]:
    """按优先级返回服务端会依次查询的密钥**环境变量名**（不含任何取值）。

    优先级（与历史行为完全一致）：档案的 ``key_env`` →
    ``MODEL_<档案 ID 大写>_API_KEY`` → ``OPENAI_API_KEY``（仅 openai 协议）。
    这是唯一的口径出处：UI 与诊断接口都必须读这份结果，不能自己复刻顺序，
    否则「界面上说没配密钥、服务端却能用」这类矛盾会再次出现。
    """
    configured = str(model.get("key_env") or model.get("api_key_env") or "").strip()
    candidates: List[str] = []
    if configured and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", configured):
        candidates.append(configured)
    safe_id = re.sub(r"[^A-Za-z0-9]+", "_", str(model.get("id") or "MODEL")).strip("_").upper()
    if safe_id:
        candidates.append("MODEL_%s_API_KEY" % safe_id)
    if str(model.get("protocol") or "").lower() == "openai":
        candidates.append("OPENAI_API_KEY")
    # key_env 直接写 OPENAI_API_KEY 这类情况会重复，保留首次出现的顺序去重
    unique: List[str] = []
    for name in candidates:
        if name not in unique:
            unique.append(name)
    return unique


def resolve_key(model: dict) -> tuple:
    """取到第一个有取值的 ``(环境变量名, 密钥)``；一个都没有就报错。

    返回值里的密钥只能在服务端用于发请求。写进 API 响应、对话记录或服务端
    日志都是泄漏——本模块的其它函数一律只回传变量名。
    """
    candidates = key_candidates(model)
    for name in candidates:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    raise errors.HarnessError(
        errors.E_MODEL_INVALID,
        "模型档案未配置服务端 API Key。请在服务端环境变量中设置对应密钥后重试。",
        "可用变量：%s" % ("、".join(candidates) if candidates else "（档案的 id 与 key_env 都推不出合法的环境变量名）"),
    )


def _model_key(model: dict) -> str:
    """按显式 key_env → 模型专属环境变量 → OPENAI_API_KEY 读取密钥。"""
    return resolve_key(model)[1]


def key_status(model: dict) -> dict:
    """密钥来源的只读诊断：候选变量名、生效变量名、是否取到值。

    ``effective`` 取到值时是那个变量名，一个都没取到时回退到首个候选名，
    这样界面能直接告诉操作者「该往哪个变量里写」。绝不返回取值本身。
    """
    candidates = key_candidates(model)
    effective = ""
    present = False
    for name in candidates:
        if os.environ.get(name, "").strip():
            effective = name
            present = True
            break
    if not present and candidates:
        effective = candidates[0]
    return {"candidates": candidates, "effective": effective, "present": present}


def has_usable_base_url(model: dict) -> bool:
    """base_url 是否填写且是 http(s) 地址（与 ``_base_url`` 同一口径）。"""
    try:
        _base_url(model)
    except errors.HarnessError:
        return False
    return True


def _base_url(model: dict) -> str:
    base = str(model.get("base_url") or "").strip().rstrip("/")
    if not base:
        # 以前这里静默兜底到 api.openai.com，使用者以为在调自己的模型，
        # 实际请求发去了 OpenAI 并因为模型名对不上而失败——必须显式报错
        raise errors.HarnessError(
            errors.E_MODEL_INVALID,
            "这个模型档案没有填 base_url，无法确定要调用哪个服务。"
            "请到「模型档案」填写：OpenAI 官方填 https://api.openai.com/v1，"
            "其他兼容服务填服务商给出的地址（通常以 /v1 结尾）。",
            "model_id=%s" % model.get("id"),
        )
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


def _sanitize_text(text: str, secret: str = "") -> str:
    """抹掉凭据后再回传/落日志的文本。

    两层防护：标签式凭据（``Authorization: …``、``api_key=…``、``Bearer …``）
    按模式替换；服务商把密钥原文嵌进错误信息里（``invalid key 'sk-xxx'``）
    的情况按值替换。诊断结果与服务端日志只允许出现这里出来的字符串。
    """
    cleaned = re.sub(
        r"(?i)(authorization|api[-_ ]?key)\s*[:=]\s*[^,\s}]+", r"\1: [redacted]", str(text or ""))
    cleaned = re.sub(r"(?i)\bbearer\s+[^\s,;\"'}]+", "Bearer [redacted]", cleaned)
    needle = str(secret or "")
    if needle:
        cleaned = cleaned.replace(needle, "[redacted]")
    return cleaned


def _clip(text: str, limit: int = DOCTOR_DETAIL_CHARS) -> str:
    """诊断文本限长：详情给操作者看趋势，不搬整份响应体。"""
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _scrub_secrets(node: object, secret: str) -> object:
    """最后一道闸：把结果树里任意位置出现的密钥取值（含带标签形态）抹掉。

    档位文本大多已经过 ``_sanitize_text``，这里是防「以后有人新增了字段却忘了
    清洗」的兜底——体检响应直接进浏览器，一次泄漏就是永久泄漏。
    """
    if not secret:
        return node
    if isinstance(node, str):
        return _sanitize_text(node, secret)
    if isinstance(node, list):
        return [_scrub_secrets(item, secret) for item in node]
    if isinstance(node, dict):
        return {key: _scrub_secrets(value, secret) for key, value in node.items()}
    return node


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
        detail = _sanitize_text(detail, key)
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回了 HTTP %s，请检查模型档案和服务端密钥。" % exc.code, detail)
    except (url_error.URLError, TimeoutError, OSError) as exc:
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口连接失败，请检查 base_url、网络和服务端密钥。", _sanitize_text(str(exc), key))
    try:
        value = json.loads(util.decode_output(raw))
    except (TypeError, ValueError) as exc:
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回的不是合法 JSON。", _sanitize_text(str(exc), key))
    if not isinstance(value, dict):
        raise errors.HarnessError(errors.E_CHAT_FAILED, "模型接口返回格式不受支持。")
    return value


def _get_json(url: str, key: str = "", timeout: float = DOCTOR_TIMEOUT_S) -> dict:
    """doctor 用的 GET：把结果摊平成 ``{status, value, error}``，不抛异常。

    ``key`` 为空时**不发** Authorization 头（未配置密钥也要能量出连通性）。
    网络层异常统一翻译成 ``status=None``，由 doctor 写成档位结论；诊断接口
    要的是「这一档通不通」这个业务事实，不是一次 500。
    """
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    req = url_request.Request(url, method="GET", headers=headers)
    status: Optional[int] = None
    try:
        with url_request.urlopen(req, timeout=timeout) as response:
            raw = response.read(DOCTOR_MAX_BYTES)
            code = int(getattr(response, "status", 0) or response.getcode() or 0)
            status = code or None
    except url_error.HTTPError as exc:
        status = int(exc.code)
        try:
            raw = exc.read(DOCTOR_MAX_BYTES)
        except OSError:
            raw = b""
    except (url_error.URLError, TimeoutError, OSError, ValueError) as exc:
        # ValueError：base_url 过了 http(s) 前缀校验但 host/port 仍然非法
        return {"status": None, "value": None, "error": _sanitize_text(str(exc), key)}
    text = util.decode_output(raw)
    if status is not None and 400 <= status < 500 and not text.strip():
        return {"status": status, "value": None, "error": ""}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"status": status, "value": None,
                "error": _sanitize_text(text[:DOCTOR_DETAIL_CHARS], key) if text.strip() else ""}
    return {"status": status, "value": value, "error": ""}


def _model_names(value: object) -> Optional[List[str]]:
    """把 /models 的响应摊平成模型名列表；结构不像列表时返回 None。

    OpenAI 兼容形态是 ``{"data": [{"id": …}]}``，也有服务商直接回数组或
    ``{"models": […]}``。这里刻意写得宽松，但只认「确有一段列表」，
    否则 doctor 会把「我们读不懂」误报成「模型名不存在」。
    """
    items: object = None
    if isinstance(value, dict):
        for field in ("data", "models", "result", "body"):
            if isinstance(value.get(field), list):
                items = value[field]
                break
        if items is None:
            return None
    elif isinstance(value, list):
        items = value
    else:
        return None
    names: List[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            for field in ("id", "name", "model", "slug"):
                candidate = item.get(field)
                if isinstance(candidate, str) and candidate.strip():
                    names.append(candidate.strip())
                    break
    return names


def _stage(stage_id: str, ok: Optional[bool], detail: str = "",
           http_status: Optional[int] = None, hint: str = "") -> dict:
    """一档诊断结果。``ok=None`` 表示这一档被跳过/无从判断，绝不冒充真假。"""
    return {
        "id": stage_id,
        "ok": ok,
        "detail": _clip(_sanitize_text(detail)),
        "http_status": http_status,
        "hint": _clip(_sanitize_text(hint)),
    }


def doctor(cfg: dict, model_id: str) -> dict:
    """分档体检一个已保存的模型档案：key → base_url → reach → model。

    除档案 id 不存在（``E_MODEL_NOT_FOUND``，由路由回 404）之外一律返回业务
    结果：「连不上」是给界面看的结论，不是异常。响应里只有环境变量名、
    base_url 与服务商状态码，绝不含密钥取值；``_get_json`` 可被测试替换，
    真实网络只在操作者点击「检测」时发生。
    """
    model = config.find_model(cfg, str(model_id or ""))
    stages: List[dict] = []

    # ---- 1. key：只看环境变量名，取值只在服务端用于发请求 ----------------
    candidates = key_candidates(model)
    key_name = ""
    secret = ""
    try:
        key_name, secret = resolve_key(model)
    except errors.HarnessError:
        pass
    if key_name:
        stages.append(_stage("key", True, key_name))
    else:
        stages.append(_stage(
            "key", False, candidates[0] if candidates else "",
            hint="在服务端（启动评测台的那台机器）设置环境变量 %s 后重启服务；"
                 "密钥不经过浏览器，也不写进 config.json。"
                 % (candidates[0] if candidates else "对应变量")))

    # ---- 2. base_url：必须是显式填写的 http(s) 地址 -----------------------
    base_url = ""
    try:
        base_url = _base_url(model)
    except errors.HarnessError as exc:
        stages.append(_stage("base_url", False, str(model.get("base_url") or ""), hint=exc.message))
    else:
        stages.append(_stage("base_url", True, base_url))

    # ---- 3/4. reach + model：需要一次真实的 GET {base_url}/models --------
    endpoint: Optional[str] = None
    try:
        _mode, endpoint = _endpoint(model)
    except errors.HarnessError:
        endpoint = None

    names: Optional[List[str]] = None
    if not base_url:
        stages.append(_stage("reach", None, "", hint="没有可用的 base_url，跳过连通性检查。"))
        stages.append(_stage("model", None, "", hint="未做模型名核对（没有可查询的模型列表）。"))
    else:
        probe_url = base_url + "/models"
        try:
            probe = _get_json(probe_url, secret, DOCTOR_TIMEOUT_S)
            if not isinstance(probe, dict):
                raise TypeError("服务商探测结果不是对象")
        except Exception as exc:  # noqa: BLE001 - 体检接口本身不许抛异常
            probe = {"status": None, "value": None, "error": _sanitize_text(str(exc), secret)}
        status = probe.get("status")
        value = probe.get("value")
        error = str(probe.get("error") or "")
        parsed = _model_names(value)
        code = int(status) if isinstance(status, int) else None
        if code is None:
            stages.append(_stage(
                "reach", False, "连接失败：%s" % (error or "未取得 HTTP 状态码"),
                hint="请检查 base_url、本机网络与代理设置；这一档只测 GET %s/models。" % base_url))
        elif code in (404, 405):
            stages.append(_stage("reach", None, "HTTP %s：没有 /models 路由。" % code,
                                 http_status=code, hint=DOCTOR_NO_LIST_HINT))
        elif 400 <= code < 500:
            stages.append(_stage(
                "reach", False, "HTTP %s：%s" % (code, _clip(error, 80)), http_status=code,
                hint=("档案没有可用的服务端密钥，服务商拒绝了未认证请求；配好环境变量后重试。"
                      if not key_name and code in (401, 403) else
                      "服务商拒绝了这次请求，请核对 base_url 与密钥（%s）。"
                      % ("已发送 %s" % key_name if key_name else "未发送密钥"))))
        elif code >= 500:
            stages.append(_stage("reach", False, "HTTP %s：%s" % (code, _clip(error, 80)),
                                 http_status=code, hint="服务商自身报错，稍后重试或换一家。"))
        elif parsed is None:
            stages.append(_stage("reach", None, "HTTP %s：返回内容不是一份模型列表。" % code,
                                 http_status=code, hint=DOCTOR_NO_LIST_HINT))
        else:
            names = parsed
            stages.append(_stage("reach", True, "HTTP %d：模型列表可用（%d 项）。" % (code, len(parsed)),
                                 http_status=code))

        if names is None:
            stages.append(_stage("model", None, "",
                                 hint="模型列表未取得或被跳过，无法核对模型名（不臆断可用与否）。"))
        else:
            wanted = str(model.get("model") or model.get("id") or "")
            if not wanted:
                stages.append(_stage("model", None, "", hint="档案没有填 model 名，无需核对。"))
            elif wanted in names:
                stages.append(_stage("model", True, "服务商列表里有 %s（共 %d 项）。" % (wanted, len(names))))
            else:
                stages.append(_stage(
                    "model", False, "服务商列表里没有 %s（共 %d 项）。" % (wanted, len(names)),
                    hint="可参考列表前几项：%s" % "、".join(names[:5])))

    # 跳过的档位（ok=None）不参与总判定：没有模型列表不算失败。
    overall = not any(stage["ok"] is False for stage in stages)
    payload = {
        "id": str(model.get("id") or model_id or ""),
        "ok": overall,
        "checked_at": util.iso_now(),
        "endpoint": endpoint,
        "stages": stages,
    }
    return dict(_scrub_secrets(payload, secret))


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
    # argv[0] 只允许裸命令名：带盘符/分隔符的可执行文件路径会把校验整个绕到沙箱外
    if argv[0] != ntpath.basename(argv[0]) or re.match(r"^[A-Za-z]:", argv[0]):
        raise ValueError("只允许按命令名运行（如 \"python\"），不允许指定可执行文件路径")
    executable = ntpath.basename(argv[0]).lower()
    if executable not in ALLOWED_COMMANDS:
        raise ValueError("只允许运行 git、python/pytest、node/npm 相关检查命令")
    lowered = [item.lower() for item in argv[1:]]
    # `--eval=<code>` 这类 = 连写形式与整参数形式同样危险，统一切掉 = 后缀再比对
    flag_parts = [item.split("=", 1)[0] for item in lowered]
    if any(flag in BLOCKED_COMMAND_FLAGS for flag in flag_parts):
        raise ValueError("不允许执行内联脚本；请把修改写入沙箱文件后再运行检查")
    bare_executable = executable.rsplit(".", 1)[0] if executable.endswith((".exe", ".cmd")) else executable
    if bare_executable == "node":
        for flag in flag_parts:
            # `-p`/`-e` 的短组合（如 `-pe`）也按内联执行处理
            if flag in _NODE_INLINE_FLAGS or flag in {"-pe", "-ep"}:
                raise ValueError("不允许执行内联脚本；请把修改写入沙箱文件后再运行检查")
    if bare_executable == "python" or bare_executable == "py":
        for index, flag in enumerate(flag_parts):
            # 按首段模块名匹配：`-m pip.__main__` 这种绕过 `-m pip` 精确匹配的
            # 写法一样是 pip 本体，必须同拦。
            if flag == "-m" and index + 1 < len(flag_parts):
                module = flag_parts[index + 1].split(".", 1)[0]
                if module in _PYTHON_BLOCKED_MODULES:
                    raise ValueError("不允许通过 python -m 安装依赖或创建环境（pip/venv 等）")
    if bare_executable == "git":
        operation = next((item for item in lowered if not item.startswith("-")), "")
        if operation in _GIT_BLOCKED_OPS:
            raise ValueError("不允许改变仓库来源、git 配置或与外部仓库交换代码")
    if bare_executable == "npm":
        operation = next((item for item in lowered if not item.startswith("-")), "")
        if operation not in _NPM_ALLOWED_OPS:
            raise ValueError("npm 只允许 run/test/start 等本地脚本操作；依赖已由评测台准备")
    for item in argv[1:]:
        normalized = str(item).replace("\\", "/")
        if (ntpath.isabs(str(item)) or normalized.startswith(("//", "~/"))
                or normalized.startswith(("/", "\\"))
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


def send(cfg: dict, run: dict, text: str, *, max_tool_rounds: int = MAX_TOOL_ROUNDS) -> dict:
    """发送一条用户消息并返回最终 assistant 消息及最新历史。"""
    text = str(text or "").strip()
    if not text:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "消息不能为空。")
    run_id = str(run.get("run_id") or "")
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
                for _round in range(max(1, min(MAX_TOOL_ROUNDS, int(max_tool_rounds or MAX_TOOL_ROUNDS)) + 1)):
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
                raise errors.HarnessError(errors.E_CHAT_FAILED, "模型连续调用工具超过上限，已停止本轮请求。")

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

