"""验收：运行记录与统计（设计文档 §16）。

    · 每轮目录存齐 run.json / baseline_manifest.json / diff.patch / report.json / notes.md；
    · 记分板与排行榜读的是成绩台账（runs/_results/ledger.json），不是这些记录；
    · 校准排队不铺工作区，跑一次消耗一个。

台账本身的语义（结束/废弃、作废与无效轮、回填、删档案级联）另见 test_results_ledger.py。
"""

from __future__ import annotations

import math
import os
import threading

import pytest

from conftest import BACKEND_TASK, make_run, write_in_sandbox
from harness import calibrate, chat, config, errors, results, runs, sandbox, util


def read(path):
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


def make_rounds(passed: bool, score: float, attempts: int = 1) -> list:
    return [{"attempt": i, "score": score if passed or i == 1 else 0.0,
             "passed": bool(passed and i == 1), "invalidated": False,
             "graded_at": "2026-01-0%dT00:00:00" % i,
             "report": "round-%d.json" % i} for i in range(1, attempts + 1)]


def store_run(cfg, run_id, task, model, passed, score, attempts=1, revealed=False):
    """只造记录，不铺沙箱（统计口径的验证不需要真跑一遍）。"""
    run = {
        "run_id": run_id, "task": task, "model": model, "attempt": attempts,
        "attempts_allowed": 3, "status": "graded", "created_at": "2026-01-01T00:00:00",
        "round_started_at": "2026-01-01T00:00:00",
        "revealed": revealed, "calibration": False, "rounds": make_rounds(passed, score, attempts),
        "last_score": score, "last_passed": passed, "note": "", "drive": "", "sandbox": "",
        "baseline_commit": "", "baseline_digest": "deadbeef",
    }
    runs.save_run(cfg, run)
    return run


def ledger(cfg, task, model, *, score=100.0, passed=True, pass1=None,
           rounds=1, work=None, wall=None, run_id=""):
    """往台账写一条（= 一次「结束本轮」的结果）。"""
    return results.append_entry(cfg, results.make_entry(
        task, model, model, source_run_id=run_id,
        rounds=rounds, best_round=rounds, score=score, passed=passed,
        pass1=bool(passed) if pass1 is None else pass1,
        model_work_seconds=work, wall_seconds=wall,
        graded_at="2026-01-01T00:00:00",
    ))


def cell_of(cfg, task, model):
    board = runs.scoreboard(cfg)
    row = next(r for r in board["matrix"] if r["task"] == task)
    return row["cells"][model]


def install_providers(cfg, providers):
    """装一份受控的供应商配置，并按 config.load 同一条派生路径展开扁平档案。"""
    cfg["providers"] = providers
    cfg["models"] = config.expand_models(providers)


# ------------------------------------------------------------------ 归档

def test_reopen_voids_the_round_and_unlocks_the_chat(cfg):
    """误校验的补救：本轮分数作废、退回可对话，记分板不再计入它。"""
    run = make_run(cfg, model="重开模型")
    runs.save_run(cfg, run)
    stored = runs.get_run(cfg, run["run_id"])
    stored["status"] = "graded"
    stored["last_score"] = 83.3
    stored["last_passed"] = False
    stored["rounds"] = [{"attempt": 1, "score": 83.3, "passed": False}]
    runs.save_run(cfg, stored)

    out = runs.reopen(cfg, stored["run_id"])
    assert out["status"] == "ready"
    assert out["voided_rounds"] == 1

    after = runs.get_run(cfg, stored["run_id"])
    assert after["rounds"][0]["voided"] is True
    assert after["last_score"] is None
    assert runs.record_run_result(cfg, after) is None, "作废轮不能进台账"

    # 没校验过的轮次不需要重开；已揭晓参考解的不允许重开
    with pytest.raises(errors.HarnessError):
        runs.reopen(cfg, after["run_id"])
    after["status"] = "graded"
    after["revealed"] = True
    runs.save_run(cfg, after)
    with pytest.raises(errors.HarnessError) as exc:
        runs.reopen(cfg, after["run_id"])
    assert "参考解" in exc.value.message


