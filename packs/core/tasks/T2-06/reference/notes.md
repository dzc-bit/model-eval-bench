# T2-06 参考解说明（成题版）

> 本文件只进 `reference/`，永不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁全过（见 `calibration/` 的 gate_*.json，packgate 实测 2026-09-30）。

## 一、注入端口（4 处，落盘 3 文件）

| # | 位置（原文行号） | 注入内容 | 症状面 |
| --- | --- | --- | --- |
| ① | `data/filelock.py:94`（`acquire`）+ 模块 docstring | 互斥退化成"哨兵存在性检查"：新增 `_claim_sentinel`（文件可打开且为空即视为拿到），`_try_lock_file` 被替换；模块 docstring 同步改写成"哨兵协调器"口径 | 两个进程同时进入临界区 → 夜间并发写丢更新 |
| ② | `data/warehouse.py:199`（`_atomic_write_parquet`）+ `:181`（写路径注释） | 去原子替换：删掉临时文件 + `os.replace`，直接 `to_parquet(path)`；docstring 改写成"哨兵已协调、不留中间产物"的口径 | 读者撞上"截断后未写完"窗口 → 半截分区 |
| ③ | `data/warehouse.py:1330`（`_safe_read_parquet`） | 损坏静默：登记 `_note_corrupt_partition` 后**返回空表**（原为 re-raise）；docstring 改写成"按分区隔离、不让单个坏分区拖死读者"（模仿 `cache.py` 读侧的降级口径） | 坏分区被当成"还没采集" → 补数空转、覆盖数清零又自愈 |
| ④ | `service.py:781`（`/diagnostics/data-gaps`） | 健康口径常量：`warehouse_health` 拿掉 `corrupt_partitions` 明细，成功分支恒报 `healthy: True`，失败分支只报 `healthy: False`；注释改写成"画像已覆盖缺口分布，健康口径不掺分区细节" | 前端永远看不到"哪个分区坏了" |

四个端口都静默：不改函数签名、不抛错、不产生日志差异。注入与参考解均为
**合成改写**（与历史修复 diff 的相似度远低于 0.6）：历史修复是"新增跨进程锁 /
新增原子替换 / 新增损坏登记与暴露"，注入方向相反（拆除既有防线），且每处
注释与 docstring 都改写成自洽的"另一种设计口径"，任何历史提交里都不会出现。

## 二、原生（非注入）事实，成题时的取舍

- `DEFAULT_LOCK_TIMEOUT_SECONDS = 120`（filelock.py:28）与
  `_corrupt_partitions_lock`（warehouse.py:75）：**都是正确设计**，是本题的
  两个诱饵点（见"四、陷阱"）。快照原样保留。
- `service.py:339` `_read_coverage_snapshot` 的损坏日志回退路径原样保留：
  注入态下 coverage 路径（`pq.ParquetFile` 直开，只豁免 FileNotFoundError）仍会
  把坏分区抛出来、走到"数据仓分区损坏"的 error 日志——这是**有意**的：
  它让症状里"一查就报文件损坏"与"有些损坏不报错"两种表现并存，正是
  "口径分叉"的现场，而不是题面失真。
- `cache.py` 读侧的"失败降级为空表 + 记日志"是**正确**模式（缓存可回源），
  保留；第 2 级提示词明确点出"把这套模式搬进数据仓"是错的，防止模型照抄。
- `test_no_silent_swallow.py`（静默吞异常白名单闸）：注入态实测**仍绿**——
  端口③的形态是"登记后 `return`"，不匹配守卫的 `except …: pass` 正则。
  它没有进 `visible.prune`（沙箱里保留），理由：它守的是"白名单"机制本身
  而非本题任何端口，注入既没让它红，留着也不点名答案。
- `market_trade_date_counts` 在注入态仍绿（坏分区登记 + 跳过、计数补 0，
  与原行为一致）——但其名字与 docstring 点名"corrupt_partitions 登记语义"，
  按 L2 纪律进 `visible.prune`（T1-02 对同名用例同样裁剪）。

