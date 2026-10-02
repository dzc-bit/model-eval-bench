/**
 * icons.js — 内联 SVG 线性图标工厂（§2 .empty-state / §0 禁止装饰）
 *
 * 职责：
 *   1. 给全站提供**真能渲染出来的图形**。空态以前用文字符号 `○` 冒充图标，
 *      读起来像坏掉的界面而不是设计，这里一次性补上一套几何线稿图标。
 *   2. 只用 `document.createElementNS` 建节点：`core/dom.js` 的 `el()` 走
 *      `createElement`（HTML 命名空间），塞不进 SVG 子元素，参照 result-mark.js 的做法。
 *   3. 无障碍纪律：形状只做辅助，语义全靠文字。默认 `aria-hidden="true"`；
 *      传 `label` 时才改为 `role="img" + aria-label`。
 *   4. 颜色纪律：一律 `currentColor`，从 CSS 继承。本文件**不出现任何字面色值**
 *      （selfcheck 的 hardcoded_color 会扫 js/）。
 *
 * 风格：24×24 网格、1.5 描边、圆头圆角、无填充、无阴影、无渐变。
 *
 * 依赖：无（不依赖 core/dom.js）。
 * 导出：createIcon(name, opts) → SVGSVGElement、DEFAULT_ICON、hasIcon、resolveIcon
 */

/** SVG 命名空间。 */
const SVG_NS = 'http://www.w3.org/2000/svg';

/** 未识别的图标名一律回落到它——空态不许出现"没有图形"。 */
export const DEFAULT_ICON = 'inbox';

/** 默认画布尺寸（属性尺寸，CSS 可以再放大/缩小）。 */
const BASE_SIZE = 24;

/**
 * 图标笔画表。
 * 每项是一组 `{ tag, ...属性 }`，属性值全是几何量（坐标/半径），不含颜色。
 * 需要着色时用 `currentColor` 关键字，不是色值字面量。
 * @type {Record<string, Array<Record<string, string|number>>>}
 */
