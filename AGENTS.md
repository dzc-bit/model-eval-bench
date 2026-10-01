# Harness 现状与维护纪律

> 这份文件写给下一个动手改这个项目的人（包括未来的我）。它记的是**当前事实与约束**，
> 不是变更日志；`git log` 才是历史。改代码前先读完「删除语义」「时间口径」「验证门禁」三节。

## 这个 harness 是什么

本地一轮模型评测的完整闭环：选任务 + 选模型档案 → 工作台内与模型对话，模型只能通过受限工具
动**当前 run 自己的沙箱目录** → 评测台在独立的评分树里跑隐藏用例给分 → 记分板/排行榜汇总。
不存在"把提示词复制给外部聊天站再粘回来"这条路，`chat-panel.js` 也不接受手工粘贴。

代码地图：

- 后端（纯标准库，无框架）：`console/harness/{runs,grade,chat,packs,sandbox,config,calibrate,server}.py`，
  检查器在 `console/harness/checks/{pytest,vitest}.py`，工具自检 `console/harness/selfcheck.py`。
- 前端：原生 ES module，入口 `console/static/js/main.js`，视图 `console/static/js/views/**`，
  工作台按面板拆成 `views/workspace/{chat,sandbox,grade,prompt,run}-panel.js`；样式令牌在 `static/css/tokens.css`。
- 设计文档 `设计文档.md` 是规则源，用户手册 `README.md`，出题纪律 `packs/core/README.md`。
  三处与代码冲突时，先判"是实现错还是规则源过期"，两边都可能要改。

## 运行记录的生命周期

`queued → preparing → ready → grading → graded`，外加 `error` / `cancelled`。关键字段：

- `rounds[]`：每一轮的评分结果。`voided` = 用户点「作废本轮成绩」（误校验的补救，证据保留但不计分）；
  `invalidated` = 越界/回归/校验器出错导致本轮不作数。
- `round_started_at`：本轮起点（重建沙箱会重置），是「模型工作时长」的下限。
- `revealed`：看过参考解，成绩不再进排行榜。
- `run.drive` 恒为空串（盘符沙箱已退役，只留字段兼容旧记录）。
- `model_gone`（只读视图字段）：记录还在但档案被删，工作台必须说得出这句话而不是显示空下拉。

**门禁是功能不是障碍**：模型没动过手不给校验；对话在飞（`chat_busy`）不给评分/晋升/删除；
`chat_busy` 来自服务端内存里的发送线程计数，重启即清零——别为了让按钮可点去改门禁，
该做的是把原因写在按钮上。

## 删除语义（硬纪律：删除就是真删）

三个动作各有分工，别再合并、别再改名回退：

| 动作 | 界面文案 | 磁盘后果 |
| --- | --- | --- |
| 作废本轮成绩 | 「继续对话（本轮分数作废）」 | 只翻 `voided` 标志，证据全留 |
| 结束本轮 | 「结束本轮并回收沙箱」 | 删沙箱副本，记录/对话/报告留着，仍可复盘 |
| 废弃本轮 | 「废弃本轮（真删）」 | `DELETE /api/runs/{id}`：全删，不可恢复 |

`delete_run` / `purge_run` 的真删范围：记录目录（含 `chat.jsonl`、`epochs/` 纪元归档、
`report*.json`、`diff.patch`、指纹、依赖基线）+ `sandboxes/<run_id>` + `sandboxes/_grade/<run_id>`。
档案目录被删空后父壳一并 rmdir，`runs/` 里不许留一串空目录。共享快照缓存
`sandboxes/.snapshots` 是受测仓库的基线，别的 run 还要用，**不动**。

两条不可妥协的实现约束：

- 路径必须在预期根目录内（`util.path_within`），越界立刻抛 `E_INTERNAL` 中止整次删除；
  删除不可逆，"少删一个目录继续走"是数据事故。
- `runs/_quarantine/` 只剩改造前的旧归档，新删除不再往里写任何东西。

`DELETE /api/models?id=…&with_runs=1` 才会级联删名下记录；不带 `with_runs` 时记录保留并
在 `skipped_busy` 里说明哪些因为正在对话没删掉。模型档案页的删除确认框已带「连同其下运行
记录一起删除」勾选项（默认不勾，确认前列出名下记录数，2026-10-02 落地）。

**幽灵列的成因**：记分板的列 = `config.json` 里的档案 ∪ 记录目录里出现过的档案名。
所以"档案不存在了但列还在"= 有记录没有档案；"档案在但列空"= 有档案没有记录。
只删一边列不会消失，2026-10-02 的 `1` / `01` 两个幽灵就是分别命中这两种情况。

## 时间与统计口径

- **排名用模型工作时间**（`model_work_seconds`：按每条用户提示切分对话跨度，并以 `round_started_at`
  为下限），墙钟 `wall_seconds` 只作为对照下发给前端悬停显示。排行榜表头写「模型用时」，
  tooltip 里说清墙钟含挂机与思考。
- 记分板与排行榜必须同口径：`voided` / `invalidated` 轮既不计通过也不进均分。历史上两套视图
  给过互相矛盾的结论，别再分叉。
