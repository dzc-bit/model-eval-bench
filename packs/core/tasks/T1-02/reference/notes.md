# T1-02 参考解说明（成题版）

> 本文件只进 `reference/`，永不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁全过（见 `calibration/` 的 gate_*.json）。

## 一、注入端口（3 处，落盘 2 文件 + 1 处语义端口）

| # | 位置 | 注入内容 | 症状面 |
| --- | --- | --- | --- |
| 1 | `backend/.../data/trading_calendar.py` | `_A_SHARE_HOLIDAY_RANGES` 整年删除 **2026**（含元旦/春节/清明/五一/端午/中秋/国庆七段） | 覆盖表把春节等假期当交易日要数据 → "缺失交易日"假阳性 |
| 2 | `frontend/src/tradingCalendar.ts` | `A_SHARE_HOLIDAY_RANGES` 整年删除 **2025** | 页面侧把 2025 假期当交易日 |
| 3 | `frontend/src/tradingCalendar.ts` | `recentAShareTradingDateRange` 不再把末端回退到最近交易日（直接取今天） | 默认窗口末端落在休市日（如国庆当天），补数白跑 |

端口 1 与 2 组成"两端各缺一年"的镜像结构——coherence 组必然红；端口 3 独立成组。
三个端口都静默：不改函数签名、不抛错、不产生任何告警差异。

**为什么不是回滚历史修复（相似度 < 0.6）**：历史上的 8aebce8 是"补录 2015-2023"，
本注入是"删除单年条目 + 去掉落点回退"，形态与注释完全不同；落点回退的删除附带了
自洽的新注释（"数据末日的对齐由上层裁剪"），读起来是合理简化而非破坏。

## 二、原生（非注入）事实，成题时的取舍

- 前端表原生只覆盖 2024–2028（后端 2015–2028）。**保留**这一差异但不作为考点：
  coherence 组的日期采样与年份断言全部限定在运营年份 2024–2028 内，避免把
  初级题变成"抄九年数据"。notes 留档，防止后续成题者误当注入修。
- `operations.effective_a_share_date_range` 的裁剪语义**正确**——按症状读它最可疑，
  是本题的主诱饵点（allowed_paths 故意包含 operations.py 与 App.tsx）。
- 快照里可见测试注入后变红 3 条（点名 2026 日历语义），已全部进 `visible.prune`：
  `test_fetch_capital_flow_into_cache_clips_requested_range_to_a_share_trade_dates`、
  `test_market_trade_date_counts_skips_corrupt_partition`、
  `test_read_capital_flow_missing_symbols_spans_window_partitions`，
  另有 2 条 2026 点名用例与 `frontend/src/tradingCalendar.test.ts`（整文件）按
  "名字/断言点名答案"预裁剪。沙箱内 0 个可见红测试。

## 三、锚解的形态：抽共享点，接通 2 个调用方

1. `trading_calendar.py`：恢复 2026 年七段假期。
2. `tradingCalendar.ts`：恢复 2025 年六段假期。
3. `tradingCalendar.ts`：抽 `tradingWindowEnding(endDate, days)` 共享实现——
   末端先 `previousAShareTradingDay` 回退、再往回数满 N 个交易日；
   `recentAShareTradingDateRange` 与 `recentAShareTradingDateRangeEnding` 都委托给它，
   任何一处都不再自行决定"落点要不要校验"。

## 四、陷阱

- **陷阱 A（半成品演示）**：只修后端表。`partial.patch` 实测 20/100——
  `backend_calendar_exit` 绿，coherence（2 权重）与页面侧两组全红。
- **陷阱 B**：只修前端。对称地 `backend_calendar_exit` 红。
- **陷阱 C**："把默认窗口的回退去掉，让上层裁剪兜底"——症状更隐蔽但
  `default_window_exit` 红（两条入口都必须经共享实现回退）。
- **陷阱 D**：删掉/改写 2027、2028 等相邻年份"省事"——
  `test_adjacent_years_keep_their_own_holidays_and_regular_weeks` 红。
- **诱饵点**：`effective_a_share_date_range`（裁剪语义正确）；`App.tsx` 的
  `min(数据末日, 今天)`（两个候选都是合理日期，问题在回退语义而非取数）。

## 五、§6.5 反过易检查清单

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"——注入删除整年条目，无注释残留；AGENTS/CHANGELOG 不进快照。
- [x] ≥1 个"看似可疑但实际正确"的诱饵点——`effective_a_share_date_range`、`App.tsx` 取数。
- [x] 每组隐藏测试有第二数据场景——后端组：七段假期+常规工作周；页面组：2025 与 2026 双缺口+2024 对照；窗口组：节假日末端+周末末端+任意末端+跨年四处。
- [x] 症状与三级提示词不含任何文件/函数/常量名——通篇只有"覆盖表/页面/默认窗口/春节/国庆"。
- [x] 只修一个端口的半成品必然 <100——partial 实测 20 分。
- [x] 出题者自评"10 分钟能一次做对"→ 退回重做——**超过 10 分钟**：三处注入分属两端、
  coherence 要求逐条一致，先要把"哪些年份哪段缺失"从数据反推出来，再决定共享实现落点。

## 六、门禁自验结果（§5.3，packgate 实测 2026-09-30）

| 门禁 | 结果 |
| --- | --- |
| 锚解（fix.patch） | **100.0**，4 组全绿，p2p 166/166 绿 |
| 半成品（partial.patch） | **20.0**（<100 成立） |
| 注入态 | 0.0，4 组全红 |
| 注入态 ×20 | 得分稳定 0.0，无 flaky |
| 参考解不触碰 forbidden_paths | 通过（仅 trading_calendar.py 与 tradingCalendar.ts，均在 allowed_paths） |
| 沙箱可见红测试 | 0（6 条点名用例已裁剪） |

## 七、校准状态（§6.4）

`calibration/results.json` 盲测表待填，`calibrated = false`。出题模型不做盲测。
