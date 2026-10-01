"""验收：第 4 层隔离 + 分组部分分（设计文档 §4.2 / §4.4 / §5）。

全链路走一遍：准备沙箱 → 模拟模型改动 → 分组评分 → 清空 → 重建。
三个分档（16.7 / 33.3 / 100）与两处红线（p2p 回归判 0、越界作弊判 0）都在这里钉死。
"""

from __future__ import annotations

import os

import pytest

from conftest import (BACKEND_TASK, FRONTEND_TASK, MODEL_FULL_FIX, MODEL_PARTIAL_FIX,
                      make_run, write_in_sandbox)
from harness import grade, packs, report, sandbox, util

GROUP_IDS = ["turnover_exit", "adapter_exit", "engine_exit", "market_cap_exit", "coherence"]


def read(path):
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


@pytest.fixture
def bench(cfg, log):
    """一台"评分台"：准备沙箱，然后反复用不同改动跑分。"""
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "评分模型", run_id="TEST-01__评分模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)

    def grade_with(files=None):
        """先清空改动，再写入 files，然后跑一轮评分。"""
        sandbox.reset_changes(run["sandbox"])
        for rel, text in (files or {}).items():
            write_in_sandbox(run["sandbox"], rel, text)
        return grade.run_grade(cfg, run, meta, log=log)

    yield cfg, run, meta, grade_with
    sandbox.destroy(cfg, run, log=log)


def group_map(result):
    return {g["id"]: g for g in result["groups"]}


# ------------------------------------------------------------------ 三档分数

def test_baseline_scores_only_the_untouched_port(bench):
    """什么都不改：只有市值那一组是绿的 → 100 × 1/6 = 16.7。"""
    _cfg, _run, _meta, grade_with = bench
    result = grade_with()
    groups = group_map(result)

    assert result["score"] == pytest.approx(16.7, abs=0.1), \
        "期望 16.7，实际 %s（通过组：%s）" % (
            result["score"], [g["id"] for g in result["groups"] if g["passed"]])
    assert result["total_weight"] == 6.0
    assert result["passed_weight"] == 1.0
    assert groups["market_cap_exit"]["passed"] is True
    for gid in ("turnover_exit", "adapter_exit", "engine_exit", "coherence"):
        assert groups[gid]["passed"] is False, "%s 本轮应当是红的" % gid
    assert not result["p2p_broken"]
    assert result["violations"] == []


def test_partial_fix_scores_thirds(bench):
    """只修 pricing：换手率口径那组转绿，其余仍红 → 100 × 2/6 = 33.3。"""
    _cfg, _run, _meta, grade_with = bench
    result = grade_with({MODEL_PARTIAL_FIX[0]: MODEL_PARTIAL_FIX[1]})
    groups = group_map(result)

    assert result["score"] == pytest.approx(33.3, abs=0.1), \
        "期望 33.3，实际 %s（通过组：%s）" % (
            result["score"], [g["id"] for g in result["groups"] if g["passed"]])
    assert groups["turnover_exit"]["passed"] is True
    assert groups["market_cap_exit"]["passed"] is True
    assert groups["adapter_exit"]["passed"] is False
    assert groups["engine_exit"]["passed"] is False
    assert groups["coherence"]["passed"] is False


def test_full_fix_scores_hundred(bench):
    """三处全修 → 6/6 = 100 分。"""
    _cfg, _run, _meta, grade_with = bench
    result = grade_with(MODEL_FULL_FIX)
    groups = group_map(result)

    assert result["score"] == pytest.approx(100.0, abs=0.1), \
        "期望 100，实际 %s（通过组：%s）" % (
            result["score"], [g["id"] for g in result["groups"] if g["passed"]])
    assert result["passed"] is True
    assert all(groups[gid]["passed"] for gid in GROUP_IDS)
    assert result["violations"] == []
    assert not result["similarity"]["flagged"], \
        "模型自己的写法与参考解差得够远，不该被标成抄历史：%s" % result["similarity"]


