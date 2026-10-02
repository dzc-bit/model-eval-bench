from __future__ import annotations

import json
from pathlib import Path

from harness import itemstats, results, util


def test_itemstats_cli_summarizes_target_metric_and_group_discrimination(cfg, capsys):
    meta_path = Path(cfg["packs_root"]) / "core" / "tasks" / "TEST-01" / "meta.json"
    meta = util.read_json(str(meta_path), default={})
    meta["calibration"] = {"target_metric": "pass_at_1", "target_band": [0.4, 0.6]}
    util.write_json_atomic(str(meta_path), meta)

    results.append_entry(cfg, results.make_entry(
        "TEST-01", "模型甲", "模型甲", score=100, passed=True, pass1=True,
        rounds=1, groups=[
            {"id": "g1", "port": "第一出口", "weight": 2,
             "passed": True, "total": 2, "passed_count": 2},
            {"id": "g2", "port": "第二出口", "weight": 1,
             "passed": False, "total": 1, "passed_count": 0},
        ],
    ))
    results.append_entry(cfg, results.make_entry(
        "TEST-01", "模型乙", "模型乙", score=50, passed=False, pass1=False,
        rounds=2, groups=[
            {"id": "g1", "port": "第一出口", "weight": 2,
             "passed": False, "total": 2, "passed_count": 1},
            {"id": "g2", "port": "第二出口", "weight": 1,
             "passed": True, "total": 1, "passed_count": 1},
        ],
    ))

    assert itemstats.main([
        "--runs-root", cfg["runs_root"],
        "--packs-root", cfg["packs_root"],
        "--task", "TEST-01",
    ]) == 0
    task = json.loads(capsys.readouterr().out)["tasks"][0]

    assert task["n"] == 2
    assert task["p"] == 0.5
    assert task["p_std"] == 0.7071
    assert task["mean_score"] == 75.0
    assert task["score_std"] == 35.355
    assert task["ceiling"] is True and task["floor"] is False
    assert task["band_status"] == "within_target"
    correlations = {group["id"]: group["point_biserial_r"] for group in task["groups"]}
    assert correlations == {"g1": 1.0, "g2": -1.0}


def test_itemstats_uses_pass_at_3_and_marks_floor():
    task = itemstats.summarize_task("KING-01", [{
        "task": "KING-01", "score": 20, "rounds": 3,
        "passed": False, "pass1": False,
    }], {
        "KING-01": {
            "target_metric": "pass_at_3", "target_band": [0.05, 0.25],
        },
    })

    assert task["p"] == 0.0
    assert task["floor"] is True and task["ceiling"] is False
    assert task["band_status"] == "below_target"
