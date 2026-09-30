"""harness 包：模型评测台的纯 Python 校验内核。

模块划分（设计文档 §2）：
    snapshot  第 1 层隔离：白名单快照 + 脱敏 + 兜底 grep
    sandbox   第 2/3 层隔离：盘符映射 + 单提交 git + node_modules 联接 + 生命周期
    grade     第 4 层隔离：现场拼评分树 + 越界检测 + 分组部分分
    report    报告组装（设计文档 §5.2）
    calibrate 盲测校准排队（设计文档 §6.4）
    checks    checker 插件：pytest / vitest / script
    selfcheck 零构建下的静态自检（设计文档 §10.7）

删除与 junction 的硬规则（设计文档 §4.3）在本包里统一遵守：
    · 绝不 `del /s`（会穿透 junction 删真实 node_modules）
    · 清空改动只用 `git reset --hard baseline && git clean -fd`（不带 -x）
    · 删沙箱只许整树 rmtree；单独摘 junction 用 os.rmdir
"""

__all__ = [
    "calibrate", "checks", "config", "errors", "grade", "packs", "report",
    "runs", "sandbox", "selfcheck", "snapshot", "util",
]

__version__ = "1.0.0"
