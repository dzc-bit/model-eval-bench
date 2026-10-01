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

#: 上下文窗口参数在 config.DEFAULT_CHAT（config.json 的 chat 节能按字段覆盖）。
#: 下面三个上限是工具执行的安全线，不随配置放松，只能收紧。
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
#: 正在执行 send 的运行（浏览器关掉/刷新后服务端线程还在跑，前端靠这个感知）
_ACTIVE_SENDS: set = set()
_ACTIVE_SENDS_GUARD = threading.Lock()
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
#: git 黑名单分两级：改仓库来源/配置的子命令，与纯出网通道（fetch/push/pull、
#: ls-remote/archive 同样会把沙箱内的东西送出去或从外网拉东西）。
_GIT_REPO_BLOCKED_OPS = {"clone", "init", "remote", "config", "worktree", "submodule",
                         "difftool", "mergetool", "daemon", "filter-branch", "filter-repo", "p4", "svn"}
_GIT_NETWORK_OPS = {"fetch", "push", "pull", "ls-remote", "archive"}


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


def _read_records(run: dict) -> List[dict]:
    """按落盘顺序读出全部对话记录，不做任何窗口裁剪。

    裁剪是「发给模型」那一侧的事（见 _model_history）；前端展示与 chat.jsonl
    都是完整记录，续轮时用户要能看见上一轮的全过程。
    """
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
            return out
    except OSError:
        return []


def _group_rounds(items: List[dict]) -> List[List[dict]]:
    """切成一轮一轮：一条 user 起头，后面跟着它自己的 assistant/tool。

    裁剪与压缩都以整轮为单位，tool 永远留在自己那一轮里，
    不会出现「assistant 带 tool_calls 却没有对应响应」的形态。
    """
    groups: List[List[dict]] = []
    current: List[dict] = []
    for item in items:
        if item.get("role") == "user" and current:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups


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
    """完整对话记录（前端展示用）：不受发给模型的上下文窗口约束。"""
    return _read_records(run)


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
        "模型档案还没有可用密钥。到「模型档案」页编辑该档案并粘贴 API 密钥，或在服务端环境变量中设置后重试。",
        "可用变量：%s" % ("、".join(candidates) if candidates else "（档案的 id 与 key_env 都推不出合法的环境变量名）"),
    )


def _model_key(model: dict) -> str:
    """按本机密钥文件（页面粘贴）→ key_candidates 口径的环境变量优先级读取密钥。"""
    stored = keyring.get_key(str(model.get("id") or ""))
    if stored:
        return stored
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
        if operation in _GIT_REPO_BLOCKED_OPS:
            raise ValueError("不允许改变仓库来源、git 配置或与外部仓库交换代码")
        if operation in _GIT_NETWORK_OPS:
            raise ValueError("不允许通过 git 访问网络；评测只允许读写当前沙箱内的文件")
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


def _system_prompt(run: dict, tool_enabled: bool = True, omitted_rounds: int = 0) -> str:
    root = util.norm(str(run.get("sandbox") or ""))
    prompt = (
        "你正在一个代码评测 harness 中工作。当前唯一允许读写和运行命令的工作区是：%s。"
        "所有相对路径都相对于该目录；不要访问工作区之外的文件、环境变量、密钥或网络，"
        "不要执行 git commit。请先检查现状，再用工具完成用户要求，最后简要说明改动。" % root
    )
    if tool_enabled:
        prompt += " 可用工具只能操作该工作区：list_files、read_file、write_file、run_command。"
    if omitted_rounds:
        # 上下文窗口放不下时才会走到这里：明确告诉模型更早的轮次被省略了，
        # 别让它以为对话只有这些（历史里的工具返回此时已压成摘要）。
        prompt += " 更早的 %d 轮对话因超出上下文窗口已省略，历史工具调用只剩摘要。" % omitted_rounds
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


def _chat_option(cfg: dict, key: str, minimum: int = 1) -> int:
    """读 config 的 chat 节数值项：坏值退回默认，再夹到下限。

    窗口数字写错不该让整条对话不可用，但也不能被写成 0 来绕过裁剪。
    """
    section = cfg.get("chat") if isinstance(cfg.get("chat"), dict) else {}
    default = config.DEFAULT_CHAT[key]
    try:
        value = int(section.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, value)


