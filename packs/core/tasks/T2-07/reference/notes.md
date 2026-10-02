# T2-07 参考解说明（成题版）

> 本文件**只进 `reference/`**，永远不进沙箱快照白名单（§4.2 答案隔离）。
> 状态：成题完成，§5.3 门禁见 `calibration/gate_*.json`。

## 一、锚解的形态：把"停止"的三条协议接回去

§6.3 对中级题的硬性规格是"修复需设计机制或协议"。本题的机制是一句话：

> **"停止"不是开关，而是一份协议**：取消令牌在哪些边界被检查、悬空的工具调用
> 怎么补、会话锁怎么释放——三条边互相咬合，缺一条"停止"就退化成一种灾难。

锚解把三条协议逐条接回（无新增文件、无新增公共接口）：

| 协议 | 锚解动作 |
| --- | --- |
| 取消边界 | 恢复模型流**分片之间**的取消检查：发现已取消立即带 partial 收尾（协议历史留模型原文、展示层留停止说明），`finally` 关断上游流 |
| 消息配对 | 恢复被取消槽位的 interrupted 结果回填：assistant(tool_calls) 落盘的每一个 call 都当场有配对结果，下一轮修复无事可做 |
| 锁释放 | 恢复 worker `finally` 的无条件释放：被停止的轮次同样在保存后释放锁并归还引用，重发请求零等待 |

## 二、注入态的改动（三端口，落盘 3 文件 / 6 处编辑）

| # | 位置（原始行号） | 注入内容 | 症状面 |
| --- | --- | --- | --- |
| 1 | `agent.py` 模型流循环（约 L139） | 删除 token 分片之间的 `token.cancelled` 检查（`_finish_cancelled(partial=…)` 的唯一调用点随之消失） | 停止后长流照常写完、停止说明再也不出现 |
| 2 | `agent.py` `run()` docstring（约 L102） | 安全边界清单里同步去掉"模型流的 token 之间" | 注释与代码保持"自洽"，不残留线索 |
| 3 | `agent.py` `_execute_tool_calls`（约 L260） | 被取消槽位不再补 interrupted 结果，改为跳过（"缺口留给下一轮修复兜底"） | 停止后协议历史留下孤儿工具头；下一次提问先靠修复偷偷补洞 |
| 4 | `agent.py` `_invoke` docstring（约 L346）+ 导入行 | 同步改写；只为补结果服务的两个名字从导入中收窄 | 同上 |
| 5 | `facade.py` worker `finally`（原始 L406 附近） | 被停止的轮次跳过 `release_session_lock()`（"锁留在原地，让重发请求排在这轮收尾后面"——但收尾早已结束，没人再还锁） | 停止后锁被永久占用，重发白等 90 秒后判忙 |
| 6 | `cancel.py` 模块 docstring（约 L4） | 安全边界清单同步去掉"模型流的 token 之间" | 同 2 |

三处端口全部静默：不抛错、不改签名、不产生任何告警差异；注释与 docstring
同步改写成"看起来是有意的设计"。

**为什么不是回滚历史修复（相似度 < 0.6）**：注入不是删代码了事——每个删除点
都补了自洽的新注释给出一条"貌似合理的错误理由"（跳过的槽位"留给下一轮修复"、
被停止轮次的锁"留给重发请求排队"）；端口③还是一条新增的条件分支而非删除。
这组形态在任何历史提交里都不会同时出现。

## 三、陷阱

- **陷阱 A（半成品演示）**：只修配对端口。`partial.patch` 实测得分见下表——
  `pairing_exit` 绿，取消边界与会话锁两组全红，coherence（权重 2）因流中与
  工具批中两个时点仍不可用而红。
- **陷阱 B**：把锁等待上限调小（90 → 1）——"白等"看起来消失了，但锁的真正
  问题（被停止轮次漏释放）还在：每一次停止后的重发都会白等满上限然后判忙，
  `lock_release_exit` 两条用例红。
- **陷阱 C**：取消时直接丢弃整轮消息（"干净但暴力"）——用户已看到的答案没了，
  `cancel_boundary_exit` 的"已生成内容保留"断言红。
- **陷阱 D**：只在某一个取消时点做对（比如只处理流中取消）——coherence 组在
  另外两个时点各有一条用例（工具批中看配对、收尾轮中看边界+锁），必然红。
