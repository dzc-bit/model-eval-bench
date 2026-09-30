"""验收：文件夹沙箱生命周期与实体依赖目录安全。"""

from __future__ import annotations

import os

import pytest

from conftest import BACKEND_TASK, FRONTEND_TASK, make_run
from harness import packs, sandbox, util


def read(path):
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


@pytest.fixture
def prepared(cfg, log):
    """准备一份后端题的文件夹沙箱；用例结束后销毁。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "后端模型", run_id="TEST-01__后端模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)
    yield cfg, run, meta
    sandbox.destroy(cfg, run, log=lambda _message: None)


@pytest.fixture
def prepared_front(cfg, log):
    """准备一份前端题的文件夹沙箱（会复制 node_modules）。"""
    meta = packs.load_meta(cfg, FRONTEND_TASK)
    run = make_run(cfg, FRONTEND_TASK, "前端模型", run_id="TEST-02__前端模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)
    yield cfg, run, meta
    sandbox.destroy(cfg, run, log=lambda _message: None)


def test_prepare_produces_isolated_tree(prepared):
    """工作区只有注入后的代码，没有题包、历史或受测仓库文档。"""
    cfg, run, meta = prepared
    root = run["sandbox"]
    present = {util.rel_posix(p, root) for p in util.iter_files(root)}

    assert "backend/miniapp/pricing.py" in present
    assert os.path.isdir(os.path.join(root, ".git"))
    assert util.path_within(cfg["sandbox_root"], root)
    assert not any(rel.startswith("packs/") for rel in present)
    assert not any(rel.split("/")[0] in {"docs", ".reference", "运行产物"} for rel in present)
    assert run["drive"] == ""
    assert sandbox.verify_integrity(cfg, run, meta) == []


def test_prepare_applies_patches_in_order(prepared):
    """注入补丁按文件名顺序应用，三个端口都要落到工作区。"""
    _cfg, run, meta = prepared
    assert run["injected"] == 2
    pricing = read(os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py"))
    adapters = read(os.path.join(run["sandbox"], "backend", "miniapp", "adapters.py"))
    engine = read(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"))
    assert "return round(volume / close, 6)" in pricing
    assert "float(row[\"shares\"]) * 100.0" in adapters
    assert "return derived[\"market_cap\"] > MARKET_CAP_FLOOR" in engine
    assert len(packs.list_patches(meta)) == 2


def test_prepare_makes_single_baseline_commit(prepared):
    """工作区 git 只有一个 baseline 提交，没有可翻阅的历史。"""
    _cfg, run, _meta = prepared
    count = util.git(run["sandbox"], "rev-list", "--all", "--count").stdout.strip()
    assert count == "1"
    head = util.git(run["sandbox"], "rev-parse", "HEAD").stdout.strip()
    baseline = util.git(run["sandbox"], "rev-parse", "baseline").stdout.strip()
    assert head == baseline == run["baseline_commit"]


def test_prepare_registers_full_tree_manifest(prepared):
    """基线全树清单落盘，摘要与运行记录一致。"""
    _cfg, run, _meta = prepared
    manifest = util.read_json(os.path.join(run["run_dir"], "baseline_manifest.json"))
    assert isinstance(manifest, dict) and manifest
    assert "backend/miniapp/pricing.py" in manifest
    assert util.manifest_digest(manifest) == run["baseline_digest"]


def test_integrity_flags_missing_folder(prepared):
    """工作区目录被删除后，自检必须报告沙箱缺失。"""
    cfg, run, meta = prepared
    target = run["sandbox"]
    util.remove_tree(target)
    problems = sandbox.verify_integrity(cfg, run, meta)
    assert {p["kind"] for p in problems} == {"sandbox_missing"}


def test_prepare_failure_cleans_folder(monkeypatch, cfg):
    """准备失败时清理本次文件夹工作区，不留下半成品。"""
    run = make_run(cfg, BACKEND_TASK, "失败回滚", run_id="TEST-01__失败回滚__20260101-000000")
    util.ensure_dir(run["run_dir"])

    def fail_prepare(_cfg, _run, _meta, target, _log, **_kwargs):
        util.ensure_dir(os.path.join(target, "partial"))
        raise RuntimeError("simulated folder prepare failure")

    monkeypatch.setattr(sandbox, "_prepare_into_folder", fail_prepare)
    with pytest.raises(RuntimeError, match="simulated folder prepare failure"):
        sandbox.prepare(cfg, run, packs.load_meta(cfg, BACKEND_TASK), log=lambda _message: None)

    expected = os.path.join(cfg["sandbox_root"], util.sanitize_id(run["run_id"]))
    assert run["drive"] == run["sandbox"] == ""
    assert run["status"] == "error"
    assert not os.path.exists(expected)


def test_recovery_cleans_only_in_root_prepare_folder(cfg):
    """启动恢复清理内部工作区，不触碰沙箱根目录之外的路径。"""
    stale_id = "TEST-01__stale__20260101-000000"
    stale_dir = os.path.join(cfg["runs_root"], "TEST-01", "stale", "20260101-000000")
    stale_target = os.path.join(cfg["sandbox_root"], util.sanitize_id(stale_id))
    util.ensure_dir(stale_dir)
    util.ensure_dir(stale_target)
    stale_run = {
        "run_id": stale_id, "run_dir": stale_dir, "status": "preparing",
        "drive": "", "sandbox": util.norm(stale_target),
    }
    util.write_json_atomic(os.path.join(stale_dir, "run.json"), stale_run)

    foreign_id = "TEST-01__foreign__20260101-000000"
    foreign_dir = os.path.join(cfg["runs_root"], "TEST-01", "foreign", "20260101-000000")
    foreign_target = os.path.join(os.path.dirname(cfg["sandbox_root"]), "outside-sandbox")
    util.ensure_dir(foreign_dir)
    util.ensure_dir(foreign_target)
    foreign_run = {
        "run_id": foreign_id, "run_dir": foreign_dir, "status": "preparing",
        "drive": "", "sandbox": util.norm(foreign_target),
    }
    util.write_json_atomic(os.path.join(foreign_dir, "run.json"), foreign_run)

    assert sandbox.recover_interrupted_prepares(cfg, log=lambda _message: None) == 1
    assert not os.path.exists(stale_target)
    assert os.path.isdir(foreign_target)
    recovered = util.read_json(os.path.join(stale_dir, "run.json"), default={})
    assert recovered["status"] == "error"
    assert recovered["drive"] == recovered["sandbox"] == ""


def test_reset_changes_is_fast_and_keeps_integrity(prepared):
    """清空改动回到 baseline，且工作区完整性保持不变。"""
    cfg, run, meta = prepared
    target = os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py")
    util.write_text_atomic(target, "# 模型乱改了一行\n")
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "新文件.py"), "x = 1\n")
    util.write_text_atomic(os.path.join(run["sandbox"], "tests", "test_p2p.py"), "# 改了测试\n")

    info = sandbox.reset_changes(run["sandbox"])
    assert info["seconds"] < 2.0
    assert "volume / close" in read(target)
    assert not os.path.exists(os.path.join(run["sandbox"], "backend", "miniapp", "新文件.py"))
    assert "model" not in read(os.path.join(run["sandbox"], "tests", "test_p2p.py")).lower()
    assert sandbox.verify_integrity(cfg, run, meta) == []


def test_gitignore_protection_line_survives_reset(prepared_front):
    """清空改动不能删掉 node_modules 保护行。"""
    cfg, run, _meta = prepared_front
    ignore_path = os.path.join(run["sandbox"], ".gitignore")
    before = read(ignore_path)
    assert "node_modules/" in before

    sandbox.reset_changes(run["sandbox"], dependencies_source=sandbox.node_modules_baseline(run))
    assert read(ignore_path) == before
    assert os.path.isdir(os.path.join(run["sandbox"], "node_modules"))


def test_node_modules_is_entity_copy(prepared_front):
    """前端题把 node_modules 复制到沙箱，而不是建立外部联接。"""
    cfg, run, _meta = prepared_front
    copy = os.path.join(run["sandbox"], "node_modules")
    source = os.path.join(cfg["repo_root"], "node_modules")
    assert os.path.isdir(copy)
    assert not util.is_junction(copy)
    assert not os.path.islink(copy)
    assert util.path_within(run["sandbox"], copy)
    assert os.path.isfile(os.path.join(copy, "tiny-dep", "package.json"))
    assert util.manifest_digest(util.tree_manifest(copy, skip_dirs=())) == util.manifest_digest(
        util.tree_manifest(source, skip_dirs=()))


def test_entity_copy_survives_reset_and_rebuild(prepared_front):
    """实体依赖副本要活过清空和重建，受测仓库依赖内容不受影响。"""
    cfg, run, meta = prepared_front
    target = os.path.join(cfg["repo_root"], "node_modules")
    before = util.tree_manifest(target)

    dep_file = os.path.join(run["sandbox"], "node_modules", "tiny-dep", "package.json")
    original_dep = read(dep_file)
    util.write_text_atomic(dep_file, "{\"name\":\"tampered\"}\n")
    sandbox.reset_changes(run["sandbox"], dependencies_source=sandbox.node_modules_baseline(run))
    assert os.path.isdir(os.path.join(run["sandbox"], "node_modules"))
    assert read(dep_file) == original_dep
    sandbox.rebuild(cfg, run, meta, log=lambda _message: None)
    copy = os.path.join(run["sandbox"], "node_modules")
    assert run["drive"] == ""
    assert os.path.isdir(copy)
    assert not util.is_junction(copy)
    assert not os.path.islink(copy)
    assert util.manifest_digest(util.tree_manifest(target)) == util.manifest_digest(before)

    sandbox.destroy(cfg, run, log=lambda _message: None)
    assert not os.path.exists(copy)
    assert not os.path.exists(os.path.join(run["run_dir"], ".node_modules-baseline"))
    assert os.path.isfile(os.path.join(target, "tiny-dep", "package.json"))


def test_integrity_flags_external_node_modules_link(prepared_front, tmp_path):
    """依赖目录被替换为外部链接后，自检必须报出来。"""
    cfg, run, meta = prepared_front
    link = os.path.join(run["sandbox"], "node_modules")
    util.remove_tree(link)
    outside = os.path.join(str(tmp_path), "outside-node-modules")
    util.ensure_dir(outside)
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 环境不允许创建目录 symlink")
    problems = sandbox.verify_integrity(cfg, run, meta)
    assert any(p["kind"] == "node_modules_broken" for p in problems), problems
    sandbox.destroy(cfg, run, log=lambda _message: None)


def test_integrity_flags_modified_gitignore(prepared_front):
    """.gitignore 被改掉保护行后立即报告。"""
    cfg, run, meta = prepared_front
    ignore_path = os.path.join(run["sandbox"], ".gitignore")
    util.write_text_atomic(ignore_path, "*.log\n")
    problems = sandbox.verify_integrity(cfg, run, meta)
    assert any(p["kind"] == "gitignore_modified" for p in problems), problems
    sandbox.destroy(cfg, run, log=lambda _message: None)


def test_rebuild_drops_model_changes(prepared):
    """重建删除整个文件夹工作区，模型改动和未跟踪残留都消失。"""
    cfg, run, meta = prepared
    old_sandbox = run["sandbox"]
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"), "# 模型改的\n")
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "随手.log"), "垃圾\n")

    sandbox.rebuild(cfg, run, meta, log=lambda _message: None)
    assert run["sandbox"] == old_sandbox
    assert run["drive"] == ""
    assert "模型改的" not in read(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"))
    assert not os.path.exists(os.path.join(run["sandbox"], "backend", "miniapp", "随手.log"))
    assert "volume / close" in read(os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py"))
    sandbox.destroy(cfg, run, log=lambda _message: None)


def test_prepare_rejects_unsafe_task_id(cfg, log):
    """题号路径穿越字符必须被收敛，不能写到 sandbox_root 之外。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "穿越", run_id="../../ESCAPED")
    run["run_id"] = "../../ESCAPED"
    util.ensure_dir(run["run_dir"])
    try:
        sandbox.prepare(cfg, run, meta, log=log)
        assert util.path_within(cfg["sandbox_root"], run["sandbox"])
        assert ".." not in run["sandbox"]
        assert os.path.basename(run["sandbox"]) == "ESCAPED"
    finally:
        sandbox.destroy(cfg, run, log=lambda _message: None)
