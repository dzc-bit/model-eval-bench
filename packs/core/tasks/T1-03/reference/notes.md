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

## 九、校准状态

`calibration/results.json` 仍为 `calibrated=false`，目标带 `[0.6, 0.85]`，盲测表保持空白；作者不进行 pass@1 校准。