- **诱饵点**：①`cancel_turn` 只置位不阻塞是**正确**的协作式设计（第 3 级提示词
  已否决"改成同步等收尾"）；②`AI_STREAM_HEARTBEAT_SECONDS = 15` 看似与"停止
  无响应"有关，实际是前端保活机制，与本题无关；③`AI_SESSION_LOCK_TIMEOUT_SECONDS
  = 90` 是症状来源，但它是**上限**不是缺陷——把锅扣在常量上就是陷阱 B。

## 四、§6.5 反过易检查清单（逐条打勾）

- [x] **grep / 读文档 / git log 找不到"该修哪里、改成什么"。** AGENTS.md §15/§16
  与 CHANGELOG 相关条目已在 `redactions` 里划掉；三个注入点的周边注释同步改写
  成自然口径，不残留"这里曾经有检查"的痕迹。
- [x] **≥1 个"看似可疑但实际正确"的诱饵点。** 三个，见陷阱后的诱饵清单。
- [x] **每组隐藏测试有第二数据场景，硬编码 / 特判必挂。** 边界组三个场景
  （流首取消 / 流末前取消 / 流尾带 tool_calls）；配对组两批 + 修复不变量；
  锁组单轮与连续两轮停止；coherence 三个时点各一条。
- [x] **症状与三级提示词不含任何文件 / 函数 / 常量名。** 通篇只有"停止 /
  生成 / 工具调用 / 会话锁 / 白等"这类业务词汇。
- [x] **只修一个端口的半成品必然 < 100。** `partial.patch` 实测见下表。
- [x] **出题者自评"我 10 分钟能一次做对" → 退回重做。** 自评结论：**超过
  10 分钟**。三个注入点分属两条取消链路（agent 消息协议 / facade 锁协议），
  症状互相掩护（"停不下来"会掩盖"配对残缺"，"白等"又像性能问题）；修复
  需要同时理解 TurnRegistry 的登记/摘除、会话锁的引用计数与 worker 的
  finally 顺序，再写出三种时点都对齐的收尾。

## 五、visible.prune 与 p2p

| 条目 | 理由 |
| --- | --- |
| `tests/test_ai_agent.py`（整文件） | 该文件的 5 条取消守卫（`test_session_pairs_stay_valid_after_cancel`、`test_cancel_between_rounds_starts_no_further_model_call`、`test_cancel_after_tool_calls_landed_still_pairs_every_call`、`test_cancel_mid_stream_keeps_partial_text_verbatim_in_protocol_history`、`test_cancel_skips_remaining_tools_but_keeps_call_pairing`）名字+docstring 把三条协议在三个时点上的口径全部点名，留在沙箱等于发答案；其中两条在注入态还会红。按例裁剪对本文件不可用（harness 文本嗅探按前 4096 字节判定，该文件在字节 4094 处切中多字节字符被当作二进制跳过），故整文件裁剪。 |
| `tests/test_ai_service_http.py::test_ai_chat_cancel_stops_the_worker_and_releases_the_session` | 注入态变红 + docstring 直接点名"停止后释放会话锁"。 |

`test_ai_chat_cancel_requires_session_id` **保留**在 p2p：它只断言空参 400 校验，
不点名取消协议，注入后仍绿（与任务书草案预计的"也 prune"不同，按实测与
"点名才裁"的准则保留，减少无谓的回归覆盖损失）。

修复机制本体的回归随整文件裁剪一起离开沙箱，改由**两条隐藏 p2p 条目**承担
（`hidden/tests_hidden/test_collab_cancel.py::test_repair_synthesizes_placeholders_for_dangling_calls`
与 `::test_repair_keeps_intact_history_unchanged`，镜像被裁的既有守卫）：注入
不触碰修复机制，三条门禁状态下都必须绿；模型若以"删掉修复机制"的方式"修"
配对，这两条红 → 本轮作废。

p2p 白名单：37 条 = 会话锁/回收用例（tests/test_ai_sessions.py）+
非取消 HTTP 用例（tests/test_ai_service_http.py，剔除上面裁掉的一条）+ 修复机制
隐藏回归 2 条；基线探针剔除 0 条基线红，注入探针剔除
0 条注入红。

## 六、门禁自验结果（§5.3，packgate 实测）

| 门禁 | 结果 |
| --- | --- |
| 锚解（fix.patch） | 得分 **100.0**（1 次，稳定=True），cancel_boundary_exit 绿、pairing_exit 绿、lock_release_exit 绿、coherence 绿，p2p 破坏 0 条 |
| 半成品（partial.patch，只修配对） | 得分 **20.0**（1 次，稳定=True），cancel_boundary_exit 红、pairing_exit 绿、lock_release_exit 红、coherence 红，p2p 破坏 0 条 |
| 注入态 ×20 | 得分 **0.0**（20 次，稳定=True），cancel_boundary_exit 红、pairing_exit 红、lock_release_exit 红、coherence 红，p2p 破坏 0 条 |
| 参考解不触碰 forbidden_paths | 通过（fix.patch 仅 `ai/agent.py`、`ai/facade.py`、`ai/cancel.py`，partial.patch 仅 `ai/agent.py`，均在 allowed_paths） |
| 沙箱可见红测试 | 0（点名守卫整文件裁剪 1 + 点名单例裁剪 1，注入态变红用例全部离开沙箱） |

## 七、校准状态（§6.4）

`calibration/results.json` 盲测表待填，`calibrated = false`。出题模型不做盲测。

## 九·补、1 级题面纪律修正（2026-10-03）

第 1 级题面原本把「协作式停止、工具调用必须配对、收尾后立刻可用」三条机制不变量写成验收清单；已改写为只描述「停不下来 / 残缺记录 / 再问白等」三种用户现象。

依据：`packs/core/README.md` 附录 A「第 1 级（症状）：只写用户能观察到的现象、影响和具体例子；
**不列机制、原因、实现边界或验收不变量清单**」。改写后该级正文 690 字，
`packcheck` 复跑红 0 黄 0，三级字数仍严格递增。

改写前后的盲测数据（同一模型、同一探针、同一沙箱）见 `calibration/results.json` 的
`blind_runs.rows`；两轮均为**原始题面**，可作为「改前」基线。改后题面的 pass@1
**尚未测得**——主用模型当日配额耗尽（HTTP 429，重置 2026-10-04 00:52 UTC+8），
不填造、不推测。
