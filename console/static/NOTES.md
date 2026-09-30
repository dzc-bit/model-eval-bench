# NOTES.md — 前端实现备忘（§11.2 / §12 / §15 契约缺口）

> 本文件只记录前端侧的实现决策与契约缺口。**共享文件（`console/server.py`、`console/harness/*`、`packs/*`）一律未改动。**
> 所有请求只打本机 `/api/*`，无第三方库、字体、CDN，无构建产物。

---

## 1. 逐条自查：§11.2 的 14 条坑

| # | 坑 | 落点 | 结论 |
|---|---|---|---|
| 1 | 旧响应覆盖新响应 | `core/poller.js`（`seq` / `lastAppliedSeq` 请求序号守卫）+ `core/api.js`（AbortController per 请求） | ✅ 序号不匹配直接丢弃；视图销毁时 `scope.cancelAll()` |
| 2 | 切页后请求还在飞 | `core/api.js` `createScope()`；每个视图 `create` 时 `api.scope()`、`destroy` 时 `cancelAll()` | ✅ `task-library` / `workspace` / `models` / `settings` / `scoreboard` 都遵守 |
| 3 | 轮询整表重建丢焦点 | `core/dom.js` `patchList()`（key 化复用，顺序不变时一个节点都不动） | ✅ 分组卡、任务卡、模型行、表格行全走 `patchList` |
| 4 | 按钮双击重复提交 | `components/button.js`：`loading` / `disabled` 期间 `click` 直接吞掉；视图层再加 `if (s.busy) return` | ✅ 双保险 |
| 5 | `innerHTML` 拼业务数据 | `core/dom.js` 只提供 `textContent` / 文本节点入口，`props.html` 会被忽略并 `console.warn` | ✅ 全站 0 处 `innerHTML`（见 §4 扫描） |
| 6 | 模态叠模态 | `components/modal.js` 单例栈 `stack`，同一时刻只允许一个浮层 | ✅ `openModal` 时若已有浮层则替换（`replaced`） |
| 7 | 表单无 label | `components/field.js`：`<label for>` 强制关联；`aria-describedby` 挂 hint + error | ✅ 所有输入控件都走 `field.js` |
| 8 | 只用颜色表意 | `components/badge.js` / `status-dot.js`：符号 + 文字 + 颜色三重编码 | ✅ 例如 `✓ 通过` / `✕ 未通过` / `● 正在准备` |
| 9 | 长操作无反馈 | `components/progress.js` + 心跳（`elapsed` 每秒 +1）+ 可折叠日志 | ✅ 准备/校验/清空/重建都有进度与已用时间 |
| 10 | 错误只有一句「失败」 | `core/strings.js` `errorTitle()` / `errorBody()`：发生 + 影响 + 下一步 | ✅ 未知码回落 `S_UNKNOWN_ERROR` 并保留原始 code |
| 11 | 焦点掉进弹层回不来 | `core/a11y.js` `trapFocus()`（同步聚焦 + 宏任务/一帧双通道重试）+ `restoreFocus()` | ✅ 关闭后焦点还原到触发元素 |
| 12 | 快捷键在输入框里也触发 | `core/a11y.js` `isEditableTarget()`；`main.js` `onGlobalKeydown` 先判 editable、组合键放行 | ✅ C/G/R/1/2/3/? 在输入框内失效 |
| 13 | 状态存在组件里刷新就丢 | `core/storage.js`（`localStorage` + 前缀 + 降级容错）；`run_id` / `last-task` / `last-model` / 偏好 / 滚动位置 | ✅ 刷新 `#/workspace/<id>` 回到同一视图与区域 |
| 14 | 轮询把用户正在打的字冲掉 | 备注只在 `run.run_id` 变化时才从服务端同步；select 选项只在集合真的变了时才重建（`field.js` `syncOptions` 按签名比对） | ✅ 打字与下拉选择都不被打断 |

## 2. 逐条自查：§12 的 16 条无障碍

