"""参考解与半成品的改写规格：从**注入态**出发生成 ``reference/fix.patch`` 与
``reference/partial.patch``。

为什么参考解也要用"字符串替换规格"生成，而不是手写 patch：参考解必须精确地
apply 在注入态之上。手写 patch 一旦上下文对不上，锚解门禁就会以"补丁不适用"
的形式失败——而这跟题目对不对毫无关系。用规格生成可以保证：

* ``fix.patch`` 一定 apply 得上去（生成时就在注入态上验证过）；
* ``partial.patch`` 与 ``fix.patch`` 出自同一份文本，半成品 = 锚解少改几处，
  天然可比；
* 改了什么、为什么改，留在本文件里可审计，参考解不再是"神秘 diff"。

    python packs/core/tools/solution_edits.py --repo "D:\\New project 6" --task T1-01

T1-01 的锚解形态（§6.3「修复 = 抽共享点并接通 ≥2 个调用方」）：
新增 :mod:`models` 上的列口径常量与换算函数，采集派生、入库归一化、条件行级
判定、条件向量化预筛四个调用方全部接上去；涨跌停判定顺序还原为"先板块后 ST"。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from inject_edits import EDIT_SPECS, materialize
from mkpatch import build_patch

# ---------------------------------------------------------------------------
# T1-01 锚解
# ---------------------------------------------------------------------------
T1_01_FIX: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/models.py": [
        (
            """from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


class ConditionOperator(str, Enum):""",
            """from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator

# 派生列口径（全仓唯一来源）。写侧（采集派生、入库归一化）与读侧（条件的行级
# 判定与向量化预筛）必须接同一根线，否则同一行数据在几个出口会被解释成不同量纲。
#   - 换手率恒为百分数量纲：1.0 表示 1%。条件参数是分数（0.02 表示 2%），所以
#     读它的地方一律经 ``turnover_rate_to_fraction`` 归一；**不能**按数值大小
#     猜量纲——低换手区间（0.05 ~ 1.0）与分数区间高度重合，猜错方向会让判定
#     结果与用户口语正好相反。
#   - 推不出或缺列的换手率是"未知"（NaN），不是 0.0。0 是合法换手率，写成 0
#     之后无法与"当日真的没换手"区分，会同时污染条件判定与候选打分。
TURNOVER_RATE_PERCENT = 100.0
UNKNOWN_NUMERIC = float("nan")


def turnover_rate_to_fraction(value: Any) -> Any:
    \"\"\"把仓库的换手率（百分数）归一成条件参数用的分数；未知保持未知。\"\"\"
    return value / TURNOVER_RATE_PERCENT


class ConditionOperator(str, Enum):""",
        ),
    ],
    "backend/astock_backtester/data/importer.py": [
        (
            """import pandas as pd

REQUIRED_DAILY_COLUMNS""",
            """import pandas as pd

from astock_backtester.models import UNKNOWN_NUMERIC

REQUIRED_DAILY_COLUMNS""",
        ),
        (
            """        "turnover_rate": 0.0,""",
            """        "turnover_rate": UNKNOWN_NUMERIC,""",
        ),
    ],
    "backend/astock_backtester/data/astock_adapter.py": [
        (
            """from astock_backtester.data.symbols import a_share_market_symbol, is_st_name, market_code, normalize_symbol""",
            """from astock_backtester.data.symbols import a_share_market_symbol, is_st_name, market_code, normalize_symbol
