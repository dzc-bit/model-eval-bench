# harness 交付说明

写给接手的人：这里记的是「实现与设计文档对不齐的地方」和「还没做完的事」，
不是用法说明（用法见 `设计文档.md` 与 `启动.cmd`）。

验收基线：`python -m pytest console/harness/tests` 全绿（81 个用例，自造
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

### 4. 盘符池的「残留回收」
`allocate_drive` 在分配前会检查：`subst` 里有映射但目标目录已不存在的盘符
（上一轮崩在准备中途留下的），会当场回收再用，而不是直接报「盘符池已用尽」。
`list_subst` 解析 `subst` 的文本输出时用控制台 OEM 码页解码（中文系统 cp936），
否则中文路径会乱码、残留映射对不上。

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
校准排队只建 `runs/` 记录（状态 queued），不占盘符、不铺沙箱；
真正「跑一次」时才认领一个排队名额并当场铺沙箱（快照缓存此时已热）。
排 5 个沙箱不会把 Q/R/S 三个盘符挤爆。

### 8. `docs_include` 默认 `["README.md"]`
白名单快照默认只带 README 进骨架；任务包要更多文档就在 meta 里加。
`exclude_dirs` / `exclude_globs` / `secret_globs` 见 `console/config.json`。

### 9. 注入合并进「共享基线骨架」
`sandbox.apply_injection` 不在沙箱落地后单独跑，而是作为 snapshot 的
`injector` 钩子，让沙箱与评分树拿到同一份「已注入」的起点——
否则评分树里是未注入的干净代码，题目故意埋的缺陷会凭空消失。
骨架按「任务 + 白名单版本 + 注入补丁摘要」分目录缓存（`sandboxes/.snapshots`）。

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
4. **并行校验互斥**：进程内用锁串行化盘符池与记录写入，跨进程（两个服务实例
   同时跑）没有做磁盘锁，目前假定单机单实例。
   > 另注（2026-09-30 实测）：同一端口上**并存两个服务实例**时，Windows 的
   > `SO_REUSEADDR` 会让两个进程都 `LISTEN` 成功，请求被随机分流到新旧两套代码上，
   > 表现为"接口时好时坏/500"。排查前先确认 `netstat -ano | findstr :8899`
   > 只有一个 LISTENING 的 pid。
5. **大型 monorepo 性能**：全树哈希越界检测在大仓库上是 O(N) 次 stat+sha256，
   受测仓库（1.7 万文件 node_modules 走联接不进树）没问题，若受测仓库本身
   很大，校验前的 manifest 会变慢，届时可换成「只对白名单子树做哈希」。
   > 注意：`sandbox._tree_digest()`（判断补丁是否真的改了东西）也走全树清单，
   > 每个补丁前后各算一次；题包补丁数不多时可忽略，仓库再大需要一并优化。
6. **跑批的沙箱不留存**：`batch.auto_release=True` 跑完即删沙箱。
   真要做"跑批 + 事后人工进沙箱复看"，需要给批次条目加"保留最近 N 条"的策略。

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

#### 5. 新增：并发跑批（`harness/batch.py`）

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

### 2026-09-30 · `subst` 列表按错码页解码，中文名沙箱被误判为「环境损坏」

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
