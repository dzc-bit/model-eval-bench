"""T1-02 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

产出（写进 packs/core/tasks/T1-02/）：
  inject/patches/0001-backend-holiday-table.patch   后端节日表缺 2026 年
  inject/patches/0002-frontend-calendar-window.patch 前端表缺 2025 年 + 默认窗口去掉落点回退
  reference/fix.patch                               锚解：两端表复原 + 窗口语义收敛到共享实现
  reference/partial.patch                           半成品：只修后端表
  hidden/tests_hidden/test_calendar_two_sides.py    pytest 隐藏测试（后端出口 + 跨端 coherence）
  hidden/groups.json
  hidden-fe/tests_hidden_fe/tradingCalendar.hidden.test.ts  vitest 隐藏测试
  hidden-fe/groups_fe.json
  p2p.json / p2p-fe.json                            候选白名单（基线树收集，注入后经验修正）
  prompts/1.md 2.md 3.md、calibration/results.json、meta.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T1-02"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

BACKEND_REL = "backend/astock_backtester/data/trading_calendar.py"
FE_REL = "frontend/src/tradingCalendar.ts"

backend_src = (REPO / BACKEND_REL).read_text(encoding="utf-8")
fe_src = (REPO / FE_REL).read_text(encoding="utf-8")

# --------------------------------------------------------------------------
# 一、注入变体（三端口：后端表缺 2026；前端表缺 2025；默认窗口不回退落点）
# --------------------------------------------------------------------------

backend_injected = backend_src.replace(
    """    2026: (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ),
""", "", 1)
assert backend_injected != backend_src, "后端 2026 条目未命中"

fe_injected = fe_src.replace(
    """  2025: [
    ["2025-01-01", "2025-01-01"],
    ["2025-01-28", "2025-02-04"],
    ["2025-04-04", "2025-04-06"],
    ["2025-05-01", "2025-05-05"],
    ["2025-05-31", "2025-06-02"],
    ["2025-10-01", "2025-10-08"]
  ],
