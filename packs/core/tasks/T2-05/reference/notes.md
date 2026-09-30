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

## 七、校准状态（§6.4）

`calibration/results.json` 保持空表，`calibrated = false`。出题模型不参与盲测校准。
