"""验收：受测仓库路径自检（config.repo_readable）。

回归背景：健康检查原先只查 cfg["repo_root"]，而题目真正取的是
repo_root_for() 按 meta.repo.id 解析出的 repos.<id>。
两者不是同一个路径，于是出现过"设置页绿灯、11 道核心题全废"。
这里必须能真的抓到那种情况，否则等于没验。
"""

from __future__ import annotations

from harness import config


def test_all_repos_present_is_readable(tmp_path):
    root = tmp_path / "root"
    core = tmp_path / "core"
    root.mkdir()
    core.mkdir()
    ok, msg = config.repo_readable({"repo_root": str(root), "repos": {"core": str(core)}})
    assert ok is True
    assert msg.startswith("可读")


def test_missing_repos_entry_is_reported(tmp_path):
    """这条就是当年那个 bug：repo_root 在、repos.core 不在，必须报红并点名。"""
    root = tmp_path / "root"
    root.mkdir()
    ok, msg = config.repo_readable({
        "repo_root": str(root),
        "repos": {"core": str(tmp_path / "gone")},
    })
    assert ok is False
    assert "repos.core" in msg
    assert "gone" in msg


def test_repo_root_still_checked(tmp_path):
    ok, msg = config.repo_readable({"repo_root": str(tmp_path / "nope"), "repos": {}})
    assert ok is False
    assert "repo_root" in msg


def test_empty_path_is_reported_not_crashed(tmp_path):
    ok, msg = config.repo_readable({"repo_root": "", "repos": {"demo": None}})
    assert ok is False
    assert "repo_root" in msg and "repos.demo" in msg
