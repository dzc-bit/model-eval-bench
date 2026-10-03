# T3-09 成题报告（status: finalized）

> 本文件只进 `reference/`，永不进沙箱白名单。

## 一、出题意图（§7 第 9 题 · 高级）

真实缺陷模式：**实时快照仲裁全链**——single-flight 的生命周期、迟到 worker 的
发布、代际（generation）仲裁、前端降级重试。§7 指定陷阱："修一处仍有竞态；
前端组独立"。主战场 `tests/test_realtime.py` 原有 150 例，裁剪与 p2p 工作量
在十题中居首。

## 二、注入点清单（四端口落地）

| 端口 | 文件与锚点位置 | 原实现（正确形态） | 注入形态（合成改写） | 故障机理 |
| --- | --- | --- | --- | --- |
| **① single-flight 生命周期** | `backend/astock_backtester/data/realtime.py` (L617) | `future.add_done_callback(lambda _future: self._breadth_in_flight.release())` | 移除 callback，在 `_fetch_live_breadth_with_budget` 的 `finally` 块中立即 `self._breadth_in_flight.release()` | 外层超时退出即提前释放单飞锁，而底层 worker 线程仍在跑；后来的请求以为通道空闲，提交给只有 1 个 worker 的执行器，排在慢请求身后被活活拖死。 |
| **② 迟到 worker 发布** | `backend/astock_backtester/data/realtime.py` (L610, L1252) | worker 使用私有诊断并在超时后丢弃；`_publish_sector_rows` 检查 `_source_chain_cancelled` | worker 直接共享 `diagnostics`；`_publish_sector_rows` 移除取消与截止时间检查 | 超时或被取消的请求，其迟到的诊断与分块行数据绕过检查直接发布到了对外可见状态中。 |
| **③ 代际仲裁退化** | `backend/astock_backtester/data/realtime.py` (L298-306) | `generation` 优先递增比较，严格世代仲裁，遗留无世代调用不得覆盖世代守护快照 | 简化为时间戳比较：`should_update = snapshot.updated_at >= current.updated_at` | 相同时间戳、时钟抖动或无世代迟到响应会直接覆盖最新的新世代快照，引发快照向旧版本回跳。 |
| **④ 前端降级重试缺失** | `frontend/src/marketRefresh.ts` (L46-56) | 部分成功（`missingBreadth`）时返回 `DEGRADED_RETRY_MS` (45s) | 移除部分成功判断，恒返回正常轮询周期间隔（交易期 60s） | 部分字段缺失时页面无法触发 45s 加速重试，降级为傻等；同时旧快照晚到可能回跳成功时间戳。 |

### 前端落点微调说明及理由

草案曾提及 `MarketDashboard.tsx` 与 `DataCenter.tsx`。成题时，将第四端口微调至 `frontend/src/marketRefresh.ts`。
**理由**：
1. `marketRefresh.ts` 是前端实时行情轮询控制、刷新间隔判定与元数据推进的纯逻辑计算中心，被 `useMarketPolling` 全局消费；
2. 其作为纯 TS 函数，无 React DOM 渲染环境依赖，在 vitest 下 0.1s 极速且 100% 确定性运行，绝不引入 UI 层 flaky；
3. 语义与出题草案完全等价（部分成功触发 45s 加速重试，晚到旧响应不得回退成功时间戳）。`meta.json` 的 `allowed_paths` 已同步更新。

## 三、锚解形态与半成品解

- **锚解（`reference/fix.patch`）**：
  1. 后端：恢复 `_remember_successful_snapshot` 的世代递增比较；
  2. 后端：恢复 `future.add_done_callback` 作为单飞锁释放的唯一时机，移除 `finally` 提前释放；
  3. 后端：恢复 `_publish_sector_rows` 取消拦截与私有诊断隔离；
  4. 前端：恢复 45s 降级重试，并在 `nextMarketRefreshMeta` 中守卫 `last_success_at` 的单调递增。
- **半成品解（`reference/partial.patch`）**：
  仅修复 single-flight 释放时机（端口 ①），代际仲裁退化、迟到发布与前端重试均不修。实测得分 28.57 分，演示"修一处仍有竞态"。

## 四、陷阱与诱饵设计

