# T1-03 参考解说明（成题版）

> 本文件只进 `reference/`，不进入沙箱快照白名单。
> 门禁结果由 `calibration/gate_*.json` 留档；盲测校准仍按 §6.4 由非出题模型执行。

## 一、注入端口（3 类，落盘 3 文件）

| # | 位置（原始快照行号） | 注入内容 | 可见症状 |
| --- | --- | --- | --- |
| 1 | `backend/astock_backtester/service.py:91-99` | 服务层流式分类只剩“参数非法/请求失败”两档；`LocalDataUnavailable` 被父类判断吞成校验失败，`KeyError` 被并入通用失败 | 调用方失去“无本地数据/缺字段”的稳定类别 |
| 2a | `backend/astock_backtester/service.py:629-640` | chat 在开始迭代时抛错后只关闭生成器，不再经共享助手写 error 终态 | 未配置等早期失败以空流结束 |
| 2b | `backend/astock_backtester/ai/facade.py:393-436` | worker 仍保存异常与投递哨兵，但消费端越过哨兵后不再 yield error 帧 | 上游失败先出现 session，随后无失败终态地断流 |
| 3 | `frontend/src/aiTypes.ts:307-333` | 三种已识别类别统一显示“AI 请求失败，请稍后重试”，未知错误的 message 兜底仍保留 | 用户无法判断应去设置、等待/停止还是检查上游 |

三处注入均不改签名，不影响正常成功路径。注入注释改成了“分类收成两档/连接关闭统一识别/哨兵后不追加”的自然维护口径，与原守卫注释不复刻。

## 二、锚解形态

1. 恢复具体异常优先于宽泛父类的服务层映射，四个基础类别各守其责。
2. 恢复 `_write_ai_error_event` 这一共享终态出口，让 `AiError` 与未知异常两个 catch 分支都调用它；worker 内失败则由 facade 在哨兵后送出保存的 error 帧。两类失败路径因此都以明确 error 终态收尾，成功路径仍只有 result。
3. 页面翻译以稳定类别为主分支，三个高频故障分别给设置、等待/停止、检查服务商的建议；并显式镜像后端 `AiError` 声明的全部类别。说明文本只做旧版本兼容与未知类别兜底。

`partial.patch` 只完成第 1、2 点（后端两个端口），故后端分类与终态组转绿，但页面组和包含全码镜像的 coherence 组保持红。

## 三、隐藏分组

| 组 | 权重 | 第二场景与防特判设计 |
| --- | ---: | --- |
| `error_code_exit` | 1 | 缺数据+缺字段；另以参数非法+未知异常作对照 |
| `stream_terminal_exit` | 1 | 未配置的迭代前失败、stub agent 的 worker 失败；另守正常 result |
| `frontend_branch_exit` | 1 | 三类码三种文案；另测上游 detail、未知码与非 Error 兜底 |
| `coherence` | 2 | 同一上游故障走 JSON/流式同码；文本/AST 扫描前端翻译镜像后端全部 `AiError` 码 |
| `p2p` | 0 | pytest 与 vitest 既有行为白名单，任一回归即整轮 0 分 |

## 四、陷阱与诱饵

- **半成品陷阱**：只修服务层/worker 终态，页面仍把已知类别压成通用文案；预计 40/100。
- **字符串猜码陷阱**：只按 message 片段分类，JSON 与流式的说明稍有不同就漂移，且全码镜像守卫仍红。
- **伪终态陷阱**：对任意 EOF 补成功 result 会吞掉真实失败；正常/失败终态守卫会区分。
- **正确诱饵 1**：`backend/astock_backtester/ai/errors.py:52-55` 对非 `AiError` 返回通用码是未知异常的正确兜底，不应改坏。
- **正确诱饵 2**：`service.py:1256-1281` 的普通请求捕获顺序保证准入冲突先于宽泛异常；调整会破坏 HTTP 409 语义。
- **正确诱饵 3**：`frontend/src/api.ts` 的非 2xx 解析和 `App.tsx` 的回测文案分支都不是本题退化点。

## 五、原生场景取舍

`service.py:745-755` 的常驻 AI 通知流在异常时只关闭生成器、不发送有限任务终态，这是原生行为，保持原样且隐藏测试不作断言。该通道由调用方重连，没有“最终结果”；把 chat 的有限流终态契约套过去会扩大题目范围并引入错误修复。

## 六、裁剪、p2p 与脱敏