def test_model_fix_is_not_penalised_for_style(bench):
    """换一种正确写法同样满分：评分只看事实成立与否，不看像不像参考解。"""
    _cfg, _run, _meta, grade_with = bench
    alternative = dict(MODEL_FULL_FIX)
    engine_src = alternative["backend/miniapp/engine.py"]
    engine_src = engine_src.replace(
        "    turnover_ok = derived['turnover'] > TURNOVER_FLOOR\n"
        "    cap_ok = derived['market_cap'] > MARKET_CAP_FLOOR\n"
        "    return bool(turnover_ok and cap_ok)\n",
        "    return all([\n"
        "        derived['turnover'] > TURNOVER_FLOOR,\n"
        "        derived['market_cap'] > MARKET_CAP_FLOOR,\n"
        "    ])\n",
    )
    alternative["backend/miniapp/engine.py"] = engine_src
    assert grade_with(alternative)["score"] == pytest.approx(100.0, abs=0.1)


def test_zero_weight_pack_never_mints_a_pass():
    """权重合计为 0 的题包是坏题包：只能记 0 分，不能因为「全绿」给满分。

    计分口径一旦在这里放开，一个把 weight 全写成 0（或漏写 scored 组）的题包就会
    对所有模型报 100 分，而门禁与排行榜都看不出异常。
    """
    all_green = {"groups": [
        {"id": "g1", "weight": 0.0, "passed": True},
        {"id": "g2", "weight": 0.0, "passed": True},
    ], "regressions": []}
    assert grade.compute_score(all_green)["score"] == 0.0
    assert grade.compute_score({"groups": [], "regressions": []})["score"] == 0.0


# ------------------------------------------------------------------ 回归红线

def test_p2p_regression_zeroes_the_round(bench):
    """破坏了注入前就绿的用例 → 整轮 0 分，并把回归清单单列。"""
    _cfg, _run, _meta, grade_with = bench
    files = dict(MODEL_FULL_FIX)
    # 为了"顺手统一一下"把市值也改了 —— 直接破坏既有行为
    files["backend/miniapp/pricing.py"] = files["backend/miniapp/pricing.py"].replace(
        "    return round(close * shares / 1e8, 6)",
        "    return round(close * shares / 1e7, 6)",
    )
    result = grade_with(files)

    assert result["p2p_broken"] is True
    assert result["score"] == 0.0
    assert result["raw_score"] > 0, "分组部分分仍应算出来，便于报告解释"
    node_ids = [r["node_id"] for r in result["regressions"]]
    assert any("test_market_cap_stable" in n for n in node_ids), node_ids
    assert result["invalidated"] is True
    assert "既有通过用例" in result["invalid_reason"]


# ------------------------------------------------------------------ 越界红线

def test_tampering_with_tests_is_inert_and_recorded(bench):
    """改测试、塞 conftest、改 pytest 配置：在评分树里全部无效，并记进 violations。"""
    _cfg, run, _meta, grade_with = bench
    files = dict(MODEL_FULL_FIX)
    files["tests/test_pricing_hidden.py"] = "# 偷偷放水：全部通过\n\ndef test_dummy():\n    pass\n"
    files["conftest.py"] = "def pytest_collection_modifyitems(items):\n    pass\n"
    files["pyproject.toml"] = "[tool.pytest.ini_options]\naddopts = '--no-header'\n"
    result = grade_with(files)

    # 越界：这三处都不在 allowed_paths 里
    touched = {v["path"] for v in result["violations"]}
    assert "conftest.py" in touched, result["violations"]
    assert "pyproject.toml" in touched, result["violations"]
    assert any("test_pricing_hidden" in p for p in touched), result["violations"]

    # 判 0，但原始分留着：报告里要能看出"改测试一分没多拿"
    assert result["score"] == 0.0
    assert result["raw_score"] == pytest.approx(100.0, abs=0.1)
    assert result["invalidated"] is True
    assert "越界" in result["invalid_reason"]

    # 评分树里模型的这些文件压根没进去
    grade_dir = result["grade_dir"]
    assert not os.path.exists(os.path.join(grade_dir, "conftest.py"))
    assert read(os.path.join(grade_dir, "pyproject.toml")).find("no-header") < 0
    assert "偷偷放水" not in read(os.path.join(grade_dir, "hidden", "tests_hidden",
                                             "test_pricing_hidden.py"))