- **陷阱 A（§7 指定）**：只修 single-flight 释放时机 → 迟到发布、代际仲裁与前端降级 3 组仍红，得分仅 28.57 分。
- **陷阱 B**：调大 `timeout` 或各源预算（如将 `breadth_time_budget` 从 8s 调至 15s）"试图消除超时" → 慢源依然会阻塞通道，`chain_budget_exit` 及超时截断不变量必挂。
- **陷阱 C**：在前端单纯根据时间戳丢弃数据 → 治标不治本，后端代际被冲刷后前端根本无从获知世代，服务端仲裁组必红。
- **诱饵点**：
  1. `RealtimeMarketProvider` 的各预算常量（`breadth_time_budget=8.0`, `breadth_source_timeout=2.2` 等）是精心平衡的业务常数，不是 bug；
  2. `_snapshot_from_local_with_budget` 本地兜底路径是确定性同步读取，改成异步会引入更多并发竞争。

## 五、脱敏与裁剪清单

- **脱敏（`redactions`）**：
  1. `AGENTS.md`：第 5 节（实时行情完整性，含预算与 provider 链规则）；
  2. `CHANGELOG.md`：版本 `1.5.1`（实时行情降级重试）与 `1.6.0`（红绿家数链路与预算说明）。
- **裁剪（`visible.prune`，共 21 项）**：
  点名代际仲裁、single-flight、迟到发布以及超时预算的相关用例全部从测试树中剔除，防止模型通过测试名直接抄写答案或定位修复点。
- **白名单（`p2p.json`，共 128 项）**：
  覆盖 `test_realtime.py` 的解析器、日历边界、格式转换与留存字段合并等纯逻辑用例；已剔除受 harness `NO_PROXY=*` 影响的环境断言用例；在注入态与修复态 100% 保持全绿。
- **前端白名单（`p2p-fe.json`，共 3 项）**：
  `src/marketRefresh.test.ts` 的非降级既有用例。

## 六、反过易检查清单（§6.5 逐条核验）

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"（AGENTS.md §5 与 CHANGELOG 历史条目已脱敏）；
- [x] ≥1 个"看似可疑但实际正确"的诱饵点（预算常量群、本地兜底实现）；
- [x] 每组隐藏测试有第二数据场景，硬编码/特判必挂；
- [x] 症状与三级提示词不含任何文件/函数/常量名；
- [x] 只修一个端口的半成品必然 <100 分（实测 28.57 分）；
- [x] 出题者自评：四端口跨语言并发时序机制，远超 10 分钟。

## 七、门禁实测结果（packgate）

- `fixed`（锚解）：**100.0 分**（6/6 组全绿，p2p 0 破坏）
- `partial`（半成品）：**28.57 分**（2 组绿，4 组红，p2p 0 破坏）
- `injected`（注入态 ×20 次）：**0.0 分**（稳定 20/20 全红，p2p 0 破坏，零 flaky）

## 八、基线态事实披露（2026-10-02 补记）

锚解的前端部分不是纯"恢复原样"：`nextMarketRefreshMeta` 的 `last_success_at`
**单调递增守卫在原仓库里不存在**——原始实现对迟到的旧快照会直接用
`snapshot.updated_at` 覆盖成功时点。因此隐藏用例「晚到的旧快照不得覆盖较新的
成功时间戳」在**未注入的基线树上也是红的**（T4-11 组合的 baseline 门禁实测
94.44，唯一红组即 `frontend_degrade_exit`；本题与 T4-11 共用同一份前端隐藏用例）。

2026-10-03 独立 `packgate --state baseline` 复核结果也为 **85.71**，p2p 无破坏，
`frontend_degrade_exit` 仅此一条红：`晚到的旧快照不得覆盖较新的成功时间戳`。
第 1 级已用页面可观察后果说明旧响应会把新行情跳回数秒或数十秒前，并明确要求旧结果
不得覆盖新结果；该基线缺陷可由题面推出，按既有缺陷保留，`calibration/gate_baseline.json`
留存逐用例证据。

这在题目语义上是成立的（与 T1-01 §5.1 的"原生缺陷也算考点"同款设计）：

- 该不变量在三级提示词里被明确告知（第 1 级「更早发出、更晚返回的旧结果绝对不能
  覆盖新结果」、第 3 级不变量第 7 条「成功时点不退」），模型不是在被要求猜一条
  没提过的规则；
- 注入态下该组 3 条用例全红（注入又额外拆了 45 秒降级重试），判别力不丢；
- 但 notes 此前把它写成「恢复」，措辞不准确——它是「恢复降级重试 + 新增单调守卫」
  的混合修复，特在此更正留档。

## 九·补、1 级题面纪律修正（2026-10-03）

第 1 级题面原本列出「最新一次成功优先、单飞不排队、迟到不得写入、降级加速且时点不回跳」四条机制不变量；已改写为只描述页面回退、慢源拖垮整链、降级节奏不符预期三种现象。

