"""读取与保存 console/config.json。

设计依据：设计文档 §2「换受测仓库或换仓库位置只需改 config.json 一行」。
本模块是唯一知道配置默认值的地方，其余模块都从这里取路径。
"""

from __future__ import annotations

import os
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

_WRITE_LOCK = threading.RLock()


def _defaults(overrides: dict) -> dict:
    """配置缺项时用的默认值；path 相对评测台根解析。"""
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
        "timeouts": {
            "prepare_s": 180,
            "grade_default_s": 240,
            "grade_max_s": 1800,
            "api_grade_s": 300,
        },
        "grade": {
            "diff_line_cap": 4000,
            "similarity_threshold": 0.6,
            "log_tail_lines": 400,
        },
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
    return base


def _resolve(root: str, value: str) -> str:
    return util.norm(value if os.path.isabs(value) else os.path.join(root, value))


def load() -> dict:
    """读配置并补齐默认值，路径字段统一解析成绝对路径。"""
    raw = util.read_json(CONFIG_PATH, default=None)
    if raw is None:
        if not os.path.isfile(CONFIG_PATH):
            raise errors.HarnessError(
                errors.E_CONFIG_INVALID,
                "找不到配置文件。请确认 console\\config.json 存在。",
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

    for section, keys in (("timeouts", ("prepare_s", "grade_default_s", "grade_max_s", "api_grade_s")),
                          ("grade", ("diff_line_cap", "similarity_threshold", "log_tail_lines"))):
        if not isinstance(cfg.get(section), dict):
            raise errors.HarnessError(errors.E_CONFIG_INVALID, "配置节 %s 必须是对象。" % section)
        for key in keys:
            try:
                cfg[section][key] = type(cfg[section][key])(cfg[section][key])
            except (KeyError, TypeError, ValueError):
                raise errors.HarnessError(
                    errors.E_CONFIG_INVALID, "配置项 %s.%s 必须是数字。" % (section, key))

    if not isinstance(cfg.get("models"), list):
        raise errors.HarnessError(errors.E_CONFIG_INVALID, "models 必须是数组。")
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