def test_reopen_discards_a_stale_report_left_on_a_ready_run(cfg):
    """轮次记账是后补的：状态早已退回 ready、目录里还挂着旧报告的 run 也必须能作废。

    这种 run 在面板上永远显示一个 0 分，而「作废本轮」以前只认 graded，
    用户既删不掉分数也不能重跑，只能看着一条死记录。
    """
    run = make_run(cfg, model="旧报告模型")
    run["status"] = "ready"
    run["rounds"] = []
    run["last_score"] = None
    runs.save_run(cfg, run)
    report_path = os.path.join(run["run_dir"], "report.json")
    util.write_json_atomic(report_path, {"score": 0.0, "passed": False, "groups": []})
    assert runs.load_report(cfg, runs.get_run(cfg, run["run_id"])) is not None

    out = runs.reopen(cfg, run["run_id"])
    assert out["status"] == "ready" and out["report_archived"] is True
    assert not os.path.isfile(report_path), "旧报告还在，面板就还会显示那个 0 分"
    assert runs.load_report(cfg, runs.get_run(cfg, run["run_id"])) is None
    archived = [n for n in os.listdir(run["run_dir"]) if n.startswith("report-discarded-")]
    assert archived, "作废的证据要留在原地，不能直接删掉"


def test_release_sandbox_frees_workspace_but_keeps_the_record(cfg):
    """回收沙箱只删工作区目录：磁盘要还，成绩、报告、对话记录一个都不能少。

    工作台不再用这个口子（收尾统一走 finish_round），跑批与批量回收还在用。
    """
    run = make_run(cfg, model="回收模型")
    workspace = os.path.join(cfg["sandbox_root"], run["run_id"])
    util.ensure_dir(workspace)
    with open(os.path.join(workspace, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("print('模型改过的文件')\n")
    run["sandbox"] = workspace
    run["status"] = "graded"
    runs.save_run(cfg, run)

    out = runs.release_sandbox(cfg, run["run_id"])
    assert out["released"] is True
    assert not os.path.isdir(workspace)
    after = runs.get_run(cfg, run["run_id"])
    assert after["sandbox"] == ""
    assert os.path.isfile(os.path.join(after["run_dir"], "run.json"))

    # 没有沙箱时再点一次不该报错，也不能假装回收成功
    again = runs.release_sandbox(cfg, run["run_id"])
    assert again["released"] is False and again["message"]


def test_run_directory_holds_full_archive(cfg, log):
    """一轮跑完，记录目录里该有的都在。"""
    meta = __import__("harness.packs", fromlist=["packs"]).load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "归档模型", run_id="TEST-01__归档模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    runs.save_run(cfg, run)
    sandbox.prepare(cfg, run, meta, log=log)
    runs.save_run(cfg, run)
    try:
        write_in_sandbox(run["sandbox"], "backend/miniapp/pricing.py", "# 改了\n")
        runs.start_grade(cfg, run["run_id"])
        _wait_for_grade(cfg, run["run_id"])

        stored = runs.get_run(cfg, run["run_id"])
        record_dir = stored["run_dir"]
        assert os.path.isfile(os.path.join(record_dir, "run.json"))
        assert os.path.isfile(os.path.join(record_dir, "baseline_manifest.json"))
        assert os.path.isfile(os.path.join(record_dir, "report.json"))
        assert os.path.isfile(os.path.join(record_dir, "round-1.json"))
        assert os.path.isfile(os.path.join(record_dir, "diff.patch"))
        assert os.path.isfile(os.path.join(record_dir, "notes.md"))
        assert os.path.isfile(os.path.join(record_dir, "grade.log"))

        report = util.read_json(os.path.join(record_dir, "report.json"))
        assert report["run_id"] == run["run_id"]
        assert report["groups"], "报告要有分组红绿"
        assert "_diff_text" not in report, "内部字段不该落进 report.json"
        assert "backend/miniapp/pricing.py" in read(os.path.join(record_dir, "diff.patch"))
        assert "得分" in read(os.path.join(record_dir, "notes.md"))
    finally:
        sandbox.destroy(cfg, run, log=log)


def test_revealed_patch_is_persisted_into_the_run_record(cfg):
    """揭晓参考解要落盘：以前只在当次下发给前端，刷新就没了。"""
    from harness import packs
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "揭晓模型", run_id="TEST-01__揭晓模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    runs.save_run(cfg, run)

    res = runs.reveal(cfg, run["run_id"])

    assert res["stored_at"] == runs.REVEALED_PATCH_FILE
    path = os.path.join(run["run_dir"], runs.REVEALED_PATCH_FILE)
    assert os.path.isfile(path), "参考解正文必须写进运行记录目录"
    assert read(path) == res["patch"]
    # 复盘与报告窗都从运行记录读，不依赖「当次下发」
    assert runs.load_revealed_patch(cfg, runs.get_run(cfg, run["run_id"])) == res["patch"]
    view = runs.run_view(cfg, runs.get_run(cfg, run["run_id"]))
    assert view["revealed"] is True and view["revealed_patch"] == res["patch"]
    assert meta["id"] == BACKEND_TASK


def _wait_for_grade(cfg, run_id, timeout=240):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = runs.get_run(cfg, run_id)
        if run.get("status") in {"graded", "error"}:
            return run
        time.sleep(0.4)
    raise AssertionError("校验超时未完成")


def test_grade_is_async_and_blocks_double_start(cfg, log):
    """校验异步启动；重复触发不会重复跑。"""
    from harness import packs
    meta = packs.load_meta(cfg, BACKEND_TASK)
    run = make_run(cfg, BACKEND_TASK, "异步模型", run_id="TEST-01__异步模型__20260101-000000")
    util.ensure_dir(run["run_dir"])
    runs.save_run(cfg, run)
    sandbox.prepare(cfg, run, meta, log=log)
    runs.save_run(cfg, run)
    try:
        first = runs.start_grade(cfg, run["run_id"])
        assert first["status"] == "grading"
        with pytest.raises(errors.HarnessError) as excinfo:
            runs.start_grade(cfg, run["run_id"])
        assert excinfo.value.code == errors.E_RUN_BUSY
        _wait_for_grade(cfg, run["run_id"])
    finally:
        sandbox.destroy(cfg, run, log=log)


# ---------------------------------------------------------------- Wilson

def test_wilson_interval_math():
    """Wilson 区间要与手算一致，样本为 0 时退化成 [0,1]。

    区间本身已随「结束」语义收敛退出记分板（台账条目不等于通过的样本），
    校准队列仍在用它，所以这条公式继续锁着。
    """
    low, high = runs.wilson_interval(0, 10)
    assert low == 0.0 and 0.2 < high < 0.35
    low, high = runs.wilson_interval(10, 10)
    assert 0.65 < low < 1.0 and high == 1.0
    assert runs.wilson_interval(0, 0) == (0.0, 1.0)
    low, high = runs.wilson_interval(5, 10)
    assert low < 0.5 < high


def test_wilson_is_not_normal_approximation():
    """小样本下 Wilson 明显比正态近似宽，样本少时不能给窄区间。"""
    low, high = runs.wilson_interval(1, 1)
    assert 0.2 < low < 0.3, "1/1 的下界应当被压低到约 0.206"
    z = 1.959963985
    p = 1.0
    naive = p + z * math.sqrt(p * (1 - p) / 1)
    assert high == 1.0 and naive == 1.0


# ---------------------------------------------------------------- 记分板

def test_scoreboard_matrix_rows_and_columns(cfg):
    """行=任务、列=模型，单元格是台账条目的统计。"""
    ledger(cfg, BACKEND_TASK, "A", score=100.0, passed=True, pass1=True)
    ledger(cfg, BACKEND_TASK, "A", score=33.3, passed=False, pass1=False)
    ledger(cfg, BACKEND_TASK, "B", score=16.7, passed=False, pass1=False)

    board = runs.scoreboard(cfg)
    assert set(board["models"]) >= {"A", "B"}
    assert BACKEND_TASK in board["tasks"]
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    assert row["title"] == "派生指标口径不一致：换手率被算成了三份"
    assert row["tier"] == "medium"

    cell = row["cells"]["A"]
    assert cell["attempts"] == 2
    assert cell["pass1"] == 1
    assert cell["pass_rate"] == 0.5
    assert cell["best_score"] == 100.0
    assert cell["avg_score"] == pytest.approx(66.7, abs=0.1)
    assert "ci_low" not in cell and "ci_high" not in cell, "Wilson 已随旧口径废弃"
    assert board["totals"] == {"attempts": 3, "pass1": 1, "pass_rate": pytest.approx(0.333, abs=0.001)}


def test_scoreboard_cell_verdict_follows_the_best_round(cfg):
    """格子的第一眼结论看「最好一次有没有全绿」，不是 pass@1。

    T3-08 实测：第 1 轮 85.7 未过、第 2 轮 100 全绿，台账条目是
    ``score=100, passed=true, pass1=false, rounds=2``。旧前端拿 pass1 当主判定，
    格子上顶着一个 ✕「未通过」——台账里明明写着通过。
    """
    ledger(cfg, BACKEND_TASK, "迟到的模型", score=100.0, passed=True, pass1=False, rounds=2)

    cell = cell_of(cfg, BACKEND_TASK, "迟到的模型")
    assert cell["best_passed"] is True, "格子主判定必须来自代表条目"
    assert cell["pass1"] == 0 and cell["attempts"] == 1, "pass@1 仍然是独立口径（副标）"
    assert cell["best_score"] == 100.0
    assert cell["best_rounds"] == 2, "要能说出「最好一次在第 2 轮」"


def test_scoreboard_cell_verdict_false_when_nothing_passed(cfg):
    """两次都没做对：best_passed 为假，格子的 ✕ 才是真的。"""
    ledger(cfg, BACKEND_TASK, "没做对的模型", score=83.3, passed=False, pass1=False, rounds=2)
    cell = cell_of(cfg, BACKEND_TASK, "没做对的模型")
    assert cell["best_passed"] is False
    assert cell["best_score"] == 83.3


def test_ledger_lookup_by_source_run(cfg):
    """跑批靠「按 run_id 取台账条目」落定：结束本轮之后记录没了，只有台账认得出它。"""
    entry = ledger(cfg, BACKEND_TASK, "跑批模型", score=90.0, passed=True, run_id="R-1")
    assert results.find_by_source_run(cfg, "R-1")["entry_id"] == entry["entry_id"]
    assert results.find_by_source_run(cfg, "R-不存在") is None
    assert results.find_by_source_run(cfg, "") is None


def test_scoreboard_only_counts_ended_runs(cfg):
    store_run(cfg, "TEST-01__真跑模型__20260101-000001", BACKEND_TASK, "真跑模型", True, 100.0)
    never = store_run(cfg, "TEST-01__没结束__20260101-000002", BACKEND_TASK, "真跑模型", False, 10.0)
    never["rounds"] = []             # 建了记录但一次校验都没跑过
    never["last_score"] = None
    runs.save_run(cfg, never)

    assert "真跑模型" not in runs.scoreboard(cfg)["models"], \
        "记录还在、没点结束，就连列都不该有"
    assert runs.scoreboard(cfg)["totals"]["attempts"] == 0

    out = runs.backfill_ledger(cfg)   # 回填把在册成绩按新口径写成条目
    assert out["added"] == 1 and out["skipped"] == 1
    assert cell_of(cfg, BACKEND_TASK, "真跑模型")["attempts"] == 1


def test_scoreboard_csv_drops_the_revealed_block(cfg):
    """CSV 与记分板同源：只有一张矩阵，没有「已揭晓轮次」那块了。"""
    ledger(cfg, BACKEND_TASK, "A", score=100.0)
    ledger(cfg, BACKEND_TASK, "B", score=50.0, passed=False, pass1=False)

    csv = runs.scoreboard_csv(runs.scoreboard(cfg))

    assert csv.startswith("任务,档位,")
    assert "# 已揭晓轮次" not in csv
    assert "A(结束次数" in csv
    assert "台账" in csv, "导出口径要写清数据源"


def test_scoreboard_with_no_ledger_is_empty_not_error(cfg):
    """台账为空时返回空矩阵，不报错。"""
    board = runs.scoreboard(cfg)
    assert board["totals"] == {"attempts": 0, "pass1": 0, "pass_rate": 0.0}
    assert isinstance(board["matrix"], list)


def test_list_runs_ignores_quarantined_and_blind_trees(cfg):
    """规范布局之外的运行记录（整理隔离区/出题侧）不进统计，防幽灵档案。"""
    canonical = store_run(cfg, "TEST-01__正常模型__20260101-000003", BACKEND_TASK, "正常模型", True, 100.0)
    listed = {r["run_id"] for r in runs.list_runs(cfg)}
    assert canonical["run_id"] in listed

    # 隔离区里的夹具运行（tidy 整理的历史产物）不是真实成绩
    stray = os.path.join(cfg["runs_root"], "_quarantine", "cleanup", "runs", "TEST-01", "幽灵", "20260101-000000")
    os.makedirs(stray)
    util.write_json_atomic(os.path.join(stray, "run.json"), {
        "run_id": "TEST-01__幽灵__20260101-000000", "task": "TEST-01", "model": "幽灵",
        "status": "graded", "rounds": make_rounds(True, 100.0),
    })
    listed = {r["run_id"] for r in runs.list_runs(cfg)}
    assert "TEST-01__幽灵__20260101-000000" not in listed
    assert canonical["run_id"] in listed


# ---------------------------------------------------------------- 模型身份归并

def test_scoreboard_merges_legacy_model_ids_into_current_column(cfg):
    """同一真实模型的两种身份聚成一列：老档案 id 与限定名条目并入当前模型列。

    供应商重构前后的记录（old-arch / m1 / prov::m1）必须进同一个格子，
    数字合起来算；老名字不能再单列，否则成绩被劈开。
    """
    install_providers(cfg, [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [{"id": "m1", "name": "old-arch", "note": ""}],
        "legacy_ids": ["old-arch"],
    }])
    ledger(cfg, BACKEND_TASK, "old-arch", score=100.0, passed=True, pass1=True)
    ledger(cfg, BACKEND_TASK, "m1", score=80.0, passed=True, pass1=True)
    ledger(cfg, BACKEND_TASK, "prov::m1", score=40.0, passed=False, pass1=False)

    board = runs.scoreboard(cfg)
    assert "old-arch" not in board["models"], "老档案 id 不能再单列"
    assert "prov::m1" not in board["models"]
    assert "m1" in board["models"]
    cell = board["matrix"][next(i for i, r in enumerate(board["matrix"])
                                if r["task"] == BACKEND_TASK)]["cells"]["m1"]
    assert cell["attempts"] == 3, "三条条目都该进同一格"
    assert cell["pass1"] == 2
    assert cell["avg_score"] == pytest.approx((100.0 + 80.0 + 40.0) / 3, abs=0.1)
    assert len(cell["entry_ids"]) == 3

    csv = runs.scoreboard_csv(board)
    main_block = csv.split("#")[0]
    assert "old-arch" not in main_block, "CSV 导出要与记分板同一套列名"
    assert "m1(" in main_block


def test_scoreboard_keeps_orphaned_entries_unmerged(cfg):
    """档案已删（没有任何现存档案认领）的条目不归并：保持原名单列，数字不挪。"""
    install_providers(cfg, [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [{"id": "m1", "name": "old-arch", "note": ""}],
        "legacy_ids": ["old-arch"],
    }])
    ledger(cfg, BACKEND_TASK, "old-arch", score=100.0, passed=True, pass1=True)
    ledger(cfg, BACKEND_TASK, "cbcn-gone", score=40.0, passed=False, pass1=False)

    board = runs.scoreboard(cfg)
    assert "cbcn-gone" in board["models"], "命中不到现存档案的老名字保持原名（model_gone 语义）"
    assert cell_of(cfg, BACKEND_TASK, "cbcn-gone")["attempts"] == 1
    assert cell_of(cfg, BACKEND_TASK, "cbcn-gone")["pass1"] == 0
    assert cell_of(cfg, BACKEND_TASK, "cbcn-gone")["avg_score"] == 40.0
    assert cell_of(cfg, BACKEND_TASK, "m1")["attempts"] == 1, "能认领的归并照常进行"