def test_out_of_bounds_comment_only_is_downgraded():
    """越界文件只改注释 → 记提示不作废；碰了一行代码就还是硬违规。"""
    changes = {"added": [], "modified": ["backend/miniapp/models.py"], "removed": [],
               "changed": ["backend/miniapp/models.py"]}
    allowed = ["backend/miniapp/pricing.py"]
    comment_diff = (
        "--- a/backend/miniapp/models.py\n+++ b/backend/miniapp/models.py\n"
        "@@ -1,2 +1,3 @@\n # 原说明\n-# 旧措辞\n+# 新措辞\n+# 再补一行说明\n"
    )
    hard, soft = grade._classify_violations({}, changes, allowed, [], comment_diff)
    assert hard == [], hard
    assert len(soft) == 1 and soft[0]["reason"].startswith("越界但只改了注释")

    code_diff = (
        "--- a/backend/miniapp/models.py\n+++ b/backend/miniapp/models.py\n"
        "@@ -1,2 +1,3 @@\n # 原说明\n-# 旧措辞\n+limit = 2\n"
    )
    hard2, soft2 = grade._classify_violations({}, changes, allowed, [], code_diff)
    assert [v["path"] for v in hard2] == ["backend/miniapp/models.py"]
    assert soft2 == []


def test_cheating_visible_tests_does_not_help(bench):
    """把可见测试改成永远通过，隐藏测试照样判红。"""
    _cfg, _run, _meta, grade_with = bench
    files = dict(MODEL_FULL_FIX)
    files["tests/test_p2p.py"] = (
        "def test_normalize_symbol_stable():\n    assert True\n\n\n"
        "def test_market_cap_stable():\n    assert True\n"
    )
    result = grade_with(files)
    assert result["score"] == 0.0                       # 越界直接判红
    assert not result["p2p_broken"]                     # 评分树里跑的是原始用例
    assert result["raw_score"] == pytest.approx(100.0, abs=0.1)


def test_out_of_scope_new_file_is_violation(bench):
    """在允许范围之外新建文件同样算越界。"""
    _cfg, _run, _meta, grade_with = bench
    files = dict(MODEL_FULL_FIX)
    files["backend/extra_helper.py"] = "# 越界的新文件\n"
    result = grade_with(files)
    added = [v for v in result["violations"] if v["path"] == "backend/extra_helper.py"]
    assert added and added[0]["change"] == "added"
    assert result["score"] == 0.0


def test_run_artifacts_are_noise_not_violation(bench):
    """模型跑测试留下的 __pycache__ / .log 属噪音，不该把整轮判成 0。"""
    _cfg, run, _meta, grade_with = bench
    os.makedirs(os.path.join(run["sandbox"], "backend", "miniapp", "__pycache__"), exist_ok=True)
    write_in_sandbox(run["sandbox"], "backend/miniapp/__pycache__/x.cpython-313.pyc", "\x00")
    write_in_sandbox(run["sandbox"], "pytest 输出.log", "随手存的日志\n")
    result = grade_with(MODEL_FULL_FIX)
    assert result["score"] == pytest.approx(100.0, abs=0.1)
    assert result["violations"] == []
    assert len(result["noise"]) >= 1


def test_diff_does_not_depend_on_git(bench):
    """diff 来自全树哈希，不是 git。

    阳性对照：写一个 .gitignore 已经忽略、但仍在允许范围内的文件。
    `git status` 对它闭嘴，全树比对照样把它算出来。
    """
    _cfg, run, _meta, grade_with = bench
    quiet = "backend/miniapp/临时.log"          # 基线 .gitignore 里有 *.log
    write_in_sandbox(run["sandbox"], quiet, "模型偷偷留下的东西\n")
    assert quiet not in util.git(run["sandbox"], "status", "--porcelain").stdout, \
        "阳性对照失效：git 竟然看得见这个文件，说明 .gitignore 没生效"

    result = grade_with(dict(MODEL_FULL_FIX, **{quiet: "模型偷偷留下的东西\n"}))
    assert quiet in result["diff"]["files"], \
        "全树哈希漏掉了 git 忽略的文件：%s" % result["diff"]["files"]
    assert result["diff"]["added_lines"] > 0
    # 它在 allowed_paths 内，算模型自己的改动，不越界、不影响判分
    assert result["score"] == pytest.approx(100.0, abs=0.1), "它不越界，不该影响判分"
    assert [v["path"] for v in result["violations"]] == []


