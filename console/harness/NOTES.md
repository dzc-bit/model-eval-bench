# harness 交付说明

写给接手的人：这里记的是「实现与设计文档对不齐的地方」和「还没做完的事」，
不是用法说明（用法见 `设计文档.md` 与 `启动.cmd`）。

历史验收基线：`python -m pytest console/harness/tests` 全绿（81 个用例，自造
迷你仓库与题包 fixture，不碰真实题包、不写受测仓库）。

---

## 一、接口偏差（与设计文档 §4/§5/§15/§16 相比）

### 1. 沙箱与记录目录的命名
设计文档 §16 字面写的是 `runs\<任务>\<模型>\<时间>\`。实际实现里
「模型」一段经过 `util.sanitize_id` 收敛（去掉 Windows 不允许的字符，
保留中文），因为模型名从 URL/表单进来，必须防路径穿越。沙箱目录同理，
按 `sandbox_root/<sanitize_id(run_id)>` 落盘。行为一致，只是目录段被安全化。

### 2. 越界强制判 0，但保留 raw_score
模型碰到 `violations`（改了 allowed_paths 之外的非噪音文件）时：
`score = 0`、`invalidated = true`，但 `raw_score` 保留「当作没越界时的真实分」，
前端可以用来展示「本来能拿 X 分」。设计文档只说了判 0，这个字段是新增的信息位。

### 3. 运行产物降级为「噪音」，不当越界
模型跑测试留下的 `__pycache__`、`.pytest_cache`、`*.log`、`.coverage` 之类，
不进 `violations`、不判 0，单列到 `result["noise"]` 里只提示。否则每次校验
都会因为解释器自带的缓存文件被判红。

### 4. 文件夹沙箱
当前版本直接使用 `sandbox_root` 下的绝对目录，不创建或自动回收盘符映射。
旧版 `allocate_drive` / `list_subst` 已退役；旧映射必须人工核实归属后处理。

### 5. script checker 的 parse 正则容错
题包作者写 `parse` 时，「结论词」与「用例名」的捕获顺序不固定
（`PASS name detail` 或 `name: PASS` 都出现过）。checker 不假定顺序：
先扫所有捕获组找落在 {ok/pass/passed/通过… / fail/failed/失败…} 词表里的那一组
当结论；都没有再看整行开头是不是结论词；再没有就退回「整行匹配即通过」。

### 6. 新增的便捷接口（§15 之外的补充，前端要用）
- `GET  /api/runs`                    — 运行记录列表
- `POST /api/runs/{id}/note`          — 改备注
- `POST /api/runs/{id}/diff`          — 取本轮 diff
- `GET  /api/calibration`             — 校准状态
- `POST /api/calibration/cancel`      — 取消排队
- `POST /api/selfcheck`               — 设置页一键静态自检

### 7. 校准排队是「懒物化」
校准排队只建 `runs/` 记录（状态 queued），不铺沙箱；
真正「跑一次」时才认领一个排队名额并当场铺沙箱（快照缓存此时已热）。
并发数由配置控制，不依赖盘符数量。

### 8. `docs_include` 默认 `["README.md"]`
白名单快照默认只带 README 进骨架；任务包要更多文档就在 meta 里加。
`exclude_dirs` / `exclude_globs` / `secret_globs` 见 `console/config.json`。

### 9. 注入合并进「共享基线骨架」
`sandbox.apply_injection` 不在沙箱落地后单独跑，而是作为 snapshot 的
`injector` 钩子，让沙箱与评分树拿到同一份「已注入」的起点——
否则评分树里是未注入的干净代码，题目故意埋的缺陷会凭空消失。
骨架按「任务 + 白名单版本 + 注入补丁摘要」分目录缓存（`sandboxes/.snapshots`）。

### 10. 内置对话发送改成「立即回执 + 轮询」（§15 写的是同步）
`POST /api/runs/{id}/chat` 不再等模型这一轮跑完：`chat.start_send` 登记发送计数、
起后台线程执行 `chat.send`，当场回 `{accepted, run_id, chat_busy: true}`；
前端拿到回执就转轮询 `GET .../chat`（`chat_busy` + 消息增量），结束后再刷新运行视图。

改的原因不是"超时值不够大"，而是这条请求**没有合理的超时**：一轮对话要连跑几十次
模型调用与工具执行（实测单轮 20 分钟、400+ 条记录），任何固定数字都会先被跑穿。
之前复用校验那档 300s，前端到期只会留下一个像失败的错，而服务端线程照旧在跑——
用户看到"报错"却不知道为什么模型还在动。同步等待还有第二个代价：HTTP/1.1 keep-alive
下这条连接被长请求独占，期间同连接上的后续请求服务端根本读不到，
用户连点的内容会在回合结束后一次性砸进对话（实测把三条「继续」一起塞进同一条对话）。

配套的两处：
- `timeouts.chat_s` 补进默认与校验（默认 600s）。它只管**单次模型调用**的网络超时，
  整轮对话不受任何时限约束——这是评测口径的一部分。原先 `chat.py` 读这个键但
  配置里没有、也不在合并名单上，靠代码内联的 180s 兜底。
- `_defaults` 把 `timeouts` / `grade` / `chat` 改成按字段合并。原先 `base.update(overrides)`
  整节替换，导致"新增一个默认项"就把已有 `config.json` 判成 `E_CONFIG_INVALID`
  （`chat_s` 一加进去，所有只写旧四项的配置全炸）。

`_ACTIVE_SENDS` 从 set 改成计数：一条消息在会话锁里排队时，前一条的收尾不能把
整体状态误报成空闲，否则前端会在还有未处理消息时解锁输入。

### 11. 越界判定：先告知边界，注释级改动不作废（§4.2 / §4.4 收紧前的口径）
§4.2 的字面口径是「allowed_paths 之外任何变化记 violations，命中即红」。实测把它
直接压到模型身上会毁掉测量效度：T2-05 一轮里模型只把无权文件 `models.py` 的
一行注释改写成三行（零语义变化），`raw_score 66.7` 就被判成 0，而模型**从未被告知**
哪些文件不能动——题面按 §6.5 脱敏纪律不能出现文件名，系统提示词当时也只说
「只能在工作区内写」。两处一起改：

- `_system_prompt` 每轮把 `allowed_paths` 原文告诉模型（读不受限，改动才判罚）。
  系统提示词是操作规程不是题目信息量，所以不违反 §6.5 第 2 条；纪律同步写在
  `packs/core/README.md` 第二节第 2 条与设计文档 §6.5。
- `_classify_violations` 增加第三类：`modified` 且该文件的 diff 正文**只有注释与空行**
  → 记提示不作废。判定保守：拿不准就算代码；`added` / `removed` 一律仍判红。
  为此 `build_diff_text` 现在先于越界检测执行（同一份 diff，不多跑一次 git）。

配套的用例侧纪律（§6.5 新增第 7 条）：隐藏用例不得断言"只有锚解才有的名字"，
改成形态断言。T2-05 的 `running_jobs` 是唯一一处（全包排查结论），已放宽为
「409 响应里查得到占用名额的任务编号」；注入态的异常文案只报数量不报编号，
所以三态门禁仍是 锚解 100 / 半成品 33.33 / 注入态 ×20 稳定 0。

---

## 二、自检覆盖（设计文档 §10.7）

`console/harness/selfcheck.py`，纯标准库。规则：

- **前端（`console/static/js`）**：innerHTML 拼业务数据、内联事件属性
  （含 `setAttribute('onclick', …)` 绕法与 `el.onclick = fn`）、
  `setInterval` 未配对 `clearInterval`、遗留 `console.log`、硬编码色值、
  `document.write`。
- **危险命令（`.py/.cmd/.bat/.ps1/.js/.mjs/.sh`）**：`del /s`、`git clean -fdx`。
  Python 文件用 `tokenize` 剔注释与字符串后扫描（规则说明里引用禁令不算违规），
  其余语言按行首注释符粗判。

CLI：`python console/harness/selfcheck.py`，退出码 0=通过 / 1=不通过，
设置页通过 `POST /api/selfcheck` 触发同一份报告。

---

## 三、未完成 / 待跟进

1. ~~**真实题包未联调**~~ —— **2026-09-30 已联调**：十道真实题包用
   `runs/blind/tools/packgate.py` 跑过三级门禁（锚解 / 半成品 / 注入态 ×20），
   `hidden/groups.json` 的用例命名、`p2p.json` 白名单、`inject/patches` 与
   `reference/*.patch` 的应用链路全部验证通过，原始输出在各题 `calibration/gate_*.json`。
   当前 8 题 packcheck 零红，T2-05 / T3-08 各有 2 项已知红项（见 `整理报告.md` 第五节）。
   > **同日补充**：console 侧的读链路上还有四个"假成功"缺陷，此前靠 `packgate.py`
   > 走自己的应用器（`selfgrade.apply_patch`）才没暴露。已全部修掉，见第四节。
2. ~~**前端页面**未做端到端验证~~ —— **2026-09-30 已用真实浏览器验证**：
   七个视图（任务库 / 工作台 / 批量跑批 / 记分板 / 模型 / 设置 / 帮助）逐个打开，
   无控制台报错、无 4xx（仅 `favicon.ico` 一条无害 404）；
   工作台与批量跑批都跑通了完整的"准备 → 校验 → 出报告"流程。
3. **uv / pnpm 等其它包管理器**未接。现在 Node 只探测仓库自带的
   `.tools/node-*` 与系统 PATH；若题包锁了别的包管理器，要在 checks 里加。
4. **并行校验互斥**：进程内用锁串行化运行状态与记录写入，跨进程（两个服务实例
   同时跑）没有做磁盘锁，目前假定单机单实例。
   > 另注（2026-09-30 实测）：同一端口上**并存两个服务实例**时，Windows 的
   > `SO_REUSEADDR` 会让两个进程都 `LISTEN` 成功，请求被随机分流到新旧两套代码上，
   > 表现为"接口时好时坏/500"。排查前先确认 `netstat -ano | findstr :8899`
   > 只有一个 LISTENING 的 pid。
5. **大型 monorepo 性能**：全树哈希越界检测在大仓库上是 O(N) 次 stat+sha256，
   旧版曾通过联接避开 node_modules；当前依赖使用实体副本，若受测仓库本身
   很大，校验前的 manifest 会变慢，届时可换成「只对白名单子树做哈希」。
   > 注意：`sandbox._tree_digest()`（判断补丁是否真的改了东西）也走全树清单，
   > 每个补丁前后各算一次；题包补丁数不多时可忽略，仓库再大需要一并优化。
6. **跑批的沙箱不留存**：`batch.auto_release=True` 跑完即删沙箱。
   API 可传 auto_release=False 保留目录；取消同样遵循该保留设置。

---

## 四、修复记录

### 2026-09-30 · 四个让"评测结果全错"的读侧缺陷 + 并发跑批

这一轮把"读不到 / 读错了却假装成功"的四类缺陷一次修掉，并补上并发跑批。
四个缺陷的共同特征：**不报错、只是结果错**，所以此前一直没人发现。

#### 1. 注入补丁读错目录（真实题包一个补丁都没打上）

- **现象**：真实题包的注入补丁在 `packs/core/tasks/<ID>/inject/patches/*.patch`
  （README §6 与出题工具 `inject_edits.py` 都写这里），而 `packs.list_patches()`
  只读 `<pack>/patches/`，返回空列表 → 校验日志打印「本题没有注入补丁，按原样骨架准备」，
  `run.json` 里 `injected: 0`。题目在**未注入的干净代码**上评测。
- **修法**：`packs.list_patches()` 按 `PATCH_DIRS = ("inject/patches", "patches")`
  依次探测，两个目录都在时以 `inject/patches` 为准**只取一份**（合并会让补丁被应用两次）。
  新增 `packs.find_patches_dir()` 供诊断用。

#### 2. 隐藏用例路径少了 `hidden/` 前缀（pytest 收集到 0 个用例）

- **现象**：`hidden/groups.json` 里的用例 ID 写成相对 overlay 层的
  `tests_hidden/x.py::t`，但 pytest 的 cwd 是评分树根，真实路径是
  `hidden/tests_hidden/x.py::t`。路径解析不了 → pytest **退出码 4、收集到 0 个用例**
  （`report-0.xml` 里 `tests="0"`）→ 连 p2p 白名单也一起被判
  "报告里没有任何用例记录" → **147 条既有用例全部误报为回归**，
  每轮 `invalidated=true`、得分强制 0。
- **修法**：`packs.load_hidden_for()` 把 group 的用例 ID 统一归一成"相对评分树根"的形态
  （`_qualify_node_id()` 补 `overlay_rel` 前缀；裸函数名、绝对路径、已带前缀的都不动），
  p2p 用例本来就相对树根写，保持不变。

#### 3. 裁剪补丁把源文件切成语法错误（多行装饰器只删了函数体）

- **现象**：`_py_block_spans` 用逐行缩进扫描找函数块，装饰器只认"上方紧邻且以 `@` 起头"
  的行。`@pytest.mark.parametrize(\n ... \n)` 这种**多行装饰器**的最后一行是 `)`，
  识别不到 → 装饰器留在文件里、函数体被删到 EOF → 文件以悬空装饰器结尾。
  pytest 收集即 `SyntaxError`，整批用例（含 p2p）全红。实测 `tests/test_engine.py`
  少了 91 行（1666 → 1575），末尾正是那个被切坏的装饰器。
- **修法**：`_py_block_spans()` 优先用 `ast` 拿权威行号
  （装饰器取 `decorator_list` 里最早的一行，`end_lineno` 作块尾），
  语法本身有问题时退回 `_py_block_spans_scan()`；兜底实现也改成
  **括号感知**的向上扫描（`_closes_before()`），两个实现对同一份合法源码结果一致。

#### 4. `git apply` 静默"假成功"（`injected: 3` 但一个字符都没改）

这是最隐蔽的一个，前三项修完才暴露出来。

- **现象**：骨架目录在注入阶段还没有自己的 `.git`，`git -C <骨架> apply <补丁>`
  会**一路向上找到评测台自己的 `.git`**（`D:\new model test\.git`）。补丁里的目标路径
  在那个仓库里不存在，git 只打印 `Skipped patch ... 0 files changed`，
  **退出码依然是 0**。旧实现只看退出码 → `applied += 1`，于是 `injected=3`
  而骨架是干净代码（实测 T1-01 零改动拿 85.7 分）。
- **修法**：`sandbox._apply_patches()` 不再依赖 git：
  1. 优先用**自带的内容匹配应用器** `_apply_unified()`（按 hunk 上下文往文件里打），
     不依赖任何 git 仓库；自带实现里也修了 **hunk 行号语义**——
     `-<start>` 是它在**原始文件**里的行号，不是打完前面 hunk 之后的行号，
     不累计增量就会把第二个 hunk 起错位置（`fix.patch` 的 `importer.py` 正是如此）；
  2. 自带应用器失败才退回 `git apply`，且用 `--directory` 限定根；
  3. 无论走哪条路，都用 `_tree_digest()` 比对**应用前后的全树摘要**，
     补丁必须真的改动文件，否则报 `E_SNAPSHOT_FAILED`，**绝不静默通过**。

修复后的实测（`runs/blind/tools` 的三级门禁语义，走 console 真实代码路径）：

| 状态 | 结果 |
| --- | --- |
| 注入态（模型零改动） | 得分 **0.0**，6 个目标组全红（1/3、0/2、0/2、0/2、1/5、0/2），`invalidated=false`、p2p 零误报 |
| 锚解（`reference/fix.patch`，5 个文件） | 6 个组**全绿**（3/3、2/2、2/2、2/2、5/5、2/2） |
| 半成品（`reference/partial.patch`） | 只落 1 个文件，按设计低于满分 |

#### 5. 历史实现：盘符版并发跑批（已由文件夹会话调度替代）

- **并发闸门 = 盘符池**。沙箱要独占盘符（Q/R/S），所以同时在跑的沙箱数天然等于盘符数。
  `batch.max_concurrency()` 把请求值夹到池子大小，池子为空也至少给 1。
- **自动回收**（`auto_release=True`，默认开）。盘符只有三个而跑批动辄十几条，
  不回收的话第 4 条就 `E_DRIVE_UNAVAILABLE`（**实测：4 条挂 1 条**）。
  跑完即销毁沙箱、释放盘符；`runs/<题>/<模型>/<时间>/` 的报告与 diff 全部保留，
  记分板口径不变。想留着沙箱手动改代码走工作台单轮流程。
- **盘符分配的容忍窗口**：`sandbox.allocate_drive(wait_s=...)` +
  `runs.create_run(wait_s=...)`。上一条回收与本条分配之间有毫秒级间隙，
  直接失败会让本该成功的条目无辜挂掉；跑批传 30 秒，单轮流程保持 0（立刻明确报错）。
- **逐条独立成败**：一条准备失败只记在该条的 `error` 上，整批继续。
- 落盘快照在 `sandboxes/_batches/<batch_id>/batch.json`，服务重启后仍能查到历史批次。
- 实测：2 题 × 2 模型 = 4 条，`concurrency=3` → **4/4 全部完成**，峰值并发正好 3。

### 历史排障（相关函数已退役）：`subst` 列表按错码页解码

- **现象**：`console/harness/tests` 有 20 条用例红（`test_sandbox` 4 条、`test_grade` 15 条、
  `test_scoreboard` 1 条）。真实使用中的表现是：模型名带中文时，校验前的越界检测
  报 `subst_mismatch`「盘符 Q: 现在指向别处，请重建沙箱」，而沙箱其实是好的。
- **根因**（`sandbox._oem_codepage`）：用 `GetConsoleOutputCP()` 当 `subst` 输出的码页。
  但 `启动.cmd` 开头就 `chcp 65001`，此后它返回 **65001**，而 `subst.exe` 写到管道时
  仍按 **ANSI 码页**（简中 = 936）编码。用 UTF-8 去解 GBK 字节 → 中文路径全成 `\ufffd`，
  与 Python 侧内部路径逐字符比较必然不等。
- **实测证据**：`subst Q: <中文目录>` 后，`subst` 的原始字节是
  `b'Q:\\: => D:\\...\\\xd6\xd0\xce\xc4...'`；`cp936` 解码得正确中文，
  `cp65001`/`utf-8` 解码得乱码。同机 `GetConsoleOutputCP()=65001`、`GetACP()=GetOEMCP()=936`。
- **修法**：`_subst_codepages()` 按 **ANSI → OEM → UTF-8 → GBK** 给候选码页，
  `_decode_subst()` 逐个 strict 解码（全失败才退 `util.decode_output` 的宽松解码）。
  不再依赖 `GetConsoleOutputCP()`。
- **回归**：修后 `python -m pytest console/harness/tests` → **81 passed**（0 红）；
  中文目录的 `list_subst()` 与内部路径逐字符一致。

---

## 五、外部 harness 调研与本项目取舍（2026-10-01）

给任务 2/3 当地基：这里只记「源码/文档里读到的机制 + 我们决定怎么用」，不是教程。
第一手来源是官方的 **`deepseek-ai/deepseek-harness`**（TypeScript 插件化 agent harness，
"Everything is a Plugin"，仓库自带 `.agents/notes/` 架构决策记录，逐条写「问题 / 决策 /
曾考虑的替代方案 / 后果」——正是我们要写的那种笔记的样板）。社区侧对照取 SWE-agent 系，
DeepSeek 官方 API 文档定死思维链的回传规则。

### 1. 来源（逐条可点开核对）

- 官方 harness：https://github.com/deepseek-ai/deepseek-harness
  - 轮次封闭不变式（每个会话事件都必须落在 `turn/start…turn/end` 之内，否则崩溃恢复
    会把合法事件当成残留丢掉）：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/architecture/2026-06-15-turn-enclosure-invariant.zh.md
  - 调用后压缩压力与上下文溢出恢复（压缩只在**已落盘**的边界做，绝不拆开 assistant 的
    工具调用批次与其结果；压缩无法证明有进展时保留服务商原始错误）：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/architecture/2026-07-10-after-call-compaction-pressure-and-overflow-recovery.zh.md
  - 路由模型上下文与压缩策略（上下文容量属于**模型适配器**：逐模型 `contextWindow` +
    适配器级 `defaultContextWindow`；容量报错会让压缩触发过晚（可避免的溢出）或过早
    （丢掉有用上下文））：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/architecture/2026-07-20-routed-model-context-and-compaction-policy.zh.md
  - 结构化错误分类（`code` 与 `message` 分离；结构化字段进会话日志供代码与回放使用，
    `deriveMessages` **不**把它暴露给模型，模型仍只看文本块）：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/bug-fix/2026-06-11-structured-error-taxonomy.zh.md
  - 工具 schema 属于提示词装配（`PromptAssembly {sections, tools}` 单一拦截点，工具过滤
    与提示词改写走同一条 waterfall）：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/architecture/2026-06-11-tool-schemas-in-prompt-assembly.zh.md
  - 压缩检查点用英语工程文体，且**必须原样保留字面量**（路径、命令、错误、标识符、签名）：
    https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/bug-fix/2026-07-31-english-compaction-checkpoints.zh.md
- SWE-agent 历史压缩：https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py
  （`LastNObservations`、`ClosedWindowHistoryProcessor`、`TagToolCallObservations`、`RemoveRegex`）
- mini-swe-agent 循环与预算：https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py
  （`cost_limit: float = 3.0`、`step_limit`、超预算时 `add_message` 记 exit 并停止）
- DeepSeek 思考模式与 `reasoning_content` 回传要求：
  https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
- DeepSeek 多轮对话（接口无状态，历史由客户端拼接）：
  https://api-docs.deepseek.com/zh-cn/guides/multi_round_chat
- DeepSeek-R1 README（`<think>` 起始符、温度 0.5–0.7、不给多轮历史管理规则）：
  https://github.com/deepseek-ai/DeepSeek-R1

### 2. 工具系统

| 别处怎么做 | 证据 | 本项目取舍 |
| --- | --- | --- |
| 工具粒度极粗：一个 `bash` + 一个编辑工具，靠命令本身完成读写 | mini-swe-agent `default.py` 只有 `execute_bash`/编辑动作 | **不采纳**：评测台要让模型少碰 shell，`list_files/read_file/write_file/run_command` 四个动词更好审计，也不给模型绕过命令白名单的口子 |
| 大输出「进历史前」先降级：旧 observation 换成一行 `Old environment output: (N lines omitted)` | `history_processors.py:LastNObservations` | **改造后采纳**：只压缩**往后续轮重发的历史工具返回**，当前轮刚拿到的结果保持全量（截断上限不变），否则模型下一步就没依据了 |
| 同一文件多次展示时，只保留**最后一次**的窗口，旧的降级为 `Outdated window with N lines omitted...` | `history_processors.py:ClosedWindowHistoryProcessor` | **采纳**：`read_file` 同一路径被反复读时，历史里只留最新一次的原文，旧的换成「已被后续读取覆盖（N 行省略）」 |
| 错误以「模型能照做的一句话」返回，不带堆栈 | SWE-agent / mini-swe-agent 的 observation 文案风格 | **已是本项目风格**（`{"error": "只允许运行 git、python/pytest…"}`），继续保持单键 JSON + 中文可操作文案，不加 traceback |
| 预算触发就停（`cost_limit`/`step_limit`） | mini-swe-agent `default.py` | **不采纳**：工具轮数不设上限是评测口径的一部分（模型自己收束），预算只用于**压缩**，不用于**掐断** |
| 错误分成「稳定 code + 面向人的 message」；结构化字段只进会话日志供代码与回放用，**不塞进模型历史**，模型仍只看文本 | deepseek-harness `structured-error-taxonomy` | **采纳（已是本项目形状）**：`errors.py` 就是 code/文案分离 + 前端按 code 查文案；据此工具轮展开体只给「几次报错」，报错原文不进界面也不重塞给模型 |
| 工具 schema 与 system prompt 当成同一次「装配」的产物（`PromptAssembly {sections, tools}`），工具过滤只是这次装配的一次重写 | deepseek-harness `tool-schemas-in-prompt-assembly` | **不采纳插件式 waterfall，采纳其结论**：本项目在 `_system_prompt()` + `TOOLS` 一处成对产出「模型被告知的能力」，不打算为可选插件机制再开一层抽象 |
| 文件能力走 capability seam、按会话绑定 cwd | deepseek-harness `filesystem-capability-seam` / `fs-per-session-cwd` | **采纳精神**：一次 run 的沙箱根就是能力边界（`_safe_path` +  realpath 复检 + 命令 cwd 限制），并把 git 的 `push/pull/fetch/ls-remote/archive` 也拦住——它们违反「不访问网络」这条对所有人一样的口径 |

安全红线（路径限制在沙箱内、命令白名单、超时、输出上限）在这份取舍里只加不减：
摘要化只发生在「重发给模型的历史」这一份视图里，`chat.jsonl` 与沙箱落盘不变。

### 3. 内容装配

- 接口无状态，历史必须由客户端拼：DeepSeek 官方多轮对话文档明确
  「服务端不记录用户请求的上下文」「需将之前所有对话历史拼接好后传递」。
  → **采纳**：现状就是客户端全量拼接，不动。
- `reasoning_content` 回传策略（关键，和交接单的猜测相反）：DeepSeek 思考模式文档写明
  「若请求**未携带 `tools` 参数**：`reasoning_content` 无需回传，即使传入也会被忽略」；
  「若请求**携带 `tools` 参数**：历史轮次的 `reasoning_content` 均应回传……后续所有请求中
  必须完整回传」，回传错了 API 直接 400。
  → **采纳（保持现状）**：工作台内置对话每一发请求都带 `tools`，所以历史里的
  `reasoning_content`/`reasoning` **继续原样带回**，不做丢弃也不做折叠。
  窗口裁剪时它也参与体积计算，但**不单独因为思维链长而把整轮裁掉**——这是任务 2 的约束。
- 官方 harness 的「轮次封闭不变式」：每个会话事件都必须落在某个 `turn/start…turn/end`
  之内，否则崩溃恢复会把合法事件当成残留丢掉。
  → **采纳**：本项目以「完整轮次」为唯一裁剪/压缩单位（`_group_rounds`），并要求
  `_history_for_api` 把配不上响应的 `tool_calls` 摘掉、把找不到调用者的 tool 消息丢掉——
  等价于「轮次闭合才允许出门」。
- R1 README 只要求每轮输出以 `<think>` 开头、温度 0.5–0.7，不给历史管理规则。
  → **不采纳**：harness 不替模型伪造思维链（服务商不返回就留空），与 AGENTS.md 的验收口径一致。

### 4. 上下文压缩

- `LastNObservations` 的两条硬规矩值得照搬：
  1) **第一条永不删**（他们的理由：第一条是 instance template，即任务本体）；
  2) **只删 observation 类消息**，删之前 `assert message_type == "observation"`，
     绝不让一个动作和它的结果被拆散。
  → **采纳**：映射到我们这就是「第一条 user（题目提示词）永远保留」+
  「只摘要/裁剪 tool 消息，assistant 的 `tool_calls` 与其 tool 响应必须同进同出」，
  否则 OpenAI 兼容接口会因为 tool 消息找不到对应 `tool_call_id` 直接 400。
- 他们自己的评估：「多数 SotA 模型上下文已经很够，这个历史处理器现在不一定需要」，
  但需要时是**降级而非整轮丢弃**。
  → **采纳**：本项目的 bug 正是「单轮 100+ 条消息 → 续轮整轮被裁 → 模型失忆重做」，
  修法按这条来：**先压缩（摘要化 tool 返回），压到还超预算才丢最老的整轮**，
  且最新一轮永远保留（哪怕它自己就超预算，只把它内部压小）。
- mini-swe-agent 的窗口是「按消息条数 + 成本」双约束。
  → **改造后采纳**：`config.json` 的 `chat` 节给两个闸门——`max_history`（条）
  与 `max_context_chars`（字符），任一先到就开始压缩；前端展示走**完整记录**，
  与发给模型的窗口是两套视图（`chat.messages()` 全量，`chat._model_history()` 受窗口约束）。
- 摘要后必须留下的信息：**write_file 的路径与字节数**（模型要知道自己改过哪些文件，
  否则第二轮会重复劳动或覆盖自己的成果）。
  → **采纳**：写类动作的摘要不参与「省略」，只允许正文降级。
- 落地时补的一处：历史轮 `assistant.tool_calls` 里的 `write_file.content` 参数同样降级成
  「路径 + 字节数」。实测 T2-05 那条 165 条消息的对话，原始上下文 764k 字符里最大的一块
  就是模型自己写过的文件正文——它们早已落盘，重发没有信息量，只有窗口成本。

- 官方 harness 只在**已落盘的边界**上做压缩（`agent/pre-step` 之后），并且明确「不能拆开
  assistant 的工具调用批次与其结果」；提供方也可能在给出 usage 之前就因窗口超限拒绝请求，
  此时要有一条窄的恢复路径，压缩证明不了有进展就保留原始错误。
  → **采纳**：`_model_history` 只在轮次边界动作，`_history_for_api` 兜底保证成对；
  压缩后仍超窗就不再自作主张，直接把服务商错误落进对话记录给用户看。
- 官方压缩检查点由**模型生成**（并要求逐字保留路径、命令、错误、标识符、签名），
  且要求回放的 system/工具/历史字节级一致以复用前缀缓存。
  → **改造后采纳**：「保留字面量」照做（`write_file` 的路径与字节数、`run_command` 的
  退出码与输出末尾都不参与省略）；「用模型生成摘要」**不采纳**——评测台要给所有模型同一套
  确定性规则，多花一次模型调用也徒增方差与成本；字节稳定这条也不采纳：我们每轮重写历史，
  服务商侧前缀缓存必然失效（SWE-agent 同一处也点了这个代价），与「续轮不失忆」相比可接受。
- 官方把上下文容量归到**模型适配器**（逐模型 `contextWindow` + 适配器级默认值），
  因为容量报错会让压缩触发过晚（本可避免的溢出）或过早（丢掉有用上下文）。
  → **暂不采纳为档案级覆盖**：本项目一次对话只绑一个档案，全局 `chat` 节已够用；
  换成小窗口模型时的正确动作是把 `max_context_chars` 调小，这一条写进 README §5 的提醒里。
  若以后同一批次里并跑多个不同容量的模型，再把它上移成模型档案字段（`MODEL_FIELDS` 加一项）。

> 与本项目架构冲突的一处：SWE-agent 系用 `message_type`/`tags` 给消息打标签来决定
> 保留什么，我们没有这层元数据（`chat.jsonl` 是 OpenAI 原始消息形态）。
> 这里不引入新字段（不改历史数据文件），改为**按 role + 工具名推断**：
> `tool` 消息按 `name` 分类，`write_file` 归「必须保留动作」，其余按行数降级。
