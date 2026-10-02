# T2-05 参考解说明（成题版）

> 本文件只进 `reference/`，永不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁全过（见 `calibration/` 的 gate_*.json）。

## 一、注入点清单（6 处：sync.py × 4 + service.py × 2）

| # | 位置 | 注入代码及改动 | 设计意图与症状面 |
|---|---|---|---|
| 1 | `backend/.../data/sync.py` L359 `_admit` | 拆为无锁遍历查重 + 锁内 prune/预算检查/写入；改写 docstring 删去竞态警示 | 制造 TOCTOU 竞态：并发提交相同签名任务时，两线程均未命中快照，分别启动独立 worker，造成重复抓取与数据写冲突 |
| 2 | `sync.py` L349 `_put_locked` | 删除 `running/cancelling` 分支的 `self._last_progress_at[status.job_id] = time.monotonic()` | 心跳丢失：写回进度不再刷新活跃时间，正常运行的长任务被后台巡检误判为超时僵尸并被强制按 failed 回收 |
| 3 | `sync.py` L340 `_drop_locked` | 删除 `self._signatures.pop` 与 `self._cancelled.discard`，保留注释误导为"直接命中" | 终态回收丢标记：终态记录清理后去重签名与取消标记残留在集合中，导致同签名后续任务被幽灵索引绊住、内存泄漏 |
| 4 | `sync.py` L481 `get_job` | 锁内进入后先调用 `now = time.monotonic(); self._prune_locked(now)` 再取任务 | 双重回收窗：每次查询均触发修剪与回收，制造额外回收竞态窗，让轮询查询与准入回收产生非预期的抢先清除 |
| 5 | `backend/.../service.py` L940 `GET /sync/jobs/` | `payload = job.model_dump(mode="json"); payload.pop("admission", None)` 剥除 admission | 消费方接口信息遮蔽：对外隐藏本次准入是否复用了已有在途任务的标记，调用方无法获知复用状态 |
| 6 | `service.py` L1256 `SyncCapacityError` | 409 异常响应体中删除 `"running_jobs": exc.running` 字段 | 消费方接口信息遮蔽：容量超限时只报 409 错误码，不提供当前占用的在途任务清单，前端/调用方无法显示在途详情 |

全部 6 处注入均静默且自然：不改变函数接口参数与类型签名，合成注释读起来自圆其说。

## 二、原生现状与诱饵点边界

1. **诱饵点：`run_full_market` 同步旁路不走 `_admit`**：
   - 仓库原生设计中，`run_full_market` 是阻塞执行的单次同步导入接口，其设计目标就是不排队、不占在途异步 worker 预算。
   - 试图给 `run_full_market` 加锁或塞入 `_admit` 是无用功，甚至可能造成持锁抓取死锁。
2. **原生第二场景：`_append_error` / `_append_failure` 绕过 `_put_locked`**：
   - 仓库原生代码中，`_append_error` 与 `_append_failure` 直接操作 `self._jobs[job_id] = status`，没有调用 `self._put_locked`。
   - 锚解（`fix.patch`）顺手将这两处收口为 `self._put_locked(status)`，保证错误/失败写回同样视为任务进展并刷新心跳。
3. **可见测试裁剪（`visible.prune`，共 6 条）**：
   - 注入态实测变红用例（3条）：
     - `tests/test_data_service_http.py::test_service_reports_reused_sync_admission_and_capacity_conflict`（因 409 剥除 `running_jobs` 变红）
     - `tests/test_sync_jobs.py::test_expired_terminal_job_records_are_pruned_with_their_markers`（因 `_drop_locked` 丢标记变红）
     - `tests/test_sync_jobs.py::test_pruning_protects_running_and_recently_read_jobs`（因 `get_job` 触发 prune 变红）
   - 名字点名答案与实现细节用例（3条）：
     - `tests/test_sync_jobs.py::test_identical_in_flight_sync_is_reused_without_a_second_worker`（用例名直接点名"同参数任务复用且不启第二个 worker"）
     - `tests/test_sync_jobs.py::test_service_level_budget_rejects_extra_jobs_with_running_ids`（用例名点名 running_ids）
     - `tests/test_sync_jobs.py::test_terminal_job_records_are_capped_by_count_within_retention`（点名清理与保留期规则）
   - 裁剪后快照沙箱内可见用例 0 红（全部 35 条 p2p 用例在注入态下全绿）。

## 三、锚解形态