def _chat_flag(cfg: dict, key: str) -> bool:
    section = cfg.get("chat") if isinstance(cfg.get("chat"), dict) else {}
    return bool(section.get(key, config.DEFAULT_CHAT[key]))


def _tail_lines(text: str, count: int) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return " / ".join(lines[-count:]) if lines else "无输出"


def _call_index(group: List[dict]) -> Dict[str, dict]:
    """tool_call_id → {name, arguments}：给历史里的工具返回配回它自己的调用参数。"""
    index: Dict[str, dict] = {}
    for item in group:
        calls = item.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or not call.get("id"):
                continue
            function = call.get("function") or {}
            index[str(call["id"])] = {
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or ""),
            }
    return index


def _call_listing(tool_calls: List[dict]) -> str:
    """把一轮调用过哪些工具收成一行（塌缩轮次后留给模型的唯一线索）。"""
    counts: Dict[str, int] = {}
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        name = str(((call.get("function") or {}).get("name")) or "未知工具")
        counts[name] = counts.get(name, 0) + 1
    listed = "、".join("%s ×%d" % (name, n) if n > 1 else name for name, n in counts.items())
    return "（这一轮调用过：%s）" % listed if listed else "（这一轮只调用了工具）"


def _tool_summary(item: dict, call: dict, limit: int) -> str:
    """把历史轮的工具返回压成「工具名 + 参数 + 结果摘要」。

    write_file 的路径与字节数原样保留：模型必须记得自己改过哪些文件，
    否则续轮会重复劳动或覆盖已有成果（NOTES.md 第五节第 2/4 条）。
    当前轮刚拿到的结果不走这里，仍是全量（受原有上限约束）。
    """
    name = str(item.get("name") or call.get("name") or "未知工具")
    arguments = _clip(str(call.get("arguments") or "").replace("\n", " "), 160)
    try:
        payload = json.loads(item.get("content") or "")
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        digest = _clip(str(item.get("content") or ""), limit)
    elif payload.get("error"):
        digest = "失败：%s" % _clip(str(payload.get("error")), limit)
    elif name == "write_file":
        digest = "已写入 %s（%s 字节）" % (payload.get("path"), payload.get("bytes"))
    elif name == "run_command":
        tail = _tail_lines("%s\n%s" % (payload.get("stdout") or "", payload.get("stderr") or ""), 8)
        digest = "退出码 %s，输出末尾：%s%s" % (
            payload.get("exit_code"), tail, "；输出已被截断" if payload.get("output_limited") else "")
    elif name == "read_file":
        digest = "读过 %s（%s 字符%s），原文已省略，需要就看现状重读" % (
            payload.get("path"), len(str(payload.get("content") or "")),
            "，当次已截断" if payload.get("truncated") else "")
    elif name == "list_files":
        digest = "列出 %s 共 %d 个条目%s" % (
            payload.get("path"), len(payload.get("entries") or []),
            "，当次已截断" if payload.get("truncated") else "")
    else:
        digest = _clip(json.dumps(payload, ensure_ascii=False), limit)
    return _clip("工具 %s(%s) → %s" % (name, arguments, digest), max(limit, 240))


def _slim_call(call: dict) -> dict:
    """历史轮里 write_file 的正文参数换成「已省略 + 字节数」。

    文件已经落盘，续轮没必要把整份内容再读一遍给模型；路径必须留着，
    它得知道自己改过哪些文件。其余工具的参数体积小，原样保留。
    """
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or function.get("name") != "write_file":
        return call
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except (TypeError, ValueError):
        return call
    if not isinstance(arguments, dict) or not isinstance(arguments.get("content"), str):
        return call
    slimmed = dict(arguments)
    content = slimmed["content"]
    slimmed["content"] = "（正文 %d 字符已省略，文件已写入沙箱）" % len(content)
    copied = dict(call)
    copied["function"] = dict(function, arguments=json.dumps(slimmed, ensure_ascii=False))
    return copied


