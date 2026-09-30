"""注入改写规格：把任务包的"被注入版"源码确定性地造出来，再交给 mkpatch 生成 patch。

为什么要有这一层：设计文档 §8 规定注入以 ``inject/patches/*.patch`` 落地（标准
unified diff，``git apply`` 可用）。但补丁本身是手写的，肉眼无法判断"这个改动
是不是真的落在原始文件上、锚点有没有写错"。本模块把注入表达成**精确字符串替换
规格**：``(相对路径, 旧文本, 新文本)``。跑一次就能得到可复现、可审计、可 diff 的
补丁；任一条锚点命中次数不为 1 就报错退出，不会产出半吊子的 patch。

设计约束（§4.2 第 3 层、§6.5 第 1 条）：

* 注入是**合成改写**，与历史修复 diff 相似度 < 0.6 —— 所以这里写的是"把共享口径
  就地内联 / 去掉兜底 / 调换判定顺序"这类重构型回归，而不是回滚历史提交；
* 注入点所在处的注释与 docstring 一并改写，把"这里曾经踩过什么坑、正确的口径是
  什么"的文字抹掉。否则模型 grep 一句注释就拿到标准答案，§6.5 第 1 条直接不成立；
* 改写只动"行为 + 就近说明"，不动函数签名、不动公开 API，因此参考解可以原样还原，
  且不会引入新的未使用符号（未使用 import / 变量本身就是答案路标）。

用法：

    python packs/core/tools/inject_edits.py --repo "D:\\New project 6" --task T1-01

会在 ``packs/core/tools/_work/<task>/`` 下生成注入版文件树，并把 patch 写入任务包的
``inject/patches/``。可重复生成，不留半成品。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from mkpatch import build_patch

# ---------------------------------------------------------------------------
# T1-01 · 三处派生口径漂移
#
# 注入态的"世界模型"：换手率的量纲与"未知取值"由三个地方各自决定——采集派生处把
# 推不出的行写成 0.0，条件注册表的行级实现按数值大小猜量纲、向量化实现干脆不归一；
# 流通市值改用"今天的股本 × 历史收盘"整窗覆盖；涨跌停先判 ST 再判板块。四组症状
# 互相独立，只有把口径抽到一处、让各调用方接同一根线，才能同时消掉。
# ---------------------------------------------------------------------------
T1_01: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/data/astock_adapter.py": [
        (
            # 抹掉"缺列默认值 0.0 会把未知写成 0%"的自述——它同时点破了采集侧与
            # 归一化侧两处的口径差异。
            """    # turnover_rate 同理必须显式置空：``normalize_daily_bars`` 的缺列默认值是
    # ``0.0``，不置空就会把"未知"写成"换手率 0%"——假 0 会同时污染
    # ``turnover_between`` 条件与候选打分（0 是合法值，无法与真实 0% 区分）。
    # 真实换手率稍后由 ``_derive_turnover_rate`` 用 volume/流通股 补出来。""",
            """    # 真实换手率稍后由 ``_apply_turnover_rate`` 用 volume/流通股 补出来。""",
        ),
        (
            # 抹掉"绝不写 0"的自述。
            """        公开 XHR 日 K 不带换手率，而 ``turnover_rate`` 参与
        ``turnover_between`` 条件与候选打分。用报价推出的流通股
        （``float_market_cap / price``）即可逐行还原，量纲与仓库一致是**百分数**
        （实测仓库 median≈0.38）。推不出流通股的行保持 NaN（"未知"），
        绝不写 0。
        \"\"\"""",
            """        公开 XHR 日 K 不带换手率，而 ``turnover_rate`` 参与
        ``turnover_between`` 条件与候选打分。用报价推出的流通股
        （``float_market_cap / price``）即可逐行还原，量纲与仓库一致是**百分数**
        （实测仓库 median≈0.38）。
        \"\"\"""",
        ),
        (
            # 静默注入：推不出流通股的行写假 0。0 是合法换手率，无法与真实 0% 区分。
            """        existing = pd.to_numeric(bars["turnover_rate"], errors="coerce")
        # 只补空缺：百度通道本身带 turnoverratio（同为百分数量纲），不要去覆盖它。
        bars["turnover_rate"] = existing.fillna(derived)
        return bars""",
            """        existing = pd.to_numeric(bars["turnover_rate"], errors="coerce")
        # 入库前把未知值归一：下游条件与打分都按数值比较，留空会在向量化路径上被
        # 当成 0 处理。统一按 0 兜底，语义上等价于"当日无换手"。
        bars["turnover_rate"] = existing.fillna(derived).fillna(0.0)
        return bars""",
        ),
        (
            # 抹掉"逐日值必须原样保留"的自述。
            """        A quote gives the *current* price and current float market cap, hence
        float shares; earlier rows then use ``float_shares * close``.  A constant
        snapshot is only used when shares cannot be derived.

        Rows that already carry a per-date value (the Baidu path derives
        ``volume / (turnover / 100) * close`` per row) are **kept**: that
        derivation reflects the share count *on that date*, whereas the
        quote-based estimate applies today's share count to every historical
        close and therefore overstates market cap for any stock that has since
        issued or released shares (解禁/增发).  Only gaps are filled.
        \"\"\"""",
            """        A quote gives the *current* price and current float market cap, hence
        float shares; the whole window is then valued with those shares against
        each row's close.  A constant snapshot is only used when shares cannot be
        derived.

        Valuing the whole window on one share count keeps the column on a single
        source of truth and never mixes two units.
        \"\"\"""",
        ),
        (
            # 静默注入：用今日股本覆盖历史逐日值（老日期的市值被放大）。
            """            existing = pd.to_numeric(bars["float_market_cap"], errors="coerce")
            bars["float_market_cap"] = existing.fillna(derived)
        return bars""",
            """            bars["float_market_cap"] = derived
        return bars""",
        ),
    ],
    "backend/astock_backtester/conditions.py": [
        (
            # 行级实现改成"按数值大小猜量纲"，并抹掉点破量纲的口径说明。
            """    \"\"\"Normalize the warehouse's percent-scale ``turnover_rate`` to a fraction.

    ``turnover_rate`` 在仓库里**恒为百分数量纲**（§9：由 ``_apply_turnover_rate``
    用 ``volume / 流通股 × 100`` 补，实测 229 万行：中位数 0.38、最大 98.28），
    而条件参数是分数（``{"min": 0.02, "max": 0.08}`` = 换手率 2%~8%）。因此归一
    是**无条件**的：不能按 ``value > 1`` 猜量纲 —— 真实换手 0.7% 会被当成 70%，
    0.05% 会被当成 5%，低换手区间的判定与用户意图正好相反。

    行级 evaluator 与向量化 mask builder 必须共用这一处归一（§15-5）：二者给出
    相反结论时，engine 的 prefilter（走 MASK）会把行级判定为通过的股票全部提前
    丢掉，推荐策略的换手率条件会永远筛不出任何股票。它同时接受标量与 Series，
    两套实现因此不可能各自演化。
    \"\"\"
    return value / 100.0""",
            """    \"\"\"Normalize the warehouse's ``turnover_rate`` to a fraction.

    不同来源写进来的换手率量纲并不统一：按数值大小判一次——大于 1 的按百分数除
    100，其余视为已经是分数。
    \"\"\"
    if isinstance(value, pd.Series):
        scale = value.where(value > 1.0, 100.0)
        return value / scale
    return value / 100.0 if value > 1.0 else value""",
        ),
        (
            # 向量化实现不再共用归一，直接拿采集列比较（与行级结论相反）。
            """def _mask_turnover_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    # 与行级 ``_turnover_between`` 共用同一个 ``_turnover_ratio``（§15-5）：这里
    # 若另写一份 ``where(values <= 1, values / 100)``，两套实现会各自演化，而
    # prefilter 走 MASK，漂移的后果是候选被静默丢掉。
    values = _turnover_ratio(pd.to_numeric(data["turnover_rate"], errors="coerce"))
    return values.between(float(node.params["min"]), float(node.params["max"]), inclusive="both")""",
            """def _mask_turnover_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    # 向量化路径直接对采集列取值比较，省掉一次除法。
    values = pd.to_numeric(data["turnover_rate"], errors="coerce")
    return values.between(float(node.params["min"]), float(node.params["max"]), inclusive="both")""",
        ),
    ],
    "backend/astock_backtester/engine.py": [
        (
            # 先判 ST 再判板块，并抹掉说明判定顺序的注释。
            """    symbol = str(row.get("symbol", ""))
    board = _board_limit_pct(symbol)
    # 先定板块再降 ST：ST 的 5% 是**在主板 10% 基础上**的限制，创业板/科创板
    # ST 股实际仍是 20%（先判 ST 会把它们按 5% 算，limit_up_blocks_buy 于是
    # 错误拦截真实涨停之外的价格）。北交所无 ST 降幅规则，保持 30%。
    if bool(row.get("is_st", False)) and board == 0.10:
        return 0.05
    return board""",
            """    # ST 股的涨跌幅限制统一收紧到 5%，其余按所属板块取。
    if bool(row.get("is_st", False)):
        return 0.05
    return _board_limit_pct(str(row.get("symbol", "")))""",
        ),
    ],
    # 第三处口径（只进参考解、不进注入）：列缺省值。采集侧经手的数据带
    # turnover_rate 列、未知值是空；但走"按列缺省"的入口（CSV / 外部帧）时
    # importer 填 0.0，与采集侧口径相反。参考解要把它接到同一个共享口径上，
    # 隐藏测试对此有独立断言，所以它在注入态就已经是红的。
}

