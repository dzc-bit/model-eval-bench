"""验收：成绩台账与「结束本轮」语义（2026-10-02 收尾语义收敛）。

这一组锁的是新模型的核心承诺，每条都对应用户拍板时写下的原话：

    · 点「结束本轮」= 成绩写进台账 + 记录真删（工作台回到干净空态）
    · 台账与运行记录脱钩：记录没了，榜单还有数
    · 榜单展示同一题×同一模型的**最高分**那条
    · 「废弃本轮」与「结束本轮」的**唯一区别**是进不进台账
    · 作废轮与判无效轮永不进台账；已揭晓参考解的尝试也不进
    · 删档案 = 彻底删除：名下记录与台账条目一起真删
    · 一次性回填后，榜单数字与回填前完全一致
"""

from __future__ import annotations

import os
import threading

import pytest

from conftest import BACKEND_TASK
from harness import chat, config, errors, results, runs, util


def make_rounds(passed, score, attempts=1, invalidated=False):
    """造 n 轮成绩：第 1 轮决定 pass1，其余轮次分数另给。"""
    return [
        {
            "attempt": i,
            "score": score if (passed or i == 1) else 0.0,
            "passed": bool(passed and i == 1),
            "invalidated": bool(invalidated),
            "graded_at": "2026-01-0%dT00:00:00" % i,
            "report": "round-%d.json" % i,
        }
        for i in range(1, attempts + 1)
    ]


def store_run(cfg, run_id, task, model, passed, score, attempts=1,
              revealed=False, invalidated=False, work_seconds=None):
    """只造记录，不铺沙箱。"""
    rounds = make_rounds(passed, score, attempts, invalidated)
    if work_seconds is not None:
        for rnd in rounds:
            rnd["model_work_seconds"] = work_seconds
    run = {
        "run_id": run_id, "task": task, "model": model, "attempt": attempts,
        "attempts_allowed": 3, "status": "graded", "created_at": "2026-01-01T00:00:00",
        "round_started_at": "2026-01-01T00:00:00",
        "revealed": revealed, "calibration": False, "rounds": rounds,
        "last_score": score, "last_passed": passed, "note": "", "drive": "", "sandbox": "",
        "baseline_commit": "", "baseline_digest": "deadbeef",
    }
    runs.save_run(cfg, run)
    return run


def seed_entry(cfg, task, model, *, score=100.0, passed=True, pass1=True,
               rounds=1, work=None, wall=None, run_id=""):
    """直接往台账写一条（模拟一次「结束本轮」的结果，不铺记录）。"""
    return results.append_entry(cfg, results.make_entry(
        task, model, model,
        source_run_id=run_id,
        rounds=rounds, best_round=rounds, score=score, passed=passed, pass1=pass1,
        model_work_seconds=work, wall_seconds=wall,
        graded_at="2026-01-01T00:00:00",
    ))


def entries_for(cfg, task, model):
    return [e for e in results.load_entries(cfg)
            if e["task"] == task and e["model_raw"] == model]


# ==========================================================================
# 结束本轮：写台账 + 真删记录
# ==========================================================================

