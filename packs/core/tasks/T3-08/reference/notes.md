# T3-08 参考解说明（成题版）

> 本文件只进 `reference/`，永不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁全过（原始输出见 `calibration/gate_*.json`）。

## 一、注入端口（3 处，落盘 1 文件：`backend/astock_backtester/ai/agent.py`）

| # | 端口 | 注入位置（注入版行号 / 原始行号） | 注入内容 | 症状面 |
| --- | --- | --- | --- | --- |
| ① | 归档丢失 | `_archive_overflow` 归档登记循环（注入版 :530-539 / 原 :536-541） | 调用侧只登记工具名（"调用可按原参数重发拿回"）；**tool 结果消息整条跳过、不进 pending_archive** | 长工具轮后失忆：窗口被挤爆时移出的查询结果在长期记忆层找不回 |
| ② | 字符预算空转 | `_archive_overflow` 字符超限分支（注入版 :506-512 / 原 :511-519） | 统计口径沿用"只数正文"（现状原样保留）；超限时**只搬运"不计入统计"的空壳条目**（无正文消息），带正文的条目一律不动 | "整理在跑、体积不变"：日志有归档动作，账面体积一动不动 |
| ③ | 孤儿 tool 头 | 1) `run()` 删除修复前置（原 :110-112 整块移除）；2) 归档边界回退（注入版 :514-519 / 原 :522-531） | 修复阶段不再先于归档运行；找不到 user 边界时**按统计切点硬切**（`next(..., overflow)`），可能把配对从中间切开 | 切出的孤儿 tool 结果无人认领；中断留下的悬空调用无人补齐——会话每轮必败 |

三个端口互相牵制（§7 指定结构"修预算必切断配对、修孤儿必使预算空转"）：

- ②把口径修齐后归档变多变快，落刀点全看 ③的边界语义——保留回退硬切就一定切出孤儿；
- ③只把"修复"加回来，孤儿会被 `_repair_interrupted_turn` 整条丢弃（而不是补齐），
  内容从窗口和归档里同时消失（①的丢失面扩大），预算照样空转；
- ①只把结果塞回归档，②的空转与 ③的硬切原样保留——`archive_integrity_exit` 之外的
  组依然全红。

三个端口全部静默：不改签名、不抛错、无新增告警；注释同步改写成自洽口径
（"结果可按 call_id 取回""零成本清理""超限窗口不能放着不管"），与历史修复 diff 相似度 < 0.6。

## 二、注入 vs 现状的边界（§6.5 风险项，锚解必须两处一起修）

**注入 2 利用了仓库现状里一个真实缺陷**：`_archive_overflow` 的字符统计只累加
`content`（原 :511），而 `ContextBudget.count_tokens`（context.py :231）会把
`tool_calls` 的 `arguments` 计入——同一会话两个出口各执一词。这是**现状就有的**，
不是注入制造的。

因此锚解必须同时覆盖：

1. **注入的**：空转分支（只挑不计入统计的体积）、结果侧丢弃、边界回退硬切、
   run() 修复前置被删——共四处改动；
2. **现状就有的**：归档统计口径只数正文。只修注入不修口径，`budget_effective_exit`
   的"参数体不进统计"场景（`test_budget_counts_tool_call_arguments`）在干净仓库上
   依然是红的（该测试在未注入的基线上就是红，实测确认）。

边界后果：把 `_archive_overflow` 原样恢复成"原版正确行为"**不是**锚解——
它只修好注入，修不好现状口径，固定拿不到 100 分。

## 三、锚解的形态：统一口径 + 安全切点 + 完整搬运 + 修复前置（设计新机制，§6.3 高级）

`reference/fix.patch`（仅 agent.py，+58/-39 行）：

1. **统一口径**：新增 `_message_wire_chars(message)` 静态方法——正文 + `tool_calls`
   参数体一起计（与 `count_tokens` 同口径的字符版）；`_archive_overflow` 的总量统计
   与逐条累计全部改走它。
2. **安全切点**：字符超限时从最旧处按同一口径累计、直到剩余体积回到阈值内；归档
   切点仍取"第一个 user 消息"（轮次边界，窗口头必为 user，配对天然完整）；
   **找不到 user 边界就归档 0 条并留 warning**（恢复原版的安全回退，宁可一轮超预算）。
3. **完整搬运**：归档登记恢复为"每一条都进 pending_archive"——结果侧正文原文保留，
   调用侧附工具名；`ARCHIVE_MAX_ENTRIES` 条数兜底与"已丢弃 N 条"占位原样保留。
4. **修复前置**：`run()` 恢复 `_repair_interrupted_turn(session)` 先于归档与首次
   模型调用——悬空调用先补齐"已中断"结果，归档的切点计算看到的才是完整配对。
5. `_consolidate_archive`（攒批阈值、失败保留、成功才清空）**一字不动**——它是正确设计。

## 四、陷阱与诱饵