from astock_backtester.models import TURNOVER_RATE_PERCENT, UNKNOWN_NUMERIC""",
        ),
        (
            """    # 真实换手率稍后由 ``_apply_turnover_rate`` 用 volume/流通股 补出来。
    frame["turnover_rate"] = float("nan")""",
            """    # turnover_rate 必须显式置空（未知）：缺列默认值同样是未知，而 0.0 是合法
    # 换手率，写成 0 无法与"当日真的没换手"区分。真实换手率稍后由
    # ``_apply_turnover_rate`` 用 volume/流通股 补出来。
    frame["turnover_rate"] = UNKNOWN_NUMERIC""",
        ),
        (
            """        A quote gives the *current* price and current float market cap, hence
        float shares; the whole window is then valued with those shares against
        each row's close.  A constant snapshot is only used when shares cannot be
        derived.

        Valuing the whole window on one share count keeps the column on a single
        source of truth and never mixes two units.
        \"\"\"""",
            """        A quote gives the *current* price and current float market cap, hence
        float shares; earlier rows then use ``float_shares * close``.  A constant
        snapshot is only used when shares cannot be derived.

        Rows that already carry a per-date value (the Baidu path derives
        ``volume / (turnover / TURNOVER_RATE_PERCENT) * close`` per row) are
        **kept**: that derivation reflects the share count *on that date*,
        whereas the quote-based estimate applies today's share count to every
        historical close and therefore overstates market cap for any stock that
        has since issued or released shares (解禁/增发).  Only gaps are filled.
        \"\"\"""",
        ),
        (
            """            bars["float_market_cap"] = derived
        return bars""",
            """            existing = pd.to_numeric(bars["float_market_cap"], errors="coerce")
            bars["float_market_cap"] = existing.fillna(derived)
        return bars""",
        ),
        (
            """        derived = (volume / shares.replace(0, float("nan")) * 100.0).where(lambda value: value >= 0)
        bars = bars.copy()
        existing = pd.to_numeric(bars["turnover_rate"], errors="coerce")
        # 入库前把未知值归一：下游条件与打分都按数值比较，留空会在向量化路径上被
        # 当成 0 处理。统一按 0 兜底，语义上等价于"当日无换手"。
        bars["turnover_rate"] = existing.fillna(derived).fillna(0.0)
        return bars""",
            """        derived = (volume / shares.replace(0, float("nan")) * TURNOVER_RATE_PERCENT).where(
            lambda value: value >= 0
        )
        bars = bars.copy()
        existing = pd.to_numeric(bars["turnover_rate"], errors="coerce")
        # 只补空缺：百度通道本身带 turnoverratio（同为百分数量纲），不要去覆盖它；
        # 也绝不把推不出的行写成 0.0——未知就是未知。
        bars["turnover_rate"] = existing.fillna(derived)
        return bars""",
        ),
    ],
    "backend/astock_backtester/conditions.py": [
        (
            """from astock_backtester.models import ConditionGroup, ConditionNode, ConditionOperator""",
            """from astock_backtester.models import (
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    turnover_rate_to_fraction,
)""",
        ),
        (
            """    \"\"\"Normalize the warehouse's ``turnover_rate`` to a fraction.

    不同来源写进来的换手率量纲并不统一：按数值大小判一次——大于 1 的按百分数除
    100，其余视为已经是分数。
    \"\"\"
    if isinstance(value, pd.Series):
        scale = value.where(value > 1.0, 100.0)
        return value / scale
    return value / 100.0 if value > 1.0 else value""",
            """    \"\"\"Normalize the warehouse's percent-scale ``turnover_rate`` to a fraction.

    ``turnover_rate`` 在仓库里**恒为百分数量纲**（由采集侧派生，量纲常量见
    :data:`astock_backtester.models.TURNOVER_RATE_PERCENT`），而条件参数是分数
    （``{"min": 0.02, "max": 0.08}`` = 换手率 2%~8%）。因此归一是**无条件**的：
    不能按 ``value > 1`` 猜量纲——真实换手 0.7% 会被当成 70%，0.05% 会被当成
    5%，低换手区间的判定与用户意图正好相反。

    行级 evaluator 与向量化 mask builder 必须共用这一处归一：二者给出相反结论
    时，engine 的 prefilter（走 MASK）会把行级判定为通过的股票全部提前丢掉，
    推荐策略的换手率条件会永远筛不出任何股票。它同时接受标量与 Series，两套
    实现因此不可能各自演化。
    \"\"\"
    return turnover_rate_to_fraction(value)""",
        ),
        (
            """def _mask_turnover_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    # 向量化路径直接对采集列取值比较，省掉一次除法。
    values = pd.to_numeric(data["turnover_rate"], errors="coerce")
    return values.between(float(node.params["min"]), float(node.params["max"]), inclusive="both")""",
            """def _mask_turnover_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    # 与行级 ``_turnover_between`` 共用同一个归一：这里若另写一份实现，两套
    # 实现会各自演化，而 prefilter 走 MASK，漂移的后果是候选被静默丢掉。
    values = _turnover_ratio(pd.to_numeric(data["turnover_rate"], errors="coerce"))
    return values.between(float(node.params["min"]), float(node.params["max"]), inclusive="both")""",
        ),
    ],
    "backend/astock_backtester/engine.py": [
        (
            """    # ST 股的涨跌幅限制统一收紧到 5%，其余按所属板块取。
    if bool(row.get("is_st", False)):
        return 0.05
    return _board_limit_pct(str(row.get("symbol", "")))""",
            """    symbol = str(row.get("symbol", ""))
    board = _board_limit_pct(symbol)
    # 先定板块再降 ST：ST 的 5% 是**在主板 10% 基础上**的限制，创业板/科创板
    # ST 股实际仍是 20%（先判 ST 会把它们按 5% 算，涨停拦截于是会误伤真实
    # 涨停之外的价格）。北交所无 ST 降幅规则，保持 30%。
    if bool(row.get("is_st", False)) and board == 0.10:
        return 0.05
    return board""",
        ),
    ],
}