def test_finish_writes_ledger_then_really_deletes_the_record(cfg):
    """点「结束本轮」= 成绩落台账，记录/对话/沙箱/评分树一起真删。"""
    run = store_run(cfg, "TEST-01__结束模型__20260101-000001", BACKEND_TASK,
                    "结束模型", True, 100.0)
    sandbox_dir = os.path.join(cfg["sandbox_root"], run["run_id"])
    util.ensure_dir(sandbox_dir)
    util.write_text_atomic(os.path.join(sandbox_dir, "app.py"), "x = 1\n")
    run["sandbox"] = sandbox_dir
    runs.save_run(cfg, run)
    util.write_text_atomic(os.path.join(run["run_dir"], "chat.jsonl"), '{"role":"user"}\n')
    grade_dir = os.path.join(cfg["sandbox_root"], "_grade", util.sanitize_id(run["run_id"]))
    util.ensure_dir(grade_dir)
    run_dir_path = run["run_dir"]

    out = runs.finish_round(cfg, run["run_id"])

    assert out["finished"] is True
    assert out["ledgered"] is True
    assert out["entry"]["score"] == 100.0
    assert out["entry"]["pass1"] is True
    assert out["purged"], "沙箱与记录目录都要真删"

    # 台账里确实多了一条
    entries = entries_for(cfg, BACKEND_TASK, "结束模型")
    assert len(entries) == 1 and entries[0]["score"] == 100.0

    # 记录没了，但榜单还有数
    assert not os.path.exists(run_dir_path), "记录目录必须真删"
    assert not os.path.exists(sandbox_dir)
    assert not os.path.exists(grade_dir)
    with pytest.raises(errors.HarnessError) as excinfo:
        runs.get_run(cfg, run["run_id"])
    assert excinfo.value.code == errors.E_RUN_NOT_FOUND

    board = runs.scoreboard(cfg)
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    assert row["cells"]["结束模型"]["attempts"] == 1
    assert row["cells"]["结束模型"]["best_score"] == 100.0


def test_ledger_outlives_the_run_record(cfg):
    """台账与运行记录脱钩：记录删干净之后，榜单与排行榜都还在。"""
    run = store_run(cfg, "TEST-01__脱钩模型__20260101-000001", BACKEND_TASK,
                    "脱钩模型", True, 100.0, work_seconds=42.0)
    runs.finish_round(cfg, run["run_id"])

    assert not os.path.exists(run["run_dir"])
    board = runs.scoreboard(cfg)
    assert board["totals"]["attempts"] == 1
    leader = runs.task_leaderboard(cfg, BACKEND_TASK)
    assert [e["model"] for e in leader["entries"]] == ["脱钩模型"]
    assert leader["entries"][0]["score"] == 100.0


def test_finish_is_idempotent_per_record(cfg):
    """同一条记录不会被写进台账两次（回填之后又点一次结束尤其要防）。"""
    run = store_run(cfg, "TEST-01__去重模型__20260101-000001", BACKEND_TASK,
                    "去重模型", True, 100.0)
    entry = runs.record_run_result(cfg, run)
    assert entry is not None
    results.append_entry(cfg, entry)
    # 记录还在，但台账已经有它的条目了：再记一次必须是 None
    assert runs.record_run_result(cfg, run) is None
    assert len(entries_for(cfg, BACKEND_TASK, "去重模型")) == 1


def test_finish_without_a_graded_round_deletes_but_writes_nothing(cfg):
    """没跑过校验就结束：记录照删，台账一条不写（没成绩可记）。"""
    run = {
        "run_id": "TEST-01__空跑模型__20260101-000001", "task": BACKEND_TASK,
        "model": "空跑模型", "attempt": 1, "status": "ready",
        "created_at": "2026-01-01T00:00:00", "revealed": False, "rounds": [],
        "note": "", "drive": "", "sandbox": "", "attempts_allowed": 3,
    }
    runs.save_run(cfg, run)

    out = runs.finish_round(cfg, run["run_id"])

    assert out["finished"] is True
    assert out["ledgered"] is False
    assert "没有可计入台账的成绩" in out["notice"]
    assert entries_for(cfg, BACKEND_TASK, "空跑模型") == []
    assert not os.path.exists(run["run_dir"])


# ==========================================================================
# 作废轮 / 判无效轮 / 已揭晓：一律不进台账
# ==========================================================================

def test_voided_rounds_never_enter_the_ledger(cfg):
    """「继续对话（本轮分数作废）」把轮次标 voided 之后结束，台账不收它。"""
    run = store_run(cfg, "TEST-01__作废模型__20260101-000001", BACKEND_TASK,
                    "作废模型", True, 100.0)
    runs.save_run(cfg, run)
    runs.reopen(cfg, run["run_id"])          # 作废本轮 → voided
    stored = runs.get_run(cfg, run["run_id"])
    assert stored["rounds"][0]["voided"] is True

    out = runs.finish_round(cfg, run["run_id"])

    assert out["ledgered"] is False
    assert entries_for(cfg, BACKEND_TASK, "作废模型") == []