| # | 条目 | 落点 | 结论 |
|---|---|---|---|
| 1 | 每视图一个 `h1`，路由切换聚焦 | 各视图返回 `el_h1`（`tabindex="-1"`），`main.js` 挂载后聚焦 | ✅ |
| 2 | 跳转链接 | `index.html`「跳到主内容」→ `#main-content` | ✅ |
| 3 | 地标 | `header` / `nav[aria-label]` / `main#main-content` / `footer` | ✅ |
| 4 | 焦点环不被去掉 | `css/base.css` 统一 `:focus-visible` 用 `--color-focus-ring` + offset | ✅ |
| 5 | 键盘可达：全部交互控件是原生元素 | `button` / `a` / `input` / `select` / `textarea` / `details>summary` | ✅ 无自造可点 div |
| 6 | tabs 用 roving tabindex | `components/tabs.js`：方向键 / Home / End，`aria-selected` / `aria-controls` | ✅ |
| 7 | 表格语义 | `components/table.js`：真 `<table>` + `aria-sort` + `scope` | ✅ 记分板、自检表 |
| 8 | 颜色不单独承载语义 | 符号 + 文字 + 颜色（见 §1 #8） | ✅ |
| 9 | live region 播报阶段推进 | `core/a11y.js` 单例 polite + assertive 两个 region；1.5s 去重节流 | ✅ |
| 10 | 浮层焦点陷阱 | `modal.js` + `trapFocus()` + 背景 `inert` + `aria-hidden` | ✅ |
| 11 | 图标有文字或 `aria-hidden` | 装饰符号一律 `aria-hidden="true"`，语义由相邻文字承载 | ✅ |
| 12 | 表单错误可见且关联 | `aria-invalid` + `aria-describedby` → error 节点；必填有 `aria-required` | ✅ |
| 13 | `prefers-reduced-motion` | `css/tokens.css` / `base.css`：动画时长归零 | ✅ |
| 14 | `prefers-contrast: more` | 提高边框与文字对比 | ✅ |
| 15 | 深色模式 | `prefers-color-scheme: dark` 全套 token 覆盖 | ✅ |
| 16 | 触达目标 ≥ 32px | `components.css` 按钮 `min-height`、间距走 token | ✅ |

## 3. 契约缺口（前端侧的应对，均未改共享文件）

1. **没有 `POST /api/sandbox/open`**。
   「打开沙箱目录」按钮不发请求（发了就是 404）。改为：复制沙箱路径 + toast 提示用户把它粘到文件管理器。
   落点：`views/workspace.js` `doOpenDir()`。

2. **没有 `GET /api/runs/{id}/report`**。
   报告不单独拉取——`run_view` 本身就带 `report`。「导出报告」在**前端本地**把 `run.report` 拼成 JSON，用 `api.download()`（Blob）落盘，零网络请求。
   落点：`views/workspace.js` `doExport()`。

3. **没有沙箱准备/清理日志接口**。
   `run_view` 的 `log` 只在 `grading / graded / error` 时有值（那是**校验**日志）。沙箱面板的日志改为记录**前端自己真实发过的每一步**（`opLog`：提交准备、沙箱就绪、清空、重建…，带本地时间戳），并在空态时说明这里记录的是本机操作轨迹。
   落点：`views/workspace.js` `logOp()`、`views/workspace/sandbox-panel.js` `renderLog()`。

4. **`POST /api/runs/{id}/diff` 返回 `{diff: "<文本>"}`**，不是对象。前端按字符串渲染进 `<pre>`。
   落点：`views/workspace/run-bar.js` `showDiff()`。

5. **模型密钥只写不读**：服务只存 `key_masked`，明文密钥由用户改 `console\config.json` 后重启服务。设置页/模型页文案已写明，前端不做密钥编辑框。