def test_scoreboard_will_not_guess_ambiguous_legacy_id(cfg):
    """供应商下有多个模型时，认不出属于谁的 legacy id 不乱归并（fail-closed）。

    猜错等于把一个模型的成绩记到另一个模型头上，比留着单列更糟。
    """
    install_providers(cfg, [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [
            {"id": "m1", "name": "改名一", "note": ""},
            {"id": "m2", "name": "改名二", "note": ""},
        ],
        "legacy_ids": ["old-arch", "改名一", "改名二"],
    }])
    ledger(cfg, BACKEND_TASK, "old-arch", score=100.0)
    ledger(cfg, BACKEND_TASK, "m1", score=40.0, passed=False, pass1=False)

    board = runs.scoreboard(cfg)
    assert "old-arch" in board["models"], "归属无歧义前不许归并"
    assert cell_of(cfg, BACKEND_TASK, "old-arch")["attempts"] == 1
    assert cell_of(cfg, BACKEND_TASK, "m1")["attempts"] == 1
    # 通过模型 name（同时出现在 legacy_ids 里）认领的不受影响
    ledger(cfg, BACKEND_TASK, "改名一", score=90.0)
    board = runs.scoreboard(cfg)
    assert "改名一" not in board["models"]
    assert cell_of(cfg, BACKEND_TASK, "m1")["attempts"] == 2


