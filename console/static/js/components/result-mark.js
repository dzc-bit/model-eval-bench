/**
 * result-mark.js — 成功 / 失败 / 部分成功的结果标记（内联 SVG + 描边动画）
 *
 * 职责：
 *   1. 校验结束后在得分旁给一个「一眼看懂」的表意：对勾 / 叉 / 半环。
 *   2. 用描边动画（stroke-dashoffset）把符号"画"出来，增加完成感与表现力。
 *   3. 严格遵守无障碍纪律：颜色**不单独**承载语义——符号形状 + 文字同时给出，
 *      SVG 本体 `aria-hidden`，语义由相邻文字与 `role="img"` 的标签承载。
 *   4. 尊重 `prefers-reduced-motion`：动画由 CSS 收敛为静态，不改 JS 逻辑。
 *
 * 实现说明：`core/dom.js` 的 `el()` 用 `createElement`（HTML 命名空间），
 * 对 `<svg>` 子元素无效，所以这里用 `createElementNS` 自建节点。
 * 不引第三方库、不写内联样式色值（颜色一律走 CSS 变量）。
 *
 * 依赖：core/strings.js（可选，仅取标签文案）
 * 导出：createResultMark(props) → { el, update, destroy, replay }
 */

/** SVG 命名空间。 */
const SVG_NS = 'http://www.w3.org/2000/svg';

/** 标记种类 → 语义（形状本身区分，不依赖颜色）。 */
export const MARK_KINDS = ['pass', 'fail', 'partial', 'busy', 'idle'];

/**
 * 建立 SVG 节点。
 * @param {string} tag
 * @param {object} [attrs]
 * @returns {SVGElement}
 */
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (v === null || v === undefined) return;
    node.setAttribute(k, String(v));
  });
  return node;
}

/**
 * 创建结果标记。
 *
 * @param {{
 *   kind?: 'pass'|'fail'|'partial'|'busy'|'idle',
 *   label?: string,
 *   size?: number,
 *   animate?: boolean,
 *   title?: string
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, replay: Function}}
 */
export function createResultMark(props = {}) {
  let current = {
    kind: 'idle',
    label: '',
    size: 56,
    animate: true,
    ...props,
  };

  /** 圆环（进度/背景环）。 */
  const ring = svg('circle', {
    class: 'result-mark__ring',
    cx: 50, cy: 50, r: 46, fill: 'none', 'stroke-width': 4,
  });
  /** 对勾路径。 */
  const check = svg('path', {
    class: 'result-mark__check',
    d: 'M28 52 L44 68 L74 34',
    fill: 'none', 'stroke-width': 7,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  });
  /** 叉路径（两笔）。 */
  const crossA = svg('path', {
    class: 'result-mark__cross',
    d: 'M34 34 L66 66',
    fill: 'none', 'stroke-width': 7, 'stroke-linecap': 'round',
  });
  const crossB = svg('path', {
    class: 'result-mark__cross',
    d: 'M66 34 L34 66',
    fill: 'none', 'stroke-width': 7, 'stroke-linecap': 'round',
  });
  /** 部分成功：半环（对勾仍在，但环只画一半）。 */
  const halfArc = svg('path', {
    class: 'result-mark__half',
    d: 'M50 6 A44 44 0 0 1 50 94',
    fill: 'none', 'stroke-width': 7, 'stroke-linecap': 'round',
  });
  /** 进行中：旋转弧。 */
  const spinArc = svg('path', {
    class: 'result-mark__spin',
    d: 'M50 8 A42 42 0 0 1 92 50',
    fill: 'none', 'stroke-width': 6, 'stroke-linecap': 'round',
  });

  const glyph = svg('g', { class: 'result-mark__glyph' });
  glyph.appendChild(ring);
  glyph.appendChild(halfArc);
  glyph.appendChild(spinArc);
  glyph.appendChild(crossA);
  glyph.appendChild(crossB);
  glyph.appendChild(check);

  const svgRoot = svg('svg', {
    class: 'result-mark__svg',
    viewBox: '0 0 100 100',
    width: String(current.size),
    height: String(current.size),
    role: 'img',
    'aria-hidden': 'true',
    focusable: 'false',
  });
  svgRoot.appendChild(glyph);

  const text = document.createElement('span');
  text.className = 'result-mark__label';

  const node = document.createElement('div');
  node.className = 'result-mark';
  node.appendChild(svgRoot);
  node.appendChild(text);

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    const kind = MARK_KINDS.includes(patch.kind) ? patch.kind : current.kind;
    current = { ...current, ...patch, kind };

    node.className = `result-mark result-mark--${kind}`;
    text.textContent = current.label || '';
    // 有文字时把标签挂到 svg 上，读屏能念出结论（形状本身 aria-hidden）
    if (current.label) {
      svgRoot.setAttribute('aria-label', current.label);
      svgRoot.setAttribute('aria-hidden', 'false');
    } else {
      svgRoot.removeAttribute('aria-label');
      svgRoot.setAttribute('aria-hidden', 'true');
    }
    if (current.size && Number(svgRoot.getAttribute('width')) !== current.size) {
      svgRoot.setAttribute('width', String(current.size));
      svgRoot.setAttribute('height', String(current.size));
    }
    if (current.title) node.setAttribute('title', current.title);
    else node.removeAttribute('title');

    // 不在"忙"的时候给动画类，避免校验中途一直转（由调用方决定何时 animate）
    node.classList.toggle('result-mark--animate', Boolean(current.animate));
  }

  /**
   * 重播一次描边动画（同一份结果重复校验后想要"再画一遍"的场合）。
   * 通过强制 reflow 重置 CSS 动画。
   * @returns {void}
   */
  function replay() {
    node.classList.remove('result-mark--animate');
    // 读一次布局属性强制 reflow，让移除的类立刻生效
    void node.offsetWidth;
    if (current.animate) node.classList.add('result-mark--animate');
  }

  update({});

  return {
    el: node,
    update,
    replay,
    /** 无外部监听，无需解绑。 */
    destroy() {},
  };
}

/**
 * 由校验报告推出标记种类。
 *
 * @param {{passed?: boolean, invalidated?: boolean, p2p_broken?: boolean, score?: number}} report
 * @returns {'pass'|'fail'|'partial'}
 */
export function kindForReport(report) {
  if (!report) return 'idle';
  if (report.invalidated || report.p2p_broken) return 'fail';
  if (report.passed) return 'pass';
  return 'partial';
}
