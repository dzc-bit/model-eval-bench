# 模型评测台 · Model Eval Bench

把"给模型出一道**真实的代码排障题**"这件事工程化：合成注入、沙箱隔离、分组部分分、
三级提示词、入库门禁、校准纪律，全部落成可执行的代码与可入库的任务包。

- 设计与验收标准：[`设计文档.md`](设计文档.md)（v2.2，含四层隔离、难度四杠杆、十一道起步题、任务包规范）
- 任务包出题指南：[`packs/core/README.md`](packs/core/README.md)（流水线 8 步、工具速查、删除纪律）
- 本次整理账本：[`整理报告.md`](整理报告.md)

> **⚠️ 先读这一句**：本仓库是**出题侧**——题库与**标准答案**的家。
> `packs/core/tasks/*/reference/`（锚解、半成品解、注入点说明）与 `packs/core/tasks/*/hidden/`
> （隐藏评分用例）就是答案。若要**公开**这个仓库，请先读 [§8 答案隔离红线](#8-答案隔离红线重要)。

---

## 1. 它解决什么问题

给模型出题最容易出的三种废题（设计文档 §6.1 列为禁止项）：**答案在明面上**（守卫测试名/文档
直接点名不变量）、**本质是回滚历史修复**（`git revert` 就完事）、**单点注入单点变红**
（考的是查找而不是排障+设计）。这个评测台用题目隔离、文件夹工作区、合成注入和独立评分树控制评测流程：

| 层 | 手段 |
| --- | --- |
| 1 · 内容隔离 | 沙箱快照只拷运行测试必需的子树；相关文档段（`AGENTS.md`/`CHANGELOG.md`）进 `meta.redactions`；点名不变量的守卫用例进 `meta.visible.prune` |
| 2 · 工作区约束 | 沙箱位于评测台的普通文件夹中；内置对话通过受限工具操作该目录 |
| 3 · 版本隔离 | 沙箱内 `git init` + 单提交 `baseline`；注入是**合成改写**（与历史修复相似度 < 0.6） |
| 4 · 评分隔离 | 评分树 = 原始快照（测试已裁剪）+ overlay（只收 `allowed_paths` 内产物）+ 隐藏层，**不在沙箱里跑** |

难度分四档（§6.3）：初级 T1（1 次尝试）/ 中级 T2（2 次）/ 高级 T3 与王者 T4（各 3 次），硬性规格逐档加码
（≥4 模块、必须自行设计机制、≥2 陷阱、≥5 隐藏组、含并发或性能硬约束）。计分是**按组加权
部分分**，`coherence` 组权重最高（只断言"多出口对同一事实口径一致"），`p2p` 回归组权重 0
但一断即本轮作废。

---

## 2. 目录结构

```
.
├─ 设计文档.md                  ← 规格源头（v2.2）
├─ 整理报告.md                  ← 最近一次工作区整理与 pack 体检账本
├─ 启动.cmd                    ← 双击即起服务并打开浏览器
├─ console/                    ← 评测台本体（纯标准库，零构建）
│  ├─ server.py                ← HTTP 服务 + JSON API + 静态托管
│  ├─ config.json              ← 唯一配置（受测仓库/路径/超时/快照白名单/模型档案）
│  ├─ harness/                 ← 通用校验引擎：sandbox / snapshot / grade / runs / report / packs / batch / selfcheck
│  │  └─ tests/                ← 引擎自身的用例与自造题包 fixture（数量见最新验收记录）
│  └─ static/                  ← 前端（原生 ES 模块，无构建、无第三方依赖）
│     ├─ js/views/             ← 八个视图：任务库 / 排行榜 / 工作台 / 批量跑批 / 记分板 / 模型档案 / 设置 / 帮助
│     └─ js/components/        ← 16 个组件（含 result-mark 成功失败 SVG 动画）
├─ packs/                      ← 题库与答案的家，**永不进沙箱**
│  └─ core/
│     ├─ index.json            ← 题目登记表（status / target_band / prune / p2p / 门禁摘要）
│     ├─ README.md             ← 出题指南（流水线、工具、删除纪律）
│     ├─ tools/                ← 出题侧自验工具（纯标准库）
│     └─ tasks/T1-01 … T4-11/  ← 十一道起步题
├─ runs/                       ← 运行记录与出题侧脚本
│  ├─ blind/tools/             ← 门禁 runner、成题脚本、整理工具
│  ├─ blind/gates/             ← 门禁临时评分树落点（跑完自动删，不入库）
│  └─ blind/calib/             ← 盲测试跑记录
└─ sandboxes/                  ← 沙箱落点 + 快照缓存 + 跑批快照（内容不入库）
   ├─ .snapshots/              ← 按题缓存的"已注入基线骨架"
   └─ _batches/                ← 批量跑批的批次快照（batch.json，服务重启后可查）
```

单个任务包（`packs/core/tasks/<ID>/`）：

```
meta.json                  ← 元数据（tier / allowed_paths / forbidden_paths / visible.prune / redactions / budget）
prompts/1.md 2.md 3.md     ← 三级提示词：症状 → 不一致清单 → 不变量 + 否决项（零文件名/函数名/常量名）
inject/patches/NNNN-*.patch← 合成注入（标准 unified diff，按文件名顺序应用）
hidden/tests_hidden/*.py   ← 隐藏评分用例（评分时才出现）
hidden/groups.json         ← 分组与权重（coherence 权重最高；p2p 权重 0 + mode=regression）
                             用例 ID 相对 hidden/ 那层写（`tests_hidden/x.py::t`），
                             引擎会自动补成相对评分树根的路径
p2p.json                   ← 必须保持全绿的既有用例白名单（ID 相对评分树根写：`tests/x.py::t`）
reference/fix.patch        ← 锚解（相对注入态，须拿 100 分）
reference/partial.patch    ← 半成品解（只修一个端口，须 < 100）
reference/notes.md         ← 出题侧全账：注入点、陷阱与诱饵、裁剪理由、§6.5 逐条打勾、门禁结果
calibration/results.json   ← 校准总表（盲测前保持空表）
calibration/gate_*.json    ← 三级门禁原始输出
```

---

## 3. 环境要求

| 项 | 要求 | 本机实测 |
| --- | --- | --- |
| Python | 3.11 ~ 3.13 | 3.13.1 |
| pytest | ≥ 8.2（**评测台自己不用**，是给受测仓库跑用例的） | 9.0.3 |
| Node.js | 仅**前端题**需要（`slim-py+fe` 快照）；优先用受测仓库自带的 `.tools/node-*`，否则系统 PATH | 可用 |
| 第三方依赖 | **评测台本体零依赖**（纯标准库）；受测仓库自己的依赖由它自己的 venv 提供 | — |

不需要 `pip install` 任何东西就能启动控制台；跑门禁时才需要目标仓库的依赖可导入。

---

## 4. 快速开始

```powershell
# 1) 改唯一一行配置：受测仓库在哪
#    console/config.json → "repo_root": "D:\\New project 6"
#    （其余路径都是相对评测台根目录的，换机器不用改）

# 2) 启动（二选一）
双击  启动.cmd
python console\server.py --open          # 等价写法；--port / --host 可覆盖 config.json

# 3) 打开前端
http://127.0.0.1:8899
```

启动后控制台会做一次环境自检（受测仓库可读、pytest/Node 可用、文件夹沙箱可用、静态资源完整），
异常项直接显示在页面上。API 入口：`GET /api/tasks`、`GET /api/task/<ID>`、`GET /api/runs`、
`POST /api/batches`（批量跑批）等。

**前端地址：`http://127.0.0.1:8899`**（`host`/`port` 可在 `console/config.json` 改；
前端是零构建的原生 ES 模块，改完刷新即生效，不需要打包步骤）。

---

## 5. 控制台能做什么

- **任务库**：题目档位、目标带、裁剪条数、白名单条数、历史成绩
- **工作台**：准备沙箱文件夹 → 发送提示词到内置模型对话 → 模型通过工具操作当前工作区 →
  校验 → 分组报告（红绿 + 失败摘要 + 越界检测 + 相似度标记）→ 揭晓锚解。提示词复制仅供导出或外部备用。
- **排行榜**：独立主导航；选择题目后显示该题按模型档案汇总的专属排行。
- **批量跑批**：按 `max_concurrency` 并发准备「多道题 × 多个模型」的会话。
  就绪后进入各自工作台与模型对话，再手动启动评分；评分完成后默认回收沙箱并派发下一条。
  取消后不再派发新条目，准备中的任务协作退出；已开始的评分允许收尾，批次保持“取消中”直到完成清理。
- **记分板**：按模型档案分区查看 pass@k 与目标带对比
- **模型档案**：必填只有 API 根地址、模型名和密钥——密钥直接在页面粘贴，服务端存到不入库的
  `console/keys.local.json`，`config.json` 与页面只保留脱敏值；协议、调用接口、密钥环境变量名收在「高级选项」。
- **设置**：超时、快照白名单

### 对话中的模型思考

接口返回的文本 `reasoning_content` 或 `reasoning` 会在“模型思考 / 推理摘要（接口返回）”区域默认展开，
可折叠，并随 `chat.jsonl` 保存；刷新后仍可查看。工具调用前的推理字段按原名保留，供后续 API 回合使用
（DeepSeek 一类服务商在请求携带 `tools` 时要求历史轮完整回传，不回传会直接报错）。
接口没有返回推理时不再显示“未返回可展示的推理内容”这类占位语，正文本身就是全部内容。
长请求期间轮询已落盘的模型/工具消息；当前是逐回合刷新，不是逐 token 流式输出。
这取决于服务商公开字段，不保证取得完整隐藏思维链。

### 对话的上下文窗口与压缩（`config.json` 的 `chat` 节）

发给模型的上下文由服务端按「完整轮次」组装，与前端展示的完整记录是两套视图：
`chat.jsonl` 与工作台始终是全量，只有送给模型的那一份会被压缩或裁剪。

```json
"chat": {
  "max_history": 100,        // 单位：条。历史消息条数上限，按完整 user→assistant/tool 轮次累积
  "max_context_chars": 160000,  // 单位：字符。上下文字符预算（含压缩后的历史），与条数是双闸门
  "tool_summary_chars": 800, // 单位：字符。历史轮工具返回压成摘要后保留的字符数
  "keep_first_prompt": true  // 是否始终保留第一条用户消息（题目提示词）
}
```

超预算时的处理顺序是**先压缩、再丢弃**：历史轮的工具返回换成「工具名 + 参数 + 结果摘要」
（`write_file` 保留路径与字节数，正文已落盘不必重发）→ 仍超就把更老的轮次塌成「只剩模型说过的话」
→ 再超才整轮丢弃，且钉住的第一条提示词与最新一轮不参与丢弃；真发生整轮丢弃时会在 system 消息里
写明「更早的 N 轮对话已省略」。这样修掉了旧实现里「受测模型一轮并行 6 个工具调用、消息数超过
`max_history` 就把整轮裁掉，续轮只能靠 git 重新考古」的失忆问题。
这些值是默认项，`config.json` 只写其中几项也能生效；窗口数字写坏会退回默认值，不会让对话不可用。
窗口是**全局**的：换到上下文更小的模型时，正确动作是把 `max_context_chars` 调小，
而不是等它报窗口超限。压缩会重写历史，服务商侧的前缀缓存因此必然失效——这是有意的取舍，
「续轮不失忆」比省这点 token 更重要。
工具的安全限制（沙箱路径、命令白名单、超时、输出上限）不受这些配置影响，只可能更严。

模型档案需要有效的 `base_url`、服务商支持的模型名和 API 密钥。密钥在「模型档案」页直接粘贴，
服务端保存到本机密钥文件 `console/keys.local.json`（已被 `.gitignore` 排除），立即生效、无需重启；
页面与 `config.json` 只保存脱敏值，明文不回显。也可以改用服务端环境变量提供密钥，
优先级：页面粘贴的密钥 → `key_env` 指定的变量 → `MODEL_<档案ID大写>_API_KEY` → `OPENAI_API_KEY`。

当前工作区是普通文件夹，并非操作系统级容器。文件与命令工具会限制路径、命令种类、超时、环境变量和输出量；沙箱内运行的程序仍可能利用自身能力访问本机其它资源。不要把它用于不可信代码的强安全隔离。

---

## 6. 出题流水线（给新题用）

完整八步见 [`packs/core/README.md`](packs/core/README.md)，最常用的三条：

```powershell
cd packs\core\tools

# ① 生成注入补丁（锚点命中次数强校验，失配即炸，绝不产出半吊子 patch）
python inject_edits.py --repo "D:\New project 6" --task T3-10 --work-dir _work

# ② 生成锚解 / 半成品补丁（相对注入态，保证一定 apply 得上）
python solution_edits.py --repo "D:\New project 6" --task T3-10 --work-dir _work

# ③ 完整性自检（目录树 / meta / 分级 / p2p / 提示词零名词泄露 / §6.5 清单 / 门禁材料）
python packcheck.py --repo "D:\New project 6" --out _work\packcheck.json
python packcheck_summary.py _work\packcheck.json      # 压成"每题红项清单"
```

三级门禁（§5.3，用与生产 harness 同一套拼树/裁剪/计分语义的 runner）：

```powershell
cd ..\..\runs\blind\tools
python packgate.py --task T3-10 --state fixed    --out ..\..\packs\core\tasks\T3-10\calibration\gate_fixed.json
python packgate.py --task T3-10 --state partial  --out ..\..\packs\core\tasks\T3-10\calibration\gate_partial.json
python packgate.py --task T3-10 --state injected --repeat 20 --out ..\..\packs\core\tasks\T3-10\calibration\gate_injected_x20.json
python t0310_loop.py fixed --rebuild                  # 成题期快速迭代：拼一次树，直接跑隐藏用例
```

入库门槛：锚解 100/100、半成品 < 100、注入态 ×20 稳定 0 分且目标组全红、p2p 零断裂、
`packcheck` 零红项 → `meta.json` 去掉 `status: draft` → 登记 `index.json`。

---

## 7. 工具速查

### 出题侧（`packs/core/tools/`，纯标准库）

| 工具 | 用途 |
| --- | --- |
| `mkpatch.py` | 由"原始/改写"两份文本生成 `git apply` 兼容的 unified diff |
| `inject_edits.py` | **注入规格**（精确字符串替换，锚点必须恰好命中一次）→ 生成 `inject/patches/` |
| `solution_edits.py` | **参考解规格**（相对注入态）→ 生成 `reference/fix.patch`、`partial.patch` |
| `selfgrade.py` | 轻量门禁自验（自带 diff 应用器与测试裁剪器，只在 `tests_hidden/` 布局下用） |
| `packcheck.py` | 完整性 + §6.5 清单 + 提示词名词泄露扫描（**入库唯一硬门禁**） |
| `packcheck_summary.py` | 把 packcheck 的 JSON 压成"每题红项清单" |
| `regenerate_index.py` | 按包内文件与 packcheck 实测重算 `index.json`，不手填 |

### 编排侧（`runs/blind/tools/`）

| 工具 | 用途 |
| --- | --- |
| `packgate.py` | **门禁 runner**：按真实 harness 语义拼评分树 + 跑 pytest/vitest + 按组计分 |
| `blindrun.py` | 盲测试跑编排 |
| `t0310_loop.py` | T3-10 成题期快速迭代器（拼一次树，直接跑隐藏用例） |
| `T3-10-scan.py` | 只读勘察：`outline` / `show` / `grep` 某个源文件 |
| `tidy_workspace.py` | 工作区整理（按 README §六 的删除纪律，支持 `--dry-run`） |
| `hidden_group_audit.py` | 审计某题隐藏用例是否都被分组引用（判断能否低成本补组内断言） |
| `author_t0*.py` | 各题成题脚本（历史存档，含绝对路径，换机器需改常量） |

---

## 8. 答案隔离红线（重要）

1. `packs/`、`reference/`、`hidden/` **永不进沙箱快照白名单**；`allowed_paths` 不得指向它们。
2. 症状与三级提示词**不得出现任何文件名 / 函数名 / 常量名**——`packcheck.py` 会把
   `allowed_paths` 叶子名、隐藏测试标识符与用例名、分组 id、注入与参考解里被改的文件名
   汇成禁用词表，对三级提示词做大小写不敏感的全词扫描，命中即红。
3. 注入必须是**合成改写**（与历史修复相似度 < 0.6），不做"把历史修复反向打回去"。
4. **本仓库公开 = 题目作废**。`packs/core/tasks/*/reference/fix.patch` 是标准答案，
   `hidden/tests_hidden/` 是评分用例。如果要把它作为公开作品展示，建议：
   - 只公开 `console/` + `设计文档.md` + `packs/core/README.md`（引擎与规范），
     `packs/core/tasks/` 整体私有；或
   - 公开题库但**先移除 `reference/` 与 `hidden/`**（题目将失去自验能力，仅作样例）。
5. 受测仓库（`repo_root` 指向的项目）是**独立仓库**，不在本仓库内，也请勿把它拷进来。

---

## 9. 校准纪律（硬规则）

**出题者不做盲测校准。** `calibration/results.json` 在盲测完成前保持空表
（`blind_runs.rows: []`、`summary` 全 `null`），`calibrated` 恒为 `false`，meta 与 results
两处一致；`packcheck` 会拦截任何"作者自填校准数据"的形态。盲测由后续组织的**非出题模型**
实例完成：T1–T3 每个样本只给第 1 级提示词，记录 pass@1；王者每个样本在同一沙箱中
逐轮校验并最多解锁三级提示，记录 pass@3 与 `rounds_used`。同一会话的提示轮不拆成独立样本，
并记录 Wilson 95% 置信区间（弱模型测下界 + 强模型测上界）。

---

## 10. 当前题库状态

十一道起步题（`packs/core/index.json` 的实测快照）：

| 题 | 档 | 状态 | packcheck | 锚解 | 半成品 | 注入态 ×20 |
| --- | --- | --- | --- | --- | --- | --- |
| T1-01 同一行行情三种口径 | 初 | active | 0 红 | 100 | 14.29 | 稳定 0 |
| T1-02 交易日历双端一致性 | 初 | active | 0 红 | 100 | 20.0 | 稳定 0 |
| T1-03 错误码链路 | 初 | active | 0 红 | 100 | 40.0 | 稳定 0 |
| T2-04 缺口四出口一致性 | 中 | active | 0 红 | 100 | 33.33 | 稳定 0 |
| T2-05 同步准入与失活回收 | 中 | **draft** | **2 红** | 100 | 33.33 | 稳定 0 |
| T2-06 跨进程写仓与损坏暴露 | 中 | active | 0 红 | 100 | 33.33 | 稳定 0 |
| T2-07 协作取消 | 中 | active | 0 红 | 100 | 20.0 | 稳定 0 |
| T3-08 归档三难 | 高 | **draft** | **2 红** | 100 | 14.29 | 稳定 0 |
| T3-09 实时快照仲裁 | 高 | active | 0 红 | 100 | 28.57 | 稳定 0 |
| **T3-10 跨进程写仓 + 攒批 + 服务健康** | 高 | active | **0 红** | **100** | **37.5** | **稳定 0（20/20）** |
| **T4-11 实时行情与写入链路同时失去唯一事实** | 王者 | **draft** | 0 红 | **100** | **27.78** | **稳定 0（20/20）** |

复算：`python packs/core/tools/packcheck.py --repo "<受测仓库>"`（全量）
→ 当前 **11 题，红 4 项**；`python packs/core/tools/regenerate_index.py` 会按实测刷新本表。

---

## 11. 已知遗留

| # | 遗留 | 说明与建议 |
| --- | --- | --- |
| 1 | **T2-05 的 2 项 packcheck 红** | `allowed_paths` 只 2 个文件（medium 档要求 ≥3）、`coherence` 只 1 条断言。需扩注入面或补写跨出口一致性用例并重跑该题三级门禁 |
| 2 | **T3-08 的 2 项 packcheck 红** | `coherence` 只 1 条断言（需补新用例）；`参考解` 判据用**文件集合**比较，而该题锚解只落一个文件、单元素集合不存在真子集——结构上无法满足，要么把锚解重构成跨两文件，要么把判据改成"改动行数"比较 |
| 3 | **校准全部未做** | `blind_runs` 均为空表（§9 纪律），目标带待非出题模型盲测回填 |
| 4 | **王者 T4-11 尚未校准** | 题包门禁已完成，但 `pass_at_3` 仍需非出题模型完成独立三轮会话盲测；在此之前保持 `draft` / `calibrated=false` |
| 5 | **成题脚本含绝对路径** | `runs/blind/tools/author_t0*.py` 顶部硬编码 `D:\new model test` / `D:\New project 6`（历史存档）。`packgate.py`、`console/config.json` 已改为可配置/相对路径 |
| 6 | **harness 侧的其它待跟进** | 见 `console/harness/NOTES.md` 第三节（uv/pnpm 未接、并行校验无跨进程磁盘锁、大 monorepo 全树哈希偏慢） |
| 7 | **同端口可能并存两个服务实例** | Windows `SO_REUSEADDR` 允许两个进程都 `LISTEN` 成功，请求随机分流到新旧代码，表现为"接口时好时坏/偶发 500"。排障先看 `netstat -ano \| findstr :8899` 是否只有一个 LISTENING 的 pid（本轮就踩过） |
| 8 | **跑批沙箱默认不留存** | `auto_release=True` 时在评分结束后回收目录；报告、diff 和对话记录保留。需要保留目录时可由 API 设置 `auto_release=False` |

---

## 12. 维护与整理

```powershell
# 全量体检 + 压成红项清单
cd packs\core\tools
python packcheck.py --repo "D:\New project 6" --out _work\packcheck.json
python packcheck_summary.py _work\packcheck.json

# 按实测重算题目登记表（status 只给零红项题目标 active）
python regenerate_index.py --repo "D:\New project 6"

# 工作区整理（先看再删；自动跳过正在被门禁使用的评分树）
cd ..\..\runs\blind\tools
python tidy_workspace.py --root "D:\new model test" --dry-run
python tidy_workspace.py --root "D:\new model test"
```

---

## 13. 自检与验证

```powershell
# 引擎自带用例（沙箱/快照/评分/记录/自检/题包读侧/注入应用器/裁剪完整性/并发跑批；数量见最新验收记录）
python -m pytest console\harness\tests -q

# 静态自检（设计文档 §10.7）：前端禁用写法 + 危险删除命令
python console\harness\selfcheck.py
# 等价 API：POST /api/selfcheck；设置页也有入口

# 题包体检（见 §12）
python packs\core\tools\packcheck.py --repo "D:\New project 6"
```

历史验收曾记录引擎用例 **129 passed**、静态自检无错误；这些数字属于重构前快照，
不代表当前版本。此次重构的验证范围与结果见 `console/harness/VALIDATION.md`。
真实难度校准未执行，已有题包门禁记录不能当作真实模型调用记录。

### 已修的真实缺陷

**（2026-09-30）四个「不报错、只是结果错」的读侧缺陷** —— 详见
[`console/harness/NOTES.md`](console/harness/NOTES.md) 第四节。它们此前一直没被发现，
因为都是"静默走偏"而不是抛错：

1. **注入补丁读错目录**：真实题包的补丁在 `inject/patches/`，代码只读 `patches/`
   → 返回空 → 题目在**未注入的干净代码**上评测（`injected: 0`）。
2. **隐藏用例少了 `hidden/` 前缀**：`groups.json` 写 `tests_hidden/x.py::t`，
   评分树里的真实路径是 `hidden/tests_hidden/x.py::t` → pytest 收集到 **0 个用例**
   → 147 条 p2p 白名单全部误报为回归 → 每轮强制 0 分。
3. **裁剪切坏源文件**：多行装饰器（`@pytest.mark.parametrize(...)`）只删了函数体、
   留下悬空装饰器 → 文件语法错误 → 整批用例收集失败。
4. **`git apply` 静默假成功**：骨架目录还没有自己的 `.git`，`git -C` 向上找到评测台
   自己的仓库 → git 打印 `Skipped patch` 但**退出码仍是 0** → `injected: 3`
   而一个字符都没改（实测零改动拿 85.7 分）。

修后实测：**注入态 0 分且 6 个目标组全红**、**锚解 6 个组全绿**、p2p 零误报 ——
与 §6 的入库门槛语义一致。

**历史问题（文件夹沙箱迁移后已退役）**：`sandbox.list_subst()` 曾按错码页解码。原先用
`GetConsoleOutputCP()` 当 `subst` 输出的码页。`启动.cmd` 会先 `chcp 65001`，此后该 API
返回 65001，而 `subst.exe` 写到管道时仍按 ANSI 码页（简中 936）编码 —— 中文名沙箱的路径
被解成乱码，越界检测误报「盘符指向别处，请重建沙箱」，连带 20 条引擎用例变红。当时改为
**ANSI → OEM → UTF-8 → GBK** 候选码页逐个 strict 解码。细节见
[`console/harness/NOTES.md`](console/harness/NOTES.md) 第四节。

> **排障提示**：若接口"时好时坏 / 偶发 500"，先确认 8899 端口上是不是**并存了两个服务实例**。
> Windows 的 `SO_REUSEADDR` 允许两个进程都 `LISTEN` 成功，请求会被随机分流到新旧两套代码：
> `netstat -ano | findstr :8899` 应当只有一个 LISTENING 的 pid。

---

## 14. 声明

- 本项目**未附许可证文件**；如需开源请自行选择并补 `LICENSE`。
- 受测仓库（A 股回测系统）是独立项目，其代码、数据与依赖**不包含**在本仓库内。
- `console/config.json` 与部分出题脚本含本机绝对路径，换机器按 §4/§11 调整即可。
- 夹具目录 `console/harness/tests/fixtures/mini_repo/` 里故意放了 `docs/`、`.reference/`、
  `运行产物/`、`node_modules/` 与点名不变量的 `AGENTS.md`/`CHANGELOG.md`，用来验证快照
  白名单、脱敏与泄漏兜底。该目录**自带一份 `.gitignore`**（也是夹具的一部分），会忽略
  `node_modules/` 与 `运行产物/`，所以这两个目录是用 `git add -f` 强制入库的 —— 克隆后
  请勿删除，否则 `test_sandbox.py` 会红。
