# T4-11 出题存档（王者压轴题）

> 本文只进 `reference/`，永不进入沙箱。题目处于草稿门禁状态前，不得对外宣称已校准或保证所有模型三轮必败。

## 一、出题目标与事实边界

这道组合题把两条真实故障链放在同一评分任务中：实时行情的并发仲裁/迟到发布/降级轮询，
以及数据写入的批次、跨进程互斥、缓存新鲜度与健康刷新。它要求模型在三轮累计修改中同时
恢复两条链路，并满足额外的跨子系统 coherence 组。

**已确认事实**：六个可改文件、13 个 pytest 计分组 + 1 个 Vitest 计分组、283 条 p2p
回归白名单（280 pytest + 3 Vitest）；四态门禁在本题自己的素材上已实测（见 §七、§十）。

**合理推测**：跨进程时序与前后端行为耦合、以及"两条链共用同一个事实源"的联合组，
会显著增加三轮内完整修复难度。

**未验证假设**：强模型连续三轮仍不能修完。确定性评分与公开的三级提示词无法从逻辑上
保证任何能力等级的模型必败；必须用非出题模型的 pass@3 盲测验证。作者不填造校准记录。

目标为非出题模型 `pass_at_3` 落在 `[0.0, 0.02]`。若样本不支持这个目标，应重新设计题目
并重跑盲测，不得把"王者"标签当成证据。

## 二、任务范围与四类耦合不变量

| 边界 | 必须共同满足的行为 | 评分组 |
| --- | --- | --- |
| 实时 worker 生命周期与发布授权 | 成功要交还通道、运行期间不堆叠；已取消/已超时的后台工作不能写共享诊断或行 | `single_flight_exit`、`late_publish_exit` |
| 实时快照顺序与界面探活 | 世代水位只进不退；慢源不占死整链；部分成功降级重试且成功时点单调 | `generation_exit`、`chain_budget_exit`、`coherence_realtime`、vitest `frontend_degrade_exit` |
| 分区并发写入与统计新鲜度 | 互斥内重读、合并、原子替换；跨进程行守恒；写入立刻打穿派生缓存 | `cross_process_exit`、`freshness_exit`、`amplification_exit`、`batching_exit` |
| 写入可见性与跨层对账 | 有界锁重试；健康刷新如实；脚本汇总、盘面、统计和健康口径一致 | `lock_retry_exit`、`health_exit`、`coherence_write`、`coherence` |

联合 `coherence` 组将实时仲裁交错场景与写入后健康覆盖场景放在同一组里：两条链中任一条仍旧失真，不能获得该组权重。完整分数还要求所有分组和两侧 p2p 白名单均绿。

## 三、注入与锚解

本题的注入面、隐藏用例与锚解**全部为 T4-11 自有素材**，与 T3-09 / T3-10 不再有
任何逐字节相同的文件（见 §十 的复用检测结论）。六份注入补丁按唯一顺序编号：

1. `0001-market-snapshot-arbitration.patch`（`realtime.py`，4 处）：
   ① `_remember_successful_snapshot` 两条分支都不再推进 `_last_snapshot_generation`，
   也不做 `model_copy(deep=True)`——世代水位永远停在 0，且留存对象与调用方共享；
   ② 红绿家数单飞通道的成功路径不再交还（`add_done_callback` 被删），只在超时/失败
   分支释放，并且超时后由后台 worker 收尾时把迟到诊断补交回调用方；
   ③ `_publish_sector_rows` 改成"先发布、后判定取消"，已取消的迟到分块行照样流出去。
2. `0002-frontend-refresh-meta.patch`（`marketRefresh.ts`，2 处）：
   ① `refreshIntervalForMarketResult` 删掉部分成功的降级分支；
   ② `nextMarketRefreshMeta` 的 `last_success_at` 改成无条件写 `snapshot.updated_at`。
3. `0003-run-full-market-import.patch`（`run-full-market-import.py`，3 处）：
   ① 新增模块级 `_SESSION_FRAMES` 分区会话 + `_persist_batch`，攒批语义被拆成每票落盘
   且以内存旧快照覆盖分区；② `--write-batch-size` 默认 25 → 1；
   ③ `finish` 事件的 `imported_rows` 改成 `len(symbols)`（汇总口径与落盘行数脱钩）。
4. `0004-warehouse.patch`（`warehouse.py`，1 处）：`write_daily_bars` 末尾不再调
   `invalidate_gap_profile()`，只清缺口画像——两个派生统计缓存按 10 分钟 TTL 自然过期。
