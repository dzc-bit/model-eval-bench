"""沙箱生命周期：准备 / 清空改动 / 重建 / 盘符池（设计文档 §4.3）。

硬规则（违反会毁掉真实仓库的 node_modules，务必保持）：
    1. 本模块不出现 `del /s`；删沙箱只走 util.remove_tree（整树 rmtree）。
    2. 清空改动只用 `git reset --hard baseline && git clean -fd`，绝不带 -x。
    3. 单独摘联接用 os.rmdir（Python 3.13 对 junction 调 rmtree 会抛 OSError）。
    4. 沙箱 .gitignore 必须原样保留 node_modules/ 行，否则 git clean 会把联接当未跟踪内容删掉。
    5. 受测仓库只读：这里只对它做只读探测（os.path.isdir），从不写入。
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable

from . import errors, packs, snapshot, util

Log = Callable[[str], None]

#: subst 列表输出格式：`Q:\: => D:\some\path`
_SUBST_LINE = re.compile(r"^([A-Za-z]):\\:\s*=>\s*(.+?)\s*$")

#: 进程内串行化盘符池，避免并发准备沙箱时抢同一个盘符
_DRIVE_LOCK = threading.RLock()

_DRIVE_LEASE = ".drive-lease.json"

#: 沙箱里永远不该出现的路径（评测台侧产物）
_FORBIDDEN_IN_SANDBOX = ("packs",)


def _noop(_msg: str) -> None:
    """默认日志回调。"""


# --------------------------------------------------------------------------
# 盘符池
# --------------------------------------------------------------------------

def _subst_codepages() -> list:
    """``subst`` 列表输出来自管道时按 ANSI 码页编码，逐个候选码页给 strict 解码用。

    坑（2026-09-30 实测）：**不能用 ``GetConsoleOutputCP()``**。``启动.cmd`` 会先
    ``chcp 65001``，此后 ``GetConsoleOutputCP()`` 返回 65001，而 ``subst.exe`` 仍然
    按 ANSI 码页（简中 = 936）写字节；拿 UTF-8 去解中文路径得到 ``\\ufffd`` 乱码，
    于是"盘符指向别处"的误报会把每个中文名沙箱都判成环境损坏。

    顺序：ANSI（管道语义）→ OEM → UTF-8 → GBK 兜底。
    """
    candidates: list = []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for probe in ("GetACP", "GetOEMCP"):
            try:
                value = int(getattr(kernel32, probe)())
            except Exception:  # noqa: BLE001 - 探针失败就换下一个
                continue
            if value:
                candidates.append("cp%d" % value)
    except Exception:  # noqa: BLE001 - 非 Windows / ctypes 不可用
        pass
    candidates.extend(["utf-8", "gbk", "cp936"])
    ordered: list = []
    for name in candidates:
        if name not in ordered:
            ordered.append(name)
    return ordered


def _decode_subst(raw: bytes) -> str:
    """按候选码页 strict 解码 ``subst`` 输出，全失败再退宽松解码。"""
    for codepage in _subst_codepages():
        try:
            return raw.decode(codepage)
        except (LookupError, UnicodeDecodeError):
            continue
    return util.decode_output(raw)


def list_subst() -> dict:
    """读取当前所有 subst 映射，返回 {盘符: 真实路径}。

    用 `subst` 的文本输出当真相：指向已删目录的「残留映射」在那里照样列得出来，
    而 os.path.isdir 探针会把它漏掉——漏掉了后面 subst 就会报"已经映射"。
    只有 run lease 能证明归属的中断映射才由恢复流程释放；没有 lease 的映射
    一律视为外部映射并跳过，即使目标目录已经不存在。
    唯一要注意的是列表按 ANSI/OEM 码页编码，码页选错中文路径就会乱码（见
    :func:`_subst_codepages`）。
    """
    import subprocess
    try:
        proc = subprocess.run(["subst"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return {}
    mapping = {}
    text = _decode_subst(proc.stdout)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _SUBST_LINE.match(line)
        if match:
            mapping["%s:" % match.group(1).upper()] = util.norm(match.group(2))
    return mapping


def resolve_drive(drive: str) -> str:
    """盘符当前指向哪里；未映射返回空串。"""
    return list_subst().get(drive.upper(), "")


def _lease_path(run: dict) -> str:
    run_dir = str(run.get("run_dir") or "")
    return os.path.join(run_dir, _DRIVE_LEASE) if run_dir else ""


def _write_drive_lease(run: dict, drive: str, target: str, phase: str) -> None:
    """持久化盘符所有权，确保进程崩溃后能区分本程序映射与用户映射。"""
    path = _lease_path(run)
    if not path:
        return
    util.write_json_atomic(path, {
        "version": 1,
        "run_id": str(run.get("run_id") or ""),
        "drive": drive.upper(),
        "sandbox": util.norm(target),
        "phase": phase,
        "pid": os.getpid(),
    })


def _persist_run_state(run: dict) -> None:
    """尽早保存准备中盘符，避免 run.json 落后于实际 subst 状态。"""
    run_dir = str(run.get("run_dir") or "")
    if not run_dir:
        return
    run["updated_at"] = util.iso_now()
    slim = {key: value for key, value in run.items() if key != "baseline_manifest"}
    util.write_json_atomic(os.path.join(run_dir, "run.json"), slim)


def _same_path(left: str, right: str) -> bool:
    return bool(left and right and util.norm(left).casefold() == util.norm(right).casefold())


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def allocate_drive(cfg: dict, target: str, reserved: dict | None = None,
                   wait_s: float = 0.0, log: Log = _noop,
                   run: dict | None = None) -> str:
    """给沙箱分配一个盘符并建立 subst 映射。

    :param target: 沙箱绝对路径
    :param reserved: {盘符: 已占用的 run_id}，由调用方（记录库）传入
    :param wait_s: 池子暂时用尽时最多等多久（秒）再认输。
        批量跑批会用到：并发闸门按"条目数"放行，而盘符是在条目真正铺沙箱时
        才分配；上一条的盘符回收与下一条的分配之间有毫秒级窗口，直接报
        "盘符池用尽"会让本该成功的条目无辜失败（实测 4 条里挂 1 条）。
        等一小会儿重试即可。默认 0（保持单轮流程的"立刻失败、明确报错"语义）。
    """
    reserved = reserved or {}
    deadline = time.time() + max(0.0, float(wait_s))
    while True:
        try:
            return _allocate_drive_once(cfg, target, reserved, log, run)
        except errors.HarnessError as exc:
            if exc.code != errors.E_DRIVE_UNAVAILABLE or time.time() >= deadline:
                raise
            log("盘符池暂时用尽，%0.1f 秒后重试…" % 0.5)
            time.sleep(0.5)


def _allocate_drive_once(cfg: dict, target: str, reserved: dict, log: Log,
                         run: dict | None = None) -> str:
    """分配一次（不等待）。"""
    with _DRIVE_LOCK:
        current = list_subst()
        for drive in cfg["drive_pool"]:
            existing = current.get(drive)
            if existing:
                lease_path = _lease_path(run) if run else ""
                lease = util.read_json(lease_path, default={}) if lease_path else {}
                lease_owns_existing = (
                    run is not None
                    and _same_path(existing, target)
                    and isinstance(lease, dict)
                    and lease.get("run_id") == run.get("run_id")
                    and str(lease.get("drive") or "").upper() == drive
                    and _same_path(str(lease.get("sandbox") or ""), target)
                    and lease.get("phase") in {"preparing", "ready"}
                )
                record_owns_existing = (
                    run is not None
                    and _same_path(existing, target)
                    and str(run.get("drive") or "").upper() == drive
                    and _same_path(str(run.get("sandbox") or ""), target)
                    and run.get("status") in {"preparing", "ready"}
                )
                owns_existing = lease_owns_existing or record_owns_existing
                if owns_existing:
                    run["sandbox"] = util.norm(target)
                    run["drive"] = drive
                    run["status"] = "preparing"
                    _persist_run_state(run)
                    _write_drive_lease(run, drive, target, "preparing")
                    log("盘符 %s 已由本运行占用，沿用" % drive)
                    return drive
                log("盘符 %s 已被占用（%s），跳过" % (drive, existing))
                continue

            if reserved.get(drive):
                continue

            if run:
                run["sandbox"] = util.norm(target)
                run["drive"] = drive
                run["status"] = "preparing"
                _persist_run_state(run)
                _write_drive_lease(run, drive, target, "preparing")
            result = util.run_cmd(["subst", drive, target], timeout=30)
            if result.ok:
                log("已建立盘符映射 %s → %s" % (drive, target))
                return drive
            log("盘符 %s 映射失败：%s" % (drive, result.tail(2)))

            if run:
                _clear_drive_claim(run, drive, target)
        raise errors.HarnessError(
            errors.E_DRIVE_UNAVAILABLE,
            "盘符池已用尽（%s 都已占用）。请先重建或释放其它沙箱再准备新的沙箱。"
            % "、".join(cfg["drive_pool"]),
            "当前映射：%s" % list_subst(),
        )


def _clear_drive_claim(run: dict, drive: str, target: str) -> None:
    lease_path = _lease_path(run)
    lease = util.read_json(lease_path, default={}) if lease_path else {}
    if isinstance(lease, dict) and (
            str(lease.get("drive") or "").upper() == drive.upper()
            and _same_path(str(lease.get("sandbox") or ""), target)):
        try:
            os.remove(lease_path)
        except OSError:
            pass
    if str(run.get("drive") or "").upper() == drive.upper() and _same_path(
            str(run.get("sandbox") or ""), target):
        run["drive"] = ""
        run["sandbox"] = ""
        _persist_run_state(run)


def release_drive(drive: str, expected_target: str = "", log: Log = _noop) -> bool:
    """仅当盘符仍指向调用者指定的目标时释放；空目标不执行 subst /D。"""
    if not drive:
        return False
    drive = drive.upper()
    if not expected_target:
        log("拒绝释放盘符 %s：缺少预期目标路径" % drive)
        return False
    with _DRIVE_LOCK:
        current = list_subst().get(drive, "")
        if not current:
            return False
        if not _same_path(current, expected_target):
            log("拒绝释放盘符 %s：当前目标 %s 与预期目标不符" % (drive, current))
            return False
        result = util.run_cmd(["subst", drive, "/D"], timeout=30)
        ok = result.ok or "无效参数" in (result.stdout + result.stderr)
        if ok:
            log("已释放盘符 %s" % drive)
        return ok


def recover_interrupted_prepares(cfg: dict, log: Log = _noop) -> int:
    """清理已退出进程遗留的准备中映射；无匹配 lease 的用户映射绝不触碰。"""
    runs_root = cfg.get("runs_root") or ""
    if not runs_root or not os.path.isdir(runs_root):
        return 0
    pool = {str(item).upper() for item in cfg.get("drive_pool") or []}
    recovered = 0
    for dirpath, _dirnames, filenames in os.walk(runs_root):
        if "run.json" not in filenames or _DRIVE_LEASE not in filenames:
            continue
        run_path = os.path.join(dirpath, "run.json")
        lease_path = os.path.join(dirpath, _DRIVE_LEASE)
        run = util.read_json(run_path, default=None)
        lease = util.read_json(lease_path, default=None)
        if not isinstance(run, dict) or not isinstance(lease, dict):
            continue

        run_id = str(run.get("run_id") or "")
        drive = str(lease.get("drive") or "").upper()
        target = util.norm(str(lease.get("sandbox") or ""))
        expected = util.norm(os.path.join(cfg["sandbox_root"], util.sanitize_id(run_id)))
        phase = lease.get("phase")
        if (not run_id or lease.get("run_id") != run_id or drive not in pool
                or phase not in {"preparing", "cleanup_failed"}
                or run.get("status") != "preparing"
                or str(run.get("drive") or "").upper() != drive
                or not _same_path(str(run.get("sandbox") or ""), target)
                or not _same_path(target, expected)
                or not util.path_within(cfg["sandbox_root"], target)):
            continue
        try:
            owner_pid = int(lease.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if phase == "preparing" and _pid_is_alive(owner_pid):
            continue

        mapped = list_subst().get(drive, "")
        if mapped and not _same_path(mapped, target):
            log("恢复时保留盘符 %s：当前映射已指向其它路径 %s" % (drive, mapped))
            continue
        if mapped and not release_drive(drive, expected_target=target, log=log):
            if _same_path(resolve_drive(drive), target):
                log("恢复时无法释放盘符 %s，保留沙箱以避免映射悬空" % drive)
                continue
        if _same_path(resolve_drive(drive), target):
            log("恢复时盘符 %s 仍指向沙箱，保留以避免映射悬空" % drive)
            continue

        try:
            util.remove_tree(target)
            run["drive"] = ""
            run["sandbox"] = ""
            run["status"] = "error"
            run["last_error"] = {
                "code": "interrupted_prepare",
                "message": "服务在准备沙箱时退出，已清理未完成的沙箱。",
            }
            _persist_run_state(run)
            os.remove(lease_path)
        except OSError:
            log("中断沙箱 %s 已释放盘符，但清理记录或目录失败；下次启动会重试" % run_id)
            continue
        recovered += 1
        log("已清理中断的沙箱准备：%s（%s）" % (run_id, drive))
    return recovered


# --------------------------------------------------------------------------
# node_modules 联接
# --------------------------------------------------------------------------

def repo_node_modules(cfg: dict) -> str:
    """受测仓库的 node_modules 实物路径（只读引用，绝不复制）。"""
    return os.path.join(cfg["repo_root"], "node_modules")


def link_node_modules(cfg: dict, sandbox: str, log: Log = _noop) -> dict:
    """在沙箱根建 node_modules 目录联接，指向受测仓库实物。

    mklink /J 不需要管理员权限；联接只让沙箱能读到依赖，不会把内容拷进来。
    """
    target = repo_node_modules(cfg)
    if not os.path.isdir(target):
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "受测仓库里没有 node_modules，前端题无法准备沙箱。请先在仓库里安装依赖。",
            target,
        )
    dest = os.path.join(sandbox, "node_modules")
    if os.path.exists(dest):
        if util.is_junction(dest) and util.junction_target(dest) == util.norm(target):
            return {"path": dest, "target": target, "created": False}
        util.remove_tree(dest)
    result = util.run_cmd(["cmd", "/c", "mklink", "/J", dest, target], timeout=60)
    if not result.ok or not util.is_junction(dest):
        raise errors.HarnessError(
            errors.E_SANDBOX_BROKEN,
            "node_modules 联接创建失败，前端题跑不起来。请点「重建沙箱」重试。",
            result.tail(6),
        )
    log("已复用 node_modules：联接到受测仓库实物（不复制内容）")
    return {"path": dest, "target": target, "created": True}


def check_node_modules(sandbox: str, expected_target: str) -> tuple:
    """校验联接是否完好，返回 (是否正常, 中文说明)。"""
    path = os.path.join(sandbox, "node_modules")
    if not os.path.exists(path):
        return False, "node_modules 联接已丢失"
    if not util.is_junction(path):
        return False, "node_modules 不是联接，可能是模型替换成了真实目录"
    target = util.junction_target(path)
    if util.norm(target) != util.norm(expected_target):
        return False, "node_modules 联接指向了别处：%s" % target
    return True, "node_modules 联接正常"


# --------------------------------------------------------------------------
# 注入（任务包的合成改写）
# --------------------------------------------------------------------------

def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        return util.decode_output(fh.read())


def _apply_patches(sandbox: str, patch_paths: list, log: Log) -> int:
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
        text = _read_text(path)
        name = os.path.basename(path)
        before = _tree_digest(sandbox)
        try:
            touched = _apply_unified(sandbox, text)
            reason = ""
        except errors.HarnessError as exc:
            touched, reason = [], exc.detail or exc.message
        if not touched:
            touched = _apply_with_git(sandbox, path, log)
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


def _apply_with_git(sandbox: str, patch_path: str, log: Log) -> list:
    """兜底：用 `git apply` 应用；把它限定在本目录内，并核对真的改到了东西。"""
    result = util.run_cmd(
        ["git", "-C", sandbox, "apply", "--whitespace=nowarn",
         "--unsafe-paths", "--directory=.", patch_path],
        timeout=120,
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


def _apply_inject_steps(sandbox: str, steps: list, log: Log) -> int:
    """按 inject/apply.json 的有序指令做锚点替换/插入/删除。"""
    applied = 0
    for index, step in enumerate(steps, start=1):
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


def apply_injection(sandbox: str, meta: dict, log: Log = _noop) -> int:
    """把任务包的注入改动打进骨架目录（patches 优先，其次 inject/apply.json）。"""
    patch_paths = packs.list_patches(meta)
    if patch_paths:
        return _apply_patches(sandbox, patch_paths, log)
    steps = packs.load_inject_plan(meta)
    if steps:
        return _apply_inject_steps(sandbox, steps, log)
    log("本题没有注入补丁，按原样骨架准备")
    return 0


def skeleton_for(cfg: dict, meta: dict, log: Log = _noop) -> dict:
    """取这道题的基线骨架：白名单快照 + 任务包注入，按需重建并缓存。

    沙箱准备与评分树拼装都走这里——两边必须拿到同一份"题目起点"，
    否则评分树里会退回未注入的干净代码，注入的缺陷就不见了。
    """
    def injector(dest: str, inner_log: Log) -> int:
        return apply_injection(dest, meta, inner_log)

    path = snapshot.ensure_snapshot(cfg, meta, log, injector=injector)
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

def init_baseline(sandbox: str, log: Log = _noop) -> dict:
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
        result = util.run_cmd(argv, timeout=120)
        if not result.ok:
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "沙箱初始化失败（%s）。请点「重建沙箱」重试。" % desc,
                result.tail(20),
            )
    commit = util.git(sandbox, "rev-parse", "baseline", timeout=30).stdout.strip()
    log("基线提交就绪：%s" % commit[:12])
    return {"commit": commit}


def reset_changes(sandbox: str, log: Log = _noop) -> dict:
    """清空改动：秒级回到 baseline。

    只用 `git clean -fd`（不带 -x）：加 -x 会把被 .gitignore 忽略的 node_modules
    联接当未跟踪内容删掉，前端题当场失效。
    """
    if not os.path.isdir(os.path.join(sandbox, ".git")):
        raise errors.HarnessError(
            errors.E_SANDBOX_MISSING,
            "沙箱还没有准备好，无法清空改动。请先点「准备沙箱」。",
            sandbox,
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
    elapsed = time.time() - started
    log("清空改动完成，用时 %.1fs" % elapsed)
    return {"seconds": round(elapsed, 2), "cleaned": clean.stdout.strip()}


# --------------------------------------------------------------------------
# 完整性自检（设计文档 §4.4 第 6 项）
# --------------------------------------------------------------------------

def needs_frontend(meta: dict) -> bool:
    """这道题是否用到前端（决定要不要建 node_modules 联接）。"""
    for check in meta.get("checks") or []:
        if str(check.get("kind", "")).lower() in {"vitest", "frontend", "node"}:
            return True
    return any(str(p).startswith("frontend") for p in meta.get("allowed_paths") or [])


def verify_integrity(cfg: dict, run: dict, meta: dict) -> list:
    """校验前自检：返回问题列表（空列表表示完好）。

    检查三项（设计文档 §4.3 第 6 条 / §4.4 第 6 项）：
    盘符映射仍指向本沙箱、.gitignore 未被改、node_modules 联接仍在且指向正确。
    """
    problems = []
    sandbox = run.get("sandbox")
    if not sandbox or not os.path.isdir(sandbox):
        return [{"kind": "sandbox_missing", "message": "沙箱目录不存在，请重建沙箱。"}]

    drive = run.get("drive")
    if drive:
        target = resolve_drive(drive)
        if not target:
            problems.append({
                "kind": "subst_missing",
                "message": "盘符 %s 的映射已丢失，请重建沙箱。" % drive,
            })
        elif util.norm(target) != util.norm(sandbox):
            problems.append({
                "kind": "subst_mismatch",
                "message": "盘符 %s 现在指向别处（%s），请重建沙箱。" % (drive, target),
            })

    gitignore = os.path.join(sandbox, ".gitignore")
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
        ok, message = check_node_modules(sandbox, repo_node_modules(cfg))
        if not ok:
            problems.append({"kind": "node_modules_broken", "message": message + "，请重建沙箱。"})

    for name in _FORBIDDEN_IN_SANDBOX:
        if os.path.exists(os.path.join(sandbox, name)):
            problems.append({
                "kind": "sandbox_contaminated",
                "message": "沙箱里出现了任务包目录（%s），说明隔离有洞。请重建沙箱。" % name,
            })
    return problems


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

def prepare(cfg: dict, run: dict, meta: dict, reserved: dict | None = None,
            wait_s: float = 0.0, log: Log = _noop) -> dict:
    """准备沙箱：快照 → 脱敏 → 注入 → git init → subst → 联接（设计文档 §4.3）。

    盘符在 subst 前写入 run 记录和 lease；中途失败会精确释放并清理，
    进程崩溃时由启动恢复接手。

    :param wait_s: 盘符暂时用尽时的等待上限（秒），跑批时用。
    """
    run_id = run["run_id"]
    sandbox = os.path.join(cfg["sandbox_root"], util.sanitize_id(run_id))
    if not util.path_within(cfg["sandbox_root"], sandbox):
        raise errors.HarnessError(errors.E_INTERNAL, "沙箱路径越界，已中止。", sandbox)
    util.remove_tree(sandbox)
    util.ensure_dir(sandbox)
    try:
        return _prepare_into(cfg, run, meta, sandbox, reserved, wait_s, log)
    except Exception as exc:
        drive = str(run.get("drive") or "").upper()
        owned_target = str(run.get("sandbox") or "")
        mapped = resolve_drive(drive) if drive and _same_path(owned_target, sandbox) else ""
        if mapped and _same_path(mapped, sandbox):
            release_drive(drive, expected_target=sandbox, log=log)
            if _same_path(resolve_drive(drive), sandbox):
                # 留下 lease 与沙箱供下一次启动恢复，避免留下指向不存在目录的盘符。
                run["status"] = "preparing"
                try:
                    _write_drive_lease(run, drive, sandbox, "cleanup_failed")
                    _persist_run_state(run)
                except OSError:
                    pass
                raise
        if drive and _same_path(owned_target, sandbox):
            _clear_drive_claim(run, drive, sandbox)
        util.remove_tree(sandbox)
        run["drive"] = ""
        run["sandbox"] = ""
        run["status"] = "error"
        run["last_error"] = {
            "code": getattr(exc, "code", "prepare_failed"),
            "message": getattr(exc, "message", str(exc)),
        }
        _persist_run_state(run)
        raise


def _prepare_into(cfg: dict, run: dict, meta: dict, sandbox: str,
                  reserved: dict | None, wait_s: float, log: Log) -> dict:
    """真正铺沙箱的主体（调用方保证失败时会清场）。"""
    skeleton = skeleton_for(cfg, meta, log)
    copied = util.copy_tree(skeleton["path"], sandbox)
    log("已拷贝基线骨架：%d 个文件" % copied)

    injected = skeleton["injected"]
    baseline = init_baseline(sandbox, log)

    manifest = util.tree_manifest(sandbox)
    digest = util.manifest_digest(manifest)

    drive = allocate_drive(cfg, sandbox, reserved=reserved, wait_s=wait_s,
                           log=log, run=run)

    node_modules = None
    if needs_frontend(meta):
        node_modules = link_node_modules(cfg, sandbox, log)

    gitignore_path = os.path.join(sandbox, ".gitignore")
    gitignore_digest = util.sha256_file(gitignore_path) if os.path.isfile(gitignore_path) else ""

    run["baseline_commit"] = baseline["commit"]
    run["baseline_digest"] = digest
    run["gitignore_digest"] = gitignore_digest
    run["injected"] = injected
    run["node_modules_target"] = node_modules["target"] if node_modules else ""
    run["status"] = "ready"
    run["updated_at"] = util.iso_now()
    # 基线全树清单单独存文件：run.json 只留指纹，避免每轮记录膨胀上百 KB
    if run.get("run_dir"):
        util.write_json_atomic(
            os.path.join(run["run_dir"], "baseline_manifest.json"), manifest)
    _persist_run_state(run)
    _write_drive_lease(run, drive, sandbox, "ready")
    log("沙箱就绪：%s（盘符 %s，%d 个文件）" % (sandbox, drive, len(manifest)))
    return run


def rebuild(cfg: dict, run: dict, meta: dict, reserved: dict | None = None,
            log: Log = _noop) -> dict:
    """重建沙箱：释放盘符 → 整树删除 → 重做全流程（秒级）。"""
    drive, old_sandbox = str(run.get("drive") or ""), str(run.get("sandbox") or "")
    if drive and old_sandbox:
        release_drive(drive, expected_target=old_sandbox, log=log)
        if _same_path(resolve_drive(drive), old_sandbox):
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "旧沙箱盘符无法释放，已保留沙箱以避免映射悬空。",
                drive,
            )
        _clear_drive_claim(run, drive, old_sandbox)
    run["drive"] = ""
    if old_sandbox:
        log("整树删除旧沙箱：%s" % old_sandbox)
        util.remove_tree(old_sandbox)
    run["sandbox"] = ""
    run["status"] = "preparing"
    return prepare(cfg, run, meta, reserved=reserved, log=log)


def destroy(cfg: dict, run: dict, log: Log = _noop) -> None:
    """彻底销毁一个沙箱（删除记录时用）：先释放盘符，再整树删除。"""
    drive, old_sandbox = str(run.get("drive") or ""), str(run.get("sandbox") or "")
    if drive and old_sandbox:
        release_drive(drive, expected_target=old_sandbox, log=log)
        if _same_path(resolve_drive(drive), old_sandbox):
            raise errors.HarnessError(
                errors.E_SANDBOX_BROKEN,
                "沙箱盘符无法释放，已保留沙箱以避免映射悬空。",
                drive,
            )
        _clear_drive_claim(run, drive, old_sandbox)
    run["drive"] = ""
    if old_sandbox and util.path_within(cfg["sandbox_root"], old_sandbox):
        util.remove_tree(old_sandbox)
    run["sandbox"] = ""
    lease_path = _lease_path(run)
    if lease_path:
        try:
            os.remove(lease_path)
        except OSError:
            pass
