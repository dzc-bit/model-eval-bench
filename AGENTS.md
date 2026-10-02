# Harness 现状与维护纪律

> 这份文件写给下一个动手改这个项目的人（包括未来的我）。它记的是**当前事实与约束**，
> 不是变更日志；`git log` 才是历史。改代码前先读完「收尾语义」「时间口径」「验证门禁」三节。

## 这个 harness 是什么

本地一轮模型评测的完整闭环：选任务 + 选模型档案 → 工作台内与模型对话，模型只能通过受限工具
动**当前 run 自己的沙箱目录** → 评测台在独立的评分树里跑隐藏用例给分 → 记分板/排行榜汇总。
不存在"把提示词复制给外部聊天站再粘回来"这条路，`chat-panel.js` 也不接受手工粘贴。

题库定性（2026-10-02 拍板）：`packs/` 下的题目、`reference/` 标准答案与 `hidden/` 隐藏用例都是
**公开样例**——「隐藏」只指对被测模型不可见，不指对公众保密；四层隔离、脱敏与提示词纪律
防的是模型作弊。别把已废止的「公开 = 题目作废」旧叙事写回文档；成绩要进对外对比时另起
私有题库（`config.json` 的 `packs_root` 指过去，口径见 README §8）。

代码地图：

- 后端（纯标准库，无框架）：`console/harness/{runs,grade,chat,packs,sandbox,config,calibrate,server}.py`，
  检查器在 `console/harness/checks/{pytest,vitest}.py`，工具自检 `console/harness/selfcheck.py`。
- 前端：原生 ES module，入口 `console/static/js/main.js`，视图 `console/static/js/views/**`，
  工作台 = `views/workspace.js`（编排）+ `views/workspace/{task-node,chat-stream,report-node,run-details,dock}.js`；
  样式令牌在 `static/css/tokens.css`。
- 设计文档 `设计文档.md` 是规则源，用户手册 `README.md`，出题纪律 `packs/core/README.md`。
  三处与代码冲突时，先判"是实现错还是规则源过期"，两边都可能要改。

## 运行记录的生命周期

`queued → preparing → ready → grading → graded`，外加 `error` / `cancelled`。关键字段：

- `rounds[]`：每一轮的评分结果。`voided` = 用户点「继续对话（本轮分数作废）」（误校验的补救，
  证据保留但不计分）；`invalidated` = 越界/回归/校验器出错导致本轮不作数。两者都永不进台账。
- `round_started_at`：本轮起点（重建沙箱会重置），是「模型工作时长」的下限。
- `revealed`：看过参考解。参考解正文同时落盘到记录目录的 `revealed.patch`，报告窗与复盘
  都从运行记录读，刷新后仍在；该轮成绩永不进台账。
- `run.drive` 恒为空串（盘符沙箱已退役，只留字段兼容旧记录）。
- `model_gone`（只读视图字段）：记录还在但档案被删，工作台必须说得出这句话而不是显示空下拉。

**门禁是功能不是障碍**：模型没动过手不给校验；对话在飞（`chat_busy`）不给评分/晋升/删除；
`chat_busy` 来自服务端内存里的发送线程计数，重启即清零——别为了让按钮可点去改门禁，
该做的是把原因写在按钮上。

## 收尾语义（硬纪律：一个出口就是彻底结束）

工作台只有**一个收尾出口**，另外两个不是它的替代品：

| 动作 | 界面文案 | 进台账？ | 磁盘后果 |
| --- | --- | --- | --- |
| 结束本轮 | 「结束本轮」（常驻按钮） | 进 | 先写台账，再 `purge_run` 全删 |
| 作废本轮成绩 | 「继续对话（本轮分数作废）」（⋯ 菜单） | 不进（它不是出口，是继续路径） | 只翻 `voided`，证据全留 |
| 废弃本轮 | 「废弃本轮（真删，不留成绩）」（⋯ 菜单） | 不进 | `DELETE /api/runs/{id}`：全删，不可恢复 |

「结束本轮」与「废弃本轮」的磁盘后果完全相同，**唯一区别是进不进台账**：跑错了不想让这次
进榜就用废弃。`POST /api/runs/{id}/finish` → `runs.finish_round`：先 `record_run_result` 写
`runs/_results/ledger.json`，再 `purge_run`。顺序不能反——反了就是记录和成绩一起没了。
结束后工作台回到干净空态；地址栏里的 run_id 段照旧要摘掉（红线不变）。

**台账 = `runs/_results/ledger.json`**（`harness/results.py`；目录名的 `_` 前缀是必须的：
`list_runs` 不下钻 `_` 开头的层，台账因此天然不参与幽灵记录判定，`_prune_empty_dirs` 也够不着它）。
它与运行记录脱钩——记录删了榜单还有数。记分板与排行榜**只从台账读数**，不再从 `runs/` 记录现算，
所以还没结束的记录不在榜上，这是有意的。台账保留每一次结束的条目，榜单展示最高分那条。

