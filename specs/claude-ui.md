# Claude 风格 UI 验收清单

> **规范原件在仓库里**：`specs/claude-DESIGN.md`（20KB，来自 github.com/nexu-io/open-design
> 的 plugins/_official/design-systems/claude/DESIGN.md）。本文件只是把它翻译成
> 本项目可执行的逐条验收项。**两者冲突时以原件为准，并回来更新本文件。**
> 注意：stylepilot 那个网页没有渲染出组件章节，别拿网页当依据。


给 AI 看的实现规格。人看的说明写在 README 和帮助页，这里只放色值、尺寸、逐组件判定标准。

规范来源：stylepilot `styles/claude`。落地位置：`console/static/css/tokens.css`（令牌）、
`components.css`（16 个组件）、`views.css`（8 个视图）。

---

## 0. 全局基调（违反任何一条即判不合格）

| 项 | 要求 |
| --- | --- |
| 容器 | **禁止卡片套卡片**。一层容器到底，内层用分隔线/留白分组，不再加边框 |
| 边框 | 只在"需要被读作一个独立面板"时用。列表行、字段、分组一律不用边框 |
| 强调 | 一屏内实心强调按钮**最多 1 个**。重复动作用幽灵样式 |
| 留白 | 面板内边距 ≥24px；分组之间 ≥20px。宁可高，不可挤 |
| 层级 | 靠"字号 + 字重 + 颜色深浅"分层，不靠边框和阴影堆 |
| 装饰 | 禁止高光扫过、渐变按钮、彩色左侧竖条、发光阴影 |
| 冷色 | `#3898ec` 只允许出现在焦点环。其它任何地方出现蓝色即判错 |

## 1. 令牌

浅色（默认）：底 `#f5f4ed` / 容器 `#faf9f5` / 内容块 `#f7f6f0` / 沙 `#e8e6dc`
深色：底 `#262624` / 容器 `#30302e` / 内容块 `#383835` / 沙 `#30302e`
强调 `#c96442`，强调文字（浅底上）`#a8482b`，深色态强调 `#d97757`
正文 `#141413`，次要 `#4d4a44`，弱 `#6b675f`；深色对应 `#ece9e2 / #bdb7ac / #928b7f`

字体：标题 `--font-display`（思源宋体），正文 `--font-sans`，代码 `--font-mono`
字号：xs 12.5 / sm 14 / md 15 / lg 16.5 / xl 19 / 2xl 24 / 3xl 30
行高：正文 ≥1.6，标题 ≥1.45（CJK）
圆角：控件 8.5，内容块 12，面板 16，大容器 24
阴影：浅 `0 4px 24px rgba(20,20,19,.05)`，深 `rgba(0,0,0,.3)`

## 2. 逐组件判定标准

### .btn
- 主按钮：深炭实心（浅色态）/ 暖沙实心（深色态），文字用 `--color-primary-fg`。**无 ::after 高光**
- 次按钮：沙底 `--color-surface-3` + 正常文字色，无边框
- 幽灵按钮：透明底 + 赤陶文字，hover 才有 `--color-accent-soft` 底
- 内边距 `8px 16px`（sm 用 `4px 12px`），高度走 `--control-height-*`
- 同屏出现两个同义动作时，第二个必须降级为幽灵或折叠项

### .card / .panel
- `.panel`：章节容器。象牙底 + 1px 边框 + 16px 圆角 + 柔影，内边距 `24px 28px`
- `.card`：内容块。**无描边**，底色差分层，hover 才出现边框和阴影
- `.panel__head` 与内容之间用 1px 分隔线，不用第二个容器
- 禁止 `.panel` 里再放带边框的 `.card`

### .badge
- 描边从自身文字色 `color-mix()` 推导，禁止借用语义状态色（danger/warn/info）
- 档位四色必须同一饱和度同一明度，不能有一档看起来像报错

### .app-nav（侧栏）
- 分组：评测流程（任务库/排行榜/工作台/批量跑批/记分板）与配置（模型档案/设置/帮助）
  之间留 20px 间距 + 一条分隔线
- active：赤陶文字 + 3px 赤陶左条 + `--color-accent-soft` 底。禁止用 `--color-primary` 表达选中
- 内边距全部走令牌，**不允许出现写死的 10px/14px/34px**

### .task-card
- 标题衬线 19px 是主角；正文 15px；元信息 14px 弱色
- 底部分隔线用实线 1px `--color-border`，不用虚线
- 一卡内最多 1 个实心按钮

### .table
- 短中文列（状态词、行头）`white-space: nowrap`
- 长值列（路径、版本、哈希）单独一列，`word-break: break-all` + 等宽字体，不挤压其它列
- 表头 sticky，底色 `--color-surface-2`

### .field / 表单
- 输入框、下拉、文本域高度走 `--control-height-*`，边框 `--color-border`
- 焦点：`--color-focus-ring` 2px + 2px offset
- 标签与控件间距 8px，提示文字 13px 弱色
- 空值字段（全是 `—`）应折叠或隐藏，不占版面

### .empty-state
- **必须有一个能渲染出来的图形**，不能是"圆角方块里一个空心圆"
- 标题 16.5px + 说明 15px + 1 个动作按钮，垂直居中，上下留白 ≥32px

### .status-dot / 状态表达
- 形状 + 颜色 + 文字三重编码保留
- 状态词 `nowrap`

### 锚点跳转链接（工作台顶部）
- 不做成一行 5 个同权重赤陶链接
- 改成小型分段控件（segmented control）或收进"跳转"下拉，视觉权重低于面板标题

### 元信息（沙箱路径 / 基线哈希）
- 不挤在标题右侧
- 移到面板底部或独立"本轮信息"区，用 label/value 两列对齐

## 3. 验收方式

改完必须跑：
1. `python -m pytest console/harness/tests` —— 干净检出必须 exit 0
2. 前端语法：`console/static/js/**/*.js` 逐个复制成 `.mjs` 再 `node --check`
   （直接对 `.js` 跑 `node --check` 会静默放行 ESM 语法错误，等于没测）
3. 起服务后在浏览器里走一遍五个视图，看 console 有无 ReferenceError/TypeError；
   对比度按 WCAG AA 实测，量不到的项标 unresolved，不许当成「没问题」

> 原稿这里写的 `_ui-audit/run-matrix.sh`、`_ui-audit/aggregate.py` 测量台没有入库
> （一次性调试页 `_audit.html` 也已删除），所以验收步骤换成上面这三条可复现的。
3. 无头 Edge 出图，**浅色和深色各看一遍** 8 个视图
4. 逐条对照本清单第 2 节，写明"符合/不符合"

任何"采不到数据"都必须报错，不许当成通过。