# 半成品：只把行级归一改对（去掉猜量纲），向量化预筛仍然直接拿采集列比较。
# 这正是 §7 描述的陷阱——症状是"预筛把候选全丢了"，只修行级不动预筛，
# coherence 组仍然红。
T1_01_PARTIAL: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/conditions.py": [
        (
            """    \"\"\"Normalize the warehouse's ``turnover_rate`` to a fraction.

    不同来源写进来的换手率量纲并不统一：按数值大小判一次——大于 1 的按百分数除
    100，其余视为已经是分数。
    \"\"\"
    if isinstance(value, pd.Series):
        scale = value.where(value > 1.0, 100.0)
        return value / scale
    return value / 100.0 if value > 1.0 else value""",
            """    \"\"\"Normalize the warehouse's ``turnover_rate`` to a fraction.

    ``turnover_rate`` 在仓库里**恒为百分数量纲**，而条件参数是分数，所以归一是
    **无条件**的。
    \"\"\"
    return value / 100.0""",
        ),
    ],
}

# ---------------------------------------------------------------------------
# T2-04 锚解
#
# 形态：把"0 行日不是横截面证据"这条协议收回共享纯函数的调用约定上——三个模块
# 各自持有等值的样本下限（5），各自在喂给纯函数之前先滤掉 0 行日，sync 删掉就地
# 算的阈值改回委托。这不是"回滚历史提交"：注入是"就地重算阈值 + 放宽下限 +
# 取消过滤"的组合，参考解是把协议写回调用点，并补上把协议讲清楚的那段说明。
# ---------------------------------------------------------------------------
_T2_SYNC_IMPORT_NEW = """from datetime import date, datetime, timedelta
from statistics import median
from threading import Lock, Thread"""
_T2_SYNC_IMPORT_OLD = """from datetime import date, datetime, timedelta
from threading import Lock, Thread"""

_T2_SYNC_WAREHOUSE_IMPORT_NEW = """from astock_backtester.data.warehouse import Warehouse, lifecycle_bound"""
_T2_SYNC_WAREHOUSE_IMPORT_OLD = """from astock_backtester.data.warehouse import Warehouse, classify_market_days_by_cross_section, lifecycle_bound"""

_T2_SYNC_MIN_NEW = """# 停牌分类的横截面样本下限。
MIN_SUSPENSION_CLASSIFICATION_DAYS = 1"""
_T2_SYNC_MIN_OLD = """# 停牌分类的横截面样本下限：窗口交易日太少时“当日行数 ≥ 中位数×0.5”不稳，
# 退回旧行为（整窗必需、照抓不误），宁可多抓也不误判成停牌。
MIN_SUSPENSION_CLASSIFICATION_DAYS = 5"""

_T2_SYNC_BODY_NEW = """    窗口内每个交易日的落库行数 ≥ 全窗口中位数 × 0.5 → 市场正常日，它的缺行是
    停牌类（不可补）；行数异常低 → thin day（疑似写入失败，可补）。分界点就地
    算，快照路径不必绕模块级纯函数。
    \"\"\"
    if len(required_dates) < MIN_SUSPENSION_CLASSIFICATION_DAYS:
        return set()
    rows_by_date = window_frame.groupby("_td_norm").size().to_dict()
    if not rows_by_date:
        return set()
    threshold = max(1.0, float(median(rows_by_date.values())) * 0.5)
    return {day for day, count in rows_by_date.items() if count >= threshold}"""