def test_gitignore_tampering_aborts_the_round(bench):
    """模型把 .gitignore 改成 * 想让 git 闭嘴 → 自检当场中止，本轮 0 分。"""
    _cfg, run, _meta, grade_with = bench
    files = dict(MODEL_FULL_FIX)
    files[".gitignore"] = "*\n"
    result = grade_with(files)

    assert result["error"] == "sandbox_broken"
    assert result["score"] == 0.0
    assert result["invalidated"] is True
    assert any(p["kind"] == "gitignore_modified" for p in result["integrity"]), result["integrity"]
    assert "沙箱" in result["invalid_reason"]


# ------------------------------------------------------------------ 评分树

def test_grade_tree_is_assembled_from_pristine_skeleton(bench):
    """评分树 = 原始骨架 + allowed_paths 覆盖 + hidden；前端依赖使用实体副本。"""
    _cfg, _run, _meta, grade_with = bench
    result = grade_with(MODEL_FULL_FIX)
    grade_dir = result["grade_dir"]

    assert os.path.isfile(os.path.join(grade_dir, "tests", "test_p2p.py"))
    assert os.path.isfile(os.path.join(grade_dir, "scripts", "check_guard.py"))
    assert os.path.isfile(os.path.join(grade_dir, ".gitignore"))
    assert os.path.isfile(os.path.join(grade_dir, "package.json"))
    assert os.path.isfile(os.path.join(grade_dir, "backend", "miniapp", "pricing.py"))
    assert os.path.isfile(os.path.join(grade_dir, "hidden", "tests_hidden",
                                       "test_pricing_hidden.py"))
    # 后端题不该接 node_modules
    assert not os.path.exists(os.path.join(grade_dir, "node_modules"))
    # 沙箱里的 .git 不进评分树
    assert not os.path.exists(os.path.join(grade_dir, ".git"))
    # 题包目录不进评分树
    assert not os.path.exists(os.path.join(grade_dir, "packs"))


def test_grade_env_is_hermetic(bench):
    """统一环境注入：禁代理、禁字节码、锁死哈希种子、清掉外部干扰。"""
    _cfg, _run, _meta, grade_with = bench
    env = grade.build_env(_cfg, "D:/某个评分树")
    assert env["NO_PROXY"] == "*"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONHASHSEED"] == "0"
    for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        assert key not in env


def test_grade_env_does_not_export_server_secrets(bench, monkeypatch):
    """评分树里跑的是模型提交的代码：服务端环境变量里的密钥一律不能透传。

    旧实现整包 os.environ.copy()，等于把模型 API 密钥交给被测代码。
    """
    _cfg, _run, _meta, _grade_with = bench
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("CBCN_GATEWAY_TOKEN", "tok-secret-value")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = grade.build_env(_cfg, "D:/某个评分树")
    assert "OPENAI_API_KEY" not in env
    assert "CBCN_GATEWAY_TOKEN" not in env
    assert "sk-secret-value" not in " ".join(env.values())
    # 白名单不是「只减密钥」：跑 pytest/vitest 必需的系统变量必须还在
    assert env.get("PATH")
    assert env.get("SYSTEMROOT") or env.get("WINDIR") or env.get("TEMP")


def test_broken_sandbox_aborts_before_scoring(bench):
    """完整性自检不过就直接中止，不要拿一个坏环境去跑分。"""
    cfg, run, meta, _grade_with = bench
    util.remove_tree(run["sandbox"])
    result = grade.run_grade(cfg, run, meta, log=lambda m: None)
    assert result["score"] == 0.0
    assert result["error"] == "sandbox_broken"
    assert result["invalidated"] is True
    assert "沙箱" in result["invalid_reason"]


# ------------------------------------------------------------------ 前端题

