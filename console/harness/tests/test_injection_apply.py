"""验收：注入补丁应用器（回归）。

真实缺陷（2026-09-30 实测，整条流水线的"假注入"）：

骨架目录在注入阶段还没有 `.git`，`git -C <骨架> apply <补丁>` 会一路向上找到
**评测台自己的 `.git`**。补丁里的目标路径在那个仓库里不存在，git 只打印
"Skipped patch"，**退出码依然是 0**。旧实现只看退出码 → `injected=3`，
而骨架是干净代码：题目在未注入的代码上评测，注入态该红的组全绿
（实测 T1-01 零改动拿 85.7 分）。

修法：自带一个内容匹配的 unified diff 应用器（不依赖 git），
并用全树摘要确认"确实改到了文件"。

另一个同类坑：unified diff 里每个 hunk 的 `-<start>` 是它在**原始文件**里的
行号，不是打完前面 hunk 之后的行号。不累计增量就会把第二个 hunk 起错位置
（`fix.patch` 的 `importer.py` 就是这样被误判成上下文不匹配）。
"""

from __future__ import annotations

import os

import pytest

from harness import errors, sandbox, util


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


# --------------------------------------------------------------------------
# 基础正确性
# --------------------------------------------------------------------------

BASIC = """\
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,4 +1,5 @@
 import os
+import sys
 
 
 def f():
"""


def test_apply_unified_adds_lines(tmp_path):
    root = str(tmp_path)
    _write(root, "pkg/mod.py", "import os\n\n\ndef f():\n    return 1\n")
    touched = sandbox._apply_unified(root, BASIC)
    assert touched == ["pkg/mod.py"]
    body = open(os.path.join(root, "pkg/mod.py"), encoding="utf-8").read()
    assert "import sys" in body
    assert body.index("import os") < body.index("import sys")


def test_apply_unified_replaces_and_deletes(tmp_path):
    root = str(tmp_path)
    _write(root, "a.txt", "one\ntwo\nthree\n")
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,3 +1,2 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        "-three\n"
    )
    sandbox._apply_unified(root, patch)
    assert open(os.path.join(root, "a.txt"), encoding="utf-8").read() == "one\nTWO\n"


def test_context_mismatch_raises(tmp_path):
    root = str(tmp_path)
    _write(root, "a.txt", "one\nCHANGED\nthree\n")
    patch = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
    )
    with pytest.raises(errors.HarnessError):
        sandbox._apply_unified(root, patch)