# ---------------------------------------------------------------------------
# T2-04 · 缺口四出口口径漂移
#
# 同一天的"市场正常日 / thin day"分类本应由一个共享纯函数决定，但四个出口各自
# 喂不同的输入、各自设不同的样本下限：sync 喂"窗口内总行数"并自算阈值、operations
# 不滤 0 行日、warehouse 的资金流豁免不滤 0 行日且样本下限压到 1 天、coverage 的
# 内部洞改用"按日历铺零"的口径自己算分界点。任一处单修都不能让四出口对齐。
# ---------------------------------------------------------------------------
T2_04: dict[str, list[tuple[str, str]]] = {
    "backend/astock_backtester/data/operations.py": [
        (
            """# 逐股缺口做停牌类剔除前，窗口至少要有这么多 A 股交易日：横截面中位数在
# 三五天的窗口上抖动太大，分类结果不可信，此时退回平日历口径（旧行为）。
MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION = 5""",
            """# 逐股缺口做停牌类剔除前，窗口至少要有这么多 A 股交易日。
MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION = 2""",
        ),
        (
            # 0 行日混入横截面中位数：阈值被压低，分类结果与另三个出口分叉。
            """        # 0 行日不进横截面：market_trade_date_counts 按交易日历补 0，而 coverage()
        # 与 sync 的计数都来自“有行日期”的 groupby——把 0 混进中位数会压低阈值，
        # 让三个出口对同一天给出不同分类。0 行日不在任一集合里，逐股缺口按平日历
        # 口径保留（0 行 < 任何阈值 → 可行动，与另两个出口行为一致）；全是 0 时
        # 整体退回平日历口径。
        positive_counts = {day: count for day, count in counts.items() if count > 0}
        if not positive_counts:
            return None
        classification = classify_market_days_by_cross_section(positive_counts)""",
            """        # 交易日历铺出来的 0 行日一并参与横截面：中位数因此代表"窗口常态水位"，
        # 分界点更贴近当日全市场实际写进来的行数。
        classification = classify_market_days_by_cross_section(counts)""",
        ),
    ],
    "backend/astock_backtester/data/sync.py": [
        (
            """from datetime import date, datetime, timedelta
from threading import Lock, Thread""",
            """from datetime import date, datetime, timedelta
from statistics import median
from threading import Lock, Thread""",
        ),
        (
            """from astock_backtester.data.warehouse import Warehouse, classify_market_days_by_cross_section, lifecycle_bound""",
            """from astock_backtester.data.warehouse import Warehouse, lifecycle_bound""",
        ),
        (
            """# 停牌分类的横截面样本下限：窗口交易日太少时“当日行数 ≥ 中位数×0.5”不稳，
# 退回旧行为（整窗必需、照抓不误），宁可多抓也不误判成停牌。
MIN_SUSPENSION_CLASSIFICATION_DAYS = 5""",
            """# 停牌分类的横截面样本下限。
MIN_SUSPENSION_CLASSIFICATION_DAYS = 1""",
        ),
        (
            # 快照自算阈值，且统计口径换成"落库总行数"（含 OHLC 不完整的行）。
            """    与 ``Warehouse.coverage()`` 共用 :func:`classify_market_days_by_cross_section`
    的阈值口径：当日全市场 OHLC 完整行数 ≥ 中位数 × 0.5 → 市场正常日，它的
    缺行是停牌类（不可补）；行数异常低 → thin day（疑似写入失败，可补）。
    \"\"\"
    if len(required_dates) < MIN_SUSPENSION_CLASSIFICATION_DAYS:
        return set()
    rows_by_date = window_frame.groupby("_td_norm").size().to_dict()
    if not rows_by_date:
        return set()
    return classify_market_days_by_cross_section(rows_by_date).market_normal_days""",
            """    窗口内每个交易日的落库行数 ≥ 全窗口中位数 × 0.5 → 市场正常日，它的缺行是
    停牌类（不可补）；行数异常低 → thin day（疑似写入失败，可补）。分界点就地
    算，快照路径不必绕模块级纯函数。
    \"\"\"
    if len(required_dates) < MIN_SUSPENSION_CLASSIFICATION_DAYS:
        return set()
    rows_by_date = window_frame.groupby("_td_norm").size().to_dict()
    if not rows_by_date:
        return set()
    threshold = max(1.0, float(median(rows_by_date.values())) * 0.5)
    return {day for day, count in rows_by_date.items() if count >= threshold}""",
        ),
        (
            """    三条例外与 ``Warehouse.coverage()`` 的 ``missing_rows``/``suspension_rows``
    口径一致（AGENTS §9）：
    - 落在 thin day 的缺行仍必需（可行动缺口，照抓）；""",
            """    三条例外：
    - 落在 thin day 的缺行仍必需（可行动缺口，照抓）；""",
        ),
    ],
    "backend/astock_backtester/data/warehouse.py": [
        (
            """# 资金流缺口名单做停牌类豁免前，窗口至少要有这么多 A 股交易日：横截面中位数
# 在短窗口上抖动太大，样本不足时退回平日历口径（旧行为）。与
# data/operations.py::MIN_TRADE_DAYS_FOR_SUSPENSION_CLASSIFICATION、
# data/sync.py::MIN_SUSPENSION_CLASSIFICATION_DAYS 同为 5（各自模块持有，
# warehouse 不反向依赖它们）。
MIN_TRADE_DAYS_FOR_CAPITAL_FLOW_SUSPENSION_EXEMPTION = 5""",
            """# 资金流缺口名单做停牌类豁免前，窗口至少要有这么多 A 股交易日。
MIN_TRADE_DAYS_FOR_CAPITAL_FLOW_SUSPENSION_EXEMPTION = 1""",
        ),
        (
            # coverage 的内部洞分界点改成"按日历铺零"就地算：0 行日混入中位数。
            """        分类委托给模块级纯函数 :func:`classify_market_days_by_cross_section`
        （threshold = 当日行数 ≥ 中位数 × thin_day_ratio 的市场正常日）：thin
        day（疑似写入失败）的缺行计入 missing_rows（可行动）；市场正常日的
        缺行是停牌类（公开渠道天然没有，不可补），计入 suspension_rows。
        输入 ``rows_per_date`` 没有覆盖的交易日（0 行）不属于任何市场正常日
        → 按 thin day 计入 missing_rows。""",
            """        分界点（当日行数 ≥ 中位数 × thin_day_ratio 的市场正常日）在窗口日历上
        就地算：thin day（疑似写入失败）的缺行计入 missing_rows（可行动）；
        市场正常日的缺行是停牌类（公开渠道天然没有，不可补），计入
        suspension_rows。""",
        ),
        (
            """        classification = classify_market_days_by_cross_section(rows_per_date, thin_day_ratio=thin_day_ratio)
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
        return missing_total, suspension_total""",
            """        day_counts = {day: rows_per_date.get(day, 0) for day in calendar}
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
        return missing_total, suspension_total""",
        ),
        (
            # 资金流豁免：0 行日混入分类 → 大量交易日被当成市场正常日 → 豁免过宽。
            """        与 ``data/sync.py`` 的窗口快照、``data/operations.py`` 的逐股覆盖共用
        :func:`classify_market_days_by_cross_section` 的阈值口径，且只喂**有行**
        的交易日：``market_trade_date_counts`` 按交易日历补的 0 行日不是横截面
        证据（通常是节假日表覆盖问题或全市场未写入），混进中位数会压低阈值，
        让各出口对同一天给出不同分类——``coverage()`` / sync 的计数都来自有行
        日期的 groupby，这里必须对齐。
        \"\"\"""",
            """        与 ``data/sync.py`` 的窗口快照、``data/operations.py`` 的逐股覆盖共用
        :func:`classify_market_days_by_cross_section` 的阈值口径。
        \"\"\"""",
        ),
        (
            """        positive_counts = {day: count for day, count in counts.items() if count > 0}
        if not positive_counts:
            return set()
        return classify_market_days_by_cross_section(positive_counts).market_normal_days""",
            """        return classify_market_days_by_cross_section(counts).market_normal_days""",
        ),
        (
            # 抹掉"喂分类器前先滤 0 行日"的点名说明。
            """        供复用 :func:`classify_market_days_by_cross_section` 横截面阈值时充当输入
        （``data/sync.py`` / ``data/operations.py`` 与本模块的资金流缺口名单）；
        0 值键按交易日历补出、不是横截面证据，喂给分类器前应由调用方过滤。""",
            """        供按横截面阈值判定"市场正常日 / thin day"时充当输入。""",
        ),
        (
            # 抹掉共享纯函数 docstring 里"各出口因此会对同一天给出不同分类"的答案。
            """    纯函数、不读仓：``Warehouse.coverage()``（内部洞分类）与后续
    ``data/sync.py`` / ``data/operations.py`` 共用同一阈值口径。键会被归一化为
    ``pd.Timestamp``（同日多键行数累加）；返回的两个集合只覆盖输入里出现的
    日期——输入里没有行的交易日不在任一集合中，调用方按"0 行 < 任何阈值"
    自行归入 thin。0 值键传进来也会被分进 thin，但各调用方约定先把 0 行日
    过滤掉再分类：0 行日不是横截面证据，混入中位数会压低阈值，让三个出口对
    同一天给出不同分类（``market_trade_date_counts`` 按交易日历补 0，其调用方
    见 operations 的 ``_market_day_classification`` 与 warehouse 的
    ``_capital_flow_market_normal_days``）。空输入返回两个空集合。""",
            """    纯函数、不读仓。键会被归一化为 ``pd.Timestamp``（同日多键行数累加）；
    返回的两个集合只覆盖输入里出现的日期——输入里没有行的交易日不在任一集合
    中。空输入返回两个空集合。""",
        ),
    ],
}