6. **档位取值口径**：后端 packs 返回 `primary / medium / hard`，设计文档与 mock 用 `easy` 指初级。前端 `core/strings.js` 提供 `normalizeTier()` 归一后参与筛选与徽章取词，`TIER_NAMES` / `TIER_GLYPHS` 两个键都认。
   （若后端将来统一成 `easy`，只需删掉别名，其余代码不动。）

7. **`POST /api/runs` / `POST /api/sandbox/reset` / `rebuild` 是同步阻塞的，`POST /api/runs/{id}/grade` 是异步的**。
   所以前端只有**校验**走轮询（`core/poller.js`）；准备/清空/重建按长请求处理（`api.longPost` / `api.post`），进度条来自本机心跳。
   mock（`core/mock.js`）据此只对 `/grade` 做时间推进模拟，`POST /runs` 直接返回 `ready`，避免前端出现一段假进度。

8. **批量跑批接口（2026-09-30 新增，后端 `harness/batch.py`）**。

   | 方法 | 路径 | 说明 |
   | --- | --- | --- |
   | POST | `/api/batches` | 起一批。体 `{items:[{task,model,attempt}], concurrency?}`，或更省事的 `{tasks:[], models:[], attempt?}` 做笛卡尔积 |
   | GET | `/api/batches` | 批次列表（内存 + `sandboxes/_batches/*/batch.json`），按时间倒序 |
   | GET | `/api/batches/{id}` | 单个批次进度：`{batch_id,status,concurrency,total,done,passed,running,items[],problems[]}` |
   | POST | `/api/batches/{id}/cancel` | 请求取消：已开跑的跑完当前一步，未开始的不再派发 |

   契约要点：
   - `status ∈ running | cancelling | finished | cancelled`；item 的
     `status ∈ pending | preparing | grading | graded | error | cancelled`。
   - `item.score` 只在 `graded` 时有值（可能是 `0`，别用 `||` 取默认值）。
   - **沙箱跑完即回收**：后端默认 `auto_release=True`，条目的 `run.sandbox` / `run.drive`
     会被清空。所以跑批的条目**不能**再进工作台接着改代码——要那种用法走单轮流程。
   - `problems[]` 是开工前就被判掉、根本没进队列表的条目（模型档案不存在、轮次超上限等）。

## 4. 交付相关的实现决策

