# T3-10 出题存档（reference/notes.md）

> 本文件只进 `reference/`，永不进沙箱白名单。它是这道题的"出题侧全账"：注入点、
> 陷阱、裁剪理由、§6.5 逐条自证、门禁原始结论。

## 1. 出题意图（§7 第 10 题 · 高级）

真实缺陷模式：**脚本 → 仓库 → 服务** 三层之间没有一条统一的写入协议。外部补数
脚本自持一份"分区现状"并在内存里合并、整段覆盖回盘；仓库侧的写入入口（分区锁 +
读改写合并）被绕开；写入派生的统计口径与健康口径各自为政。四个症状互相独立，
只有把"谁持有分区现状、什么时候失效统计、健康看不看得见写入"一次接回来才同时消掉。

§7 指定结构：**锁/原子/攒批/健康四组 + 进程级并发测试**；陷阱是"单进程测试全绿但
进程级组必红"。本题的进程级断言用**两个真子进程 + 文件栅栏**，不用墙钟计时。

与邻题的分工（注入点零重合，成题时逐行核对过）：

| 邻题 | 它的地盘 | 本题避开的做法 |
| --- | --- | --- |
| T2-06 | 仓库**内部**四道防线：跨进程锁失效、原子替换失效、损坏静默、诊断健康隐藏 | 不碰 `data/filelock.py`；不碰 `_atomic_write_parquet`；不碰 `/diagnostics/data-gaps` 的 `warehouse_health`；`warehouse.py` 只碰统计缓存失效 |
| T2-04 | 缺口四出口的横截面分类阈值 | 不碰 `classify_market_days_by_cross_section` 与三个样本下限 |
| T1-01 | 派生列口径（换手率/市值/涨跌停） | 与派生列无关 |

## 2. §6.5 反过易检查清单（逐条打勾）

- [x] grep / 读文档 / git log 找不到"该修哪里、改成什么"：注入是**合成改写**（自造"分区会话"这一历史上不存在的形态），不是回滚历史提交；`AGENTS.md` §9/§15 与 `CHANGELOG.md` 1.5.1/1.5.2/1.6.0/1.6.1 里直接点破写入协议与健康刷新的段落已进 `meta.redactions`；仓库里没有任何守卫用例的名字或断言指向"分区会话"。
- [x] ≥1 个"看似可疑但实际正确"的诱饵点：`--write-batch-size` 旋钮本身（调大只把"以内存快照覆盖分区"的窗口拉长，丢的行更多，且隐藏用例显式传批大小，改默认值无效）；以及 `scripts/backfill-market-cap.py` 的"整段读进来再整行写回去"——在按年分区的本地仓里这是**正确**的分区级协议，改它只会弄红既有用例。
- [x] 每组隐藏测试有第二数据场景，硬编码/特判必挂：攒批组两组不同批大小与只数；放大组两组不同行数与批大小；新鲜度组"每交易日行数"与"股票池计数"两个独立缓存；锁争用组一次/两次瞬时超时 + 一次端到端；健康组"外部写入可见"与"刷新在跑如实报告"；跨进程组"不同股票"与"同一只股票不同交易日"。
- [x] 症状与三级提示词不含任何文件/函数/常量名：`packcheck.py` 把 allowed_paths、隐藏测试标识符、注入补丁与参考解里的文件名汇成禁用词表逐级扫描，三级提示词零命中。
- [x] 只修一个端口的半成品必然 < 100：`reference/partial.patch` 只还原脚本侧（攒批/放大/跨进程三组转绿），其余三处仍退化，实测 **37.5 / 100**（3/8 权重）。
- [x] 出题者自评"我 10 分钟能一次做对" → 退回重做：不成立。要同时想到"写入方不该自持分区快照""写入后必须打穿两个统计缓存""健康检查要参与写入侧刷新""锁超时有界重试"四件事，且单进程跑通永远看不见跨进程丢失与健康哑火，远超 10 分钟。

## 3. 勘察结论（注入前的仓库现状，行号已复核）

