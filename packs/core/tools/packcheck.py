#!/usr/bin/env python3
"""packcheck.py —— 任务包完整性自检（纯标准库，不跑测试）。

职责边界（刻意与 selfgrade.py 分开，别互相冒充）：

* packcheck 只看**包的自洽**：目录树齐不齐、meta.json 字段对不对 §8、
  分组/p2p/提示词/校准材料互相对不对得上、以及 §6.5 六条反过易清单有没有
  留下可核查的自证。任何一个红项都说明"这道题还没法入库"，不是"答案错了"。
* selfgrade 看**解的对错**：拼评分树、跑 pytest、按组算分。

用法::

    python packcheck.py                          # 扫 packs/core 下全部任务
    python packcheck.py --task T1-01              # 只扫一道题
    python packcheck.py --repo "D:\\New project 6" # 加上与真实仓库的交叉核对
    python packcheck.py --out tools/_work/packcheck.json

退出码：0 = 无红项；1 = 有红项；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 报告原语
# --------------------------------------------------------------------------

RED = "红"
YELLOW = "黄"
GREEN = "绿"
SKIP = "略"

_SEVERITY_ORDER = {RED: 0, YELLOW: 1, SKIP: 2, GREEN: 3}


@dataclass
class Report:
    """一道题（或整个包）的检查结果。"""

    task: str
    findings: list[tuple[str, str, str, str]] = field(default_factory=list)

    def add(self, level: str, group: str, message: str, evidence: str = "") -> None:
        self.findings.append((level, group, message, evidence))

    def ok(self, group: str, message: str, evidence: str = "") -> None:
        self.add(GREEN, group, message, evidence)

    def bad(self, group: str, message: str, evidence: str = "") -> None:
        self.add(RED, group, message, evidence)

    def warn(self, group: str, message: str, evidence: str = "") -> None:
        self.add(YELLOW, group, message, evidence)

    def skip(self, group: str, message: str, evidence: str = "") -> None:
        self.add(SKIP, group, message, evidence)

    @property
    def red_count(self) -> int:
        return sum(1 for level, *_ in self.findings if level == RED)

    @property
    def yellow_count(self) -> int:
        return sum(1 for level, *_ in self.findings if level == YELLOW)

    def sorted_findings(self) -> list[tuple[str, str, str, str]]:
        return sorted(self.findings, key=lambda item: _SEVERITY_ORDER.get(item[0], 9))


# --------------------------------------------------------------------------
# §6.3 档位硬性规格（唯一权威表，出题与自检共用）
# --------------------------------------------------------------------------

TIER_SPEC = {
    "primary": {"attempts": 1, "files": 3, "target_band": (0.60, 0.85)},
    "medium": {"attempts": 2, "files": 3, "target_band": (0.25, 0.55)},
    "hard": {"attempts": 3, "files": 4, "target_band": (0.05, 0.25)},
    "king": {"attempts": 3, "files": 6, "target_band": (0.0, 0.02), "scored_groups": 8},
}

REQUIRED_META_KEYS = [
    "schema",
    "id",
    "tier",
    "attempts",
    "title",
    "repo",
    "allowed_paths",
    "forbidden_paths",
    "visible",
    "redactions",
    "checks",
    "budget",
    "calibration",
]

# §6.5 六条清单在 notes.md 里的判别关键词。顺序即清单顺序。
CHECKLIST_65 = [
    ("线索可搜索性", ("grep",)),
    ("诱饵点", ("诱饵",)),
    ("第二数据场景", ("第二数据场景", "硬编码")),
    ("提示词零名词", ("提示词",)),
    ("半成品上限", ("半成品", "只修一个端口")),
    ("十分钟自评", ("10 分钟", "十分钟", "退回重做")),
]

# 提示词里出现这些就说明泄露了答案载体（附录 A：全程无文件名/函数名/常量名）
LEAKY_EXTENSIONS = (".py", ".ts", ".tsx", ".json", ".toml", ".cfg", ".ini", ".cmd", ".sql")
FORBIDDEN_LITERALS = ("D:\\New project 6", "D:/New project 6", "醍醐测试", "reference/", "hidden/")

# 扫描标识符时跳过的通用词：它们既不是本仓库特有的，也不会指向答案。
TOKEN_STOPWORDS = {
    "import", "median", "sorted", "absent", "present", "content", "context", "index",
    "insert", "update", "delete", "create", "read", "write", "list", "dict", "set_",
    "main", "data", "name", "names", "value", "values", "key", "keys", "path",
    "paths", "file", "files", "line", "lines", "text", "code", "size", "count",
    "counts", "row", "rows", "column", "columns", "date", "dates", "day", "days",
    "test", "tests", "hidden", "self", "cls", "args", "kwargs", "result", "results",
    "assert", "expect", "should", "check", "checks", "group", "groups", "score",
    "scores", "weight", "weights", "first", "last", "left", "right", "start", "end",
    "next", "prev", "true", "false", "none", "null", "str", "int", "float", "bool",
    "bytes", "object", "type", "types", "status", "state", "reason", "message",
    "summary", "detail", "details", "note", "notes", "report", "reports", "item",
    "items", "node", "nodes", "case", "cases", "level", "levels", "mode", "modes",
}


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def posix(path: Path) -> str:
    return path.as_posix()


def matches_any(rel_path: str, patterns: list[str]) -> list[str]:
    """返回命中的 glob 列表。fnmatch 的 ``*`` 会跨 ``/``，正合 ``tests/**`` 的用法。"""
    return [pattern for pattern in patterns if fnmatch.fnmatch(rel_path, pattern)]


def patch_targets(patch_text: str) -> list[str]:
    """从 unified diff 头里取出被改动的仓库相对路径。"""
    found: list[str] = []
    for line in patch_text.splitlines():
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if match:
            found.append(match.group(2))
            continue
        if line.startswith("--- ") and not line.startswith("--- /dev/null"):
            candidate = line[4:].split("\t")[0].strip()
            if candidate.startswith("a/"):
                candidate = candidate[2:]
            if candidate and candidate != "/dev/null":
                found.append(candidate)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def is_unified_diff(patch_text: str) -> tuple[bool, str]:
    if not patch_text.startswith("diff --git ") and not patch_text.startswith("--- "):
        return False, "补丁缺少 diff 头"
    if "+++ " not in patch_text or "@@ " not in patch_text:
        return False, "补丁缺少 +++ 行或 @@ hunk 头"
    return True, ""


def patch_change_volume(patch_text: str) -> int:
    """补丁的改动体量：实际增删行数（不含 +++/--- 文件头）。

    用于「半成品必须是锚解的真子集」判据的同文件兜底：当锚解与半成品
    恰好落在同一批文件里（单文件锚解不存在更小的文件集合），改比
    改动行数——半成品的增删体量必须严格小于锚解。这比文件集合比较弱，
    只能证明"半成品做得更少"，证明不了"它只做了一部分端口"；所以仅当
    文件集合判据结构上不可满足时才启用，并在结论里写明降级理由。
    """
    volume = 0
    for line in patch_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            volume += 1
    return volume


# --------------------------------------------------------------------------
# 名词泄露词表
# --------------------------------------------------------------------------


def collect_identifier_vocabulary(task_dir: Path, meta: dict, repo: Path | None) -> set[str]:
    """汇总"绝对不能出现在提示词里"的名词：文件名、函数名、常量名。"""
    tokens: set[str] = set()

    def eat(raw: str) -> None:
        token = raw.strip().strip("`\"'()[]{}<>:,.;")
        if len(token) < 4 or token.lower() in TOKEN_STOPWORDS:
            return
        tokens.add(token)

    # 1) 题目元数据里出现的仓库路径与用例 id
    for rel in list(meta.get("allowed_paths", [])) + list(meta.get("forbidden_paths", [])):
        leaf = rel.split("/")[-1]
        if "*" in leaf:
            leaf = leaf.split("*")[0]
        eat(leaf)
        if leaf.endswith(".py"):
            eat(leaf[:-3])
    for check in meta.get("checks", []):
        for key in ("hidden", "groups", "p2p"):
            value = check.get(key)
            if isinstance(value, str) and "/" in value:
                leaf = value.split("/")[-1]
                if leaf.endswith(".json"):
                    eat(leaf[:-5])

    # 2) 隐藏测试与分组里出现的标识符
    for path in sorted((task_dir / "hidden").rglob("*.py")) if (task_dir / "hidden").is_dir() else []:
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError as error:
            eat("")
            print(f"[packcheck] 隐藏测试 {path.name} 语法错误：{error}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                eat(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper() and "_" in target.id:
                        eat(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id.isupper() and "_" in node.target.id:
                    eat(node.target.id)

    groups_path = task_dir / "hidden" / "groups.json"
    if groups_path.is_file():
        groups = read_json(groups_path)
        for group in groups.get("groups", []):
            for node_id in group.get("tests", []):
                eat(node_id.split("::")[-1].split("[")[0])

    # 3) 注入补丁与参考解里出现的被改文件名
    patch_dirs = [task_dir / "inject" / "patches", task_dir / "reference"]
    for directory in patch_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.patch")):
            for rel in patch_targets(read_text(path)):
                leaf = rel.split("/")[-1]
                eat(leaf)
                if leaf.endswith(".py"):
                    eat(leaf[:-3])

    # 4) allowed_paths 指向的真实文件名（有些文件注入没碰，但仍是答案位置）
    if repo is not None:
        for rel in meta.get("allowed_paths", []):
            if "*" in rel:
                continue
            leaf = (repo / rel).name
            if leaf:
                eat(leaf)
                if leaf.endswith(".py"):
                    eat(leaf[:-3])

    # 含下划线的多词标识符最具指向性，一律保留；单段词只保留仓库文件名类
    return {token for token in tokens if "_" in token or "." in token or len(token) >= 5}


def scan_prompt_leaks(prompt_text: str, vocabulary: set[str]) -> list[str]:
    """返回泄露描述列表，空列表表示干净。"""
    leaks: list[str] = []
    for literal in FORBIDDEN_LITERALS:
        if literal in prompt_text:
            leaks.append(f"出现禁止字面量 {literal!r}")
    for extension in LEAKY_EXTENSIONS:
        if extension in prompt_text:
            leaks.append(f"出现代码/配置文件后缀 {extension!r}")
    lowered = prompt_text.lower()
    for token in sorted(vocabulary, key=len, reverse=True):
        needle = token.lower()
        if "_" in token or "." in token:
            hit = re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w])", lowered)
        else:
            hit = re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", lowered)
        if hit:
            start = max(0, hit.start() - 24)
            leaks.append(f"出现名词 {token!r}：…{prompt_text[start:hit.end() + 24]}…")
    return leaks


# --------------------------------------------------------------------------
# 各项检查
# --------------------------------------------------------------------------


def check_layout(task_dir: Path, report: Report) -> None:
    required = [
        "meta.json",
        "prompts/1.md",
        "prompts/2.md",
        "prompts/3.md",
        "hidden/groups.json",
        "p2p.json",
        "reference/fix.patch",
        "reference/partial.patch",
        "reference/notes.md",
        "calibration/results.json",
    ]
    missing = [rel for rel in required if not (task_dir / rel).is_file()]
    if missing:
        report.bad("目录树", "缺少必需文件", ", ".join(missing))
    else:
        report.ok("目录树", "§8 目录树齐备", f"{len(required)} 个必需文件")

    patches = sorted((task_dir / "inject" / "patches").glob("*.patch")) if (task_dir / "inject" / "patches").is_dir() else []
    if not patches:
        report.bad("目录树", "inject/patches/ 下没有补丁", "§8 要求用 unified diff 注入")
    else:
        report.ok("目录树", f"注入补丁 {len(patches)} 份")

    hidden_tests = sorted((task_dir / "hidden" / "tests_hidden").rglob("test_*.py")) if (task_dir / "hidden" / "tests_hidden").is_dir() else []
    if not hidden_tests:
        report.bad("目录树", "hidden/tests_hidden/ 下没有隐藏测试")
    else:
        report.ok("目录树", f"隐藏测试 {len(hidden_tests)} 个文件")


def check_meta(task_dir: Path, meta: dict, report: Report) -> None:
    missing = [key for key in REQUIRED_META_KEYS if key not in meta]
    if missing:
        report.bad("meta", "缺字段", ", ".join(missing))
    else:
        report.ok("meta", f"schema {meta['schema']} 字段齐备")

    tier = meta.get("tier")
    if tier not in TIER_SPEC:
        report.bad("meta", f"未知档位 {tier!r}", f"合法值：{sorted(TIER_SPEC)}")
        return
    spec = TIER_SPEC[tier]
    if meta.get("attempts") != spec["attempts"]:
        report.bad("meta", f"{tier} 档尝试次数应为 {spec['attempts']}", f"实际 {meta.get('attempts')}")
    else:
        report.ok("meta", f"{tier} 档 attempts={spec['attempts']}")

    band = meta.get("calibration", {}).get("target_band")
    if not (isinstance(band, list) and len(band) == 2 and band == list(spec["target_band"])):
        report.bad("meta", f"{tier} 档目标带应为 {list(spec['target_band'])}", f"实际 {band}")
    else:
        report.ok("meta", f"目标带 {band}")

    repo_meta = meta.get("repo", {})
    for key in ("id", "snapshot", "commit"):
        if not repo_meta.get(key):
            report.bad("meta", f"repo.{key} 缺失")
    commit = str(repo_meta.get("commit", ""))
    if commit and not re.fullmatch(r"[0-9a-f]{40}", commit):
        report.warn("meta", "repo.commit 不是 40 位 sha", commit)

    if not meta.get("title"):
        report.bad("meta", "title 为空")
    if not re.fullmatch(r"T[1234]-\d{2}", str(meta.get("id", ""))):
        report.bad("meta", "id 不符合 T<档>-<两位序号>", f"实际 {meta.get('id')!r}")
    else:
        report.ok("meta", f"id {meta['id']}")

    # §4.2 第 4 层：答案目录永不入 allowed_paths
    for rel in meta.get("allowed_paths", []):
        if rel.startswith(("packs/", "console/", "reference/", "hidden/")):
            report.bad("meta", f"allowed_paths 指向答案/控制台目录：{rel}")
    for rel in meta.get("forbidden_paths", []):
        if "reference" in rel or "hidden" in rel or rel.startswith("packs/"):
            report.ok("meta", f"forbidden_paths 已圈住 {rel}")

    if not isinstance(meta.get("redactions"), list):
        report.bad("meta", "redactions 必须是数组")

    for check in meta.get("checks", []):
        if check.get("kind") not in {"pytest", "vitest"}:
            report.warn("meta", f"未识别的 check.kind {check.get('kind')!r}")
        for key in ("hidden", "groups", "p2p"):
            value = check.get(key)
            if not value or not (task_dir / value).exists():
                report.bad("meta", f"checks[0].{key} 指向的路径不存在", str(value))


def check_paths(task_dir: Path, meta: dict, repo: Path | None, report: Report) -> None:
    allowed = list(meta.get("allowed_paths", []))
    forbidden = list(meta.get("forbidden_paths", []))
    if not allowed:
        report.bad("allowed_paths", "为空")
        return

    spec = TIER_SPEC.get(meta.get("tier"), {})
    minimum = spec.get("files", 3)
    if len(allowed) < minimum:
        report.bad("allowed_paths", f"{meta.get('tier')} 档要求 ≥{minimum} 个可改文件", f"实际 {len(allowed)}")
    else:
        report.ok("allowed_paths", f"{len(allowed)} 个可改文件（≥{minimum}）")

    overlap = [(rel, matches_any(rel, forbidden)) for rel in allowed]
    overlap = [(rel, hits) for rel, hits in overlap if hits]
    if overlap:
        report.bad("allowed_paths", "同一文件既可改又被禁", "; ".join(f"{rel} 命中 {hits}" for rel, hits in overlap))
    else:
        report.ok("allowed_paths", "与 forbidden_paths 无交集")

    for rel in allowed:
        if "*" in rel:
            continue
        if repo is None:
            continue
        if not (repo / rel).is_file():
            report.bad("allowed_paths", f"仓库里没有这个文件：{rel}")
    if repo is not None:
        report.ok("allowed_paths", "与仓库实际文件交叉核对完毕")

    # 注入补丁与参考解都只准碰 allowed_paths
    patch_files = sorted((task_dir / "inject" / "patches").glob("*.patch"))
    patch_files += sorted((task_dir / "reference").glob("*.patch"))
    for patch_file in patch_files:
        text = read_text(patch_file)
        valid, why = is_unified_diff(text)
        if not valid:
            report.bad("补丁", f"{patch_file.name} 不是合法 unified diff", why)
            continue
        targets = patch_targets(text)
        for rel in targets:
            hits = matches_any(rel, forbidden)
            if hits:
                report.bad("补丁", f"{patch_file.name} 触碰 forbidden_paths：{rel}", f"命中 {hits}")
            if "*" not in allowed and rel not in allowed and not any(fnmatch.fnmatch(rel, pat) for pat in allowed):
                report.bad("补丁", f"{patch_file.name} 改了 allowed_paths 之外的文件：{rel}")
        if not targets:
            report.bad("补丁", f"{patch_file.name} 解析不出目标文件")
    report.ok("补丁", f"{len(patch_files)} 份补丁均为合法 unified diff 且只碰允许路径")

    fix_targets = patch_targets(read_text(task_dir / "reference" / "fix.patch")) if (task_dir / "reference" / "fix.patch").is_file() else []
    partial_targets = patch_targets(read_text(task_dir / "reference" / "partial.patch")) if (task_dir / "reference" / "partial.patch").is_file() else []
    if fix_targets and partial_targets:
        if set(partial_targets) < set(fix_targets):
            report.ok("参考解", f"锚解改 {len(fix_targets)} 个文件，半成品只改 {len(partial_targets)} 个")
        elif set(partial_targets) <= set(fix_targets):
            # 同一文件集合（典型：锚解只落一个文件，不存在更小的真子集）——
            # 降级为改动体量比较：半成品的增删行数必须严格小于锚解。
            fix_volume = patch_change_volume(read_text(task_dir / "reference" / "fix.patch"))
            partial_volume = patch_change_volume(read_text(task_dir / "reference" / "partial.patch"))
            if partial_volume < fix_volume:
                report.ok(
                    "参考解",
                    f"锚解与半成品同落 {len(fix_targets)} 个文件；半成品改动 {partial_volume} 行 < 锚解 {fix_volume} 行（体量判据）",
                    "文件集合判据结构上不可满足（单文件锚解），降级为改动行数比较",
                )
            else:
                report.bad(
                    "参考解",
                    "半成品与锚解同文件集合，且改动体量不小于锚解",
                    f"partial={partial_volume} 行 vs fix={fix_volume} 行；半成品应当只修锚解的一部分",
                )
        else:
            report.bad("参考解", "半成品触碰了锚解之外的文件", f"fix={fix_targets} partial={partial_targets}")


def check_groups(task_dir: Path, report: Report, tier: str = "") -> None:
    groups_path = task_dir / "hidden" / "groups.json"
    if not groups_path.is_file():
        report.bad("分组", "缺 hidden/groups.json")
        return
    try:
        data = read_json(groups_path)
    except json.JSONDecodeError as error:
        report.bad("分组", "groups.json 不是合法 JSON", str(error))
        return

    groups = data.get("groups", [])
    if not groups:
        report.bad("分组", "groups 为空")
        return

    ids = [group.get("id") for group in groups]
    if len(set(ids)) != len(ids):
        report.bad("分组", "组 id 重复", ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    else:
        report.ok("分组", f"{len(groups)} 组，id 唯一：{', '.join(str(i) for i in ids)}")

    p2p_seen = False
    for group in groups:
        gid = group.get("id")
        weight = group.get("weight")
        tests = group.get("tests", [])
        if weight == 0:
            p2p_seen = True
            if group.get("mode") != "regression":
                report.bad("分组", f"{gid} 权重 0 但 mode 不是 regression")
            if tests:
                report.warn("分组", f"{gid} 权重 0 却列了 {len(tests)} 条用例", "既有用例白名单在 p2p.json")
        else:
            if not isinstance(weight, int) or weight < 1:
                report.bad("分组", f"{gid} 权重应为 ≥1 的整数", f"实际 {weight}")
            if len(tests) < 2:
                report.bad("分组", f"{gid} 只有 {len(tests)} 条断言", "§6.5：每组需要第二数据场景，硬编码/特判必挂")
        for node_id in tests:
            if "::" not in node_id:
                report.bad("分组", f"{gid} 的用例 id 缺 :: 用例名", node_id)
    if not p2p_seen:
        report.bad("分组", "缺 p2p 回归组（weight 0, mode=regression）")
    elif not any(group.get("id") == "coherence" for group in groups):
        report.warn("分组", "没有 coherence 组", "§5.1 建议 coherence 权重最高")
    else:
        coherence = next(g for g in groups if g["id"] == "coherence")
        others = [g.get("weight", 0) for g in groups if g.get("id") != "coherence" and g.get("weight", 0) > 0]
        if others and coherence.get("weight", 0) <= max(others):
            report.warn("分组", f"coherence 权重 {coherence.get('weight')} 未高于其它组 {max(others)}")

    king_minimum = TIER_SPEC.get(tier, {}).get("scored_groups")
    if king_minimum:
        scored = sum(1 for group in groups if group.get("mode") != "regression" and group.get("weight", 0) > 0)
        if scored < king_minimum:
            report.bad("分组", f"king 档要求至少 {king_minimum} 个计分组", f"实际 {scored}")
        else:
            report.ok("分组", f"king 档计分组 {scored} 个（≥{king_minimum}）")


def check_hidden_collectable(task_dir: Path, report: Report) -> None:
    """隐藏测试文件必须能 import/收集，否则组永远是红的。"""
    hidden_dir = task_dir / "hidden" / "tests_hidden"
    if not hidden_dir.is_dir():
        return
    available: set[str] = set()
    for path in sorted(hidden_dir.rglob("test_*.py")):
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError as error:
            report.bad("隐藏测试", f"{path.name} 语法错误", str(error))
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                available.add(f"{path.name}::{node.name}")
    groups_path = task_dir / "hidden" / "groups.json"
    if not groups_path.is_file():
        return
    data = read_json(groups_path)
    unknown: list[str] = []
    for group in data.get("groups", []):
        for node_id in group.get("tests", []):
            file_part, _, func = node_id.partition("::")
            leaf = file_part.split("/")[-1]
            base = func.split("[")[0]
            if f"{leaf}::{base}" not in available:
                unknown.append(node_id)
    if unknown:
        report.bad("隐藏测试", "分组里引用了不存在的用例", "; ".join(unknown[:5]))
    else:
        report.ok("隐藏测试", f"{len(available)} 个用例函数与分组清单一致")


def check_p2p_and_prune(task_dir: Path, meta: dict, repo: Path | None, report: Report) -> None:
    p2p_path = task_dir / "p2p.json"
    if not p2p_path.is_file():
        return
    try:
        p2p = read_json(p2p_path)
    except json.JSONDecodeError as error:
        report.bad("p2p", "p2p.json 不是合法 JSON", str(error))
        return
    node_ids = p2p.get("node_ids") or p2p.get("tests") if isinstance(p2p, dict) else p2p
    if not isinstance(node_ids, list) or not node_ids:
        report.bad("p2p", "p2p 白名单为空")
        return
    bad_shape = [item for item in node_ids if not (isinstance(item, str) and item.startswith("tests/") and "::" in item)]
    if bad_shape:
        report.bad("p2p", "存在形状不对的白名单条目", "; ".join(map(str, bad_shape[:5])))
    report.ok("p2p", f"{len(node_ids)} 条既有用例白名单")

    prune = list(meta.get("visible", {}).get("prune", []))
    pruned_in_p2p = [item for item in prune if item in node_ids]
    if pruned_in_p2p:
        report.bad("p2p", "被裁剪的用例不该同时留在 p2p 白名单（已不再被收集）", "; ".join(pruned_in_p2p))
    elif prune:
        report.ok("p2p", f"与 visible.prune（{len(prune)} 条）无交集")

    if repo is not None:
        missing = [item for item in prune if not (repo / item.split("::")[0]).is_file()]
        if missing:
            report.bad("visible.prune", "裁剪了仓库里不存在的用例", "; ".join(missing))
        else:
            report.ok("visible.prune", f"{len(prune)} 条裁剪项在仓库中均存在")


def check_frontend_p2p(task_dir: Path, meta: dict, repo: Path | None, report: Report) -> None:
    """Vitest p2p lists are separate files and must be tied to this task and source tree."""
    for check in meta.get("checks", []):
        if str(check.get("kind") or "pytest").lower() != "vitest":
            continue
        rel = check.get("p2p")
        path = task_dir / str(rel or "")
        if not rel or not path.is_file():
            report.bad("p2p", f"vitest checker 缺前端 p2p 白名单 {rel or '(未配置)'}")
            continue
        try:
            fe_p2p = read_json(path)
        except json.JSONDecodeError as error:
            report.bad("p2p", f"{rel} 不是合法 JSON", str(error))
            continue
        if fe_p2p.get("task") != meta.get("id"):
            report.bad("p2p", f"{rel} task 字段与 meta.id 不一致", str(fe_p2p.get("task")))
        fe_nodes = fe_p2p.get("tests")
        if not isinstance(fe_nodes, list):
            report.bad("p2p", f"{rel} 的 tests 必须是数组")
            continue
        bad_shape = [item for item in fe_nodes
                     if not (isinstance(item, str) and item.startswith("src/") and "::" in item)]
        if bad_shape:
            report.bad("p2p", f"{rel} 存在形状不对的 Vitest 条目", "; ".join(map(str, bad_shape[:5])))
        elif fe_nodes:
            report.ok("p2p", f"{rel} 有 {len(fe_nodes)} 条前端既有用例白名单")
        else:
            report.ok("p2p", f"{rel} 前端回归白名单为空（当前 checker 未保留既有前端测试）")
        if repo is not None:
            missing = [node for node in fe_nodes if isinstance(node, str)
                       and not (repo / "frontend" / node.split("::", 1)[0]).is_file()]
            if missing:
                report.bad("p2p", f"{rel} 引用了仓库中不存在的前端用例", "; ".join(missing[:5]))


def check_prompts(task_dir: Path, meta: dict, repo: Path | None, report: Report) -> None:
    levels = {}
    for level in (1, 2, 3):
        path = task_dir / "prompts" / f"{level}.md"
        if not path.is_file():
            report.bad("提示词", f"缺 prompts/{level}.md")
            continue
        levels[level] = read_text(path)
    if len(levels) != 3:
        return

    sizes = {level: len(text) for level, text in levels.items()}
    if sizes[1] < sizes[2] < sizes[3]:
        report.ok("提示词", f"信息量逐级递增（{sizes[1]} < {sizes[2]} < {sizes[3]} 字）")
    else:
        report.bad("提示词", "三级提示词信息量未逐级增加", str(sizes))

    # 附录 A 的接线说明在文件夹沙箱下的固定措辞（旧模板里的「当前目录 / Q:\」已随盘符池退役）
    wiring_markers = ("你面前有一个独立的代码仓库副本", "唯一允许操作的位置", "不要执行 git commit")
    for level, text in levels.items():
        missing = [marker for marker in wiring_markers if marker not in text]
        if missing:
            report.bad("提示词", f"第 {level} 级缺附录 A 的接线说明", "、".join(missing))
    report.ok("提示词", "接线说明固定出现在三级开头")

    if "验收" not in levels[1]:
        report.warn("提示词", "第 1 级未见『验收』字样", "附录 A 要求第 1 级含验收要求")

    vocabulary = collect_identifier_vocabulary(task_dir, meta, repo)
    total_leaks = 0
    for level, text in levels.items():
        leaks = scan_prompt_leaks(text, vocabulary)
        if leaks:
            total_leaks += len(leaks)
            report.bad("提示词", f"第 {level} 级泄露名词 {len(leaks)} 处", "; ".join(leaks[:4]))
        else:
            report.ok("提示词", f"第 {level} 级零名词泄露（比对 {len(vocabulary)} 个禁用名词）")
    if total_leaks == 0:
        report.ok("提示词", "§6.5 第 4 条成立")


def check_notes(task_dir: Path, report: Report) -> None:
    path = task_dir / "reference" / "notes.md"
    if not path.is_file():
        report.bad("notes", "缺 reference/notes.md")
        return
    text = read_text(path)
    if "陷阱" not in text:
        report.bad("notes", "notes.md 未记录陷阱（§6.5 诱饵点）")
    else:
        report.ok("notes", "含陷阱说明")

    boxes = re.findall(r"^\s*[-*]\s*\[([ xX])\]\s*(.+)$", text, flags=re.MULTILINE)
    if len(boxes) < len(CHECKLIST_65):
        report.bad("notes", f"§6.5 清单只找到 {len(boxes)} 条打勾项", f"应至少 {len(CHECKLIST_65)} 条")
        return
    head = text[: text.find("## 五")] if "## 五" in text else text
    unticked = [title for mark, title in boxes[: len(CHECKLIST_65)] if mark.lower() != "x"]
    if unticked:
        report.bad("notes", "§6.5 存在未打勾项", "; ".join(unticked))
    matched = 0
    for index, (_name, keywords) in enumerate(CHECKLIST_65):
        title = boxes[index][1] if index < len(boxes) else ""
        if any(keyword in title or keyword in head for keyword in keywords):
            matched += 1
        else:
            report.bad("notes", f"§6.5 第 {index + 1} 条对不上", f"标题：{title[:40]}")
    if matched == len(CHECKLIST_65):
        report.ok("notes", f"§6.5 六条清单逐条打勾（命中 {matched}/{len(CHECKLIST_65)}）")


def check_calibration(task_dir: Path, meta: dict, report: Report) -> None:
    path = task_dir / "calibration" / "results.json"
    if not path.is_file():
        return
    data = read_json(path)
    if data.get("calibrated") is not False:
        report.bad("校准", "calibrated 必须为 false（§6.4 作者不可自测）", str(data.get("calibrated")))
    else:
        report.ok("校准", "calibrated=false（盲测留给非出题模型）")
    if meta.get("calibration", {}).get("calibrated") is not False:
        report.bad("校准", "meta.calibrated 必须为 false")

    blind = data.get("blind_runs", {})
    rows = blind.get("rows") if isinstance(blind, dict) else None
    if rows:
        report.warn("校准", f"blind_runs 已有 {len(rows)} 行", "确认是他人盲测填写的")
    else:
        report.ok("校准", "blind_runs 为空表")

    summary = data.get("summary", {})
    metric = data.get("target_metric") or (meta.get("calibration") or {}).get("target_metric") or "pass_at_1"
    expected_metric = (meta.get("calibration") or {}).get("target_metric")
    if expected_metric and data.get("target_metric") != expected_metric:
        report.bad("校准", "results.target_metric 与 meta.calibration.target_metric 不一致",
                   f"results={data.get('target_metric')} meta={expected_metric}")
    if metric not in ("pass_at_1", "pass_at_3"):
        report.bad("校准", f"不支持的 target_metric {metric!r}")
    if metric == "pass_at_3":
        columns = blind.get("columns") or []
        if "rounds_used" not in columns or "pass@3" not in columns:
            report.bad("校准", "pass_at_3 盲测列必须包含 rounds_used 与 pass@3")
        for row in rows or []:
            if type(row.get("rounds_used")) is not int or not 1 <= row["rounds_used"] <= int(meta.get("attempts") or 3):
                report.bad("校准", f"{row.get('run_id', 'blind run')} 的 rounds_used 超出题目轮数")
            if not isinstance(row.get("pass@3"), bool):
                report.bad("校准", f"{row.get('run_id', 'blind run')} 缺布尔 pass@3 结果")
    for key in (metric, "confidence_interval", "in_band", "conclusion"):
        if summary.get(key) is not None and rows == []:
            report.bad("校准", f"没有盲测数据却填了 summary.{key}")

    gate_dir = task_dir / "calibration"
    anchor = _load_gate(gate_dir / "gate_fixed.json")
    partial = _load_gate(gate_dir / "gate_partial.json")
    injected = _load_gate(gate_dir / "gate_injected_x20.json")

    if anchor is not None:
        score = anchor.get("score_min")
        if score == 100.0 and anchor.get("score_max") == 100.0:
            weight_passed, weight_total = _gate_weights(anchor)
            report.ok("门禁", f"锚解 100/100（{weight_passed}/{weight_total} 权重）")
        else:
            report.bad("门禁", f"锚解不是 100 分", f"min={anchor.get('score_min')} max={anchor.get('score_max')}")
        if anchor.get("forbidden_overlap"):
            report.bad("门禁", "锚解触碰 forbidden_paths", str(anchor["forbidden_overlap"]))
        else:
            report.ok("门禁", "锚解不触碰 forbidden_paths（§5.3 可解性）")
        p2p_fail = _p2p_failures(anchor)
        if p2p_fail is not None:
            if p2p_fail:
                report.bad("门禁", f"锚解下 p2p 回归破 {p2p_fail} 条")
            else:
                report.ok("门禁", "锚解下 p2p 白名单全绿")
    else:
        report.bad("门禁", "缺 calibration/gate_fixed.json（§5.3 锚解验证）")

    if partial is not None:
        score = partial.get("score_min")
        if score is not None and score < 100.0:
            report.ok("门禁", f"半成品 {score} < 100（只修一个出口必然不满分）")
        else:
            report.bad("门禁", "半成品拿了 100 分或更高", f"min={score}")
    else:
        report.bad("门禁", "缺 calibration/gate_partial.json")

    if injected is not None:
        run = (injected.get("runs") or [{}])[0]
        groups = run.get("groups", [])
        still_green = [g.get("id") for g in groups if g.get("passed")]
        if still_green:
            report.bad("门禁", "注入态下仍有目标组是绿的", ", ".join(map(str, still_green)))
        else:
            report.ok("门禁", f"注入态 {len(groups)} 个目标组全红")
        if injected.get("stable") and injected.get("score_min") == injected.get("score_max") == 0.0:
            report.ok("门禁", f"注入态 ×{injected.get('repeat')} 稳定 0.0（不 flaky）")
        else:
            report.bad(
                "门禁",
                "注入态重复运行不稳定",
                f"stable={injected.get('stable')} min={injected.get('score_min')} max={injected.get('score_max')}",
            )
        p2p_fail = _p2p_failures(run)
        if p2p_fail:
            report.warn("门禁", f"注入态下 p2p 回归破 {p2p_fail} 条", "注入本身不该动既有用例")
    else:
        report.bad("门禁", "缺 calibration/gate_injected_x20.json（§5.3 注入态验证）")


def _load_gate(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except json.JSONDecodeError:
        return None


def _gate_weights(gate: dict) -> tuple[object, object]:
    """取门禁报告的权重口径：selfgrade 写在顶层，packgate 只有逐组明细。

    两者都没有就据 groups 明细现算，再算不出就原样显示 None（不伪造）。
    """
    passed = gate.get("weight_passed")
    total = gate.get("weight_total")
    if passed is None and gate.get("runs"):
        passed = gate["runs"][0].get("weight_passed")
        total = gate["runs"][0].get("weight_total")
    if passed is None and gate.get("runs"):
        groups = gate["runs"][0].get("groups") or []
        if groups:
            total = sum(float(g.get("weight", 0)) for g in groups)
            passed = sum(float(g.get("weight", 0)) for g in groups if g.get("passed"))
    return passed, total


def _p2p_failures(run: dict) -> list | None:
    for key in ("p2p_failures", "p2p_broken", "p2p_fail"):
        if isinstance(run.get(key), list):
            return run[key]
    return None


# --------------------------------------------------------------------------
# 顶层
# --------------------------------------------------------------------------


def check_task(task_dir: Path, repo: Path | None, full: bool = False) -> Report:
    report = Report(task=task_dir.name)
    meta_path = task_dir / "meta.json"
    if not meta_path.is_file():
        report.bad("目录树", "缺 meta.json")
        return report
    try:
        meta = read_json(meta_path)
    except json.JSONDecodeError as error:
        report.bad("meta", "meta.json 不是合法 JSON", str(error))
        return report

    if meta.get("status") == "draft" and not full:
        return _check_draft(task_dir, meta, report)
    if meta.get("status") == "draft" and full:
        report.warn(
            "草稿",
            "status=draft，按 --full 强制执行成品级检查",
            "draft 的默认语义是跳过成品级检查；本报告是强制检查的结果，不代表题目已转正",
        )

    check_layout(task_dir, report)
    check_meta(task_dir, meta, report)
    check_paths(task_dir, meta, repo, report)
    check_groups(task_dir, report, str(meta.get("tier") or ""))
    check_hidden_collectable(task_dir, report)
    check_p2p_and_prune(task_dir, meta, repo, report)
    check_frontend_p2p(task_dir, meta, repo, report)
    check_prompts(task_dir, meta, repo, report)
    check_notes(task_dir, report)
    check_calibration(task_dir, meta, report)
    return report


def _check_draft(task_dir: Path, meta: dict, report: Report) -> Report:
    """草稿脚手架只要求骨架可读，不按成品标准判红。"""
    for rel in ("prompts/1.md", "reference/notes.md"):
        if not (task_dir / rel).is_file():
            report.bad("草稿", f"缺 {rel}")
    tier = meta.get("tier")
    if tier not in TIER_SPEC:
        report.bad("meta", f"未知档位 {tier!r}")
    elif meta.get("attempts") != TIER_SPEC[tier]["attempts"]:
        report.bad("meta", f"{tier} 档 attempts 应为 {TIER_SPEC[tier]['attempts']}")
    if meta.get("calibration", {}).get("calibrated") is not False:
        report.bad("校准", "草稿的 calibrated 也必须为 false")
    report.skip("草稿", "status=draft，跳过成品级检查（注入/隐藏测试/门禁/提示词）")
    return report


def iter_tasks(pack_root: Path) -> list[Path]:
    tasks_dir = pack_root / "tasks"
    if not tasks_dir.is_dir():
        return []
    return sorted(path for path in tasks_dir.iterdir() if path.is_dir() and (path / "meta.json").is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务包完整性自检（§8 + §6.5）")
    parser.add_argument("--pack", default=str(Path(__file__).resolve().parent.parent), help="packs/core 目录")
    parser.add_argument("--task", default="", help="只检查某一题，如 T1-01")
    parser.add_argument("--repo", default="", help="受测仓库根目录，给了就做交叉核对")
    parser.add_argument("--out", default="", help="把 JSON 报告写到这里")
    parser.add_argument(
        "--full",
        action="store_true",
        help="对 status=draft 的题目也强制执行成品级检查（默认草稿只查骨架）",
    )
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    pack_root = Path(args.pack).resolve()
    repo = Path(args.repo).resolve() if args.repo else None
    if repo is not None and not repo.is_dir():
        print(f"仓库路径不存在：{repo}", file=sys.stderr)
        return 2

    tasks = iter_tasks(pack_root)
    if args.task:
        tasks = [path for path in tasks if path.name == args.task]
        if not tasks:
            print(f"找不到任务 {args.task}", file=sys.stderr)
            return 2
    if not tasks:
        print(f"{pack_root} 下没有任务", file=sys.stderr)
        return 2

    reports = [check_task(task_dir, repo, full=args.full) for task_dir in tasks]
    payload = {
        "pack": posix(pack_root),
        "repo": posix(repo) if repo else None,
        "tasks": [
            {
                "task": report.task,
                "red": report.red_count,
                "yellow": report.yellow_count,
                "findings": [
                    {"level": level, "group": group, "message": message, "evidence": evidence}
                    for level, group, message, evidence in report.sorted_findings()
                ],
            }
            for report in reports
        ],
    }

    for report in reports:
        head = f"── {report.task} "
        print(head + "─" * max(0, 58 - len(report.task)))
        for level, group, message, evidence in report.sorted_findings():
            if level == GREEN:
                continue
            line = f"  [{level}] {group}：{message}"
            if evidence:
                line += f"\n         证据：{evidence}"
            print(line)
        print(f"  红 {report.red_count} · 黄 {report.yellow_count} · 绿 {sum(1 for f in report.findings if f[0] == GREEN)}")

    total_red = sum(report.red_count for report in reports)
    print("─" * 62)
    print(f"合计：{len(reports)} 题，红 {total_red} 项" + ("" if repo else "（未给 --repo，跳过与仓库的交叉核对）"))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 报告：{out_path}")

    return 1 if total_red else 0


if __name__ == "__main__":
    raise SystemExit(main())
