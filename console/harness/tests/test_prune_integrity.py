"""验收：裁剪（visible.prune）不能产出语法残缺的文件（回归）。

背景（真实缺陷）：`_py_block_spans` 原先用逐行缩进扫描找函数块，装饰器只认
"上方紧邻且以 `@` 起头"的行。于是多行装饰器

    @pytest.mark.parametrize(
        ("symbol", "is_st"),
        [("600001", False), ...],
    )
    def test_xxx(...):

的最后一行是 `)`，识别不到 → 装饰器留在文件里、函数体被删到 EOF →
文件以 `@pytest.mark.parametrize(...)` 后面直接结束。pytest 收集即 SyntaxError，
整批用例（含 p2p 白名单）全红、退出码 4，评测结果全错。

修法：优先用 `ast` 拿权威行号（装饰器取所有 decorator_list 里最早的一行），
语法本身有问题时才退回逐行扫描。下面的用例锁住这两条。
"""

from __future__ import annotations

import ast

from harness import snapshot, util


def _lines(text: str) -> list:
    return text.splitlines(keepends=True)


#: 末尾那个函数带多行装饰器——正是旧实现会切坏的情形
SAMPLE = '''\
import pytest


def helper():
    return 1


@pytest.mark.parametrize(
    ("symbol", "want"),
    [
        ("600001", 1),
        ("000001", 2),
    ],
)
def test_last_guarded(symbol, want):
    assert helper() == 1
    assert want in (1, 2)
'''


def test_prune_removes_multiline_decorator_with_its_function():
    """多行装饰器必须和函数体一起删，不能留下悬空装饰器。"""
    lines = _lines(SAMPLE)
    spans = snapshot._py_block_spans(lines)
    by_name = {name: (head, end) for name, head, end in spans}

    assert "test_last_guarded" in by_name, "应当识别到被测函数"
    head, end = by_name["test_last_guarded"]

    # head 必须落在 `@pytest.mark.parametrize(` 那一行，而不是 `def` 行
    assert lines[head].strip().startswith("@pytest.mark.parametrize"), (
        "装饰器起点没被纳入区间，实际首行是 %r" % lines[head])
    assert lines[end - 1].strip() == "assert want in (1, 2)"
    assert end == len(lines), "末尾函数应当删到文件结尾"


def test_ast_and_scan_agree_on_spans():
    """两个实现（ast 权威 / 逐行兜底）对同一份合法源码要给出一致的块区间。"""
    lines = _lines(SAMPLE)
    ast_spans = snapshot._py_block_spans_ast(lines)
    scan_spans = snapshot._py_block_spans_scan(lines)

    assert ast_spans is not None
    assert sorted(ast_spans) == sorted(scan_spans), (
        "ast 与兜底扫描结果不一致：%r vs %r" % (ast_spans, scan_spans))


def test_prune_keeps_file_parseable():
    """按区间删除后，剩下的源码必须还是合法 Python。"""
    lines = _lines(SAMPLE)
    spans = snapshot._py_block_spans(lines)
    by_name = {name: (head, end) for name, head, end in spans}
    head, end = by_name["test_last_guarded"]

    drop = set(range(head, end))
    left = "".join(line for i, line in enumerate(lines) if i not in drop)

    ast.parse(left)                      # 不抛异常即通过
    assert "test_last_guarded" not in left
    assert "test_other" not in left      # 顺带确认没误删别的
    assert "def helper" in left, "无关函数不该被删"


def test_apply_prune_on_temp_file(tmp_path):
    """走完整 apply_prune：临时文件被裁剪后仍是合法 Python。"""
    target = tmp_path / "tests"
    target.mkdir()
    py = target / "test_engine.py"
    py.write_text(SAMPLE, encoding="utf-8")

    meta = {"visible": {"prune": ["tests/test_engine.py::test_last_guarded"]}}
    touched = snapshot.apply_prune(str(tmp_path), meta, log=lambda m: None)

    assert touched == ["tests/test_engine.py"]
    left = py.read_text(encoding="utf-8")
    ast.parse(left)
    assert "def test_last_guarded" not in left
    assert "def helper" in left


def test_scan_fallback_survives_syntax_error():
    """源码本身语法坏掉时，ast 返回 None，兜底扫描仍要给出可用区间。"""
    broken = "def ok():\n    return 1\n\ndef bad(:\n    pass\n"
    lines = _lines(broken)

    assert snapshot._py_block_spans_ast(lines) is None, "语法错误时应让位给兜底实现"
    spans = snapshot._py_block_spans(lines)          # 不能抛
    assert any(name == "ok" for name, _h, _e in spans)


def test_multiline_decorator_with_args_on_def_line():
    """装饰器与 def 之间隔着空行/注释的写法也不能切错。"""
    text = (
        "def first():\n"
        "    return 1\n"
        "\n"
        "\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "@pytest.mark.parametrize('a', [\n"
        "    1,\n"
        "    2,\n"
        "])\n"
        "def second(a):\n"
        "    assert a\n"
    )
    lines = _lines(text)
    spans = {name: (h, e) for name, h, e in snapshot._py_block_spans(lines)}

    head, end = spans["second"]
    assert lines[head].strip().startswith("@pytest.mark.skipif"), (
        "最早的装饰器行没被包含，实际 %r" % lines[head])
    assert end == len(lines)
    left = "".join(l for i, l in enumerate(lines) if not (head <= i < end))
    ast.parse(left)