5. `0005-service.patch`（`service.py`，1 处）：`_has_fresh_coverage_snapshot` 改成
   "刷新跑过一轮就算新鲜"，不再看 TTL，写入之后的覆盖变化进不了健康口径。
6. `0006-operations.patch`（`operations.py`，1 处）：`_write_with_lock_retry` 不再做
   有界退避重试，瞬时锁超时直接上抛等于整批失败。

`fix.patch` 覆盖全部六个 allowed 文件，把上表六组缺陷逐一还原；前端另加了
`nextLastSuccessAt` 单调守卫（`last_success_at` 只进不退，部分成功与不可用都不推进它）。
它不改变隐藏测试、依赖配置或 checker。
`partial.patch` 只改 `realtime.py` 一个文件（只还原注入 ①②③ 的实时侧），故意不修前端
降级、写入攒批、仓储缓存失效、锁重试与健康刷新，因此是 `fix.patch` 的真子集，应低于满分。

## 四、陷阱与诱饵

- 只修"旧快照回跳"而不处理迟到 worker 的发布授权，`late_publish_exit` 与
  `single_flight_exit` 仍失败：三个缺陷分别落在世代水位、通道交还与发布顺序上。
- 只修实时后端、不修前端降级周期，vitest 组仍失败；反过来只做前端时间戳过滤也无法
  令服务端的世代仲裁与迟到发布组变绿。
- 只把写入批大小调大，显式指定批大小的隐藏场景仍会发现逐票落盘与写放大；而且
  跨进程行守恒与统计缓存新鲜度都没有修复。
- 让 `/health` 同步全量扫描，不能满足"快速返回 + 如实报告扫描在跑"的双重要求。
- `scripts/backfill-market-cap.py` 的按年分区整行写回是诱饵；该路径使用不同的正确
  分区协议，不应被本题顺手改动。
- `_atomic_write_parquet` 的临时文件名是诱饵：观测只看"有没有真的把行写进 parquet"，
  不认临时文件叫什么（README 第 8 条）。

## 五、脱敏、裁剪与白名单

- `visible.prune` 覆盖实时仲裁答案路标（single-flight 生命周期、迟到发布、代际仲裁、
  成功时点）与写入侧答案路标（缓存失效、锁重试、健康刷新、攒批），共 30 条；
  裁剪条目均从 p2p 白名单剔除，与 p2p 无交集。
- Python 侧 p2p 白名单 280 条，Vitest 侧 3 条（沿用实时题的前端既有用例）。
  任一回归红都让该轮记 0 分。改造后四态实测 p2p 破坏均为 0（283/283 全绿）。
- `redactions` 合并原两题对 `AGENTS.md` 与 `CHANGELOG.md` 的脱敏要求。提示词只使用
  业务症状和状态不变量，不写文件、函数、常量名。

## 六、§6.5 清单

- [x] grep/读文档/git log 找不到“该修哪里、改成什么”：两条题源的相关文档段落均列入 redactions。
- [x] 有多个看似可疑但实际正确的诱饵点：分区级整行写入、预算常数、本地兜底实现。
- [x] 每个评分组有第二数据场景；测试使用临时目录、事件栅栏与 stub，不依赖在线数据。
- [x] 症状与三级提示词不包含实现文件名、函数名或常量名。
- [x] 半成品只修部分端口，门禁必须实测 <100；不得通过提示词或静态声称替代门禁。
- [x] 十分钟自评不成立：六个文件、两种语言、并发顺序、发布授权、数据守恒、刷新状态和 p2p 同时受约束。

## 七、门禁与盲测状态

组合包在去复用改造完成后于当前受测快照重跑四态门禁（原始输出见
`calibration/gate_*.json`，2026-10-03）：

| 门禁 | 实测 | 说明 |
| --- | --- | --- |
| `fixed` | **100.0** | 14 组全绿（18/18 权重）；p2p 283/283，破坏 0 |
| `partial` | **33.33** | 只绿实时侧 5 组；前端与写入侧 9 组全红；p2p 破坏 0 |
| `baseline` | **94.44** | 唯一红组是已披露的 `frontend_degrade_exit`；p2p 破坏 0 |
| `injected` ×20 | **稳定 0.0（20/20）** | 14 组每轮全红；p2p 283/283，破坏 0 |

`packcheck --full --task T4-11`（对 draft 强制成品级检查）：**红 0 · 黄 1 · 绿 35**，
黄项即"强制检查 draft"自身的提示。