## 三、锚解的形态：四道防线逐一接回

`reference/fix.patch`（实测 100.0 / 100）：

1. `filelock.py`：`acquire` 回到 `_try_lock_file`（msvcrt/fcntl 字节锁），
   删除 `_claim_sentinel`，模块 docstring 回到"跨进程互斥"口径；
2. `warehouse.py` 写路径：`_atomic_write_parquet` 回到"同目录临时文件 +
   `os.replace` + 失败清理"，写路径注释回到"跨进程互斥"口径；
3. `warehouse.py` 读侧：`_safe_read_parquet` 回到"FileNotFoundError = 空表、
   其他异常登记后**原样 re-raise**"；
4. `service.py`：`warehouse_health` 回到由 `corrupt_partitions` 驱动
   （成功与失败两个分支都带明细，`healthy = not corrupt`）。

锚解没有引入任何新机制——四道防线在仓库里本来就有，注入把它们拆掉了。
中级题的难度不在"设计新协议"，而在：①从症状反推**四道防线**都要查
（一处修好 ≠ 症状消失）；②损坏语义要同时接回**读、登记、健康**三个出口
并保持互恰（coherence 组）；③不能把"读侧容忍"错误推广成"读侧吞掉"。

## 四、陷阱

- **陷阱 A（半成品演示，§7 指定）**：只修写路径（锁 + 原子替换）。
  `partial.patch` 实测 **33.33**——`exclusive_write_exit`、`atomic_replace_exit`
  绿；`corrupt_visibility_exit`、`health_exit` 红，`coherence`（权重 2）红：
  读侧"岁月静好"返回空表而登记里有记录（或反之），三口径自相矛盾。
  说明：33.33 低于任务书"预期 40~60"的软区间；权重按设计文档 §5.1/
  附录 B（四端口各 1、coherence 2）不可再调，与 T2-04 的 partial 同分，
  门禁硬条件（< 100 且 p2p 0 失败）满足，偏差记录在案。
- **陷阱 B**："损坏时抛错会把查询炸掉，所以返回空表才稳"——
  `coherence` 组的对照断言（好分区行数必须原样数出来）与
  `corrupt_visibility_exit` 一起红：正确解是**登记 + 原样抛 + 按分区隔离**，
  让上层（如 `market_trade_date_counts` 的跳过、service 的回退日志）各自
  决定降级，而不是读侧替所有人吞掉。
- **陷阱 C**：照抄 `cache.py` 读侧的降级模式（记日志 + 空表）。缓存有
  "回源重抓"兜底，数据仓分区没有——空表把坏分区永久伪装成"没采集"，
  补数循环每轮空转且无报错。第 2 级提示词已把这条思路点名否决。
- **诱饵 1**：`DEFAULT_LOCK_TIMEOUT_SECONDS = 120` 看似"太长"（"改成 1 秒
  就好了"）——超时抛错是互斥成立的证据，改小只会让大分区正常写入更频繁
  被误判超时，互斥本身分毫未动。注入刻意让模块 docstring 提一句
  "默认等待时长保持 120 秒不变"，给这个诱饵加戏。
- **诱饵 2**：`_corrupt_partitions_lock` 看似多余（"单进程哪来的锁"）——
  它保护的是多线程登记（threading 级），删掉会在并发读下丢登记。锚解保留。
- **诱饵 3**：`service.py:339` 的 coverage 回退日志（含"这不是数据缺失"字样）
  看上去正是"该修的地方"——它是对的，注入没碰它。

## 五、visible.prune（5 条）与理由

