# 出题笔记（评测台内部用，不进沙箱）

## 三个端口

1. `pricing.turnover_rate` 分母写成收盘价 → 换手率量级爆掉。
2. `adapters.row_to_derived` 自己重算换手率（多除了一个 100）→ 与 pricing 不一致。
3. `engine.board_pass` 只校验市值闸门，换手率闸门被摘掉。

## 期望分档

| 模型行为 | 通过组权重 | 分数 |
| --- | --- | --- |
| 什么都不改 | 1 / 6 | 16.7 |
| 只修 pricing | 2 / 6 | 33.3 |
| 三处全修 | 6 / 6 | 100 |

`market_cap_exit` 权重 1 且始终为绿：它存在的意义是确认模型没有"为了凑绿"去乱动
本来正确的市值口径。`coherence` 权重 2，断言的是"三处一致"这件事本身，
所以逐个改对但没统一口径的做法拿不到这一组。

## 为什么裁掉那条可见测试

`tests/test_pricing.py::test_turnover_rate_is_volume_over_shares` 直接点名了
"换手率 = 成交量 / 流通股本"。留着它等于把答案写在题面上，所以走 `visible.prune` 裁掉；
同文件里其它用例（归一化、市值、ST 识别、零股本）仍然保留，不影响模型定位问题。

## 为什么脱敏 AGENTS.md 与 CHANGELOG.md

这两份文档的 §9 / §15 / §18 段与 1.5.2 / 1.6.0 / 1.6.1 三条记录，逐字写明了
"换手率 = 成交量 / 流通股本"和"engine 要同时校验两个闸门"。它们是标准的答案载体，
只在题包显式声明时随 README 一起拷进快照，再由 redactions 把相关段删掉。

## p2p

`test_normalize_symbol_stable` 与 `test_market_cap_stable` 在注入前后都是绿的。
任何"顺手把 market_cap 也改了"的提交都会在这里现形，整轮判 0。