- `visible.prune` 共 2 条：未配置 chat 流的具体 error 事件守卫，以及非法 session id 下仍返回同一错误码的守卫。两条都直接点名注入后缺失的终态/码；其余正常 chat、取消、锁与心跳用例保留可见。
- pytest p2p 共 10 条：在基线对 `tests/test_ai_service_http.py --collect-only` 后选出的非 AI 流式用例。
- vitest p2p 共 15 条：`api.stream.test.ts` 11 条 + `aiApi.chat.test.ts` 4 条。vitest 没有 pytest 的 collect-only 接口，故按标题手写，再由 packgate 实跑确认。
- redactions 使用 T2-04 同款结构，移除会直接给出错误码/终态答案的 AGENTS 与 CHANGELOG 章节；当前 slim 快照若未复制这些文档，规则仍作为未来白名单扩展的防泄漏声明。

## 七、§6.5 反过易检查清单

- [x] grep/读沙箱文档不能直接得到三处改法；答案型架构/更新记录已声明脱敏。
- [x] 至少一个看似可疑但实际正确的诱饵：AI 通用兜底、普通请求捕获顺序、前端传输兜底。
- [x] 每个计分组都有第二数据场景，单值硬编码或一刀切会失败。
- [x] 症状与三级提示词不含文件名、函数名、常量名或错误码字面量。
- [x] 只修后端端口的 `partial.patch` 必然低于 100；页面组与 coherence 独立保持红。
- [x] 出题者自评无法在 10 分钟内一次做对：需跨 Python/TypeScript 追踪分类、两类流式异常时机与页面消费，并避开三个正确诱饵。

## 八、门禁结果（packgate，2026-09-30）

| 状态 | 实测结果 |
| --- | --- |
| `fixed` | **100.0**；4 个计分组全绿；p2p 25/25，0 失败 |
| `partial` | **40.0**；后端分类与终态两组绿，页面与 coherence 两组红；p2p 0 失败 |
| `injected ×20` | **20/20 均为 0.0**；每轮 4 个计分组全红；score_min=score_max=0.0，stable=true；p2p 0 失败 |

三个门禁原始 JSON 位于 `calibration/`。结果符合预设，无分数组成偏差。

## 八·补、基线门禁复核（2026-10-03）

`calibration/gate_baseline.json`（`packgate --state baseline`）实测 **60.0**，p2p 无破坏。
四个计分组里只有 `coherence`（权重 2）红，红用例一条：

| 红用例 | 基线实测 | 判定 |
| --- | --- | --- |
| `hidden/tests_hidden/test_error_code_chain.py::test_frontend_translator_mirrors_every_declared_ai_error_code` | `AssertionError: 页面错误翻译缺少后端已声明类别：['ai_memory_not_found', 'ai_session_not_found', 'request_failed']` | 既有缺陷 |

**根因（现状即有，非注入）**：干净仓库的 `frontend/src/aiTypes.ts::translateAiError`
只显式识别三个类别（未配置 / 上一轮占用 / 上游失败），后端 `ai/errors.py` 声明的
另外三个类别（会话不存在 / 记忆不存在 / 通用请求失败）没有对应分支，
落进 `return error.message` 的兜底；同时被识别的三类又被压成同一句
「AI 请求失败，请稍后重试。」——这正是注入端口 3 放大的那个口径缺陷。
锚解（§三 / `fix.patch`）把它重写成"六个声明类别各有稳定分支 + 文本兜底仅作旧数据兼容"，
所以该用例在基线态红、锚解态绿。

**题面覆盖核对**：该行为可由题面推出，不是给锚解私设的新要求。

- 第 1 级验收要求已把"页面能识别后端每一种类别"写成必要条件：
  「页面要按类别给出不同的中文处理建议，**未知类别仍应保留有用的原始说明**」。
- 第 3 级不变量第 4 条是最直接的对应：「同一个上游故障经普通请求和有限流返回时，
  机器可读类别必须相同；**页面能够识别后端声明的每一种助手故障类别，新增类别不能
  悄悄落入错误的旧分支**」——上表那条红正是"三种已声明类别悄悄落进旧兜底分支"。
- 第 3 级不变量第 3 条把页面口径的判据写全：「未配置要引导完成设置，上一轮占用要提示
  等待或停止，上游失败要保留诊断并给重试建议；这些文案必须彼此可区分」。
- 该用例断言的是**后端声明集合与页面分支集合的镜像关系**（AST 读后端 `code` 常量、
  文本检查页面翻译函数），不点名任何锚解新造的函数名或字符串常量，符合红线第 6 条。

