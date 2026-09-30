"""把"原始文件"与"注入版文件"合成标准 unified diff（可被 ``git apply`` 直接消费）。

设计文档 §8 规定注入用 ``inject/patches/*.patch``（unified diff），不发明
``apply.json`` 定制语法。本模块只负责**生成**该 patch，并保证：

* 头部形如 ``diff --git a/<相对路径> b/<相对路径>``，路径以仓库根为基准；
* 正文由标准库 ``difflib.unified_diff`` 产生，行尾统一 ``\\n``；
* 源文件按 UTF-8 读取、UTF-8 无 BOM 写出（受测仓库全树是 UTF-8，Windows 下
  不加 BOM，否则 ``git apply`` 会在首行引入不可见字符）。

只依赖标准库，可在任何 Windows 环境下直接运行：

    python packs/core/tools/mkpatch.py --repo "D:\\New project 6" ^
        --original _orig/backend/x.py --injected _inj/backend/x.py ^
        --rel-path backend/x.py --out packs/core/tasks/T1-01/inject/patches/0001-x.patch

同一批注入可一次生成多个 patch（--pair 可重复），每个文件一个 patch，
顺序即应用顺序。
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


def _read_text(path: Path) -> list[str]:
    """按行读取（保留行尾信息由 splitlines 处理，避免 CRLF 噪声进入 diff）。"""
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _ensure_trailing_newline(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith("\n"):
        return [*lines[:-1], lines[-1] + "\n"]
    return lines


def build_patch(rel_path: str, original: list[str], injected: list[str]) -> str:
    """生成单个文件的 git-apply 兼容 patch 文本。"""
    original = _ensure_trailing_newline(original)
    injected = _ensure_trailing_newline(injected)
    if original == injected:
        return ""
    body = "".join(
        difflib.unified_diff(
            original,
            injected,
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )
    if not body:
        return ""
    return f"diff --git a/{rel_path} b/{rel_path}\n{body}"


def build_patch_from_dirs(pairs: list[tuple[str, Path, Path]]) -> list[tuple[str, str]]:
    """批量生成；返回 ``(相对路径, patch 文本)`` 列表，跳过无差异的文件。"""
    built: list[tuple[str, str]] = []
    for rel_path, original_path, injected_path in pairs:
        text = build_patch(rel_path, _read_text(original_path), _read_text(injected_path))
        if text:
            built.append((rel_path, text))
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成统一注入 patch（git apply 可用）")
    parser.add_argument(
        "--pair",
        action="append",
        required=True,
        metavar="REL:ORIGINAL:INJECTED",
        help="待生成的一组文件，格式 相对路径:原始文件:注入版文件；可重复",
    )
    parser.add_argument("--out-dir", required=True, help="patch 输出目录")
    parser.add_argument("--prefix", default="0001", help="patch 文件名前缀，默认 0001")
    args = parser.parse_args(argv)

    pairs: list[tuple[str, Path, Path]] = []
    for item in args.pair:
        parts = item.split(":")
        if len(parts) != 3:
            parser.error(f"--pair 格式错误：{item}（应为 REL:ORIGINAL:INJECTED）")
        rel_path, original, injected = parts
        pairs.append((rel_path, Path(original), Path(injected)))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    built = build_patch_from_dirs(pairs)
    if not built:
        print("没有差异，未生成 patch。", file=sys.stderr)
        return 1

    for index, (rel_path, text) in enumerate(built, start=1):
        stem = Path(rel_path).name.replace(".py", "")
        name = f"{args.prefix}{index:02d}-{stem}.patch"
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
        changed = sum(1 for line in text.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))
        print(f"已生成 {name}：{rel_path}（{changed} 行增删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
