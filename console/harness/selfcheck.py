"""零构建下的静态自检（设计文档 §10.7），可在设置页一键运行。

只用标准库与正则，扫两类问题：
  A. 前端 js 的禁止项——innerHTML 拼业务数据、内联 onclick、setInterval 没配对
     clearInterval、console.log 遗留、硬编码色值、document.write；
  B. 危险命令字样——`del /s` 与 `git clean -fdx`（设计文档 §4.3 / §18）。
     这两个一旦出现就可能穿透 junction 删掉真实仓库的 node_modules。

注意：本文件自己也会被扫，所以危险命令的正则用片段拼出来，源码里不出现完整字样。
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional

if __package__ in (None, ""):
    # 允许 `python console\harness\selfcheck.py` 直接跑（设置页也会 import 它）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from harness import config, util
else:
    from . import config, util

#: 危险命令：拆成片段拼装，避免本文件自己被自己判违规
DANGEROUS_DEL = r"del\s*" + re.escape("/") + r"\s*" + re.escape("/") + r"?\s*s"
DANGEROUS_CLEAN_FDX = r"git\s+clean\b[^\n\"']*?" + re.escape("-") + r"f[a-z]*x"

#: 允许出现危险字样的文件（本文件自己：规则就写在这里）
SELF_PATH = os.path.abspath(__file__)

FRONTEND_EXT = (".js", ".mjs")
#: 危险命令只扫「代码与脚本」：文档里引用这两个词是为了说明它们被禁，不算违规
DANGER_EXT = (".py", ".cmd", ".bat", ".ps1", ".js", ".mjs", ".sh")

#: 事件名（onclick / onchange / onmouseover …），三条事件相关规则共用
_EVENT_NAMES = r"(?:click|change|input|load|error|mouse\w+|key\w+|submit|focus|blur)"


class Rule:
    """一条自检规则。

    :param needs_pairing: 命中后还要满足「同一文件里存在配对清理」才不报，
        用于 setInterval / clearInterval 这类成对出现的 API。
    """

    def __init__(self, rule_id: str, title: str, pattern: str, message: str,
                 level: str = "error", scope: str = "frontend", flags: int = 0,
                 needs_pairing: str = ""):
        self.id = rule_id
        self.title = title
        self.message = message
        self.level = level          # error / warning / info
        self.scope = scope          # frontend / danger
        self.needs_pairing = needs_pairing
        self.regex = re.compile(pattern, flags)


#: 全部规则（顺序即报告里的展示顺序）
RULES: List[Rule] = [
    Rule("inner_html", "innerHTML 拼业务数据", r"\.innerHTML\s*\+?=",
         "禁止用 innerHTML 拼业务数据：日志、diff、报告内容都要用 textContent；"
         "高亮请用结构化分段渲染。", level="error"),
    # 内联事件有两种等价写法，都算违规：
    #   ① HTML 字符串里的 onclick="…" 属性
    #   ② setAttribute('onclick', …) 这种绕一圈的写法
    Rule("inline_onclick_attr", "内联事件属性",
         r"[\"'][^\"'\n]*\bon" + _EVENT_NAMES + r"\s*="
         r"|setAttribute\(\s*[\"']on" + _EVENT_NAMES,
         "禁止内联 onclick 之类的事件属性，事件一律用 addEventListener 绑定、销毁时解绑。"),
    Rule("inline_onclick_js", "JS 内直接赋事件属性",
         r"\.on" + _EVENT_NAMES + r"\s*=",
         "不要用 element.onclick = fn 的写法，请用 addEventListener。"),
    Rule("interval_no_clear", "setInterval 未配对 clearInterval", r"\bsetInterval\s*\(",
         "用了 setInterval 就必须在 destroy() 里配对 clearInterval，否则视图卸载后定时器还在跑。",
         level="error", needs_pairing="clearInterval"),
    Rule("console_log", "console.log 遗留", r"\bconsole\.(log|debug|trace)\s*\(",
         "不要留 console.log；调试信息请走统一的日志封装或直接删掉。", level="warning"),
    Rule("hardcoded_color", "硬编码色值", r"(#[0-9a-fA-F]{3,8}\b)|(\brgba?\s*\()|(\bhsla?\s*\()",
         "颜色要走 css/tokens.css 里的 token 变量，别在 js 里写死色值。", level="warning"),
    Rule("document_write", "document.write", r"\bdocument\s*\.\s*write(?:ln)?\s*\(",
         "禁止 document.write：它是同步阻塞的，且会破坏无障碍树。"),
    Rule("danger_del_s", "危险命令：递归删除", DANGEROUS_DEL,
         "禁止 del /s：它会穿透 junction 删掉真实 node_modules 的内容。删沙箱只许整树 rmtree。",
         scope="danger", flags=re.IGNORECASE),
    Rule("danger_clean_fdx", "危险命令：git clean -fdx", DANGEROUS_CLEAN_FDX,
         "禁止 git clean -fdx：加 -x 会把被 .gitignore 忽略的 node_modules 联接删掉。"
         "清空改动只用 git reset --hard baseline && git clean -fd。",
         scope="danger", flags=re.IGNORECASE),
]


def _iter_js_files(static_root: str) -> List[str]:
    out = []
    js_root = os.path.join(static_root, "js")
    if not os.path.isdir(js_root):
        return out
    for path in util.iter_files(js_root, skip_dirs=("node_modules",)):
        if path.lower().endswith(FRONTEND_EXT):
            out.append(path)
    return out


def _iter_danger_files(cfg: dict) -> List[str]:
    """要扫危险字样的文件：console 下的代码与脚本，外加根目录的启动.cmd。

    刻意不扫 packs/、runs/、sandboxes/ 与设计文档——那里出现这两个词是正常的
    （题包说明、违规记录、规则原文都会提到它们）。
    """
    out = []
    for path in util.iter_files(config.CONSOLE_DIR,
                                skip_dirs=("node_modules", "__pycache__", ".git", "static")):
        if path.lower().endswith(DANGER_EXT):
            out.append(path)
    launcher = os.path.join(config.EVAL_ROOT, "启动.cmd")
    if os.path.isfile(launcher):
        out.append(launcher)
    return sorted(set(out))


def _read_lines(path: str) -> Optional[List[str]]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not util.guess_text(raw):
        return None
    return util.decode_output(raw).splitlines()


def _python_code_lines(path: str) -> Optional[set]:
    """用 tokenize 找出真正会被执行的代码行号（排除注释与字符串）。

    规则本身必须写在注释/文档字符串里（"禁止 del /s"），所以扫危险字样时
    要跳过它们，否则规则定义处永远自己告自己。注释里描述禁令是安全的。
    """
    import tokenize
    code = set()
    try:
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL,
                                tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                                tokenize.ENDMARKER):
                    continue
                for line_no in range(tok.start[0], tok.end[0] + 1):
                    code.add(line_no)
    except (SyntaxError, OSError, UnicodeDecodeError, tokenize.TokenError):
        return None
    return code


def _is_comment(path: str, line: str) -> bool:
    """非 Python 文件的注释行判断。"""
    text = line.strip()
    if not text:
        return True
    low = path.lower()
    if low.endswith((".cmd", ".bat")):
        return text.lower().startswith("rem ") or text.lower().startswith("::") or text.lower() == "rem"
    if low.endswith((".js", ".mjs")):
        return text.startswith(("//", "/*", "*"))
    if low.endswith(".ps1"):
        return text.startswith("#")
    return False


def _scan_file(path: str, rules: List[Rule], static_root: str) -> List[dict]:
    lines = _read_lines(path)
    if lines is None:
        return []
    body = "\n".join(lines)
    is_python = path.lower().endswith(".py")
    is_danger = any(rule.scope == "danger" for rule in rules)
    # Python 用 tokenize 精确剔除注释与字符串；其它语言按行首符号粗判。
    # 前端 js 也要剔注释：规则说明里常写「这里不用 innerHTML」，那不是违规。
    code_lines = _python_code_lines(path) if (is_danger and is_python) else None
    issues = []
    for index, line in enumerate(lines, start=1):
        if code_lines is not None:
            if index not in code_lines:
                continue
        elif _is_comment(path, line):
            continue
        for rule in rules:
            match = rule.regex.search(line)
            if not match:
                continue
            suffix = ""
            if rule.needs_pairing and rule.needs_pairing not in body:
                suffix = "（本文件没有出现 %s）" % rule.needs_pairing
            elif rule.needs_pairing:
                continue          # 配对清理在，别报
            issues.append(_issue(rule, path, index, line, static_root,
                                 excerpt=line[max(0, match.start() - 30):match.end() + 30],
                                 suffix=suffix))
    return issues


def _issue(rule: Rule, path: str, line_no: int, line: str, static_root: str,
           excerpt: str = "", suffix: str = "") -> dict:
    return {
        "rule": rule.id,
        "title": rule.title,
        "level": rule.level,
        "message": rule.message + suffix,
        "file": _display(path, static_root),
        "line": line_no,
        "excerpt": (excerpt or line).strip()[:200],
    }


def _display(path: str, static_root: str) -> str:
    for root, label in ((static_root, "static"), (config.EVAL_ROOT, "")):
        if root and util.path_within(root, path):
            rel = util.rel_posix(path, root)
            return "%s/%s" % (label, rel) if label else rel
    return path


def scan(cfg: Optional[dict] = None) -> dict:
    """跑一遍自检，返回中文报告（前端与危险命令两段）。"""
    cfg = cfg or config.load()
    static_root = cfg.get("static_root", os.path.join(config.CONSOLE_DIR, "static"))

    issues: List[dict] = []
    frontend_files = _iter_js_files(static_root)
    if frontend_files:
        frontend_rules = [r for r in RULES if r.scope == "frontend"]
        for path in frontend_files:
            issues.extend(_scan_file(path, frontend_rules, static_root))
    else:
        issues.append({
            "rule": "static_missing", "title": "前端目录缺失", "level": "warning",
            "message": "没找到 console\\static\\js，本次没有扫描前端。等前端就位后再跑一次。",
            "file": "static/js", "line": 0, "excerpt": "",
        })

    danger_files = _iter_danger_files(cfg)
    danger_rules = [r for r in RULES if r.scope == "danger"]
    for path in danger_files:
        if os.path.abspath(path) == SELF_PATH:
            continue          # 规则定义处不算违规
        issues.extend(_scan_file(path, danger_rules, static_root))

    issues.sort(key=lambda i: (i["level"] != "error", i["file"], i["line"]))
    errors = sum(1 for i in issues if i["level"] == "error")
    warnings = sum(1 for i in issues if i["level"] == "warning")
    return {
        "generated_at": util.iso_now(),
        "scanned_frontend_files": len(frontend_files),
        "scanned_danger_files": len(danger_files),
        "issues": issues,
        "summary": {
            "error": errors,
            "warning": warnings,
            "ok": errors == 0,
        },
        "rules": [{"id": r.id, "title": r.title, "level": r.level, "message": r.message}
                  for r in RULES],
    }


def format_report(report: dict) -> str:
    """把报告渲染成中文文本（CLI 与设置页共用）。"""
    lines = ["静态自检报告（%s）" % report["generated_at"], ""]
    lines.append("扫描范围：前端 js %d 个文件；代码与脚本 %d 个文件。"
                 % (report["scanned_frontend_files"], report["scanned_danger_files"]))
    lines.append("结论：%s（错误 %d 项，提示 %d 项）" % (
        "通过" if report["summary"]["ok"] else "不通过",
        report["summary"]["error"], report["summary"]["warning"]))
    lines.append("")
    if not report["issues"]:
        lines.append("没有发现问题。")
    for issue in report["issues"]:
        tag = "错误" if issue["level"] == "error" else "提示"
        lines.append("[%s] %s —— %s:%d" % (tag, issue["title"], issue["file"], issue["line"]))
        lines.append("    %s" % issue["message"])
        if issue["excerpt"]:
            lines.append("    片段：%s" % issue["excerpt"])
    return "\n".join(lines) + "\n"


def main(argv: List[str] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001 - 自检要在配置坏了时也能跑
        print("读取配置失败，仍按默认路径扫描：%s" % exc)
        cfg = {"static_root": os.path.join(config.CONSOLE_DIR, "static")}
    report = scan(cfg)
    if "--json" in argv:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