def _compress_group(group: List[dict], summary_chars: int) -> List[dict]:
    """历史轮第一档：工具返回换成摘要，assistant 的正文与思维链原样留着。"""
    calls = _call_index(group)
    out = []
    for item in group:
        if item.get("role") == "tool":
            call = calls.get(str(item.get("tool_call_id") or ""), {})
            out.append(dict(item, content=_tool_summary(item, call, summary_chars)))
        elif item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list) and item["tool_calls"]:
            out.append(dict(item, tool_calls=[_slim_call(call) for call in item["tool_calls"]]))
        else:
            out.append(item)
    return out


def _collapse_group(group: List[dict]) -> List[dict]:
    """历史轮第二档：tool 消息与 assistant.tool_calls 一起走，只留模型说过的话。

    tool_calls 必须同时删掉——留着它却没有对应 tool 响应，服务商按 OpenAI 序列校验会 400。
    """
    out = []
    for item in group:
        if item.get("role") == "tool":
            continue
        if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list) and item["tool_calls"]:
            copied = dict(item)
            listing = _call_listing(copied.pop("tool_calls"))
            content = str(copied.get("content") or "").strip()
            copied["content"] = ("%s\n%s" % (content, listing)).strip() if content else listing
            out.append(copied)
        else:
            out.append(item)
    return out


def _context_size(rounds: List[List[dict]]) -> tuple:
    flat = [item for group in rounds for item in group]
    chars = sum(len(json.dumps(item, ensure_ascii=False)) for item in flat)
    return len(flat), chars


def _model_history(cfg: dict, records: List[dict]) -> tuple:
    """组装发给模型的历史上下文，返回 (消息列表, 被整轮丢弃的轮数)。

    三层力度，从便宜到昂贵：
      1. 除最新一轮外的历史轮，工具返回摘要化；
      2. 仍超预算就把更老的轮次塌成「只剩模型说过的话」；
      3. 再超才整轮丢弃——钉住的第一条用户消息（题目提示词）与最新一轮不参与丢弃。

    旧实现是「一轮的消息数超过 MAX_HISTORY 就整轮裁掉」，实测受测模型每步并行
    6 个工具调用、一轮 100+ 条消息，续轮因此完全忘记上一轮做过什么，只能靠 git
    重新考古（NOTES.md 第五节第 4 条）。条数与字符是两个独立闸门，先到哪个都只
    触发压缩，不直接丢整轮。
    """
    items = [item for item in records if item.get("status") != "error"]
    groups = _group_rounds(items)
    if not groups:
        return [], 0
    if len(groups) == 1:
        return groups[0], 0
    summary_chars = _chat_option(cfg, "tool_summary_chars")
    max_items = _chat_option(cfg, "max_history")
    max_chars = _chat_option(cfg, "max_context_chars")
    keep_first = _chat_flag(cfg, "keep_first_prompt")

    older = groups[:-1]
    newest = groups[-1]
    levels = [_compress_group(group, summary_chars) for group in older]
    rounds = levels + [newest]

    def fits(candidate: List[List[dict]]) -> bool:
        count, chars = _context_size(candidate)
        return count <= max_items and chars <= max_chars

    if not fits(rounds):
        # 第 2 层：从最老的轮次开始塌成「只剩模型说过的话」。
        for index in range(len(levels)):
            levels[index] = _collapse_group(older[index])
            if fits(levels + [newest]):
                break
    dropped = 0
    guard = 0
    while not fits(levels + [newest]) and guard < 4 * (len(levels) + 2):
        guard += 1
        floor = 1 if keep_first else 0
        if len(levels) > floor:
            # 第 3 层：整轮丢弃也只丢最老的，钉住的第一条提示词那轮不动。
            levels.pop(1 if keep_first else 0)
            dropped += 1
            continue
        gutted = False
        for index in range(len(levels)):
            group = levels[index]
            if len(group) > 3:
                questions = [item for item in group if item.get("role") == "user"][:1]
                # 题目和模型自己最后一段正文留下（那是它做过什么、结论是什么的记忆），
                # 中间的工具往返换成一行省略说明。
                last_text = [item for item in group
                             if item.get("role") == "assistant" and str(item.get("content") or "").strip()][-1:]
                kept = questions + last_text
                levels[index] = kept + [{
                    "role": "assistant",
                    "content": "（这一轮的 %d 条对话与工具结果已省略）" % (len(group) - len(kept)),
                }]
                gutted = True
                break
        if not gutted:
            # 第 4 层兜底：最新一轮自己就超预算时宁可带着超出的窗口发出去，
            # 也不能把题目或当前诉求丢掉——那才是评测里最坏的失忆。
            break
    rounds = levels + [newest]
    return [item for group in rounds for item in group], dropped


