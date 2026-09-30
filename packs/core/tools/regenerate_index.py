"""按任务包现状重算 packs/core/index.json（登记表，README §七）。

只做一件事：把每道题的真实交付现状从包内文件里读出来，重新登记，不猜、不手填：

* ``status``：``packcheck --task <ID>`` 无红项 → ``active``，否则保持 ``draft``；
* ``attempts`` / ``title`` / ``tier`` / ``target_band`` 来自 ``meta.json``；
* ``prune_count`` / ``p2p_count`` / ``hidden_groups`` 来自 ``meta.visible.prune``、
  all checker p2p lists、``hidden/groups.json``；
* ``gate`` 来自 ``calibration/gate_fixed.json`` / ``gate_partial.json`` /
  ``gate_injected_x20.json``（缺失则记 ``null``）。

    python regenerate_index.py --pack .. --repo "D:\\New project 6"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _packcheck_reds(pack: Path, repo: Path, task: str) -> int | None:
    completed = subprocess.run(
        [sys.executable, str(TOOLS / "packcheck.py"), "--pack", str(pack), "--task", task,
         "--repo", str(repo), "--out", str(TOOLS / "_work" / f"packcheck-{task}.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    payload = _json(TOOLS / "_work" / f"packcheck-{task}.json")
    if payload is None:
        return None
    return sum(int(item["red"]) for item in payload["tasks"])


def main() -> int:
    parser = argparse.ArgumentParser(description="重算 packs/core/index.json")
    parser.add_argument("--pack", default=str(TOOLS.parent))
    parser.add_argument("--repo", default=r"D:\New project 6")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    pack = Path(args.pack).resolve()
    repo = Path(args.repo).resolve()
    out_path = Path(args.out) if args.out else pack / "index.json"
    index = _json(out_path) or {"schema": 1, "pack": pack.name}
    tasks_dir = pack / "tasks"

    entries = []
    for task_dir in sorted(path for path in tasks_dir.iterdir() if (path / "meta.json").is_file()):
        task = task_dir.name
        meta = _json(task_dir / "meta.json") or {}
        groups = (_json(task_dir / "hidden" / "groups.json") or {}).get("groups", [])
        p2p = _json(task_dir / "p2p.json") or {}
        p2p_count = len(p2p.get("tests") or [])
        for check in meta.get("checks", []):
            if str(check.get("kind") or "pytest").lower() != "vitest":
                continue
            fe_p2p = _json(task_dir / str(check.get("p2p") or "")) or {}
            p2p_count += len(fe_p2p.get("tests") or [])
        fixed = _json(task_dir / "calibration" / "gate_fixed.json")
        partial = _json(task_dir / "calibration" / "gate_partial.json")
        injected = _json(task_dir / "calibration" / "gate_injected_x20.json")

        reds = _packcheck_reds(pack, repo, task)
        entry = {
            "id": task,
            "tier": meta.get("tier"),
            # A draft pack must remain draft until its author explicitly promotes it;
            # draft packcheck intentionally skips production gates and therefore
            # cannot earn active status merely from a zero-red summary.
            "status": "active" if meta.get("status") != "draft" and reds == 0 else "draft",
            "attempts": meta.get("attempts"),
            "title": meta.get("title"),
            "target_band": (meta.get("calibration") or {}).get("target_band"),
            "prune_count": len((meta.get("visible") or {}).get("prune") or []),
            "p2p_count": p2p_count,
            "hidden_groups": [group.get("id") for group in groups],
            "gate": {
                "fixed": None if fixed is None else fixed.get("score_min"),
                "partial": None if partial is None else partial.get("score_min"),
                "injected_x20": None if injected is None else (
                    f"stable {injected.get('score_min')}" if injected.get("stable") else
                    f"flaky {injected.get('score_min')}~{injected.get('score_max')}"
                ),
            },
            "packcheck_red": reds,
        }
        if (meta.get("calibration") or {}).get("target_metric"):
            entry["target_metric"] = meta["calibration"]["target_metric"]
            entry["calibrated"] = meta["calibration"]["calibrated"]
        entries.append(entry)
        print(f"{task}: status={entry['status']} 红={reds} prune={entry['prune_count']} "
              f"p2p={entry['p2p_count']} gate={entry['gate']}")

    index["tasks"] = entries
    notes = index.setdefault("notes", [])
    for note_idx, note in enumerate(notes):
        if str(note).startswith("target_band 是"):
            notes[note_idx] = (
                "target_band 是各题 target_metric 的强模型目标带（既有题目多为 pass@1，"
                "王者为 pass_at_3）。盲测由用户组织的非出题模型完成（§6.4），出题者不校准。"
            )
    if not any("packcheck_red" in note for note in notes):
        notes.append(
            "packcheck_red = 该题当前 packcheck 红项数（由 tools/regenerate_index.py 重算）；"
            "只有红项为 0 的题目登记为 status=active。"
        )
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