""", "", 1)
assert fe_injected != fe_src, "前端 2025 条目未命中"

fe_injected = fe_injected.replace(
    """export function recentAShareTradingDateRange(days = 5): { startDate: string; endDate: string } {
  const end = previousAShareTradingDay(new Date());
  const start = new Date(end);""",
    """export function recentAShareTradingDateRange(days = 5): { startDate: string; endDate: string } {
  // 窗口末端直接取今天：数据末日的对齐由上层裁剪，这里不必再回退一步
  const end = new Date();
  end.setHours(0, 0, 0, 0);
  const start = new Date(end);""", 1)
assert "end.setHours(0, 0, 0, 0);" in fe_injected, "前端窗口注入未命中"

# --------------------------------------------------------------------------
# 二、锚解变体（后端表复原；前端表复原 + 两个默认窗口函数收敛到共享实现）
# --------------------------------------------------------------------------

backend_fixed = backend_src

fe_fixed = fe_src.replace(
    """export function recentAShareTradingDateRange(days = 5): { startDate: string; endDate: string } {
  const end = previousAShareTradingDay(new Date());
  const start = new Date(end);
  let counted = 1;
  while (counted < days) {
    start.setDate(start.getDate() - 1);
    if (isAShareTradingDay(start)) {
      counted += 1;
    }
  }
  return { startDate: formatLocalDate(start), endDate: formatLocalDate(end) };
}

export function recentAShareTradingDateRangeEnding(
  endDate: string,
  days = 5
): { startDate: string; endDate: string } {
  const end = new Date(`${endDate}T00:00:00`);
  const start = new Date(end);
  let counted = 1;
  while (counted < days) {
    start.setDate(start.getDate() - 1);
    if (isAShareTradingDay(start)) {
      counted += 1;
    }
  }
  return { startDate: formatLocalDate(start), endDate };
}""",
    """// 默认窗口的唯一实现：末端先回退到最近的交易日，再往回数满 days 个交易日。
// 两个对外入口都必须走这里，任何一处都不允许自行决定"落点要不要校验"。
function tradingWindowEnding(
  endDate: Date,
  days = 5
): { startDate: string; endDate: string } {
  const end = previousAShareTradingDay(endDate);
  const start = new Date(end);
  let counted = 1;
  while (counted < days) {
    start.setDate(start.getDate() - 1);
    if (isAShareTradingDay(start)) {
      counted += 1;
    }
  }
  return { startDate: formatLocalDate(start), endDate: formatLocalDate(end) };
}

export function recentAShareTradingDateRange(days = 5): { startDate: string; endDate: string } {
  return tradingWindowEnding(new Date(), days);
}

export function recentAShareTradingDateRangeEnding(
  endDate: string,
  days = 5
): { startDate: string; endDate: string } {
  return tradingWindowEnding(new Date(`${endDate}T00:00:00`), days);
}""", 1)
assert "tradingWindowEnding" in fe_fixed, "前端锚解未命中"

# --------------------------------------------------------------------------
# 三、补丁产出
# --------------------------------------------------------------------------

def diff(original: str, current: str) -> str:
    return build_patch("", original.splitlines(keepends=True), current.splitlines(keepends=True))


INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE.mkdir(parents=True, exist_ok=True)

patch_backend = build_patch(BACKEND_REL, backend_src.splitlines(keepends=True), backend_injected.splitlines(keepends=True))
patch_fe = build_patch(FE_REL, fe_src.splitlines(keepends=True), fe_injected.splitlines(keepends=True))
(INJECT_DIR / "0001-backend-holiday-table.patch").write_text(patch_backend, encoding="utf-8")
(INJECT_DIR / "0002-frontend-calendar-window.patch").write_text(patch_fe, encoding="utf-8")

fix_parts = []
if backend_fixed != backend_injected:
    fix_parts.append(build_patch(BACKEND_REL, backend_injected.splitlines(keepends=True), backend_fixed.splitlines(keepends=True)))
if fe_fixed != fe_injected:
    fix_parts.append(build_patch(FE_REL, fe_injected.splitlines(keepends=True), fe_fixed.splitlines(keepends=True)))
(REFERENCE / "fix.patch").write_text("".join(fix_parts), encoding="utf-8")

partial_parts = [build_patch(BACKEND_REL, backend_injected.splitlines(keepends=True), backend_fixed.splitlines(keepends=True))]
(REFERENCE / "partial.patch").write_text("".join(partial_parts), encoding="utf-8")
print("补丁已生成")

# --------------------------------------------------------------------------
# 四、隐藏测试
# --------------------------------------------------------------------------

HIDDEN = TASK / "hidden" / "tests_hidden"
HIDDEN_FE = TASK / "hidden-fe" / "tests_hidden_fe"
HIDDEN.mkdir(parents=True, exist_ok=True)
HIDDEN_FE.mkdir(parents=True, exist_ok=True)

(HIDDEN / "test_calendar_two_sides.py").write_text('''"""T1-02 隐藏测试：跨端交易日历一致性。

只断言"事实"层面的不变量，不点名实现位置：同一份休市安排被两端各自维护时，
重叠年份的回答必须一致；缺失年份的节日在哪一端都不能变成交易日。
"""

from pathlib import Path

import pandas as pd

from backend.astock_backtester.data.trading_calendar import (
    _A_SHARE_HOLIDAY_RANGES,
    a_share_trade_dates,
)

FRONTEND_CALENDAR = Path("frontend/src/tradingCalendar.ts")

# 两端在产品上都应当覆盖的"运营年份"：覆盖窗口与默认日期都落在这个范围内
OPERATIONAL_YEARS = [2024, 2025, 2026, 2027, 2028]


def _ranges_per_year(text: str) -> dict[int, list[tuple[str, str]]]:
    """从前端源码里解析出每年节假日区间（原文逐条比对，不做语义解释）。"""
    table = text.split("A_SHARE_HOLIDAY_RANGES", 1)[1]
    result: dict[int, list[tuple[str, str]]] = {}
    current: int | None = None
    for line in table.splitlines():
        head = line.strip()
        if head.startswith("};"):
            break
        year = re.match(r"^(\\d{4}): \\[$", head)
        if year:
            current = int(year.group(1))
            result.setdefault(current, [])
            continue
        pair = re.match(r'^\\["(\\d{4}-\\d{2}-\\d{2})", "(\\d{4}-\\d{2}-\\d{2})"\\],?$', head)
        if pair and current is not None:
            result[current].append((pair.group(1), pair.group(2)))
    return result


import re  # noqa: E402  放在解析函数之后便于阅读


def test_spring_festival_and_other_2026_holidays_are_not_trade_dates():
    """注入年份（2026）的全部假期段都必须被排除在交易日之外。"""
    for start, end in [
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ]:
        days = a_share_trade_dates(pd.Timestamp(start), pd.Timestamp(end))
        assert not days, f"{start}~{end} 是法定节假日，不应出现交易日：{sorted(days)}"


def test_adjacent_years_keep_their_own_holidays_and_regular_weeks():
    """邻近年份的假期必须原样保留，常规工作周也不得被误伤（防"删年了事"）。"""
    for start, end in [
        ("2024-10-01", "2024-10-07"),
        ("2025-01-28", "2025-02-04"),
        ("2027-02-05", "2027-02-13"),
        ("2028-01-25", "2028-02-02"),
    ]:
        days = a_share_trade_dates(pd.Timestamp(start), pd.Timestamp(end))
        assert not days, f"{start}~{end} 应为假期，却出现了交易日：{sorted(days)}"
    regular = a_share_trade_dates(pd.Timestamp("2026-02-09"), pd.Timestamp("2026-02-13"))
    assert len(regular) == 5, f"春节前的工作周应完整：{sorted(regular)}"


def test_both_ends_cover_the_operational_years():
    """两端都必须覆盖全部运营年份——缺哪一年，这一端就该红。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))
    missing_backend = [y for y in OPERATIONAL_YEARS if y not in _A_SHARE_HOLIDAY_RANGES]
    missing_frontend = [y for y in OPERATIONAL_YEARS if y not in fe]
    assert not missing_backend, f"后端节日表缺年份：{missing_backend}"
    assert not missing_frontend, f"前端节日表缺年份：{missing_frontend}"