台账口径（`runs.record_run_result`，与 2026-10-02 之前的旧记分板逐条对齐，回填后数字必须一致）：

- 只收 `_counted_rounds`：作废轮与越界/回归判无效轮一条都不进；整轮已揭晓参考解的运行一条都不进。
- 代表分 = 作数轮最高分；`pass1` 看第 1 轮。旧口径的 `trials` / `pass_any` / `ci_low` / `ci_high` /
  `revealed` 已随 Wilson 区间一起废弃：条目不再等于"通过的样本"，硬算区间是假精确。
- `entry.source_run_id` 用来去重：回填之后又点一次「结束」不会把同一次尝试数两遍。
- 一次性回填（幂等，不删任何记录）：
  `python -c "import sys;sys.path.insert(0,'console');from harness import runs,config;print(runs.backfill_ledger(config.load()))"`

`delete_run` / `purge_run` 的真删范围：记录目录（含 `chat.jsonl`、`epochs/` 纪元归档、
`report*.json`、`revealed.patch`、指纹、依赖基线）+ `sandboxes/<run_id>` + `sandboxes/_grade/<run_id>`。
档案目录被删空后父壳一并 rmdir，`runs/` 里不许留一串空目录。共享快照缓存
`sandboxes/.snapshots` 是受测仓库的基线，别的 run 还要用，**不动**。

两条不可妥协的实现约束：

- 路径必须在预期根目录内（`util.path_within`），越界立刻抛 `E_INTERNAL` 中止整次删除；
  删除不可逆，"少删一个目录继续走"是数据事故。
- `runs/_quarantine/` 只剩改造前的旧归档，新删除不再往里写任何东西。

**删档案 = 彻底删除**（2026-10-02）：`delete_provider` / `delete_model` 无条件级联删名下记录
与台账条目，`with_runs` 参数只为兼容旧调用保留、恒按 True 执行。确认框的级联勾选项已去掉，
文案改成「连同 N 条记录一并删除，不可恢复」。计数由 `/api/providers` 下发 `run_count` /
`entry_count`，匹配逻辑只有 `runs._provider_ownership` 一份（删除、计数、台账清理共用）——
前端以前复刻过一遍前缀 + legacy_ids 匹配，两份必然漂移，漂了就是删不干净的幽灵列。

**幽灵列的成因**：记分板的列 = `config.json` 里的档案 ∪ 台账里出现过的模型名。
删档案会连台账条目一起删，所以"档案删了列还在"只可能是台账里有档案不认领的条目。

## 时间与统计口径

- **排名用模型工作时间**（`model_work_seconds`：按每条用户提示切分对话跨度，并以 `round_started_at`
  为下限），墙钟 `wall_seconds` 只作为对照下发给前端悬停显示。排行榜表头写「模型用时」，
  tooltip 里说清墙钟含挂机与思考。
- 排行榜排序（`runs.task_leaderboard`）：最高分 → 轮数少 → 模型工作时间；同一模型只占一行，
  `attempts` 一并下发，让人看得出最高分那条是稳出来的还是撞出来的。
- 记分板与排行榜必须同口径、同数据源（同一份台账），历史上两套视图给过互相矛盾的结论，别再分叉。
- 检查器**没真正跑起来**（找不到 node、找不到 vitest、报告解析失败、零用例）一律
  `CheckResult.executed = False` → 记 `run_error` 并中断，绝不给 0 分冒充"跑过了"；
  组权重全零同样是 fail-closed，不发满分。
- 重建/重置沙箱会 `_archive_epoch` + `_void_rounds`：上一纪元的对话、报告、diff、round 产物
  归档后作废，新沙箱不会带着上一模型的成绩开工。

## 前端红线（用户逐条定过的）

- 出口不许条件隐藏：每个状态都要把「能走的路」全摆出来；卡住时给禁用态 + 原因，不是消失。
- 「结束本轮」必须显式常驻，不能只留折叠起来的回收。
- 最终总结内联在对话流里，不做弹窗、不做 sticky；历史区不 sticky 悬浮。
- 报告窗是打开时刻的快照：开窗期间有新报告只在顶部给一条 polite 的「有新结果，点击刷新」，
  不抢焦点、不自动替换正文。
- 回基线（重建沙箱）必须作废旧成绩，旧错误列表必须跟着消失。
- 只有网络调用进 `try`：删除已经落盘成功，收尾步骤出岔子不能报成「删除失败」。
- 地址栏是书签：记录被删后要把 `run_id` 段摘掉，否则刷新生成的是指向空记录的空书签。

## 工作台形态（2026-10-02 对话流改版）