def test_scoreboard_maps_legacy_id_of_single_model_provider(cfg):
    """供应商下恰好一个模型时，legacy_ids 里的老档案 id 归属无歧义，直接归并。"""
    install_providers(cfg, [{
        "id": "solo", "display_name": "solo", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://solo.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [{"id": "s1", "name": "展示名不是身份", "note": ""}],
        "legacy_ids": ["solo-old"],
    }])
    ledger(cfg, BACKEND_TASK, "solo-old", score=100.0)

    board = runs.scoreboard(cfg)
    assert "solo-old" not in board["models"]
    assert "s1" in board["models"]
    assert cell_of(cfg, BACKEND_TASK, "s1")["attempts"] == 1
    assert runs.canonical_model(cfg, "solo-old") == "s1"
    assert runs.canonical_model(cfg, "谁也不认识") == "谁也不认识"


def test_task_leaderboard_merges_legacy_model_ids(cfg):
    """排行榜与记分板同口径（红线）：老档案 id 的条目以当前档案身份参赛。"""
    install_providers(cfg, [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [{"id": "m1", "name": "old-arch", "note": ""}],
        "legacy_ids": ["old-arch"],
    }])
    ledger(cfg, BACKEND_TASK, "old-arch", score=70.0)
    ledger(cfg, BACKEND_TASK, "m1", score=100.0)

    result = runs.task_leaderboard(cfg, BACKEND_TASK)
    assert [e["model"] for e in result["entries"]] == ["m1"], \
        "同一模型的两种身份不能在排行榜各占一行"
    assert result["entries"][0]["attempts"] == 2
    assert result["entries"][0]["score"] == 100.0


