"""验收：第 1 层隔离——白名单快照（设计文档 §4.2）。

要点：
    · 只拷"跑测试必需"的目录，docs / .reference / 运行产物 / node_modules 一个都不许进；
    · 题包 meta 声明的 redactions 与 visible.prune 必须落到快照里；
    · 生成后全树 grep，命中受测仓库绝对路径即判打包失败。
"""

from __future__ import annotations

import os

import pytest

from conftest import BACKEND_TASK, NEVER_SNAPSHOTTED, read_pack_meta
from harness import errors, packs, snapshot, util


@pytest.fixture
def built(cfg, log):
    """生成一份快照骨架，返回 (meta, 目标目录, 生成信息)。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    dest = os.path.join(cfg["snapshot_cache"], "TEST-01")
    info = snapshot.build(cfg, meta, dest, log)
    return meta, dest, info


def read(path):
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


# ------------------------------------------------------------------ 白名单

def test_snapshot_keeps_only_whitelist(built):
    """跑测试必需的骨架必须在，题目答案与依赖绝不在。"""
    _meta, dest, _info = built
    present = {util.rel_posix(p, dest) for p in util.iter_files(dest)}

    for required in ("backend/miniapp/pricing.py", "backend/miniapp/engine.py",
                     "tests/test_pricing.py", "tests/test_p2p.py",
                     "scripts/check_guard.py", "pyproject.toml",
                     "package.json", ".gitignore", "README.md",
                     "frontend/src/panel.js", "frontend/vitest.config.ts"):
        assert required in present, "快照缺了 %s" % required

    assert not any(rel.startswith("packs/") for rel in present), "题包不能进沙箱"
    assert not any("fix.patch" in rel for rel in present), "参考解不能进沙箱"


def test_snapshot_excludes_docs_artifacts_and_node_modules(cfg, built):
    """docs / .reference / 运行产物 / node_modules / .git 一律不进快照。"""
    _meta, dest, _info = built
    present = {util.rel_posix(p, dest) for p in util.iter_files(dest)}
    for banned in NEVER_SNAPSHOTTED:
        leaked = [rel for rel in present if rel.split("/")[0] == banned]
        assert not leaked, "%s 不该出现在快照里，实际有：%s" % (banned, leaked)
        # 夹具里这些目录确实存在，排除不是因为"本来就没有"
        assert os.path.exists(os.path.join(cfg["repo_root"], *banned.split("/"))), \
            "夹具里 %s 应该存在，否则这条排除规则没验到东西" % banned


def test_snapshot_never_carries_answers(built):
    """参考解与隐藏测试都不在快照里——它们只在评分树与题包里。"""
    meta, dest, _info = built
    present = {util.rel_posix(p, dest) for p in util.iter_files(dest)}
    assert "hidden" not in {rel.split("/")[0] for rel in present}
    for rel in present:
        assert "fix.patch" not in rel and "groups.json" not in rel


# -------------------------------------------------------------------- 脱敏

def test_redactions_drop_answer_sections(built):
    """AGENTS.md 的 §9/§15/§18 与 CHANGELOG 的三个版本条目必须被删掉。"""
    _meta, dest, _info = built
    agents = read(os.path.join(dest, "AGENTS.md"))
    assert "派生指标只有一处定义" not in agents
    assert "换手率 = 成交量 / 流通股本" not in agents
    assert "否决：在 adapter 里就地重算换手率" not in agents
    # 不相关的段要留着，不能整篇抹掉
    assert "目录结构" in agents and "backend/miniapp/" in agents

    changelog = read(os.path.join(dest, "CHANGELOG.md"))
    assert "1.5.2" not in changelog
    assert "1.6.0" not in changelog
    assert "1.6.1" not in changelog
    assert "0.1.0" in changelog, "无关的版本条目应保留"


def test_prune_removes_naming_guard_test(built):
    """点名不变量的可见用例必须被裁掉，同文件其它用例留下。"""
    _meta, dest, info = built
    text = read(os.path.join(dest, "tests", "test_pricing.py"))
    assert "test_turnover_rate_is_volume_over_shares" not in text
    assert "流通股本" not in text
    assert "def test_normalize_symbol_strips_market_suffix" in text
    assert "def test_special_treated_detection" in text
    assert "tests/test_pricing.py" in info["pruned"]


def test_prune_entry_is_written_as_glob(cfg):
    """题包里那条 prune 写的是通配形态，验收要确认它能命中多个用例。"""
    meta_raw = read_pack_meta(cfg["packs_root"], BACKEND_TASK)
    assert meta_raw["visible"]["prune"] == ["tests/test_pricing.py::test_turnover_rate_*"]


# ---------------------------------------------------------------- 泄漏兜底

def test_leak_grep_blocks_packaging(cfg, log):
    """源码里写死受测仓库绝对路径 → 判打包失败，不许出这道题。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    readme = os.path.join(cfg["repo_root"], "README.md")
    original = read(readme)
    util.write_text_atomic(readme, original + "\n本地路径：D:\\New project 6\\backend\n")
    dest = os.path.join(cfg["snapshot_cache"], "LEAK")
    try:
        with pytest.raises(errors.HarnessError) as excinfo:
            snapshot.build(cfg, meta, dest, log)
        assert excinfo.value.code == errors.E_LEAK_DETECTED
        assert "受测仓库" in excinfo.value.message
    finally:
        util.write_text_atomic(readme, original)


def test_snapshot_is_cached_and_reused(cfg, logs, log):
    """同一份白名单下第二次直接命中缓存，不重复拷贝。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    first = snapshot.ensure_snapshot(cfg, meta, log)
    marker = util.read_json(os.path.join(first, "_snapshot.json"))
    assert marker["task"] == BACKEND_TASK and marker["file_count"] > 0
    logs.clear()
    second = snapshot.ensure_snapshot(cfg, meta, log)
    assert second == first
    assert any("命中骨架缓存" in line for line in logs)


def test_snapshot_leaves_source_repo_untouched(cfg, log):
    """受测仓库是只读的：跑完快照，它一个字节都不能变。"""
    before = util.tree_manifest(cfg["repo_root"])
    meta = packs.load_meta(cfg, BACKEND_TASK)
    snapshot.build(cfg, meta, os.path.join(cfg["snapshot_cache"], "只读检查"), log)
    after = util.tree_manifest(cfg["repo_root"])
    assert util.manifest_digest(before) == util.manifest_digest(after)
