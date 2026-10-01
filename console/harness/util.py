"""harness 通用工具：哈希树、路径安全、子进程、原子写。

只用标准库。所有面向人的文本（注释、错误消息）一律中文；标识符用英文。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

#: 遍历文件树时永远跳过的目录名（答案、依赖缓存、构建产物）
ALWAYS_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".pytest_cache", "node_modules", ".vite",
    ".ruff_cache", ".mypy_cache", ".idea", ".vscode",
})

#: 单次读文件的大小上限，避免误吞超大二进制
MAX_READ_BYTES = 8 * 1024 * 1024

#: Python 3.12+ 的 rmtree 用 onexc，3.11 及以前叫 onerror
_RMTREE_USES_ONEXC = "onexc" in shutil.rmtree.__code__.co_varnames


# --------------------------------------------------------------------------
# 文本与编码
# --------------------------------------------------------------------------

def decode_output(raw: bytes) -> str:
    """把子进程输出解成文本。

    Windows 控制台默认 GBK(cp936)，但 pytest/node 有时输出 UTF-8，
    这里先严格试 UTF-8，失败再退回 GBK 宽松解码，避免中文日志变乱码。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def read_text(path: str) -> str:
    """按 UTF-8 读文本，失败时退回 GBK。"""
    with open(path, "rb") as fh:
        return decode_output(fh.read())


def guess_text(raw: bytes) -> bool:
    """粗判二进制文件（含 NUL 字节即视为二进制）。"""
    if b"\x00" in raw[:4096]:
        return False
    try:
        raw[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            raw[:4096].decode("gbk")
            return True
        except UnicodeDecodeError:
            return False


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------

def norm(path: str) -> str:
    """规范化绝对路径（不解析 junction，仅消除 . 与 .. 与大小写抖动）。"""
    return os.path.normpath(os.path.abspath(path))


def path_within(root: str, candidate: str) -> bool:
    """candidate 是否严格位于 root 之内（root 自身也算）。

    评分树拼装、overlay 复制都要过这道闸：模型可以用 `..` 或绝对路径
    试图让沙箱文件落到评测台之外，必须在复制前就拦掉。
    """
    root_n = norm(root)
    cand_n = norm(candidate)
    if cand_n == root_n:
        return True
    return cand_n.startswith(root_n.rstrip("\\/") + os.sep)


def rel_posix(path: str, root: str) -> str:
    """相对 root 的 POSIX 风格路径（报告、patch、allowed_paths 统一用它）。"""
    return os.path.relpath(norm(path), norm(root)).replace("\\", "/")


def is_junction(path: str) -> bool:
    """判断是否是 Windows 目录联接（junction）。

    Python 3.12 起有 os.path.isjunction；3.11 及以下退回 os.readlink 探测。
    """
    checker = getattr(os.path, "isjunction", None)
    if checker is not None:
        try:
            return bool(checker(path))
        except OSError:
            return False
    try:
        return os.path.islink(path)
    except OSError:
        return False


def junction_target(path: str) -> str:
    """读出联接指向的真实目录；不是联接则返回空串。

    os.readlink 在 Windows 上返回 `\\\\?\\` 前缀的规范路径，剥掉再用。
    """
    if not is_junction(path):
        return ""
    try:
        target = os.readlink(path)
    except OSError:
        return ""
    if target.startswith("\\\\?\\"):
        target = target[4:]
    return norm(target)


def remove_junction(path: str) -> bool:
    """单独移除一个目录联接。

    Python 3.13 对 junction 调 shutil.rmtree 会抛 OSError（见设计文档 §4.3 第 5 条），
    所以这里固定走 os.rmdir——它只删链接本身，不跟随到目标目录。
    """
    if not is_junction(path):
        return False
    os.rmdir(path)
    return True


def _retry_readonly(func, path: str, exc_info) -> None:
    """删不掉时先去掉只读位再重试一次。

    git 的对象文件在工作区里是 444 只读，Windows 上直接 rmtree 会 PermissionError；
    沙箱里满是 .git/objects，不处理的话「重建沙箱」必然失败。
    """
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    try:
        func(path)
    except OSError:
        pass


def remove_tree(path: str) -> None:
    """整树删除一个沙箱根目录。

    设计文档 §4.3 第 4 条：只允许整树删除，绝不 `del /s`（会穿透 junction
    删掉真实 node_modules）。这里先摘掉顶层的 junction，再用 shutil.rmtree；
    实测 Python 3.13 整树 rmtree 不跟随 junction，双保险。
    """
    if not os.path.exists(path):
        return
    if is_junction(path):
        remove_junction(path)
        return
    for name in list(os.listdir(path)):
        child = os.path.join(path, name)
        if os.path.isdir(child) and is_junction(child):
            try:
                os.rmdir(child)
            except OSError:
                pass
    if _RMTREE_USES_ONEXC:
        shutil.rmtree(path, onexc=lambda f, p, e: _retry_readonly(f, p, e))
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: _retry_readonly(f, p, e))


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return norm(path)


