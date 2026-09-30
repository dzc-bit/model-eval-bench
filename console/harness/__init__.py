"""harness 包：模型评测台的纯 Python 校验内核。

模块划分（设计文档 §2）：
    snapshot  第 1 层隔离：白名单快照 + 脱敏 + 兜底 grep
    sandbox   文件夹工作区 + 单提交 git + node_modules 实体副本 + 生命周期
    grade     第 4 层隔离：现场拼评分树 + 越界检测 + 分组部分分
    report    报告组装（设计文档 §5.2）
    calibrate 盲测校准排队（设计文档 §6.4）
    checks    checker 插件：pytest / vitest / script
    selfcheck 零构建下的静态自检（设计文档 §10.7）

文件夹沙箱规则（设计文档 §4.3）在本包里统一遵守：
    · 工作区依赖必须是实体副本，不允许 junction 或 symlink 越出工作区
    · 清空改动只用 `git reset --hard baseline && git clean -fd`（不带 -x）
    · 删沙箱只许整树 rmtree
"""

__all__ = [
    "calibrate", "checks", "config", "errors", "grade", "packs", "report",
    "runs", "sandbox", "selfcheck", "snapshot", "util",
]

__version__ = "1.0.0"
