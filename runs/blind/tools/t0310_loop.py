"""T3-10 快速迭代器（编排侧草稿工具，非交付物）。

只做一件事：按真实 harness 的语义拼一次评分树（含隐藏层），然后在树里直接跑
pytest —— 省掉 packgate 每轮重新拼树 + 全组计分的时间，便于成题期反复试跑。

用法::

    python t0310_loop.py injected                       # 跑全部隐藏用例
    python t0310_loop.py fixed -k cross_process         # 只跑某几个
    python t0310_loop.py injected --rebuild             # 强制重新拼树
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
HARNESS = ROOT / "console"
TOOLS = ROOT / "packs" / "core" / "tools"
GATES = ROOT / "runs" / "blind" / "gates"
TASK = "T3-10"

sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

import selfgrade as sg  # noqa: E402
import packgate as pg  # noqa: E402  # 复用拼树/叠隐藏层（同一套语义）


def build_tree(state: str, rebuild: bool) -> Path:
    dest = GATES / f"{TASK}-{state}"
    if dest.exists() and not rebuild:
        return dest
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    meta = pg._load_meta(TASK)
    task_dir = pg._task_dir(TASK)
    inject = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    if state == "baseline":
        inject = []
    if state == "fixed":
        inject.append(task_dir / "reference" / "fix.patch")
    elif state == "partial":
        inject.append(task_dir / "reference" / "partial.patch")
    pg.build_tree(TASK, meta, dest, inject)
    (dest / ".grade-cache").mkdir(parents=True, exist_ok=True)
    pg._overlay_hidden(dest, meta, (meta.get("checks") or [{}])[0])
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", choices=["baseline", "injected", "fixed", "partial"])
    parser.add_argument("--rebuild", action="store_true")
    args, extra = parser.parse_known_args()

    dest = build_tree(args.state, args.rebuild)
    targets = ["hidden/tests_hidden/test_write_chain.py", *extra]
    command = [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "--no-header"]
    print(f"[loop] tree={dest}")
    print(f"[loop] {' '.join(command)}")
    completed = subprocess.run(command, cwd=str(dest))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