- **`components/field.js` 是第 15 个组件**（§11.1 列了 14 个）：所有输入控件（含 select / textarea）的 label、hint、error、`aria-*` 关联都收敛在这一个组件里，否则 §12.12 会在每个视图重复一遍。
- **`components/result-mark.js` 是第 16 个组件**（2026-09-30 新增）：校验结束/跑批逐条的成败标记，内联 SVG + `stroke-dashoffset` 描边动画。**不依赖任何第三方库**，颜色全部走 CSS 变量（`prefers-reduced-motion` 由 CSS 收敛为静态）。`core/dom.js` 的 `el()` 用 `createElement`，对 `<svg>` 子元素无效，所以这个组件内部用 `createElementNS` 自建节点。语义纪律：形状本身区分（对勾 / 叉 / 半环 / 转圈），另有文字标签；`svg` 在无标签时 `aria-hidden="true"`，有标签时挂 `aria-label`。
- **`views/batch.js` 是第 7 个视图**（2026-09-30 新增，「批量跑批」）：`#/batch` 路由，把「多题 × 多模型」排进后端并发跑批，逐条轮询进度。并发数上限 = 盘符池大小（后端 `batch.max_concurrency` 会夹住）。
- **两个 live region 分开**：polite（阶段推进播报）与 assertive（错误）分开，避免一条慢播报顶掉一条错误播报；toast 又是独立宿主（`components/toast.js`），因为 toast 会被自动移除，而 live region 的文本要留给读屏。
- **innerHTML 纪律**：`grep -n "innerHTML\|insertAdjacentHTML\|outerHTML\|document.write" js/**/*.js` 命中 7 处，全部是注释或字符串常量，无一处真实调用。设计文档允许的「纯静态模板例外」本项目实测不需要，未使用。
- **mock（`core/mock.js`）**：URL 带 `?mock=1` 时 `core/api.js` 用 `await import('./mock.js')` 顶替传输层，**真实 fetch 代码路径一行不改**。mock 的运行记录存 `sessionStorage`（键 `evalconsole:mock:runs`），仅为让「刷新回到同一轮」这条验收在 mock 下也能演出来；真实后端本来就落盘。
- **剪贴板三级降级**（`components/copy-button.js`）：`navigator.clipboard.writeText` → `document.execCommand('copy')`（离屏 textarea）→ 选中源文本 + 手动 Ctrl+C。第三级在没有「对应源元素」时（比如「复制全部」是把两段拼起来的）会造一个**留在页面里**的只读 textarea 并全选聚焦，焦点离开后自动收掉——不能复制完就摘，否则选区跟着消失，提示就成了空话。
- **模态焦点时机**：`modal.js` 必须先把浮层 `appendChild` 进 DOM 再调 `trapFocus()`。对游离节点 `focus()` 会落到 `body` 上，插入后焦点不会自己回来。`trapFocus()` 先同步聚焦，失败再走宏任务 + 一帧两条重试通道——只押 `requestAnimationFrame` 的话，后台标签页 / 部分内嵌视图根本不发帧，焦点就永远进不去浮层。
- **`t()` 两种写法都支持**：`t('KEY', vars)` 与 `t(S.KEY, vars)` 等价（键名是 ASCII 大写下划线、文案是中文，不会撞车）。全站 80 处调用用的是 `t(S.KEY, vars)` 风格。
- **工作台四区域的排版（2026-09-30 调整）**：提示词 / 沙箱并排，校验区与**运行区**各占整行。运行区原先挤在半栏里，右侧留出 **668px 的空半行**（实测 1440 视口下 1320 网格只用了 652），而且它内部是横向条状信息（模型下拉 + 备注 + diff 统计），改成整行后内部走 `repeat(auto-fit, minmax(260px,1fr))` 多列，页面总高从 1782 → 1482，空半行归零。`.ws-region--run` 这个类名由 `run-bar.js` 挂在根节点上。

## 5. 验收记录（?mock=1）

| 步骤 | 结果 |
|---|---|
| 任务库选 T1-01 进入工作台 | ✅ 头部显示任务、档位、尝试指示灯、盘符/基线哈希事实 |
| 准备沙箱 | ✅ `POST /api/runs` → `Q:` + 沙箱路径 + 基线哈希，状态「沙箱就绪」，opLog 2 行 |
| 复制提示词（含接线说明） | ✅ 240 字符成功；权限被拒时第三级降级给出可手动 Ctrl+C 的内容 |
| 运行校验（G） | ✅ 实时进度「已用 00:0x」+ 可折叠校验日志 → 完成后按组给红绿与部分分 |
| 分组报告 | ✅ 5 组卡片、部分分 33.3、p2p/越界/相似度/diff/上一轮对比/执行详情、红组可展开失败摘要、下一步提示 |
| 清空改动（R） | ✅ 二次确认写明「评测记录不会被删除」；完成后 toast 提示可换模型从第 1 级重来 |
| 换模型重试 | ✅ 切到 `gpt-5-codex` 后重新校验，报告出现「新结果」标记 |
| 刷新 `#/workspace/T1-01` | ✅ 回到同一视图与区域（mock 用 sessionStorage，真实后端本来就落盘） |
| 真实后端（无 mock） | ✅ 任务库 10 题、档位筛选 初级 3 / 中级 4 / 高级 3、记分板 / 设置 / 帮助（动态 `import()`）全部正常 |
| 浏览器控制台 | ✅ 全程无报错、无警告 |
| 36 个 JS 文件 | ✅ `node --check` 全过、无 BOM/NUL 污染、具名 import ↔ export 全对得上（589 个文案键 0 缺漏） |