| 条目 | 理由 |
| --- | --- |
| `tests/test_data_warehouse_concurrency.py`（整文件） | 13 条用例全部是本题四端口的守卫（文件名与模块 docstring 直接点名）；注入态实测 6 红（filelock 阻塞 / 双实例并发丢更新 / 原子替换可见性 / 损坏登记 / 健康暴露 ×2），其余 7 条绿的同样名字点名（filelock 重入/哨兵、atomic_write 临时文件、diagnostics health）。整文件裁剪的工程理由：harness `snapshot.apply_prune` 对同一文件逐条改写后，`guess_text` 以 `raw[:4096]` 截断判文本，截断点落在多字节 UTF-8 字符中间会把纯文本误判为二进制、**静默跳过后续条目**（本机实测复现：第 3 条起失效）；整文件删除不走该路径，行为确定。另：其中 run_full_market 攒批用例属 T3-10 地盘，一并移出本题沙箱（见"九、分工边界"）。 |
| `test_warehouse_safe_read_parquet_only_treats_missing_files_as_empty` | 注入态实测变红；名字点名"missing = empty、其余 re-raise"语义 |
| `test_warehouse_surfaces_corrupt_recent_partition_for_latest_and_coverage` | 注入态实测变红；名字点名损坏暴露 |
| `test_warehouse_does_not_overwrite_corrupt_partition_when_new_rows_arrive` | 注入态实测变红；名字点名"坏分区不被覆盖" |
| `test_market_trade_date_counts_skips_corrupt_partition` | 注入态仍绿，但名字与 docstring 点名"坏分区跳过并登记"；按 L2 纪律裁剪（T1-02 同名同判） |

注入态探针实测：裁剪后沙箱内 `tests/test_warehouse.py` 与
`tests/test_no_silent_swallow.py` **0 个可见红测试**（§6.5）。

## 六、p2p 白名单（50 条）

- 候选来源：`tests/test_warehouse.py`（基线实测 54/54 全绿，裁掉 4 条守卫后
  收 50 条）。`tests/test_data_warehouse_concurrency.py` 因整文件裁剪不提供候选。
- 快照里本就红的用例：无（两文件在受测仓库基线均全绿）。
- p2p 在三态下全部 0 失败（fixed 100 态 50/50 绿；partial / injected 同样 0 破坏）。

## 七、§6.5 反过易检查清单（逐条打勾）

- [x] **grep / 读文档 / git log 找不到"该修哪里、改成什么"。**
  四处注入的注释与 docstring 全部改写成自洽的"另一种设计口径"（哨兵协调器 /
  不留中间产物 / 按分区隔离 / 健康口径不掺细节），无一处残留"原子替换 /
  互斥 / re-raise / 损坏明细"字样；AGENTS.md 与 CHANGELOG.md 不进快照
  （`redactions` 为存档记录：§9 与 1.5.2/1.6.0/1.6.1，与 T2-04 同款）。
- [x] **≥1 个"看似可疑但实际正确"的诱饵点。** 三个：120 秒超时、
  `_corrupt_partitions_lock`、service 的 coverage 回退日志。
- [x] **每组隐藏测试有第二数据场景，硬编码 / 特判必挂。**
  - `exclusive_write_exit`：纯锁语义（子进程持锁 + 超时探测）+ 数据仓级
    （外部脚本持锁写 8 行 → 数据仓写必须排队，并集 16 行）；
  - `atomic_replace_exit`：读到一半的观察（半截现场必须不在"整旧/整新"里）
    + 写失败原子性（旧分区字节级不变 + 无 tmp 残留）；
  - `corrupt_visibility_exit`：好坏分区混合 + 只有好分区缺失的对照 +
    "不存在的分区 = 空表、不误报损坏"的反向对照；
  - `health_exit`：有坏分区（healthy=False + 明细）+ 干净仓对照；
  - `coherence`：坏年三口径互恰（读失败 ⟺ 登记 ⟺ 健康）×2 场景 +
    干净年对照（防止"全报损坏"的过度纠正）。
- [x] **症状与三级提示词不含任何文件 / 函数 / 常量名。** 通篇只有
  "数据仓 / 分区 / 哨兵文件 / 临时文件 / 损坏 / 健康口径 / 补数"等业务词。