#: Windows 文件名里不能用的字符（含路径分隔符），连同控制字符一起换掉
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
#: Windows 保留设备名，做目录名会直接失败
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *("COM%d" % i for i in range(1, 10)),
    *("LPT%d" % i for i in range(1, 10)),
}


def sanitize_id(raw: str, fallback: str = "x") -> str:
    """把用户输入的 ID 收敛成安全目录名（防路径穿越）。

    中文要保留：模型名、题名都是中文，全换成 "-" 会让 runs\\ 下面
    全是 "x-2" 这种没法认的目录名，排查问题全靠猜。这里只干掉
    Windows 不允许的字符与控制字符。
    """
    text = _ILLEGAL_NAME_CHARS.sub("-", str(raw or ""))
    # `..` 是穿越的起点：前导/尾随的点、连字符、下划线一律收干净
    cleaned = re.sub(r"\s+", " ", text).strip(" ._-")
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        return "_" + cleaned
    return cleaned[:64]


# --------------------------------------------------------------------------
# 文件遍历与复制
# --------------------------------------------------------------------------

def match_any(rel: str, patterns: Sequence[str]) -> bool:
    """rel 是否命中任一 glob（patterns 用 '/' 分隔，支持 ** 与 basename 匹配）。"""
    if not patterns:
        return False
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if "/" not in pat and fnmatch.fnmatch(base, pat):
            return True
        # 让 "**/x/**" 也能匹配 "x/y" 这种省略前缀的写法
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


def iter_files(root: str, skip_dirs: Iterable[str] = ALWAYS_SKIP_DIRS,
               follow_junctions: bool = False) -> Iterator[str]:
    """遍历目录下的普通文件，产出绝对路径（POSIX 风格相对路径另行计算）。

    默认不跟随 junction，也不进入依赖缓存目录——评分树与快照都靠这条保证
    不会把 242MB 的 node_modules 拷进沙箱，也不会顺着它写出评测台。
    """
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for name in dirnames:
            if name in skip:
                continue
            full = os.path.join(dirpath, name)
            if not follow_junctions and is_junction(full):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            yield os.path.join(dirpath, name)


def copy_file(src: str, dst: str) -> None:
    ensure_dir(os.path.dirname(dst))
    shutil.copy2(src, dst)


def copy_tree(src: str, dst: str, skip_dirs: Iterable[str] = ALWAYS_SKIP_DIRS) -> int:
    """安全整树复制（跳过 junction 与依赖缓存），返回复制的文件数。"""
    count = 0
    for path in iter_files(src, skip_dirs=skip_dirs):
        rel = rel_posix(path, src)
        copy_file(path, os.path.join(dst, rel.replace("/", os.sep)))
        count += 1
    return count


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: str, skip_dirs: Iterable[str] = ALWAYS_SKIP_DIRS) -> dict:
    """全树哈希清单：{POSIX 相对路径: sha256}。

    这是设计文档 §4.4 第 2 项「全树哈希 diff」的基础，也是「基线哈希」的来源。
    刻意不依赖 git——模型改了 .gitignore 就能让 git 闭嘴，但骗不过这里的文件遍历。
    """
    manifest = {}
    for path in iter_files(root, skip_dirs=skip_dirs):
        rel = rel_posix(path, root)
        try:
            manifest[rel] = sha256_file(path)
        except OSError:
            continue
    return manifest


def manifest_digest(manifest: dict) -> str:
    """把清单压成一个短指纹（目录顺序无关，改一个字节就变）。"""
    digest = hashlib.sha256()
    for rel in sorted(manifest):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(manifest[rel].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def manifest_diff(baseline: dict, current: dict) -> dict:
    """比对两棵树的清单，得出改动分类。"""
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    modified = sorted(
        rel for rel in set(baseline) & set(current)
        if baseline[rel] != current[rel]
    )
    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "changed": sorted(set(added) | set(removed) | set(modified)),
    }


# --------------------------------------------------------------------------
# 子进程
# --------------------------------------------------------------------------