_T2_SYNC_BODY_OLD = """    与 ``Warehouse.coverage()`` 共用 :func:`classify_market_days_by_cross_section`
    的阈值口径：当日全市场 OHLC 完整行数 ≥ 中位数 × 0.5 → 市场正常日，它的
    缺行是停牌类（不可补）；行数异常低 → thin day（疑似写入失败，可补）。
    \"\"\"
    if len(required_dates) < MIN_SUSPENSION_CLASSIFICATION_DAYS:
        return set()
    rows_by_date = window_frame.groupby("_td_norm").size().to_dict()
    if not rows_by_date:
        return set()
    return classify_market_days_by_cross_section(rows_by_date).market_normal_days"""

_T2_SYNC_DOC_NEW = """    三条例外："""
_T2_SYNC_DOC_OLD = """    三条例外与 ``Warehouse.coverage()`` 的 ``missing_rows``/``suspension_rows``
    口径一致（AGENTS §9）："""

_T2_OPS_MIN_NEW = """# 逐股缺口做停牌类剔除前，窗口至少要有这么多 A 股交易日。
MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION = 2"""
_T2_OPS_MIN_OLD = """# 逐股缺口做停牌类剔除前，窗口至少要有这么多 A 股交易日：横截面中位数在
# 三五天的窗口上抖动太大，分类结果不可信，此时退回平日历口径（旧行为）。
MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION = 5"""

_T2_OPS_BODY_NEW = """        # 交易日历铺出来的 0 行日一并参与横截面：中位数因此代表"窗口常态水位"，
        # 分界点更贴近当日全市场实际写进来的行数。
        classification = classify_market_days_by_cross_section(counts)"""
_T2_OPS_BODY_OLD = """        # 0 行日不进横截面：market_trade_date_counts 按交易日历补 0，而 coverage()
        # 与 sync 的计数都来自“有行日期”的 groupby——把 0 混进中位数会压低阈值，
        # 让三个出口对同一天给出不同分类。0 行日不在任一集合里，逐股缺口按平日历
        # 口径保留（0 行 < 任何阈值 → 可行动，与另两个出口行为一致）；全是 0 时
        # 整体退回平日历口径。
        positive_counts = {day: count for day, count in counts.items() if count > 0}
        if not positive_counts:
            return None
        classification = classify_market_days_by_cross_section(positive_counts)"""

_T2_WH_MIN_NEW = """# 资金流缺口名单做停牌类豁免前，窗口至少要有这么多 A 股交易日。
MIN_TRADE_DAYS_FOR_CAPITAL_FLOW_SUSPENSION_EXEMPTION = 1"""
_T2_WH_MIN_OLD = """# 资金流缺口名单做停牌类豁免前，窗口至少要有这么多 A 股交易日：横截面中位数
# 在短窗口上抖动太大，样本不足时退回平日历口径（旧行为）。与
# data/operations.py::MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION、
# data/sync.py::MIN_SUSPENSION_CLASSIFICATION_DAYS 同为 5（各自模块持有，
# warehouse 不反向依赖它们）。
MIN_TRADE_DAYS_FOR_CAPITAL_FLOW_SUSPENSION_EXEMPTION = 5"""

_T2_WH_COVER_DOC_NEW = """        分界点（当日行数 ≥ 中位数 × thin_day_ratio 的市场正常日）在窗口日历上
        就地算：thin day（疑似写入失败）的缺行计入 missing_rows（可行动）；
        市场正常日的缺行是停牌类（公开渠道天然没有，不可补），计入
        suspension_rows。

        返回 (missing_rows, suspension_rows)。"""
_T2_WH_COVER_DOC_OLD = """        分类委托给模块级纯函数 :func:`classify_market_days_by_cross_section`
        （threshold = 当日行数 ≥ 中位数 × thin_day_ratio 的市场正常日）：thin
        day（疑似写入失败）的缺行计入 missing_rows（可行动）；市场正常日的
        缺行是停牌类（公开渠道天然没有，不可补），计入 suspension_rows。
        输入 ``rows_per_date`` 没有覆盖的交易日（0 行）不属于任何市场正常日
        → 按 thin day 计入 missing_rows。

        返回 (missing_rows, suspension_rows)。"""

_T2_WH_COVER_BODY_NEW = """        day_counts = {day: rows_per_date.get(day, 0) for day in calendar}
        threshold = max(1.0, float(median(day_counts.values())) * thin_day_ratio)
        market_normal_days = {day for day, count in day_counts.items() if count >= threshold}
        missing_total = 0
        suspension_total = 0
        for day in calendar:
            day_value = day.value
            spanning = bisect.bisect_right(starts, day_value) - bisect.bisect_left(ends, day_value)
            internal = spanning - rows_per_date.get(day, 0)
            if internal <= 0:
                continue
            if day in market_normal_days:
                suspension_total += internal
            else:
                missing_total += internal
        return missing_total, suspension_total"""
