"""读取与保存 console/config.json。

设计依据：设计文档 §2「换受测仓库或换仓库位置只需改 config.json 一行」。
本模块是唯一知道配置默认值的地方，其余模块都从这里取路径。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from typing import Any

from . import errors, util

#: console 目录（server.py / config.json / harness / static 都在这下面）
CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 评测台根目录（console 的上一级，也是 sandbox/runs/packs 的落点）
EVAL_ROOT = os.path.dirname(CONSOLE_DIR)
CONFIG_PATH = os.path.join(CONSOLE_DIR, "config.json")

#: 配置里模型档案的字段（设计文档 §15）。
#: ``api_mode`` 把 OpenAI 兼容协议下实际要接的 endpoint 记录清楚。
MODEL_FIELDS = ("id", "protocol", "api_mode", "base_url", "model", "key_masked", "key_env", "note")
MODEL_PROTOCOLS = ("openai", "anthropic", "gemini", "custom")
OPENAI_API_MODES = ("responses", "chat_completions", "completions")
MODEL_API_MODES = OPENAI_API_MODES + ("native",)
DEFAULT_OPENAI_API_MODE = "chat_completions"

#: 供应商层的字段（2026-10-01 模型配置重构，对齐 DSH 的 provider/model 分层）。
#: 端点、协议、密钥属于供应商；容量与输出上限属于模型——同一供应商下加模型
#: 不必重复填 base_url。密钥只存"引用"（keyring 按 provider_id 存），配置里不含秘密。
PROVIDER_FIELDS = ("id", "display_name", "protocol", "api_mode", "base_url",
                   "default_context_window", "default_max_tokens", "key_masked",
                   "legacy_ids", "note", "models")
#: 模型条目字段。context_window / max_tokens 留空则继承供应商的 default_*。
PROVIDER_MODEL_FIELDS = ("id", "name", "context_window", "max_tokens", "note")
#: 容量兜底默认值，取自 DSH 的 defaultContextWindow / defaultMaxTokens。
DEFAULT_CONTEXT_WINDOW = 262_144
DEFAULT_MAX_TOKENS = 32_768

#: 内置对话送给模型的上下文窗口默认值（config.json 的 chat 节可按字段覆盖）。
#: 单位与取舍见 _defaults() 里的注释；前端展示与 chat.jsonl 落盘不受这些值约束。
DEFAULT_CHAT = {
    "max_history": 100,
    "max_context_chars": 160_000,
    "tool_summary_chars": 800,
    "keep_first_prompt": True,
}

_WRITE_LOCK = threading.RLock()


def _defaults(overrides: dict) -> dict:
    """配置缺项时用的默认值；path 相对评测台根解析。"""
    default_timeouts = {
        "prepare_s": 180,
        "grade_default_s": 240,
        "grade_max_s": 1800,
        "api_grade_s": 300,
        # 内置对话里「单次模型调用」的网络超时，不是整轮对话的超时：
        # 一轮可以连跑几十次调用，整轮不受任何时限约束。
        "chat_s": 600,
    }
    default_grade = {"diff_line_cap": 4000, "similarity_threshold": 0.6, "log_tail_lines": 400}
    base = {
        "port": 8899,
        "host": "127.0.0.1",
        "repo_root": r"D:\New project 6",
        "sandbox_root": "sandboxes",
        "packs_root": "packs",
        "runs_root": "runs",
        "static_root": os.path.join("console", "static"),
        "snapshot_cache": os.path.join("sandboxes", ".snapshots"),
        # 批量并发只受进程内工作线程和配置限制，不创建任何盘符映射。
        "max_concurrency": max(1, (os.cpu_count() or 2) // 2),
        "timeouts": default_timeouts,
        "grade": default_grade,
        # 内置对话送给模型的上下文窗口与压缩参数，逐项含义见 DEFAULT_CHAT 上方说明。
        # 只影响发给模型的上下文；前端展示与 chat.jsonl 落盘都是完整记录。
        "chat": dict(DEFAULT_CHAT),
        "snapshot": {
            "include": ["backend", "frontend", "tests", "scripts",
                        "pyproject.toml", "package.json", ".gitignore"],
            "docs_include": ["README.md"],
            "exclude_dirs": [".git", "docs", "release-assets", "dist", "src-tauri",
                             ".reference", "运行产物", ".tools", ".venv", ".venv-release",
                             ".astock-cache", ".pytest_cache", ".ruff_cache",
                             "__pycache__", ".idea", ".vscode"],
            "exclude_globs": ["**/__pycache__/**", "**/*.pyc", "**/*.pyo",
                              "**/.pytest_cache/**", "**/*.egg-info/**",
                              "**/node_modules/**", "**/dist/**", "**/coverage/**",
                              "**/htmlcov/**", "**/*.log", "**/.coverage"],
            "secret_globs": ["**/.env", "**/.env.*", "**/*.pem", "**/*.key",
                             "**/id_rsa*", "**/id_ed25519*", "**/.npmrc", "**/.pypirc",
                             "**/credentials*", "**/*secret*", "**/*token*.json"],
            "forbidden_literals": [r"D:\New project 6", "醍醐测试"],
            "max_file_bytes": 4 * 1024 * 1024,
        },
        "models": [],
    }
    base.update(overrides)
    # timeouts / grade / chat 三节按字段合并：config.json 里只写其中一项时，
    # 其余仍取默认值；整节替换会让「新增一个默认项」把已有配置直接判成无效。
    for section, defaults in (("timeouts", default_timeouts),
                              ("grade", default_grade),
                              ("chat", DEFAULT_CHAT)):
        override = overrides.get(section)
        # 缺这一节（None）或类型不对都不参与合并：merged 本来就是 defaults，
        # 不动它就是"用兜底值"。少了 isinstance 这半个条件，
        # 手写一份没带 timeouts 的最小 config.json 会直接 TypeError 起不来。
        if not isinstance(override, dict):
            continue
        merged = dict(defaults)
        merged.update(override)
        base[section] = merged
    return base


#: 回环地址（整台机器自己）。中转站跑在本机时推不出厂商名，只能按端口认人。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _endpoint_host_port(base_url: str) -> tuple:
    """拆端点：返回 (小写主机名, 端口)。地址不合法就两个空值，不抛。"""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(str(base_url or ""))
        port = int(parts.port or 0)     # 端口写了但不是数字时这里会抛
    except (ValueError, TypeError):
        return "", 0
    return (parts.hostname or "").lower(), port


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _is_private_host(host: str) -> bool:
    """回环 / RFC1918 / 链路本地：不是厂商，是本机或内网里的中转。"""
    if _is_loopback(host):
        return True
    if host.startswith(("10.", "192.168.", "169.254.")):
        return True
    match = re.match(r"^172\.(\d{1,3})\.", host)
    return bool(match and 16 <= int(match.group(1)) <= 31)


def _slug(text: str) -> str:
    """可读 id 片段：小写字母数字，非字母数字一律压成连字符。"""
    return re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-").lower()


def _provider_identity(base_url: str) -> tuple:
    """从端点推 ``(供应商 id, 显示名, 类别)``，类别为 vendor / relay / unknown。

    名字**只由 base_url 决定**：同一个端点迁移多少次、模型按什么顺序排、
    排在第几位，推出来的名字都一样。掺进档案 id 或序号就会漂——同一个中转站
    因为"第一个模型叫什么"顶着不同的名字，用户不手动改就一直认错供应商。

    - 真实厂商取域名核心段：``api.deepseek.com`` → ``deepseek``。
    - 本机中转：``http://127.0.0.1:20128/v1`` → ``local-20128``，
      显示名写明「本机中转 127.0.0.1:20128」（ip 推不出厂商名）。
    - 内网中转：``192.168.1.50:8080`` → ``lan-192-168-1-50-8080``，
      与本机中转分开，免得两台机器上的同端口中转撞成一个名字。
    - 地址为空（老档案没填 base_url）推不出名字，回落由调用方按档案 id 决定。
    """
    host, port = _endpoint_host_port(base_url)
    if not host:
        return "", "", "unknown"
    if _is_private_host(host):
        loopback = _is_loopback(host)
        stem = "local" if loopback else "lan-" + _slug(host)
        where = "%s:%d" % (host, port) if port else host
        return ("%s-%d" % (stem, port) if port else stem,
                "%s %s" % ("本机中转" if loopback else "内网中转", where),
                "relay")
    parts = [p for p in host.split(".") if p]
    # 取倒数第二段（api.example.com → example；example.com → example）
    label = _slug(parts[-2] if len(parts) >= 2 else parts[0])
    if not label:
        return "", "", "unknown"
    if label[0].isdigit():
        label = "p-" + label       # 与前端 suggestId 同口径：数字开头补个前缀
    return label, label, "vendor"


def _assign_provider_names(grouped: dict, order: list) -> None:
    """给每个分组定 id 与显示名：先各自按端点推导，重名的再加稳定短后缀。

    后缀取端点自己的哈希而不是"第几个撞车"：两个都叫 deepseek 的端点里，
    后加进来的那个不会把先前那个的名字改掉；同一份配置迁移几次结果都一样。
    没有端点的老档案推不出名字，沿用档案 id（这也是它唯一的稳定标识）。
    """
    for key in order:
        item = grouped[key]
        pid, display, _kind = _provider_identity(item["base_url"])
        if not pid:
            pid = (item["legacy_ids"] or ["provider"])[0]
            display = pid
        item["id"] = pid
        item["display_name"] = display

    counts: dict = {}
    for key in order:
        counts[grouped[key]["id"]] = counts.get(grouped[key]["id"], 0) + 1
    dupes = {pid for pid, n in counts.items() if n > 1}
    if not dupes:
        return
    used = {grouped[k]["id"] for k in order if grouped[k]["id"] not in dupes}
    for key in order:
        item = grouped[key]
        if item["id"] not in dupes:
            continue
        digest = hashlib.sha1((item["base_url"] or key).encode("utf-8")).hexdigest()
        for size in (4, 6, 8, 16):
            candidate = "%s-%s" % (item["id"], digest[:size])
            if candidate not in used:
                item["id"] = candidate
                break
        else:
            item["id"] = "%s-%s" % (item["id"], digest)
        used.add(item["id"])
        # 显示名跟着区分开：显示名不掺哈希，写成「名字（端点）」还能一眼认出是哪家
        host, _port = _endpoint_host_port(item["base_url"])
        if host and host not in item["display_name"]:
            item["display_name"] = "%s（%s）" % (item["display_name"], host)


def _normalize_provider(raw: dict, index: int) -> dict:
    """把一个供应商条目归一成内部结构（含它展开后的模型列表）。"""
    pid = str(raw.get("id") or "").strip() or "provider-%d" % (index + 1)
    protocol = str(raw.get("protocol") or "openai").strip().lower()
    api_mode = str(raw.get("api_mode") or "").strip()
    base_url = str(raw.get("base_url") or "").strip()

    def _positive_int(value):
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    provider = {
        "id": pid,
        "display_name": str(raw.get("display_name") or pid).strip() or pid,
        "protocol": protocol,
        "api_mode": api_mode,
        "base_url": base_url,
        "default_context_window": _positive_int(raw.get("default_context_window")) or DEFAULT_CONTEXT_WINDOW,
        "default_max_tokens": _positive_int(raw.get("default_max_tokens")) or DEFAULT_MAX_TOKENS,
        "note": str(raw.get("note") or ""),
        "models": [],
    }
    if raw.get("key_masked"):
        provider["key_masked"] = str(raw["key_masked"])
    legacy = raw.get("legacy_ids")
    if isinstance(legacy, list) and legacy:
        provider["legacy_ids"] = [str(x) for x in legacy if x]

    for m_index, m in enumerate(raw.get("models") or []):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        # 容量与输出上限：模型级优先，留空继承供应商兜底。
        # 这样「整个中转站都是 128k」只需在供应商上填一次。
        provider["models"].append({
            "id": mid,
            "name": str(m.get("name") or mid).strip() or mid,
            "context_window": _positive_int(m.get("context_window")) or provider["default_context_window"],
            "max_tokens": _positive_int(m.get("max_tokens")) or provider["default_max_tokens"],
            "note": str(m.get("note") or ""),
        })
    return provider


def _providers_from_legacy_models(models: list) -> list:
    """把老的平铺 models 数组按 base_url 分组成供应商（读时迁移，不改盘）。

    老结构里每个模型各自重复写 base_url / protocol / api_mode，
    同端点的档案合并成一个供应商——这正是重构要消除的重复。

    供应商的名字从**端点**推导（见 ``_provider_identity``），不从第一个老档案的
    id 推：中转站里第一个模型往往只是碰巧排在前面，用它当供应商名会让整张卡
    顶着某个模型的名字，看上去像"这个供应商就这一个模型"。
    """
    grouped = {}
    order = []
    for item in models:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        base_url = str(item.get("base_url") or "").strip()
        key = base_url or "__no_url_%s" % mid
        if key not in grouped:
            grouped[key] = {
                "id": "",
                "display_name": "",
                "protocol": item.get("protocol") or "openai",
                "api_mode": item.get("api_mode") or "",
                "base_url": base_url,
                # 老档案的脱敏密钥带到新供应商上：不带的话界面上会显示
                # 「未配置密钥」，用户以为密钥丢了要重填
                "key_masked": str(item.get("key_masked") or ""),
                # 老档案 id 一并记下：本机密钥文件里那把密钥还挂在它们名下
                # （keyring 的 key 重构前是 model_id），查密钥时要能回退过去
                "legacy_ids": [],
                "note": "",
                "models": [],
            }
            order.append(key)
        if mid not in grouped[key]["legacy_ids"]:
            grouped[key]["legacy_ids"].append(mid)
        # 老结构里 model 字段才是「请求时填的模型名」；老 id 是档案名。
        # 迁移后模型 id 用 model 字段（那才是真正发给服务商的名字），
        # 老档案 id 记进 name，保证界面上还认得出原来叫啥。
        grouped[key]["models"].append({
            "id": str(item.get("model") or mid).strip() or mid,
            "name": mid,
            "note": str(item.get("note") or ""),
        })
    _assign_provider_names(grouped, order)
    return [grouped[k] for k in order]


def resolve_providers(raw: dict) -> list:
    """读出供应商列表：新结构直接用，老结构（平铺 models）读时迁移。"""
    providers = raw.get("providers")
    if isinstance(providers, list) and providers:
        return [_normalize_provider(p, i) for i, p in enumerate(providers) if isinstance(p, dict)]
    legacy = raw.get("models")
    if isinstance(legacy, list) and legacy:
        return [_normalize_provider(p, i) for i, p in enumerate(_providers_from_legacy_models(legacy))]
    return []


def expand_models(providers: list) -> list:
    """把供应商列表展开成扁平模型档案（下游 chat / grade / batch 只认这个形态）。

    每个档案带上它所属供应商的端点、协议与容量兜底，于是下游读
    ``model["base_url"]`` 的代码一行都不用改；同时带 ``provider_id``
    与 ``qualified_id``（``provider/model``），run 记录用后者区分同名模型。

    ``legacy_ids`` 也一起带下来：迁移来的供应商 id 是从端点推的，与当年存档的
    老档案 id 不同，而本机密钥文件里的密钥仍挂在老档案名下。少了这一段，
    ``chat._stored_key`` 只会按新 id 去查 → 取不到 → 「明明存过密钥却发无密钥请求」。
    """
    out = []
    for p in providers:
        legacy = [str(x) for x in (p.get("legacy_ids") or []) if str(x).strip()]
        for m in p.get("models") or []:
            item = {
                "id": m["id"],
                "name": m.get("name") or m["id"],
                "provider_id": p["id"],
                "provider_name": p.get("display_name") or p["id"],
                # 分隔符用 "::" 而不是 "/"：模型 id 本身常带斜杠
                # （cbcn/hy4-preview 这类中转站命名），再用 / 就分不清段落。
                "qualified_id": "%s::%s" % (p["id"], m["id"]),
                "protocol": p.get("protocol") or "openai",
                "api_mode": p.get("api_mode") or "",
                "base_url": p.get("base_url") or "",
                "context_window": m.get("context_window"),
                "max_tokens": m.get("max_tokens"),
                "note": m.get("note") or "",
                # 老字段名保留：chat.py 的 is_supported_model 等按 "model" 读请求名。
                "model": m["id"],
            }
            if legacy:
                item["legacy_ids"] = list(legacy)
            out.append(item)
    return out


def _resolve(root: str, value: str) -> str:
    return util.norm(value if os.path.isabs(value) else os.path.join(root, value))


#: 入库的配置模板。config.json 本身不入库——它装着模型档案、密钥脱敏值、
#: 各人的本机绝对路径，2026-10-01 之前曾经把这些带进过仓库。
#: 首次启动时从模板复制一份，之后各人改自己的。
CONFIG_TEMPLATE_PATH = os.path.join(CONSOLE_DIR, "config.example.json")


def ensure_config_file() -> str:
    """首次运行时从模板生成 config.json；已存在则原样返回路径。

    没有这一步，别人 clone 下来会没有 config.json、服务直接起不来。
    """
    if os.path.isfile(CONFIG_PATH) or not os.path.isfile(CONFIG_TEMPLATE_PATH):
        return CONFIG_PATH          # 后者交给 load() 报「找不到配置文件」
    with _WRITE_LOCK:
        if not os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_TEMPLATE_PATH, "r", encoding="utf-8") as src:
                    content = src.read()
                with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as dst:
                    dst.write(content)
            except OSError:
                pass                # 写不了就交给 load() 报错，不在这里吞异常
    return CONFIG_PATH


def load() -> dict:
    """读配置并补齐默认值，路径字段统一解析成绝对路径。"""
    ensure_config_file()
    raw = util.read_json(CONFIG_PATH, default=None)
    if raw is None:
        if not os.path.isfile(CONFIG_PATH):
            raise errors.HarnessError(
                errors.E_CONFIG_INVALID,
                "找不到配置文件。请确认 console\\config.json 存在"
                "（可以复制 console\\config.example.json 作为起点）。",
                CONFIG_PATH,
            )
        raise errors.HarnessError(
            errors.E_CONFIG_INVALID,
            "配置文件不是合法 JSON。请检查 console\\config.json 的格式。",
            CONFIG_PATH,
        )
    if not isinstance(raw, dict):
        raise errors.HarnessError(errors.E_CONFIG_INVALID, "配置文件顶层必须是对象。", CONFIG_PATH)

    cfg = _defaults(raw)

    # 按仓库 ID 配置多个受测仓库（题包 meta.repo.id → 路径）。
    # 没配的题包回退到 repo_root——这样 A 股仓库不在本机时，demo 包照常能跑，
    # core 包也会得到"哪个仓库没配"的明确报错，而不是含糊的复制失败。
    repos_raw = raw.get("repos") if isinstance(raw.get("repos"), dict) else {}
    cfg["repos"] = {}
    for rid, rpath in repos_raw.items():
        if isinstance(rid, str) and rid and isinstance(rpath, str) and rpath:
            cfg["repos"][rid] = _resolve(EVAL_ROOT, rpath)

    for key in ("repo_root", "sandbox_root", "packs_root", "runs_root", "static_root",
                "snapshot_cache"):
        if not isinstance(cfg.get(key), str) or not cfg.get(key):
            raise errors.HarnessError(
                errors.E_CONFIG_INVALID, "配置项 %s 必须是非空字符串。" % key)
        cfg[key] = _resolve(EVAL_ROOT, cfg[key])

    try:
        cfg["max_concurrency"] = max(1, int(cfg.get("max_concurrency") or 1))
    except (TypeError, ValueError):
        raise errors.HarnessError(errors.E_CONFIG_INVALID, "配置项 max_concurrency 必须是正整数。")

    for section, keys in (("timeouts", ("prepare_s", "grade_default_s", "grade_max_s", "api_grade_s", "chat_s")),
                          ("grade", ("diff_line_cap", "similarity_threshold", "log_tail_lines"))):
        if not isinstance(cfg.get(section), dict):
            raise errors.HarnessError(errors.E_CONFIG_INVALID, "配置节 %s 必须是对象。" % section)
        for key in keys:
            try:
                cfg[section][key] = type(cfg[section][key])(cfg[section][key])
            except (KeyError, TypeError, ValueError):
                raise errors.HarnessError(
                    errors.E_CONFIG_INVALID, "配置项 %s.%s 必须是数字。" % (section, key))

    if not isinstance(cfg.get("chat"), dict):
        raise errors.HarnessError(errors.E_CONFIG_INVALID, "配置节 chat 必须是对象。")

    if not isinstance(cfg.get("models"), list):
        raise errors.HarnessError(errors.E_CONFIG_INVALID, "models 必须是数组。")

    # 供应商层：新结构读 providers，老结构（平铺 models）读时迁移成供应商。
    # cfg["models"] 是展开后的扁平档案，供下游沿用；cfg["providers"] 是嵌套结构，
    # 供「模型档案」页编辑。两者由同一份原始数据派生，不会各说各话。
    cfg["providers"] = resolve_providers(raw)
    cfg["models"] = expand_models(cfg["providers"]) or cfg["models"]
    cfg["providers_migrated"] = bool(
        not raw.get("providers") and isinstance(raw.get("models"), list) and raw.get("models")
    )
    return cfg


def save(cfg: dict) -> None:
    """整份配置原子落盘（只保留可序列化的原始字段，不写派生路径）。"""
    with _WRITE_LOCK:
        util.write_json_atomic(CONFIG_PATH, cfg)


def update_models(models: list) -> None:
    """增删改模型档案：只改 models 字段，其余配置原样保留。"""
    with _WRITE_LOCK:
        raw = util.read_json(CONFIG_PATH, default={}) or {}
        raw["models"] = models
        util.write_json_atomic(CONFIG_PATH, raw)


def update_providers(providers: list) -> None:
    """增删改供应商（含其模型清单）：只改 providers 字段。

    首次保存时把老的 models 字段清掉——两套结构并存会让「读时迁移」
    每次启动都把用户刚编辑的内容再分组一遍，越改越乱。
    """
    with _WRITE_LOCK:
        raw = util.read_json(CONFIG_PATH, default={}) or {}
        raw["providers"] = providers
        raw.pop("models", None)
        util.write_json_atomic(CONFIG_PATH, raw)


def find_model(cfg: dict, model_id: str) -> dict:
    """按 id 找模型档案；找不到就报可操作的错误码。"""
    for item in cfg.get("models", []):
        if isinstance(item, dict) and str(item.get("id")) == str(model_id):
            return item
    raise errors.HarnessError(
        errors.E_MODEL_NOT_FOUND,
        "找不到模型档案 %s。请到「模型」页新建档案后再运行。" % model_id,
    )


def repo_root_for(cfg: dict, meta: dict) -> str:
    """按题包 meta.repo.id 取受测仓库路径；未单独配置的回退 repo_root。"""
    rid = str((meta.get("repo") or {}).get("id") or "")
    root = (cfg.get("repos") or {}).get(rid)
    return root or cfg["repo_root"]


def repo_readable(cfg: dict) -> tuple:
    """受测仓库可读性自检（/api/health 用）。返回 (是否可读, 说明)。

    决定题目能否运行的是 repo_root_for()，它按题包 meta.repo.id 去取 cfg["repos"][id]。
    只检查 cfg["repo_root"] 会出现"设置页绿灯、核心题全废"，所以这里把 repos.* 一起查。
    """
    targets = [("repo_root", cfg.get("repo_root") or "")]
    targets += sorted(
        ("repos.%s" % rid, path) for rid, path in (cfg.get("repos") or {}).items()
    )
    problems = []
    for name, path in targets:
        if not path:
            problems.append("%s 未配置" % name)
        elif not os.path.isdir(path):
            problems.append("%s 目录不存在：%s" % (name, path))
        elif not os.access(path, os.R_OK):
            problems.append("%s 无读取权限：%s" % (name, path))
    if problems:
        return False, "；".join(problems)
    return True, "可读（%d 处）" % len(targets)


def ensure_workspace_dirs(cfg: dict) -> None:
    """把运行期需要的目录建出来（沙箱、记录、快照缓存）。"""
    for key in ("sandbox_root", "runs_root", "snapshot_cache", "packs_root"):
        util.ensure_dir(cfg[key])
