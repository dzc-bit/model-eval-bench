"""验收：记录与统计（设计文档 §16）。

    · 每轮目录存齐 run.json / baseline_manifest.json / diff.patch / report.json / notes.md；
    · 记分板保留任务×模型统计，单元格带 Wilson 95% 区间；
    · 揭晓过的轮次单列，不混进主统计；
    · 校准排队不铺工作区，跑一次消耗一个。
"""

from __future__ import annotations

import math
import os
import threading

import pytest

from conftest import BACKEND_TASK, make_run, write_in_sandbox
from harness import calibrate, chat, errors, runs, sandbox, util


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
    cell = runs._cell_stats([after])
    assert cell["avg_score"] == 0.0 and cell["pass_any"] == 0

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
    """回收沙箱只删工作区目录：磁盘要还，成绩、报告、对话记录一个都不能少。"""
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


def test_leaderboard_ranks_by_model_work_time_not_wall_clock(cfg):
    """排行榜排的是"模型干了多久"，不是"从建号到交卷挂了多久"。

    墙钟口径会让挂机比干活更快：一条 3 秒交卷但模型实际跑了 15 分钟的记录，
    不该赢过一条挂了半小时、模型只干了 1 分钟的记录。
    """
    idle = store_run(cfg, "TEST-01__挂机模型__20260101-000001", BACKEND_TASK, "挂机模型", True, 100.0)
    idle["rounds"][0]["graded_at"] = "2026-01-01T00:00:03"
    idle["rounds"][0]["model_work_seconds"] = 900.0
    runs.save_run(cfg, idle)

    busy = store_run(cfg, "TEST-01__干活模型__20260101-000001", BACKEND_TASK, "干活模型", True, 100.0)
    busy["rounds"][0]["graded_at"] = "2026-01-01T00:30:00"
    busy["rounds"][0]["model_work_seconds"] = 60.0
    runs.save_run(cfg, busy)

    entries = runs.task_leaderboard(cfg, BACKEND_TASK)["entries"]
    assert [e["model"] for e in entries] == ["干活模型", "挂机模型"]
    assert entries[0]["duration_s"] == 60.0
    assert entries[0]["wall_seconds"] == 1800.0, "墙钟口径要留着做对照，不能悄悄丢掉"


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


def test_delete_model_with_runs_purges_records(cfg, monkeypatch, tmp_path):
    """删除档案可连带真删名下运行记录；不带 with_runs 时记录保留。"""
    store_run(cfg, "TEST-01__全删模型__20260101-000008", BACKEND_TASK, "全删模型", True, 100.0)
    store_run(cfg, "TEST-01__全删模型__20260101-000009", BACKEND_TASK, "全删模型", False, 20.0)
    shadow = tmp_path / "config.json"
    shadow.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runs.config, "CONFIG_PATH", str(shadow))
    cfg["models"] = [{"id": "全删模型", "protocol": "custom", "base_url": "", "model": "m", "key_masked": "", "note": ""}]

    out = runs.delete_model(cfg, "全删模型", with_runs=True)
    assert out["deleted"] is True
    assert sorted(out["removed_runs"]) == [
        "TEST-01__全删模型__20260101-000008", "TEST-01__全删模型__20260101-000009",
    ]
    assert out["remaining"] == 0
    assert all(not os.path.exists(p) for p in out["purged_paths"]), "说好的真删，路径得真的没了"
    assert all(r.get("model") != "全删模型" for r in runs.list_runs(cfg))
    import json as _json
    assert _json.loads(shadow.read_text(encoding="utf-8"))["models"] == []

    # 不带 with_runs：只删档案，记录保留
    store_run(cfg, "TEST-01__留档模型__20260101-000010", BACKEND_TASK, "留档模型", True, 80.0)
    cfg["models"] = [{"id": "留档模型", "protocol": "custom", "base_url": "", "model": "m", "key_masked": "", "note": ""}]
    out2 = runs.delete_model(cfg, "留档模型")
    assert out2["deleted"] is True
    assert out2["removed_runs"] == []
    assert any(r.get("model") == "留档模型" for r in runs.list_runs(cfg))


def test_delete_provider_with_runs_purges_records(cfg, monkeypatch, tmp_path):
    """删除供应商可连带真删名下运行记录：限定名前缀与老裸 id 两种形态都算名下。"""
    store_run(cfg, "TEST-01__prov-m1__20260101-000011", BACKEND_TASK, "prov::m1", True, 90.0)
    store_run(cfg, "TEST-01__prov-m2__20260101-000012", BACKEND_TASK, "prov::m2", False, 10.0)
    store_run(cfg, "TEST-01__01__20260101-000013", BACKEND_TASK, "01", True, 70.0)
    store_run(cfg, "TEST-01__别家__20260101-000014", BACKEND_TASK, "别家", False, 0.0)
    shadow = tmp_path / "config.json"
    shadow.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runs.config, "CONFIG_PATH", str(shadow))
    cfg["providers"] = [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [
            {"id": "m1", "name": "m1", "context_window": 262144, "max_tokens": 32768, "note": ""},
            {"id": "m2", "name": "m2", "context_window": 262144, "max_tokens": 32768, "note": ""},
        ],
        "legacy_ids": ["01"],
    }]

    out = runs.delete_provider(cfg, "prov", with_runs=True)
    assert out["deleted"] is True
    assert sorted(out["removed_runs"]) == [
        "TEST-01__01__20260101-000013", "TEST-01__prov-m1__20260101-000011",
        "TEST-01__prov-m2__20260101-000012",
    ]
    assert out["remaining"] == 0
    assert all(not os.path.exists(p) for p in out["purged_paths"]), "说好的真删，路径得真的没了"
    remaining_models = [r.get("model") for r in runs.list_runs(cfg)]
    assert remaining_models == ["别家"], "别家供应商的记录不能被牵连"
    import json as _json
    assert _json.loads(shadow.read_text(encoding="utf-8"))["providers"] == []


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