def test_delete_run_purges_everything_it_owns(cfg):
    """废弃 = 真删：记录目录（含对话与 epochs 归档）、沙箱、评分树全部消失。

    旧的"移进隔离区"留着一堆永远不会再看的目录，用户要的"废弃后回到初始界面"
    也就做不到；共享的快照缓存 sandboxes/.snapshots 必须原样留着。
    """
    run = store_run(cfg, "TEST-01__待删模型__20260101-000006", BACKEND_TASK, "待删模型", True, 100.0)
    sandbox_dir = os.path.join(cfg["sandbox_root"], run["run_id"])
    util.ensure_dir(sandbox_dir)
    util.write_text_atomic(os.path.join(sandbox_dir, "marker.txt"), "x")
    run["sandbox"] = sandbox_dir
    runs.save_run(cfg, run)
    run_dir_path = run["run_dir"]
    util.write_text_atomic(os.path.join(run_dir_path, "chat.jsonl"), '{"role":"user"}\n')
    util.ensure_dir(os.path.join(run_dir_path, "epochs", "20260101000000"))
    grade_dir = os.path.join(cfg["sandbox_root"], "_grade", util.sanitize_id(run["run_id"]))
    util.ensure_dir(grade_dir)
    util.write_text_atomic(os.path.join(grade_dir, "leftover.py"), "x = 1\n")
    snapshot_cache = os.path.join(cfg["sandbox_root"], ".snapshots")
    util.ensure_dir(snapshot_cache)
    util.write_text_atomic(os.path.join(snapshot_cache, "keep.me"), "共享基线")

    out = runs.delete_run(cfg, run["run_id"])
    assert out["deleted"] is True and out["purged"]
    assert not os.path.exists(run_dir_path), "记录目录必须真删，不是挪进隔离区"
    assert not os.path.exists(sandbox_dir)
    assert not os.path.exists(grade_dir)
    assert os.path.isfile(os.path.join(snapshot_cache, "keep.me")), "别的 run 还要用快照缓存"
    assert run["run_id"] not in {r["run_id"] for r in runs.list_runs(cfg)}
    assert not os.path.exists(os.path.dirname(run_dir_path)), "档案目录空了要一起收掉，别留空壳"
    with pytest.raises(errors.HarnessError) as excinfo:
        runs.delete_run(cfg, run["run_id"])
    assert excinfo.value.code == errors.E_RUN_NOT_FOUND