- **陷阱 A（半成品演示）**：`partial.patch` = 只修端口②（口径 + 空转）。
  实测 **14.29** 分——只有 `budget_effective_exit` 绿，其余五组全红，
  p2p 0 破坏（牵制结构的直接证明）。
  另两个单端口形态亦实测 <100：只修端口①（恢复结果侧登记，空转/硬切/删修复保留）
  = **57.14**（4/7，archive_integrity/pairing/budget 三组仍红）；只修端口③
  （恢复修复前置 + 安全回退，空转/丢弃/口径保留）= **14.29**（1/7，仅 pairing 绿）。
- **陷阱 B**：修预算时按"最后一对之后"切 → 切点永远找不到（配对消息把窗口顶满），
  空转原样保留；必须先修复配对、再按完整轮归档。
- **陷阱 C**：修孤儿时把孤儿结果整条删除 → 配对更破、内容丢失（`no_data_loss_exit` 红）；
  修复必须"补结果或补标记"，且发生在归档之前。
- **陷阱 D**：把 `SHORT_TERM_MAX_CHARS` / `SHORT_TERM_WINDOW` 调大 → 表面缓解，
  第二数据场景（更长会话、参数体膨胀会话）照样红。
- **诱饵点**（看似可疑、实际正确，不许真改坏）：
  - `SHORT_TERM_WINDOW = 24` 与 `ARCHIVE_MAX_ENTRIES = 400`——两个"调大就好"的旋钮；
  - `_consolidate_archive` 的失败保留 + 条数兜底占位——`no_data_loss_exit` 有专测看守
    （`test_compaction_failure_keeps_archive_and_cap_leaves_placeholder`）；
  - `memory.recall` 的严格预算（整行累加、超限即停）——`recall_reference_exit` 用作
    对照锚，防止"把所有预算都改成永不截断"的一刀切。

## 五、§6.5 反过易检查清单

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"——注入改写注释自洽；AGENTS.md
  §15/§16 与 CHANGELOG 1.6.1/1.4.0（点名分层记忆/归档孤儿的段落）进 redactions，
  不进快照（白名单本就不含 AGENTS/CHANGELOG，redactions 为纵深防御声明，照 T2-04 样式）。
- [x] ≥1 个"看似可疑但实际正确"的诱饵点——两个常量旋钮、压缩失败保留、recall 严格语义。
- [x] 每组隐藏测试有第二数据场景——归档组：工具轮形 + 纯 user 形；配对组：悬空尾 +
  孤儿头两种形态；预算组：正文超限 + 参数体超限两口径；召回组：30×200 字截断 + 8×短记录全注入；
  no_data_loss 组：3 轮结果原文 + 压缩失败/条数兜底两分支。
- [x] 症状与三级提示词不含任何文件/函数/常量名——通篇只有"长期记忆区/整理动作/预算/
  配对/孤儿结果"这类现象词（T3 全脱敏，症状只以现象/日志痕迹出现）。
- [x] 只修一个端口的半成品必然 <100——partial（端口②）实测 14.29；端口① 单修实测 57.14、
  端口③ 单修实测 14.29（隐藏测试逐用例验证，任何单端口形态都有专属红组压住）。
- [x] 出题者自评"10 分钟能一次做对"→ 退回重做——**远超 10 分钟**：要先发现
  归档统计与预算工具的口径相反（现状缺陷），再设计"统一口径 + 轮次边界切点 +
  修复前置"的合取机制，任何一个"顺手改法"都各有专属红组。

## 六、门禁自验结果（§5.3，packgate 实测 2026-09-30）

| 门禁 | 结果 |
| --- | --- |
| 锚解（fix.patch） | **100.0**，6 组全绿（archive_integrity / pairing / budget_effective / no_data_loss / recall_reference / coherence×2），p2p 42/42 绿 |
| 半成品（partial.patch） | **14.29**（<100 成立，落在预期 14~30），p2p 0 破坏 |
| 注入态 | 0.0，6 组全红 |
| 注入态 ×20 | 得分稳定 0.0（min=max=0.0, stable=true），每组各红 20/20，p2p 30 条 0 破坏 |
| fix/partial 只触碰 allowed_paths | 通过（三个 patch 均仅 agent.py） |
| 泄漏兜底 | 题包全目录 grep 无受测仓库路径/敏感目录名；注入树构建时 `assert_no_leak` 通过 |
| 沙箱可见红测试 | 0（3 条点名用例已裁剪，见下） |

## 六·补、2026-10-02 体检修复记录（两道 packcheck 红清零）

本轮体检（runs/audit/2026-10-02）发现两道红并修复：

1. **coherence 组只有 1 条断言** → 新增第二数据场景
   `test_interrupted_overflowing_session_keeps_all_promises`：中断残留（悬空调用）
   + 参数体驱动的字符超限，在同一会话里同时压修复前置、安全切点、完整搬运与
   真实体积口径四条边。逐态实测：fixed 绿、partial（只修端口②）红（修复前置
   缺失 → 配对断言红）、injected 红（悬空 + 口径双杀）。coherence 组现有 2 条。