_T2_WH_COVER_BODY_OLD = """        classification = classify_market_days_by_cross_section(rows_per_date, thin_day_ratio=thin_day_ratio)
        missing_total = 0
        suspension_total = 0
        for day in calendar:
            day_value = day.value
            spanning = bisect.bisect_right(starts, day_value) - bisect.bisect_left(ends, day_value)
            internal = spanning - rows_per_date.get(day, 0)
            if internal <= 0:
                continue
            if day in classification.market_normal_days:
                suspension_total += internal
            else:
                missing_total += internal
        return missing_total, suspension_total"""

_T2_WH_FLOW_DOC_NEW = """        与 ``data/sync.py`` 的窗口快照、``data/operations.py`` 的逐股覆盖共用
        :func:`classify_market_days_by_cross_section` 的阈值口径。"""
_T2_WH_FLOW_DOC_OLD = """        与 ``data/sync.py`` 的窗口快照、``data/operations.py`` 的逐股覆盖共用
        :func:`classify_market_days_by_cross_section` 的阈值口径，且只喂**有行**
        的交易日：``market_trade_date_counts`` 按交易日历补的 0 行日不是横截面
        证据（通常是节假日表覆盖问题或全市场未写入），混进中位数会压低阈值，
        让各出口对同一天给出不同分类——``coverage()`` / sync 的计数都来自有行
        日期的 groupby，这里必须对齐。"""

_T2_WH_FLOW_BODY_NEW = """        return classify_market_days_by_cross_section(counts).market_normal_days"""
_T2_WH_FLOW_BODY_OLD = """        positive_counts = {day: count for day, count in counts.items() if count > 0}
        if not positive_counts:
            return set()
        return classify_market_days_by_cross_section(positive_counts).market_normal_days"""

_T2_WH_COUNTS_DOC_NEW = """        供按横截面阈值判定"市场正常日 / thin day"时充当输入。"""
_T2_WH_COUNTS_DOC_OLD = """        供复用 :func:`classify_market_days_by_cross_section` 横截面阈值时充当输入
        （``data/sync.py`` / ``data/operations.py`` 与本模块的资金流缺口名单）；
        0 值键按交易日历补出、不是横截面证据，喂给分类器前应由调用方过滤。"""

_T2_WH_PURE_DOC_NEW = """    纯函数、不读仓。键会被归一化为 ``pd.Timestamp``（同日多键行数累加）；
    返回的两个集合只覆盖输入里出现的日期——输入里没有行的交易日不在任一集合
    中。空输入返回两个空集合。"""
_T2_WH_PURE_DOC_OLD = """    纯函数、不读仓：``Warehouse.coverage()``（内部洞分类）与后续
    ``data/sync.py`` / ``data/operations.py`` 共用同一阈值口径。键会被归一化为
    ``pd.Timestamp``（同日多键行数累加）；返回的两个集合只覆盖输入里出现的
    日期——输入里没有行的交易日不在任一集合中，调用方按"0 行 < 任何阈值"
    自行归入 thin。0 值键传进来也会被分进 thin，但各调用方约定先把 0 行日
    过滤掉再分类：0 行日不是横截面证据，混入中位数会压低阈值，让三个出口对
    同一天给出不同分类（``market_trade_date_counts`` 按交易日历补 0，其调用方
    见 operations 的 ``_market_day_classification`` 与 warehouse 的
    ``_capital_flow_market_normal_days``）。空输入返回两个空集合。"""

