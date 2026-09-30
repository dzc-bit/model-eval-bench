"""工作区整理：删掉出题期留下的临时树与缓存（编排侧工具，非交付物）。

安全纪律（对齐 packs/core/README.md §六）：

* 每个目标先解析成绝对路径，确认落在工作区根之内才动；
* 目录联接（junction / symlink）先用 ``os.rmdir`` 摘掉——对 junction 本身跑
  ``shutil.rmtree`` 会抛 ``OSError``，而对它跑 ``del /s`` 会穿透删掉真实内容；
* 其余整树用 ``shutil.rmtree``（Python 3.13 不跟进 junction）；
* ``--dry-run`` 只打印不删。

    python tidy_workspace.py --root "D:\\new model test" [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
from pathlib import Path

# 保留：正在被门禁使用的评分树；以及交付物与源码
KEEP = {
    "packs/core/tasks",
    "packs/core/tools/_work",
    "console",
    "runs/blind/tools",
    "runs/blind/calib",
}

JUNK_DIRS = [
    "runs/blind/gates/T1-02-collect",
    "runs/blind/gates/T2-05-collect-baseline",
    "runs/blind/gates/T2-05-collect-injected",
    "runs/blind/gates/T2-06-collect",
    "runs/blind/gates/T2-06-probe",
    "runs/blind/gates/T2-07-collect-baseline",
    "runs/blind/gates/T2-07-collect-injected",
    "runs/blind/gates/T3-08-collect",
    "runs/blind/gates/T3-08-probe",
    "runs/gates",
    "sandboxes/T1-01-recovered",
    "packs/core/tools/_grade",
]

JUNK_GLOBS = [
    "runs/blind/gates/T3-10-t*.txt",
    "runs/blind/gates/T3-10-tmp*.txt",
    "runs/blind/gates/T3-10-visible-*.txt",
    "runs/blind/gates/T3-10-p2p-candidates.txt",
    "runs/blind/gates/T1-02-gate-*.json",
    "runs/blind/gates/T1-02-injected-probe.json",
    "runs/blind/gates/fixed-log.txt",
    "runs/blind/gates/partial-log.txt",
    "runs/blind/gates/probe-log.txt",
    "runs/blind/gates/x20-log.txt",
]

# 缓存目录：整树扫出来再删（跳过正在被门禁使用的评分树）
CACHE_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache"}
SKIP_TREES = {"runs/blind/gates/T3-10-injected"}


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attrs = path.lstat().st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _drop_junctions(root: Path) -> int:
    """先自底向上摘掉树里的联接，再删其余内容。"""
    removed = 0
    for current, dirs, _files in os.walk(root, topdown=False):
        for name in list(dirs):
            child = Path(current) / name
            if _is_link(child):
                try:
                    os.rmdir(child)
                    removed += 1
                except OSError:
                    pass
    return removed


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return target != root


def main() -> int:
    parser = argparse.ArgumentParser(description="清理出题期临时树与缓存")
    parser.add_argument("--root", default=r"D:\new model test")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"工作区不存在：{root}")

    planned: list[Path] = []
    for rel in JUNK_DIRS:
        candidate = (root / rel).resolve()
        if candidate.exists():
            planned.append(candidate)
    for pattern in JUNK_GLOBS:
        planned.extend(sorted(path.resolve() for path in root.glob(pattern)))

    skip = {(root / rel).resolve() for rel in SKIP_TREES}
    caches: list[Path] = []
    for current, dirs, _files in os.walk(root):
        here = Path(current).resolve()
        if any(here == item or item in here.parents for item in skip):
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if not _is_link(Path(current) / name)]
        for name in list(dirs):
            if name in CACHE_NAMES:
                caches.append(Path(current) / name)

    total = 0
    seen: set[Path] = set()
    for target in planned + caches:
        if target in seen or not target.exists():
            continue
        seen.add(target)
        if not _inside(root, target):
            print(f"  [跳过] 越界：{target}")
            continue
        if target.is_dir():
            size = sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
            kind = "目录"
        else:
            size = target.stat().st_size
            kind = "文件"
        total += size
        if args.dry_run:
            print(f"  [dry] {kind} {size / 1024:9.1f} KB  {target.relative_to(root)}")
            continue
        if target.is_dir():
            links = _drop_junctions(target)
            shutil.rmtree(target, ignore_errors=True)
            print(f"  删目录 {size / 1024:9.1f} KB  联接 {links} 个  {target.relative_to(root)}")
        else:
            target.unlink(missing_ok=True)
            print(f"  删文件 {size / 1024:9.1f} KB  {target.relative_to(root)}")
    print(f"合计 {'将释放' if args.dry_run else '已释放'} {total / 1024 / 1024:.1f} MB（{len(planned)} 个目标 + {len(caches)} 个缓存目录）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
