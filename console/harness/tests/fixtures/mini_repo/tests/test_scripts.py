"""守卫脚本的回归用例：验证 .gitignore 的保护行没被删（p2p 白名单里点名的就是这里）。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_gitignore_keeps_node_modules_rule():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_guard.py")],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