T2_04_FIX: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/data/operations.py": [
        (_T2_OPS_MIN_NEW, _T2_OPS_MIN_OLD),
        (_T2_OPS_BODY_NEW, _T2_OPS_BODY_OLD),
    ],
    "backend/astock_backtester/data/sync.py": [
        (_T2_SYNC_IMPORT_NEW, _T2_SYNC_IMPORT_OLD),
        (_T2_SYNC_WAREHOUSE_IMPORT_NEW, _T2_SYNC_WAREHOUSE_IMPORT_OLD),
        (_T2_SYNC_MIN_NEW, _T2_SYNC_MIN_OLD),
        (_T2_SYNC_BODY_NEW, _T2_SYNC_BODY_OLD),
        (_T2_SYNC_DOC_NEW, _T2_SYNC_DOC_OLD),
    ],
    "backend/astock_backtester/data/warehouse.py": [
        (_T2_WH_MIN_NEW, _T2_WH_MIN_OLD),
        (_T2_WH_COVER_DOC_NEW, _T2_WH_COVER_DOC_OLD),
        (_T2_WH_COVER_BODY_NEW, _T2_WH_COVER_BODY_OLD),
        (_T2_WH_FLOW_DOC_NEW, _T2_WH_FLOW_DOC_OLD),
        (_T2_WH_FLOW_BODY_NEW, _T2_WH_FLOW_BODY_OLD),
        (_T2_WH_COUNTS_DOC_NEW, _T2_WH_COUNTS_DOC_OLD),
        (_T2_WH_PURE_DOC_NEW, _T2_WH_PURE_DOC_OLD),
    ],
}

# 半成品：只把"覆盖汇总"这一个出口修好（委托回共享纯函数 + 下限回到 5），
# 另外三个出口原样不动。实测 100 * 1 / 7 = 14.29 分，且 coherence 组仍然红
# ——这正是"只修一个端口必然 <100"的证明。
T2_04_PARTIAL: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/data/warehouse.py": [
        (_T2_WH_MIN_NEW, _T2_WH_MIN_OLD),
        (_T2_WH_COVER_DOC_NEW, _T2_WH_COVER_DOC_OLD),
        (_T2_WH_COVER_BODY_NEW, _T2_WH_COVER_BODY_OLD),
    ],
}

# ---------------------------------------------------------------------------
# T3-10 锚解
#
# 注入是"每个写入方自己维护分区会话"这一条协议级改写，锚解就是把写入权交回
# 唯一的写入入口，并把随之退化的三处出口一起接回：
#   1. 补数脚本不再自持分区快照，整批行一次性交给仓库的写入入口——由仓库在
#      分区锁内做 read-modify-write，跨进程的丢失窗口随之消失，落盘量回到
#      O(批数) 而不是 O(批数 × 分区)；默认批大小回到"攒批"语义。
#   2. 写入后统计口径立刻失效（股票池计数 + 每交易日行数，不靠 TTL 兜底）。
#   3. 健康检查重新参与覆盖刷新，写入侧状况对 /health 可见。
#   4. 跨进程写锁超时恢复有界重试：锁忙不再等于整批失败。
#
# fix = 注入规格的逆（逐条倒序还原），因此一定精确 apply 在注入态之上；
# partial = 只还原第 1 条（脚本侧），其余三处仍然退化——这正是"单进程测试
# 全绿、进程级链路仍坏"的陷阱，实测 100 × 3/8 = 37.5 分。
# ---------------------------------------------------------------------------
T3_10_FIX: dict[str, list[tuple[str, str]]] = {
    rel_path: [(injected, original) for original, injected in reversed(edits)]
    for rel_path, edits in EDIT_SPECS["T3-10"].items()
}

T3_10_PARTIAL: dict[str, list[tuple[str, str]]] = {
    "scripts/run-full-market-import.py": T3_10_FIX["scripts/run-full-market-import.py"],
}

# ---------------------------------------------------------------------------
# T4-12 锚解
#
# 注入态把四条自动通道的"状态口径"各劈成两套，锚解就是逐条还原（与注入规格互逆，
# 逐条倒序，因此一定精确 apply 在注入态之上）：
#   A. 会话回收：忙碌判定恢复"引用计数 + 锁"双重口径；时间戳不可读只参与条数回收。
#   B. 快讯节流：上限滑动窗口回到 1 小时；去重键恢复档位划分。
#   C. 简报：只有真的产出简报才推进记账；新增判定与入库去重共用去空白标题口径；
#      每次运行最多推送 2 条快讯；新闻来源恢复不可信围栏。
#   D. 寻优：整数参数的小数候选交给组合评估判废；排名只看有成交的组合；指标增强
#      整个网格共用一份；过拟合小样本只数可比较组合；critical 下限回到 5 笔；
#      结果事件带上废组合名单；网格超限返回专用错误码。
#   E. 前端：阶段事件不算终态；最优高亮跟随服务端声明；拒绝名单按服务端口径展示。
#
# partial = 只还原 C（简报链一个端口），A/B/D/E 仍然失真——"只修一个端口必然 <100"
# 的证明：简报两组绿、联合 coherence 组仍因寻优侧红（预期约 4/23 ≈ 17.4 分）。
# ---------------------------------------------------------------------------
T4_12_FIX: dict[str, list[tuple[str, str]]] = {
    rel_path: [(injected, original) for original, injected in reversed(edits)]
    for rel_path, edits in EDIT_SPECS["T4-12"].items()
}