| 位置 | 现状 |
| --- | --- |
| `scripts/run-full-market-import.py` | `--write-batch-size` 默认 25（L184）；`flush_batch` 把整批 `pd.concat` 后**一次**交给 `warehouse.write_daily_bars`（L217-238）；逐票进度 JSONL 事件（L197-308） |
| `scripts/run-capital-flow-backfill.py` | `DEFAULT_BATCH_SIZE = 20`（L32）；`run_supervised_backfill` 连败熔断 + JSONL 断点续跑（L309-483）；写库经 `data/operations.py::fetch_capital_flow_into_cache` |
| `scripts/backfill-market-cap.py` | `backfill_market_cap`（L124）逐分区读后整行回写——**正确的分区级协议**，本题的诱饵 |
| `backend/astock_backtester/data/warehouse.py` | `write_daily_bars` 持分区锁做 read-modify-write + 原子替换（L172-196）；`invalidate_gap_profile` 一次清三份缓存（缺口画像 / 股票池计数 / 每交易日行数，L215-222）；`market_trade_date_counts` 10 分钟 TTL 单条缓存、key 带窗口（L1259-1286） |
| `backend/astock_backtester/service.py` | `health_payload`（L217）触发并短等一次覆盖刷新、如实回报 `coverage_refreshing`；写入路由在写入后 `set_coverage_snapshot` + `start_coverage_refresh(force=True)`（L1005-1024、L1117-1118） |
| `backend/astock_backtester/data/operations.py` | `_write_with_lock_retry`（L55）对 `CrossProcessFileLock` 超时做 3 次有界退避重试（L47-52），被 3 处写库调用点复用 |

可见测试风险点（注入后实测变红，已全部进 `visible.prune`，见第 6 节）。

## 4. 四端口注入点

| # | 文件 | 注入形态 | 对应症状 |
| --- | --- | --- | --- |
| 1 | `scripts/run-full-market-import.py` | 新增模块级 `_PARTITION_SESSIONS` + `_persist_batch()`：首次落盘把整段分区读进内存会话，之后每批只跟会话合并、整段覆盖回盘；`flush_batch` 改为**逐票**过一次会话；`--write-batch-size` 默认 25 → 1 | 越补越慢、越写越坏 |
| 2 | `backend/astock_backtester/data/warehouse.py` | `invalidate_gap_profile` 只清缺口画像缓存，股票池计数与每交易日行数缓存改由 10 分钟 TTL 兜底 | 外面看不出任何异常 |
| 3 | `backend/astock_backtester/service.py` | `health_payload` 不再触发/等待覆盖刷新，`coverage_refreshing` 恒为 `False` | 健康检查一直说"正常" |
| 4 | `backend/astock_backtester/data/operations.py` | `WRITE_LOCK_RETRY_ATTEMPTS` 3 → 1，去掉注释里"有界重试避免整批被丢掉"的口径 | 偶发整批失败 |

端口 1 是主端口，同时长出三件事：**攒批退化**（落盘次数 = 股票只数）、**写放大**
（每次落盘重写整段会话，累计写盘量随批数平方增长）、**跨进程丢失**（会话快照只取
一次，另一进程期间写入的行被整段覆盖）。它还顺带绕开了仓库的写入入口，所以统计
缓存失效与数据集登记也一起丢了——这正是 `coherence` 组要抓的"四处对不上"。

## 5. 陷阱与诱饵

1. **陷阱 A（§7 指定）**：只把单进程路径修好（例如只让脚本回到"整批一次落盘"）
   仍然会丢：写入方自持分区快照这件事没解决，进程级组照旧红。`reference/partial.patch`
   就是这一形态，实测 37.5 分。
2. **陷阱 B**：把 `--write-batch-size` 调大"治慢"。隐藏用例显式传批大小，改默认值
   一分不得；而且批越大，"以内存快照覆盖分区"的窗口越长，丢的行越多。
3. **陷阱 C**：让健康检查每次都同步做一遍全量重扫。全仓重扫是几十秒级的，健康
   检查被拖死，`health_exit` 的"刷新在跑要如实报告"反而拿不到。
4. **陷阱 D**：把锁超时的失败直接吞掉当成功。汇总行数与盘面长期偏离，从"偶发
   整批失败"变成"永久静默少数据"。