1. **`backend/astock_backtester/data/sync.py`**：
   - 恢复 `_admit` 在单次 `with self._lock:` 内完成 prune、查重、预算检查、写入与签名登记的原子闭环，恢复警示 docstring；
   - 恢复 `_put_locked` 在 `running/cancelling` 状态下刷新 `self._last_progress_at[status.job_id] = time.monotonic()`；
   - 恢复 `_drop_locked` 清理终态记录时同步清除 `self._signatures.pop(job_id, None)` 与 `self._cancelled.discard(job_id)`；
   - 恢复 `get_job` 为轻量读取，移除进入时的 `self._prune_locked(now)`；
   - 顺手将 `_append_error` 与 `_append_failure` 的直接字典赋值改为调用 `self._put_locked(status)`。
2. **`backend/astock_backtester/service.py`**：
   - 恢复 `GET /sync/jobs/{id}` 完整序列化，不剥除 `admission` 字段；
   - 恢复 `SyncCapacityError` 409 处理分支，在返回 JSON 中保留 `"running_jobs": exc.running`。

## 四、陷阱与半成品分析

- **陷阱 A（半成品演示 `partial.patch`）**：只修复后端 `sync.py` 中的准入与心跳（①+②）。
  实测得分 33.33/100：`admission_exit` 与 `heartbeat_exit` 绿；但 `reclaim_exit`（标记未清）、`consumer_exit`（HTTP 出口缺失字段）以及 `coherence`（全周期清理失败）全红。
- **陷阱 B（只改大容量上限）**：将 `max_concurrent_jobs` 改大以避开 409。无法解决并发重复起 worker 与僵尸任务永久累积问题。
- **陷阱 C（调小失活超时）**：将 `RUNNING_JOB_STALE_SECONDS` 改小以掩盖卡死，会导致正常慢速抓取的任务被频繁误杀。
- **陷阱 D（单侧加锁）**：仅给外部入口或 `run_full_market` 加锁，未解决 `_admit` 内部检查与写入的窗口脱节。

## 五、§6.5 反过易检查清单

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"——注入采用自然口径注释重写，无历史 commit 痕迹，AGENTS/CHANGELOG 已脱敏。
- [x] ≥1 个"看似可疑但实际正确"的诱饵点——`run_full_market` 同步旁路（不走准入是正确设计）。
- [x] 每组隐藏测试有第二数据场景——准入组含串行+确定性并发竞态；心跳组含写回续命+查询续命+真正僵尸；回收组含终态标记清理+释放预算再起任务；消费方含 409 与 GET 两接口；coherence 组串联完整生命周期。
- [x] 症状与三级提示词不含任何文件/函数/常量名——提示词严格遵守零名词规范。
- [x] 只修一个端口的半成品必然 <100——`partial.patch` 实测 33.33 分（<100 严格成立）。
- [x] 出题者自评"10 分钟能一次做对"→ 退回重做——**预计 >10 分钟**：需同时厘清准入原子性、心跳写回、终态标记联动与 HTTP 消费方契约四个维度。

## 六、门禁自验结果（§5.3，packgate 实测 2026-09-30）

| 门禁 | 结果 |
|---|---|
| 锚解（`fix.patch`） | **100.0**，5 组全绿，p2p 35/35 绿 |
| 半成品（`partial.patch`） | **33.33**（<100 严格成立，p2p 35/35 绿） |
| 注入态（`injected`） | **0.0**，5 组全红，p2p 35/35 绿 |
| 注入态 ×20（`injected --repeat 20`） | 得分稳定 **0.0**，零 flaky |
| 参考解路径合规 | 仅修改 `allowed_paths` 内文件，未触碰 `forbidden_paths` |
| 沙箱可见红测试 | **0**（6 条变红/点名用例已全部裁剪入 `visible.prune`） |

## 六·补、2026-10-02 体检修复记录（两道 packcheck 红清零）

本轮体检（runs/audit/2026-10-02）发现两道红并修复：

1. **medium 档要求 ≥3 个可改文件，本题只有 2 个**（sync.py + service.py）→
   `allowed_paths` 增加 `backend/astock_backtester/data/operations.py` 作为
   **记录在案的诱饵文件**：症状"同一批数据反复起 worker"的自然嫌疑犯就是
   缺口/覆盖口径（它决定哪些票被反复判成"没补齐"），而该文件在注入态是正确
   的——改它不会让任何组转绿。注入面与锚解不变（仍只落 sync.py + service.py），
   与 T1-02 的 operations.py / App.tsx 诱饵先例同款。
2. **coherence 组只有 1 条断言**（§6.5 每组需第二数据场景）→ 新增
   `test_cancel_then_prune_then_resubmit_starts_fresh`：取消 → 终态清理 →
   同签名重提，与既有"完成路径"全周期用例互补；注入态下幽灵签名/取消标记
   断言必红，锚解下绿，半成品（未修 `_drop_locked` 标记清理）下仍红。