# ---------------------------------------------------------------------------
# T3-10 · 跨进程写仓 + 攒批 + 服务健康（脚本 → 仓库 → 服务 全链）
#
# 注入态的"世界模型"：外部补数脚本不再把整批行交给仓库的写入入口，而是自己
# 维护一份"分区会话"——首次落盘时把分区整段读进内存，之后每批只跟内存里的
# 会话合并、再把会话整段覆盖回分区。由此同时长出三个症状：
#   ① 攒批退化（每票各自过一次会话 → 落盘次数 = 股票只数）；
#   ② 读改写放大（每次落盘都重写整段分区，累计写入量随批数平方增长）；
#   ③ 跨进程丢失（会话快照只取一次，另一进程期间写入的行会被整体覆盖掉），
#      并且这条路径绕开了仓库的写入入口，统计缓存失效与数据集登记也一并丢失。
# 另外两处配合：统计缓存的写入失效被收窄（写入后口径陈旧），健康检查不再
# 触发覆盖刷新（写入侧状况对外不可见），跨进程写锁超时后的有界重试被去掉
# （锁一忙就整批失败）。
# 诱饵：`--write-batch-size` 旋钮（调大只把丢失窗口拉长，不治放大）；
#       `backfill-market-cap.py` 的整行回写（那本就是正确的分区级协议，不许动）。
# ---------------------------------------------------------------------------
T3_10: dict[str, list[tuple[str, str]]] = {
    "scripts/run-full-market-import.py": [
        (
            # ① 攒批退化：默认批大小降到 1（诱饵旋钮本身保留，但不再有任何攒批收益）。
            '''    parser.add_argument("--write-batch-size", type=int, default=25)''',
            '''    parser.add_argument("--write-batch-size", type=int, default=1)''',
        ),
        (
            # ②③ 分区会话：绕开仓库写入入口，自带 read-modify-write 与进程内快照。
            '''def fetch_daily_bars(symbol: str, start_date: str, end_date: str, adata_provider: ADataProvider) -> tuple[pd.DataFrame, str]:''',
            '''_PARTITION_SESSIONS: dict[str, pd.DataFrame] = {}


def _persist_batch(warehouse: Warehouse, frames: list[pd.DataFrame]) -> None:
    """把一批行并进分区会话，再把会话整段落盘。

    分区一旦开过会话就以会话为准：后续批次只跟内存里那份合并，不再回读磁盘。
    回读整段分区是这条路上最贵的一步（分区越大越贵），而写入方是按批次顺序
    推进的，磁盘上此刻的内容与自己上一批写下的那份本来就该一致。
    """
    frame = pd.concat(frames, ignore_index=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date"])
    if frame.empty:
        return
    for year, year_frame in frame.groupby(frame["trade_date"].dt.year):
        path = warehouse._partition_path(int(year))
        key = str(path)
        session = _PARTITION_SESSIONS.get(key)
        if session is None:
            session = warehouse._safe_read_parquet(path) if path.exists() else pd.DataFrame()
        merged = pd.concat([session, year_frame], ignore_index=True)
        if {"symbol", "trade_date"}.issubset(merged.columns):
            merged = merged.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
        merged = merged.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        _PARTITION_SESSIONS[key] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        warehouse._atomic_write_parquet(merged, path)


def fetch_daily_bars(symbol: str, start_date: str, end_date: str, adata_provider: ADataProvider) -> tuple[pd.DataFrame, str]:''',
        ),
        (
            # ① flush 从"整批 concat 一次落盘"变成"逐票各自过一次会话"。
            '''    def flush_batch() -> None:
        nonlocal imported_rows
        if not batch:
            return
        warehouse.write_daily_bars(pd.concat([item[1] for item in batch], ignore_index=True))
        for symbol, _frame, source, seconds, rows, index in batch:''',
            '''    def flush_batch() -> None:
        nonlocal imported_rows
        if not batch:
            return
        # 每只票各自过一次分区会话：批越小，中途失败时丢的行越少，进度事件也
        # 更贴近实际落盘节奏；批边界仍由上面的 --write-batch-size 决定。
        for _symbol, frame, _source, _seconds, _rows, _index in batch:
            _persist_batch(warehouse, [frame])
        for symbol, _frame, source, seconds, rows, index in batch:''',
        ),
    ],
    "backend/astock_backtester/data/warehouse.py": [
        (
            # 统计缓存的写入失效被收窄到只清缺口画像：写入后的口径停在旧值上。
            '''    def invalidate_gap_profile(self) -> None:
        """写入后丢弃缺口画像、股票池计数与每交易日行数缓存，让下一次读取反映最新数据。"""
        with self._gap_profile_lock:
            self._gap_profile_cache = None
        with self._symbol_count_lock:
            self._symbol_count_cache = None
        with self._trade_date_counts_lock:
            self._trade_date_counts_cache = None''',
            '''    def invalidate_gap_profile(self) -> None:
        """写入后丢弃缺口画像缓存，让下一次读取反映最新数据。

        股票池计数与每交易日行数缓存仍按各自的 TTL（10 分钟）自然过期：
        每落一批数据就把两个全局统计缓存打穿，会让写入期间的读路径反复重算，
        整体读成本反而更高。
        """
        with self._gap_profile_lock:
            self._gap_profile_cache = None''',
        ),
    ],
    "backend/astock_backtester/service.py": [
        (
            # 健康检查不再触发/等待覆盖刷新：写入侧状况对外不可见。
            '''    def health_payload(self) -> ServiceHealth:
        refresh_finished = self.start_coverage_refresh()
        if refresh_finished is not None:
            refresh_finished.wait(HEALTH_COVERAGE_WAIT_SECONDS)
        with self._coverage_lock:
            coverage_refreshing = self._coverage_refreshing
        return ServiceHealth(
            **self.identity_payload(),
            coverage=self.coverage_snapshot(),
            coverage_refreshing=coverage_refreshing,
        )''',
            '''    def health_payload(self) -> ServiceHealth:
        # 健康检查保持轻量：只回报进程身份、最近一次 coverage 快照与"服务在跑"，
        # 不在这里触发或等待任何刷新。覆盖数据由数据中心的读写链路与后台定时
        # 刷新推进，健康检查不参与。
        return ServiceHealth(
            **self.identity_payload(),
            coverage=self.coverage_snapshot(),
            coverage_refreshing=False,
        )''',
        ),
    ],
    "backend/astock_backtester/data/operations.py": [
        (
            # 跨进程写锁超时后的有界重试被去掉：锁一忙，整批数据直接判失败。
            '''# 跨进程写锁（LocalCache / Warehouse 的 CrossProcessFileLock，120s 超时）超时后的
# 有界重试：抓取结果此刻还在内存里，锁持有者崩溃时 OS 会自动释放锁，短退避后再试
# 一次就能避免整批数据被丢掉并变成 HTTP 400。3 次 = 首次 + 2 次重试，
# 退避 0.5s/1.0s 线性递增（总等待 ≤ 1.5s，远小于锁本身的 120s 预算）。
WRITE_LOCK_RETRY_ATTEMPTS = 3
WRITE_LOCK_RETRY_BACKOFF_SECONDS = 0.5''',
            '''# 跨进程写锁（LocalCache / Warehouse 的 CrossProcessFileLock，120s 超时）超时
# 之后不再叠加应用层重试：锁本身已经等了 120 秒，退避重试只是把写入方彼此
# 排队的时间再乘一遍重试次数，同时写库的程序越多、整体越慢。
WRITE_LOCK_RETRY_ATTEMPTS = 1
WRITE_LOCK_RETRY_BACKOFF_SECONDS = 0.5''',
        ),
    ],
}

