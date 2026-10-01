"""验收：真实题包布局的读侧契约（回归）。

这两个缺陷曾让整条流水线"假通过"——能准备、能校验，但结果全错：

1. 注入补丁在真实题包里是 `inject/patches/*.patch`（README §6 与出题工具
   `inject_edits.py` 都写这里），而 `packs.list_patches()` 只读 `<pack>/patches/`。
   于是真实题目的注入补丁**一个都没被应用**：沙箱里是干净骨架，题面却按"已注入"
   描述，`run["injected"]` 为 0，出题侧还以为题目正常。

2. `groups.json` 里的隐藏用例 ID 写成相对 overlay 层的 `tests_hidden/x.py::t`，
   但 pytest 的 cwd 是评分树根，真实路径是 `hidden/tests_hidden/x.py::t`。
   路径解析不了 → pytest 退出码 4、收集到 0 个用例 → 连 p2p 白名单也一起被判
   "报告里没有任何用例记录"，每轮都 `invalidated=true`、分数强制 0。

下面的用例锁住这两条读侧契约，避免再次退化。
"""

from __future__ import annotations

import os

import pytest

from harness import packs


# --------------------------------------------------------------------------
# 1 · 注入补丁目录：inject/patches 与 patches 都要认
# --------------------------------------------------------------------------