5. **诱饵**：`--write-batch-size` 旋钮；`scripts/backfill-market-cap.py` 的整行回写
   （正确协议，不许按"更省"的思路改）。

## 6. 隐藏分组（6 个计分组 + 1 个 coherence + p2p）

| 组 | 权重 | 断言点 | 用例数 |
| --- | --- | --- | --- |
| `batching_exit` | 1 | 落盘次数与批大小相称（25 只 / 批 10 → ≤5 次；7 只 / 批 100 → 1 次） | 2 |
| `amplification_exit` | 1 | 累计写盘行数 ≤ 3 × 总行数（两组不同行数/批大小） | 2 |
| `freshness_exit` | 1 | 写入后每交易日行数立刻重算；股票池计数缓存回到未热 | 2 |
| `lock_retry_exit` | 1 | 一次/两次瞬时锁超时必须重试成功；端到端整批不得作废 | 3 |
| `health_exit` | 1 | 外部写入后健康口径可见；刷新在跑时如实报告 | 2 |
| `cross_process_exit` | 1 | 两个真进程写同一分区零丢失（不同股票 / 同一只股票不同交易日） | 2 |
| `coherence` | 2 | 脚本汇总 = 盘上行数 = 写入后统计口径；健康口径 = 盘上真实只数 | 2 |
| `p2p` | 0 | 既有用例白名单（`p2p.json`，301 条），任一条红 → 本轮作废 | — |

跨进程组的确定性来自**文件栅栏**而不是 sleep：第二个写入方在第 2 次取数时落一个
标记文件并阻塞，第一个写入方等这个标记再全程跑完、落 `first-done`，第二个写入方
才继续。谁先读分区、谁后写分区因此是确定的。断言只看"每一方写进去的行都还在"
（行数守恒），不看耗时。

## 7. 脱敏与裁剪

`visible.prune`（9 条，逐条理由）：

| 裁剪项 | 理由 |
| --- | --- |
| `tests/test_warehouse.py::test_market_trade_date_counts_caches_by_window_until_written` | 名字与断言直接点名"写入路径让缓存失效"这条不变量（`second is not first`），是端口 2 的答案路标 |
| `tests/test_realtime.py::test_symbol_count_cache_refresh_and_write_invalidation` | 同上，点名"写入失效"（`cached_symbol_count() is None`） |
| `tests/test_data_operations.py::test_fetch_daily_bars_retries_cache_write_after_lock_timeout` | 直接演示"锁超时后重试成功"，是端口 4 的答案 |
| `tests/test_data_operations.py::test_import_daily_bars_retries_cache_write_after_lock_timeout` | 同上（第二处调用点） |
| `tests/test_data_operations.py::test_fetch_capital_flow_retries_cache_write_after_lock_timeout` | 同上（第三处调用点） |
| `tests/test_data_service_http.py::test_service_health_returns_json_when_warehouse_coverage_fails` | 断言 `/health` 会触发刷新并回报 `coverage_refreshing`，是端口 3 的答案 |
| `tests/test_data_service_http.py::test_service_health_does_not_restart_coverage_refresh_when_snapshot_is_fresh` | 断言 `coverage_refreshing` 的初值与收敛，直接点名健康刷新语义 |
| `tests/test_data_service_http.py::test_service_health_reports_warehouse_market_cap_and_capital_flow_coverage` | 断言"外部写库后 /health 反映覆盖"，正是端口 3 要考的点 |
| `tests/test_data_warehouse_concurrency.py::test_run_full_market_batches_writes_instead_of_per_symbol` | 名字与 docstring 直接写出"修复前写入次数 == 股票只数"这条攒批不变量，是 `batching_exit` 的答案路标 |

以上 9 条同时从 `p2p.json` 剔除（裁剪后不再被收集，留在白名单里等于恒定断裂）。
注入态下没有其它可见用例变红（注入前 7 条基线红全在 `tests/test_scripts.py`，
与本题无关，本就不在 p2p 里）。

`redactions`：

