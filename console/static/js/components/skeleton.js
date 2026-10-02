/**
 * skeleton.js — 骨架屏（§11.1 / §10.5 / §11.2 #6）
 *
 * 状态清单：静态（只做展示，无交互）。
 * 键盘路径：不可聚焦（避免 Tab 停在假内容上）。
 * ARIA 要点：
 *   - `aria-hidden="true"`：骨架不是真内容，读屏不应读它。
 *   - 外层容器带 `aria-busy="true"` + 中文「正在加载」，让读屏知道在等。
 *   - 动画由 CSS `prefers-reduced-motion: reduce` 统一收敛（§12.13）。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createSkeleton(props) → { el, update, destroy }
 */

import { el } from '../core/dom.js';
import { S } from '../core/strings.js';

/**
 * 创建骨架屏。
 *
 * @param {{rows?: number, variant?: 'row'|'text'|'card'|'title', label?: string, width?: string}} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createSkeleton(props = {}) {
  let current = { ...props };
  const inner = el('div', { class: 'skeleton-stack', 'aria-hidden': 'true' });
  const root = el(
    'div',
    { class: 'u-stack', 'aria-busy': 'true' },
    el('span', { class: 'visually-hidden' }, current.label || S.STATE_LOADING),
    inner,
  );

  /**
   * 渲染骨架块。
   */
  function render() {
    const rows = current.rows || 3;
    const variant = current.variant || 'row';
    inner.textContent = '';
    for (let i = 0; i < rows; i += 1) {
      const piece = el('div', { class: `skeleton skeleton--${variant}` });
      if (current.width) piece.style.width = current.width;
      inner.appendChild(piece);
    }
  }

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    const changed =
      patch.rows !== undefined || patch.variant !== undefined || patch.width !== undefined;
    current = { ...current, ...patch };
    if (patch.label) {
      const labelNode = root.querySelector('.visually-hidden');
      if (labelNode) labelNode.textContent = current.label;
    }
    if (changed) render();
  }

  render();

  return {
    el: root,
    update,
    /** 纯展示组件，无需解绑。 */
    destroy() {},
  };
}
