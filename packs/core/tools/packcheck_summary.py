"""把 packcheck 的 JSON 报告压成"每题红项清单"（整理期的体检表，非交付物）。

    python packcheck_summary.py _work/packcheck.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "_work/packcheck.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for task in payload["tasks"]:
        reds = [f for f in task["findings"] if f["level"] == "红"]
        yellows = [f for f in task["findings"] if f["level"] == "黄"]
        if not reds:
            print(f"{task['task']}: 干净（黄 {len(yellows)}）")
            for f in yellows:
                print("   ~", f["group"], "|", f["message"], "|", f["evidence"][:90])
            continue
        print(f"{task['task']}: 红 {len(reds)}")
        for f in reds:
            print("   -", f["group"], "|", f["message"], "|", f["evidence"][:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