def test_invalidated_rounds_never_enter_the_ledger(cfg):
    """越界/回归判无效的轮次不写台账——本轮不作数就是不作数。"""
    store_run(cfg, "TEST-01__越界模型__20260101-000001", BACKEND_TASK,
              "越界模型", True, 100.0, invalidated=True)
    runs.backfill_ledger(cfg)
    assert entries_for(cfg, BACKEND_TASK, "越界模型") == []
    # 一条台账都没有 → 记分板上连这一列都不该出现（空台账不产生幽灵列）
    assert "越界模型" not in runs.scoreboard(cfg)["models"]


def test_revealed_run_never_enters_the_ledger(cfg):
    """看过参考解的尝试永不进台账（揭晓等于看过答案）。"""
    store_run(cfg, "TEST-01__揭晓模型__20260101-000001", BACKEND_TASK,
              "揭晓模型", True, 100.0, revealed=True)
    runs.backfill_ledger(cfg)
    assert entries_for(cfg, BACKEND_TASK, "揭晓模型") == []


def test_mixed_rounds_take_the_best_counted_round(cfg):
    """同一轮里混着作废轮与无效轮时，代表分取作数轮里的最高分。"""
    run = {
        "run_id": "TEST-01__混合模型__20260101-000001", "task": BACKEND_TASK,
        "model": "混合模型", "attempt": 3, "status": "graded",
        "created_at": "2026-01-01T00:00:00", "round_started_at": "2026-01-01T00:00:00",
        "revealed": False, "last_score": 60.0,
        "rounds": [
            {"attempt": 1, "score": 90.0, "passed": True, "graded_at": "2026-01-01T00:00:01"},
            {"attempt": 2, "score": 100.0, "passed": True, "graded_at": "2026-01-01T00:00:02",
             "invalidated": True},
            {"attempt": 3, "score": 60.0, "passed": False, "graded_at": "2026-01-01T00:00:03"},
        ],
    }
    runs.save_run(cfg, run)
    entry = runs.record_run_result(cfg, run)
    assert entry["score"] == 90.0, "判无效的 100 分不能当代表分"
    assert entry["rounds"] == 2, "作数轮数 = 2（第 2 轮无效被排除）"
    assert entry["best_round"] == 1
    assert entry["pass1"] is True


# ==========================================================================
# 废弃 vs 结束：唯一区别是进不进台账
# ==========================================================================

def test_discard_leaves_no_trace_in_the_ledger(cfg):
    """「废弃本轮」与「结束本轮」磁盘后果一样，唯一区别是台账里没有它。"""
    run = store_run(cfg, "TEST-01__废弃模型__20260101-000001", BACKEND_TASK,
                    "废弃模型", True, 100.0)
    sandbox_dir = os.path.join(cfg["sandbox_root"], run["run_id"])
    util.ensure_dir(sandbox_dir)
    run["sandbox"] = sandbox_dir
    runs.save_run(cfg, run)
    run_dir_path = run["run_dir"]

    runs.delete_run(cfg, run["run_id"])

    assert not os.path.exists(run_dir_path)
    assert not os.path.exists(sandbox_dir)
    assert entries_for(cfg, BACKEND_TASK, "废弃模型") == [], "废弃不该进台账"
    assert "废弃模型" not in runs.scoreboard(cfg)["models"], \
        "废弃之后不该在记分板上留下一个空列"


# ==========================================================================
# 榜单取最高分
# ==========================================================================

