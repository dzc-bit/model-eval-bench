"""第 1 层隔离：白名单快照（设计文档 §4.2）。

做三件事：
1. 只把「跑测试必需的文件」从受测仓库拷进快照——受测仓库全程只读；
2. 应用任务包 meta 的 redactions（删文档里点名不变量的段）与 visible.prune（删会点名的守卫用例）；
3. 生成后全树 grep，命中受测仓库绝对路径或敏感目录名即判打包失败（宁可不出题，也不泄漏答案）。

产物是"基线骨架"= 白名单快照 + 任务包注入。沙箱与评分树共用同一份骨架：
沙箱拿它当起点，评分树拿它当不可改的原始层。注入必须在这一步做完，
否则评分树里会是未注入的干净代码，题目注入的缺陷会凭空消失。
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable

from . import config, errors, packs, util

Log = Callable[[str], None]

#: 段脱敏用的标题正则：`## 9 数据中心…` / `## 9. 数据中心…` / `## 15 架构…`
_SECTION_HEADING = re.compile(r"^(#{1,6})[ \t]*(\d+(?:\.\d+)*)(?:[.、\s]|$)")
#: 版本条目正则：`## 1.5.2 - …` / `## [1.5.2] …` / `### 1.6.0 (2025-01-01)`
_VERSION_HEADING = re.compile(r"^#{1,6}[ \t]*\[?(\d+(?:\.\d+)+)\]?(?![0-9.])")
#: Python 定义行
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
#: JS/TS 测试块起点
_JS_DEF = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:it|test|describe)\s*\(\s*['\"`]([^'\"`]+)['\"`]")


def _noop(_msg: str) -> None:
    """默认日志回调。"""


# --------------------------------------------------------------------------
# 拷贝白名单
# --------------------------------------------------------------------------

def _should_skip(rel: str, cfg_snap: dict) -> str:
    """返回跳过的原因；不跳过则返回空串。"""
    parts = rel.split("/")
    for part in parts[:-1]:
        if part in cfg_snap.get("exclude_dirs", []):
            return "目录在排除名单"
    if parts[0] in cfg_snap.get("exclude_dirs", []):
        return "目录在排除名单"
    if util.match_any(rel, cfg_snap.get("exclude_globs", [])):
        return "命中排除通配"
    if util.match_any(rel, cfg_snap.get("secret_globs", [])):
        return "疑似密钥/凭据文件"
    return ""


def copy_whitelist(cfg: dict, dest: str, log: Log = _noop, meta: dict | None = None) -> dict:
    """按白名单把受测仓库拷进 dest（只读源，绝不写回仓库）。"""
    repo = config.repo_root_for(cfg, meta or {})
    if not os.path.isdir(repo):
        rid = str((meta or {}).get("repo") or {}).get("id", "") if meta else ""
        raise errors.HarnessError(
            errors.E_REPO_UNREADABLE,
            "受测仓库%s在本机不存在（%s）。这道题属于仓库「%s」，"
            "请在 console/config.json 的 repos 里配置它的路径，或把受测仓库拷到本机。"
            % ("「%s」" % rid if rid else "", repo, rid or "默认"),
        )
    cfg_snap = cfg["snapshot"]
    util.ensure_dir(dest)
    copied = 0
    skipped = []

    for entry in cfg_snap.get("include", []):
        entry = str(entry).replace("\\", "/")
        src = os.path.join(repo, *entry.split("/"))
        rel = entry
        if not os.path.exists(src):
            log("跳过 %s：受测仓库里没有这个条目" % entry)
            continue
        if os.path.isfile(src):
            reason = _should_skip(rel, cfg_snap)
            if reason:
                skipped.append((rel, reason))
                continue
            util.copy_file(src, os.path.join(dest, rel.replace("/", os.sep)))
            copied += 1
            continue
        for path in util.iter_files(src, skip_dirs=tuple(cfg_snap.get("exclude_dirs", []))):
            sub = util.rel_posix(path, src)
            full_rel = "%s/%s" % (rel, sub)
            reason = _should_skip(full_rel, cfg_snap)
            if reason:
                skipped.append((full_rel, reason))
                continue
            try:
                if os.path.getsize(path) > cfg_snap.get("max_file_bytes", 4 * 1024 * 1024):
                    skipped.append((full_rel, "单文件超过体积上限"))
                    continue
            except OSError:
                continue
            util.copy_file(path, os.path.join(dest, full_rel.replace("/", os.sep)))
            copied += 1

    for entry in cfg_snap.get("docs_include", []) or []:
        entry = str(entry).replace("\\", "/")
        src = os.path.join(repo, *entry.split("/"))
        if not os.path.isfile(src):
            continue
        reason = _should_skip(entry, cfg_snap)
        if reason:
            skipped.append((entry, reason))
            continue
        util.copy_file(src, os.path.join(dest, entry.replace("/", os.sep)))
        copied += 1

    log("白名单快照：拷贝 %d 个文件，跳过 %d 项" % (copied, len(skipped)))
    return {"copied": copied, "skipped": skipped}


# --------------------------------------------------------------------------
# 脱敏：redactions
# --------------------------------------------------------------------------

def _strip_sections(lines: list, selectors: set, kind: str) -> tuple:
    """按标题删掉若干段（含标题本身，直到下一个同级或更高级标题）。"""
    pattern = _SECTION_HEADING if kind == "section" else _VERSION_HEADING
    out = list(lines)
    drop = [False] * len(out)
    active = False
    active_level = 0
    for idx, line in enumerate(out):
        head = pattern.match(line)
        if head:
            level = len(head.group(1))
            # 段标题的第 2 组是编号，版本标题只有 1 组（版本号本身）
            key = head.group(2) if (head.lastindex or 1) >= 2 else head.group(1)
            if active and level <= active_level:
                active = False
            if key in selectors:
                active = True
                active_level = level
        if active:
            drop[idx] = True
    kept = [line for idx, line in enumerate(out) if not drop[idx]]
    return kept, drop


def _target_files(root: str, spec: str) -> list:
    """把 redaction 的 file 字段解析成实际文件列表（支持 glob）。"""
    spec = str(spec).replace("\\", "/")
    if not any(ch in spec for ch in "*?"):
        path = os.path.join(root, *spec.split("/"))
        return [path] if os.path.isfile(path) else []
    hits = []
    for path in util.iter_files(root):
        rel = util.rel_posix(path, root)
        if util.match_any(rel, [spec]):
            hits.append(path)
    return hits


def apply_redactions(root: str, meta: dict, log: Log = _noop) -> list:
    """应用 meta.redactions；返回实际改动过的文件列表。"""
    touched = []
    for rule in meta.get("redactions") or []:
        spec = str(rule.get("file") or "")
        if not spec:
            continue
        for path in _target_files(root, spec):
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if not util.guess_text(raw):
                continue
            lines = util.decode_output(raw).splitlines(keepends=True)
            before = len(lines)
            for kind, key in (("section", "sections"), ("version", "versions")):
                selectors = {str(x) for x in (rule.get(key) or [])}
                if selectors:
                    lines, _ = _strip_sections(lines, selectors, kind)
            for pattern in rule.get("patterns") or []:
                try:
                    regex = re.compile(str(pattern))
                except re.error:
                    log("脱敏规则 %s 的正则不合法，已跳过：%s" % (spec, pattern))
                    continue
                lines = [ln for ln in lines if not regex.search(ln)]
            if len(lines) != before:
                util.write_text_atomic(path, "".join(lines))
                touched.append(util.rel_posix(path, root))
                log("脱敏 %s：%d 行 → %d 行" % (util.rel_posix(path, root), before, len(lines)))
    return touched


# --------------------------------------------------------------------------
# 裁剪：visible.prune
# --------------------------------------------------------------------------

def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _py_block_spans(lines: list) -> list:
    """找出 .py 文件里所有顶层 def/class 块的 (名字, 起始行, 结束行)。

    起始行含上方**完整**的装饰器（含多行 `@pytest.mark.parametrize(...)`）；
    结束行是该函数体的最后一行。

    优先用 `ast` 拿权威行号：手写的缩进扫描识别不了最后一行是 `)` 的多行装饰器，
    会把装饰器留在文件里、把函数体删掉，产出 `@pytest.mark.parametrize(...)` 后面
    直接 EOF 的残缺文件（pytest 收集即 SyntaxError，整批用例全红）。
    文件语法本身有问题时（裁剪正是要处理这种半成品）退回逐行扫描。
    """
    spans = _py_block_spans_ast(lines)
    if spans is not None:
        return spans
    return _py_block_spans_scan(lines)


def _py_block_spans_ast(lines: list) -> "list | None":
    """用 ast 解析，返回全部顶层 def/class 的行号区间（0 基、左闭右开）。

    语法错误返回 None，由调用方退回扫描实现。
    """
    import ast
    try:
        tree = ast.parse("".join(lines))
    except (SyntaxError, ValueError):
        return None
    spans = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        head = node.lineno - 1
        # 装饰器：取所有装饰器表达式里最早的一行（多行装饰器要求整段一起删）
        for deco in getattr(node, "decorator_list", []) or []:
            head = min(head, getattr(deco, "lineno", head + 1) - 1)
        end = getattr(node, "end_lineno", None)
        if not end:
            continue
        spans.append((node.name, head, end))
    return spans


def _closes_before(lines: list, start: int, limit: int) -> bool:
    """从 start 行的 `@` 起做括号配对，判断装饰器是否在 limit 行之前收尾。

    多行装饰器的参数里可能有嵌套括号与字符串，这里只做字符级配对：
    装饰器表达式在到达 `def` 之前括号配平即认为它属于同一个块。
    """
    depth = 0
    for i in range(start, min(limit, len(lines))):
        for ch in lines[i]:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
        if depth <= 0:
            return True
    return False


def _py_block_spans_scan(lines: list) -> list:
    """逐行扫描兜底实现（语法错误时使用）。

    装饰器识别只认"上方紧邻且以 @ 起头"的行；遇到多行装饰器会保守地
    从 `@` 起头那一行开始吞，直到遇到比 `def` 更浅缩进的非空行。
    """
    spans = []
    n = len(lines)
    idx = 0
    while idx < n:
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            idx += 1
            continue
        if _indent_of(line) != 0:
            idx += 1
            continue
        match = _PY_DEF.match(line)
        if not match:
            idx += 1
            continue
        # 向上找装饰器块的起点：允许装饰器跨多行（参数括号续行，
        # 收尾的 `)` 缩进为 0），也允许它与上面那条语句之间隔着空行/注释。
        head = idx
        back = idx - 1
        while back >= 0:
            stripped = lines[back].strip()
            if not stripped or stripped.startswith("#"):
                back -= 1
                continue
            if stripped.startswith("@"):
                # 从这一行往下做括号配对，确认它就是本函数的装饰器起点
                if _closes_before(lines, back, idx):
                    head = back
                back -= 1
                continue
            # 装饰器参数/列表的续行：括号未配平的行也算装饰器的一部分
            if _indent_of(lines[back]) > 0 or stripped[0] in ")]},":
                back -= 1
                continue
            break
        name = match.group(3)
        end = n
        scan = idx + 1
        while scan < n:
            cur = lines[scan]
            if cur.strip() and _indent_of(cur) == 0:
                end = scan
                break
            scan += 1
        while end > idx + 1 and not lines[end - 1].strip():
            end -= 1
        spans.append((name, head, end))
        idx = end if end > idx else idx + 1
    return spans


def _js_block_span(lines: list, start: int) -> int:
    """从测试函数起点做花括号配对，返回块结束行（含）。"""
    depth = 0
    seen = False
    n = len(lines)
    for i in range(start, n):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
        if seen and depth <= 0:
            return i + 1
    return n


def apply_prune(root: str, meta: dict, log: Log = _noop) -> list:
    """应用 meta.visible.prune：删掉会点名不变量的守卫用例。

    条目格式 `tests/test_x.py::test_foo_*`；没有 `::` 时表示整文件不拷。
    """
    prune = meta.get("visible", {}).get("prune") or []
    if not prune:
        return []
    touched = []
    for entry in prune:
        entry = str(entry).replace("\\", "/")
        if "::" not in entry:
            path = os.path.join(root, *entry.split("/"))
            if os.path.isfile(path):
                os.remove(path)
                touched.append(entry)
                log("裁剪整文件：%s" % entry)
            continue
        file_rel, pattern = entry.split("::", 1)
        path = os.path.join(root, *file_rel.split("/"))
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        if not util.guess_text(raw):
            continue
        lines = util.decode_output(raw).splitlines(keepends=True)
        spans = _py_block_span_any(path, lines, pattern)
        if not spans:
            log("裁剪未命中：%s（检查通配符）" % entry)
            continue
        keep = []
        drop = [False] * len(lines)
        for _, head, end in spans:
            for i in range(head, end):
                drop[i] = True
        for i, line in enumerate(lines):
            if not drop[i]:
                keep.append(line)
        util.write_text_atomic(path, "".join(keep))
        touched.append(file_rel)
        log("裁剪 %s：移除 %d 个用例（%s）" % (file_rel, len(spans), pattern))
    return touched


def _py_block_span_any(path: str, lines: list, pattern: str) -> list:
    """按通配符找出所有要删的块（.py 按 def 块，JS/TS 按花括号块）。"""
    lowered = path.lower()
    if lowered.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        spans = []
        for idx, line in enumerate(lines):
            match = _JS_DEF.search(line)
            if not match:
                continue
            name = match.group(1) or match.group(2) or ""
            if name and util.match_any(name, [pattern]):
                spans.append((name, idx, _js_block_span(lines, idx)))
        return spans
    hits = []
    for name, head, end in _py_block_spans(lines):
        if util.match_any(name, [pattern]):
            hits.append((name, head, end))
    return hits


# --------------------------------------------------------------------------
# 生成后兜底 grep
# --------------------------------------------------------------------------

def assert_no_leak(root: str, literals: list, log: Log = _noop) -> None:
    """全树 grep：命中受测仓库绝对路径/敏感目录名即判打包失败。"""
    if not literals:
        return
    needles = [(text, text.encode("utf-8")) for text in literals]
    hits = []
    for path in util.iter_files(root):
        try:
            with open(path, "rb") as fh:
                raw = fh.read(util.MAX_READ_BYTES)
        except OSError:
            continue
        for text, needle in needles:
            pos = raw.find(needle)
            if pos < 0 and "D:" in text:
                # Windows 下源码里也可能写成正斜杠或转义形式，一并兜底
                alt = text.replace("\\", "/").encode("utf-8")
                if alt != needle:
                    pos = raw.find(alt)
            if pos >= 0:
                rel = util.rel_posix(path, root)
                line_no = raw.count(b"\n", 0, pos) + 1
                hits.append((rel, line_no, text))
    if hits:
        detail = "\n".join("  %s 第 %d 行命中 %r" % h for h in hits[:20])
        raise errors.HarnessError(
            errors.E_LEAK_DETECTED,
            "快照里出现了受测仓库的位置痕迹，这题不能入库。请检查题包文档或源码里是否写了绝对路径。",
            detail,
        )
    log("兜底扫描通过：未发现受测仓库位置痕迹")


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------

def build(cfg: dict, meta: dict, dest: str, log: Log = _noop,
          injector: "Callable[[str, Log], int] | None" = None,
          cancel_event: threading.Event | None = None) -> dict:
    """生成一份基线骨架到 dest（会被覆盖）。

    顺序：白名单拷贝 → 脱敏 → 裁剪 → 注入 → 泄漏兜底。
    注入必须在兜底 grep 之前：补丁本身也可能带出受测仓库的位置痕迹。

    injector 由 sandbox 传入（sandbox.apply_injection），放在这里是为了让
    沙箱与评分树共用同一份"已注入"的骨架——否则评分树里会是未注入的干净代码，
    题目注入的缺陷会凭空消失。
    """
    _raise_if_cancelled(cancel_event)
    log("开始生成基线骨架：%s" % util.rel_posix(dest, cfg["sandbox_root"]))
    util.remove_tree(dest)
    util.ensure_dir(dest)

    stats = copy_whitelist(cfg, dest, log, meta=meta)
    _raise_if_cancelled(cancel_event)
    redacted = apply_redactions(dest, meta, log)
    pruned = apply_prune(dest, meta, log)
    injected = 0
    if injector is not None:
        injected = injector(dest, log)
    _raise_if_cancelled(cancel_event)
    assert_no_leak(dest, cfg["snapshot"].get("forbidden_literals", []), log)

    manifest = util.tree_manifest(dest)
    info = {
        "dest": dest,
        "file_count": len(manifest),
        "bytes": sum(os.path.getsize(os.path.join(dest, *r.split("/"))) for r in manifest)
        if manifest else 0,
        "digest": util.manifest_digest(manifest),
        "redacted": redacted,
        "pruned": pruned,
        "injected": injected,
        "skipped": stats["skipped"],
    }
    log("基线骨架完成：%d 个文件 / %s / 指纹 %s"
        % (info["file_count"], util.human_bytes(info["bytes"]), info["digest"][:12]))
    return info


def _cache_stamp(cfg: dict, meta: dict,
                 cancel_event: threading.Event | None = None) -> str:
    """骨架缓存版本号：受测仓库 HEAD + 白名单配置摘要 + 题包内容摘要。"""
    import hashlib
    import json as _json
    _raise_if_cancelled(cancel_event)
    kwargs = {"timeout": 60}
    if cancel_event is not None:
        kwargs["cancel_event"] = cancel_event
    # 必须显式给 HEAD：裸 `git rev-parse` 是 usage 错误，stdout 恒空 → stamp 恒为
    # "nohead"，受测仓库更新后骨架缓存永远不会失效（E2E 实测踩中）。
    head = util.git(config.repo_root_for(cfg, meta), "rev-parse", "HEAD", **kwargs).stdout.strip()
    blob = _json.dumps(cfg["snapshot"], ensure_ascii=False, sort_keys=True)
    pack_blob = _json.dumps(
        {k: meta.get(k) for k in ("id", "allowed_paths", "redactions", "visible", "repo")},
        ensure_ascii=False, sort_keys=True, default=str)
    pack_digest = hashlib.sha256(pack_blob.encode("utf-8")).hexdigest()[:12]
    # 注入补丁不在 meta 里，单独把补丁目录的内容折进版本号
    patch_digest = ""
    for path in packs.list_patches(meta):
        _raise_if_cancelled(cancel_event)
        try:
            patch_digest += util.sha256_file(path)[:8]
        except OSError:
            patch_digest += "missing"
    # inject/apply.json 是补丁之外的另一条注入通道（packs.load_inject_plan），
    # 不折进版本号的话，改了它也会命中旧缓存，静默按旧注入态出题。
    plan_path = os.path.join(meta["pack_dir"], "inject", "apply.json")
    plan_digest = ""
    if os.path.isfile(plan_path):
        plan_digest = util.sha256_file(plan_path)[:8]
    return "%s|%s|%s|%s|%s" % (
        head[:12] or "nohead",
        hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12],
        pack_digest, patch_digest or "noinject", plan_digest or "noplan")


_BUILD_LOCK = threading.RLock()


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise errors.HarnessError(
            errors.E_RUN_CANCELLED,
            "批次已取消，正在停止骨架准备。",
        )


def ensure_snapshot(cfg: dict, meta: dict, log: Log = _noop,
                    injector: "Callable[[str, Log], int] | None" = None,
                    cancel_event: threading.Event | None = None) -> str:
    """取一份可用的基线骨架（命中缓存就直接复用，否则重建）。

    缓存按「任务 + 白名单版本 + 注入补丁」分目录，沙箱准备、评分树拼装、
    校准排队都走这里，3~4MB 的拷贝因此只在首次付代价。
    """
    # 仓库存在性最先查：stamp/构建都需要 git 和白名单拷贝，仓库不在时给
    # "哪个仓库没配"的业务报错，而不是 git 报错泛化成 E_INTERNAL。
    repo_root = config.repo_root_for(cfg, meta)
    if not os.path.isdir(repo_root):
        rid = str((meta.get("repo") or {}).get("id") or "")
        raise errors.HarnessError(
            errors.E_REPO_UNREADABLE,
            "受测仓库%s在本机不存在（%s）。这道题属于仓库「%s」，请在 console/config.json "
            "的 repos 里配置它的路径，或把受测仓库拷到本机。"
            % ("「%s」" % rid if rid else "", repo_root, rid or "默认"),
        )
    cache_dir = os.path.join(cfg["snapshot_cache"], util.sanitize_id(meta["id"]))
    marker = os.path.join(cache_dir, "_snapshot.json")
    stamp = _cache_stamp(cfg, meta, cancel_event)
    with _BUILD_LOCK:
        _raise_if_cancelled(cancel_event)
        # marker 必须在锁内读：线程 A/B 同时首跑同一题，B 持锁建好缓存返回后，
        # A 若还拿着锁外的 cached=None，会整树 remove_tree 拆掉 B 刚建好的缓存——
        # 而此刻别的调用方可能正在锁外从该目录拷贝骨架。
        cached = util.read_json(marker, default=None)
        if (
            isinstance(cached, dict)
            and cached.get("stamp") == stamp
            and cached.get("task") == meta.get("id")
            and bool(cached.get("injected")) == bool(injector)
            and os.path.isfile(os.path.join(cache_dir, ".keep"))
        ):
            log("命中骨架缓存：%s" % util.rel_posix(cache_dir, cfg["sandbox_root"]))
            return cache_dir

        log("重建骨架缓存（受测仓库、白名单或注入补丁有变动）")
        kwargs = {"injector": injector}
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        info = build(cfg, meta, cache_dir, log, **kwargs)
        util.write_text_atomic(os.path.join(cache_dir, ".keep"), "")
        util.write_json_atomic(marker, {
            "stamp": stamp, "task": meta.get("id"),
            "digest": info["digest"], "file_count": info["file_count"],
            "injected": info["injected"], "built_at": util.iso_now(),
        })
    return cache_dir


def task_meta(cfg: dict, task_id: str) -> dict:
    """便捷入口：读某道题的 meta。"""
    return packs.load_meta(cfg, task_id)
