"""报告组装（设计文档 §5.2）。

内容：逐组红绿 + 失败摘要、violations / similarity / diff 统计、
与上一轮对比「哪些组红转绿」、next_hint（进入下一轮 / 换模型 / 揭晓）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import util

#: next_hint 的动作码（前端据此决定主按钮）
ACTION_COMPLETE = "complete"            # 全绿，本题结束
ACTION_PROMOTE = "promote"              # 还有轮次，进入下一级提示词
ACTION_FIX = "fix"                      # 越界或回归，先修沙箱
ACTION_REVEAL = "reveal"                # 轮次用尽，看参考解或换模型


def _group_state(groups: List[dict]) -> Dict[str, bool]:
    return {g["id"]: bool(g["passed"]) for g in groups}


def compare_rounds(previous: Optional[dict], current: dict) -> dict:
    """与上一轮对比：哪些组红转绿、哪些组又红了。"""
    if not previous:
        return {"has_previous": False, "turned_green": [], "stayed_red": [], "regressed": []}
    before = _group_state(previous.get("groups") or [])
    after = _group_state(current.get("groups") or [])
    turned_green, stayed_red, regressed = [], [], []
    for gid, passed in after.items():
        was = before.get(gid)
        if passed and was is False:
            turned_green.append(gid)
        elif not passed and was is True:
            regressed.append(gid)
        elif not passed:
            stayed_red.append(gid)
    return {
        "has_previous": True,
        "previous_attempt": previous.get("attempt"),
        "previous_score": previous.get("score"),
        "turned_green": turned_green,
        "stayed_red": stayed_red,
        "regressed": regressed,
    }


def decide_next(grade_result: dict, meta: dict, revealed: bool = False) -> dict:
    """给出下一步建议（设计文档 §5.2 的 next_hint）。"""
    attempt = int(grade_result.get("attempt") or 1)
    allowed = int(meta.get("attempts") or 1)
    violations = grade_result.get("violations") or []
    regressions = grade_result.get("regressions") or []
    score = float(grade_result.get("score") or 0.0)

    if violations:
        return {
            "action": ACTION_FIX,
            "label": "先处理越界改动",
            "reason": "有 %d 处改动超出了允许范围，本轮已作废。清空改动后重来，或把这些改动收回允许范围内。"
                      % len(violations),
            "can_promote": False,
        }
    if regressions:
        return {
            "action": ACTION_FIX,
            "label": "先修回归",
            "reason": "破坏了 %d 条原本通过的用例，本轮已作废。请在允许范围内修好再校验。" % len(regressions),
            "can_promote": False,
        }
    if score >= 100.0:
        return {
            "action": ACTION_COMPLETE,
            "label": "全绿，本题完成",
            "reason": "所有分组都通过了。可以换个模型重来，或查看参考解收尾。",
            "can_promote": False,
        }
    if revealed:
        return {
            "action": ACTION_REVEAL,
            "label": "已揭晓本轮",
            "reason": "这一轮已看过参考解，按规则不计入通过率统计。点「清空改动」可以换个模型重来。",
            "can_promote": False,
        }
    if attempt < allowed:
        return {
            "action": ACTION_PROMOTE,
            "label": "进入第 %d 轮" % (attempt + 1),
            "reason": "还有 %d 次尝试机会。下一级提示词会多给一层信息（仍不含文件名）。" % (allowed - attempt),
            "can_promote": True,
        }
    return {
        "action": ACTION_REVEAL,
        "label": "轮次用尽",
        "reason": "已经用完 %d 次机会。可以查看参考解（该轮标记为已揭晓，不计入统计），或换个模型重来。"
                  % allowed,
        "can_promote": False,
    }


def summarize_groups(groups: List[dict]) -> dict:
    """给前端列表用的分组摘要（不带大段失败堆栈）。"""
    out = []
    for group in groups:
        failed = [c for c in group.get("cases", []) if c.get("outcome") != "passed"]
        out.append({
            "id": group["id"],
            "title": group.get("title") or group["id"],
            "weight": group.get("weight", 1),
            "passed": bool(group.get("passed")),
            "total": group.get("total", 0),
            "passed_count": group.get("passed_count", 0),
            "failed_tests": [c["node_id"] for c in failed],
            "first_failure": failed[0]["message"] if failed else "",
        })
    return {
        "groups": out,
        "green": sum(1 for g in out if g["passed"]),
        "red": sum(1 for g in out if not g["passed"]),
    }


def build(run: dict, meta: dict, grade_result: dict,
          previous: Optional[dict] = None) -> dict:
    """把校验结果组装成最终报告（写盘的那一份）。

    下划线开头的键是内部中转数据（例如 diff 正文），不进报告。
    """
    report = {k: v for k, v in grade_result.items() if not k.startswith("_")}
    report["task_title"] = meta.get("title", "")
    report["tier"] = meta.get("tier", "")
    report["attempts_allowed"] = meta.get("attempts", 1)
    report["allowed_paths"] = meta.get("allowed_paths", [])
    report["comparison"] = compare_rounds(previous, grade_result)
    report["next_hint"] = decide_next(report, meta, revealed=bool(run.get("revealed")))
    report["summary"] = summarize_groups(grade_result.get("groups") or [])
    report["model_note"] = str(run.get("note") or "")
    report["baseline_digest"] = str(run.get("baseline_digest") or "")
    report["drive"] = str(run.get("drive") or "")
    report["sandbox"] = str(run.get("sandbox") or "")
    report["generated_at"] = util.iso_now()
    return report


def notes_markdown(run: dict, meta: dict, report: dict) -> str:
    """生成 runs/<任务>/<模型>/<时间>/notes.md 里的中文小结。"""
    lines = [
        "# %s · %s 第 %d 轮" % (meta.get("id", ""), run.get("model", ""), int(run.get("attempt") or 1)),
        "",
        "- 时间：%s" % report.get("graded_at", ""),
        "- 得分：**%s / 100**" % report.get("score", 0),
        "- 盘符：%s" % (run.get("drive") or "（已释放）"),
        "- 基线指纹：%s" % str(report.get("baseline_digest") or "")[:12],
        "",
        "## 分组",
    ]
    for group in report.get("summary", {}).get("groups", []):
        lines.append("- %s %s（权重 %s，%d/%d）" % (
            "✅" if group["passed"] else "⛔", group["title"], group["weight"],
            group["passed_count"], group["total"]))
        if not group["passed"] and group.get("first_failure"):
            lines.append("    - 首个失败：%s" % group["first_failure"].splitlines()[0][:200])
    if report.get("regressions"):
        lines += ["", "## 回归破坏（本轮作废）"]
        for item in report["regressions"]:
            lines.append("- %s" % item.get("node_id"))
    if report.get("violations"):
        lines += ["", "## 越界改动（本轮作废）"]
        for item in report["violations"]:
            lines.append("- %s（%s）%s" % (item.get("path"), item.get("change"), item.get("reason")))
    hint = report.get("next_hint") or {}
    lines += ["", "## 下一步", "", "- %s：%s" % (hint.get("label", ""), hint.get("reason", ""))]
    if run.get("note"):
        lines += ["", "## 备注", "", run["note"]]
    return "\n".join(lines) + "\n"
