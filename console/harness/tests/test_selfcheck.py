"""验收：静态自检（设计文档 §10.7）。

自检要能真的抓到东西，所以这里用"阳性对照"：
把一段有问题的前端 js 放进临时 static 目录，确认每条规则都命中；
再把危险命令塞进一份临时代码文件，确认它被报出来；
最后确认真实代码里（注释里提到禁令不算）没有违规。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from harness import selfcheck, util

CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 阳性对照：每行都应当被某条规则命中
BAD_JS = """\
// 阳性对照：这一行只是注释，不该被报
const box = document.querySelector('#log');
box.innerHTML = '<b>' + userText + '</b>';
document.write('<p>hi</p>');
box.onclick = function () { start(); };
el.setAttribute('onchange', handler);
setInterval(tick, 500);
console.log('调试残留', box);
const color = '#ff8800';
const rgba = 'rgba(0,0,0,.5)';
"""


def write_static(root, name, body):
    path = os.path.join(root, "js", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


def rules_hit(report, name):
    return {i["rule"] for i in report["issues"] if os.path.basename(i["file"]) == name}


# ------------------------------------------------------------- 阳性对照

def test_frontend_rules_all_fire(cfg):
    """六条前端规则逐条命中。"""
    write_static(cfg["static_root"], "bad.js", BAD_JS)
    report = selfcheck.scan(cfg)
    hit = rules_hit(report, "bad.js")

    for rule_id in ("inner_html", "inline_onclick_attr", "inline_onclick_js",
                    "interval_no_clear", "console_log", "hardcoded_color", "document_write"):
        assert rule_id in hit, "%s 规则没命中阳性对照，实际命中：%s" % (rule_id, sorted(hit))
    assert report["summary"]["ok"] is False
    # 9 条违规里 7 条是 error，2 条 warning（console.log / 第二个 rgba 色值）
    assert report["summary"]["error"] >= 5, "检出错误：%s" % report["summary"]


def test_comment_line_is_not_flagged(cfg):
    """注释里写"禁止 innerHTML"不该被当成违规。"""
    write_static(cfg["static_root"], "ok.js",
                 "// 这里说明为什么不用 innerHTML：XSS\n"
                 "export function render(text) {\n"
                 "  const el = document.createElement('pre');\n"
                 "  el.textContent = text;\n"
                 "  return el;\n"
                 "}\n")
    report = selfcheck.scan(cfg)
    assert rules_hit(report, "ok.js") == set(), rules_hit(report, "ok.js")


def test_setinterval_with_clearinterval_is_allowed(cfg):
    """配了对 clearInterval 就不报。"""
    write_static(cfg["static_root"], "poller.js",
                 "export function createPoller(fn, interval) {\n"
                 "  const id = setInterval(fn, interval);\n"
                 "  return { destroy() { clearInterval(id); } };\n"
                 "}\n")
    report = selfcheck.scan(cfg)
    assert "interval_no_clear" not in rules_hit(report, "poller.js")


def test_dangerous_commands_are_caught(tmp_path, monkeypatch):
    """危险命令在真代码里必须被抓出来。"""
    conf = {"static_root": str(tmp_path / "static")}
    console_copy = tmp_path / "console"
    os.makedirs(console_copy, exist_ok=True)
    monkeypatch.setattr(selfcheck.config, "CONSOLE_DIR", str(console_copy))
    monkeypatch.setattr(selfcheck.config, "EVAL_ROOT", str(tmp_path))

    # 拼出来的字面量，测试文件本身才不会被自己判违规
    del_cmd = "del" + " /s /q"
    clean_cmd = "git" + " clean -fdx"
    with open(os.path.join(console_copy, "dangerous.py"), "w", encoding="utf-8") as fh:
        fh.write("# 正常脚本\n")
        fh.write("CMD = %r\n" % del_cmd)
        fh.write("CLEAN = %r\n" % clean_cmd)
    with open(os.path.join(console_copy, "clean.ps1"), "w", encoding="utf-8") as fh:
        fh.write("git %s\n" % clean_cmd)

    report = selfcheck.scan(conf)
    hit = {i["rule"] for i in report["issues"]}
    assert "danger_del_s" in hit, sorted(hit)
    assert "danger_clean_fdx" in hit, sorted(hit)
    assert report["summary"]["ok"] is False


def test_dangerous_command_in_python_comment_is_ignored(tmp_path, monkeypatch):
    """Python 注释里描述禁令是安全的（规则本身就得写在那里）。"""
    conf = {"static_root": str(tmp_path / "static")}
    console_copy = tmp_path / "console"
    os.makedirs(console_copy, exist_ok=True)
    monkeypatch.setattr(selfcheck.config, "CONSOLE_DIR", str(console_copy))
    monkeypatch.setattr(selfcheck.config, "EVAL_ROOT", str(tmp_path))

    del_cmd = "del" + " /s"
    with open(os.path.join(console_copy, "doc.py"), "w", encoding="utf-8") as fh:
        fh.write('"""这个模块绝不执行 %s。"""\n' % del_cmd)
        fh.write("# 也不执行 %s\n" % del_cmd)
        fh.write("SAFE = 1\n")

    report = selfcheck.scan(conf)
    assert [i for i in report["issues"] if i["rule"] == "danger_del_s"] == []


# ------------------------------------------------------------- 真实代码

def test_real_codebase_is_clean():
    """真实代码与脚本里没有危险命令（注释里提到禁令不算）。"""
    report = selfcheck.scan()
    danger = [i for i in report["issues"] if i["rule"].startswith("danger_")]
    assert danger == [], "真实代码里出现了危险命令：%s" % danger
    assert report["scanned_danger_files"] > 0, "危险命令扫描范围不能是空的"


def test_launcher_has_no_recursive_delete():
    """启动脚本不许出现递归删除。"""
    launcher = os.path.join(selfcheck.config.EVAL_ROOT, "启动.cmd")
    if not os.path.isfile(launcher):
        pytest.skip("启动.cmd 不在预期位置")
    with open(launcher, "rb") as fh:
        body = fh.read().decode("utf-8", errors="replace")
    lowered = body.lower().replace("  ", " ")
    assert ("del " + "/s") not in lowered
    assert "rd /s" not in lowered and "rmdir /s" not in lowered


def test_report_is_chinese_and_exit_code_matches():
    """CLI 跑一遍：中文报告 + 退出码反映结论。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(CONSOLE_DIR, "harness", "selfcheck.py")],
        capture_output=True, cwd=CONSOLE_DIR, timeout=180,
    )
    # 子进程走管道时按控制台码页编码（简中 = cp936），不能假定 UTF-8；
    # 统一走 util.decode_output 的「先 UTF-8 后 GBK」容错解码。
    body = util.decode_output(proc.stdout) + util.decode_output(proc.stderr)
    assert "静态自检报告" in body
    assert "扫描范围" in body and "结论" in body
    assert proc.returncode in (0, 1), "退出码只应是 0（通过）或 1（不通过），实际 %d" % proc.returncode
    if "不通过" in body:
        assert proc.returncode == 1
    else:
        assert proc.returncode == 0


def test_scan_survives_missing_static(cfg, tmp_path):
    """前端还没就位时也要给出中文报告，不能崩。"""
    conf = dict(cfg)
    conf["static_root"] = str(tmp_path / "不存在的目录")
    report = selfcheck.scan(conf)
    ids = {i["rule"] for i in report["issues"]}
    assert "static_missing" in ids
    assert report["scanned_frontend_files"] == 0
    text = selfcheck.format_report(report)
    assert "静态自检报告" in text and "前端目录缺失" in text
