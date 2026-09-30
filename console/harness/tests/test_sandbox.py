"""验收：沙箱生命周期与 junction 安全（设计文档 §4.3）。

硬规则逐条验：
    · 清空改动只用 `git reset --hard baseline && git clean -fd`（不带 -x）；
    · 删沙箱只走整树 rmtree，单独摘联接用 os.rmdir；
    · 每次校验前自检 junction 指向、subst 映射、.gitignore 保护行；
    · 前端题的 node_modules 是联接不是拷贝，全程不得损坏。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from conftest import BACKEND_TASK, FRONTEND_TASK, make_run
from harness import errors, packs, sandbox, util


def read(path):
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


@pytest.fixture
def prepared(cfg, log):
    """准备一份后端题的沙箱；用例结束后销毁（盘符池只有三个，必须归还）。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "后端模型", run_id="TEST-01__后端模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)
    yield cfg, run, meta
    sandbox.destroy(cfg, run, log=lambda m: None)


@pytest.fixture
def prepared_front(cfg, log):
    """准备一份前端题的沙箱（会建 node_modules 联接）。"""
    meta = packs.load_meta(cfg, FRONTEND_TASK)
    run = make_run(cfg, FRONTEND_TASK, "前端模型", run_id="TEST-02__前端模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)
    yield cfg, run, meta
    sandbox.destroy(cfg, run, log=lambda m: None)


# ------------------------------------------------------------------ 准备

def test_prepare_produces_isolated_tree(prepared):
    """沙箱里只有注入后的代码，没有 .git 历史、没有题包、没有受测仓库痕迹。"""
    _cfg, run, _meta = prepared
    root = run["sandbox"]
    present = {util.rel_posix(p, root) for p in util.iter_files(root)}

    assert "backend/miniapp/pricing.py" in present
    assert os.path.isdir(os.path.join(root, ".git"))
    assert not any(rel.startswith("packs/") for rel in present)
    assert not any(rel.split("/")[0] in {"docs", ".reference", "运行产物"} for rel in present)
    assert sandbox.verify_integrity(_cfg, run, _meta) == []


def test_prepare_applies_patches_in_order(prepared):
    """注入补丁按文件名顺序应用，三个端口都要落到沙箱里。"""
    _cfg, run, meta = prepared
    assert run["injected"] == 2, "应当应用两个补丁文件"
    pricing = read(os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py"))
    adapters = read(os.path.join(run["sandbox"], "backend", "miniapp", "adapters.py"))
    engine = read(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"))
    assert "return round(volume / close, 6)" in pricing
    assert "float(row[\"shares\"]) * 100.0" in adapters
    assert "return derived[\"market_cap\"] > MARKET_CAP_FLOOR" in engine
    assert len(packs.list_patches(meta)) == 2


def test_prepare_makes_single_baseline_commit(prepared):
    """沙箱 git 只有一个提交、一条 baseline 分支：没有历史可翻。"""
    _cfg, run, _meta = prepared
    count = util.git(run["sandbox"], "rev-list", "--all", "--count").stdout.strip()
    assert count == "1", "沙箱里只该有 1 个提交，实际 %s 个" % count
    head = util.git(run["sandbox"], "rev-parse", "HEAD").stdout.strip()
    baseline = util.git(run["sandbox"], "rev-parse", "baseline").stdout.strip()
    assert head == baseline == run["baseline_commit"]


def test_prepare_registers_full_tree_manifest(prepared):
    """基线全树清单要落盘，越界检测全靠它（不依赖 git status）。"""
    _cfg, run, _meta = prepared
    manifest = util.read_json(os.path.join(run["run_dir"], "baseline_manifest.json"))
    assert isinstance(manifest, dict) and manifest
    assert "backend/miniapp/pricing.py" in manifest
    assert util.manifest_digest(manifest) == run["baseline_digest"]


# ------------------------------------------------------------------ 盘符

def test_subst_points_at_this_sandbox(prepared):
    """subst 映射必须指向本沙箱，且 Q:\\ 能真的读到这个沙箱。"""
    _cfg, run, _meta = prepared
    drive = run["drive"]
    assert drive in _cfg["drive_pool"]
    assert util.norm(sandbox.resolve_drive(drive)) == util.norm(run["sandbox"])
    assert os.path.isfile(os.path.join(drive + "\\", "backend", "miniapp", "pricing.py"))


def test_integrity_flags_lost_subst(prepared):
    """盘符映射被外部撤掉 → 自检必须报出来（设计文档 §4.4 第 6 项）。"""
    cfg, run, meta = prepared
    sandbox.release_drive(run["drive"], expected_target=run["sandbox"])
    try:
        problems = sandbox.verify_integrity(cfg, run, meta)
        kinds = {p["kind"] for p in problems}
        assert "subst_missing" in kinds
    finally:
        sandbox.allocate_drive(cfg, run["sandbox"], run=run)


def test_release_drive_never_removes_a_different_mapping(monkeypatch, tmp_path):
    """用户把池内盘符指向其它位置时，释放必须因目标不符而拒绝。"""
    user_target = util.norm(str(tmp_path / "user-drive"))
    mappings = {"Q:": user_target}
    calls = []

    monkeypatch.setattr(sandbox, "list_subst", lambda: dict(mappings))

    def fake_run(args, timeout=30):
        calls.append(args)
        if args[-1] == "/D":
            mappings.pop(args[1].upper(), None)
        return SimpleNamespace(ok=True, stdout="", stderr="", tail=lambda _n: "")

    monkeypatch.setattr(util, "run_cmd", fake_run)
    assert sandbox.release_drive("Q:") is False
    assert sandbox.release_drive("Q:", expected_target=str(tmp_path / "other")) is False
    assert mappings["Q:"] == user_target
    assert calls == []
    assert sandbox.release_drive("Q:", expected_target=user_target) is True
    assert "Q:" not in mappings


def test_allocator_skips_user_subst_even_when_target_is_missing(monkeypatch, cfg, tmp_path):
    """盘符池里失效或外部映射仍属于用户，不因目录不存在就被回收。"""
    missing = util.norm(str(tmp_path / "missing"))
    external = util.norm(str(tmp_path / "external"))
    util.ensure_dir(external)
    mappings = {"Q:": missing, "R:": external}
    calls = []
    monkeypatch.setattr(sandbox, "list_subst", lambda: dict(mappings))

    def fake_run(args, timeout=30):
        calls.append(args)
        if len(args) == 3 and args[-1] != "/D":
            mappings[args[1].upper()] = util.norm(args[2])
        elif args[-1] == "/D":
            mappings.pop(args[1].upper(), None)
        return SimpleNamespace(ok=True, stdout="", stderr="", tail=lambda _n: "")

    monkeypatch.setattr(util, "run_cmd", fake_run)
    target = util.norm(str(tmp_path / "sandbox"))
    util.ensure_dir(target)
    drive = sandbox.allocate_drive(cfg, target, log=lambda _message: None)

    assert drive == "S:"
    assert mappings["Q:"] == missing
    assert mappings["R:"] == external
    assert [args[1].upper() for args in calls] == ["S:"]
    assert sandbox.release_drive(drive, expected_target=target)


def test_allocator_reuses_its_lease_instead_of_creating_another_mapping(
        monkeypatch, cfg, tmp_path):
    """同一 run 再次准备时沿用其有所有权标记的映射，不额外占一个盘符。"""
    target = util.norm(str(tmp_path / "sandbox"))
    run = make_run(cfg, BACKEND_TASK, "重复准备", run_id="TEST-01__重复准备__20260101-000000")
    util.ensure_dir(run["run_dir"])
    util.ensure_dir(target)
    run.update({"status": "ready", "drive": "Q:", "sandbox": target})
    util.write_json_atomic(os.path.join(run["run_dir"], "run.json"), run)
    sandbox._write_drive_lease(run, "Q:", target, "ready")
    mappings = {"Q:": target}
    calls = []
    monkeypatch.setattr(sandbox, "list_subst", lambda: dict(mappings))
    monkeypatch.setattr(util, "run_cmd", lambda *a, **k: calls.append(a))

    drive = sandbox.allocate_drive(
        cfg, target, reserved={"Q:": "another-run"}, run=run,
        log=lambda _message: None)

    assert drive == "Q:"
    assert mappings == {"Q:": target}
    assert calls == []


def test_prepare_failure_releases_only_its_mapping(monkeypatch, cfg):
    """盘符建立后准备失败时，应释放映射并清理本次沙箱。"""
    mappings = {}
    calls = []
    monkeypatch.setattr(sandbox, "list_subst", lambda: dict(mappings))

    def fake_run(args, timeout=30):
        calls.append(args)
        if args[-1] == "/D":
            mappings.pop(args[1].upper(), None)
        else:
            mappings[args[1].upper()] = util.norm(args[2])
        return SimpleNamespace(ok=True, stdout="", stderr="", tail=lambda _n: "")

    monkeypatch.setattr(util, "run_cmd", fake_run)
    run = make_run(cfg, BACKEND_TASK, "失败回滚", run_id="TEST-01__失败回滚__20260101-000000")
    util.ensure_dir(run["run_dir"])

    def fail_after_allocate(_cfg, current_run, _meta, target, reserved, wait_s, log):
        sandbox.allocate_drive(_cfg, target, reserved, wait_s, log, run=current_run)
        raise RuntimeError("simulated post-allocation failure")

    monkeypatch.setattr(sandbox, "_prepare_into", fail_after_allocate)
    with pytest.raises(RuntimeError, match="simulated post-allocation failure"):
        sandbox.prepare(cfg, run, packs.load_meta(cfg, BACKEND_TASK), log=lambda _message: None)

    assert mappings == {}
    assert len(calls) == 2
    assert calls[0][0] == "subst" and calls[1][-1] == "/D"
    assert run["drive"] == run["sandbox"] == ""
    assert run["status"] == "error"
    assert not os.path.exists(os.path.join(cfg["sandbox_root"], util.sanitize_id(run["run_id"])))
    assert not os.path.exists(sandbox._lease_path(run))


def test_recovery_cleans_only_matching_dead_prepare_lease(monkeypatch, cfg):
    """启动恢复只清理 run、lease、映射三者一致的死亡准备任务。"""
    stale_id = "TEST-01__stale__20260101-000000"
    stale_dir = os.path.join(cfg["runs_root"], "TEST-01", "stale", "20260101-000000")
    stale_target = os.path.join(cfg["sandbox_root"], util.sanitize_id(stale_id))
    util.ensure_dir(stale_dir)
    util.ensure_dir(stale_target)
    stale_run = {
        "run_id": stale_id, "run_dir": stale_dir, "status": "preparing",
        "drive": "Q:", "sandbox": util.norm(stale_target),
    }
    util.write_json_atomic(os.path.join(stale_dir, "run.json"), stale_run)
    sandbox._write_drive_lease(stale_run, "Q:", stale_target, "preparing")

    foreign_id = "TEST-01__foreign__20260101-000000"
    foreign_dir = os.path.join(cfg["runs_root"], "TEST-01", "foreign", "20260101-000000")
    foreign_target = os.path.join(cfg["sandbox_root"], util.sanitize_id(foreign_id))
    foreign_mapping = util.norm(os.path.join(cfg["sandbox_root"], "user-owned"))
    util.ensure_dir(foreign_dir)
    util.ensure_dir(foreign_target)
    foreign_run = {
        "run_id": foreign_id, "run_dir": foreign_dir, "status": "preparing",
        "drive": "R:", "sandbox": util.norm(foreign_target),
    }
    util.write_json_atomic(os.path.join(foreign_dir, "run.json"), foreign_run)
    sandbox._write_drive_lease(foreign_run, "R:", foreign_target, "preparing")

    mappings = {"Q:": util.norm(stale_target), "R:": foreign_mapping}
    calls = []
    monkeypatch.setattr(sandbox, "list_subst", lambda: dict(mappings))
    monkeypatch.setattr(sandbox, "_pid_is_alive", lambda _pid: False)

    def fake_run(args, timeout=30):
        calls.append(args)
        mappings.pop(args[1].upper(), None)
        return SimpleNamespace(ok=True, stdout="", stderr="", tail=lambda _n: "")

    monkeypatch.setattr(util, "run_cmd", fake_run)
    assert sandbox.recover_interrupted_prepares(cfg, log=lambda _message: None) == 1

    assert "Q:" not in mappings
    assert mappings["R:"] == foreign_mapping
    assert not os.path.exists(stale_target)
    assert os.path.isdir(foreign_target)
    assert calls == [["subst", "Q:", "/D"]]
    recovered = util.read_json(os.path.join(stale_dir, "run.json"), default={})
    assert recovered["status"] == "error"
    assert recovered["drive"] == recovered["sandbox"] == ""
    assert not os.path.exists(sandbox._lease_path(stale_run))


# ------------------------------------------------------------------ 清空

def test_reset_changes_is_fast_and_keeps_integrity(prepared):
    """清空改动 < 2 秒，回到 baseline，且联接/映射/保护行都还在。"""
    cfg, run, meta = prepared
    target = os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py")
    util.write_text_atomic(target, "# 模型乱改了一行\n")
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "新文件.py"), "x = 1\n")
    util.write_text_atomic(os.path.join(run["sandbox"], "tests", "test_p2p.py"), "# 改了测试\n")

    info = sandbox.reset_changes(run["sandbox"])
    assert info["seconds"] < 2.0, "清空改动用了 %.2fs，超出设计文档的 2 秒预算" % info["seconds"]
    assert "volume / close" in read(target), "应该回到注入后的基线"
    assert not os.path.exists(os.path.join(run["sandbox"], "backend", "miniapp", "新文件.py"))
    assert "model" not in read(os.path.join(run["sandbox"], "tests", "test_p2p.py")).lower()
    assert sandbox.verify_integrity(cfg, run, meta) == []


def test_gitignore_protection_line_survives_reset(prepared_front):
    """沙箱 .gitignore 必须原样保留 node_modules/ 行，否则 clean 会把联接当垃圾删掉。"""
    _cfg, run, _meta = prepared_front
    ignore_path = os.path.join(run["sandbox"], ".gitignore")
    before = read(ignore_path)
    assert "node_modules/" in before

    sandbox.reset_changes(run["sandbox"])
    assert read(ignore_path) == before
    assert util.is_junction(os.path.join(run["sandbox"], "node_modules"))


# ---------------------------------------------------------------- 联接

def test_node_modules_is_junction_not_copy(prepared_front):
    """前端题用联接复用受测仓库的 node_modules，不是拷贝。"""
    _cfg, run, _meta = prepared_front
    link = os.path.join(run["sandbox"], "node_modules")
    assert util.is_junction(link), "node_modules 应该是联接"
    assert util.norm(util.junction_target(link)) == util.norm(
        os.path.join(_cfg["repo_root"], "node_modules"))
    # 联接里能看到真实内容（夹具里放了一个假依赖），但没有把内容复制进沙箱
    assert os.path.isfile(os.path.join(link, "tiny-dep", "package.json"))
    assert not os.path.islink(os.path.join(link, "tiny-dep"))


def test_junction_survives_reset_and_rebuild(prepared_front):
    """联接要活过「校验 → 清空 → 重建」全程，且目标目录内容不受影响。"""
    cfg, run, meta = prepared_front
    target = os.path.join(cfg["repo_root"], "node_modules")
    before = util.tree_manifest(target)

    sandbox.reset_changes(run["sandbox"])
    assert util.is_junction(os.path.join(run["sandbox"], "node_modules"))

    sandbox.rebuild(cfg, run, meta, log=lambda m: None)
    link = os.path.join(run["sandbox"], "node_modules")
    assert util.is_junction(link)
    assert util.norm(util.junction_target(link)) == util.norm(target)
    assert util.manifest_digest(util.tree_manifest(target)) == util.manifest_digest(before), \
        "重建沙箱绝不能动到受测仓库的 node_modules"

    sandbox.destroy(cfg, run, log=lambda m: None)
    assert not os.path.exists(link), "销毁沙箱后联接应被摘掉"
    assert os.path.isfile(os.path.join(target, "tiny-dep", "package.json")), \
        "受测仓库的 node_modules 实体必须毫发无损"


def test_integrity_flags_broken_junction(prepared_front):
    """联接被换成真实目录或指向别处 → 自检必须报出来。"""
    cfg, run, meta = prepared_front
    link = os.path.join(run["sandbox"], "node_modules")
    util.remove_junction(link)
    util.ensure_dir(link)
    util.write_text_atomic(os.path.join(link, "偷梁换柱.txt"), "假的\n")
    problems = sandbox.verify_integrity(cfg, run, meta)
    assert any(p["kind"] == "node_modules_broken" for p in problems), problems
    sandbox.destroy(cfg, run, log=lambda m: None)


def test_integrity_flags_modified_gitignore(prepared_front):
    """.gitignore 被改掉保护行 → 立刻中止本轮校验。"""
    cfg, run, meta = prepared_front
    ignore_path = os.path.join(run["sandbox"], ".gitignore")
    util.write_text_atomic(ignore_path, "*.log\n")
    problems = sandbox.verify_integrity(cfg, run, meta)
    assert any(p["kind"] == "gitignore_modified" for p in problems), problems
    sandbox.destroy(cfg, run, log=lambda m: None)


# ---------------------------------------------------------------- 重建

def test_rebuild_drops_model_changes(prepared):
    """重建 = 释放盘符 → 整树删除 → 重做，模型改的东西全没了。"""
    cfg, run, meta = prepared
    old_drive, old_sandbox = run["drive"], run["sandbox"]
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"),
                           "# 模型改的\n")
    # 放一个 git 看不见的文件：重建是整树删除，它也必须一起没
    util.write_text_atomic(os.path.join(run["sandbox"], "backend", "miniapp", "随手.log"), "垃圾\n")

    sandbox.rebuild(cfg, run, meta, log=lambda m: None)

    # 重建后沙箱仍是同一个 run_id 对应的目录（盘符也跟着回来）
    assert run["sandbox"] == old_sandbox
    assert run["drive"] == old_drive
    assert sandbox.resolve_drive(run["drive"]) == util.norm(run["sandbox"])
    # 模型改的文件回到基线内容，git 看不见的残留也一并清掉
    assert "模型改的" not in read(os.path.join(run["sandbox"], "backend", "miniapp", "engine.py"))
    assert not os.path.exists(os.path.join(run["sandbox"], "backend", "miniapp", "随手.log"))
    assert "volume / close" in read(os.path.join(run["sandbox"], "backend", "miniapp", "pricing.py"))
    sandbox.destroy(cfg, run, log=lambda m: None)