- [x] **只修一个端口的半成品必然 < 100 分。** `partial.patch` 实测 33.33。
- [x] **出题者自评"10 分钟能一次做对"→ 退回重做。** 自评**远超 10 分钟**：
  四处注入分属三个文件、两种层次（进程互斥 / 文件原子性 / 异常语义 /
  HTTP 口径）；症状"覆盖数清零又自愈"需要推出"读侧吞掉 + 写侧覆盖坏文件"
  的完整回路；修齐还要同时满足"损坏必须响、缺失必须静默、健康必须带明细、
  好分区不能被连带吞掉"四条互相牵制的约束。

## 八、门禁自验结果（§5.3，packgate 实测 2026-09-30）

| 门禁 | 结果 |
| --- | --- |
| 锚解（`fix.patch`） | **100.0**，五组全绿，p2p 50/50 绿 |
| 半成品（`partial.patch`） | **33.33**（< 100 成立，p2p 0 失败） |
| 注入态 | 0.0，五组全红（组内对照用例按设计保持绿，组整体红） |
| 注入态 ×20 | 得分稳定 0.0（score_min = score_max = 0.0，stable），无 flaky |
| 参考解不触碰 forbidden_paths | 通过（filelock.py / warehouse.py / service.py，均在 allowed_paths） |
| 沙箱可见红测试 | 0（探针实测：test_warehouse.py 与 test_no_silent_swallow.py 注入态全绿） |

进程级用例的确定性：子进程 stdout 做 HELD/RELEASED 消息握手 + `writer-go`
放行信号文件（沿用 `test_data_warehouse_concurrency.py:77` 的既有手法），
不依赖 sleep 时序、不碰网络；×20 全红证明无 flaky。

## 九、与 T3-10 的分工边界

两题同属"写仓"主题，注入点**零重叠**：

- **T2-06（本题）**：单机多进程写与损坏可见性——filelock 互斥、warehouse
  原子替换与损坏读侧、service 健康暴露。改动面在 `warehouse.py` /
  `filelock.py` / `service.py` 的**写入与读取语义**。
- **T3-10**：脚本侧攒批与服务健康全链——`scripts/**` 侧的攒批落盘、
  进程级写仓与服务健康全链。`scripts/**` 因而在本题进 `forbidden_paths`
  （防改错方向，也是 T3-10 的地盘）；`test_data_warehouse_concurrency.py`
  里的 run_full_market 攒批用例随整文件裁剪移出本题沙箱，避免双题共守
  同一批可见用例。
- `data/sync.py` 同样在 `forbidden_paths`：名字与"同步写入"相近，但本题
  症状不在同步任务语义上（那是 T2-05 的地盘），防止模型改错方向。

## 十、校准状态（§6.4）

`calibration/results.json` 为空表，`calibration.calibrated = false`。
出题模型不做盲测校准（硬纪律）；盲测由非出题模型实例完成。

## 十一、2026-10-03 难度审核（非出题模型盲做 + 出题侧复核）

盲做成绩：**第 1 轮 33.33**（p2p 0 破坏）→ 读第 2 级提示词后 **第 2 轮 100.0**
（p2p 0/50 破坏）。pass@1 = 0.00，最好成绩 100。四组隐藏用例的红绿归因、
等价实现复验与本次改动记录如下。

### 11.1 复验：2026-10-02 那次假阴性已根除，但同族残留仍在（已修）

用四种"行为正确、写法不同"的等价实现跑真实隐藏用例（`--state custom`）：

| 变体 | 落盘写法 | 修前 | 修后 |
| --- | --- | --- | --- |
| V1 | `tmp = path.with_suffix(".tmp")` 固定名（本仓库 5 处代码的写法） | **100** | 100 |
| V2 | 临时文件放同卷的另一目录 `.staging/`，固定名 | **100** | 100 |
| V3 | 临时名带 pid+随机后缀，先清残留、fsync 后才 replace | **100** | 100 |
| V4 | 临时文件仍同目录，但 `to_parquet` 收**文件对象**而不是路径 | **83.33 红** | **100** |

结论：题面点名的三自由度（**临时文件名 / 存放目录 / 调用顺序**）现在真的自由
了，V1–V3 全绿，"只改成 `where != target`" 那次修复是有效的。

