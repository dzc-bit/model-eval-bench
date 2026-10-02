"""任务包加载（只读 packs/，评测台不写 packs）。

任务包规范见设计文档 §8：
    packs/core/tasks/T2-04/
    ├─ meta.json
    ├─ prompts/1.md 2.md 3.md
    ├─ inject/apply.json 或 patches/*.patch
    ├─ hidden/tests_hidden/... + hidden/groups.json
    ├─ p2p.json
    └─ reference/fix.patch partial.patch notes.md

本模块只做「读 + 校验 + 归一化」，不解释注入语义（那是 snapshot/sandbox 的事）。
读侧必须容忍缺项：packs/ 为空、meta 写了一半，都要给出能看懂的错误而不是崩。
"""

from __future__ import annotations

import fnmatch
import os
from typing import Any

from . import errors, util

#: 任务 meta 必填字段
REQUIRED_META = ("id", "title", "tier")
#: 档位 → 允许尝试次数（设计文档 §6.3）
TIER_ATTEMPTS = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
    "king": 3,
    "初级": 1,
    "中级": 2,
    "高级": 3,
    "王者": 3,
}
#: 档位别名
TIER_ALIASES = {
    "t1": "easy", "t2": "medium", "t3": "hard", "t4": "king",
    # 真实题包 index.json 用的英文档位名；缺了它 normalize_tier("primary")
    # 会原样返回，attempts 默认取到 easy 之外的档、UI 显示原始串
    "primary": "easy", "intermediate": "medium", "advanced": "hard", "expert": "king",
    "初级": "easy", "初级 t1": "easy",
    "中级": "medium", "中级 t2": "medium",
    "高级": "hard", "高级 t3": "hard",
    "王者": "king", "王者 t4": "king",
}


def find_pack(cfg: dict, task_id: str) -> str:
    """在全部分包下找任务目录，返回其绝对路径。"""
    packs_root = cfg["packs_root"]
    task_id = util.sanitize_id(task_id)
    if not os.path.isdir(packs_root):
        raise errors.HarnessError(
            errors.E_TASK_NOT_FOUND,
            "任务库是空的，还没有任何题包。请先在 packs\\ 下放入任务包目录。",
            packs_root,
        )
    # 常见位置优先：packs/core/tasks/<id>
    for candidate in (
        os.path.join(packs_root, "core", "tasks", task_id),
        os.path.join(packs_root, task_id),
    ):
        if os.path.isdir(candidate):
            return candidate
    for repo_dir in sorted(os.listdir(packs_root)):
        base = os.path.join(packs_root, repo_dir)
        if not os.path.isdir(base):
            continue
        for tasks_dir in ("tasks", ""):
            hit = os.path.join(base, tasks_dir, task_id) if tasks_dir else os.path.join(base, task_id)
            if os.path.isdir(hit):
                return hit
    raise errors.HarnessError(
        errors.E_TASK_NOT_FOUND,
        "找不到任务 %s。请到「任务库」确认题号是否正确。" % task_id,
        packs_root,
    )