T4_12_PARTIAL: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/ai/digest.py": T4_12_FIX["backend/astock_backtester/ai/digest.py"],
}

SOLUTIONS: dict[str, dict[str, dict[str, list[tuple[str, str]]]]] = {
    "T1-01": {"fix": T1_01_FIX, "partial": T1_01_PARTIAL},
    "T2-04": {"fix": T2_04_FIX, "partial": T2_04_PARTIAL},
    "T3-10": {"fix": T3_10_FIX, "partial": T3_10_PARTIAL},
    "T4-12": {"fix": T4_12_FIX, "partial": T4_12_PARTIAL},
}


def _apply_spec(text_by_path: dict[str, str], spec: dict[str, list[tuple[str, str]]]) -> None:
    for rel_path, edits in spec.items():
        if rel_path not in text_by_path:
            raise SystemExit(f"[参考解失败] 注入态里没有 {rel_path}")
        text = text_by_path[rel_path]
        for index, (old, new) in enumerate(edits, start=1):
            count = text.count(old)
            if count != 1:
                head = old.strip().splitlines()[0][:80] if old.strip() else "(空锚点)"
                raise SystemExit(f"[参考解失败] {rel_path} 第 {index} 条锚点命中 {count} 次。\n  锚点首行：{head}")
            text = text.replace(old, new, 1)
        text_by_path[rel_path] = text


def _solution_paths(task: str) -> list[str]:
    paths: list[str] = []
    for spec in SOLUTIONS[task].values():
        for rel_path in spec:
            if rel_path not in paths:
                paths.append(rel_path)
    return paths


def build_solution_patches(repo: Path, task: str, work_root: Path) -> dict[str, str]:
    """返回 ``{"fix": patch, "partial": patch}``，两者都相对注入态。"""
    injected_dir = work_root / "injected"
    built = materialize(repo, EDIT_SPECS[task], injected_dir)
    injected_text = {
        rel_path: (injected_dir / rel_path).read_text(encoding="utf-8") for rel_path, _patch, _t in built
    }
    # 参考解要改、但注入态与原始态一致的文件（本次是 models.py），diff 的基线
    # 取原始文本。补丁因此描述的是"注入态 → 修好态"的净效果，可以直接叠加在
    # inject/patches/*.patch 之后。
    for rel_path in _solution_paths(task):
        if rel_path not in injected_text:
            original = repo / rel_path
            if not original.is_file():
                raise SystemExit(f"[参考解失败] 仓库里找不到 {rel_path}")
            injected_text[rel_path] = original.read_text(encoding="utf-8")

    out: dict[str, str] = {}
    for kind in ("fix", "partial"):
        text_by_path = dict(injected_text)
        _apply_spec(text_by_path, SOLUTIONS[task][kind])
        chunks: list[str] = []
        for rel_path in _solution_paths(task):
            if text_by_path[rel_path] == injected_text[rel_path]:
                continue
            patch = build_patch(
                rel_path,
                injected_text[rel_path].splitlines(keepends=True),
                text_by_path[rel_path].splitlines(keepends=True),
            )
            if patch:
                chunks.append(patch)
        if not chunks:
            raise SystemExit(f"[参考解失败] {task}/{kind} 没有产生任何改动")
        out[kind] = "".join(chunks)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成参考解与半成品补丁")
    parser.add_argument("--repo", required=True, help="受测仓库根（只读）")
    parser.add_argument("--task", required=True, choices=sorted(SOLUTIONS))
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)

    tools_dir = Path(__file__).resolve().parent
    work_root = Path(args.work_dir) if args.work_dir else tools_dir / "_work" / args.task
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    patches = build_solution_patches(Path(args.repo), args.task, work_root)
    out_dir = tools_dir.parent / "tasks" / args.task / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    for kind, text in patches.items():
        (out_dir / f"{kind}.patch").write_text(text, encoding="utf-8", newline="\n")
        files = sum(1 for line in text.splitlines() if line.startswith("diff --git "))
        changed = sum(1 for line in text.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---"))
        print(f"[{args.task}] {kind}.patch  覆盖 {files} 个文件，{changed} 行增删")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