当前校准纪律：`calibrated=false`，`blind_runs.rows=[]`，汇总值全部为 null。由非出题
模型完成至少 5 组独立三轮轨迹后，按 `pass_at_3` 统计；达到样本量后再判断是否进入
`[0.0, 0.02]` 目标带。没有此证据前，不能保证最强模型三轮无法通过。

## 八、2026-10-03 去复用改造（用户拍板：T4-11 必须是独立题）

**体检发现的问题**：改造前 T4-11 的素材与 T3-09 / T3-10 逐字节相同——6 份注入补丁
全部是那两题的原文，两个 pytest 隐藏用例文件与前端隐藏用例文件也是原文（9 对同
SHA256）。后果：做过 T3-09/T3-10 的模型等于已经见过 T4-11 的注入点与隐藏判据，
本题失去独立测量能力，不配当王者压轴题。

**改造范围**（只动 `packs/core/tasks/T4-11/**`，T3-09/T3-10 一个字节都没改）：

- 注入面全部重写：6 份补丁的目标函数与缺陷机制都与原两题不同（见 §三 的逐条表），
  只保留"用户可观测症状"这一层叙事。
- 隐藏用例全部重写：`test_market_authority.py`（行情侧 10 条）、
  `test_write_authority.py`（写入侧 15 条）、`marketAuthority.hidden.test.ts`
  （前端 7 条，其中 4 条计分），`test_cross_chain_coherence.py` 保留（原本就是本题独有）。
- `groups.json` / `groups_fe.json` 的 node id 全部换成新用例；组 id、权重、
  `coherence` 最高权重（3）与 king 档规格（6 文件 / 14 计分组）保持不变。
- `fix.patch` / `partial.patch` 按新注入面重做：`partial.patch` 只改 `realtime.py`
  一个文件，是 `fix.patch` 六个文件的真子集（`check_paths` 的严格子集判据成立）。

**未改动**（硬约束）：`meta.json` 的 `tier/attempts/status/target_band/target_metric`
与 `visible.prune`、`p2p.json`、`p2p-fe.json`、`redactions` 全部原样；
`calibration/results.json` 的 `blind_runs` 仍为空表、`calibrated` 恒为 `false`。

## 九、2026-10-02 联合组重做（用户拍板方案 b：补真正跨链的交互不变量）

体检指出旧联合 `coherence` 组只是"两条链各抽一条既有用例"的合取记账，
不断言两条链之间的任何相互作用。已按方案 (b) 重做：

- 新增 `hidden/tests_hidden/test_cross_chain_coherence.py`，两条联合用例
  断言**真实相互作用**：
  1. `test_write_invalidates_the_pool_the_realtime_guard_checks_against`——
     写入侧的股票池失效协议是行情侧宽度完整性校验的事实来源：写入第 5 只股票后，
     行情兜底快照不得再引用写前的旧池子（4）做校验（要么诚实报"未热"、要么已重建为 5），
     落定后必须按写后事实校验。失效协议失灵时，行情侧拿旧池子校验新数据且两边都不报错。
  2. `test_external_write_reaches_health_and_market_exits_with_the_same_fact`——
     同一次外部程序写入（不经 HTTP、不经服务进程内的仓库句柄）必须同时穿透
     健康覆盖出口与行情兜底出口：健康口径报的只数与最新交易日，
     必须等于行情兜底快照引用的最新交易日。
- `groups.json` 的联合 `coherence` 组（权重 3，仍为全表最高）改为引用这两条新用例，
  不再复用两条链各自的既有用例。
- 这两条用例是本题自有素材，去复用改造后**原样保留**（§八）。

## 十、复用检测结论（2026-10-03）

用逐文件 SHA256 对全库 `inject/**`、`hidden/**`、`hidden-fe/**` 下所有
`.patch/.py/.ts/.tsx` 做交叉比对，结果：

- **跨题逐字节相同的素材：0 组**（改造前是 9 组）。
- T4-11 名下 12 个素材文件**全部「独有」**：
  `test_cross_chain_coherence.py`、`test_market_authority.py`、
  `test_write_authority.py`、`marketAuthority.hidden.test.ts`、
  `inject/patches/0001..0006`、`reference/fix.patch`、`reference/partial.patch`。

复现命令（脚本在 `D:\tmp\reuse-check.py`，非仓库文件）：对题包根做 sha256 分组，
同一 sha 出现在多个题名下即判复用。