修复后门禁重跑（packgate，2026-10-02，结果已写回 `calibration/`）：

| 门禁 | 结果 |
|---|---|
| fixed | **100.0**，5 组全绿（coherence 2/2 用例），p2p 35/35 绿 |
| partial | **33.33**（不变），reclaim / consumer / coherence 三组红 |
| injected ×20 | **稳定 0.0**（20/20），5 组全红，p2p 0 破坏 |
| packcheck | **0 红 0 黄** → 由 regenerate_index 登记为 active |

## 七、校准状态（§6.4）

`calibration/results.json` 保持空表，`calibrated = false`。出题模型不参与盲测校准。

## 八·补二、2026-10-03 难度审核（盲做 + 复核）：隐藏用例曾把私有命名当成标准

审核人自评（第一段盲做，只读 `prompts/1.md`）：第 1 轮 **100.0 / p2p 零破坏**（一轮即满，
未触发 `prompts/2.md`）。复核后确认这是**偏易**信号，并查实一处题包缺陷与一处覆盖空洞。

### 1. 查实的缺陷：11 条隐藏用例里有 8 条在考私有命名（假阴性）

改用等价的隐藏层跑同一份**行为正确**的实现，判分从 100 掉到 37.5：

- 等价实现 A（只把 11 个私有标识符改名，行为一字未动）：
  `manager._jobs` / `_signatures` / `_cancelled` / `_last_read` / `_last_progress_at` /
  `_finished_at` / `_put_locked` / `_prune_locked` / `_reap_stale_running_locked` /
  `_drop_locked` 全部改名 → **8 条 AttributeError 判红**，仅 3 条 stub 型消费方用例存活。
- 等价实现 B（结构真改写）：五张以 job_id 为键的散表 + 一个独立取消集合，合并成一张
  `_JobState` 状态表（记录本体 + 去重指纹 + 取消标记 + 四个时刻），回收顺序调换，
  心跳求值点合并 → 行为等价。

题面从未要求保留任何私有结构，`prompts/1.md` 也未点名这些名字，而仓库既有测试虽同风格
（`tests/test_sync_jobs.py` 里的 `_store_job` / `_admit_probe` 助手），但那几个助手对应的
用例已被 `visible.prune` 裁掉，**沙箱里没有任何可见用例会因改名而红**。于是"改名"是模型
完全有权做、且不违反任何题面要求的选择，却要付 62.5 分——这是标准的假阴性，不是难度。

### 2. 处置：隐藏用例重写为只考行为（2026-10-03）

改写 `hidden/tests_hidden/test_sync_admission_lifecycle.py`（11 → 14 条），判据纪律：

1. 只走公开面：`start_full_market` / `cancel_job` / `get_job` / HTTP 入口；
2. "起了几个 worker"由 **provider 侧抓取线程计数**观测（测试内把抓取线程池关成 1，
   线程数即 worker 数），不再读管理器内部字典；
3. 并发竞态不再 monkeypatch 私有方法造缝，改用**可控时钟在锁内挂起**：
   第一个读时钟的调用者等所有提交者就位后多留一段真实时间，判定与写入之间的缝被
   确定性撑开（`RUNNING_JOB_STALE_SECONDS` 与 `time.sleep` 都不参与，也不再依赖
   `sync_module.RUNNING_JOB_STALE_SECONDS` 这个常量名）；
4. 失活与保留期用偏移式可控时钟 `advance()` 推进，替代 `monkeypatch` 超时常量 +
   `time.sleep`（原来 0.07/0.1 秒的边界在 Windows 负载下本身就是 flaky 源）；
5. "痕迹跟着记录一起清"改成**属性名无关**的痕迹自省：记录查不到之后，遍历管理器上
   任何容器，凡是还有以该 job_id 为键/元素的痕迹即判红；
6. 用例用了 4 批不同股票（`_BATCH_A/B/C` + 单独票），把"硬编码一批票"和
   "把查重写成不看签名"这类作弊路径堵死。

复验结果（同一份新用例）：等价实现 A **14/14 绿**、等价实现 B **14/14 绿**、
注入态 **8 红**、锚解 **100.0**。假阴性关闭，且判别力没降。

### 3. 顺带补上的覆盖空洞：注入 #4 此前无任何用例判红