2. **「半成品应是锚解真子集」判据在单文件锚解上结构上不可满足**（fix 与 partial
   都只落 `agent.py`，单元素集合不存在真子集）→ 修 `packcheck.py`：同一文件
   集合时降级为改动体量比较（半成品 25 行 < 锚解 58 行），非子集仍然判红。

修复后门禁重跑（packgate，2026-10-02，结果已写回 `calibration/`）：

| 门禁 | 结果 |
| --- | --- |
| fixed | **100.0**，6 组全绿（含新 coherence 场景），p2p 30/30 绿 |
| partial | **14.29**（不变），coherence 仍红 |
| injected ×20 | **稳定 0.0**（20/20），6 组全红，p2p 0 破坏 |
| packcheck | **0 红 0 黄** → meta.json 摘除 `status: draft` |

隐藏测试确定性纪律：模型全部脚本化（FakeModel/ExplodingModel）、常量全部
monkeypatch、零网络零 sleep；性能硬约束（整理动作的模型调用 O(1)/轮）用**计数器
断言**（`test_tidy_keeps_model_calls_batched_per_turn` 与 coherence 组的压缩调用
计数），不做裸计时。

## 七、脱敏与裁剪记录

- `visible.prune`（3 条，全部点名答案的守卫用例，注入后实测变红）：
  - `tests/test_ai_agent.py::test_agent_archives_nothing_when_no_safe_cut_point_exists`——
    用例名直接点名"安全切点"概念；
  - `tests/test_ai_agent.py::test_agent_avoids_splitting_tool_pair_at_window_edge`——
    点名"配对切分"守卫；
  - `tests/test_ai_agent.py::test_agent_repairs_dangling_tool_calls_from_interrupted_run`——
    点名"中断修复"机制。
- `p2p.json`：候选 = `tests/test_ai_context.py` 全文件（预算工具本体）+
  `tests/test_ai_agent.py`；基线树 `--collect-only` 收集 33 条，基线 0 红，
  注入后 3 条变红（即上述 prune 名单）剔除后 **30 条**入白名单。
- `forbidden_paths` 含 `backend/astock_backtester/ai/cancel.py`（T2-07 地盘）。

## 八、基线门禁复核（2026-10-03）

`calibration/gate_baseline.json` 与 2026-10-03 独立 `packgate --state baseline` 复跑一致：
实测 **85.71**，p2p 无破坏（42/43 通过）。六个计分组里只有 `budget_effective_exit` 红，
红用例一条：

| 红用例 | 基线实测 | 判定 |
| --- | --- | --- |
| `hidden/tests_hidden/test_archive_trilemma.py::test_budget_counts_tool_call_arguments` | `AssertionError: 参数体必须计入预算口径：整理后真实体积 940 仍超限`（`assert 940 <= 600`） | 既有缺陷 |

**根因（§二已披露的现状缺陷，非注入）**：`agent.py::_archive_overflow` 的字符统计
只累加消息 `content`，而 `ContextBudget.count_tokens` 会把 `tool_calls` 的
`arguments` 计入——同一会话两个出口各执一词。注入端口 ② 只是**沿用了这个错误口径**
去做空转搬运，没有制造它。所以"把 `_archive_overflow` 恢复成原版"修不好这条：
原版口径照样漏掉参数体，会话"看起来不大"却每轮超限。

**题面覆盖核对**：该行为可由题面推出，不是给锚解私设的新要求。

- 第 1 级第 2 条给的就是这条的可观察后果：「个别会话每轮必败……每一轮请求都被上游以
  "超过长度上限"拒绝，怎么重试都没用。**奇怪的是这些会话"看起来"不大，
  甚至比正常会话还小**」——"看起来不大"正是"统计口径漏算了参数体"的用户可见形态。
- 第 1 级验收要求第 2 条把它变成判据：「任何一轮请求发出前，实际发送的内容必须真的在
  预算内——"看起来整理过"不算数，**会话"看起来不大"也不算数**，发出去超限就是失败」。
- 第 3 级不变量把口径写成机制条款：真实发送体积 = 正文 + 调用参数体。
- 该用例用 `_wire_chars`（正文 + `tool_calls.arguments`）度量模型实收请求窗口，
  不点名锚解新造的方法名（`_message_wire_chars` 在隐藏用例中 0 次出现），
  符合红线第 6 条的形态检查要求。

**处置**：保留为可从题面推出的既有缺陷，不降权、不移除。锚解通过新增
`_message_wire_chars` 统一口径把它一并修掉（§三.1），这正是 §二所述
「恢复原样 ≠ 100 分」这一层高级题设计。注入态下该组三条用例全红，判别力不丢。

**与 T1-01 / T3-09 的口径一致**：基线红项均为可从题面推出的既有缺陷，
锚解 = 恢复注入退化 + 新增被题面明确要求的正确行为，三题同款设计。

## 九、校准状态（§6.4）

`calibration/results.json` 盲测表待填，`calibrated = false`，目标带 [0.05, 0.25]。
出题模型不做盲测。
