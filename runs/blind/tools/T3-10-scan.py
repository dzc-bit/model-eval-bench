"""T3-10 成题脚本 检索/生成辅助入口（保留原勘察工具）。

本文件由 T3-10 出题者维护（整理时从 runs/blind/gates/ 移到 runs/blind/tools/，
与其余出题侧工具同处；只读勘察，不写任何文件）。
- cmd=scan  : 输出仓库锚点行（只读勘察）
- 其余命令由 author 分片脚本使用 python -c 直接调用，不落盘。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"D:\New project 6")


def _root() -> Path:
    if "--root" in sys.argv:
        return Path(sys.argv[sys.argv.index("--root") + 1])
    return ROOT


def outline(rel: str) -> str:
    p = _root() / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    out = [f"==== {rel} ({len(lines)} lines)"]
    for i, line in enumerate(lines, 1):
        if re.match(r"^(\s*)(class |def |    def )", line):
            out.append(f"{i:5} {line}")
    return "\n".join(out)


def show(rel: str, a: int, b: int) -> str:
    p = _root() / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    out = [f"==== {rel} [{a}:{b}]"]
    for i in range(a - 1, min(b, len(lines))):
        out.append(f"{i+1:5} {lines[i]}")
    return "\n".join(out)


def grep(rel: str, pattern: str) -> str:
    p = _root() / rel
    lines = p.read_text(encoding="utf-8").splitlines()
    rx = re.compile(pattern)
    out = [f"==== grep {rel} /{pattern}/"]
    for i, line in enumerate(lines, 1):
        if rx.search(line):
            out.append(f"{i:5} {line}")
    return "\n".join(out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if "--root" in args:
        i = args.index("--root")
        args = args[:i] + args[i + 2 :]
    cmd = args[0]
    if cmd == "outline":
        sys.stdout.write(outline(args[1]))
    elif cmd == "show":
        sys.stdout.write(show(args[1], int(args[2]), int(args[3])))
    elif cmd == "grep":
        sys.stdout.write(grep(args[1], args[2]))