def test_scoreboard_keeps_every_attempt_and_shows_the_best(cfg):
    """台账保留每一次结束；单元格展示最高分那条，均分仍按全部条目算。"""
    seed_entry(cfg, BACKEND_TASK, "反复模型", score=30.0, passed=False, pass1=False, run_id="r1")
    seed_entry(cfg, BACKEND_TASK, "反复模型", score=100.0, passed=True, pass1=True, run_id="r2")
    seed_entry(cfg, BACKEND_TASK, "反复模型", score=60.0, passed=False, pass1=False, run_id="r3")

    board = runs.scoreboard(cfg)
    row = next(r for r in board["matrix"] if r["task"] == BACKEND_TASK)
    cell = row["cells"]["反复模型"]

    assert cell["attempts"] == 3, "每一次结束都要留一条，不能只留最好的"
    assert cell["pass1"] == 1
    assert cell["best_score"] == 100.0
    assert cell["avg_score"] == pytest.approx((30.0 + 100.0 + 60.0) / 3, abs=0.1)
    assert len(cell["entry_ids"]) == 3


def test_leaderboard_shows_the_best_entry_per_model(cfg):
    """排行榜：同一模型只占一行，展示台账里分数最高的那次结束。"""
    seed_entry(cfg, BACKEND_TASK, "低分模型", score=30.0, passed=False, pass1=False,
               rounds=1, work=10.0, run_id="r1")
    seed_entry(cfg, BACKEND_TASK, "低分模型", score=95.0, passed=False, pass1=False,
               rounds=3, work=900.0, run_id="r2")
    seed_entry(cfg, BACKEND_TASK, "高分模型", score=100.0, passed=True, pass1=True,
               rounds=1, work=60.0, run_id="r3")

    result = runs.task_leaderboard(cfg, BACKEND_TASK)
    entries = result["entries"]

    assert [e["model"] for e in entries] == ["高分模型", "低分模型"], "先按最高分排"
    assert [e["rank"] for e in entries] == [1, 2]
    best = entries[1]
    assert best["score"] == 95.0, "展示的是分数最高那条，不是最快那条"
    assert best["rounds"] == 3
    assert best["duration_s"] == 900.0, "排名里带的是那条自己的用时"
    assert best["attempts"] == 2, "attempts 说明最高分那条是撞出来的还是稳出来的"


def test_leaderboard_breaks_ties_by_rounds_then_work_time(cfg):
    """同分先比轮数，再比模型工作时间（挂机不算）。"""
    seed_entry(cfg, BACKEND_TASK, "一轮模型", score=100.0, rounds=1, work=900.0)
    seed_entry(cfg, BACKEND_TASK, "三轮模型", score=100.0, rounds=3, work=5.0)

    result = runs.task_leaderboard(cfg, BACKEND_TASK)
    assert [e["model"] for e in result["entries"]] == ["一轮模型", "三轮模型"], \
        "同分时一次全绿的比改了三次才全绿的强，哪怕它更慢"


def test_ranking_uses_model_work_time_not_wall_clock(cfg):
    """排的是「模型干了多久」，不是「从建号到交卷挂了多久」。"""
    seed_entry(cfg, BACKEND_TASK, "挂机模型", score=100.0, rounds=1, work=900.0, wall=1800.0)
    seed_entry(cfg, BACKEND_TASK, "干活模型", score=100.0, rounds=1, work=60.0, wall=60.0)

    entries = runs.task_leaderboard(cfg, BACKEND_TASK)["entries"]
    assert [e["model"] for e in entries] == ["干活模型", "挂机模型"]
    assert entries[0]["duration_s"] == 60.0
    assert entries[1]["wall_seconds"] == 1800.0, "墙钟口径要留着做对照，不能悄悄丢掉"


# ==========================================================================
# 删档案 = 彻底删除（记录 + 台账一起）
# ==========================================================================

