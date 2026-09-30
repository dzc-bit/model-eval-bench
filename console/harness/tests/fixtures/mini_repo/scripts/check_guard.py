"""守卫脚本：检查 .gitignore 是否保留了 node_modules 保护行。

评分树里也会带上这个脚本，p2p 用例 `tests/test_p2p.py` 依赖它。
"""

import sys
from pathlib import Path

REQUIRED_RULES = ("node_modules/", "__pycache__/")


def check(root: Path) -> int:
    text = (root / ".gitignore").read_text(encoding="utf-8")
    missing = [rule for rule in REQUIRED_RULES if rule not in text]
    if missing:
        print("守卫失败：.gitignore 缺少 %s" % "、".join(missing))
        return 1
    print("守卫通过：.gitignore 保护行齐全")
    return 0


if __name__ == "__main__":
    sys.exit(check(Path(__file__).resolve().parent.parent))