**处置**：保留为可从题面推出的既有缺陷，不降权、不移除；`coherence` 组另一条用例
（`test_same_ai_failure_keeps_its_code_in_json_and_stream`）在基线态是绿的，
说明该组红的唯一来源就是这条页面镜像缺口，不是后端出口本身的问题。

**连带观察（记录在案，本轮不改）**：`coherence` 权重 2 让基线分停在 60.0，
而本题是初级档——按入库门槛，基线红项必须能被第 1 级题面推出，本例成立（见上）。
但 60.0 的基线底分意味着"只修后端、不碰页面"的作答也拿不到 60，
这与初级档"抽共享点接通 ≥2 调用方"的定位一致，无需调整。

## 九、校准状态

`calibration/results.json` 仍为 `calibrated=false`，目标带 `[0.6, 0.85]`，盲测表保持空白；作者不进行 pass@1 校准。

## 十、难度审核复检（2026-10-03，盲做 + 归因的外部审核）

由非出题会话执行两段式审核（先盲做后归因），结论与改动记录如下，证据 JSON 在
`D:\tmp\dif-audit\T1-03\gate_*.json`（临时目录，不入库）。

**盲做实测（attempts=1，仅第 1 级题面）**：三处注入全部独立命中（service.py 两处 +
facade.py 一处），基线缺口（页面翻译缺 3 码）同轮命中；评测反馈驱动的第 1 轮内修正
0 → 60 → 100，p2p 零破坏，与 §八 门禁结果完全同构。

**发现并修复一处隐藏用例假阴性（镜像守卫的实现位置耦合）**：

- `test_frontend_translator_mirrors_every_declared_ai_error_code` 原实现把扫描范围限定在
  `export function translateAiError` **之后**的文本。审核中一种行为完全正确的等价实现
  （六个码放函数上方带引号查找表 + 函数内查表）实测 frontend_branch_exit 全绿、
  三条行为断言全过，却仍被镜像守卫判红（`gate_r1.json` 记录 60.0，缺 4 码）——
  判据耦合了「定义位置」，属 §6 假阴性条款（等价实现复验未通过）。
- **修复**：扫描范围放宽为整个 `frontend/src/aiTypes.ts`（页面翻译模块全文），
  同时接受单/双引号字面量；镜像标准不变——后端 `AiError` 声明的 6 个码缺任何一个
  仍判红，基线态既有缺口（缺 3 码）仍被兜住。
- **修复后复验**：锚解 `fixed` = 100.0（`gate_fixed.json`）；`partial` = 40.0 < 100
  （`gate_partial_afterfix.json`，镜像红保留）；被误判的等价实现重跑 = 100.0
  （`gate_eqimpl.json`）；审核解最终态 = 100.0（`gate_r1.json`）。三条硬线未破：
  未删用例、未降权重、未放宽对真实缺陷的断言。

**连带观察（不改，记录在案）**：

1. `test_specific_stream_failures_keep_distinct_codes` 直接 import 私有函数
   `_stream_error_code`。它位于被服务树且是本题唯一分类接缝，自然修法是就地扩展，
   故不判为假阴性；但若作答者把映射内联进两个调用点会 collection error。留给后续
   出题参考：隐藏用例尽量走 HTTP 行为面，少 import 下划线私有名。
2. **反判别力缺口（提难方向）**：镜像守卫是文本级的，「只修后端两个端口 + 在前端
   塞 6 个码的死字面量、不改翻译行为」可拿 coherence 绿 → 80 分，恰落在目标带内。
   封死路径：给 `frontend_branch_exit` 补 `ai_session_not_found` / `ai_memory_not_found`
   的翻译行为断言（当前这两码只有文本级覆盖），死镜像会在行为面露馅。
3. 题面「上一轮还没结束」（ai_session_busy）症状目前无任何隐藏用例覆盖终端行为；
   可作为第二流终态场景补入（提难，不属缺陷）。
4. 工具层两处 bug（report-only，未动）：`auditlib.py` ① 给 `packgate.build_tree`
   传 `str` 导致 `dest / "node_modules"` TypeError（应传 `Path`）；② `cmd_patch`
   用 `splitlines()` 喂 `difflib.unified_diff`，diff body 行丢失换行、整段粘连，
   `selfgrade._split_patch` 按行解析必然报「上下文不匹配」（T2-04 的 solver.patch
   同样粘连，该题历史审计若用过 grade 亦受影响）。审核用等价自写脚本绕过，
   未改任何引擎/工具文件。