const ICON_SPECS = {
  /* 空托盘：箱体的斜肩 + 中间的开口缺口（"还没有东西放进来"） */
  inbox: [
    { tag: 'path', d: 'M6.2 5.5 3 12.5v5a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5L17.8 5.5Z' },
    { tag: 'path', d: 'M21 12.5h-5.5L13.75 15.5h-3.5L8.5 12.5H3' },
  ],
  /* 文件夹：左上带标签凸起，单层轮廓 */
  folder: [
    { tag: 'path', d: 'M2 6.5a1.5 1.5 0 0 1 1.5-1.5h4.75l2 2.5H18a2 2 0 0 1 2 2v7.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 2 17z' },
  ],
  /* 条形图：L 形坐标轴 + 三根等高线宽的立柱 */
  chart: [
    { tag: 'path', d: 'M4 4v16h16' },
    { tag: 'rect', x: 8, y: 12, width: 2.5, height: 8 },
    { tag: 'rect', x: 12.5, y: 8, width: 2.5, height: 12 },
    { tag: 'rect', x: 17, y: 14, width: 2.5, height: 6 },
  ],
  /* 断开的插头：插脚 + 插头外壳 + 中间留一段断口的电线（"尚未连接"） */
  plug: [
    { tag: 'line', x1: 9, y1: 3, x2: 9, y2: 8 },
    { tag: 'line', x1: 15, y1: 3, x2: 15, y2: 8 },
    { tag: 'path', d: 'M6 8v1a6 6 0 0 0 12 0V8' },
    { tag: 'line', x1: 12, y1: 15, x2: 12, y2: 17.5 },
    { tag: 'line', x1: 12, y1: 19.5, x2: 12, y2: 22 },
  ],
  /* 搜索：圆环 + 45° 手柄 */
  search: [
    { tag: 'circle', cx: 10.5, cy: 10.5, r: 6.5 },
    { tag: 'line', x1: 15.2, y1: 15.2, x2: 20, y2: 20 },
  ],
  /* 警示：等腰三角（靠 stroke-linejoin 自然圆角）+ 感叹号 */
  alert: [
    { tag: 'path', d: 'M12 4.5l8.5 15h-17z' },
    { tag: 'line', x1: 12, y1: 10, x2: 12, y2: 14 },
    { tag: 'circle', cx: 12, cy: 16.8, r: 0.8, fill: 'currentColor', stroke: 'none' },
  ],
  /* 对勾：一笔成形 */
  check: [
    { tag: 'path', d: 'M4.5 12.5l4.75 4.75L19.5 6.5' },
  ],
  /* 时钟：表盘 + 两根指针 */
  clock: [
    { tag: 'circle', cx: 12, cy: 12, r: 8.5 },
    { tag: 'path', d: 'M12 7.25V12l3.5 2' },
  ],

  /* ---------- 侧栏导航图标（2026-10-01 侧栏重设计） ----------
     与视图语义一一对应，几何全部落在 24×24 网格的安全区内
     （描边外缘不出 3..21），线性 1.5 描边、圆头、无填充。 */

  /* 任务库：清单——三条任务线，第一条带完成对勾 */
  tasks: [
    { tag: 'path', d: 'M4.5 6l1.8 1.8L9.5 4.6' },
    { tag: 'line', x1: 13, y1: 6, x2: 20, y2: 6 },
    { tag: 'path', d: 'M4.5 12.5l1.8 1.8 3.2-3.2' },
    { tag: 'line', x1: 13, y1: 12.5, x2: 20, y2: 12.5 },
    { tag: 'line', x1: 4.5, y1: 19, x2: 20, y2: 19 },
  ],
  /* 排行榜：领奖台——三阶台座，冠军位居中抬高 */
  podium: [
    { tag: 'rect', x: 8.75, y: 4.5, width: 6.5, height: 6.5, rx: 1 },
    { tag: 'rect', x: 3.5, y: 11, width: 6.5, height: 8.5, rx: 1 },
    { tag: 'rect', x: 14, y: 13.5, width: 6.5, height: 6, rx: 1 },
  ],
  /* 工作台：对话气泡——圆角气泡 + 省略号三点 */
  chat: [
    { tag: 'path', d: 'M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v8a2.5 2.5 0 0 1-2.5 2.5H12l-4.6 3.4A.9.9 0 0 1 6 19.6V17h-.5A2.5 2.5 0 0 1 3 14.5z' },
    { tag: 'circle', cx: 8.2, cy: 10.5, r: 0.9, fill: 'currentColor', stroke: 'none' },
    { tag: 'circle', cx: 12, cy: 10.5, r: 0.9, fill: 'currentColor', stroke: 'none' },
    { tag: 'circle', cx: 15.8, cy: 10.5, r: 0.9, fill: 'currentColor', stroke: 'none' },
  ],
  /* 批量跑批：播放——右指三角，代表"一批一起跑" */
  play: [
    { tag: 'path', d: 'M7.5 5.2a1 1 0 0 1 1.53-.85l10 6.8a1 1 0 0 1 0 1.7l-10 6.8a1 1 0 0 1-1.53-.85z' },
  ],
  /* 记分板：表格计分——外框 + 表头线 + 两条计分列 */
  scoreboard: [
    { tag: 'rect', x: 3.5, y: 4.5, width: 17, height: 15, rx: 2 },
    { tag: 'line', x1: 3.5, y1: 9.5, x2: 20.5, y2: 9.5 },
    { tag: 'line', x1: 12, y1: 9.5, x2: 12, y2: 19.5 },
    { tag: 'line', x1: 3.5, y1: 14.5, x2: 12, y2: 14.5 },
  ],
  /* 模型档案：芯片——本体 + 引脚，"模型服务"的标准图形 */
  chip: [
    { tag: 'rect', x: 7, y: 7, width: 10, height: 10, rx: 1.5 },
    { tag: 'rect', x: 10.25, y: 10.25, width: 3.5, height: 3.5, rx: 0.75 },
    { tag: 'line', x1: 9.5, y1: 3.5, x2: 9.5, y2: 7 },
    { tag: 'line', x1: 14.5, y1: 3.5, x2: 14.5, y2: 7 },
    { tag: 'line', x1: 9.5, y1: 17, x2: 9.5, y2: 20.5 },
    { tag: 'line', x1: 14.5, y1: 17, x2: 14.5, y2: 20.5 },
    { tag: 'line', x1: 3.5, y1: 9.5, x2: 7, y2: 9.5 },
    { tag: 'line', x1: 3.5, y1: 14.5, x2: 7, y2: 14.5 },
    { tag: 'line', x1: 17, y1: 9.5, x2: 20.5, y2: 9.5 },
    { tag: 'line', x1: 17, y1: 14.5, x2: 20.5, y2: 14.5 },
  ],
  /* 设置：滑杆——两根轨道各带一个旋钮（旋钮是轨道上的圆环），比齿轮轻 */
  sliders: [
    { tag: 'line', x1: 4, y1: 8, x2: 20, y2: 8 },
    { tag: 'circle', cx: 9.5, cy: 8, r: 2.4 },
    { tag: 'line', x1: 4, y1: 16, x2: 20, y2: 16 },
    { tag: 'circle', cx: 14.5, cy: 16, r: 2.4 },
  ],
  /* 帮助：圆内问号 */
  help: [
    { tag: 'circle', cx: 12, cy: 12, r: 8.5 },
    { tag: 'path', d: 'M9.6 9.8a2.4 2.4 0 1 1 3.4 2.2c-.7.35-1 .8-1 1.5v.3' },
    { tag: 'circle', cx: 12, cy: 16.4, r: 0.9, fill: 'currentColor', stroke: 'none' },
  ],
  /* 加号（CTA 用）：两笔等长交叉 */
  plus: [
    { tag: 'line', x1: 12, y1: 5, x2: 12, y2: 19 },
    { tag: 'line', x1: 5, y1: 12, x2: 19, y2: 12 },
  ],
  /* 锁（侧栏脚注用）：锁体 + 锁梁 */
  lock: [
    { tag: 'rect', x: 5.5, y: 10.5, width: 13, height: 9, rx: 2 },
    { tag: 'path', d: 'M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5' },
  ],

  /* ---------- 工作台状态栏图标（2026-10-02 四改：运行详情 / 本轮备注收成图标 + 小窗口） ---------- */

  /* 运行详情：圆环里一个 i（信息），一竖 + 一点 */
  info: [
    { tag: 'circle', cx: 12, cy: 12, r: 8.5 },
    { tag: 'line', x1: 12, y1: 11, x2: 12, y2: 16.5 },
    { tag: 'circle', cx: 12, cy: 8, r: 0.9, fill: 'currentColor', stroke: 'none' },
  ],
  /* 本轮备注：纸页（右上折角）+ 两条正文线 */
  note: [
    { tag: 'path', d: 'M5.5 4.5h8L19 10v9.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1z' },
    { tag: 'path', d: 'M13.5 4.5V10H19' },
    { tag: 'line', x1: 8, y1: 13.5, x2: 16, y2: 13.5 },
    { tag: 'line', x1: 8, y1: 17, x2: 13.5, y2: 17 },
  ],
};