def test_hard_delete_keeps_a_sibling_record(cfg):
    """收空壳只在该档案真的没记录之后：同档案还有兄弟记录时父目录必须留着。"""
    keep = store_run(cfg, "TEST-01__留兄弟__20260101-000010", BACKEND_TASK, "留兄弟", True, 80.0)
    gone = store_run(cfg, "TEST-01__留兄弟__20260101-000011", BACKEND_TASK, "留兄弟", False, 10.0)
    parent = os.path.dirname(gone["run_dir"])

    runs.delete_run(cfg, gone["run_id"])

    assert parent == os.path.dirname(keep["run_dir"])
    assert not os.path.exists(gone["run_dir"])
    assert os.path.isdir(os.path.join(parent, "20260101-000010")), "兄弟记录不能跟着被收掉"


def test_purge_refuses_paths_outside_the_roots(cfg):
    """路径越界必须当场中止：删除不可逆，不能"少删一个目录继续往下走"。"""
    run = store_run(cfg, "TEST-01__越界模型__20260101-000008", BACKEND_TASK, "越界模型", False, 0.0)
    run["sandbox"] = os.path.dirname(os.path.abspath(cfg["sandbox_root"]))
    runs.save_run(cfg, run)

    with pytest.raises(errors.HarnessError) as excinfo:
        runs.delete_run(cfg, run["run_id"])
    assert excinfo.value.code == errors.E_INTERNAL
    assert os.path.isdir(os.path.dirname(os.path.abspath(cfg["sandbox_root"]))), "越界路径一个字节都不能碰"


