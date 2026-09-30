"""script checker：在评分树里跑一条命令型守卫。

用途是接住「不是 pytest/vitest 的那一层」——仓库自带的 lint 门禁、类型检查、
打包守卫等。退出码即判分；也可以用 `parse` 里的正则把输出拆成逐条用例，
让分组部分分照样能按条给。
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Dict

from .. import util
from . import CaseResult, CheckContext, CheckResult, register


def _build_command(ctx: CheckContext) -> list:
    """拼命令；相对路径按评分树根解析。"""
    raw = ctx.spec.get("command") or ctx.spec.get("argv") or []
    if isinstance(raw, str):
        argv = shlex.split(raw, posix=False)
    else:
        argv = [str(x) for x in raw]
    if not argv:
        return []
    head = argv[0]
    if head in {"python", "py", "node", "npm", "npx", "ruff", "bash", "cmd"}:
        resolved = shutil_which(head) or head
        argv[0] = resolved
    argv = [util.norm(a) if os.path.isabs(a) else a for a in argv]
    return argv


def shutil_which(name: str):
    import shutil
    return shutil.which(name)


@register("script")
def run_script(ctx: CheckContext) -> CheckResult:
    """跑一条命令型守卫并把退出码/输出折成用例结果。"""
    result = CheckResult(kind="script")
    argv = _build_command(ctx)
    if not argv:
        result.notes.append("script checker 没有配置 command")
        return result

    workdir = os.path.join(ctx.workdir, str(ctx.spec.get("cwd") or "."))
    if not util.path_within(ctx.workdir, workdir):
        result.notes.append("script checker 的 cwd 越出评分树，已拒绝执行")
        return result

    result.command = argv
    proc = util.run_cmd(argv, cwd=workdir, env=ctx.env, timeout=ctx.timeout_s, log=ctx.log)
    result.returncode = proc.returncode
    result.duration_s = proc.duration_s
    result.timed_out = proc.timed_out
    result.stdout = proc.stdout
    result.stderr = proc.stderr

    pattern = ctx.spec.get("parse")
    if pattern:
        try:
            regex = re.compile(str(pattern))
        except re.error as exc:
            result.notes.append("parse 正则不合法，已退回按退出码判分：%s" % exc)
            regex = None
        if regex:
            result.cases.update(_parse_lines(result, ctx.node_ids, regex))
            if result.cases:
                return result

    failed = bool(proc.timed_out) or proc.returncode != 0
    message = "" if not failed else (
        "命令超时" if proc.timed_out else "命令退出码 %d" % proc.returncode)
    for node_id in ctx.node_ids or ["__script__"]:
        result.cases[node_id] = CaseResult(
            node_id=node_id,
            outcome="failed" if failed else "passed",
            message=message,
            detail=proc.tail(40),
        )
    return result


#: 守卫输出里代表"通过"的词
_PASS_WORDS = {"ok", "pass", "passed", "通过", "成功", "true", "yes"}
#: 代表"失败"的词
_FAIL_WORDS = {"fail", "failed", "failure", "失败", "不通过", "false", "no", "error"}


def _split_match(match) -> tuple:
    """从一条匹配里认出「用例名」与「结论」。

    出题 agent 写 parse 正则时两种顺序都见过：
        ^(?:PASS|FAIL)\\s+(\\S+)\\s+(\\S+)$      结论非捕获、只捕名字与细节
        ^(\\S+?)\\s*[:：]\\s*(PASS|FAIL)\\s*$    用例名在前、结论在后
    这里不假定顺序，而是看哪个捕获组落在结论词表里；
    捕获组里都没有，就看整行开头是不是结论词（守卫最常见的输出形态）。
    """
    groups = [g for g in match.groups() if g is not None]
    for index, value in enumerate(groups):
        low = value.strip().lower()
        if low in _PASS_WORDS or low in _FAIL_WORDS:
            names = groups[:index] + groups[index + 1:]
            return (names[0].strip() if names else ""), low
    head = match.group(0).strip().split(None, 1)
    if head and (head[0].lower() in _PASS_WORDS or head[0].lower() in _FAIL_WORDS):
        state = head[0].lower()
        if groups:
            name = groups[0].strip()
        else:
            name = head[1].split(None, 1)[0] if len(head) > 1 else ""
        return name, state
    # 没有结论词：退回「第一组是名字、整行匹配即通过」
    return (groups[0].strip() if groups else ""), ""


def _parse_lines(result: CheckResult, node_ids, regex) -> Dict[str, CaseResult]:
    """按 `用例名: 通过/失败` 之类的输出拆成逐条结果。"""
    cases: Dict[str, CaseResult] = {}
    body = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in body.splitlines():
        match = regex.search(line)
        if not match:
            continue
        name, state = _split_match(match)
        if not name:
            continue
        outcome = "passed" if (not state or state in _PASS_WORDS) else "failed"
        cases[name] = CaseResult(node_id=name, outcome=outcome, message=line.strip())
    # 组里点名但输出里没有的用例一律判红：守卫没提到它，就当没通过
    for node_id in node_ids:
        if node_id in cases:
            continue
        tail = node_id.split("::")[-1]
        if tail in cases:
            cases[node_id] = cases[tail]
            continue
        cases[node_id] = CaseResult(
            node_id=node_id, outcome="failed", message="守卫输出里没有这条用例")
    return cases
