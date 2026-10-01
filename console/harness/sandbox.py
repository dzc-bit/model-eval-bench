"""沙箱生命周期：准备 / 清空改动 / 重建普通文件夹工作区。

硬规则（违反会毁掉真实仓库的 node_modules，务必保持）：
    1. 删除沙箱只走 util.remove_tree（整树 rmtree）。
    2. 清空改动只用 git reset --hard baseline && git clean -fd，绝不带 -x。
    3. node_modules 必须是当前沙箱内的实体副本，不能用 junction 或 symlink 指向外部。
    4. 沙箱 .gitignore 必须原样保留 node_modules/ 行。
    5. 受测仓库只读：这里只对它做只读探测，从不写入。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from typing import Callable

from . import errors, packs, snapshot, util

Log = Callable[[str], None]

#: 沙箱里永远不该出现的路径（评测台侧产物）
_FORBIDDEN_IN_SANDBOX = ("packs",)
_NODE_MODULES_BASELINE = ".node_modules-baseline"


def _noop(_msg: str) -> None:
    """默认日志回调。"""


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    """把批次取消转换成稳定业务异常，供上层及时收尾。"""
    if cancel_event is not None and cancel_event.is_set():
        raise errors.HarnessError(
            errors.E_RUN_CANCELLED,
            "批次已取消，正在停止沙箱准备。",
        )


def _run_cmd(argv: list, *, timeout: float, log: Log | None = None,
             cancel_event: threading.Event | None = None):
    """兼容旧桩的可取消命令入口。"""
    _raise_if_cancelled(cancel_event)
    kwargs = {"timeout": timeout}
    if log is not None:
        kwargs["log"] = log
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    result = util.run_cmd(argv, **kwargs)
    if getattr(result, "cancelled", False):
        _raise_if_cancelled(cancel_event)
    return result


def _persist_run_state(run: dict) -> None:
    """在准备过程中及时保存运行状态，便于服务重启后清理残留工作区。"""
    run_dir = str(run.get("run_dir") or "")
    if not run_dir:
        return
    run["updated_at"] = util.iso_now()
    slim = {key: value for key, value in run.items() if key != "baseline_manifest"}
    util.write_json_atomic(os.path.join(run_dir, "run.json"), slim)


# --------------------------------------------------------------------------
# node_modules 实体副本
# --------------------------------------------------------------------------

def repo_node_modules(cfg: dict) -> str:
    """受测仓库的 node_modules 源路径（只读引用，不作为沙箱工作区）。"""
    return os.path.join(cfg["repo_root"], "node_modules")


def _remove_dependency_path(path: str) -> None:
    """Remove a dependency directory or a dangling link without following it."""
    if not os.path.lexists(path):
        return
    if util.is_junction(path) or os.path.islink(path):
        try:
            os.unlink(path)
        except OSError:
            os.rmdir(path)
        return
    util.remove_tree(path)


def _copy_dependency_tree(source: str, destination: str,
                       cancel_event: threading.Event | None = None) -> dict:
    """复制不含链接的依赖目录，避免把工作区引回外部路径。"""
    if not os.path.isdir(source) or util.is_junction(source) or os.path.islink(source):
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "受测仓库里没有 node_modules，前端题无法准备沙箱。请先在仓库里安装依赖。",
            source,
        )
    if os.path.lexists(destination):
        _remove_dependency_path(destination)
    util.ensure_dir(os.path.dirname(destination))
    util.ensure_dir(destination)
    copied = 0
    try:
        for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
            _raise_if_cancelled(cancel_event)
            rel_dir = os.path.relpath(dirpath, source)
            target_dir = destination if rel_dir == "." else os.path.join(destination, rel_dir)
            util.ensure_dir(target_dir)
            for name in list(dirnames):
                path = os.path.join(dirpath, name)
                if util.is_junction(path) or os.path.islink(path):
                    raise OSError("依赖目录包含链接：%s" % path)
            for name in filenames:
                _raise_if_cancelled(cancel_event)
                path = os.path.join(dirpath, name)
                if util.is_junction(path) or os.path.islink(path):
                    raise OSError("依赖目录包含链接：%s" % path)
                shutil.copy2(path, os.path.join(target_dir, name))
                copied += 1
    except errors.HarnessError:
        util.remove_tree(destination)
        raise
    except OSError as exc:
        util.remove_tree(destination)
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "复制 node_modules 失败，前端题无法准备沙箱。",
            str(exc),
        )
    return {"path": destination, "source": source, "created": True, "files": copied}


def _dependency_dir_problem(path: str, root: str) -> str:
    if not os.path.isdir(path):
        return "node_modules 实体目录已丢失"
    if util.is_junction(path) or os.path.islink(path):
        return "node_modules 不能是 junction 或 symlink"
    root_real = util.norm(os.path.realpath(root))
    if not util.path_within(root_real, os.path.realpath(path)):
        return "node_modules 路径解析后越过目录边界"
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        if any(util.is_junction(os.path.join(dirpath, name)) or os.path.islink(os.path.join(dirpath, name))
               for name in dirnames + filenames):
            return "node_modules 内含链接"
    return ""


def node_modules_baseline(run: dict) -> str:
    """返回位于运行记录目录中的依赖基线；忽略记录里的任意自定义路径。"""
    run_dir = str(run.get("run_dir") or "")
    baseline = os.path.join(run_dir, _NODE_MODULES_BASELINE) if run_dir else ""
    saved = str(run.get("node_modules_baseline") or "")
    if not baseline or not saved or util.norm(saved) != util.norm(baseline):
        return ""
    return baseline


def _capture_node_modules_baseline(cfg: dict, run: dict, log: Log,
                                   cancel_event: threading.Event | None = None) -> str:
    run_dir = str(run.get("run_dir") or "")
    baseline = os.path.join(run_dir, _NODE_MODULES_BASELINE) if run_dir else ""
    if not baseline or not util.path_within(run_dir, baseline):
        raise errors.HarnessError(errors.E_SANDBOX_BROKEN,
                                  "运行记录目录不可用，无法保存 node_modules 本地基线。")
    result = _copy_dependency_tree(repo_node_modules(cfg), baseline, cancel_event)
    run["node_modules_baseline"] = util.norm(baseline)
    log("已保存 node_modules 本地基线：%d 个文件" % result["files"])
    return baseline


def copy_node_modules(source: str, workdir: str, log: Log = _noop,
                      cancel_event: threading.Event | None = None) -> dict:
    """把本地依赖基线复制为 workdir/node_modules 实体目录。"""
    dest = os.path.join(workdir, "node_modules")
    result = _copy_dependency_tree(source, dest, cancel_event)
    log("已复制 node_modules：%d 个文件（实体目录）" % result["files"])
    return result


def check_node_modules(sandbox: str, expected_target: str = "") -> tuple:
    """校验依赖是沙箱内实体目录，返回 (是否正常, 中文说明)。"""
    del expected_target  # 旧调用方曾传入宿主目标；实体副本不接受外部目标。
    path = os.path.join(sandbox, "node_modules")
    problem = _dependency_dir_problem(path, sandbox)
    return (not problem), problem or "node_modules 实体副本正常"


# --------------------------------------------------------------------------
# 注入（任务包的合成改写）
# --------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


def _apply_patches(sandbox: str, patch_paths: list, log: Log,
                   cancel_event: threading.Event | None = None) -> int:
    """按文件名顺序应用 unified diff 补丁。

    真实缺陷（2026-09-30 实测，导致"假注入"）：

    骨架目录在注入阶段还没有 `.git`，`git -C <骨架> apply <补丁>` 会一路向上
    找到**评测台自己的 `.git`**（`D:\\new model test\\.git`）。补丁里的目标路径
    在那个仓库里不存在，git 只打印 "Skipped patch ... 0 files changed"，
    **退出码依然是 0**。旧实现只看退出码 → `applied += 1`，实际一个字符都没改，
    于是 `injected=3` 而骨架是干净代码：题目在未注入的代码上评测，
    注入态该红的组全绿（实测 T1-01 零改动拿 85.7 分），结果完全不可信。

    修法分两层：
      1. **首选自带的内容匹配应用器** `_apply_unified()`：直接按 hunk 上下文
         往文件里打，不依赖任何 git 仓库。
      2. 自带应用器失败时退回 `git apply`（题包契约仍写着"能喂给 git apply"），
         但用 `--directory` 明确根，并在前后比对全树摘要，**必须真的改到文件**
         才算应用成功；否则报错，绝不静默通过。
    """
    applied = 0
    for path in patch_paths:
        _raise_if_cancelled(cancel_event)
        text = _read_text(path)
        name = os.path.basename(path)
        before = _tree_digest(sandbox)
        try:
            touched = _apply_unified(sandbox, text)
            reason = ""
        except errors.HarnessError as exc:
            touched, reason = [], exc.detail or exc.message
        if not touched:
            touched = _apply_with_git(sandbox, path, log, cancel_event)
            if touched:
                reason = ""
        after = _tree_digest(sandbox)
        if not touched or before == after:
            raise errors.HarnessError(
                errors.E_SNAPSHOT_FAILED,
                "注入补丁 %s 没有改动任何文件，这道题当前无法准备沙箱。"
                "（在未注入的干净代码上评测会让题目作废。）" % name,
                reason or "补丁的上下文与骨架不匹配；请核对注入补丁与快照版本。",
            )
        applied += 1
        log("已应用注入补丁：%s（%d 个文件）" % (name, len(touched)))
    return applied


# --------------------------------------------------------------------------
# 自带 unified diff 应用器
# --------------------------------------------------------------------------

_HUNK_HEAD = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified(root: str, patch_text: str) -> list:
    """在 root 下应用一份 unified diff，返回被改动的相对路径列表。

    不依赖 git：按 `diff --git` / `+++` 定位文件，按 hunk 上下文逐行
    校验并替换。上下文不匹配就报错（宁可炸，也不产出半吊子注入）。

    关键语义（真实踩过）：unified diff 里每个 hunk 的 `-<start>` 是它在
    **原始文件**里的行号，不是"打完前面 hunk 之后"的行号。所以必须一边打
    一边累计前面 hunk 造成的行数增量（`delta`），否则第二个 hunk 及其后
    全部错位——`fix.patch` 里 `importer.py` 的第二个 hunk 就是这样被误判成
    上下文不匹配的。
    """
    lines = patch_text.splitlines()
    touched: list = []
    index = 0
    current: "str | None" = None
    pending: list = []          # 当前文件待应用的 hunk：(old_start, body)

    while index < len(lines):
        raw = lines[index]
        line = raw.strip()
        if line.startswith("diff --git "):
            _flush_unified(root, current, pending, touched)
            current, pending = None, []
            index += 1
            continue
        if raw.startswith("--- "):
            index += 1
            continue
        if raw.startswith("+++ "):
            _flush_unified(root, current, pending, touched)
            pending = []
            target = raw[4:].strip()
            if target.startswith(("b/", "a/")):
                target = target[2:]
            current = None if target == "/dev/null" else target.replace("\\", "/")
            index += 1
            continue
        if line.startswith("@@ "):
            if not current:
                # 裸文件名头（无 ---/+++）：hunk 上方那行就是目标路径
                prev = lines[index - 1].strip() if index else ""
                if prev and not prev.startswith("@@"):
                    current = prev.replace("\\", "/")
                if not current:
                    raise errors.HarnessError(
                        errors.E_SNAPSHOT_FAILED, "补丁缺少目标文件路径。",
                        "出现 hunk 之前没有 `+++ b/<path>` 或文件名行。")
            match = _HUNK_HEAD.match(line)
            if not match:
                raise errors.HarnessError(
                    errors.E_SNAPSHOT_FAILED, "无法解析 hunk 头：%r" % raw)
            body: list = []
            cursor = index + 1
            while cursor < len(lines):
                candidate = lines[cursor]
                if candidate.strip().startswith("@@ ") or candidate.startswith("diff --git "):
                    break
                if candidate.startswith("--- ") and not candidate.startswith("----"):
                    break
                if candidate.startswith("\\"):    # \ No newline at end of file
                    cursor += 1
                    continue
                body.append(candidate)
                cursor += 1
            pending.append((int(match.group(1)), body))
            index = cursor
            continue
        index += 1

    _flush_unified(root, current, pending, touched)
    return touched


def _flush_unified(root: str, rel: "str | None", hunks: list, touched: list) -> None:
    """把某个文件累计的 hunk 按序打进文件（一次性写回）。"""
    if not rel or not hunks:
        return
    path = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(path):
        raise errors.HarnessError(
            errors.E_SNAPSHOT_FAILED, "补丁目标文件不存在：%s" % rel)

    with open(path, "rb") as fh:
        raw = fh.read()
    eol = "\r\n" if b"\r\n" in raw[:8192] else "\n"
    original = util.decode_output(raw).splitlines()

    output: list = []
    pos = 0          # 在 original 中已消费到的下标
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        # old_start 是"原始文件"行号；前面 hunk 造成的增删已通过 pos 消化，
        # 这里把 start 夹到当前消费位置之后，避免交叉与负向错位。
        if start < pos:
            start = pos
        output.extend(original[pos:start])
        pos = start
        for line in body:
            marker, content = line[:1], line[1:]
            if marker == " ":
                if pos >= len(original) or original[pos] != content:
                    raise errors.HarnessError(
                        errors.E_SNAPSHOT_FAILED,
                        "%s 第 %d 行上下文不匹配。" % (rel, pos + 1),
                        "期望 %r\n实际 %r" % (
                            content, original[pos] if pos < len(original) else "<EOF>"))
                output.append(content)
                pos += 1
            elif marker == "-":
                if pos >= len(original) or original[pos] != content:
                    raise errors.HarnessError(
                        errors.E_SNAPSHOT_FAILED,
                        "%s 第 %d 行待删内容不匹配。" % (rel, pos + 1),
                        "期望 %r\n实际 %r" % (
                            content, original[pos] if pos < len(original) else "<EOF>"))
                pos += 1
            elif marker == "+":
                output.append(content)
            elif marker == "":
                output.append("")
            else:
                raise errors.HarnessError(
                    errors.E_SNAPSHOT_FAILED, "补丁行缺少 +/-/空格 前缀：%r" % line)
    output.extend(original[pos:])

    util.write_text_atomic(path, eol.join(output) + eol)
    if rel not in touched:
        touched.append(rel)


def _apply_with_git(sandbox: str, patch_path: str, log: Log,
                    cancel_event: threading.Event | None = None) -> list:
    """兜底：用 `git apply` 应用；把它限定在本目录内，并核对真的改到了东西。"""
    result = _run_cmd(
        ["git", "-C", sandbox, "apply", "--whitespace=nowarn",
         "--unsafe-paths", "--directory=.", patch_path],
        timeout=120, cancel_event=cancel_event,
    )
    if not result.ok:
        return []
    # git apply 不支持返回改动清单，靠调用方的摘要比对判断是否成功
    return ["<git apply>"]


def _tree_digest(root: str) -> str:
    """工作区全树摘要（用于判断补丁是否真的改了东西，复用引擎自己的清单实现）。"""
    try:
        return util.manifest_digest(util.tree_manifest(root))
    except OSError:
        return ""


def _apply_inject_steps(sandbox: str, steps: list, log: Log,
                        cancel_event: threading.Event | None = None) -> int:
    """按 inject/apply.json 的有序指令做锚点替换/插入/删除。"""
    applied = 0
    for index, step in enumerate(steps, start=1):
        _raise_if_cancelled(cancel_event)
        op = str(step.get("op") or step.get("kind") or "replace")
        rel = str(step.get("file") or "").replace("\\", "/")
        if not rel:
            raise errors.HarnessError(errors.E_TASK_INVALID, "第 %d 条注入指令缺少 file 字段。" % index)
        path = os.path.join(sandbox, *rel.split("/"))
        if op in {"delete_file", "remove_file"}:
            if not os.path.isfile(path):
                raise errors.HarnessError(
                    errors.E_TASK_INVALID, "注入指令要删的文件不存在：%s" % rel)
            os.remove(path)
            applied += 1
            log("注入 %d：删除文件 %s" % (index, rel))
            continue
        if not os.path.isfile(path):
            raise errors.HarnessError(
                errors.E_TASK_INVALID, "注入指令要改的文件不存在：%s" % rel)
        text = _read_text(path)
        find = str(step.get("find") or step.get("anchor") or "")
        if not find:
            raise errors.HarnessError(errors.E_TASK_INVALID, "第 %d 条注入指令缺少锚点文本。" % index)
        count = text.count(find)
        if count != 1 and int(step.get("expect", 1)) != count:
            raise errors.HarnessError(
                errors.E_TASK_INVALID,
                "注入指令在 %s 里找到 %d 处锚点，锚点不唯一，出题后请调整。" % (rel, count),
                find[:200],
            )
        if op in {"replace", "replace_all"}:
            new_text = text.replace(find, str(step.get("replace") or step.get("text") or ""))
        elif op in {"insert_after", "append_after"}:
            new_text = text.replace(find, find + str(step.get("text") or step.get("replace") or ""), 1)
        elif op in {"insert_before", "prepend_before"}:
            new_text = text.replace(find, str(step.get("text") or step.get("replace") or "") + find, 1)
        elif op in {"delete", "remove"}:
            new_text = text.replace(find, "")
        else:
            raise errors.HarnessError(errors.E_TASK_INVALID, "注入指令 %d 的 op %r 不支持。" % (index, op))
        util.write_text_atomic(path, new_text)
        applied += 1
        log("注入 %d：%s %s" % (index, op, rel))
    return applied


def apply_injection(sandbox: str, meta: dict, log: Log = _noop,
                    cancel_event: threading.Event | None = None) -> int:
    """把任务包的注入改动打进骨架目录（patches 优先，其次 inject/apply.json）。"""
    patch_paths = packs.list_patches(meta)
    if patch_paths:
        return _apply_patches(sandbox, patch_paths, log, cancel_event)
    steps = packs.load_inject_plan(meta)
    if steps:
        return _apply_inject_steps(sandbox, steps, log, cancel_event)
    log("本题没有注入补丁，按原样骨架准备")
    return 0


def skeleton_for(cfg: dict, meta: dict, log: Log = _noop,
                 cancel_event: threading.Event | None = None) -> dict:
    """取这道题的基线骨架：白名单快照 + 任务包注入，按需重建并缓存。

    沙箱准备与评分树拼装都走这里——两边必须拿到同一份"题目起点"，
    否则评分树里会退回未注入的干净代码，注入的缺陷就不见了。
    """
    def injector(dest: str, inner_log: Log) -> int:
        return apply_injection(dest, meta, inner_log, cancel_event)

    _raise_if_cancelled(cancel_event)
    path = snapshot.ensure_snapshot(cfg, meta, log, injector=injector,
                                    cancel_event=cancel_event)
    _raise_if_cancelled(cancel_event)
    marker = util.read_json(os.path.join(path, "_snapshot.json"), default={}) or {}
    return {
        "path": path,
        "digest": marker.get("digest", ""),
        "injected": int(marker.get("injected", 0) or 0),
        "file_count": int(marker.get("file_count", 0) or 0),
    }


# --------------------------------------------------------------------------
# git 基线
# --------------------------------------------------------------------------

def init_baseline(sandbox: str, log: Log = _noop,
                  cancel_event: threading.Event | None = None) -> dict:
    """沙箱内 git init + 单提交 baseline（有 diff 体验、无历史可查）。

    关掉 autocrlf：否则 Windows 上 reset 回来的是 CRLF，
    与「基线哈希」对不上，越界检测会误报。
    """
    steps = [
        (["git", "-C", sandbox, "init", "-q"], "初始化 git"),
        (["git", "-C", sandbox, "config", "core.autocrlf", "false"], "关闭换行符转换"),
        (["git", "-C", sandbox, "config", "user.email", "harness@local"], "设置提交身份"),
        (["git", "-C", sandbox, "config", "user.name", "评测台"], "设置提交身份"),
        (["git", "-C", sandbox, "config", "commit.gpgsign", "false"], "关闭签名"),
        (["git", "-C", sandbox, "add", "-A"], "暂存全部文件"),
        (["git", "-C", sandbox, "commit", "-q", "--no-verify", "-m", "baseline"], "创建基线提交"),
        (["git", "-C", sandbox, "branch", "-f", "baseline"], "建立 baseline 引用"),
    ]
    for argv, desc in steps:
        result = _run_cmd(argv, timeout=120, cancel_event=cancel_event)
        if not result.ok:
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "沙箱初始化失败（%s）。请点「重建沙箱」重试。" % desc,
                result.tail(20),
            )
    kwargs = {"timeout": 30}
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    commit = util.git(sandbox, "rev-parse", "baseline", **kwargs).stdout.strip()
    log("基线提交就绪：%s" % commit[:12])
    return {"commit": commit}


def reset_changes(sandbox: str, log: Log = _noop,
                  dependencies_source: str = "") -> dict:
    """清空改动：秒级回到 baseline。

    只用 `git clean -fd`（不带 -x），然后对前端依赖目录做一次实体副本重建。
    依赖目录被 `.gitignore` 忽略，不能依赖 git reset/clean 恢复模型对它的改动。
    """
    if not os.path.isdir(os.path.join(sandbox, ".git")):
        raise errors.HarnessError(
            errors.E_SANDBOX_MISSING,
            "沙箱还没有准备好，无法清空改动。请先点「准备沙箱」。",
            sandbox,
        )
    dependency_path = os.path.join(sandbox, "node_modules")
    if os.path.lexists(dependency_path) and not dependencies_source:
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "node_modules 本地基线不存在，无法安全清空依赖改动。请重建沙箱。",
        )
    if dependencies_source:
        problem = _dependency_dir_problem(
            dependencies_source, os.path.dirname(dependencies_source))
        if problem:
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "node_modules 本地基线不可用，请重建沙箱。",
                problem,
            )
    started = time.time()
    reset = util.git(sandbox, "reset", "--hard", "baseline", timeout=120, log=log)
    if not reset.ok:
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "清空改动失败，沙箱可能已被外部改动。请点「重建沙箱」重试。",
            reset.tail(20),
        )
    clean = util.git(sandbox, "clean", "-fd", timeout=120, log=log)
    if not clean.ok:
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "清空改动时清理未跟踪文件失败。请点「重建沙箱」重试。",
            clean.tail(20),
        )
    if dependencies_source:
        copy_node_modules(dependencies_source, sandbox, log)
    elapsed = time.time() - started
    log("清空改动完成，用时 %.1fs" % elapsed)
    return {"seconds": round(elapsed, 2), "cleaned": clean.stdout.strip()}


# --------------------------------------------------------------------------
# 文件夹工作区生命周期
# --------------------------------------------------------------------------
#
# 工作区始终是 sandbox_root 下的普通目录，run.drive 保留为空以兼容旧记录。
# 所有模型工具、评分和重启恢复都使用同一个绝对路径。

def _workspace_path(cfg: dict, run_id: str) -> str:
    path = util.norm(os.path.join(cfg["sandbox_root"], util.sanitize_id(run_id)))
    if not util.path_within(cfg["sandbox_root"], path):
        raise errors.HarnessError(errors.E_INTERNAL, "沙箱路径越界，已中止。", path)
    return path


def recover_interrupted_prepares(cfg: dict, log: Log = _noop) -> int:
    """清理服务中断留下的准备中目录，只在 sandbox_root 内操作。"""
    root = cfg.get("runs_root") or ""
    if not os.path.isdir(root):
        return 0
    recovered = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if "run.json" not in filenames:
            continue
        run = util.read_json(os.path.join(dirpath, "run.json"), default=None)
        if not isinstance(run, dict) or run.get("status") != "preparing":
            continue
        sandbox = str(run.get("sandbox") or "")
        if not sandbox or not util.path_within(cfg["sandbox_root"], sandbox):
            continue
        if os.path.isdir(sandbox):
            util.remove_tree(sandbox)
        run_dir = str(run.get("run_dir") or dirpath)
        dependency_baseline = os.path.join(run_dir, _NODE_MODULES_BASELINE)
        if util.path_within(run_dir, dependency_baseline):
            util.remove_tree(dependency_baseline)
        run["sandbox"] = ""
        run["drive"] = ""
        run["node_modules_baseline"] = ""
        run["status"] = "error"
        run["last_error"] = {
            "code": "interrupted_prepare",
            "message": "服务在准备沙箱时退出，已清理未完成的沙箱。",
        }
        run_dir = str(run.get("run_dir") or dirpath)
        run["run_dir"] = run_dir
        _persist_run_state(run)
        recovered += 1
        log("已清理中断的沙箱准备：%s" % run.get("run_id", ""))
    return recovered


def needs_frontend(meta: dict) -> bool:
    """判断题目是否需要复制受测仓库的前端依赖目录。"""
    for check in meta.get("checks") or []:
        if str(check.get("kind", "")).lower() in {"vitest", "frontend", "node"}:
            return True
    return any(str(path).replace("\\", "/").startswith("frontend")
               for path in meta.get("allowed_paths") or [])


def verify_integrity(cfg: dict, run: dict, meta: dict) -> list:
    """校验工作区目录、基线保护文件和实体依赖副本是否仍完整。"""
    problems = []
    workspace = str(run.get("sandbox") or "")
    if not workspace or not os.path.isdir(workspace):
        return [{"kind": "sandbox_missing", "message": "沙箱目录不存在，请重建沙箱。"}]
    if not util.path_within(cfg["sandbox_root"], workspace):
        return [{"kind": "sandbox_escape", "message": "沙箱路径不在评测台工作区内，请重建沙箱。"}]

    gitignore = os.path.join(workspace, ".gitignore")
    if os.path.isfile(gitignore):
        try:
            content = _read_text(gitignore)
        except OSError:
            content = ""
        if "node_modules/" not in content:
            problems.append({
                "kind": "gitignore_modified",
                "message": "沙箱 .gitignore 被改动，node_modules 保护行已丢失。请重建沙箱。",
            })
        digest = util.sha256_file(gitignore)
        if run.get("gitignore_digest") and digest != run["gitignore_digest"]:
            problems.append({
                "kind": "gitignore_modified",
                "message": "沙箱 .gitignore 被外部修改。请重建沙箱。",
            })

    if needs_frontend(meta):
        ok, message = check_node_modules(workspace)
        if not ok:
            problems.append({"kind": "node_modules_broken", "message": message + "，请重建沙箱。"})
        dependency_baseline = node_modules_baseline(run)
        if not dependency_baseline:
            problems.append({
                "kind": "node_modules_broken",
                "message": "node_modules 本地基线不存在或路径无效，请重建沙箱。",
            })
        else:
            baseline_problem = _dependency_dir_problem(
                dependency_baseline, os.path.dirname(dependency_baseline))
            if baseline_problem:
                problems.append({
                    "kind": "node_modules_broken",
                    "message": "node_modules 本地基线不可用：%s，请重建沙箱。" % baseline_problem,
                })
    for name in _FORBIDDEN_IN_SANDBOX:
        if os.path.exists(os.path.join(workspace, name)):
            problems.append({
                "kind": "sandbox_contaminated",
                "message": "沙箱里出现了任务包目录（%s），说明隔离有洞。请重建沙箱。" % name,
            })
    return problems


def prepare(cfg: dict, run: dict, meta: dict, log: Log = _noop,
            cancel_event: threading.Event | None = None) -> dict:
    """在 sandbox_root 内创建一个普通文件夹工作区。"""
    _raise_if_cancelled(cancel_event)
    workspace = _workspace_path(cfg, run["run_id"])
    util.remove_tree(workspace)
    util.ensure_dir(workspace)
    run["sandbox"] = workspace
    run["drive"] = ""
    run["status"] = "preparing"
    _persist_run_state(run)
    try:
        kwargs = {}
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        return _prepare_into_folder(cfg, run, meta, workspace, log, **kwargs)
    except Exception as exc:
        try:
            util.remove_tree(workspace)
            run_dir = str(run.get("run_dir") or "")
            dependency_baseline = os.path.join(run_dir, _NODE_MODULES_BASELINE) if run_dir else ""
            if dependency_baseline and util.path_within(run_dir, dependency_baseline):
                util.remove_tree(dependency_baseline)
        finally:
            run["sandbox"] = ""
            run["drive"] = ""
            run["node_modules_baseline"] = ""
            run["status"] = "error"
            run["last_error"] = {
                "code": getattr(exc, "code", "prepare_failed"),
                "message": getattr(exc, "message", str(exc)),
            }
            _persist_run_state(run)
        raise


def _prepare_into_folder(cfg: dict, run: dict, meta: dict, workspace: str, log: Log,
                         cancel_event: threading.Event | None = None) -> dict:
    _raise_if_cancelled(cancel_event)
    skeleton = skeleton_for(cfg, meta, log, cancel_event)
    copied = util.copy_tree(skeleton["path"], workspace)
    _raise_if_cancelled(cancel_event)
    log("已拷贝基线骨架：%d 个文件" % copied)
    baseline = init_baseline(workspace, log, cancel_event)
    _raise_if_cancelled(cancel_event)
    manifest = util.tree_manifest(workspace)
    digest = util.manifest_digest(manifest)
    node_modules = None
    if needs_frontend(meta):
        dependency_baseline = _capture_node_modules_baseline(cfg, run, log, cancel_event)
        node_modules = copy_node_modules(dependency_baseline, workspace, log, cancel_event)
    gitignore_path = os.path.join(workspace, ".gitignore")
    gitignore_digest = util.sha256_file(gitignore_path) if os.path.isfile(gitignore_path) else ""
    run["baseline_commit"] = baseline["commit"]
    run["baseline_digest"] = digest
    run["gitignore_digest"] = gitignore_digest
    run["injected"] = skeleton["injected"]
    run["node_modules_target"] = ""
    if not node_modules:
        run["node_modules_baseline"] = ""
    run["drive"] = ""
    run["status"] = "ready"
    run["updated_at"] = util.iso_now()
    if run.get("run_dir"):
        util.write_json_atomic(os.path.join(run["run_dir"], "baseline_manifest.json"), manifest)
    _persist_run_state(run)
    log("沙箱就绪：%s（文件夹工作区，%d 个文件）" % (workspace, len(manifest)))
    return run


def rebuild(cfg: dict, run: dict, meta: dict, log: Log = _noop) -> dict:
    """删除当前工作区并在同一个根目录内重新准备。"""
    old = str(run.get("sandbox") or "")
    if old and util.path_within(cfg["sandbox_root"], old):
        util.remove_tree(old)
    run["sandbox"] = ""
    run["drive"] = ""
    run["status"] = "preparing"
    return prepare(cfg, run, meta, log=log)


def destroy(cfg: dict, run: dict, log: Log = _noop) -> None:
    """删除当前工作区目录，不删除评测台外部文件。"""
    del log
    workspace = str(run.get("sandbox") or "")
    if workspace and util.path_within(cfg["sandbox_root"], workspace):
        util.remove_tree(workspace)
    dependency_baseline = node_modules_baseline(run)
    if dependency_baseline:
        util.remove_tree(dependency_baseline)
    run["sandbox"] = ""
    run["drive"] = ""
    run["node_modules_baseline"] = ""