工作台是单列对话流：顶部一条粘性状态栏（任务 · 档位 · 轮次 · 档案下拉 · 状态一句话），
主轴依次是「任务与提示词」折叠节点 → 消息流（思考默认折叠成一行；工具调用是默认折叠的
紧凑卡，展开看每次调用的入参/返回）→ 校验结果内联节点 → 运行详情 / 本轮备注折叠节点；
底部粘性区 = 操作栏（**每时刻一个主按钮** + 显式常驻的「结束本轮」+ ⋯ 更多操作菜单，
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
# 每个前端模块复制成 .mjs 后 node --check（当前 42 个）
```

- 跑 pytest 前必须给一个**全新**的 `EVAL_PYTEST_TMP`；残留的被锁 `.pytest-tmp` 会让 fixture
  大面积 ERROR，那是环境问题不是代码问题。跑完删掉。
- **测试绝不许碰真实的 `runs/` 与 `sandboxes/`**（2026-10-02 出过一次真实数据事故）。
  `conftest.forbid_writing_real_config` 已经在删除函数的**入口**上挡了这两条路径：任何用例
  只要让 `cfg["runs_root"]` / `cfg["sandbox_root"]` 指回真实目录就直接炸。两个必须记住的点：
  (1) 只 patch `config.CONFIG_PATH` **不等于**写侧落在临时目录——`config.load()` 会把
  `runs_root`/`sandbox_root` 按默认值解析到真实的 `runs/`、`sandboxes/`，删除类用例会真删；
  (2) 保险丝必须挂在**入口**（`delete_provider`/`delete_run`/`purge_run`/`sandbox.destroy`），
  挂在 `purge_run` 上不够——级联删是先 `list_runs` 再逐条 purge 的，真实 `runs/` 空了
  purge 一次都不会被调到，检查就成了摆设。新写删除类用例记得用 `cfg` fixture，
  或在自己的影子 config 里一起覆盖这两个根。
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
- `packgate.py` 的补丁应用仍借用 `selfgrade.apply_patch`，与 harness 自带的 `_apply_unified`
  是两份实现（2026-10-02 复核：`selfgrade.py` 不认识 vitest 一条已修——声明了非 pytest 检查的题
  现在 fail-closed；T4-11 与 T3-09 用例重复、node-ID 前缀约定两条也已随题库修复定稿，
  前缀约定见 `packs/core/README.md` 步骤 3）。
- 真实外部服务商调用仍未验证（本机没有有效密钥）；难度校准本轮明确不执行。

### 2026-10-02 两起事故的处置记录

- **T1-02 假 60 分**（校验器没跑起来却记成作数轮）已修：`run_error` 现在进 `invalid`
  （`grade.run_grade`），`executed=False` 的轮次一律 invalidated、永不进台账；报告分组带
  真实故障原因（不再是"隐藏测试可能导入失败"）。空 `node_modules` 加了两道 fail-closed：
  基线捕获 0 文件时 prepare 当场报错（提示 npm install），完整性自检认得"空目录"。
  8899 已于 2026-10-02 17:01 重启加载新后端，并用真实探针验证过：T2-06（纯后端）正常建箱，
  T1-02（前端）被 409 拒收、报错即上述文案（探针已删）。
  **那条 60 分的记录是修复前产物（`error` 有值、`invalidated=false`），处置：先 npm install
  再重建沙箱重测；对它点「结束本轮」仍会把 60 写进台账。**
- **批次取消毁在飞输出**（交接单 `docs/批次取消后工作台卡死-2026-10-02.md` 的缺陷 D）已修：
  沙箱回收统一走 `runs.release_sandbox` 门面（独占锁 + `chat.send_active` 在飞闸门 + 状态守卫），
  `batch._release_item_sandbox` 与手动回收不再自己拆 `sandbox.destroy`；在飞时拒收并保留目录。
  缺陷 A/B（`statusInfo`/主按钮，前端）由另一会话处理，缺陷 C（取消记录的出路）等拍板。
- `D:\New project 6` 的 node_modules 于 10-01 晚后被外部清空（目录创建/修改时间被改成
  2093-12-09 的垃圾值，回收站、npm 日志、Avast 隔离区均排除），**元凶未查明**；
  同窗口仓库 HEAD 被 reset 过。恢复依赖（npm install）后重测；要定位确切时刻可用
  管理员跑 `fsutil usn readjournal D: csv`（USN 日志未覆盖的前提下）。
- **批次停止语义修正（2026-10-02）**：`batch.cancel` 只拦 pending，已派发的条目
  （准备中/对话中/评分中）自然跑完，`cancel_requested` 不再被写入（旧标志无清除路径，
  会把 run 变成永久死路；旧记录上残留的标志仍被 runs.* 各闸门识别）。
  `_release_item_sandbox` 只在条目自然落定后走门面回收。`POST /api/batches` 新透传
  `auto_release`（默认 True 不变，想让沙箱留到人工复盘可按批关掉）。
- **台账代表条目补时间口径**：`results.best_of` 在分数、轮数之后加「有时间优先、
  用时短优先」——同分同轮数时测出真实用时的条目顶掉回填的无时间旧条目（与排行榜
  第三排序键同口径）；`record_run_result` 的墙钟在最高分轮非当前轮时置 None
  （旧实现会算出假 0），`model_work_seconds` 为真实 0.0 时不再触发重算。