def test_delete_provider_purges_records_and_ledger_entries(cfg, monkeypatch, tmp_path):
    """删供应商：名下记录与台账条目一起真删，别家的一个字节都不能碰。"""
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
    cfg["models"] = config.expand_models(cfg["providers"])
    store_run(cfg, "TEST-01__prov-m1__20260101-000011", BACKEND_TASK, "prov::m1", True, 90.0)
    store_run(cfg, "TEST-01__01__20260101-000013", BACKEND_TASK, "01", True, 70.0)
    store_run(cfg, "TEST-01__别家__20260101-000014", BACKEND_TASK, "别家", False, 0.0)
    seed_entry(cfg, BACKEND_TASK, "prov::m1", score=90.0, run_id="e1")
    seed_entry(cfg, BACKEND_TASK, "01", score=70.0, run_id="e2")
    seed_entry(cfg, BACKEND_TASK, "别家", score=10.0, run_id="e3")

    out = runs.delete_provider(cfg, "prov")

    assert out["deleted"] is True
    assert sorted(out["removed_runs"]) == [
        "TEST-01__01__20260101-000013", "TEST-01__prov-m1__20260101-000011",
    ]
    assert len(out["removed_entries"]) == 2
    assert all(not os.path.exists(p) for p in out["purged_paths"])
    assert [r.get("model") for r in runs.list_runs(cfg)] == ["别家"]
    assert [e["model_raw"] for e in results.load_entries(cfg)] == ["别家"], \
        "删档案不留幽灵成绩：别家的还在，名下的全没了"

    # 服务端每个请求都重新读 config，所以删完后的下一个请求看到的就是新配置
    after = dict(cfg)
    after["providers"] = config.resolve_providers(
        __import__("json").loads(shadow.read_text(encoding="utf-8")))
    after["models"] = config.expand_models(after["providers"])
    board = runs.scoreboard(after)
    assert "m1" not in board["models"] and "01" not in board["models"]
    assert "别家" in board["models"]


def test_delete_model_is_always_cascading(cfg, monkeypatch, tmp_path):
    """删档案没有「只删档案留记录」的选项：默认即级联，不带 with_runs 也一样。"""
    store_run(cfg, "TEST-01__全删模型__20260101-000008", BACKEND_TASK, "全删模型", True, 100.0)
    store_run(cfg, "TEST-01__全删模型__20260101-000009", BACKEND_TASK, "全删模型", False, 20.0)
    seed_entry(cfg, BACKEND_TASK, "全删模型", score=100.0, run_id="e1")
    shadow = tmp_path / "config.json"
    shadow.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runs.config, "CONFIG_PATH", str(shadow))
    cfg["models"] = [{"id": "全删模型", "protocol": "custom", "base_url": "",
                      "model": "m", "key_masked": "", "note": ""}]

    out = runs.delete_model(cfg, "全删模型")          # 不传 with_runs

    assert out["deleted"] is True
    assert len(out["removed_runs"]) == 2
    assert len(out["removed_entries"]) == 1
    assert all(r.get("model") != "全删模型" for r in runs.list_runs(cfg))
    assert entries_for(cfg, BACKEND_TASK, "全删模型") == []


def test_provider_view_reports_run_and_entry_counts(cfg):
    """级联计数由后端下发：前端以前自己复刻匹配逻辑，两份必然漂移。"""
    cfg["providers"] = [{
        "id": "prov", "display_name": "prov", "protocol": "openai",
        "api_mode": "chat_completions", "base_url": "https://prov.test/v1",
        "default_context_window": 262144, "default_max_tokens": 32768, "note": "",
        "models": [{"id": "m1", "name": "m1", "context_window": 262144,
                    "max_tokens": 32768, "note": ""}],
        "legacy_ids": ["old-arch"],
    }]
    cfg["models"] = config.expand_models(cfg["providers"])
    store_run(cfg, "TEST-01__old-arch__20260101-000021", BACKEND_TASK, "old-arch", True, 100.0)
    store_run(cfg, "TEST-01__prov-m1__20260101-000022", BACKEND_TASK, "prov::m1", True, 80.0)
    store_run(cfg, "TEST-01__别家__20260101-000023", BACKEND_TASK, "别家", False, 0.0)
    seed_entry(cfg, BACKEND_TASK, "old-arch", score=100.0, run_id="e1")
    seed_entry(cfg, BACKEND_TASK, "prov::m1", score=80.0, run_id="e2")

    view = runs.list_providers(cfg)["providers"][0]

    assert view["run_count"] == 2, "限定名前缀与老档案 id 两种形态都要算名下"
    assert view["entry_count"] == 2, "确认框要能说出「连同 N 条成绩一并删除」"


