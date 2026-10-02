"""盲测沙箱与补丁工具：构建注入态沙箱、按生产口径抽取模型补丁。

配合 `blindprobe.py` 使用：

  python blindtool.py build T3-09 <工作目录>     # 建注入态沙箱（= 模型该看到的那棵树）
  python blindtool.py prep  <工作目录> <轮次>     # 复制出一份干净的作答沙箱
  python blindtool.py diff  T3-09 <工作目录> <轮次>  # 抽补丁（含越界判定）

补丁格式有个坑：目标仓库的最小 diff 应用器（`selfgrade._split_patch`）用
`diff --git` 当**文件分隔符**、用 `+++ b/<path>` 取目标路径。所以必须同时给出
`diff --git a/X b/X` 与带文件名的 `+++`/`---`；只用 `difflib.unified_diff` 的默认
空文件名会把路径冲成空串，导致 `FileNotFoundError: 补丁目标不存在`。
"""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "console"))
from harness import packs as hpacks  # noqa: E402
from harness import util as hutil  # noqa: E402

#: 与生产 `util.ALWAYS_SKIP_DIRS` 一致：这些目录不进全树清单，模型在里面留东西不算改动。
SKIP_DIRS = set(hutil.ALWAYS_SKIP_DIRS) | {".grade-cache"}
#: 与生产 `grade.NOISE_GLOBS` 一致：命中只提示、不作废整轮。
NOISE_GLOBS = [
    "**/__pycache__/**", "**/*.pyc", "**/*.pyo", "**/.pytest_cache/**",
    "**/*.egg-info/**", "**/.coverage", "**/coverage.xml", "**/htmlcov/**",
    "**/*.log", "**/*.tmp", "**/.DS_Store",
]


def files_under(root: Path) -> dict:
    out = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        out[str(rel).replace("\\", "/")] = path
    return out


def build(task: str, work: Path) -> None:
    """建注入态沙箱：直接借 packgate 的建树逻辑，保证与门禁树同源。

    packgate 自己知道受测仓库在哪（题包 meta.repo + 共享快照缓存），所以这里
    不需要传 --repo —— 它没有这个参数。
    """
    packgate = Path(__file__).resolve().parent / "packgate.py"
    command = [sys.executable, str(packgate), "--task", task, "--state", "injected", "--keep"]
    print("执行：%s" % " ".join(command))
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tree = REPO / "runs" / "blind" / "gates" / ("%s-injected" % task)
    if not tree.is_dir():
        print((proc.stdout or "")[-2000:])
        print((proc.stderr or "")[-1000:])
        raise SystemExit("门禁树未生成，无法建沙箱：%s" % tree)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(tree, work, symlinks=True)
    for junk in ("report-0.xml",):
        path = work / junk
        if path.exists():
            path.unlink()
    print("沙箱：%s（%d 个文件）" % (work, len(files_under(work))))


def prep(work: Path, trial_dir: Path) -> None:
    if trial_dir.exists():
        shutil.rmtree(trial_dir, ignore_errors=True)
    shutil.copytree(work, trial_dir, symlinks=True)
    count = len(files_under(trial_dir))
    print("%s（%d 个文件）" % (trial_dir, count))
    if count < 100:
        raise SystemExit("沙箱只有 %d 个文件，明显不完整（应为 200+）——先查是不是被别的进程删过"
                         % count)


def diff(task: str, work: Path, trial_dir: Path, out: Path) -> None:
    left, right = files_under(work), files_under(trial_dir)
    chunks = []
    added, removed = set(right) - set(left), set(left) - set(right)
    for rel in sorted(set(left) | set(right)):
        before, after = left.get(rel), right.get(rel)
        if before is None or after is None:
            continue  # 增/删文件：最小 diff 应用器不支持，留给末尾提示
        a = before.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        b = after.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if a == b:
            continue
        chunks.append("diff --git a/%s b/%s\n" % (rel, rel))
        chunks.extend(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel, n=3))

    patch = "".join(chunks)
    out.write_text(patch, encoding="utf-8")
    touched = sorted({line.split("+++ b/", 1)[1].strip() for line in patch.splitlines()
                      if line.startswith("+++ b/")})

    # 越界判定与生产同口径：allowed_paths 之外任何改动记违规，命中即整轮判红。
    meta = json.loads((REPO / "packs" / "core" / "tasks" / task / "meta.json")
                      .read_text(encoding="utf-8"))
    allowed = [str(p) for p in (meta.get("allowed_paths") or [])]
    outside = [rel for rel in touched
               if not hpacks.allowed_match(rel, allowed)
               and not hutil.match_any(rel, NOISE_GLOBS)]
    if outside:
        out.write_text("", encoding="utf-8")
        print("越界改动 %d 项 → 本轮判无效（0 分，不进统计）：%s" % (len(outside), outside))
        return

    skipped = sorted((added | removed) - set(touched))
    print("补丁 %s：改动 %d 个文件 %s" % (out, len(touched), touched))
    if skipped:
        print("  已跳过（最小 diff 应用器不支持增/删文件）：%s" % skipped)


def main() -> int:
    parser = argparse.ArgumentParser(description="盲测沙箱与补丁工具")
    sub = parser.add_subparsers(dest="action", required=True)

    p_build = sub.add_parser("build", help="建注入态沙箱")
    p_build.add_argument("task")
    p_build.add_argument("work", type=Path)

    p_prep = sub.add_parser("prep", help="复制出一份作答沙箱")
    p_prep.add_argument("work", type=Path)
    p_prep.add_argument("trial_dir", type=Path)

    p_diff = sub.add_parser("diff", help="抽补丁（含越界判定）")
    p_diff.add_argument("task")
    p_diff.add_argument("work", type=Path)
    p_diff.add_argument("trial_dir", type=Path)
    p_diff.add_argument("out", type=Path)

    args = parser.parse_args()
    if args.action == "build":
        build(args.task, args.work)
    elif args.action == "prep":
        prep(args.work, args.trial_dir)
    else:
        diff(args.task, args.work, args.trial_dir, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
