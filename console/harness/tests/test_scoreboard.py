"""验收：记录与统计（设计文档 §16）。

    · 每轮目录存齐 run.json / baseline_manifest.json / diff.patch / report.json / notes.md；
    · 记分板保留任务×模型统计，单元格带 Wilson 95% 区间；
    · 揭晓过的轮次单列，不混进主统计；
    · 校准排队不铺工作区，跑一次消耗一个。
"""

from __future__ import annotations

import math
import os

import pytest

from conftest import BACKEND_TASK, make_run, write_in_sandbox
from harness import calibrate, errors, runs, sandbox, util


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
        "revealed": revealed, "calibration": False, "rounds": make_rounds(passed, score, attempts),
        "last_score": score, "last_passed": passed, "note": "", "drive": "", "sandbox": "",
        "baseline_commit": "", "baseline_digest": "deadbeef",
    }
    runs.save_run(cfg, run)
    return run


# ------------------------------------------------------------------ 归档

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
    """Wilson 区间要与手算一致，样本为 0 时退化成 [0,1]。"""
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
    """行=任务、列=模型，单元格统计 pass@1 与区间。"""
    store_run(cfg, "TEST-01__A__20260101-000001", BACKEND_TASK, "A", True, 100.0)
    store_run(cfg, "TEST-01__A__20260101-000002", BACKEND_TASK, "A", False, 33.3)
    store_run(cfg, "TEST-01__B__20260101-000001", BACKEND_TASK, "B", False, 16.7)

    board = runs.scoreboard(cfg)
    assert set(board["models"]) >= {"A", "B"}
    assert BACKEND_TASK in board["tasks"]
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    assert row["title"] == "派生指标口径不一致：换手率被算成了三份"
    assert row["tier"] == "medium"

    cell = row["cells"]["A"]
    assert cell["trials"] == 2
    assert cell["pass1"] == 1
    assert cell["pass_rate"] == 0.5
    assert cell["ci_low"] < 0.5 < cell["ci_high"]
    assert cell["avg_score"] == pytest.approx(66.7, abs=0.1)
    assert board["totals"]["trials"] == 3
    assert board["totals"]["pass1"] == 1


def test_task_leaderboard_prioritizes_rounds_then_elapsed_time(cfg):
    """题目排行榜只列有效未揭晓的成功记录，先比通过轮次，再比总耗时。"""
    slower = store_run(cfg, "TEST-01__慢模型__20260101-000001", BACKEND_TASK, "慢模型", True, 100.0)
    slower["rounds"][0]["graded_at"] = "2026-01-01T00:00:20"
    runs.save_run(cfg, slower)

    faster = store_run(cfg, "TEST-01__快模型__20260101-000001", BACKEND_TASK, "快模型", True, 100.0)
    faster["rounds"][0]["graded_at"] = "2026-01-01T00:00:05"
    runs.save_run(cfg, faster)

    later_round = store_run(cfg, "TEST-01__两轮模型__20260101-000001", BACKEND_TASK, "两轮模型", True, 100.0, attempts=2)
    later_round["rounds"][0]["passed"] = False
    later_round["rounds"][0]["score"] = 50.0
    later_round["rounds"][1]["passed"] = True
    later_round["rounds"][1]["graded_at"] = "2026-01-01T00:00:01"
    runs.save_run(cfg, later_round)

    invalidated = store_run(cfg, "TEST-01__作废模型__20260101-000001", BACKEND_TASK, "作废模型", True, 100.0)
    invalidated["rounds"][0]["invalidated"] = True
    runs.save_run(cfg, invalidated)
    store_run(cfg, "TEST-01__揭晓模型__20260101-000001", BACKEND_TASK, "揭晓模型", True, 100.0, revealed=True)

    result = runs.task_leaderboard(cfg, BACKEND_TASK)
    assert [entry["model"] for entry in result["entries"]] == ["快模型", "慢模型", "两轮模型"]
    assert [entry["rank"] for entry in result["entries"]] == [1, 2, 3]
    assert [entry["rounds"] for entry in result["entries"]] == [1, 1, 2]
    assert result["entries"][0]["duration_s"] == 5.0


def test_revealed_rounds_are_excluded_from_main_stats(cfg):
    """揭晓过的轮次不进通过率，但要单独计数并出现在 CSV 的已揭晓块。"""
    store_run(cfg, "TEST-01__A__20260101-000001", BACKEND_TASK, "A", True, 100.0)
    store_run(cfg, "TEST-01__A__20260101-000002", BACKEND_TASK, "A", True, 100.0, revealed=True)

    board = runs.scoreboard(cfg)
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    cell = row["cells"]["A"]
    assert cell["trials"] == 1, "已揭晓的那轮不计入分母"
    assert cell["pass1"] == 1
    assert cell["revealed"] == 1

    csv = runs.scoreboard_csv(board)
    main_block, revealed_block = csv.split("# 已揭晓轮次")
    assert "TEST-01" in main_block
    assert "TEST-01,A,1" in revealed_block
    assert csv.startswith("任务,档位,")


def test_pass_at_k_counts_any_green_round(cfg):
    """pass@k：前 k 轮里有一轮全绿就算通过。"""
    run = {
        "run_id": "TEST-01__C__20260101-000001", "task": BACKEND_TASK, "model": "C",
        "attempt": 2, "status": "graded", "created_at": "2026-01-01T00:00:00",
        "revealed": False, "rounds": [
            {"attempt": 1, "score": 33.3, "passed": False, "graded_at": "x"},
            {"attempt": 2, "score": 100.0, "passed": True, "graded_at": "x"},
        ],
        "last_score": 100.0,
    }
    runs.save_run(cfg, run)
    board = runs.scoreboard(cfg)
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    cell = row["cells"]["C"]
    assert cell["pass1"] == 0, "第 1 轮没全绿"
    assert cell["pass_any"] == 1, "第 2 轮全绿，pass@2 应算通过"


def test_scoreboard_with_no_runs_is_empty_not_error(cfg):
    """一条记录都没有时返回空矩阵，不报错。"""
    board = runs.scoreboard(cfg)
    assert board["totals"]["trials"] == 0
    assert board["totals"]["ci_low"] == 0.0 and board["totals"]["ci_high"] == 1.0
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


def test_scoreboard_skips_runs_without_task_or_model(cfg):
    """缺 task/model 的坏记录不建幽灵行列（曾出现重复的 None 列）。"""
    kept = store_run(cfg, "TEST-01__正常模型__20260101-000004", BACKEND_TASK, "正常模型", True, 100.0)
    broken = dict(kept)
    broken["run_id"] = "TEST-01__坏记录__20260101-000005"
    broken["model"] = None
    broken["task"] = None
    runs.save_run(cfg, broken)

    board = runs.scoreboard(cfg)
    assert "None" not in board["models"]
    assert "" not in board["models"]
    assert "None" not in board["tasks"]
    assert board["matrix"][0]["cells"]["正常模型"]["trials"] == 1


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
