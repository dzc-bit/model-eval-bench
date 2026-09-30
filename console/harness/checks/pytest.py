"""pytest checker。

命令形态取自设计文档 §5.4：
    python -m pytest <选定文件> -q -p no:cacheprovider --junitxml=<评分树>\\report.xml
再补两条本机实测出来的硬要求：
    · --basetemp 指向评分树内（不留临时文件到系统盘）
    · PYTHONDONTWRITEBYTECODE=1（不落 __pycache__）
超时由 harness 的 subprocess 统一管理，不依赖 pytest-timeout 插件。

同时提供 JUnit XML 解析：把报告变成 {用例 ID: 结果} 供分组计分。
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .. import util
from . import CaseResult, CheckContext, CheckResult, register


def build_command(ctx: CheckContext, report_xml: str) -> list:
    """拼 pytest 命令行。"""
    argv = [
        sys.executable, "-m", "pytest",
        *ctx.node_ids,
        "-q",
        "-p", "no:cacheprovider",
        "--junitxml=%s" % report_xml,
    ]
    if ctx.tmp_dir:
        argv.append("--basetemp=%s" % os.path.join(ctx.tmp_dir, "pytest-basetemp"))
    return argv


@register("pytest")
def run_pytest(ctx: CheckContext) -> CheckResult:
    """在评分树里跑一批 pytest 用例并解析 JUnit XML。"""
    result = CheckResult(kind="pytest")
    if not ctx.node_ids:
        result.notes.append("本批没有需要跑的用例")
        return result

    os.makedirs(ctx.workdir, exist_ok=True)
    report_xml = os.path.join(ctx.workdir, "report-%d.xml" % ctx.batch)
    result.command = build_command(ctx, report_xml)
    if ctx.batch_total > 1:
        ctx.log("第 %d/%d 批 pytest：%d 个用例" % (ctx.batch + 1, ctx.batch_total, len(ctx.node_ids)))

    proc = util.run_cmd(result.command, cwd=ctx.workdir, env=ctx.env,
                        timeout=ctx.timeout_s, log=ctx.log)
    result.returncode = proc.returncode
    result.duration_s = proc.duration_s
    result.timed_out = proc.timed_out
    result.stdout = proc.stdout
    result.stderr = proc.stderr

    if proc.timed_out:
        result.notes.append("pytest 超过 %d 秒被中止" % ctx.timeout_s)
        # 超时也要尽量保住已产出的报告
    if not os.path.isfile(report_xml):
        result.notes.append("没有产出 JUnit 报告，可能是收集阶段就失败了")
        _mark_all(result, ctx.node_ids, "error", "用例未收集到（详见日志）")
        return result

    try:
        result.cases.update(parse_junit(report_xml))
    except ET.ParseError as exc:
        result.notes.append("JUnit 报告解析失败：%s" % exc)
        _mark_all(result, ctx.node_ids, "error", "报告解析失败")
    if not result.cases:
        _mark_all(result, ctx.node_ids, "error", "报告里没有任何用例记录")
    return result


def _mark_all(result: CheckResult, node_ids: list, outcome: str, message: str) -> None:
    for node_id in node_ids:
        result.cases.setdefault(node_id, CaseResult(node_id=node_id, outcome=outcome, message=message))


# --------------------------------------------------------------------------
# JUnit XML 解析
# --------------------------------------------------------------------------

def parse_junit(report_xml: str) -> Dict[str, CaseResult]:
    """解析 pytest 的 JUnit XML，产出多键索引的用例结果表。

    一个用例会挂多个可查的键：完整 node id、去掉目录的文件名形式、裸函数名。
    分组配置里写 `tests_hidden/x.py::test_y` 还是 `x.py::test_y` 都能查到。
    """
    tree = ET.parse(report_xml)
    root = tree.getroot()
    suites = list(root.iter("testsuite")) if root.tag != "testsuite" else [root]

    cases: Dict[str, CaseResult] = {}
    for suite in suites:
        suite_file = suite.get("file") or ""
        for case in suite.findall("testcase"):
            name = case.get("name") or ""
            classname = case.get("classname") or ""
            duration = _float(case.get("time"))
            outcome, message, detail = _outcome_of(case)
            info = CaseResult(
                node_id="%s::%s" % (suite_file, name) if suite_file else name,
                outcome=outcome, duration=duration, message=message, detail=detail,
            )
            for key in _keys_for(suite_file, classname, name):
                cases.setdefault(key, info)
    return cases


def _keys_for(suite_file: str, classname: str, name: str) -> List[str]:
    keys: List[str] = []
    if suite_file:
        keys.append("%s::%s" % (suite_file, name))
    # classname 形如 pkg.mod 或 pkg.mod.TestCls
    if classname:
        parts = classname.split(".")
        for split in range(len(parts) - 1, -1, -1):
            module_parts = parts[:split]
            cls_parts = parts[split:]
            if not module_parts:
                continue
            module_path = "/".join(module_parts) + ".py"
            tail = "::".join(cls_parts + [name])
            keys.append("%s::%s" % (module_path, tail))
            keys.append("%s::%s" % ("/".join(module_parts) + ".py", name))
    if name:
        keys.append(name)
    seen = set()
    unique = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _outcome_of(case) -> tuple:
    """从 <testcase> 的子元素判定结果。"""
    for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        node = case.find(tag)
        if node is not None:
            message = (node.get("message") or "").strip()
            detail = (node.text or "").strip()
            if not message:
                message = detail.splitlines()[0] if detail else "用例%s" % (
                    "失败" if outcome == "failed" else "报错")
            return outcome, message, detail
    return "passed", "", ""


def _float(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def make_resolver(cases: Dict[str, CaseResult]):
    """把多键索引包成「给一个用例 ID 找结果」的解析器。"""

    def resolve(node_id: str) -> Optional[CaseResult]:
        if not node_id:
            return None
        if node_id in cases:
            return cases[node_id]
        tail = node_id.split("::", 1)[1] if "::" in node_id else node_id
        if tail in cases:
            return cases[tail]
        last = tail.split("::")[-1]
        if last in cases:
            return cases[last]
        # 组配置里可能漏了目录前缀
        if "::" in node_id:
            without_dir = node_id.split("/", 1)[-1]
            if without_dir in cases:
                return cases[without_dir]
        return None

    return resolve