# ==========================================================================
# 回填
# ==========================================================================

def test_backfill_matches_legacy_numbers(cfg):
    """一次性回填：榜单数字必须与回填前逐格一致（本机 T1-01/T1-02 100、T1-03 0、T2-05 83.3）。"""
    # 用与本机在册记录同形的四组数据复刻旧口径
    store_run(cfg, "T1-01__老模型__20261001-145503", "T1-01", "老模型", True, 100.0)
    store_run(cfg, "T1-02__老模型__20261001-145503", "T1-02", "老模型", True, 100.0)
    store_run(cfg, "T1-03__老模型__20261001-145503", "T1-03", "老模型", False, 0.0)
    two = store_run(cfg, "T2-05__老模型__20261001-121839", "T2-05", "老模型",
                    False, 83.3, attempts=2)

    # 回填前的旧口径（手算复刻 _cell_stats 的判定）
    legacy = {"T1-01": (1, 1, 100.0), "T1-02": (1, 1, 100.0),
              "T1-03": (1, 0, 0.0), "T2-05": (1, 0, 83.3)}
    del two

    out = runs.backfill_ledger(cfg)

    assert out["added"] == 4 and out["skipped"] == 0
    board = runs.scoreboard(cfg)
    for task, (attempts, pass1, best) in legacy.items():
        row = next(r for r in board["matrix"] if r["task"] == task)
        cell = row["cells"]["老模型"]
        assert cell["attempts"] == attempts, task
        assert cell["pass1"] == pass1, task
        assert cell["best_score"] == pytest.approx(best, abs=0.1), task
    assert board["totals"] == {"attempts": 4, "pass1": 2, "pass_rate": 0.5}


def test_backfill_is_idempotent_and_keeps_the_records(cfg):
    """回填只迁移读取来源，不删记录；重复执行不会把同一次尝试数两遍。"""
    store_run(cfg, "TEST-01__回填模型__20260101-000001", BACKEND_TASK, "回填模型", True, 100.0)

    first = runs.backfill_ledger(cfg)
    second = runs.backfill_ledger(cfg)

    assert first["added"] == 1
    assert second["added"] == 0 and second["skipped"] == 1
    assert len(entries_for(cfg, BACKEND_TASK, "回填模型")) == 1
    assert len(runs.list_runs(cfg)) == 1, "回填不许删记录"


def test_backfill_dry_run_writes_nothing(cfg):
    store_run(cfg, "TEST-01__试跑模型__20260101-000001", BACKEND_TASK, "试跑模型", True, 100.0)

    out = runs.backfill_ledger(cfg, dry_run=True)

    assert out["added"] == 1 and out["dry_run"] is True
    assert results.load_entries(cfg) == []


# ==========================================================================
# 台账文件本身的纪律
# ==========================================================================

def test_ledger_lives_under_runs_root_and_is_invisible_to_list_runs(cfg):
    """台账在 runs/ 之下，且 list_runs 不把它当运行记录（防幽灵档案）。"""
    seed_entry(cfg, BACKEND_TASK, "台账模型", score=100.0)
    path = results.ledger_path(cfg)
    assert os.path.isfile(path)
    assert os.path.dirname(os.path.dirname(path)) == cfg["runs_root"]
    assert all(r.get("model") != "_results" for r in runs.list_runs(cfg))


def test_ledger_survives_a_corrupt_file(cfg):
    """台账坏了不能让记分板 500：读侧一律当空台账处理。"""
    seed_entry(cfg, BACKEND_TASK, "半截模型", score=100.0)
    util.write_text_atomic(results.ledger_path(cfg), "{ 这不是 JSON")

    assert results.load_entries(cfg) == []
    board = runs.scoreboard(cfg)
    assert board["totals"]["attempts"] == 0
    assert isinstance(board["matrix"], list)