- 记分板 `trials` 分母 = **真实跑过的尝试数**：建了记录但从未进入评分流程、或所有轮次都被
  作废/判无效的 run 不进分母（2026-10-02 修复，见 `runs._counted_rounds`）；均分 = 每条 run
  只贡献一个代表分（其作数轮的最高分）在作数尝试上的均值，同一档案多次尝试各算一次。
- 检查器**没真正跑起来**（找不到 node、找不到 vitest、报告解析失败、零用例）一律
  `CheckResult.executed = False` → 记 `run_error` 并中断，绝不给 0 分冒充"跑过了"；
  组权重全零同样是 fail-closed，不发满分。
- 重建/重置沙箱会 `_archive_epoch` + `_void_rounds`：上一纪元的对话、报告、diff、round 产物
  归档后作废，新沙箱不会带着上一模型的成绩开工。

## 前端红线（用户逐条定过的）

- 出口不许条件隐藏：每个状态都要把「能走的路」全摆出来；卡住时给禁用态 + 原因，不是消失。
- 要有显式的「结束本轮并回收沙箱」，不能只留折叠起来的回收。
- 最终总结内联在对话流里，不做弹窗、不做 sticky；历史区不 sticky 悬浮。
- 回基线（重建沙箱）必须作废旧成绩，旧错误列表必须跟着消失。
- 只有网络调用进 `try`：删除已经落盘成功，收尾步骤出岔子不能报成「删除失败」。
- 地址栏是书签：记录被删后要把 `run_id` 段摘掉，否则刷新生成的是指向空记录的空书签。

## 工作台形态（2026-10-02 对话流改版）

工作台是单列对话流：顶部一条粘性状态栏（任务 · 档位 · 轮次 · 档案下拉 · 状态一句话），
主轴依次是「任务与提示词」折叠节点 → 消息流（思考默认折叠成一行；工具调用是默认折叠的
紧凑卡，展开看每次调用的入参/返回）→ 校验结果内联节点 → 运行详情 / 本轮备注折叠节点；
底部粘性区 = 操作栏（**每时刻一个主按钮** + 显式「结束本轮并回收沙箱」 + ⋯ 更多操作菜单，
菜单项常列、禁用项写原因）+ 输入区。编排层在 `views/workspace.js`，节点实现按
`views/workspace/{task-node,chat-stream,report-node,run-details,dock}.js` 拆分；
区域锚点 `#ws-region-{prompt,chat,sandbox,grade,run}` 是书签契约，改名要同步路由。

两个真实踩过的 JS 坑，写代码时先想起来：

- `createWorkspace(props)` 的 `const { runId: routeRunId = '' } = props` 是 **const 绑定**，
  运行期改它会抛 `TypeError: Assignment to constant variable`，并把一次成功的真删误报成失败。
  需要改的值复制成 `let`（见 `urlRunId`）。
- `node --check x.js` 对 ESM 语法**静默返回 0**，等于没查。必须复制成 `.mjs` 或
  `node --input-type=module --check` 重跑；别人 PR 描述里"全部 JS 通过 node --check"不能采信。

## 验证门禁（他说"绿"就是这几条全绿）

```bash
EVAL_PYTEST_TMP=D:/tmp/evalpytest-<新目录> python -m pytest console/harness/tests -q
python console/harness/selfcheck.py          # 0 错误 0 提示
# 每个前端模块复制成 .mjs 后 node --check（当前 41 个）
```

- 跑 pytest 前必须给一个**全新**的 `EVAL_PYTEST_TMP`；残留的被锁 `.pytest-tmp` 会让 fixture
  大面积 ERROR，那是环境问题不是代码问题。跑完删掉。
- 静态声明与单测不算验收。UI 行为要在浏览器里点一遍并读证据（`evaluate_script` +
  `getBoundingClientRect`；Browser 面板没打开时 click/screenshot 会失败）。
- 8899 是本机唯一实例：重启前扫**所有** `runs/*/*/*/chat.jsonl` 的 mtime，最近 1–2 分钟还在写
  的都算在飞；`git pull` 后必须重启，否则新前端调旧后端 = "项目打不开"。只改 `console/static/**`
  不用重启（静态文件 `no-store`）。
- `packs/`（`reference/` 是标准答案、`hidden/` 是隐藏用例）与 `runs/`、`sandboxes/` 默认只读；
  要动先拿到明确授权。盲测校准是用户本人的事，出题者不自测，别往 `calibrated` 里填东西。
- Windows 专属：`du`/`cp -r` 进 `runs/` 会挂在 junction 上，只按文件名拷；控制台是 GBK，
  中文测试输出经管道会花屏（用文件读，不要 `| grep`）；PowerShell 一律 `pwsh` 且写 `.ps1`。

## 待拍板 / 已知未修

这些已经报过，等他点头再动：

- 批次视图不渲染 `started_at` / `finished_at`；跑批与工作台对"同一组合"的措辞还没统一。
- `T4-11` 的隐藏前端用例与 `T3-09` 完全相同；前端题的 node-ID 前缀约定还没定稿。
- `packgate.py` 仍留 private 兜底；`selfgrade.py` 不认识 vitest，前端题自校会假绿。
- `T2-05` 还是 `draft`：`allowed_paths` 只有 2 个文件、`coherence` 组只有 1 条断言。
- 真实外部服务商调用仍未验证（本机没有有效密钥）；难度校准本轮明确不执行。