def normalize_tier(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return TIER_ALIASES.get(text, text or "medium")


def load_meta(cfg: dict, task_id: str) -> dict:
    """读并归一化一道题的 meta；字段缺失给默认值，结构错误才报错。"""
    pack_dir = find_pack(cfg, task_id)
    meta_path = os.path.join(pack_dir, "meta.json")
    raw = util.read_json(meta_path, default=None)
    if raw is None:
        raise errors.HarnessError(
            errors.E_TASK_INVALID,
            "任务 %s 缺少 meta.json 或内容不是合法 JSON，无法入库。" % task_id,
            meta_path,
        )
    if not isinstance(raw, dict):
        raise errors.HarnessError(errors.E_TASK_INVALID, "任务 %s 的 meta.json 顶层必须是对象。" % task_id)

    for field in REQUIRED_META:
        if not raw.get(field):
            raise errors.HarnessError(
                errors.E_TASK_INVALID,
                "任务 %s 的 meta.json 缺少必填字段 %s。" % (task_id, field),
                meta_path,
            )

    tier = normalize_tier(raw.get("tier"))
    attempts = raw.get("attempts") or TIER_ATTEMPTS.get(tier, 2)
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = TIER_ATTEMPTS.get(tier, 2)
    attempts = max(1, attempts)

    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    meta = {
        "id": util.sanitize_id(raw.get("id")),
        "title": str(raw.get("title")),
        "tier": tier,
        "attempts": attempts,
        "summary": str(raw.get("summary") or ""),
        "symptom": str(raw.get("symptom") or ""),
        "tags": [str(t) for t in (raw.get("tags") or [])],
        "repo": raw.get("repo") if isinstance(raw.get("repo"), dict) else {},
        "allowed_paths": [str(p).replace("\\", "/") for p in (raw.get("allowed_paths") or [])],
        "forbidden_paths": [str(p).replace("\\", "/") for p in (raw.get("forbidden_paths") or [])],
        "visible": raw.get("visible") if isinstance(raw.get("visible"), dict) else {},
        "redactions": [r for r in (raw.get("redactions") or []) if isinstance(r, dict)],
        "checks": [c for c in (raw.get("checks") or []) if isinstance(c, dict)],
        "budget": {
            "grade_timeout_s": int(budget.get("grade_timeout_s", cfg["timeouts"]["grade_default_s"])),
            "diff_line_cap": int(budget.get("diff_line_cap", cfg["grade"]["diff_line_cap"])),
        },
        "calibration": raw.get("calibration") if isinstance(raw.get("calibration"), dict) else {},
        "pack_dir": pack_dir,
    }
    if not meta["allowed_paths"]:
        # 没写 allowed_paths 会让越界检测形同虚设，这里按设计文档的常见档位给个保守默认
        meta["allowed_paths"] = ["backend/**", "frontend/src/**"]
    return meta


def load_prompts(meta: dict) -> list:
    """读三级提示词；缺级就少一级（前端按实际条数渲染页签）。"""
    prompts_dir = os.path.join(meta["pack_dir"], "prompts")
    prompts = []
    for level in range(1, meta["attempts"] + 1):
        path = os.path.join(prompts_dir, "%d.md" % level)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                body = util.decode_output(fh.read())
        except OSError:
            continue
        prompts.append({"level": level, "text": body.strip()})
    return prompts


def load_hidden_for(meta: dict, spec: dict) -> dict:
    """按某一条 check 声明读隐藏测试目录与分组配置。

    一道题可以有多条 check（例如 pytest 一条、vitest 一条），各自的隐藏测试
    目录与 groups.json 各自独立。
    """
    hidden_rel = str(spec.get("hidden") or "hidden/tests_hidden")
    groups_rel = str(spec.get("groups") or "hidden/groups.json")
    p2p_rel = str(spec.get("p2p") or "p2p.json")
    hidden_dir = os.path.join(meta["pack_dir"], *hidden_rel.replace("\\", "/").split("/"))
    groups_path = os.path.join(meta["pack_dir"], *groups_rel.replace("\\", "/").split("/"))
    p2p_path = os.path.join(meta["pack_dir"], *p2p_rel.replace("\\", "/").split("/"))

    groups_doc = util.read_json(groups_path, default=None)
    if groups_doc is None:
        raise errors.HarnessError(
            errors.E_TASK_INVALID,
            "任务 %s 缺少分组配置 %s，分组部分分无法计算。" % (meta["id"], groups_rel),
            groups_path,
        )
    raw_groups = groups_doc.get("groups") if isinstance(groups_doc, dict) else groups_doc
    if not isinstance(raw_groups, list) or not raw_groups:
        raise errors.HarnessError(
            errors.E_TASK_INVALID,
            "任务 %s 的分组配置里没有 groups 数组。" % meta["id"],
            groups_path,
        )

    groups = []
    for item in raw_groups:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        mode = str(item.get("mode") or "scored")
        try:
            weight = float(item.get("weight", 1))
        except (TypeError, ValueError):
            weight = 1.0
        # title 是组名（题包几乎都不写），port 是「这个组在守哪个出口」的中文口径。
        # 只把 tests（用例 node id）留在后端跑评分用，port 会随报告与任务详情下发给
        # 校验弹窗做解释（模型看不到控制台，公开的是口径不是用例）。
        groups.append({
            "id": str(item["id"]),
            "weight": weight,
            "mode": mode,
            "tests": [str(t) for t in (item.get("tests") or [])],
            "title": str(item.get("title") or ""),
            "port": str(item.get("port") or item.get("note") or ""),
        })

    p2p_doc = util.read_json(p2p_path, default={}) or {}
    if isinstance(p2p_doc, list):
        p2p_tests = [str(t) for t in p2p_doc]
    else:
        p2p_tests = [str(t) for t in (p2p_doc.get("tests") or [])]

    if not os.path.isdir(hidden_dir):
        raise errors.HarnessError(
            errors.E_TASK_INVALID,
            "任务 %s 的隐藏测试目录不存在，无法评分。" % meta["id"],
            hidden_dir,
        )
    kind = str(spec.get("kind") or "").lower()
    if kind == "vitest":
        # vitest 以 frontend/ 为 root（vitest.config.ts 在 frontend/ 下），隐藏测试
        # 必须落在 root 之内才会被收集。旧实现把 hidden-fe **整层**搬进 frontend/src，
        # 测试位置其实是对的（copy_tree 复制源目录内容 → frontend/src/tests_hidden_fe/），
        # 代价是题包配置 groups_fe.json 也一起进了被测源码树，而且 overlay_rel 仍是
        # "hidden-fe"，与真实落点不一致，让 build_grade_tree 的按层去形同虚设。
        # 现在只搬隐藏测试那一层，目的地写全路径：与 groups.json 里
        # `src/tests_hidden_fe/...` 的用例 ID 一致，也满足隐藏测试里 `../<模块>`
        # 的相对导入（与 src 下被测模块互为兄弟目录）。
        overlay_src = hidden_dir
        hidden_name = hidden_dir.replace("\\", "/").rstrip("/").rpartition("/")[2]
        overlay_rel = "/".join(("frontend", "src", hidden_name))
        prefix = "src"
    else:
        # 评分树里 hidden 那一层要保持题包的原样布局：题包写 hidden/tests_hidden/xx.py，
        # 评分树里就是 hidden/tests_hidden/xx.py，groups.json 里可以直接写全路径。
        overlay_rel, overlay_src = _overlay_layer(meta, hidden_rel, hidden_dir)
        # groups.json 里的用例 ID 通常是**相对 overlay 层**写的（如 `tests_hidden/x.py::t`），
        # 但 pytest 的 cwd 是评分树根，路径必须补上 overlay 前缀（`hidden/tests_hidden/...`）
        # 才找得到。这里统一归一成"相对评分树根"的形态，两种写法都能跑。
        prefix = "" if overlay_rel in ("", ".") else overlay_rel.replace("\\", "/").strip("/")
    # 搬运目的地与上面算出的 overlay_rel 一致：vitest 分支已经把它指到
    # frontend/src/<目录名>（vitest 的 root 之内），其余走题包原样布局。
    overlay_dest = overlay_rel
    for group in groups:
        group["tests"] = [_qualify_node_id(t, prefix) for t in group["tests"]]
    p2p_tests = [_qualify_node_id(t, "") for t in p2p_tests]
    return {
        "hidden_dir": hidden_dir,
        "hidden_rel": hidden_rel,
        "overlay_src": overlay_src,
        "overlay_rel": overlay_rel,
        "overlay_dest_rel": overlay_dest,
        "groups": groups,
        "p2p_tests": p2p_tests,
        "groups_path": groups_path,
    }


def _qualify_node_id(node_id: str, prefix: str) -> str:
    """把用例 ID 归一成「相对评分树根」的路径形态。

    - 空前缀 / 绝对路径 / 已经是 `prefix/...` / 不带路径的裸函数名 → 原样返回；
    - 其余（`tests_hidden/x.py::t` + 前缀 `hidden`）→ 补成 `hidden/tests_hidden/x.py::t`。
    """
    node_id = str(node_id or "").strip()
    if not node_id or not prefix or os.path.isabs(node_id):
        return node_id
    file_part, sep, rest = node_id.partition("::")
    path = file_part.replace("\\", "/").lstrip("./")
    # 没有路径部分（裸函数名）交给 resolver 按尾名匹配，不需要前缀
    if "/" not in path:
        return node_id
    if path.split("/", 1)[0] == prefix:
        return node_id                      # 已经带了前缀，别补成 hidden/hidden/...
    qualified = "%s/%s" % (prefix, path)
    return "%s::%s" % (qualified, rest) if sep else qualified


def _overlay_layer(meta: dict, hidden_rel: str, hidden_dir: str) -> tuple:
    """算出"要整目录搬进评分树的那一层"及其评分树内的相对路径。

    spec 写 `hidden/tests_hidden` 时搬的是题包的 `hidden/` 整层（groups.json 一起进评分树），
    评分树里的相对路径同样是 `hidden`；只写一层（如 `tests_hidden`）时只搬那一个目录，
    落到评分树的 `hidden/` 下。
    """
    parts = [p for p in hidden_rel.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        candidate = os.path.join(meta["pack_dir"], parts[0])
        if os.path.isdir(candidate):
            return parts[0], candidate
    return "hidden", hidden_dir


#: 注入补丁的候选目录（按优先级）。真实题包用 `inject/patches/`（README §6 与出题
#: 流水线的 inject_edits.py 都写这里），早期 fixture 与设计文档 §8 写的是 `patches/`。
#: 两个都认，避免"补丁在盘上、代码读不到、于是静默按未注入骨架出题"的假通过。
PATCH_DIRS = ("inject/patches", "patches")


def find_patches_dir(meta: dict) -> str:
    """定位题包里的注入补丁目录；都没有则返回空串。"""
    for rel in PATCH_DIRS:
        candidate = os.path.join(meta["pack_dir"], *rel.split("/"))
        if os.path.isdir(candidate):
            return candidate
    return ""


def list_patches(meta: dict) -> list:
    """按文件名顺序列出注入补丁。

    同时兼容 `inject/patches/`（真实题包）与 `patches/`（设计文档 §8 / 旧 fixture）。
    两个目录都存在时以 `inject/patches/` 为准，只取一份，避免补丁被应用两次。
    """
    patches_dir = find_patches_dir(meta)
    if not patches_dir:
        return []
    names = sorted(n for n in os.listdir(patches_dir) if n.endswith((".patch", ".diff")))
    return [os.path.join(patches_dir, n) for n in names]


def load_inject_plan(meta: dict) -> list:
    """读 inject/apply.json 的有序注入指令（patches 之外的另一条注入通道）。"""
    path = os.path.join(meta["pack_dir"], "inject", "apply.json")
    doc = util.read_json(path, default=None)
    if doc is None:
        return []
    steps = doc.get("steps") if isinstance(doc, dict) else doc
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict)]