def test_entry_ids_increase_and_are_not_reused(cfg):
    first = seed_entry(cfg, BACKEND_TASK, "编号模型", score=10.0, run_id="r1")
    second = seed_entry(cfg, BACKEND_TASK, "编号模型", score=20.0, run_id="r2")
    assert first["entry_id"] == "res-000001"
    assert second["entry_id"] == "res-000002"

    # 删掉中间一条后，新条目不能复用已删的号
    with results._LEDGER_LOCK:
        results.write_entries(cfg, [e for e in results.load_entries(cfg)
                                    if e["entry_id"] != "res-000001"])
    third = seed_entry(cfg, BACKEND_TASK, "编号模型", score=30.0, run_id="r3")
    assert third["entry_id"] == "res-000003"


# ==========================================================================
# 测试套件自身的保险丝（2026-10-02 真实数据事故）
# ==========================================================================

def test_test_suite_cannot_delete_real_run_records(tmp_path):
    """删除类用例一旦让 runs_root 指回真实的 runs/，必须当场炸掉。

    这条锁的是一次真实事故：`test_provider_migration` 只 patch 了
    ``config.CONFIG_PATH``，``runs_root``/``sandbox_root`` 仍被解析到真实的
    ``runs/`` 与 ``sandboxes/``。当时 ``delete_provider`` 的 ``with_runs`` 默认为
    False，用例属于**侥幸安全**；把级联改成默认行为（删档案即彻底删除）之后，
    同一个用例真删了本机在册的四条运行记录（含对话、报告、diff 与沙箱）。

    教训：**「读侧 patch 了配置」不等于「写侧落在临时目录」**，检查必须放在
    删除函数的入口，而不是挂在 purge 上（真实 runs/ 空了 purge 根本不会被调用）。
    """
    from harness import config as real_config

    real_runs = os.path.join(real_config.EVAL_ROOT, "runs")
    cfg = dict(real_config.load())          # 真实配置：runs_root 指向真实的 runs/
    assert os.path.normcase(os.path.abspath(cfg["runs_root"])) == \
        os.path.normcase(os.path.abspath(real_runs)), \
        "前提失效：这个用例现在拿不到真实的 runs_root 了"

    for name, args in (("delete_run", ("不存在的run__id__20260101-000000",)),
                       ("purge_run", ({"run_id": "不存在__x__20260101-000000"},))):
        with pytest.raises(AssertionError) as excinfo:
            getattr(runs, name)(cfg, *args)
        assert "runs_root" in str(excinfo.value) and "tmp_path" in str(excinfo.value), \
            "%s 挡住真实 runs/ 时要说清楚该把路径改到哪" % name


def test_test_suite_cannot_delete_real_sandboxes(cfg):
    """沙箱侧同理：sandbox.destroy 指回真实 sandboxes/ 也要炸。"""
    from harness import sandbox as harness_sandbox

    real_cfg = dict(cfg)
    real_cfg["sandbox_root"] = os.path.join(config.EVAL_ROOT, "sandboxes")

    with pytest.raises(AssertionError) as excinfo:
        harness_sandbox.destroy(real_cfg, {"run_id": "x", "sandbox": ""})

    assert "sandbox_root" in str(excinfo.value)


def test_finish_is_refused_while_the_run_is_busy(cfg):
    """对话在飞时不许结束：评的是写了一半的沙箱。"""
    run = store_run(cfg, "TEST-01__占忙模型__20260101-000001", BACKEND_TASK, "占忙模型", True, 100.0)
    acquired = threading.Event()
    release = threading.Event()

    def hold():
        with chat.lock_for(run["run_id"]):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(errors.HarnessError) as excinfo:
            runs.finish_round(cfg, run["run_id"])
        assert excinfo.value.code == errors.E_RUN_BUSY
    finally:
        release.set()
        holder.join(timeout=5)
    assert os.path.exists(run["run_dir"]), "被拒绝的结束不能顺手删掉记录"
    assert entries_for(cfg, BACKEND_TASK, "占忙模型") == []


# ==========================================================================
# 代表条目选取与用时口径（2026-10-02）
# ==========================================================================