/**
 * 这个名字有没有对应图形。
 * @param {unknown} name
 * @returns {boolean}
 */
export function hasIcon(name) {
  return typeof name === 'string' && Object.prototype.hasOwnProperty.call(ICON_SPECS, name);
}

/**
 * 名字 → 实际渲染的名字：认不出的回落到 DEFAULT_ICON，不渲染空壳。
 * @param {unknown} name
 * @returns {string}
 */
export function resolveIcon(name) {
  return hasIcon(name) ? name : DEFAULT_ICON;
}

/**
 * 建立 SVG 节点并批量落属性（属性名不带命名空间，直接 setAttribute 即可）。
 * @param {string} tag
 * @param {Record<string, string|number>} [attrs]
 * @returns {SVGElement}
 */
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    node.setAttribute(key, String(value));
  });
  return node;
}

/**
 * 创建一个图标。
 *
 * 尺寸优先用 CSS（外层类名控制 `width/height`），`size` 只是给没有 CSS 时的兜底。
 *
 * @param {string} [name] 图标名（见 ICON_SPECS）；认不出来的名字回落 inbox
 * @param {{
 *   size?: number, class?: string, label?: string
 * }} [opts] `label` 存在时图标不再 aria-hidden，改由 aria-label 承载语义
 * @returns {SVGSVGElement}
 */
export function createIcon(name, opts = {}) {
  const resolved = resolveIcon(name);
  const size = Number(opts.size) > 0 ? Number(opts.size) : BASE_SIZE;
  const label = typeof opts.label === 'string' ? opts.label.trim() : '';

  const root = svg('svg', {
    class: opts.class || `icon icon--${resolved}`,
    viewBox: '0 0 24 24',
    width: size,
    height: size,
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': 1.5,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    focusable: 'false',
    'aria-hidden': label ? 'false' : 'true',
  });

  if (label) {
    root.setAttribute('role', 'img');
    root.setAttribute('aria-label', label);
  }

  (ICON_SPECS[resolved] || []).forEach((spec) => {
    const { tag, ...attrs } = spec;
    root.appendChild(svg(tag, attrs));
  });

  return root;
}