def allowed_match(rel: str, patterns: list) -> bool:
    """相对路径是否落在 allowed_paths 内（支持 `**`）。"""
    return util.match_any(rel, patterns)


def task_summary(cfg: dict, pack_dir: str) -> dict | None:
    """给 /api/tasks 用的轻量摘要；meta 坏了就返回 None 由上层过滤。"""
    meta_path = os.path.join(pack_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        raw = util.read_json(meta_path, default=None)
    except OSError:
        return None
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    tier = normalize_tier(raw.get("tier"))
    calib = raw.get("calibration") if isinstance(raw.get("calibration"), dict) else {}
    return {
        "id": util.sanitize_id(raw.get("id")),
        "title": str(raw.get("title") or ""),
        "tier": tier,
        "attempts": int(raw.get("attempts") or TIER_ATTEMPTS.get(tier, 2)),
        "summary": str(raw.get("summary") or ""),
        "symptom": str(raw.get("symptom") or ""),
        "tags": [str(t) for t in (raw.get("tags") or [])],
        "calibrated": bool(calib.get("calibrated")),
        "target_band": calib.get("target_band") or [],
        "pack_dir": pack_dir,
    }


def list_tasks(cfg: dict) -> list:
    """列出全部任务（packs 为空时返回空列表，不报错）。"""
    packs_root = cfg["packs_root"]
    out = []
    if not os.path.isdir(packs_root):
        return out
    for repo_dir in sorted(os.listdir(packs_root)):
        base = os.path.join(packs_root, repo_dir)
        if not os.path.isdir(base) or repo_dir.startswith("."):
            continue
        task_roots = [os.path.join(base, "tasks")] if os.path.isdir(os.path.join(base, "tasks")) else [base]
        for task_root in task_roots:
            if not os.path.isdir(task_root):
                continue
            for name in sorted(os.listdir(task_root)):
                pack_dir = os.path.join(task_root, name)
                if not os.path.isdir(pack_dir) or name.startswith("."):
                    continue
                item = task_summary(cfg, pack_dir)
                if item:
                    item["repo_id"] = repo_dir
                    out.append(item)
    return out


def reference_path(meta: dict, name: str = "fix.patch") -> str | None:
    """参考解补丁路径；没有就返回 None（不是错误，只是这题没写）。"""
    path = os.path.join(meta["pack_dir"], "reference", name)
    return path if os.path.isfile(path) else None


def check_plan(meta: dict) -> list:
    """这道题这一轮会查什么：按 check 汇总分组口径（不含隐藏用例 id）。

    给控制台的「校验」弹窗用：用户点下校验的那一刻就能看到「这次要查哪几个出口、
    每个出口在守什么、权重多少」，不必等出分才知道自己在评什么。**用例 node id
    一律不下发**——那是隐藏用例的落点，模型看不到控制台也不该有第二条泄露面。

    题包写坏（缺 groups.json、目录不存在）时不抛错：返回 []，由报告与任务详情
    各自按「题包坏了」的老路径处理；弹窗拿不到计划就少解释一段，不影响校验本身。
    """
    specs = meta.get("checks") or []
    plan: list = []
    for index, spec in enumerate(specs):
        try:
            hidden = load_hidden_for(meta, spec if isinstance(spec, dict) else {})
        except errors.HarnessError:
            continue
        for group in hidden.get("groups") or []:
            if group.get("mode") == "regression":
                continue
            plan.append({
                "id": group["id"],
                "title": group.get("title") or "",
                "port": group.get("port") or "",
                "weight": group.get("weight", 1),
                "kind": str((spec if isinstance(spec, dict) else {}).get("kind") or ""),
                "spec_index": index,
            })
    return plan


def reference_patches(meta: dict) -> list:
    """全部参考补丁（fix / partial / 历史修复），用于相似度标记。"""
    ref_dir = os.path.join(meta["pack_dir"], "reference")
    if not os.path.isdir(ref_dir):
        return []
    return [
        os.path.join(ref_dir, n)
        for n in sorted(os.listdir(ref_dir))
        if n.endswith((".patch", ".diff")) and os.path.isfile(os.path.join(ref_dir, n))
    ]