- `AGENTS.md` §9（数据中心与交易日历）—— 直接写着"覆盖表的 missing_rows 只能来自
  刷新后的真实 coverage""/health 不能同步阻塞重型扫描、应用后台刷新更新缺失行数"
  "缺口画像 10 分钟缓存、写入自动失效"，以及"复用任务开始时的完整性快照，避免每个
  写入批次重复扫仓"；§15（质量门禁与架构不变量）—— 不变量 3 点名
  `DataServiceState.start_coverage_refresh()/coverage_snapshot()` 与"禁止跨模块私有访问"。
- `CHANGELOG.md` 1.5.1（数据仓跨进程写锁上线）、1.5.2 / 1.6.0 / 1.6.1（覆盖口径、
  股票池计数缓存 TTL + 写入失效、AI 写路径转向后台刷新）。

## 8. 门禁结果（§5.3，原始输出见 `calibration/gate_*.json`）

| 门禁 | 标准 | 实测 | 结论 |
| --- | --- | --- | --- |
| 锚解 `fixed` | 100/100，目标组全绿，p2p 全绿 | **100.0**；7 个计分组全绿；p2p 破坏 0/301 | 通过 |
| 半成品 `partial` | < 100 | **37.5**（绿：攒批 / 放大 / 跨进程；红：新鲜度 / 锁争用 / 健康 / coherence） | 通过 |
| 注入态 ×20 | 全部 0 分、目标组全红、稳定不 flaky、p2p 零断裂 | 见 `gate_injected_x20.json`：15 条隐藏用例全红，7 个计分组全红，20 次得分恒为 0.0，p2p 破坏 0/301 | 通过 |
| 基线 | p2p 白名单在未注入快照上全绿 | 7 个计分组全绿；p2p 仅 1 条环境相关红（见下），已剔除 | 通过 |

基线那条环境相关的红是 `tests/test_realtime.py::test_scraping_session_ignores_proxy_environment`：
它靠"宿主环境确实配了代理"来证明会话忽略代理，而评分环境强制 `NO_PROXY=*`，
`get_environ_proxies()` 因此返回空字典，该用例在评分环境里**恒红**、与本题无关，
按"快照里基线本来就红的用例也要剔除"处理。

可解性：`fix.patch` 只改 `allowed_paths` 内的 4 个文件（`scripts/run-full-market-import.py`、
`data/warehouse.py`、`service.py`、`data/operations.py`），`forbidden_paths` 零触碰。

## 9. 校准状态

`calibration/results.json` 的 `calibrated` 恒为 `false`，`blind_runs.rows` 为空表，
`summary` 全 `null`——§6.4 硬纪律：出题模型不得给自己出的题做校准。本题目标带
`[0.05, 0.25]`（高级档），需由非出题模型实例跑 ≥5 次"只给第 1 级提示词"的一次作答
后回填。

## 1 级题面纪律修正（2026-10-03）

原第 1 级题面的"验收要求"把**机制与验收不变量清单**直接写进了症状级：六条验收
写着「两个真实进程同时写最终零丢失」「不许出现每写一批就把整段重读/重写的放大效应」
「写完之后的统计口径必须立刻反映新数据」「健康检查必须能看见写入侧真实状况」
「汇总行数必须与盘上真实行数一致」「锁争用不能让整批作废」——把行数守恒、
写盘量线性、写入即失效、口径互恰、健康可见、争用可恢复六条不变量连答案一起给了。

依据 `packs/core/README.md` 附录 A：「第 1 级（症状）：只写用户能观察到的现象、
影响和具体例子；**不列机制、原因、实现边界或验收不变量清单**」。
这处越权会把高级题顶到 `target_band` 上沿——模型不需要自己设计机制，照抄清单即可。

已改写为只陈述用户可观测现象与「对账时看得见的事实」式验收；六条机制不变量仍
完整保留在第 3 级（那是它该在的地方，另含"诱饵勿动"与五条已否决思路）。
改写后该级 677 → 805 字，`packcheck --full` 复跑**红 0 黄 0**，三级字数仍严格递增。

**校准状态不变**：`calibrated=false`、`blind_runs` 保持空表。
本题本轮**未取得**盲测数据（两个模型当日配额均耗尽：hunyuan 重置
2026-10-03 08:00、deepseek 重置 2026-10-04 00:52 UTC+8），故不填造、不外推任何 pass@1。