@dataclass
class CmdResult:
    """一次子进程执行的结果。"""

    argv: list = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    output_limited: bool = False

    @property
    def ok(self) -> bool:
        return (self.returncode == 0 and not self.timed_out and not self.cancelled
                and not self.output_limited)

    def tail(self, lines: int = 40) -> str:
        """末尾若干行，用于错误摘要。"""
        body = (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")
        parts = [ln for ln in body.splitlines() if ln.strip()]
        return "\n".join(parts[-lines:])


def run_cmd(argv: Sequence[str], cwd: str | None = None, env: dict | None = None,
            timeout: float | None = 120, log: Callable[[str], None] | None = None,
            stdin_text: str | None = None,
            cancel_event: threading.Event | None = None,
            max_output_bytes: int | None = None) -> CmdResult:
    """跑一条命令并统一管超时。

    设计文档 §4.5：本机没有 pytest-timeout/xdist，超时由 harness 的 subprocess 统一管理。
    超时后先 terminate 再 kill，并如实标记 timed_out，不假装成功。
    """
    argv = [str(a) for a in argv]
    started = time.time()
    if cancel_event is not None and cancel_event.is_set():
        return CmdResult(argv=list(argv), returncode=-2,
                         duration_s=0.0, cancelled=True)
    creation = {}
    if sys.platform == "win32":
        creation["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    elif hasattr(os, "setsid"):
        creation["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **creation,
        )
    except OSError as exc:
        return CmdResult(argv=list(argv), returncode=127, stderr="无法启动进程：%s" % exc,
                         duration_s=time.time() - started)

    if log:
        log("执行：%s" % " ".join(argv))
    cancelled = False
    timed_out = False
    output_limited = False
    watcher_stop = threading.Event()
    watcher = None

    def terminate_tree() -> None:
        """终止命令及其子进程，避免取消后留下 node/pytest 孤儿。"""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                )
            elif hasattr(os, "killpg"):
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def watch_cancel() -> None:
        nonlocal cancelled
        if cancel_event is None:
            return
        while not watcher_stop.wait(0.1):
            if not cancel_event.is_set():
                continue
            if proc.poll() is None:
                cancelled = True
                terminate_tree()
            return

    limit = None
    if max_output_bytes is not None:
        try:
            limit = max(1, int(max_output_bytes))
        except (TypeError, ValueError):
            limit = None
    output_lock = threading.Lock()
    output_used = 0
    output_stop = threading.Event()
    output_buffers = {"stdout": bytearray(), "stderr": bytearray()}

    def read_output(name: str, stream) -> None:
        nonlocal output_used, output_limited
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                with output_lock:
                    if limit is None:
                        output_buffers[name].extend(chunk)
                        continue
                    remaining = limit - output_used
                    if remaining > 0:
                        output_buffers[name].extend(chunk[:remaining])
                        output_used += min(len(chunk), remaining)
                    if len(chunk) > max(0, remaining) and not output_limited:
                        output_limited = True
                        output_stop.set()
                if output_stop.is_set():
                    terminate_tree()
                    return
        except (OSError, ValueError):
            return

    readers = [
        threading.Thread(target=read_output, args=("stdout", proc.stdout), name="stdout-reader", daemon=True),
        threading.Thread(target=read_output, args=("stderr", proc.stderr), name="stderr-reader", daemon=True),
    ]
    for reader in readers:
        reader.start()
    if stdin_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    if cancel_event is not None:
        watcher = threading.Thread(target=watch_cancel, name="cancel-watch", daemon=True)
        watcher.start()
    deadline = None if timeout is None else started + max(0.0, float(timeout))
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                terminate_tree()
                break
            if output_stop.is_set():
                terminate_tree()
                break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                terminate_tree()
                break
            time.sleep(0.02)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            terminate_tree()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        watcher_stop.set()
        if watcher is not None:
            watcher.join(timeout=1)
        for reader in readers:
            reader.join(timeout=2)
    duration = time.time() - started
    result = CmdResult(
        argv=list(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=decode_output(bytes(output_buffers["stdout"])),
        stderr=decode_output(bytes(output_buffers["stderr"])),
        duration_s=duration,
        timed_out=timed_out,
        cancelled=cancelled,
        output_limited=output_limited,
    )
    if log:
        if cancelled:
            log("命令因取消被终止：%s" % argv[0])
        elif timed_out:
            log("命令超时（%.0fs 上限）：%s" % (timeout or 0, argv[0]))
        else:
            log("完成（退出码 %d，用时 %.1fs）：%s" % (result.returncode, duration, argv[0]))
    return result


def git(repo: str, *args: str, timeout: float = 120,
        log: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None) -> CmdResult:
    """在指定仓库里跑 git。"""
    kwargs = {"timeout": timeout, "log": log}
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    return run_cmd(["git", "-C", repo, *args], **kwargs)


# --------------------------------------------------------------------------
# 文件写入
# --------------------------------------------------------------------------

def write_text_atomic(path: str, content: str) -> None:
    """原子写文本：先写临时文件再替换，避免半截文件被前端读到。

    临时名带线程 id：同一进程内两个线程并发写同一路径（如 batch 取消线程与
    准备线程同时写 run.json）时，共享临时名会互相截断再各自 replace。
    """
    ensure_dir(os.path.dirname(path))
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp, path)


def write_json_atomic(path: str, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: str, default: Any = None) -> Any:
    """读 JSON；文件缺失或损坏时返回默认值（读侧要能容忍半成品目录）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def now_stamp() -> str:
    """运行目录用的时间戳：20260929-2015。"""
    return time.strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def disk_free_bytes(path: str) -> int:
    """路径所在卷的可用字节数；路径不存在时向上找最近的存在的祖先。"""
    probe = norm(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return -1
    return int(usage.free)


def human_bytes(num: int) -> str:
    """给日志用的中文体积描述。"""
    if num < 0:
        return "未知"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < step:
            return "%.1f %s" % (num, unit) if unit != "B" else "%d B" % num
        num /= step
    return "%.1f PB" % num
