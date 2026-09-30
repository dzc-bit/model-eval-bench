"""模型评测台 · 本地服务入口（标准库 http.server，无第三方依赖）。

- 静态托管 console/static（/ 回退 index.html），静态文件缺失也不崩，返回中文 404；
- REST API 严格按设计文档 §15 实现，错误统一返回 {code, message}；
- 校验异步启动，前端轮询 GET /api/runs/{id} 拿状态 / 日志 / 报告。

启动：python console\\server.py --port 8899 --open
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import posixpath
import re
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import calibrate, checks, chat as chat_mod, config, errors, packs, report as report_mod  # noqa: E402
from harness import batch as batch_mod  # noqa: E402
from harness import runs, sandbox as sandbox_mod, selfcheck, util  # noqa: E402

#: 服务启动时间（用于 /api/health 的运行时长）
STARTED_AT = time.time()

#: health 结果缓存秒数（每次都去拉 pytest/node 版本太吵）
HEALTH_TTL = 10.0
_health_cache = {"at": 0.0, "payload": None}
_health_lock = threading.Lock()


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first(node_id: str) -> str:
    return node_id.split("__", 1)[0] if node_id else ""


# --------------------------------------------------------------------------
# 业务处理
# --------------------------------------------------------------------------

def api_health(cfg: dict) -> dict:
    """自检：python、pytest、node、磁盘、沙箱目录、受测仓库可达性。"""
    with _health_lock:
        now = time.time()
        if _health_cache["payload"] and now - _health_cache["at"] < HEALTH_TTL:
            return _health_cache["payload"]
        payload = _build_health(cfg)
        _health_cache["at"] = now
        _health_cache["payload"] = payload
        return payload


def _build_health(cfg: dict) -> dict:
    checks_list = []
    warnings = []

    py_ok = True
    py_value = "%s（%s）" % (sys.version.split()[0], os.path.basename(sys.executable))
    checks_list.append({"id": "python", "label": "Python", "ok": py_ok, "value": py_value})

    probe = util.run_cmd([sys.executable, "-c", "import pytest;print(pytest.__version__)"], timeout=30)
    pytest_ok = probe.ok and probe.stdout.strip()
    checks_list.append({
        "id": "pytest", "label": "pytest", "ok": bool(pytest_ok),
        "value": probe.stdout.strip() if pytest_ok else "不可用（评分将无法进行）",
    })
    if not pytest_ok:
        warnings.append("系统 Python 装不上 pytest，前端可以看，但所有题都跑不了校验。")

    node = _node_info(cfg)
    checks_list.append({
        "id": "node", "label": "Node", "ok": node["ok"],
        "value": node["value"] if node["ok"] else "%s（只有前端题需要）" % node["value"],
    })
    if not node["ok"]:
        warnings.append("找不到可用的 Node，前端题（vitest）暂时无法校验。")

    repo_ok, repo_msg = config.repo_readable(cfg)
    checks_list.append({"id": "repo", "label": "受测仓库", "ok": repo_ok, "value": repo_msg})
    if not repo_ok:
        warnings.append("受测仓库读不到（%s）。请检查 config.json 的 repo_root。" % repo_msg)

    sandbox_root = cfg["sandbox_root"]
    try:
        util.ensure_dir(sandbox_root)
        workspace_ok = os.path.isdir(sandbox_root) and os.access(sandbox_root, os.W_OK)
    except OSError:
        workspace_ok = False
    checks_list.append({
        "id": "workspace", "label": "文件夹沙箱", "ok": workspace_ok,
        "value": util.norm(sandbox_root) if workspace_ok else "沙箱根目录不可写",
    })
    if not workspace_ok:
        warnings.append("沙箱根目录不可写，无法准备内部工作区。")

    free = util.disk_free_bytes(cfg["sandbox_root"])
    disk_ok = free < 0 or free > 512 * 1024 * 1024
    checks_list.append({
        "id": "disk", "label": "沙箱磁盘", "ok": disk_ok,
        "value": ("可用 %s" % util.human_bytes(free)) if free >= 0 else "无法读取",
    })
    if not disk_ok:
        warnings.append("沙箱根所在磁盘可用空间不足 512MB，请先清理。")

    static_index = os.path.join(cfg["static_root"], "index.html")
    static_ok = os.path.isfile(static_index)
    checks_list.append({
        "id": "static", "label": "前端", "ok": static_ok,
        "value": "已就绪" if static_ok else "console\\static\\index.html 还没有",
    })

    task_list = packs.list_tasks(cfg)
    checks_list.append({
        "id": "packs", "label": "题库", "ok": True,
        "value": "%d 道题" % len(task_list),
    })
    if not task_list:
        warnings.append("packs\\ 下还没有任务包，任务库会是空的。")

    blocking = [c for c in checks_list if not c["ok"] and c["id"] in {"pytest", "repo", "disk", "workspace"}]
    return {
        "ok": not blocking,
        "checked_at": util.iso_now(),
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "checks": checks_list,
        "warnings": warnings,
        "checkers": checks.available(),
    }


def _node_info(cfg: dict) -> dict:
    """定位 Node：先看配置，再看受测仓库自带的 .tools，最后看 PATH。"""
    import shutil as _shutil
    candidates = []
    tools_dir = os.path.join(cfg["repo_root"], ".tools")
    if os.path.isdir(tools_dir):
        for name in sorted(os.listdir(tools_dir)):
            if name.lower().startswith("node"):
                exe = os.path.join(tools_dir, name, "node.exe")
                if os.path.isfile(exe):
                    candidates.append(exe)
    which = _shutil.which("node")
    if which:
        candidates.append(which)
    for exe in candidates:
        result = util.run_cmd([exe, "-v"], timeout=20)
        if result.ok and result.stdout.strip():
            return {"ok": True, "path": exe, "value": "%s（%s）" % (result.stdout.strip(), exe)}
    return {"ok": False, "path": "", "value": "未找到 node 可执行文件"}


def api_tasks(cfg: dict, query: dict) -> dict:
    """任务库（含校准状态与历史成绩）。"""
    tier = (query.get("tier") or "").strip()
    tag = (query.get("tag") or "").strip()
    items = packs.list_tasks(cfg)
    history = _task_history(cfg)
    out = []
    for item in items:
        if tier and item["tier"] != tier:
            continue
        if tag and tag not in item["tags"]:
            continue
        item = dict(item)
        item.pop("pack_dir", None)
        item["history"] = history.get(item["id"], {})
        out.append(item)
    return {"tasks": out, "count": len(out)}


def _task_history(cfg: dict) -> dict:
    """每道题的历史成绩（跑过几轮、最高分、最近一次）。"""
    out: dict = {}
    for run in runs.list_runs(cfg):
        task = run.get("task")
        if not task:
            continue
        entry = out.setdefault(task, {"runs": 0, "models": [], "best_score": 0.0, "last_at": ""})
        entry["runs"] += 1
        if run.get("model") not in entry["models"]:
            entry["models"].append(str(run.get("model")))
        score = float(run.get("last_score") or 0)
        entry["best_score"] = max(entry["best_score"], score)
        stamp = str(run.get("updated_at") or "")
        entry["last_at"] = max(entry["last_at"], stamp)
    return out


def api_task_detail(cfg: dict, task_id: str, run_id: str = "") -> dict:
    """meta + 对应运行记录已解锁的提示词。"""
    meta = packs.load_meta(cfg, task_id)
    latest = None
    task_runs = [run for run in runs.list_runs(cfg) if run.get("task") == task_id]
    if run_id:
        latest = next((run for run in task_runs if run.get("run_id") == run_id), None)
        if latest is None:
            raise errors.HarnessError(
                errors.E_RUN_NOT_FOUND,
                "这次运行记录不属于该任务或已被删除。",
                run_id,
            )
    elif task_runs:
        latest = task_runs[0]
    unlocked = int(latest.get("attempt") or 1) if latest else 0
    prompts = [p for p in packs.load_prompts(meta) if p["level"] <= (unlocked or meta["attempts"])]
    detail = {
        "id": meta["id"],
        "title": meta["title"],
        "tier": meta["tier"],
        "attempts": meta["attempts"],
        "summary": meta["summary"],
        "symptom": meta["symptom"],
        "tags": meta["tags"],
        "allowed_paths": meta["allowed_paths"],
        "forbidden_paths": meta["forbidden_paths"],
        "budget": meta["budget"],
        "calibration": meta["calibration"],
        "prompts": prompts,
        "unlocked_prompts": unlocked,
        "wiring_note": WIRING_NOTE,
        "run": None,
    }
    if latest:
        detail["run"] = {
            "run_id": latest["run_id"],
            "model": latest.get("model"),
            "attempt": latest.get("attempt"),
            "status": latest.get("status"),
            "sandbox": latest.get("sandbox"),
            "drive": latest.get("drive"),
            "revealed": bool(latest.get("revealed")),
        }
    return detail


#: 接线说明（设计文档 附录 A），固定文案，每次复制给模型
WIRING_NOTE = (
    "你面前有一个独立的代码仓库副本，工作目录是服务端返回的 sandbox 文件夹路径；"
    "它是唯一允许操作的位置，不要访问该目录之外的任何路径。\n"
    "请只在这个目录内工作；当前页面可以直接与模型对话，完成后说明改动，不要执行 git commit。"
)


def api_create_run(cfg: dict, body: dict) -> dict:
    """准备新一轮：{task, model, attempt} → {run_id, sandbox, prompt_level}。"""
    task = str(body.get("task") or "").strip()
    model = str(body.get("model") or "").strip()
    if not task or not model:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "缺少 task 或 model 参数。")
    attempt = _as_int(body.get("attempt"), 1)
    profile = config.find_model(cfg, model)
    if not chat_mod.is_supported_model(profile):
        raise errors.HarnessError(
            errors.E_CHAT_UNSUPPORTED,
            "当前工作台只支持 OpenAI-compatible Chat Completions 模型。请在模型档案中选择该接口后重试。",
            "protocol=%s api_mode=%s" % (
                profile.get("protocol"), profile.get("api_mode", config.DEFAULT_OPENAI_API_MODE)),
        )
    run = runs.create_run(cfg, task, model, attempt)
    return {
        "run_id": run["run_id"],
        "sandbox": run.get("sandbox", ""),
        "drive": run.get("drive", ""),
        "prompt_level": int(run.get("attempt") or 1),
        "task": run["task"],
        "model": run["model"],
        "status": run.get("status"),
        "baseline_digest": run.get("baseline_digest", ""),
        "chat": {"messages": [], "tools": chat_mod.TOOLS},
        "wiring_note": WIRING_NOTE,
    }


def api_chat_history(cfg: dict, run_id: str) -> dict:
    """读取当前 run 的服务端对话记录。"""
    run = runs.get_run(cfg, run_id)
    model = config.find_model(cfg, str(run.get("model") or ""))
    return {
        "run_id": run_id,
        "messages": chat_mod.messages(run),
        "chat_busy": chat_mod.send_active(run_id),
        "model": {"id": model.get("id"), "model": model.get("model"),
                   "protocol": model.get("protocol"), "api_mode": model.get("api_mode")},
        "tools": chat_mod.TOOLS,
    }


def api_chat_send(cfg: dict, run_id: str, body: dict) -> dict:
    """向当前 run 的模型发送一条消息，并驱动受限工具调用闭环。"""
    run = runs.get_run(cfg, run_id)
    text = body.get("message")
    if not isinstance(text, str) or not text.strip():
        raise errors.HarnessError(errors.E_BAD_REQUEST, "消息不能为空。")
    return chat_mod.send(cfg, run, text)


def api_run_view(cfg: dict, run_id: str) -> dict:
    return runs.run_view(cfg, runs.get_run(cfg, run_id),
                         log_tail=int(cfg["grade"].get("log_tail_lines", 400)))


def api_scoreboard(cfg: dict, query: dict) -> Tuple[dict, str]:
    fmt = str(query.get("format") or "json").lower()
    board = runs.scoreboard(cfg)
    if fmt == "csv":
        return {"__csv__": runs.scoreboard_csv(board)}, "text/csv; charset=utf-8"
    return board, "application/json; charset=utf-8"


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

class Router:
    """极简路由表：方法 + 正则 → 处理函数。"""

    def __init__(self):
        self.routes = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self.routes.append((method, re.compile("^%s$" % pattern), handler))

    def match(self, method: str, path: str):
        allowed = set()
        for route_method, regex, handler in self.routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method != method:
                allowed.add(route_method)
                continue
            return handler, found.groupdict()
        if allowed:
            raise errors.HarnessError(
                errors.E_METHOD_NOT_ALLOWED,
                "这个地址不支持 %s 方法。" % method,
                "支持：%s" % "、".join(sorted(allowed)),
            )
        raise errors.HarnessError(errors.E_NOT_FOUND, "接口不存在：%s" % path, path)


def build_router() -> Router:
    r = Router()
    r.add("GET", r"/api/health", lambda ctx: (api_health(ctx["cfg"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/tasks", lambda ctx: (api_tasks(ctx["cfg"], ctx["query"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/tasks/(?P<task_id>[^/]+)/leaderboard", lambda ctx: (runs.task_leaderboard(ctx["cfg"], ctx["task_id"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/tasks/(?P<task_id>[^/]+)", lambda ctx: (api_task_detail(ctx["cfg"], ctx["task_id"], str(ctx["query"].get("run_id") or "")), "application/json; charset=utf-8"))
    r.add("GET", r"/api/runs", lambda ctx: (_list_runs(ctx["cfg"], ctx["query"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs", lambda ctx: (api_create_run(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/runs/(?P<run_id>[^/]+)/chat", lambda ctx: (api_chat_history(ctx["cfg"], ctx["run_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/chat", lambda ctx: (api_chat_send(ctx["cfg"], ctx["run_id"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/runs/(?P<run_id>[^/]+)", lambda ctx: (api_run_view(ctx["cfg"], ctx["run_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/grade", lambda ctx: (runs.start_grade(ctx["cfg"], ctx["run_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/promote", lambda ctx: (runs.promote(ctx["cfg"], ctx["run_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/reveal", lambda ctx: (runs.reveal(ctx["cfg"], ctx["run_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/note", lambda ctx: (runs.set_note(ctx["cfg"], ctx["run_id"], str(ctx["body"].get("note") or "")), "application/json; charset=utf-8"))
    r.add("POST", r"/api/runs/(?P<run_id>[^/]+)/diff", lambda ctx: ({"diff": runs.load_diff(ctx["cfg"], runs.get_run(ctx["cfg"], ctx["run_id"]))}, "application/json; charset=utf-8"))
    r.add("POST", r"/api/sandbox/reset", lambda ctx: (_reset(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/sandbox/rebuild", lambda ctx: (_rebuild(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/scoreboard", lambda ctx: api_scoreboard(ctx["cfg"], ctx["query"]))
    r.add("GET", r"/api/models", lambda ctx: ({"models": runs.list_models(ctx["cfg"])}, "application/json; charset=utf-8"))
    r.add("POST", r"/api/models", lambda ctx: (runs.upsert_model(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("PATCH", r"/api/models", lambda ctx: (runs.upsert_model(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("DELETE", r"/api/models", lambda ctx: (runs.delete_model(ctx["cfg"], str(ctx["query"].get("id") or ctx["body"].get("id") or "")), "application/json; charset=utf-8"))
    r.add("POST", r"/api/calibration", lambda ctx: (calibrate.enqueue(ctx["cfg"], str(ctx["body"].get("task") or ""), str(ctx["body"].get("model") or ""), _as_int(ctx["body"].get("trials"), 5)), "application/json; charset=utf-8"))
    r.add("GET", r"/api/calibration", lambda ctx: (calibrate.queue_status(ctx["cfg"], str(ctx["query"].get("task") or ""), str(ctx["query"].get("model") or "")), "application/json; charset=utf-8"))
    r.add("POST", r"/api/calibration/cancel", lambda ctx: (calibrate.cancel(ctx["cfg"], str(ctx["body"].get("run_id") or "")), "application/json; charset=utf-8"))
    # 批量会话：一次排「多题 × 多模型」，并发数受配置上限约束
    r.add("GET", r"/api/batches", lambda ctx: (batch_mod.list_batches(ctx["cfg"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/batches", lambda ctx: (_create_batch(ctx["cfg"], ctx["body"]), "application/json; charset=utf-8"))
    r.add("GET", r"/api/batches/(?P<batch_id>[^/]+)", lambda ctx: (batch_mod.get(ctx["cfg"], ctx["batch_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/batches/(?P<batch_id>[^/]+)/cancel", lambda ctx: (batch_mod.cancel(ctx["cfg"], ctx["batch_id"]), "application/json; charset=utf-8"))
    r.add("POST", r"/api/selfcheck", lambda ctx: (selfcheck.scan(ctx["cfg"]), "application/json; charset=utf-8"))
    return r


def _create_batch(cfg: dict, body: dict) -> dict:
    """建一个批量跑批：`{items: [{task, model, attempt}], concurrency?}`。

    也支持更省事的展开式：`{tasks: [...], models: [...], attempt?}` —— 直接做笛卡尔积。
    """
    items = body.get("items")
    if not isinstance(items, list) or not items:
        tasks = body.get("tasks") or ([body["task"]] if body.get("task") else [])
        models = body.get("models") or ([body["model"]] if body.get("model") else [])
        if not isinstance(tasks, list) or not isinstance(models, list):
            raise errors.HarnessError(
                errors.E_BAD_REQUEST,
                "批量跑批要给出 items，或同时给出 tasks 与 models 两个数组。",
            )
        attempt = _as_int(body.get("attempt"), 1)
        items = [
            {"task": str(t), "model": str(m), "attempt": attempt}
            for t in tasks for m in models
        ]
    concurrency = body.get("concurrency")
    if concurrency is not None:
        concurrency = _as_int(concurrency, batch_mod.max_concurrency(cfg))
    return batch_mod.start(cfg, items, concurrency=concurrency)


def _list_runs(cfg: dict, query: dict) -> dict:
    task = str(query.get("task") or "")
    model = str(query.get("model") or "")
    items = []
    for run in runs.list_runs(cfg):
        if task and run.get("task") != task:
            continue
        if model and str(run.get("model")) != model:
            continue
        items.append({
            "run_id": run["run_id"], "task": run.get("task"), "model": run.get("model"),
            "attempt": run.get("attempt"), "status": run.get("status"),
            "drive": run.get("drive"), "created_at": run.get("created_at"),
            "score": run.get("last_score"), "revealed": bool(run.get("revealed")),
        })
    return {"runs": items, "count": len(items)}


def _reset(cfg: dict, body: dict) -> dict:
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "缺少 run_id 参数。")
    return runs.reset_sandbox(cfg, run_id)


def _rebuild(cfg: dict, body: dict) -> dict:
    task = str(body.get("task") or "").strip()
    run_id = str(body.get("run_id") or "").strip()
    if not task:
        raise errors.HarnessError(errors.E_BAD_REQUEST, "缺少 task 参数。")
    return runs.rebuild_sandbox(cfg, task, run_id)


ROUTER = build_router()


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """把请求分给路由；出错统一吐 {code, message}。"""

    server_version = "ModelEvalConsole/1.0"
    protocol_version = "HTTP/1.1"

    # -- 基础输出 ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

    def _send_json(self, status: int, payload, extra: Optional[dict] = None) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8", extra)

    def _send_error_payload(self, exc: errors.HarnessError) -> None:
        self._send_json(exc.http_status, exc.to_dict())

    def _send_placeholder(self, path: str) -> None:
        """前端还没就位时给一张中文说明页，而不是抛 500。"""
        body = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>前端尚未就位</title>"
            "<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:4rem auto;max-width:40rem;"
            "line-height:1.8;color:#222}h1{font-size:1.4rem}code{background:#f4f4f5;padding:.1rem .3rem}</style>"
            "</head><body><h1>前端文件还没有就位</h1>"
            "<p>后端服务已正常启动，但你请求的页面不存在：<code>%s</code></p>"
            "<p>请确认 <code>console\\static\\index.html</code> 已生成后刷新本页。"
            "接口本身可以先用 <code>/api/health</code> 与 <code>/api/tasks</code> 验证。</p>"
            "</body></html>" % _escape_html(path)
        )
        self._send(404, body.encode("utf-8"), "text/html; charset=utf-8")

    # -- 请求入口 ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def _dispatch(self, method: str) -> None:
        parsed = _split_path(self.path)
        path, query = parsed
        if not path.startswith("/api/"):
            if method != "GET":
                self._send_error_payload(errors.HarnessError(
                    errors.E_METHOD_NOT_ALLOWED, "静态资源只支持 GET。"))
                return
            self._serve_static(path)
            return
        try:
            handler, params = ROUTER.match(method, path)
            body = self._read_body() if method in {"POST", "PATCH", "DELETE"} else {}
            cfg = _load_config()
            # 路径参数（task_id / run_id）直接摊到 ctx 上，
            # 路由表里的 lambda 才能用 ctx["run_id"] 这种写法取。
            ctx = {"cfg": cfg, "query": query, "body": body, "method": method}
            ctx.update(params)
            payload, content_type = handler(ctx)
            if isinstance(payload, dict) and "__csv__" in payload:
                body_bytes = payload["__csv__"].encode("utf-8-sig")
                self._send(200, body_bytes, content_type,
                           {"Content-Disposition": 'attachment; filename="scoreboard.csv"'})
                return
            self._send_json(200, payload)
        except errors.HarnessError as exc:
            self._send_error_payload(exc)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都要变成可读的中文
            detail = traceback.format_exc(limit=6)
            self.log_error("未预期异常：%r" % exc)
            self._send_json(errors.HTTP_STATUS[errors.E_INTERNAL], {
                "code": errors.E_INTERNAL,
                "message": "服务内部出错了。请把这一轮的详情发给维护者，或点「重建沙箱」后重试。",
                "detail": str(exc),
                "traceback": detail,
            })

    def _read_body(self) -> dict:
        try:
            length = _as_int(self.headers.get("Content-Length") or 0, 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            raise errors.HarnessError(errors.E_BAD_REQUEST, "请求体过大。")
        raw = self.rfile.read(length)
        text = util.decode_output(raw).strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise errors.HarnessError(errors.E_BAD_REQUEST, "请求体不是合法 JSON。", str(exc))
        if not isinstance(data, dict):
            raise errors.HarnessError(errors.E_BAD_REQUEST, "请求体顶层必须是 JSON 对象。")
        return data

    # -- 静态资源 ---------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        try:
            cfg = _load_config()
        except errors.HarnessError:
            cfg = {"static_root": os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")}
        static_root = cfg.get("static_root") or ""
        rel = posixpath.normpath(unquote(path)).lstrip("/")
        if rel in {"", ".", "/"}:
            rel = "index.html"
        target = os.path.normpath(os.path.join(static_root, rel.replace("/", os.sep)))
        # 目录穿越防护：解析后必须仍在 static 之内
        if not util.path_within(static_root, target):
            self._send_error_payload(errors.HarnessError(
                errors.E_BAD_REQUEST, "非法的资源路径。", path))
            return
        if os.path.isdir(target):
            index = os.path.join(target, "index.html")
            if not os.path.isfile(index):
                self._send_placeholder(path)
                return
            target = index
        if not os.path.isfile(target):
            self._send_placeholder(path)
            return
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            self._send_error_payload(errors.HarnessError(
                errors.E_INTERNAL, "读取静态文件失败。", str(exc)))
            return
        ctype, _ = mimetypes.guess_type(target)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript", "application/json")):
            ctype += "; charset=utf-8"
        # 前端是零构建直改直刷：静态资源一律不缓存，改完刷新就生效
        self._send(200, body, ctype)

    # -- 日志 -------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s - %s\n" % (
            time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def log_error(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] 错误 %s - %s\n" % (
            time.strftime("%H:%M:%S"), self.address_string(), fmt % args))


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def unquote(path: str) -> str:
    from urllib.parse import unquote as _unquote
    return _unquote(path)


def _split_path(raw: str) -> Tuple[str, dict]:
    from urllib.parse import parse_qs, urlsplit
    parts = urlsplit(raw)
    query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    return parts.path or "/", query


def _load_config() -> dict:
    return config.load()


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """线程化 HTTP 服务；校验在后台线程跑，轮询请求不会被拖住。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(host: str, port: int, open_browser: bool = False) -> int:
    """起服务并（可选）打开浏览器。"""
    try:
        cfg = config.load()
        config.ensure_workspace_dirs(cfg)
        recovered = sandbox_mod.recover_interrupted_prepares(
            cfg, log=lambda message: sys.stdout.write("[恢复] %s\n" % message))
        if recovered:
            sys.stdout.flush()
    except errors.HarnessError as exc:
        sys.stderr.write("配置有问题：%s\n%s\n" % (exc.message, exc.detail))
        return 2

    httpd = Server((host, port), Handler)
    url = "http://%s:%d/" % (host, port)
    sys.stdout.write("模型评测台已启动：%s\n" % url)
    sys.stdout.write("受测仓库（只读）：%s\n" % cfg["repo_root"])
    sys.stdout.write("按 Ctrl+C 停止。\n")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\n正在停止…\n")
    finally:
        httpd.server_close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="模型评测台本地服务")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认读 config.json）")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    host = args.host
    port = args.port
    if host is None or port is None:
        try:
            cfg = config.load()
        except errors.HarnessError:
            cfg = {"host": "127.0.0.1", "port": 8899}
        host = host or cfg.get("host", "127.0.0.1")
        port = port or int(cfg.get("port", 8899))
    if not args.open:
        args.open = True     # 双击启动时默认打开浏览器
    return serve(host, port, open_browser=args.open)


if __name__ == "__main__":
    sys.exit(main())
