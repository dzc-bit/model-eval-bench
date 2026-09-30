"""验收：本地服务与 REST 接口（设计文档 §15）。

真起一个 ThreadingHTTPServer，用 urllib 打真实请求：
    · /api/health 报出 Python / pytest / Node / 受测仓库 / 文件夹沙箱根 / 磁盘；
    · 题库为空时 /api/tasks 返回空列表而不是报错；
    · 静态文件缺失时返回中文 404 页，服务不崩；
    · 所有错误统一是 {code, message}，错误码稳定可映射。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if CONSOLE_DIR not in sys.path:
    sys.path.insert(0, CONSOLE_DIR)

import server  # noqa: E402
from harness import errors, runs, util  # noqa: E402


@pytest.fixture
def live(cfg, monkeypatch):
    """在临时端口上真起一个服务，配置指向临时迷你仓库。"""
    httpd, port, thread = _serve(monkeypatch, lambda: cfg)
    yield _client(port)
    _stop(httpd, thread)


def _serve(monkeypatch, loader):
    """按给定的取配置函数起一个服务，返回 (httpd, 端口, 线程)。"""
    monkeypatch.setattr(server, "_load_config", loader)
    server._health_cache["at"] = 0.0
    server._health_cache["payload"] = None
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _client(port):
    """发真实 HTTP 请求的闭包；路径会先做百分号编码（浏览器就是这么发的）。"""

    def call(path, method="GET", body=None):
        parts = urllib.parse.urlsplit(path)
        target = urllib.parse.urlunsplit((
            "", "",
            urllib.parse.quote(parts.path, safe="/"),
            urllib.parse.quote(parts.query, safe="=&"),
            "",
        ))
        url = "http://127.0.0.1:%d%s" % (port, target)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

    return call


def as_json(text):
    return json.loads(text)


# ------------------------------------------------------------------ health

def test_health_reports_environment(live, cfg):
    """health 要把版本、路径、盘符、磁盘一次说全（设计文档 §15 末段）。"""
    status, body, _ = live("/api/health")
    assert status == 200
    doc = as_json(body)
    assert "checks" in doc and doc["checks"]
    by_id = {c["id"]: c for c in doc["checks"]}

    for required in ("python", "pytest", "node", "repo", "workspace", "disk", "packs"):
        assert required in by_id, "health 少了 %s 这一项" % required
    assert by_id["python"]["value"].startswith(sys.version.split()[0])
    assert by_id["pytest"]["ok"] is True, "本机 pytest 应当可用"
    assert by_id["pytest"]["value"] and by_id["pytest"]["value"][0].isdigit()
    assert by_id["repo"]["ok"] is True
    assert by_id["repo"]["value"] == "可读"
    assert by_id["workspace"]["ok"] is True
    assert util.norm(cfg["sandbox_root"]) == util.norm(by_id["workspace"]["value"])
    assert "MB" in by_id["disk"]["value"] or "GB" in by_id["disk"]["value"]
    assert "pytest" in doc["checkers"]
    assert isinstance(doc["warnings"], list)


def test_health_fails_loudly_when_repo_missing(live, cfg, monkeypatch):
    """受测仓库读不到时 ok=False 并给中文警告，而不是假装健康。"""
    cfg["repo_root"] = os.path.join(cfg["sandbox_root"], "根本不存在的仓库")
    server._health_cache["at"] = 0.0
    server._health_cache["payload"] = None
    _status, body, _ = live("/api/health")
    doc = as_json(body)
    assert doc["ok"] is False
    by_id = {c["id"]: c for c in doc["checks"]}
    assert by_id["repo"]["ok"] is False
    assert any("repo_root" in w for w in doc["warnings"])


# -------------------------------------------------------------------- 题库

def test_tasks_endpoint_lists_fixture_packs(live):
    status, body, _ = live("/api/tasks")
    assert status == 200
    doc = as_json(body)
    ids = [t["id"] for t in doc["tasks"]]
    assert "TEST-01" in ids and "TEST-02" in ids
    assert doc["count"] == len(doc["tasks"])
    assert "pack_dir" not in doc["tasks"][0], "内部路径不该发给前端"


def test_tasks_filters_by_tier_and_tag(live):
    _status, body, _ = live("/api/tasks?tier=easy")
    assert [t["id"] for t in as_json(body)["tasks"]] == ["TEST-02"]
    _status, body, _ = live("/api/tasks?tag=联接复用")
    assert [t["id"] for t in as_json(body)["tasks"]] == ["TEST-02"]


def test_empty_packs_returns_empty_list_not_error(cfg, monkeypatch, tmp_path):
    """题库一个包都没有时，返回空列表 + 计数 0，不许报错。"""
    empty = tmp_path / "空题库"
    empty.mkdir()
    conf = dict(cfg)
    conf["packs_root"] = str(empty)
    httpd, port, thread = _serve(monkeypatch, lambda: conf)
    try:
        live = _client(port)
        status, body, _ = live("/api/tasks")
        assert status == 200
        assert as_json(body) == {"tasks": [], "count": 0}

        server._health_cache["at"] = 0.0
        server._health_cache["payload"] = None
        _status, body, _ = live("/api/health")
        health = as_json(body)
        assert any("任务包" in w for w in health["warnings"])
    finally:
        _stop(httpd, thread)


def test_task_detail_exposes_prompts(live):
    status, body, _ = live("/api/tasks/TEST-01")
    assert status == 200
    doc = as_json(body)
    assert doc["attempts"] == 2
    assert doc["allowed_paths"] == ["backend/miniapp/**"]
    assert [p["level"] for p in doc["prompts"]] == [1, 2]
    assert doc["wiring_note"], "接线说明要发给模型"
    assert "sandbox" in doc["wiring_note"]
    assert "Q:\\" not in doc["wiring_note"]


def test_task_detail_can_scope_unlocked_prompts_to_a_run(live, cfg):
    """同题多个会话时，提示词解锁级别必须跟随指定 run。"""
    runs.save_run(cfg, {
        "run_id": "TEST-01__target__20260101-000001", "task": "TEST-01", "model": "target",
        "attempt": 1, "status": "ready", "created_at": "2026-01-01T00:00:00",
        "revealed": False, "rounds": [],
    })
    runs.save_run(cfg, {
        "run_id": "TEST-01__newer__20260101-000002", "task": "TEST-01", "model": "newer",
        "attempt": 2, "status": "ready", "created_at": "2026-01-01T00:00:01",
        "revealed": False, "rounds": [],
    })

    status, body, _ = live("/api/tasks/TEST-01?run_id=TEST-01__target__20260101-000001")
    assert status == 200
    doc = as_json(body)
    assert [p["level"] for p in doc["prompts"]] == [1]
    assert doc["run"]["run_id"] == "TEST-01__target__20260101-000001"


def test_task_leaderboard_route_returns_entries(live):
    status, body, _ = live("/api/tasks/TEST-01/leaderboard")
    assert status == 200
    doc = as_json(body)
    assert doc["task"] == "TEST-01"
    assert isinstance(doc["entries"], list)


# ---------------------------------------------------------------- 错误信封

def test_unknown_api_returns_stable_code(live):
    status, body, _ = live("/api/根本没有这个接口")
    assert status == 404
    doc = as_json(body)
    assert doc["code"] == errors.E_NOT_FOUND
    assert "接口不存在" in doc["message"]


def test_wrong_method_returns_405(live):
    status, body, _ = live("/api/health", method="POST", body={})
    assert status == 405
    assert as_json(body)["code"] == errors.E_METHOD_NOT_ALLOWED


def test_unknown_task_returns_actionable_message(live):
    status, body, _ = live("/api/tasks/TEST-999")
    assert status == 404
    doc = as_json(body)
    assert doc["code"] == errors.E_TASK_NOT_FOUND
    assert "任务库" in doc["message"]


def test_bad_request_body_is_rejected(live):
    status, body, _ = live("/api/runs", method="POST", body={})
    assert status == 400
    assert as_json(body)["code"] == errors.E_BAD_REQUEST


def test_every_error_code_has_http_status():
    """错误码到 HTTP 状态的映射要齐全，前端才能稳定映射中文。"""
    for code in dir(errors):
        if code.startswith("E_"):
            assert code in errors.HTTP_STATUS, "%s 没有对应的 HTTP 状态" % code


# ---------------------------------------------------------------- 静态托管

def test_missing_static_returns_chinese_404_page(live):
    """console\\static 还没就位时给中文说明页，服务不崩。"""
    status, body, headers = live("/")
    assert status == 404
    assert headers["Content-Type"].startswith("text/html")
    assert "前端" in body and "index.html" in body
    assert "尚未就位" in body or "还没有就位" in body


def test_missing_static_asset_is_404_not_500(live):
    status, body, _ = live("/js/还不存在的文件.js")
    assert status == 404
    assert "<html" in body


def test_path_traversal_is_blocked(live):
    """目录穿越必须被挡住：拿不到 static 之外的文件内容。"""
    status, body, _ = live("/../../../../Windows/win.ini")
    assert status in (400, 404)
    # 404 说明页会把请求路径回显出来（方便定位），但绝不能是 win.ini 的真实内容
    assert "[fonts]" not in body and "[extensions]" not in body, \
        "穿越请求把系统文件内容返回了"
    assert "接口" in body or "非法" in body or "还没有就位" in body


def test_static_file_is_served_when_present(live, cfg):
    """静态目录里有文件时正常托管。"""
    os.makedirs(os.path.join(cfg["static_root"], "js"), exist_ok=True)
    with open(os.path.join(cfg["static_root"], "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html><body>评测台</body></html>")
    with open(os.path.join(cfg["static_root"], "js", "app.js"), "w", encoding="utf-8") as fh:
        fh.write("console.log('占位');")
    status, body, headers = live("/")
    assert status == 200 and "评测台" in body
    status, body, headers = live("/js/app.js")
    assert status == 200 and "app.js" not in body
    assert "javascript" in headers["Content-Type"]
    assert headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------- 记分板

def test_scoreboard_json_and_csv(live):
    status, body, headers = live("/api/scoreboard")
    assert status == 200
    board = as_json(body)
    assert "matrix" in board and "totals" in board
    assert "TEST-01" in board["tasks"]

    status, body, headers = live("/api/scoreboard?format=csv")
    assert status == 200
    assert headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers.get("Content-Disposition", "")
    text = body.lstrip("﻿")
    assert "任务,档位" in text
    assert "已揭晓" in text


# ---------------------------------------------------------------- 模型档案

def test_model_crud_roundtrip(cfg, monkeypatch, tmp_path):
    """模型档案增删改查。

    /api/models 会写 config.json，这里把它换到临时文件，绝不碰真实配置；
    取配置也改成每次重新读盘，否则改完立刻 GET 拿到的还是旧快照。
    """
    real_config = os.path.join(CONSOLE_DIR, "config.json")
    with open(real_config, "rb") as fh:
        original = fh.read()
    shadow = tmp_path / "config.json"
    shadow.write_bytes(original)
    isolated_config = util.read_json(str(shadow), default={})
    isolated_config["models"] = []
    util.write_json_atomic(str(shadow), isolated_config)
    monkeypatch.setattr(server.config, "CONFIG_PATH", str(shadow))

    def loader():
        fresh = server.config.load()          # 读影子 config.json
        for key, value in cfg.items():        # 路径仍指临时工作区
            if key not in ("models",):
                fresh[key] = value
        return fresh

    httpd, port, thread = _serve(monkeypatch, loader)
    live = _client(port)
    try:
        status, body, _ = live("/api/models", method="POST",
                               body={"id": "gpt-x", "protocol": "openai",
                                     "base_url": "http://127.0.0.1:1/v1", "model": "gpt-x",
                                     "key_masked": "sk-****1234",
                                     "api_mode": "responses"})
        assert status == 200
        assert as_json(body)["id"] == "gpt-x"
        assert as_json(body)["api_mode"] == "responses"

        _status, body, _ = live("/api/models")
        assert [m["id"] for m in as_json(body)["models"]] == ["gpt-x"]
        assert as_json(body)["models"][0]["api_mode"] == "responses"

        status, _body, _ = live("/api/models?id=gpt-x", method="DELETE")
        assert status == 200
        _status, body, _ = live("/api/models")
        assert as_json(body)["models"] == []

        # 真实配置一个字节都没动
        with open(real_config, "rb") as fh:
            assert fh.read() == original, "接口不该写真实的 config.json"
        assert util.read_json(str(shadow))["models"] == []
    finally:
        _stop(httpd, thread)


def test_invalid_model_protocol_is_rejected(live):
    status, body, _ = live("/api/models", method="POST",
                           body={"id": "x", "protocol": "不存在的协议"})
    assert status == 400
    assert as_json(body)["code"] == errors.E_MODEL_INVALID


def test_invalid_openai_api_mode_is_rejected(live):
    status, body, _ = live("/api/models", method="POST",
                           body={"id": "x", "protocol": "openai", "api_mode": "native"})
    assert status == 400
    assert as_json(body)["code"] == errors.E_MODEL_INVALID


def test_legacy_model_gets_chat_completions_default(cfg, monkeypatch, tmp_path):
    """旧 config 没有 api_mode 时，GET 也返回可解释的默认值。"""
    real_config = os.path.join(CONSOLE_DIR, "config.json")
    with open(real_config, "rb") as fh:
        original = fh.read()
    shadow = tmp_path / "config.json"
    shadow.write_bytes(original)
    monkeypatch.setattr(server.config, "CONFIG_PATH", str(shadow))
    raw = util.read_json(str(shadow), default={})
    raw["models"] = [{"id": "old", "protocol": "openai", "base_url": "", "model": "old"}]
    util.write_json_atomic(str(shadow), raw)

    def loader():
        fresh = server.config.load()
        for key, value in cfg.items():
            if key != "models":
                fresh[key] = value
        return fresh

    httpd, port, thread = _serve(monkeypatch, loader)
    live = _client(port)
    try:
        status, body, _ = live("/api/models")
        assert status == 200
        assert as_json(body)["models"][0]["api_mode"] == "chat_completions"
    finally:
        _stop(httpd, thread)