def test_both_ends_agree_on_every_operational_year():
    """重叠年份的节假日区间必须逐条一致（这是"收口"的最低要求）。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))
    for year in OPERATIONAL_YEARS:
        backend = sorted(_A_SHARE_HOLIDAY_RANGES.get(year, ()))
        frontend = sorted(fe.get(year, []))
        assert backend == frontend, (
            f"{year} 年两端节假日不一致：\\n  仅后端有 {sorted(set(backend) - set(frontend))}"
            f"\\n  仅前端有 {sorted(set(frontend) - set(backend))}"
        )


def test_trade_dates_and_frontend_table_agree_on_sampled_days():
    """抽样日上，后端交易日序列与前端表的回答必须同真同假。"""
    fe = _ranges_per_year(FRONTEND_CALENDAR.read_text(encoding="utf-8"))

    def frontend_says_holiday(day: str) -> bool:
        year = int(day[:4])
        return any(start <= day <= end for start, end in fe.get(year, []))

    samples = [
        "2024-02-12", "2024-10-02", "2025-01-29", "2025-10-02",
        "2026-02-16", "2026-02-18", "2026-10-02", "2027-02-08", "2028-01-26",
        "2026-02-11", "2027-03-15", "2025-06-05",
    ]
    for day in samples:
        stamp = pd.Timestamp(day)
        backend_says_holiday = stamp not in a_share_trade_dates(stamp, stamp)
        assert backend_says_holiday == frontend_says_holiday(day), (
            f"{day}：后端{'休市' if backend_says_holiday else '交易'}，"
            f"前端{'休市' if frontend_says_holiday(day) else '交易'}——两端口径分叉"
        )
''', encoding="utf-8")

(HIDDEN_FE / "tradingCalendar.hidden.test.ts").write_text('''// T1-02 隐藏测试（页面侧）：默认窗口落点与节假日判定。
// 只依赖本模块导出的行为，不关心实现放在哪个文件。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isAShareTradingDay,
  recentAShareTradingDateRange,
  recentAShareTradingDateRangeEnding
} from "../tradingCalendar";

const day = (text: string) => new Date(`${text}T00:00:00`);

afterEach(() => {
  vi.useRealTimers();
});

describe("页面侧交易日判定", () => {
  it("把注入缺口年份与原生缺口年份的假期都判为休市", () => {
    expect(isAShareTradingDay(day("2025-01-29"))).toBe(false);
    expect(isAShareTradingDay(day("2025-10-02"))).toBe(false);
    expect(isAShareTradingDay(day("2026-02-17"))).toBe(false);
  });

  it("不误伤周末之外的真实交易日", () => {
    expect(isAShareTradingDay(day("2024-02-12"))).toBe(false);
    expect(isAShareTradingDay(day("2026-02-11"))).toBe(true);
    expect(isAShareTradingDay(day("2025-06-05"))).toBe(true);
  });
});

describe("默认补数窗口", () => {
  it("末端是节假日时先回退到最近交易日", () => {
    vi.useFakeTimers();
    vi.setSystemTime(day("2026-10-01"));
    const range = recentAShareTradingDateRange();
    expect(range.endDate).toBe("2026-09-30");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
  });

  it("周末当口同样回退，且不重复回退", () => {
    vi.useFakeTimers();
    vi.setSystemTime(day("2024-06-08"));
    const range = recentAShareTradingDateRange();
    expect(range.endDate).toBe("2024-06-07");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
  });

  it("给定末端落在假期内时，末端与起点都必须是交易日", () => {
    const range = recentAShareTradingDateRangeEnding("2026-02-21");
    expect(isAShareTradingDay(day(range.endDate))).toBe(true);
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
    expect(range.endDate <= "2026-02-21").toBe(true);
  });

  it("跨年默认窗口的两端同样落在交易日上", () => {
    const range = recentAShareTradingDateRangeEnding("2027-01-02");
    expect(range.endDate).toBe("2026-12-31");
    expect(isAShareTradingDay(day(range.startDate))).toBe(true);
    expect(range.startDate.startsWith("2026-")).toBe(true);
  });
});
''', encoding="utf-8")

# --------------------------------------------------------------------------
# 五、分组 / p2p / 提示词 / 校准 / meta
# --------------------------------------------------------------------------

(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T1-02",
    "note": "pytest 侧分组。组 = 一个出口/一条独立事实。coherence 组权重最高，断言两端对同一日期集合的回答一致；它红，说明至少还有一端没接上。页面侧分组在 hidden-fe/groups_fe.json（vitest check 各读各的 groups）。",
    "groups": [
        {
            "id": "backend_calendar_exit",
            "weight": 1,
            "port": "后端覆盖口径：节假日不得混进交易日序列",
            "tests": [
                "hidden/tests_hidden/test_calendar_two_sides.py::test_spring_festival_and_other_2026_holidays_are_not_trade_dates",
                "hidden/tests_hidden/test_calendar_two_sides.py::test_adjacent_years_keep_their_own_holidays_and_regular_weeks",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "跨端一致性：同一日期，两端必须同答案；重叠年份逐条一致",
            "tests": [
                "hidden/tests_hidden/test_calendar_two_sides.py::test_both_ends_cover_the_operational_years",
                "hidden/tests_hidden/test_calendar_two_sides.py::test_both_ends_agree_on_every_operational_year",
                "hidden/tests_hidden/test_calendar_two_sides.py::test_trade_dates_and_frontend_table_agree_on_sampled_days",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "既有用例白名单见任务根 p2p.json。任一条红 → 本轮作废（0 分）。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")

(TASK / "hidden-fe" / "groups_fe.json").write_text(json.dumps({
    "schema": 1,
    "task": "T1-02",
    "note": "页面侧（vitest）分组，由 vitest check 加载；与 pytest 侧共用同一张计分表。",
    "groups": [
        {
            "id": "frontend_calendar_exit",
            "weight": 1,
            "port": "页面口径：页面侧的休市判定",
            "tests": [
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::把注入缺口年份与原生缺口年份的假期都判为休市",
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::不误伤周末之外的真实交易日",
            ],
        },
        {
            "id": "default_window_exit",
            "weight": 1,
            "port": "默认窗口：起止两端必须落在真实交易日",
            "tests": [
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::末端是节假日时先回退到最近交易日",
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::周末当口同样回退，且不重复回退",
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::给定末端落在假期内时，末端与起点都必须是交易日",
                "tests_hidden_fe/tradingCalendar.hidden.test.ts::跨年默认窗口的两端同样落在交易日上",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "页面侧既有用例白名单见任务根 p2p-fe.json。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8")

# 说明：pytest 侧 groups.json 只含 pytest 组；vitest 组在 hidden-fe/groups_fe.json，
# 由 vitest check 自己加载解析——与生产 harness「每条 check 各读各的 groups」的结构一致。

(TASK / "p2p-fe.json").write_text(json.dumps({
    "schema": 1,
    "task": "T1-02",
    "note": "页面侧既有用例白名单（基线全绿、注入不红）。tradingCalendar.test.ts 因点名具体节日日期已整体裁剪。",
    "tests": [],
}, ensure_ascii=False, indent=2), encoding="utf-8")

# p2p 候选：在基线树上收集三个相关测试文件的用例，注入后经验性剔除变红者。
CANDIDATE_FILES = [
    "tests/test_core.py",
    "tests/test_data_operations.py",
    "tests/test_warehouse.py",
]

baseline_tree = packgate.GATES / "T1-02-collect"
packgate.build_tree("T1-02", json.loads((TASK / "meta.json").read_text(encoding="utf-8")),
                    baseline_tree, [])
done = subprocess.run(
    [sys.executable, "-m", "pytest", *CANDIDATE_FILES, "--collect-only", "-q",
     "-p", "no:cacheprovider"],
    cwd=baseline_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
candidates = sorted({
    line.strip() for line in done.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line.strip()
})
(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T1-02",
    "note": "基线（未注入）全绿的既有用例。快照里本就红、与本任务无关的用例已剔除；注入后变红、点名答案的用例进 visible.prune（见 reference/notes.md）。",
    "tests": candidates,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"p2p 候选 {len(candidates)} 条")

PROMPTS = TASK / "prompts"
PROMPTS.mkdir(exist_ok=True)
(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

我用这套工具管理行情数据的覆盖检查，最近被几件事搞得很糊涂：

1. 把覆盖窗口选到 2 月中下旬春节那一周，覆盖表把整个春节假期都列成了"缺失交易日"，
   一列红叉。可那几天市场本来就是休市的，根本不该出现在缺失名单里。
2. 不改起止日期、直接用默认窗口做补数时，窗口的结束日期时不时落在休市日上：
   有一次正好落在 10 月 1 日，补数任务白跑一趟；跨年前后尤其容易对不上。
3. 更拧巴的是：同一个日子，页面上的提示和覆盖明细的说法对不上——页面说那天"休市"，
   覆盖明细却把它当成交易日，反过来要求那天的数据。

## 验收要求

修好之后，下面这条必须成立：**关于"哪天休市"，系统里只能有一个答案，所有地方都按它来。** 具体说：

- 法定节假日（含调休连休的整段）在任何出口都不能被当成交易日或缺失交易日；
- 两端对"某一天是不是交易日"的回答必须一致——页面显示什么，覆盖明细就认什么；
- 默认窗口（不改日期直接用）的起止两端都必须落在真实交易日上，跨年也一样。

我不要求你改测试，也不需要新增功能。请把根因修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把"哪天休市"这件事在系统里走一遍，会发现它被好几处各自维护、而且互相矛盾：

1. 覆盖检查在生成"应有哪些交易日"的名单时，用的是一端自己维护的休市安排；
2. 页面上的交易日提示，用的是另一端手抄的一份休市安排；
3. 这两份安排连重叠年份的内容都已经互相出入（近几年有的假期一段有、一段没有）；
4. 默认窗口的生成方式又是独立的一套：有的入口会把落点回退到最近的交易日，
   有的入口完全不看落点，直接拿给定日期当窗口末端。

任何一处单独看都"有自己的道理"，合在一起就是：同一个事实，多个来源、多种答案。
''', encoding="utf-8")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时成立的表述：

1. 休市安排要收口：要么只有一个权威来源，要么各副本逐条一致并有跨端校验兜底——
   任何"各自维护、偶尔同步"的状态都不合格。
2. "某天是否交易日"在所有出口（覆盖名单、页面提示、默认窗口落点）必须同源同答案。
3. 默认窗口的两端必须先落到真实交易日、再往回数窗口；不允许把任意给定日期直接
   当窗口末端，也不允许只修其中一个入口。
4. 对表外年份，宁可明确提示"超出已知的休市安排"，也不得静默当作全年无休。

已被否决的思路（不要重提）：

- "把两边的表各自手工补齐就算修好"——补齐只是又一次手工镜像，下次更新还会漂移；
  补数据可以，但必须同时把"以后不再漂移"的机制立起来。
- "覆盖检查遇到春节附近就跳过"——把问题藏进特判，换个假期照样犯。
- "把默认窗口的回退去掉，让上层自己裁"——落点校验就是防线本身，不是冗余。
''', encoding="utf-8")

CALIB = TASK / "calibration"
CALIB.mkdir(exist_ok=True)
(CALIB / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T1-02",
    "calibrated": False,
    "target_band": [0.6, 0.85],
    "owner": "author",
    "policy": "§6.4 硬纪律：出题模型不得给自己出的题做校准。本表在盲测完成前保持空表，calibrated 恒为 false。",
    "gate": {
        "note": "出题侧门禁（§5.3）由 runs/blind/tools/packgate.py 跑，原始输出见 gate_*.json。这些不是校准数据，不参与 pass@1 统计。",
        "anchor_solution": "gate_fixed.json",
        "partial_solution": "gate_partial.json",
        "injected_state": "gate_injected_x20.json",
    },
    "blind_runs": {
        "note": "每一行 = 一次『只给第 1 级提示词』的完整作答。由非出题模型填写。",
        "columns": ["run_id", "model", "tier", "prompt_level", "pass@1", "score",
                    "failed_groups", "p2p_broken", "notes"],
        "rows": [],
    },
    "summary": {"runs": 0, "pass_at_1": None, "confidence_interval": None,
                "in_band": None, "conclusion": None},
}, ensure_ascii=False, indent=2), encoding="utf-8")

meta = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
meta.pop("status", None)
meta["visible"]["prune"] = [
    "tests/test_data_operations.py::test_coverage_uses_a_share_trading_calendar_for_2026_holidays",
    "tests/test_data_operations.py::test_coverage_reports_requested_range_edge_gaps_without_holiday_false_positives",
    "frontend/src/tradingCalendar.test.ts",
]
meta["checks"] = [
    {"kind": "pytest", "hidden": "hidden/tests_hidden", "groups": "hidden/groups.json", "p2p": "p2p.json"},
    {"kind": "vitest", "hidden": "hidden-fe/tests_hidden_fe", "groups": "hidden-fe/groups_fe.json", "p2p": "p2p-fe.json"},
]
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print("T1-02 成题完成")