def test_delete_run_refuses_while_chat_lock_held(cfg):
    """对话进行中（运行锁被其它线程持有）不能删除记录。"""
    run = store_run(cfg, "TEST-01__占删模型__20260101-000007", BACKEND_TASK, "占删模型", False, 0.0)
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with chat.lock_for(run["run_id"]):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(timeout=5), "持锁线程没就绪"
    try:
        with pytest.raises(errors.HarnessError) as excinfo:
            runs.delete_run(cfg, run["run_id"])
        assert excinfo.value.code == errors.E_RUN_BUSY
    finally:
        release.set()
        holder.join(timeout=5)
    out = runs.delete_run(cfg, run["run_id"])
    assert out["deleted"] is True


# ---------------------------------------------------------------- 校准

def test_calibration_queue_does_not_hold_drives(cfg, monkeypatch):
    """排队阶段不占盘符：Q/R/S 只有三个，排 5 个也不能爆。"""
    store_run(cfg, "占位__X__20260101-000000", BACKEND_TASK, "X", False, 0.0)
    conf = dict(cfg)
    conf["models"] = [{"id": "校准模型", "protocol": "custom", "base_url": "",
                       "model": "m", "key_masked": "", "note": ""}]
    out = calibrate.enqueue(conf, BACKEND_TASK, "校准模型", trials=5)
    assert out["trials"] == 5
    assert out["target_band"] == [16.7, 33.3, 100.0]

    queued = [r for r in runs.list_runs(conf) if r.get("status") == "queued"]
    assert len(queued) == 5
    for run in queued:
        assert run["drive"] == "", "排队项不该占盘符"
        assert run["sandbox"] == "", "排队项不该现在就铺沙箱"

    status = calibrate.queue_status(conf, BACKEND_TASK, "校准模型")
    assert status["queued"] == 5 and status["done"] == 0
    assert status["ci_high"] == 1.0, "没有样本时区间应当是 [0,1]"


