"""列出某题隐藏用例的"是否已被分组引用"，用于判断 coherence 单断言能否低成本补齐。"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

TASKS = Path(r"D:\new model test\packs\core\tasks")


def main() -> int:
    for task in sys.argv[1:] or ["T2-05", "T3-08"]:
        base = TASKS / task
        print("==", task)
        funcs = []
        for path in (base / "hidden").rglob("test_*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    doc = (ast.get_docstring(node) or "").splitlines()
                    funcs.append((path.name, node.name, doc[0][:70] if doc else ""))
        groups = json.loads((base / "hidden" / "groups.json").read_text(encoding="utf-8"))["groups"]
        used = {t.split("::")[-1].split("[")[0] for g in groups for t in g.get("tests", [])}
        print(f"  用例函数 {len(funcs)}，已被引用 {len(used)}")
        for name_file, name, doc in funcs:
            mark = "used" if name in used else "FREE"
            print(f"   [{mark}] {name_file}::{name}  | {doc}")
        print("  组:", [(g["id"], len(g.get("tests", []))) for g in groups])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