def test_missing_target_raises(tmp_path):
    patch = (
        "diff --git a/nope.txt b/nope.txt\n--- a/nope.txt\n+++ b/nope.txt\n"
        "@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    with pytest.raises(errors.HarnessError):
        sandbox._apply_unified(str(tmp_path), patch)


# --------------------------------------------------------------------------
# 多 hunk 行号语义（本轮的第二个坑）
# --------------------------------------------------------------------------

MULTI_HUNK = """\
diff --git a/f.py b/f.py
--- a/f.py
+++ b/f.py
@@ -1,3 +1,5 @@
 L1
+L2a
+L2b
 L2
 L3
@@ -7,3 +9,3 @@
 L7
-L8
+L8new
 L9
"""

#: 原始文件：第 1、2、3 行是 L1/L2/L3；第 7、8、9 行是 L7/L8/L9
MULTI_HUNK_SRC = "L1\nL2\nL3\nL4\nL5\nL6\nL7\nL8\nL9\n"


def test_second_hunk_uses_original_line_numbers(tmp_path):
    """第一个 hunk 加了 2 行后，第二个 hunk 仍要按原始行号（7）定位。"""
    root = str(tmp_path)
    _write(root, "f.py", MULTI_HUNK_SRC)

    sandbox._apply_unified(root, MULTI_HUNK)

    got = open(os.path.join(root, "f.py"), encoding="utf-8").read().splitlines()
    assert got == ["L1", "L2a", "L2b", "L2", "L3", "L4", "L5", "L6", "L7", "L8new", "L9"]


def test_multi_hunk_matches_reference_patch_shape(tmp_path):
    """复刻 fix.patch 的形态：hunk1 加 2 行（改 import），hunk2 改后面的默认真值。"""
    root = str(tmp_path)
    original = (
        "from pathlib import Path\n"
        "\n"
        "import pandas as pd\n"
        "\n"
        "DEFAULTS = {\n"
        '    "amount": 0.0,\n'
        '    "turnover_rate": 0.0,\n'
        "}\n"
    )
    _write(root, "imp.py", original)
    patch = (
        "diff --git a/imp.py b/imp.py\n--- a/imp.py\n+++ b/imp.py\n"
        "@@ -1,4 +1,6 @@\n"
        " from pathlib import Path\n"
        " \n"
        " import pandas as pd\n"
        "+\n"
        "+from pkg.models import UNKNOWN\n"
        " \n"
        "@@ -6,2 +8,2 @@\n"
        '     "amount": 0.0,\n'
        '-    "turnover_rate": 0.0,\n'
        '+    "turnover_rate": UNKNOWN,\n'
    )
    sandbox._apply_unified(root, patch)
    body = open(os.path.join(root, "imp.py"), encoding="utf-8").read()
    assert "from pkg.models import UNKNOWN" in body
    assert '"turnover_rate": UNKNOWN,' in body
    assert '"turnover_rate": 0.0,' not in body


# --------------------------------------------------------------------------
# 多文件补丁 + CRLF 保持
# --------------------------------------------------------------------------

def test_multi_file_patch_touches_each_file(tmp_path):
    root = str(tmp_path)
    _write(root, "a.py", "A1\nA2\n")
    _write(root, "b.py", "B1\nB2\n")
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n-A1\n+A1x\n A2\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1,2 +1,2 @@\n B1\n-B2\n+B2x\n"
    )
    touched = sandbox._apply_unified(root, patch)
    assert touched == ["a.py", "b.py"]
    assert open(os.path.join(root, "a.py"), encoding="utf-8").read().startswith("A1x")
    assert open(os.path.join(root, "b.py"), encoding="utf-8").read().endswith("B2x\n")


def test_crlf_file_stays_crlf(tmp_path):
    """CRLF 文件改完仍要是 CRLF，否则基线哈希与越界检测会误报。"""
    root = str(tmp_path)
    path = os.path.join(root, "c.py")
    with open(path, "wb") as fh:
        fh.write(b"X1\r\nX2\r\n")
    patch = (
        "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n"
        "@@ -1,2 +1,2 @@\n X1\n-X2\n+X2new\n"
    )
    sandbox._apply_unified(root, patch)
    raw = open(path, "rb").read()
    assert raw == b"X1\r\nX2new\r\n"


# --------------------------------------------------------------------------
# 反"假注入"：摘要必须变化
# --------------------------------------------------------------------------

def test_apply_patches_rejects_noop_patch(tmp_path):
    """补丁内容与文件一致（不改任何东西）必须报错，而不是静默计成功。"""
    root = str(tmp_path)
    _write(root, "d.py", "same\n")
    patch_file = os.path.join(root, "noop.patch")
    with open(patch_file, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            "diff --git a/d.py b/d.py\n--- a/d.py\n+++ b/d.py\n"
            "@@ -1,1 +1,1 @@\n-same\n+same\n"
        )
    before = sandbox._tree_digest(root)
    with pytest.raises(errors.HarnessError) as excinfo:
        sandbox._apply_patches(root, [patch_file], log=lambda m: None)
    assert "没有改动任何文件" in excinfo.value.message
    assert sandbox._tree_digest(root) == before, "失败的补丁不该留下半吊子改动"


def test_apply_patches_counts_real_changes(tmp_path):
    root = str(tmp_path)
    _write(root, "e.py", "old\n")
    patch_file = os.path.join(root, "ok.patch")
    with open(patch_file, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            "diff --git a/e.py b/e.py\n--- a/e.py\n+++ b/e.py\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
        )
    before = sandbox._tree_digest(root)
    count = sandbox._apply_patches(root, [patch_file], log=lambda m: None)
    assert count == 1
    assert sandbox._tree_digest(root) != before
    assert open(os.path.join(root, "e.py"), encoding="utf-8").read() == "new\n"


def test_tree_digest_changes_with_content(tmp_path):
    root = str(tmp_path)
    _write(root, "z.py", "a\n")
    d1 = sandbox._tree_digest(root)
    _write(root, "z.py", "b\n")
    assert sandbox._tree_digest(root) != d1
