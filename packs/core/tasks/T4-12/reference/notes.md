# T4-12 出题存档（王者压轴题）

> 本文只进 `reference/`，永不进入沙箱。题目处于 draft 门禁状态前，不得对外宣称已校准或保证所有模型三轮必败。

## 一、出题目标与事实边界

这道组合题把四条此前各自独立、且从未互相组合过的自动通道放进同一个评分任务：
**会话回收窗口**（sessions + facade 忙碌判定）、**快讯节流**（insights 上限窗口与档位去重）、
**简报新鲜度与推送**（digest 记账点、新增判定、推送上限、来源围栏）、**寻优校验/排名/过拟合判定 + 前端流契约**
（optimizer、overfit、service 出口、aiApi 分发、寻优面板）。模型必须在一个仓库、九个可改文件、
十一个计分组里同时恢复全部口径，任意一条链没修干净，联合 coherence 组都拿不到分。

**已确认事实**（2026-10-03 实测，见第七节门禁表）：锚解在全部十一个组全绿（100.0 分，两侧 p2p 零破坏）；
只修简报链的半成品得 17.39（恰为简报两组权重 4/23）；注入态 ×20 稳定 0 分、p2p 零破坏；
基线（未注入快照）同样是 100 分——全部隐藏用例都是纯退化探测器，没有给锚解私设新行为。

**合理推测**：九个可改文件、十四条注入点、四类跨模块不变量（占用判定单一化 / 时序记账 / 信任与新增口径 /
排名与成本）加上跨语言的流契约，叠加 179 条回归白名单，任一环节改错会同时撞红业务组或打破回归。

**未验证假设**：强模型连续三轮仍不能修完。确定性评分与三级提示词无法从逻辑上保证任何能力等级的模型必败；
必须由非出题模型盲测验证（`calibration/results.json` 的 `blind_runs` 本表留空，作者不填造校准记录）。

目标为非出题模型 `pass_at_3` 落在 `[0.0, 0.02]`。若样本不支持，应重新设计题目并重跑盲测，
不得把"王者"标签当成证据。

## 二、四类跨模块不变量与分组映射

| 边界 | 必须共同满足的行为 | 评分组（权重） |
| --- | --- | --- |
| 会话占用判定单一化 | 删除与自动清理共用同一忙碌判定，覆盖"已认领、还没拿到执行权"的窗口；时间戳不可读只参与最旧优先 | `recycle_window_exit`（2） |
| 时序记账（节流与新鲜度） | 快讯上限按一小时滑动窗口记账；去重键保留档位划分；简报只有真的产出才推进"上次运行" | `insight_throttle_exit`（2）、`digest_freshness_exit`（2） |
| 信任与新增口径 | 每段抓取内容都带不可信围栏；"是否新要点"与入库去重同口径；单次运行推送封顶两条 | `digest_publish_exit`（2） |
| 校验 / 排名 / 成本 | 小数候选由组合评估判废而非判死整次请求；零成交组合不参与最优；指标增强整个网格一次 | `optimizer_validation_exit`（2）、`optimizer_rank_exit`（2） |
| 判定严重度 | 小样本只数可比较组合、措辞不虚高；critical 下限维持 5 笔 | `overfit_verdict_exit`（2） |
| 出口契约与跨语言一致 | 结果事件携带废组合名单与已评估数；超限返回专用错误码；前端断流契约与面板口径以服务端为准 | `optimize_http_exit`（2）、`optimize_stream_exit`（2）、`optimizer_panel_exit`（2） |
| 联合一致性 | 同一次运行在汇总、过拟合判定、解读上下文、库存与事件流里只有一个口径 | `coherence`（3，全表最高） |

合计 11 个计分组、总权重 23（pytest 19 + vitest 4），满足 king 档 ≥8 组；可改文件 9 个（≥6）。

## 三、注入点表（九条补丁、十四处改写）

注入以合成改写落地（`inject_edits.py::T4_12`，逐条为精确字符串替换，锚点命中数不为 1 即炸）：

