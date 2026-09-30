"""checker 插件注册表（设计文档 §17：新增语言/框架只加一个 checks.kind）。

约定：每个 checker 接收一个 CheckContext，在「评分树」里跑自己的测试命令，
把结果归一化成 {用例 ID: 结果} 交给 grade.py 做分组加权计分。
checker 不负责判分，也不关心 allowed_paths 之类的隔离逻辑。
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List

#: 插件模块名与 kind 的对应关系
_PLUGINS = {
    "pytest": "pytest",
    "vitest": "vitest",
    "script": "script",
}


@dataclass
class CheckContext:
    """一次 checker 调用的全部输入。"""

    workdir: str            # 评分树根目录（cwd 就在这里）
    spec: dict              # meta.checks 里的一项
    kind: str
    node_ids: list          # 本次要跑的用例 ID 列表
    timeout_s: int
    env: dict               # 已注入环境变量的子进程环境
    log: Callable[[str], None]
    batch: int = 0          # 第几批（第 2 批一般留给 p2p 回归）
    batch_total: int = 1
    tmp_dir: str = ""       # 评分树内的临时目录（--basetemp 等）


@dataclass
class CaseResult:
    """单个用例的归一化结果。"""

    node_id: str
    outcome: str            # passed / failed / error / skipped
    duration: float = 0.0
    message: str = ""
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


@dataclass
class CheckResult:
    """一个 checker 的整体结果。"""

    kind: str
    returncode: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    cases: Dict[str, CaseResult] = field(default_factory=dict)
    command: list = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary_line(self) -> str:
        if self.timed_out:
            return "%s 超时（%.0fs 上限）" % (self.kind, self.duration_s)
        # cases 是多键索引（一条用例会挂好几个可查的键），要按对象去重才是真实条数
        unique = {id(c): c for c in self.cases.values()}
        passed = sum(1 for c in unique.values() if c.passed)
        return "%s：%d/%d 通过，退出码 %d，用时 %.1fs" % (
            self.kind, passed, len(unique), self.returncode, self.duration_s)


def register(kind: str) -> Callable:
    """把一个 checker 注册到 kind 上。"""

    def deco(func):
        _REGISTRY[kind] = func
        return func

    return deco


_REGISTRY = {}


def _load_plugins() -> None:
    """懒加载插件模块（导入即完成注册）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    for kind, module in _PLUGINS.items():
        if kind in _REGISTRY:
            continue
        try:
            importlib.import_module("harness.checks.%s" % module)
        except Exception:  # noqa: BLE001 - 插件缺失不应拖垮整个服务
            continue


def get(kind: str):
    """按 kind 取 checker 函数；未注册则返回 None。"""
    _load_plugins()
    return _REGISTRY.get(str(kind).lower())


def available() -> List[str]:
    """已注册的 checker kind 列表（/api/health 用）。"""
    _load_plugins()
    return sorted(_REGISTRY)