EDIT_SPECS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "T1-01": T1_01,
    "T2-04": T2_04,
    "T3-10": T3_10,
}


def materialize(
    repo: Path, spec: dict[str, list[tuple[str, str]]], out_root: Path
) -> list[tuple[str, str, Path]]:
    """按规格产出注入版文件；返回 ``(相对路径, patch 文本, 注入版路径)`` 列表。"""
    results: list[tuple[str, str, Path]] = []
    for rel_path, edits in spec.items():
        source = repo / rel_path
        if not source.is_file():
            raise SystemExit(f"[注入失败] 仓库里找不到 {rel_path}")
        original_text = source.read_text(encoding="utf-8")
        text = original_text
        for index, (old, new) in enumerate(edits, start=1):
            if old == new:
                continue
            occurrences = text.count(old)
            if occurrences != 1:
                first_line = old.strip().splitlines()[0][:80] if old.strip() else "(空锚点)"
                raise SystemExit(
                    f"[注入失败] {rel_path} 第 {index} 条锚点命中 {occurrences} 次（必须恰好 1 次）。\n"
                    f"  锚点首行：{first_line}"
                )
            text = text.replace(old, new, 1)
        if text == original_text:
            raise SystemExit(f"[注入失败] {rel_path} 替换后内容无变化")
        target = out_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        patch = build_patch(
            rel_path,
            original_text.splitlines(keepends=True),
            text.splitlines(keepends=True),
        )
        results.append((rel_path, patch, target))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成任务包注入 patch")
    parser.add_argument("--repo", required=True, help="受测仓库根（只读）")
    parser.add_argument("--task", required=True, choices=sorted(EDIT_SPECS), help="任务 ID")
    parser.add_argument("--work-dir", default=None, help="注入版中间文件目录，默认 tools/_work/<task>")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    tools_dir = Path(__file__).resolve().parent
    work_dir = Path(args.work_dir) if args.work_dir else tools_dir / "_work" / args.task
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    built = materialize(repo, EDIT_SPECS[args.task], work_dir)
    out_dir = tools_dir.parent / "tasks" / args.task / "inject" / "patches"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.patch"):
        stale.unlink()

    for index, (rel_path, patch, _target) in enumerate(built, start=1):
        stem = Path(rel_path).name.replace(".py", "")
        name = f"{index:04d}-{stem}.patch"
        (out_dir / name).write_text(patch, encoding="utf-8", newline="\n")
        changed = sum(
            1 for line in patch.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")
        )
        print(f"[{args.task}] {name}  {rel_path}  （{changed} 行增删）")
    print(f"[{args.task}] 注入版中间文件：{work_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