| 补丁 | 文件 | 改写形态 | 症状 |
| --- | --- | --- | --- |
| 0001-sessions | `ai/sessions.py` | 时间戳读不出来按"最老"参与保留期淘汰（新增 `timedelta` 导入） | 坏时间戳的会话被销毁 |
| 0002-facade | `ai/facade.py` | 忙碌判定只看锁，丢掉引用计数（重写 docstring） | 认领窗口内的会话被清理/删除 |
| 0003-insights | `ai/insights.py` | ① 上限滑动窗口 1h → 24h；② 去重键丢掉档位划分 | 达到配额后整天沉默 / 跨档位移动漏报 |
| 0004-digest | `ai/digest.py` | ① 三条跳过路径都推进记账；② 新增判定改按标题原文；③ 推送去掉两条上限；④ 新闻来源丢围栏 | 简报迟迟不出、重复推送、提示注入 |
| 0005-optimizer | `ai/optimizer.py` | ① 整数参数小数候选入口判死；② 排名去掉零成交过滤；③ 增强移进组合循环 | 一个笔误废掉整个网格 / 零成交被标最优 / 写放大 |
| 0006-overfit | `ai/overfit.py` | ① 样本量计入被剔除组合；② critical 下限 5 → 3 笔 | 样本偏少提示消失、措辞组数虚高、3~4 笔降级 |
| 0007-service | `service.py` | ① 结果事件丢废组合名单；② 超限不再返回专用错误码（并移除失效导入） | 三出口口径断开 |
| 0008-aiApi.ts | `frontend/src/aiApi.ts` | 阶段事件被当成终态 | 断流不再报中断 |
| 0009-StrategyOptimizer.tsx | `frontend/src/components/StrategyOptimizer.tsx` | ① 最优高亮跟随流落点；② 拒绝名单改由页面自算 | 三出口出现第四个答案 |

**注入是合成改写而非"历史修复回滚"**：每条注入的注释、docstring 与周边文字全部改写为新的叙述
（例如把"引用计数 > 0 永不淘汰"的否决理由改写为"引用 > 0 而锁空闲说明调用方还在准备阶段"），
并引入历史上不存在的形态（`timedelta` 新导入、去重键整段重写、结果事件改字典推导）。
与历史修复（696c672 会话回收窗口、ba9c1c5 简报哨兵、5a07b58 寻优与过拟合收紧、bb566d6 流完成契约）
的 diff 文本无重叠。

`fix.patch` 是九条注入规格的逐条逆（9 个文件、115 行增删），因此一定精确 apply 在注入态之上。
`partial.patch` 只还原 0004-digest（简报链一个出口，17 行增删）——简报两组绿、其余全红，实测 17.39。

## 四、陷阱与诱饵

- **只调阈值不修判定**：保留天数/条数上限（`MAX_SESSIONS`、`SESSION_RETENTION_DAYS`）、
  冷却时长（`INSIGHT_DEDUP_WINDOW_SECONDS`）、冷却窗口（`FRESH_THRESHOLD_SECONDS`）、
  样本下限（`MIN_GRID_SAMPLES`）、笔数下限（`MIN_RELIABLE_TRADES`）、组合上限
  （`MAX_GRID_COMBINATIONS`）都是看上去最该动的旋钮，全部是诱饵：注入改的是判定本身
  （账本记在哪、谁判忙、谁进样本），挪阈值只在两个失真口径之间搬刻度。
- **启动时无条件强制跑一次简报**：掩盖记账时点的错误，未配置时的空转反而更吵。
- **在网格入口一律拒绝小数候选**：能挡住注入①，但会让一个笔误废掉整个网格，
  与"废组合只废自己"的契约相反（`optimizer_validation_exit` 两条场景一红一绿卡住这种解法）。
- **把零成交组合从结果列表里藏起来**：排名过滤与展示过滤是两件事，隐藏不改变它参与最优评选。
- **前端对服务端结果再算一遍口径**：那是第四个答案；`coherence` 组要求三出口同源。
- **未动的正确防线**（不许顺手改）：`EventBroker` 的 drop-on-full、`_looks_like_session` 形状检查、
  `ToolResultStore` 的条数/字节/TTL 三重上限、围栏先压缩后包裹的顺序（工具路径）、协作取消的组合边界——
  这些在注入态仍然正确，改动它们只会打破 179 条 p2p。

## 五、脱敏、裁剪与白名单

- `redactions`：`AGENTS.md` §4（路由表点名写侧动态清理）、§15（架构不变量点名 refs>0 与锁的双重判定、
  围栏纪律、流完成契约）、§16（AI 子系统指路）；`CHANGELOG.md` 1.5.0–1.6.1
  （点名寻优校验收紧、过拟合判定收紧、前端流完成契约）。提示词只使用业务症状与状态不变量。
