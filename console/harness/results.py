"""成绩台账（2026-10-02「结束」语义收敛）。

台账是一个**与运行记录脱钩**的持久文件：

    runs/_results/ledger.json

工作台校验跑完后点「结束本轮」，先把这一轮的成绩按新口径写成一条台账条目，
再真删整条运行记录（记录目录 + 沙箱 + 评分树）。这样记分板与排行榜在记录
消失之后仍然有数，而「废弃本轮」什么都不写——两个收尾出口的唯一区别就是
进不进这个文件。

口径（与旧记分板对齐，见 README §5）：
- 一条条目 = 一次「结束」的尝试，台账**保留每一次**结束的条目。
- 只收作数轮：被「继续对话（本轮分数作废）」作废的、以及回归/校验器故障判
  无效的轮次，以及整轮已揭晓参考解的运行，都不进台账。
- 例外（2026-10-05）：**因改动越界整轮作废的运行**写一条 ``kind="out_of_bounds"``
  的留痕条目——不带分数语义（score 恒 0、passed 恒假），不参与记分板与排行榜
  的任何统计，只是把「这次尝试发生过、为什么作废」留在成绩体系里；violations
  摘要随条目落盘。不作此留痕的话，越界尝试在台账里会彻底蒸发。
- 条目的代表分是它作数轮里的最高分（`score`）；`pass1` 看第 1 轮。
- 榜单取最高分那条，Wilson 区间随之废弃（样本量不再有意义）。

目录名带下划线是必须的：`runs.list_runs` 不下钻 `_` 开头的层，台账因此
天然不参与「幽灵记录」判定，删空父目录的 `_prune_empty_dirs` 也够不着它。
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Dict, List, Optional

from . import util

#: 台账目录（在 runs_root 之下，`_` 前缀让 list_runs 自动绕开）。
RESULTS_DIRNAME = "_results"
#: 台账文件名
LEDGER_FILENAME = "ledger.json"

#: 台账结构版本；将来改条目形状时用它判断是否需要迁移。
LEDGER_VERSION = 2

#: 进程内串行化台账写入（读-改-写三步必须同锁）
_LEDGER_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------

def ledger_dir(cfg: dict) -> str:
    """台账目录绝对路径。"""
    return os.path.join(cfg["runs_root"], RESULTS_DIRNAME)


def ledger_path(cfg: dict) -> str:
    """台账文件绝对路径。"""
    return os.path.join(ledger_dir(cfg), LEDGER_FILENAME)


def load_entries(cfg: dict) -> List[dict]:
    """读全部条目（时间正序：最早结束的在前）。

    文件缺失 / 坏 JSON 一律当空台账处理：台账坏了不该让整个记分板 500，
    更不该让工作台进不去。真要恢复得人工看文件。
    """
    doc = util.read_json(ledger_path(cfg), default=None)
    if not isinstance(doc, dict):
        return []
    raw = doc.get("entries")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("task") and e.get("model_raw")]


def write_entries(cfg: dict, entries: List[dict]) -> None:
    """整份覆写（调用方负责持锁）。"""
    util.ensure_dir(ledger_dir(cfg))
    util.write_json_atomic(ledger_path(cfg), {
        "version": LEDGER_VERSION,
        "updated_at": util.iso_now(),
        "entries": list(entries),
    })


def has_source_run(cfg: dict, run_id: str) -> bool:
    """这条运行记录是否已经有台账条目了（回填之后又点「结束」时不重复记一条）。"""
    wanted = str(run_id or "")
    if not wanted:
        return False
    return any(str(e.get("source_run_id") or "") == wanted for e in load_entries(cfg))


def find_by_source_run(cfg: dict, run_id: str) -> Optional[dict]:
    """按来源运行记录取台账条目（没有就 None）。

    跑批条目靠它落定：用户点「结束本轮」的那一刻运行记录就被整条删掉，
    批次只有从台账里才读得到「这次尝试最终是多少分、几轮、进没进榜」。
    """
    wanted = str(run_id or "")
    if not wanted:
        return None
    for entry in load_entries(cfg):
        if str(entry.get("source_run_id") or "") == wanted:
            return entry
    return None


def new_entry_id(entries: List[dict]) -> str:
    """台账条目编号：按已有最大值 +1 递增，不复用被删掉的号。"""
    high = 0
    for entry in entries:
        raw = str(entry.get("entry_id") or "")
        if raw.startswith("res-"):
            try:
                high = max(high, int(raw[4:]))
            except ValueError:
                continue
    return "res-%06d" % (high + 1)


def append_entry(cfg: dict, entry: dict) -> dict:
    """追加一条条目并落盘，返回写进去的那条。"""
    with _LEDGER_LOCK:
        entries = load_entries(cfg)
        record = dict(entry)
        record["entry_id"] = new_entry_id(entries)
        entries.append(record)
        write_entries(cfg, entries)
        return record


# --------------------------------------------------------------------------
# 台账条目
# --------------------------------------------------------------------------

#: 条目字段白名单：写出去的东西有限，别把运行记录的内部状态漏进台账。
ENTRY_FIELDS = (
    "entry_id", "task", "model", "model_raw", "source_run_id", "origin",
    "kind", "rounds", "best_round", "score", "passed", "pass1",
    "model_work_seconds", "wall_seconds", "graded_at", "ended_at", "groups",
    "violations",
)

#: 计分条目的 kind。旧条目没有 kind 字段，读取时一律视为本值（见 entry_kind）。
KIND_GRADED = "graded"
#: 越界作废留痕条目：不计分、不进平均、不参与榜单比较，只证明「试过、被作废」。
KIND_OUT_OF_BOUNDS = "out_of_bounds"


def entry_kind(entry: dict) -> str:
    """条目的种类；老条目没有 kind 字段，一律按计分条目处理。"""
    return str(entry.get("kind") or KIND_GRADED)


def make_entry(task: object, model: object, model_raw: object, *,
               source_run_id: object = "", origin: str = "run",
               kind: str = KIND_GRADED,
               rounds: int = 1, best_round: int = 1, score: float = 0.0,
               passed: bool = False, pass1: bool = False,
               model_work_seconds: Optional[float] = None,
               wall_seconds: Optional[float] = None,
               graded_at: object = "", groups: Optional[List[dict]] = None,
               violations: Optional[List[dict]] = None) -> dict:
    """组装一条台账条目（只保留 ENTRY_FIELDS）。"""
    def _num(value):
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    entry = {
        "entry_id": "",
        "task": str(task or ""),
        "model": str(model or ""),
        "model_raw": str(model_raw or ""),
        "source_run_id": str(source_run_id or ""),
        "origin": origin,
        "kind": str(kind or KIND_GRADED),
        "rounds": max(1, int(rounds or 1)),
        "best_round": max(1, int(best_round or 1)),
        "score": _num(score) or 0.0,
        "passed": bool(passed),
        "pass1": bool(pass1),
        "model_work_seconds": _num(model_work_seconds),
        "wall_seconds": _num(wall_seconds),
        "graded_at": str(graded_at or ""),
        "ended_at": util.iso_now(),
    }
    if isinstance(groups, list):
        entry["groups"] = [
            {
                "id": str(group.get("id") or ""),
                "port": str(group.get("port") or ""),
                "weight": group.get("weight", 1),
                "passed": bool(group.get("passed")),
                "total": group.get("total", 0),
                "passed_count": group.get("passed_count", 0),
            }
            for group in groups
            if isinstance(group, dict) and group.get("id")
        ]
    if isinstance(violations, list):
        # 留痕只带越界事实（路径 + 变更类型 + 一句原因），不带评分树的内部细节。
        entry["violations"] = [
            {
                "path": str(v.get("path") or ""),
                "change": str(v.get("change") or ""),
                "reason": str(v.get("reason") or ""),
            }
            for v in violations
            if isinstance(v, dict) and v.get("path")
        ] or None
    return entry


# --------------------------------------------------------------------------
# 按档案清理
# --------------------------------------------------------------------------

def matches_model(entry: dict, owned_prefix: str, legacy_ids: set) -> bool:
    """台账条目是否属于某个供应商。

    匹配口径必须与 runs.delete_provider 完全一致（限定名前缀 + 老档案 id），
    否则删完档案会留下一列查无此人的幽灵成绩。
    """
    for key in ("model_raw", "model"):
        name = str(entry.get(key) or "")
        if not name:
            continue
        if owned_prefix and name.startswith(owned_prefix):
            return True
        if name in legacy_ids:
            return True
    return False


def count_for_models(cfg: dict, owned_prefix: str, legacy_ids: set) -> int:
    """名下台账条目数（只读，给删除确认框与 run_count 用）。"""
    return sum(1 for e in load_entries(cfg) if matches_model(e, owned_prefix, legacy_ids))


def remove_for_models(cfg: dict, owned_prefix: str, legacy_ids: set) -> List[str]:
    """删掉属于该供应商的全部台账条目，返回被删的 entry_id。

    真删：条目代表已经结束的成绩，不存在「先留着以后再说」。
    """
    with _LEDGER_LOCK:
        entries = load_entries(cfg)
        kept: List[dict] = []
        removed: List[str] = []
        for entry in entries:
            if matches_model(entry, owned_prefix, legacy_ids):
                removed.append(str(entry.get("entry_id") or ""))
                continue
            kept.append(entry)
        if removed:
            write_entries(cfg, kept)
        return removed


# --------------------------------------------------------------------------
# 便捷统计
# --------------------------------------------------------------------------

def group_by_cell(cfg: dict, canonical: Callable[[str], str]) -> Dict[tuple, List[dict]]:
    """按 (任务 × 模型身份) 分组。

    模型身份由调用方的 ``canonical`` 现算（与 runs.canonical_model 同一条规则）：
    台账里存的是写入当时的档案原名，读的时候要按当前档案归并，老档案 id 的
    历史成绩才不会裂成两列。
    """
    out: Dict[tuple, List[dict]] = {}
    for entry in load_entries(cfg):
        task = str(entry.get("task") or "")
        model = canonical(str(entry.get("model_raw") or entry.get("model") or ""))
        if not task or not model:
            continue
        out.setdefault((task, model), []).append(entry)
    return out


def best_of(entries: List[dict]) -> Optional[dict]:
    """一组条目里取代表那一条：分数最高；同分取轮数少、有时间、用时短、结束早的。

    「有时间优先、用时短优先」是 2026-10-02 补的口径：手工回填的旧条目没有
    用时（None），按旧的键它会永远压住后来补测出真实时长的同分条目，排行榜
    第三排序键（模型用时）就落了空。未知用时按无穷大参与比较，与
    runs.task_leaderboard 的排序键同一个口径。
    """
    def key(entry: dict):
        try:
            score = float(entry.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        work = entry.get("model_work_seconds")
        wall = entry.get("wall_seconds")
        try:
            duration = float(work if work is not None else wall)
        except (TypeError, ValueError):
            duration = None
        return (-score,
                int(entry.get("rounds") or 1),
                0 if duration is not None else 1,
                duration if duration is not None else float("inf"),
                str(entry.get("ended_at") or ""),
                str(entry.get("entry_id") or ""))

    if not entries:
        return None
    return sorted(entries, key=key)[0]