def test_frontend_task_uses_entity_node_modules_and_node_guard(cfg, log):
    """前端题：沙箱与评分树都复制实体 node_modules，node 守卫能跑出分组。"""
    meta = packs.load_meta(cfg, FRONTEND_TASK)
    run = make_run(cfg, FRONTEND_TASK, "前端模型", run_id="TEST-02__前端模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    sandbox.prepare(cfg, run, meta, log=log)
    try:
        sandbox_deps = os.path.join(run["sandbox"], "node_modules")
        assert os.path.isdir(sandbox_deps)
        assert not util.is_junction(sandbox_deps)
        assert not os.path.islink(sandbox_deps)
        result = grade.run_grade(cfg, run, meta, log=log)
        groups = {g["id"]: g for g in result["groups"]}
        assert groups["panel_digits_exit"]["passed"] is False   # 注入把 toFixed 改成 0 位
        assert groups["panel_shape_exit"]["passed"] is True
        assert result["score"] == pytest.approx(50.0, abs=0.1)
        assert not result["p2p_broken"]

        copied = os.path.join(result["grade_dir"], "node_modules")
        assert os.path.isdir(copied), "评分树里应有实体 node_modules"
        assert not util.is_junction(copied)
        assert not os.path.islink(copied)
        assert os.path.isfile(os.path.join(copied, "tiny-dep", "package.json"))

        # 修好它
        util.write_text_atomic(
            os.path.join(run["sandbox"], "frontend", "src", "panel.js"),
            read(os.path.join(cfg["repo_root"], "frontend", "src", "panel.js")))
        fixed = grade.run_grade(cfg, run, meta, log=log)
        assert fixed["score"] == pytest.approx(100.0, abs=0.1)
    finally:
        sandbox.destroy(cfg, run, log=log)


# ------------------------------------------------------------------ 报告

def test_report_renders_red_to_green(bench):
    """报告要能说清"这轮比上轮好在哪"，并给出下一步建议。"""
    _cfg, run, meta, grade_with = bench
    first = grade_with()
    second = grade_with(MODEL_FULL_FIX)

    built = report.build(run, meta, first, previous=None)
    assert built["score"] == first["score"]
    assert built["summary"]["green"] + built["summary"]["red"] == len(GROUP_IDS)
    assert built["summary"]["red"] == 4, "首轮应当只有市值那一组是绿的"
    assert built["next_hint"]["action"] == report.ACTION_PROMOTE, "还有轮次，应建议进入下一轮"
    assert built["baseline_digest"] == run["baseline_digest"]
    assert not any(k.startswith("_") for k in built), "内部中转字段不该进报告"

    # 第二轮：对比首轮，红的组要转绿
    previous = report.build(run, meta, first)
    later = report.build(run, meta, second, previous=previous)
    assert later["next_hint"]["action"] == report.ACTION_COMPLETE
    assert set(later["comparison"]["turned_green"]) == {"turnover_exit", "adapter_exit",
                                                        "engine_exit", "coherence"}


def test_next_hint_points_at_violations(bench):
    """越界或回归时，主按钮必须是"先修沙箱"，而不是"进入下一轮"。"""
    _cfg, run, meta, grade_with = bench
    cheater = dict(MODEL_FULL_FIX)
    cheater["conftest.py"] = "x = 1\n"
    result = grade_with(cheater)
    built = report.build(run, meta, result)
    assert built["next_hint"]["action"] == report.ACTION_FIX
    assert built["next_hint"]["can_promote"] is False
    assert built["summary"]["groups"], "分组红绿仍要渲染出来"


def test_notes_markdown_is_chinese(bench):
    """归档用 notes.md 要是中文，且带上分组红绿与下一步。"""
    _cfg, run, meta, grade_with = bench
    built = report.build(run, meta, grade_with({MODEL_PARTIAL_FIX[0]: MODEL_PARTIAL_FIX[1]}))
    text = report.notes_markdown(run, meta, built)
    assert "得分" in text and "分组" in text and "下一步" in text
    assert "✅" in text or "⛔" in text
    assert "turnover_rate" not in text, "小结是给人看的，不该漏测试函数名当正文"
    assert len(text) > 100