- `visible.prune` 十四条，全部是**注入导致变红**或**直接点名答案机制**的守卫用例，裁剪后从 p2p 白名单剔除：
  | 裁剪用例 | 理由 |
  | --- | --- |
  | `test_session_busy_covers_the_claimed_but_not_yet_acquired_window` | 注入②打红；名字直接点名认领窗口 |
  | `test_prune_never_removes_a_session_in_the_claimed_window` | 注入②打红 |
  | `test_prune_keeps_unreadable_timestamps_unless_count_cap_forces_it` | 注入①打红；名字点名保留期策略 |
  | `test_insight_same_breadth_bucket_is_deduped` | 注入③b 打红；名字点名档位去重 |
  | `test_thin_grid_is_labelled_as_small_sample` | 注入⑥a 打红语义；名字点名小样本机制 |
  | `test_rejected_combinations_are_visible_in_the_dispersion_wording` | 点名废组合可见性（本题口径要求措辞不虚高） |
  | `test_very_few_trades_is_critical` | 注入⑥b 打红；名字点名 critical 下限 |
  | `test_normalize_grid_keeps_integer_knobs_integral_and_ratios_fractional` | 注入⑤a 打红；名字点名整数档容忍 |
  | `test_optimize_rejects_combinations_the_settings_model_itself_rejects` | 注入⑤a 打红 |
  | `test_optimize_prepares_indicators_once_per_run` | 注入⑤c 打红；名字点名"增强一次" |
  | `test_optimize_validates_grid_before_streaming` | 注入⑦b 打红；名字点名专用错误码 |
  | `test_optimize_reports_illegal_combinations_as_failures_over_http` | 注入⑦a 打红 |
  | `aiApi.optimize.test.ts::reports interruption when the grid stream ends without a result event` | 注入⑧打红；名字点名完成契约 |
  | `StrategyOptimizer.test.tsx::shows rejected combinations outside the ranking table` | 注入⑨b 打红 |
- 白名单规模：pytest **153 条**（AI 子系统 11 个套件在裁剪后基线快照上的全部用例，收集自基线树），
  vitest **23 条**（寻优流/寻优面板/JSON 传输/事件流订阅/行情刷新）。任一回归红都让该轮记 0 分。

## 六、§6.5 清单

- [x] grep/读文档/git log 找不到"该修哪里、改成什么"：AGENTS §4/§15/§16 与 CHANGELOG 1.5.0–1.6.1 全部脱敏；
      注入的注释/docstring 一并改写成新叙述，不留任何点名不变量的原句。
- [x] 有多个看似可疑但实际正确的诱饵点：见第四节（六个常量、EventBroker、形状检查、协作取消）。
- [x] 每个评分组有第二数据场景：九个 pytest 组各 2–3 条独立数据场景（快讯组两条用两个不同出口、
      简报组三条覆盖围栏/上限/口径、过拟合组三条含一条两态皆绿的护栏），两个 vitest 组各 3–4 条；
      fixture 数据全部临时自建，不读仓库真实数据。
- [x] 症状与三级提示词不包含实现文件名、函数名或常量名：提示词只出现"忙/闲""新/旧""合法/非法"
      "最优/废弃""完成/中断"等业务口径；`packcheck.py` 的禁用名词扫描零命中。
- [x] 半成品只修一个端口：17.39 分实测证明单端口修复必然 <100，且简报两组绿时其余链条仍红。
- [x] 十分钟自评不成立：九个文件、四条链、跨语言契约、179 条回归同时受约束。

## 七、门禁与盲测状态

| 门禁 | 结果 |
| --- | --- |
| baseline | **100.0**（11 组 28 条隐藏用例全绿，p2p 0 破坏）——隐藏组均为纯退化探测器 |
| fixed | **100.0**（11 组 28 条全绿，pytest 175/175、vitest 30/30，p2p 0 破坏） |
| partial | **17.39**（简报两组绿、其余全红，p2p 0 破坏） |
| injected ×20 | **稳定 0.0（20/20）**，11 组 28 条全红、p2p 0 破坏 |