def test_calibration_cannot_exceed_cap(cfg):
    conf = dict(cfg)
    conf["models"] = [{"id": "校准模型", "protocol": "custom", "base_url": "",
                       "model": "m", "key_masked": "", "note": ""}]
    out = calibrate.enqueue(conf, BACKEND_TASK, "校准模型", trials=99)
    assert out["trials"] == calibrate.MAX_TRIALS


def test_calibration_cancel_removes_record(cfg):
    conf = dict(cfg)
    conf["models"] = [{"id": "校准模型", "protocol": "custom", "base_url": "",
                       "model": "m", "key_masked": "", "note": ""}]
    out = calibrate.enqueue(conf, BACKEND_TASK, "校准模型", trials=2)
    calibrate.cancel(conf, out["run_ids"][0])
    remaining = [r for r in runs.list_runs(conf) if r.get("status") == "queued"]
    assert len(remaining) == 1


def test_create_run_claims_a_queued_calibration_slot(cfg, log):
    """跑一次就消耗一个排队名额，不必手动清队列。"""
    from harness import packs
    conf = dict(cfg)
    conf["models"] = [{"id": "排队模型", "protocol": "custom", "base_url": "",
                       "model": "m", "key_masked": "", "note": ""}]
    calibrate.enqueue(conf, BACKEND_TASK, "排队模型", trials=1)
    run = runs.create_run(conf, BACKEND_TASK, "排队模型", claim_queued=True, log=log)
    try:
        assert run["calibration"] is True
        assert run["status"] == "ready"
        assert run["drive"] == ""
        assert os.path.isdir(run["sandbox"])
    finally:
        sandbox.destroy(conf, run, log=log)