依据：`packs/core/README.md` 附录 A「第 1 级（症状）：只写用户能观察到的现象、影响和具体例子；
**不列机制、原因、实现边界或验收不变量清单**」。改写后该级正文 783 字，
`packcheck` 复跑红 0 黄 0，三级字数仍严格递增。

改写前后的盲测数据（同一模型、同一探针、同一沙箱）见 `calibration/results.json` 的
`blind_runs.rows`；两轮均为**原始题面**，可作为「改前」基线。改后题面的 pass@1
**尚未测得**——主用模型当日配额耗尽（HTTP 429，重置 2026-10-04 00:52 UTC+8），
不填造、不推测。

## 十、注入形态提难（2026-10-03，外部审核实测驱动的改造）

### 为什么必须改注入形态

台账与盲测都指向同一个结论：本题**偏易**，而且**不是题面造成的**。
实测（`calibration/results.json` 的 `blind_runs.rows`）：

| 题面版本 | 模型 | 结果 |
| --- | --- | --- |
| 加固前（原始 1 级） | cbcn/deepseek-v4.1-flash | 第 1 轮 100.0 |
| 加固前（原始 1 级） | codebuddy-cn/hy4-preview | 第 1 轮 100.0（77 步）|
| **加固后**（§九 删掉机制与验收清单） | codebuddy-cn/hy4-preview | **第 1 轮 100.0（123 步，6 组全绿）** |

删掉题面里的机制与验收不变量清单之后，模型照样一轮满分——说明**题面不是瓶颈**。
外部审核会话的归因（报告 `D:\tmp\dif-audit\T3-09\report.md`，非入库产物）指出
真正的病根：**注入形态本身留了路标**——注入补丁保留了正确规则的注释（等于把答案
写在注释里），同文件里还存在写对了的 sibling 函数充当现成模板。模型不需要自己
设计机制，只要读代码时注意到"注释说的和代码做的不一样"就能定位。

### 落地了什么

1. **新增两个注入点（审核报告的 P2 / P3）**，都落在 `data/realtime.py`：
   - `_yesterday_sector_snapshot_or_schedule` 的 `release` 回调：改为**仅成功时**复位
     `_yesterday_sector_in_flight`。一次刷新失败后闸门永久关闭，后续恒吃陈旧缓存
     且不报错 → 新组 `background_refresh_exit`。
   - `_fetch_cls_home_payload` 的 waiter 分支：等不到 owner 结果时**回退读陈旧缓存**
     而不是抛错 → 新组 `cls_home_waiter_exit`。
2. **隐藏用例新增 4 条**（`hidden/tests_hidden/test_realtime_arbitration.py`）：
   `test_background_refresh_gate_reopens_after_failed_refresh`、
   `test_background_refresh_in_flight_is_not_restacked`、
   `test_cls_home_waiter_rejects_stale_cache_after_owner_failure`、
   `test_cls_home_waiters_share_one_upstream_request`。
3. **计分组由 5 增至 7**：新增 `background_refresh_exit`（权重 1）与
   `cls_home_waiter_exit`（权重 1），`coherence` 仍是最高权重 2；高级题硬规格
   （≥5 组隐藏不变量）继续满足。
4. **锚解与半成品重做**：`reference/fix.patch`、`reference/partial.patch` 按新注入面
   重写；`partial.patch` 仍是 `fix.patch` 的真子集。

### 四态门禁实测（本轮复跑，逐组核过）

| 门禁 | 分数 | p2p 破坏 | 说明 |
| --- | --- | --- | --- |
| `fixed` | **100.0** | 0 | 7 个计分组全绿 + 前端 `frontend_degrade_exit` 绿 |
| `partial` | **22.22** | 0 | 红 6 组，含两个新组 |
| `injected` ×20 | **每轮都是 0.0**（stable） | 0 | 8 组每轮全红 |
| `baseline` | **88.89** | 0 | 唯一红组是已披露的 `frontend_degrade_exit` |

判别力自检：新加的两组在 `fixed` 下**绿**、在 `injected` 下**红**，证明它们真的
在测新注入的缺陷，不是恒绿或恒红的摆设。

`packcheck --full` 复跑**红 0 黄 1**（黄=「blind_runs 已有 3 行」的提示）。
跨题复用自查：**0 组**（与 T4-11 的去复用改造互不影响）。

### 校准纪律

`calibrated` 恒为 `false`；`blind_runs` 只增真实跑出来的行，不填造、不外推。
本次改造**完成后尚未**对新注入面跑盲测——上面那张表全是门禁判别力数据，
**不是** pass@1。下一轮难度复核应对新题面+新注入面重跑盲测。