def _history_for_api(history: List[dict]) -> List[dict]:
    """把内部记录换成服务商接受的消息序列。

    两条约束：
    - 每条 tool 必须紧跟在带对应 tool_calls 的 assistant 之后，配不上就丢掉——
      孤儿 tool 消息或悬空 tool_calls 都会让 OpenAI 兼容接口直接 400；
    - 内置对话每次都带 tools，按 DeepSeek 官方要求历史轮的 reasoning_content /
      reasoning 必须原样回传，所以这里不折叠也不丢弃思维链（NOTES.md 第五节第 3 条）。
    """
    out: List[dict] = []
    pending: set = set()
    pending_owner = -1

    def close_pending():
        """上一个 assistant 的 tool_calls 没等到响应就把它摘掉，别留下悬空调用。"""
        if pending and pending_owner >= 0:
            victim = out[pending_owner]
            victim.pop("tool_calls", None)
            if not str(victim.get("content") or "").strip():
                victim["content"] = "（工具调用及其结果已省略）"

    for item in history:
        if item.get("status") == "error":
            continue
        role = item.get("role")
        if role == "tool":
            call_id = str(item.get("tool_call_id") or "")
            if call_id not in pending:
                continue
            pending.discard(call_id)
            out.append({k: item[k] for k in ("role", "content", "tool_call_id") if k in item})
            continue
        close_pending()
        pending = set()
        pending_owner = -1
        if role not in {"user", "assistant"}:
            continue
        fields = ("role", "content", "tool_calls")
        if role == "assistant":
            fields += ("reasoning_content", "reasoning")
        entry = {k: item[k] for k in fields if k in item}
        out.append(entry)
        if role == "assistant" and isinstance(entry.get("tool_calls"), list) and entry["tool_calls"]:
            pending_owner = len(out) - 1
            pending = {str(call.get("id")) for call in entry["tool_calls"]
                       if isinstance(call, dict) and call.get("id")}
    close_pending()
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
        model_history, omitted_rounds = _model_history(cfg, _read_records(run))
        history = model_history
        timeout = float((cfg.get("timeouts") or {}).get("chat_s", 180) or 180)

        try:
            if mode == "chat_completions":
                api_messages = [{"role": "system", "content": _system_prompt(run, True, omitted_rounds)}] + _history_for_api(history)
                # 不限工具轮数：模型不再发起工具调用时自然收束；单轮请求有超时兜底
                read_slots: Dict[str, int] = {}
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
                        return {"message": saved, "messages": messages(run), "model": {"id": model.get("id"), "model": model.get("model"), "api_mode": mode}}
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
                        slot = len(api_messages)
                        api_messages.append({"role": "tool", "tool_call_id": tool_id, "content": serialized})
                        if name == "read_file" and isinstance(result.get("path"), str) and not result.get("error"):
                            # 同一路径只留最后一次原文（NOTES.md 第五节第 2 条）
                            previous = read_slots.get(result["path"])
                            if previous is not None:
                                api_messages[previous]["content"] = json.dumps(
                                    {"elided": "路径 %s 之后又被读取过，这份旧内容已省略" % result["path"]},
                                    ensure_ascii=False)
                            read_slots[result["path"]] = slot
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
            return {"message": saved, "messages": messages(run), "model": {"id": model.get("id"), "model": model.get("model"), "api_mode": mode}}
        except errors.HarnessError as exc:
            # 将失败放入可见记录，但不把它重新送回模型上下文。
            try:
                _append_message(run, {"role": "assistant", "content": exc.message,
                                      "status": "error", "error_code": exc.code})
            except errors.HarnessError:
                pass
            raise