def _write_patch(path: str, body: str = "--- a/f\n+++ b/f\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


@pytest.fixture
def pack_dir(tmp_path):
    """一个只有 meta.json 的空题包目录。"""
    root = tmp_path / "T9-99"
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as fh:
        fh.write('{"id": "T9-99", "title": "布局探针", "tier": "medium"}')
    return str(root)


def test_list_patches_reads_inject_patches_layout(pack_dir):
    """真实题包布局 inject/patches/ 必须被读到（这是本轮修的主缺陷）。"""
    _write_patch(os.path.join(pack_dir, "inject", "patches", "0001-alpha.patch"))
    _write_patch(os.path.join(pack_dir, "inject", "patches", "0002-beta.patch"))

    meta = {"id": "T9-99", "pack_dir": pack_dir}
    found = packs.list_patches(meta)

    assert len(found) == 2, "inject/patches 下的补丁必须被列出，实际 %r" % found
    assert [os.path.basename(p) for p in found] == ["0001-alpha.patch", "0002-beta.patch"]
    assert packs.find_patches_dir(meta) == os.path.join(pack_dir, "inject", "patches")


def test_list_patches_still_reads_legacy_patches_layout(pack_dir):
    """设计文档 §8 / 旧 fixture 的 patches/ 布局不能被打破。"""
    _write_patch(os.path.join(pack_dir, "patches", "01-only.patch"))

    meta = {"id": "T9-99", "pack_dir": pack_dir}
    found = packs.list_patches(meta)

    assert [os.path.basename(p) for p in found] == ["01-only.patch"]


def test_list_patches_prefers_inject_dir_without_duplicating(pack_dir):
    """两个目录都在时以 inject/patches 为准，绝不合并（否则补丁会被应用两次）。"""
    _write_patch(os.path.join(pack_dir, "inject", "patches", "0001-new.patch"))
    _write_patch(os.path.join(pack_dir, "patches", "01-old.patch"))

    meta = {"id": "T9-99", "pack_dir": pack_dir}
    names = [os.path.basename(p) for p in packs.list_patches(meta)]

    assert names == ["0001-new.patch"], "只该取一份，实际 %r" % names


def test_list_patches_ignores_non_patch_files(pack_dir):
    """目录里的 README 之类不算补丁。"""
    _write_patch(os.path.join(pack_dir, "inject", "patches", "0001-a.patch"))
    _write_patch(os.path.join(pack_dir, "inject", "patches", "README.md"))

    meta = {"id": "T9-99", "pack_dir": pack_dir}
    names = [os.path.basename(p) for p in packs.list_patches(meta)]

    assert names == ["0001-a.patch"]


def test_list_patches_empty_when_no_dir(pack_dir):
    """一个补丁目录都没有时要返回空（而不是崩）。"""
    assert packs.list_patches({"id": "T9-99", "pack_dir": pack_dir}) == []
    assert packs.find_patches_dir({"id": "T9-99", "pack_dir": pack_dir}) == ""


# --------------------------------------------------------------------------
# 2 · 隐藏用例 ID 必须归一成"相对评分树根"的路径
# --------------------------------------------------------------------------

def _make_pack_with_groups(tmp_path, group_tests, hidden_rel="hidden/tests_hidden"):
    """造一个带 groups.json / p2p.json / 隐藏用例文件的题包。"""
    root = tmp_path / "T9-98"
    hidden_dir = os.path.join(root, *hidden_rel.split("/"))
    os.makedirs(hidden_dir, exist_ok=True)
    with open(os.path.join(hidden_dir, "test_probe.py"), "w", encoding="utf-8") as fh:
        fh.write("def test_probe():\n    assert True\n")

    groups_dir = os.path.join(root, "hidden")
    os.makedirs(groups_dir, exist_ok=True)
    import json
    with open(os.path.join(groups_dir, "groups.json"), "w", encoding="utf-8") as fh:
        json.dump({"groups": [{"id": "g1", "weight": 1, "tests": list(group_tests)}]}, fh)
    with open(os.path.join(root, "p2p.json"), "w", encoding="utf-8") as fh:
        json.dump({"tests": ["tests/test_kept.py::test_kept"]}, fh)

    meta = {
        "id": "T9-98", "pack_dir": str(root),
        "checks": [{"kind": "pytest", "hidden": hidden_rel,
                    "groups": "hidden/groups.json", "p2p": "p2p.json"}],
    }
    return meta


def test_hidden_node_ids_get_overlay_prefix(tmp_path):
    """`tests_hidden/x.py::t` 要补成 `hidden/tests_hidden/x.py::t`，否则 pytest 找不到文件。"""
    meta = _make_pack_with_groups(
        tmp_path, ["tests_hidden/test_probe.py::test_probe"])
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    assert hidden["overlay_rel"] == "hidden"
    assert hidden["groups"][0]["tests"] == [
        "hidden/tests_hidden/test_probe.py::test_probe"]


def test_hidden_node_id_not_double_prefixed(tmp_path):
    """题包已经写成全路径时不能补成 hidden/hidden/...。"""
    meta = _make_pack_with_groups(
        tmp_path, ["hidden/tests_hidden/test_probe.py::test_probe"])
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    assert hidden["groups"][0]["tests"] == [
        "hidden/tests_hidden/test_probe.py::test_probe"]


def test_vitest_hidden_node_ids_match_the_relocated_frontend_root(tmp_path):
    """Vitest hidden nodes use frontend-root paths after relocation into src/."""
    meta = _make_pack_with_groups(
        tmp_path, ["tests_hidden_fe/test_probe.test.ts::probe"],
        hidden_rel="hidden-fe/tests_hidden_fe")
    meta["checks"][0]["kind"] = "vitest"
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    assert hidden["overlay_rel"] == "hidden-fe"
    assert hidden["groups"][0]["tests"] == [
        "src/tests_hidden_fe/test_probe.test.ts::probe"]
    # 光有路径不够：搬运目的地必须和这个前缀一致，否则文件在树根、vitest 看不见
    assert hidden["overlay_dest_rel"] == "frontend/src"


def test_vitest_hidden_layer_is_staged_under_frontend_src(tmp_path):
    """隐藏前端用例必须真的落进 frontend/src/tests_hidden_fe/。

    曾经只补了 CLI 路径、没实现搬运：文件留在评分树根的 hidden-fe/ 下，
    vitest 报「No test files found」，四道前端题的 FE 组于是永远 0 分。
    """
    from harness import grade

    meta = _make_pack_with_groups(
        tmp_path, ["tests_hidden_fe/test_probe.test.ts::probe"],
        hidden_rel="hidden-fe/tests_hidden_fe")
    meta["checks"][0]["kind"] = "vitest"
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    grade_dir = str(tmp_path / "grade-tree")
    os.makedirs(os.path.join(grade_dir, "frontend", "src"), exist_ok=True)
    grade._overlay_hidden(grade_dir, hidden["overlay_src"], hidden["overlay_dest_rel"], lambda m: None)

    staged = os.path.join(grade_dir, "frontend", "src", "tests_hidden_fe", "test_probe.py")
    assert os.path.isfile(staged), "隐藏前端用例没有被搬进 frontend/src/"
    assert not os.path.isdir(os.path.join(grade_dir, "hidden-fe"))


def test_qualified_hidden_path_exists_on_disk(tmp_path):
    """归一后的路径必须真的能落到评分树里的文件上。"""
    meta = _make_pack_with_groups(
        tmp_path, ["tests_hidden/test_probe.py::test_probe"])
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    rel_file = hidden["groups"][0]["tests"][0].split("::")[0]
    tree_root = os.path.join(meta["pack_dir"], "hidden")     # 评分树里 hidden 层的位置
    assert os.path.isfile(os.path.join(tree_root, *rel_file.split("/")[1:]))


def test_p2p_node_ids_stay_relative_to_tree_root(tmp_path):
    """p2p 用例本来就相对评分树根写（tests/...），不该被补前缀。"""
    meta = _make_pack_with_groups(
        tmp_path, ["tests_hidden/test_probe.py::test_probe"])
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    assert hidden["p2p_tests"] == ["tests/test_kept.py::test_kept"]


def test_bare_case_names_are_left_alone(tmp_path):
    """命令型 checker 的裸用例名（没有路径部分）不补前缀。"""
    meta = _make_pack_with_groups(tmp_path, ["panel_digits", "panel_shape"])
    hidden = packs.load_hidden_for(meta, meta["checks"][0])

    assert hidden["groups"][0]["tests"] == ["panel_digits", "panel_shape"]


def test_qualify_node_id_helper_directly():
    """归一函数的边界：空前缀（p2p）、绝对路径、已带前缀、裸名。"""
    assert packs._qualify_node_id("tests/x.py::t", "") == "tests/x.py::t"
    assert packs._qualify_node_id("tests_hidden/x.py::t", "hidden") == "hidden/tests_hidden/x.py::t"
    assert packs._qualify_node_id("hidden/tests_hidden/x.py::t", "hidden") == "hidden/tests_hidden/x.py::t"
    assert packs._qualify_node_id("panel_digits", "hidden") == "panel_digits"
    assert packs._qualify_node_id("", "hidden") == ""
    assert packs._qualify_node_id("C:/abs/x.py::t", "hidden").startswith("C:/abs")


# --------------------------------------------------------------------------
# 3 · 真实题包（如果在本机）也要满足这两条契约
# --------------------------------------------------------------------------

REAL_TASK = "T1-01"


def _real_cfg():
    from harness import config
    try:
        cfg = config.load()
    except Exception:            # noqa: BLE001 - 配置缺失时跳过
        return None
    task_dir = os.path.join(cfg["packs_root"], "core", "tasks", REAL_TASK)
    return cfg if os.path.isdir(task_dir) else None


def test_real_pack_exposes_injection_patches():
    """真实 T1-01 的 3 个注入补丁必须能被 list_patches 读到。"""
    cfg = _real_cfg()
    if cfg is None:
        pytest.skip("本机没有真实题包，跳过")
    meta = packs.load_meta(cfg, REAL_TASK)
    names = [os.path.basename(p) for p in packs.list_patches(meta)]
    assert names, "真实题包的注入补丁读不到——注入会静默失效"
    assert all(n.endswith(".patch") for n in names)


def test_real_pack_hidden_ids_resolve_to_files():
    """真实题包归一后的隐藏用例路径必须逐个落在评分树内的文件上。"""
    cfg = _real_cfg()
    if cfg is None:
        pytest.skip("本机没有真实题包，跳过")
    meta = packs.load_meta(cfg, REAL_TASK)
    for spec in (meta.get("checks") or [{}]):
        hidden = packs.load_hidden_for(meta, spec)
        prefix = hidden["overlay_rel"]
        src = hidden["overlay_src"]          # 题包里的 hidden/ 整层
        for group in hidden["groups"]:
            for node_id in group["tests"]:
                rel = node_id.split("::")[0]
                # 评分树里是 <overlay_rel>/<overlay 内的相对路径>
                inner = rel.split("/", 1)[1] if "/" in rel else rel
                target = os.path.join(src, *inner.split("/"))
                assert os.path.isfile(target), (
                    "归一后的用例路径 %r 指向的 %s 不存在（pytest 会收集不到）"
                    % (node_id, target))


def test_king_tier_alias_keeps_three_attempts(tmp_path):
    """王者中文档位经任务包归一后仍保留三轮契约。"""
    from harness import packs

    assert packs.normalize_tier("王者") == "king"
    assert packs.normalize_tier("王者 T4") == "king"
    assert packs.TIER_ATTEMPTS["king"] == 3

    pack_dir = tmp_path / "T4-11"
    pack_dir.mkdir()
    meta_path = pack_dir / "meta.json"
    meta_path.write_text(
        '{"id":"T4-11","title":"王者验证","tier":"王者"}',
        encoding="utf-8",
    )
    summary = packs.task_summary({}, str(pack_dir))
    assert summary["tier"] == "king"
    assert summary["attempts"] == 3


def test_king_frontend_p2p_belongs_to_king_pack():
    """A multi-check king pack must not inherit the previous task's p2p owner."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    path = root / "packs" / "core" / "tasks" / "T4-11" / "p2p-fe.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["task"] == "T4-11"
    assert all(item.startswith("src/") and "::" in item for item in data["tests"])