def test_best_of_prefers_measured_time_over_restored_unknown(cfg):
    """同分同轮数：测出真实用时的条目要顶掉没有用时的旧条目。

    背景：手工回填（origin=restored）的旧台账条目没有时间，按旧键它会永远
    压住后来补测的同分条目，排行榜第三排序键（模型用时）落空。
    """
    seed_entry(cfg, BACKEND_TASK, "计时模型", score=100.0, rounds=1,
               work=None, wall=None, run_id="r-old")
    seed_entry(cfg, BACKEND_TASK, "计时模型", score=100.0, rounds=1,
               work=517.0, wall=553.0, run_id="r-new")
    seed_entry(cfg, BACKEND_TASK, "计时模型", score=100.0, rounds=2,
               work=300.0, wall=360.0, run_id="r-new2")

    best = results.best_of(entries_for(cfg, BACKEND_TASK, "计时模型"))
    assert best["source_run_id"] == "r-new", \
        "同分之下轮数少者优先，轮数再同则有时间、用时短者优先"


def test_best_of_still_prefers_higher_score_and_fewer_rounds(cfg):
    """时间只是同分同轮数内部的决胜键，分数与轮数的优先级不变。"""
    seed_entry(cfg, BACKEND_TASK, "口径模型", score=90.0, rounds=1,
               work=100.0, wall=120.0, run_id="r-fast-low")
    seed_entry(cfg, BACKEND_TASK, "口径模型", score=100.0, rounds=2,
               work=900.0, wall=999.0, run_id="r-slow-high")
    best = results.best_of(entries_for(cfg, BACKEND_TASK, "口径模型"))
    assert best["source_run_id"] == "r-slow-high", "分数高者优先，哪怕用时更长"


def test_wall_seconds_unknown_when_best_round_is_not_current(cfg):
    """最高分轮不是当前轮：墙钟置回「未知」，不许拿新一轮起点减出假 0。"""
    run = {
        "run_id": "T-01__多轮__20260101-000000", "task": BACKEND_TASK,
        "model": "多轮模型", "attempt": 2, "status": "graded",
        "created_at": "2026-01-01T00:00:00",
        "round_started_at": "2026-01-01T00:50:00",   # 当前（第 2）轮的起点
        "revealed": False, "calibration": False, "note": "",
        "rounds": [
            {"attempt": 1, "score": 100.0, "passed": True, "invalidated": False,
             "graded_at": "2026-01-01T00:05:00", "model_work_seconds": 300.0,
             "report": "round-1.json"},
            {"attempt": 2, "score": 50.0, "passed": False, "invalidated": False,
             "graded_at": "2026-01-01T01:00:00", "model_work_seconds": 600.0,
             "report": "round-2.json"},
        ],
    }
    entry = runs.record_run_result(cfg, run)
    assert entry is not None
    assert entry["score"] == 100.0 and entry["best_round"] == 1
    assert entry["wall_seconds"] is None, "第 1 轮终点早于第 2 轮起点，差值是假 0，应为未知"
    assert entry["model_work_seconds"] == 300.0


def test_wall_seconds_measured_when_best_round_is_current(cfg):
    """最高分轮就是当前轮：墙钟按正常起点终点计算。"""
    run = {
        "run_id": "T-01__单轮__20260101-000000", "task": BACKEND_TASK,
        "model": "单轮模型", "attempt": 1, "status": "graded",
        "created_at": "2026-01-01T00:00:00",
        "round_started_at": "2026-01-01T00:00:00",
        "revealed": False, "calibration": False, "note": "",
        "rounds": [
            {"attempt": 1, "score": 100.0, "passed": True, "invalidated": False,
             "graded_at": "2026-01-01T00:10:00", "model_work_seconds": 517.0,
             "report": "round-1.json"},
        ],
    }
    entry = runs.record_run_result(cfg, run)
    assert entry is not None
    assert entry["wall_seconds"] == 600.0
    assert entry["model_work_seconds"] == 517.0