`get_job` 里那句注入态的 `_prune_locked`（notes 第一节注入点 #4）**一条用例都测不到**：
唯一能判红的 `test_pruning_protects_running_and_recently_read_jobs` 被裁剪后没进隐藏层。
结果是"查询顺手把任务判死"这种违反题面验收第 3 条（"仍有人在查询的任务不会被误杀"）
的实现可以拿满分——审核人自己第 1 轮就是这么写的，`auditlib grade` 给到 100.0。
新增 `test_reading_a_job_does_not_kill_it` 后该行为被钉死（锚解绿、注入态红），
这也是本次"偏易"结论里最实在的一档提难。

### 4. 一个必须记下来的诚实结论：注入 #2（写回刷新进度戳）在公开行为上不可见

真实 worker 每处理一个 outcome 都先 `sink.snapshot()` → `manager.get_job()`，
而 `get_job` 会刷新读取时刻。于是"最近一次进度推进"始终被"最近一次被读取"覆盖，
删掉 `_put_locked` 里那行进度戳**不改变任何可观测行为**——探针实测：注入态与锚解态
在"无人轮询 + 仍在推进"场景下都会把任务判死（死因其实是 #4 的读时回收）。
旧用例 `test_progress_writeback_prolongs_job_lifecycle` 判红，靠的是直接调用私有
`_put_locked(...)`，测的是"这个私有方法会写这个私有字典"，不是生命周期行为。

取舍：新用例保留同名的行为版断言（题面确实要求"正在推进的任务不被误杀"），但它**不再
判红于 #2**。后果是注入态仍靠其余 5 处拿 0 分（实测 20/20 稳定 0.0，验收线不受影响），
代价是 #2 变成不可判定的诱饵。若日后想让 #2 变得可判定，得先改**被测代码**（例如让
写回路径不经过读路径），而不是加用例——这一点留给下一位出题人。

### 5. 改写后门禁（packgate 实测 2026-10-03）

| 门禁 | 结果 |
|---|---|
| 锚解（`fix.patch`） | **100.0**，5 组全绿（14 条隐藏用例），p2p 35/35 绿 |
| 半成品（`partial.patch`） | **16.67**（<100 严格成立）：只有 `admission_exit` 绿 |
| 注入态 ×20 | **稳定 0.0**（20/20），零 flaky |
| 单点修复实测 | 只修准入原子性 / 只修终态标记 / 只修查询不回收 / 只修两个 HTTP 出口 —— 各 **16.67**（恰好一个组绿）；只修心跳写回 **0.0**；陷阱 B（调大上限）**0.0**；陷阱 C（调小超时）**0.0**；陷阱 D（单侧加锁）**16.67**；硬编码查重 **0.0** |
| packcheck | **红 0 · 黄 0 · 绿 33** |

### 6. 难度方向（只增不减）

审核人实测 `pass@1 = 1.0`，远高于目标带 0.25~0.55。已在隐藏层落地两条提难（不碰
`prompts/`、`meta.json`、`inject/`、`reference/fix.patch`）：注入 #4 的覆盖补齐、
准入组与消费方组各加一条独立数据场景。完整提难方案见审核报告，不在本文件展开。

## 十一、1 级题面纪律修正（2026-10-03）

原第 1 级题面把**机制与验收不变量清单**直接写进了"症状"级：

- T2-05 原文四条验收写着「同一时刻最多只有一个在跑」「不会被系统当成僵尸误杀」
  「留下的痕迹（去重依据、取消标记）也要跟着一起清掉」——把准入互斥、存活口径、
  清理同生共死三条不变量连答案一起给了。
- T3-08 原文三条验收写着「自动把更早的历史**完整地**移进长期记忆区」
  「实际发送的内容必须真的在预算内」「每一次工具调用都必须有对应的结果」——
  把单一口径、发送前硬校验、配对自愈三条机制结论直接交底。

依据 `packs/core/README.md` 附录 A：「第 1 级（症状）：只写用户能观察到的现象、
影响和具体例子；**不列机制、原因、实现边界或验收不变量清单**」。
这两处正是把「要修什么、修成什么样」提前交给模型，会让初级/中级题顶到
`target_band` 上沿。

已改写为只陈述用户可观测现象与「现象消失」式验收（机制不变量仍完整保留在
第 3 级，那是它该在的地方）。改写后该级正文 T2-05 = 622 字、T3-08 = 796 字，
`packcheck --full` 复跑**红 0 黄 0**，三级字数仍严格递增。

**校准状态不变**：`calibrated=false`、`blind_runs.rows=[]` 保持空表。
本题**没有**取得盲测数据（主用模型当日配额耗尽：deepseek 重置 2026-10-04 00:52、
hunyuan 重置 2026-10-03 08:00 UTC+8），因此不填造、不外推任何 pass@1。