def test_drive_pool_exhaustion_is_reported(cfg, log, tmp_path):
    """盘符池用尽要给可操作的中文错误，而不是崩。

    不能假定池子三格全空：外部盲测/校准沙箱可能正占着其中一格。
    所以先把当下还空闲的盘符都占掉，再确认下一次准备必报「盘符池用尽」。
    """
    meta = packs.load_meta(cfg, BACKEND_TASK)
    occupied = []
    try:
        for index in range(len(cfg["drive_pool"])):
            holder_dir = os.path.join(str(tmp_path), "hold", str(index))
            util.ensure_dir(holder_dir)
            try:
                drive = sandbox.allocate_drive(cfg, holder_dir, log=lambda m: None)
                occupied.append((drive, holder_dir))
            except errors.HarnessError:
                break          # 池子本来就快满了，无妨，后面照样能验报错
        # 此时池子必然一个空位都不剩，做一次真实准备必须给中文错误而不是崩
        crowded = make_run(cfg, BACKEND_TASK, "抢盘", run_id="TEST-01__抢盘__20260101-000000")
        util.ensure_dir(crowded["run_dir"])
        with pytest.raises(errors.HarnessError) as excinfo:
            sandbox.prepare(cfg, crowded, meta, log=log)
        assert excinfo.value.code == errors.E_DRIVE_UNAVAILABLE
        assert "盘符池" in excinfo.value.message
        assert not crowded.get("sandbox"), "prepare 失败时不该把半成品沙箱登记成可用"
        assert not os.path.exists(os.path.join(cfg["sandbox_root"],
                                               util.sanitize_id(crowded["run_id"]))), \
            "prepare 失败后要把刚铺的半成品沙箱清干净"
    finally:
        for drive, target in occupied:
            sandbox.release_drive(drive, expected_target=target)


def test_prepare_rejects_unsafe_task_id(cfg, log):
    """题号里的路径穿越字符必须被收敛掉，不能写到 sandbox_root 之外。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "穿越", run_id="../../ESCAPED")
    run["run_id"] = "../../ESCAPED"
    util.ensure_dir(run["run_dir"])
    try:
        sandbox.prepare(cfg, run, meta, log=log)
        # 沙箱必须落在 sandbox_root 之内，".." 一个都不能带到路径里
        assert util.path_within(cfg["sandbox_root"], run["sandbox"])
        assert ".." not in run["sandbox"]
        assert os.path.basename(run["sandbox"]) == "ESCAPED"
    finally:
        sandbox.destroy(cfg, run, log=lambda m: None)