原始输出见 `calibration/gate_*.json`（注入态由 4 段 × 5 次合并，单进程连跑 20 次会超出
后台任务存活上限被终止，合并结论与一次跑完等价；每段单独的文件已删除）。
当前校准纪律：`calibrated=false`，`blind_runs.rows=[]`，汇总值全部为 null。由非出题模型完成
至少 5 组独立三轮轨迹后按 `pass_at_3` 统计；达到样本量后再判断是否进入 `[0.0, 0.02]` 目标带。

## 八、隐藏测试的防假阴性自查（2026-10-03）

- **`rank_combinations` 是模块级纯函数**，用例只喂"指标字典"（成交数/收益/回撤）并断言最优归属，
  不依赖任何调用路径或命名——锚解用别的过滤方式（比如先 copy 再 filter）同样全绿。
- **快讯组用假时钟**（monkeypatch 模块内 `time`）断言窗口滑走后的恢复，不依赖真实等待；
  档位分桶的对错由"9.6% → 12.3% 同桶 → 23.1% 跨桶"三步数据区分。
- **面板用例把高亮判定与展示口径分开断言**：最优行只看 `.best-row` 落在哪一行，
  拒绝名单只看服务端名单是否原样出现——不依赖组件内部实现。
- **前端口径在"库存有该条目但未进排名"时断言名单出现**：假"页面自算"实现即使碰巧算对，
  也会在"废组合不可交易、其余都成交"的场景里露出第四个答案。
- 注：`optimizer_validation_exit` 的小数候选用例必须**先过 `normalize_grid` 再进 `run_optimization`**
  ——直接调 `run_optimization` 会绕过入口校验，注入⑤a 将无法被探测（三态门禁曾据此抓到一版假绿，已修）。

## 九、出题侧难度自检（非校准，2026-10-03）

为验证"即使给到第 3 级提示词、三轮内也极难做对"，出题侧按用户要求做了一次**工程性难度自检**
（这是难度证据，**不是** §6.4 的盲测校准；`calibrated` 仍为 false、`blind_runs` 仍留空）：

在 `D:\tmp\t4probe\sandbox`（注入态 + visible.prune 后的真实沙箱）派一个 general-purpose agent，
给它**第 3 级（最终、最严格）提示词**与九文件边界，单轮全力试做，然后用 `packgate --state custom`
对它产出的 diff 真实评分：

| 项 | 结果 |
| --- | --- |
| agent 改动文件 | 8 个（digest / facade / insights / optimizer / sessions / service / aiApi.ts / StrategyOptimizer.tsx），零越界 |
| 自称 | "九条不变量全部恢复" |
| 真实得分 | **60.87 / 100**，p2p 153+23 零破坏 |
| 仍红的组 | `insight_throttle_exit`、`overfit_verdict_exit`、`coherence`、`optimizer_panel_exit` |

失败点恰好落在"高置信区"：
- **overfit.py 一行没动**（agent 判定"网格措辞本来就自洽"）——样本量计入被剔除组合、critical 下限
  5→3 两条注入都在这里；
- **快讯档位语义理解偏**：把去重签名改成 `level|key`，没能恢复"同因同档位冷却、跨档位重发"的语义；
- **拒绝名单**虽改回按服务端 failures 渲染，但展示细节仍不满足原样口径；
- 权重最高的 **coherence 组被 overfit 拖红**——四条链互锁设计生效：单修任何一条链，
  联合不变量仍不成立。

结论：第 3 轮拿到 60.87 而非 100，且红点分布在最容易被自信跳过的文件上，与 king 档
"pass@3 目标 0.00~0.02"的设计意图一致。**这仍不是校准**——正式校准须由用户组织的非出题模型、
至少 5 组独立三轮轨迹完成。

## 十、出题侧发现的 harness 缺陷（已修，2026-10-03）

`console/harness/util.py::guess_text` 原实现用固定 `raw[:4096]` 探针做解码嗅探：多字节字符恰好横跨
探针边界时 UTF-8 与 GBK 的严格解码都会失败，**纯文本文件被误判为二进制**，而
`snapshot.apply_prune` / `apply_redactions` 的 `if not util.guess_text(raw): continue` 是**静默跳过**——
本次 14 条 `visible.prune` 里有 4 条（`tests/test_ai_sessions.py` ×3、`StrategyOptimizer.test.tsx` ×1）
因此完全没有生效，注入态 p2p 被打断而现场不留任何痕迹。修法：解码探测用完整内容
（NUL 探针不变），并在本文件注释里记下原因。`console/harness/tests` 全绿后（exit 0）才继续出题。