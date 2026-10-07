"""vitest checker（单文件）。

两处本机实测带来的处理：
1. vitest 的配置 `root` 指向 frontend/，而 appVersion.ts 会去读 `../package.json`——
   所以评分树里 package.json 必须与 frontend/ 同构（设计文档 §5.4）。
2. vitest/vite 默认把缓存写进 `node_modules/.vite`。前端题用联接复用真实
   node_modules，不改缓存目录就会写进受测仓库。本模块在评分树内生成一份
   临时配置把 `cacheDir` 指向评分树，既不改受测仓库的配置文件，也不碰真实依赖。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional

from .. import util
from . import CaseResult, CheckContext, CheckResult, mark_unexecuted, register

#: 评分树内的临时配置：继承原配置，只把缓存目录挪进评分树
_GRADING_CONFIG = """// 评测台生成的临时配置（只存在于评分树内，不属于受测仓库）
// 作用：把 vite/vitest 的缓存目录指到评分树，避免写进联接复用的真实 node_modules。
import base from './%(base)s'

const b: any = base ?? {}
const cacheDir = process.env.GRADE_VITE_CACHE_DIR

export default {
  ...b,
  cacheDir: cacheDir ?? b.cacheDir,
  test: {
    ...(b.test ?? {}),
    cache: { ...(b.test?.cache ?? {}), dir: cacheDir ?? b.test?.cache?.dir },
  },
}
"""


def vitest_entry(workdir: str) -> Optional[str]:
    """定位 vitest 入口脚本（评分树里的 node_modules 联接）。"""
    candidate = os.path.join(workdir, "node_modules", "vitest", "vitest.mjs")
    if os.path.isfile(candidate):
        return candidate
    flat = os.path.join(workdir, "node_modules", "vitest", "dist", "cli.js")
    if os.path.isfile(flat):
        return flat
    return None


def _write_grading_config(workdir: str, base_name: str, log) -> tuple:
    """在评分树内生成 vitest.grading.config.ts，返回 (路径, 是否成功)。"""
    frontend_dir = os.path.join(workdir, "frontend")
    if not os.path.isfile(os.path.join(frontend_dir, base_name)):
        return "", False
    target = os.path.join(frontend_dir, "vitest.grading.config.ts")
    try:
        util.write_text_atomic(target, _GRADING_CONFIG % {"base": base_name})
    except OSError as exc:
        log("生成 vitest 临时配置失败，将改用原配置：%s" % exc)
        return "", False
    return target, True


@register("vitest")
def run_vitest(ctx: CheckContext) -> CheckResult:
    """在评分树里跑一批 vitest 单文件用例。"""
    result = CheckResult(kind="vitest")
    if not ctx.node_ids:
        result.notes.append("本批没有需要跑的用例")
        return result

    node_exe = ctx.env.get("GRADE_NODE") or shutil.which("node")
    if not node_exe:
        result.notes.append("找不到 node 可执行文件，前端题无法校验")
        mark_unexecuted(result, ctx.node_ids, "本机找不到 node，前端用例一条都没跑")
        return result
    entry = vitest_entry(ctx.workdir)
    if not entry:
        result.notes.append("评分树里找不到 vitest（node_modules 联接可能已丢失）")
        mark_unexecuted(result, ctx.node_ids, "评分树里没有 vitest 入口（依赖基线可能丢失）")
        return result

    files = sorted({_file_of(node_id) for node_id in ctx.node_ids if _file_of(node_id)})
    cache_dir = os.path.join(ctx.workdir, ".grade-cache", "vite")
    os.makedirs(cache_dir, exist_ok=True)
    report_json = os.path.join(ctx.workdir, "vitest-report-%d.json" % ctx.batch)
    env = dict(ctx.env)
    env["GRADE_VITE_CACHE_DIR"] = cache_dir

    base_name = str(ctx.spec.get("config") or "vitest.config.ts")
    attempts = []
    generated, ok = _write_grading_config(ctx.workdir, base_name, ctx.log)
    if ok:
        attempts.append((generated, "评分树临时配置（缓存重定向）"))
        attempts.append((os.path.join(ctx.workdir, "frontend", base_name), "原始配置（回退）"))
    else:
        attempts.append((os.path.join(ctx.workdir, "frontend", base_name), "原始配置"))

    for config_path, label in attempts:
        if not os.path.isfile(config_path):
            continue
        argv = [
            node_exe, entry, "run",
            "--config", config_path,
            "--reporter=json",
            "--outputFile=%s" % report_json,
            "--no-color",
            *files,
        ]
        result.command = argv
        ctx.log("vitest（%s）：%d 个文件" % (label, len(files)))
        if os.path.exists(report_json):
            try:
                os.remove(report_json)
            except OSError:
                pass
        proc = util.run_cmd(argv, cwd=ctx.workdir, env=env, timeout=ctx.timeout_s,
                            log=ctx.log, line_log=True)
        result.returncode = proc.returncode
        result.duration_s = proc.duration_s
        result.timed_out = proc.timed_out
        result.stdout = proc.stdout
        result.stderr = proc.stderr
        if os.path.isfile(report_json):
            break
        result.notes.append("「%s」没有产出报告，改用下一种方式重试" % label)
        if proc.timed_out:
            break

    if not os.path.isfile(report_json):
        if result.timed_out:
            result.notes.append("vitest 超过 %d 秒被中止" % ctx.timeout_s)
        else:
            result.notes.append("没有产出 vitest JSON 报告，详见日志")
        mark_unexecuted(result, ctx.node_ids, "用例未运行（vitest 没产出报告，详见日志）")
        return result

    try:
        result.cases.update(parse_vitest_json(report_json, ctx.workdir))
    except (ValueError, OSError) as exc:
        result.notes.append("vitest 报告解析失败：%s" % exc)
        mark_unexecuted(result, ctx.node_ids, "vitest 报告解析失败，用例未运行")
        return result
    # 报告能产出却一条都没有：多半是测试文件在**加载阶段**就失败（import
    # 解析不到、编译错误）——vitest 把原因写在 suite 级 message 里，assertion
    # 一条不给。把它翻出来放进 notes，别让界面只会说「没收集到」；
    # 与 pytest 侧同理，这属于校验故障而不是「模型没修好」。
    if not result.cases:
        cause = _suite_failure_cause(report_json)
        if cause:
            result.notes.append("用例文件加载失败：%s" % cause)
            mark_unexecuted(result, ctx.node_ids, "用例文件导入失败：%s" % cause)
        else:
            result.notes.append("vitest 报告里没有任何用例（多半是没收集到测试文件）")
            mark_unexecuted(result, ctx.node_ids, "vitest 没有收集到任何用例")
    return result


def _suite_failure_cause(report_json: str) -> str:
    """从 vitest JSON 报告里提取 suite 级失败原因；没有就返回空串。

    vitest 对加载失败的测试文件写 ``testResults[].message``（如
    ``Failed to resolve import "…" from "…"``），assertionResults 为空——
    只看用例层的话，故障原因就丢了。
    """
    try:
        with open(report_json, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return ""
    for suite in doc.get("testResults", []) or []:
        if not isinstance(suite, dict):
            continue
        message = str(suite.get("message") or "").strip()
        if message:
            return message.splitlines()[0]
    return ""


def _file_of(node_id: str) -> str:
    """从用例 ID 里取出文件部分（支持 `文件::用例` 与 vitest CLI 的 `文件 > 用例`）。"""
    for sep in ("::", " > ", ">"):
        if sep in node_id:
            return node_id.split(sep)[0].strip()
    return ""


def parse_vitest_json(report_json: str, workdir: str) -> Dict[str, CaseResult]:
    """解析 vitest 的 JSON 报告，产出多键索引的用例结果表。"""
    with open(report_json, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    cases: Dict[str, CaseResult] = {}
    for suite in doc.get("testResults", []) or []:
        rel = str(suite.get("name") or "")
        try:
            rel_posix = util.rel_posix(rel, workdir)
        except ValueError:
            rel_posix = rel
        base = os.path.basename(rel_posix)
        for item in suite.get("assertionResults", []) or []:
            title = str(item.get("title") or "")
            ancestors = [str(a) for a in (item.get("ancestorTitles") or [])]
            status = str(item.get("status") or "unknown")
            outcome = {
                "passed": "passed", "failed": "failed", "pending": "skipped",
                "todo": "skipped", "skipped": "skipped",
            }.get(status, "failed")
            failures = item.get("failureMessages") or []
            message = ""
            if failures:
                first = str(failures[0])
                message = first.strip().splitlines()[0] if first.strip() else "断言失败"
            info = CaseResult(
                node_id=title,
                outcome=outcome,
                duration=float(item.get("duration") or 0.0),
                message=message,
                detail="\n".join(str(f) for f in failures),
            )
            full = " > ".join(ancestors + [title]) if ancestors else title
            for key in (f"{rel_posix}::{full}", f"{rel_posix}::{title}",
                        f"{base}::{title}", title, full):
                cases.setdefault(key, info)
    return cases


def make_resolver(cases: Dict[str, CaseResult]):
    """把 vitest 结果索引包成解析器（同时认 `>` 与 `::` 两种分隔）。"""

    def resolve(node_id: str) -> Optional[CaseResult]:
        if not node_id:
            return None
        for key in _candidate_keys(node_id):
            if key in cases:
                return cases[key]
        return None

    return resolve


def _candidate_keys(node_id: str) -> List[str]:
    keys = [node_id]
    if " > " in node_id:
        head, tail = node_id.split(" > ", 1)
        keys += [f"{head}::{tail}", tail, tail.split(" > ")[-1]]
    elif "::" in node_id:
        head, tail = node_id.split("::", 1)
        keys += [f"{head} > {tail}", tail, tail.split(" > ")[-1]]
    return keys