但 V4 暴露了**同族残留**：`hidden/tests_hidden/test_warehouse_corruption.py`
两处打桩写的是 `where = Path(str(path))`。`DataFrame.to_parquet` 的 `path`
文档上明确允许"str、路径对象**或文件对象**"，而文件对象上 `str(path)` 得到的是
`<_io.BufferedWriter name=...>` 这种字符串，与目标路径永不相等——于是打桩把一次
**暂存写**误判成直写目标，再对同一个已打开的流做两次写入（半截 + 完整），
两个 parquet 首尾相接，读出来报 `Column cannot have more than one dictionary`。
被测代码的目标文件在这两次调用里一个字节都没被动过，是桩自己把暂存区写坏了。

已修（只动 `hidden/`，**没有改任何断言、没有降权重、没有删用例**）：

1. 新增 `_written_path()`：把 `path` 实参解析成可比对的路径；解析不出路径
   （文件对象、整数 fd）时返回 `None`，按"不是目标本身"处理，即按暂存写处理——
   与真实行为一致。
2. 新增 `_reset_destination()`：两次 `to_parquet` 之间对流做
   flush + 截断 + 归位，把桩的模拟还原成"两次独立调用"。对路径入参是空操作
   （`to_parquet` 拿到路径本来就以 `'wb'` 重新打开、先截断）。

判别力没有因此下降：`--state injected` 连跑 **5 次恒为 0.0**、`--state fixed`
仍 100.0、`--state partial` 仍 33.33，三态门禁硬条件全部保持（§八）。

### 11.2 载荷契约未定义：`warehouse_health.corrupt_partitions` 的形态

第 1 轮我把明细下发成 `[{path, error}, ...]`，被判红。查下来：
用例写的是 `for path in corrupt` + `or {}`，所以**只接受 `list[str]` 或
`{path: error}` 映射**，不接受 `list[dict]`——三者都是"哪个分区、什么错误"的
正当编码，两级提示词都没规定形状。这条按"契约未定义"记在案，**没有改用例**
（`reference/fix.patch` 用的是 `{路径: 错误}` 映射，`ai/tools/local_tools.py`
用的是 `list[str]`，两种都能过；仓内既有口径本身就不唯一，题面无从推出唯一答案）。

### 11.3 反判别力实测（供后续提难参考）

| 修法 | 得分 | 红组 |
| --- | --- | --- |
| `partial.patch`（只修写路径） | 33.33 | corrupt_visibility / health / coherence |
| 只抛不登记（读侧与登记分叉） | 33.33 | 同上 |
| 修好三个出口但**完全不碰 `service.py`** | **50.0** | health / coherence |
| 全修，但健康口径下发**硬编码的假路径**（猜 year=2024/2025/2026） | **100.0** | — |

最后一行是个真洞：§七 打的"硬编码/特判必挂"对 `health_exit` 不成立。
`health_exit` 只有一个损坏场景（2026），`coherence` 的三口径用例也只有 2025，
而两条断言都只检查"明细里出现过 `year=YYYY`"，于是猜年份就能满分。
建议（未实施，等拍板）：把两条断言从"包含该年份"收紧成"上报集合 == 登记集合"，
用形态无关的归一化（`dict` 取键 / `list[str]` 直接用 / `list[dict]` 取 `path`），
断言强度上升、难度只增不减，改完重跑三态门禁即可。

### 11.4 一处设计口径观察（与参考解一致，不改）

第 1 轮我给 `_compute_gap_profile` 加了"坏分区按分区跳过"，让画像继续算其余年份。
复核发现这段**基本是死代码**：`_compute_gap_profile` 在调 `_safe_read_parquet`
之前先用 `pq.ParquetFile(path).schema_arrow.names` 探 schema，而那里只豁免
`FileNotFoundError`，坏分区在这一步就抛了。加上 `ai/tools/local_tools.py::data_health_report`
本就以"`data_gap_profile()` 抛异常"为前提产出 `warehouse_corrupt` 与"先修损坏"的
hint（§二 陷阱 C 的反面），定稿版已去掉这段隔离，与 `reference/fix.patch` 口径一致。
