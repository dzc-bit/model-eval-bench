/**
 * details-card.js — 可折叠卡片（§11.1）
 *
 * 状态清单：收起 | 展开。
 * 键盘路径：优先用原生 `<details>/<summary>`，Tab 到 summary、Enter / Space 展开收起，
 *   无需自造键盘逻辑，也不会出现 role 缺失。
 * ARIA 要点：原生语义自带 `aria-expanded`（由浏览器计算），summary 内必须有可读文字；
 *   折叠状态下的内容对读屏不可见，天然满足"隐藏内容不占 Tab 序列"。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createDetailsCard(props) → { el, update, destroy, setOpen }
 */

import { el, setText, clear } from '../core/dom.js';
import { S } from '../core/strings.js';

/**
 * 创建可折叠卡片。
 *
 * @param {{
 *   title: string, open?: boolean, content?: HTMLElement|string,
 *   hint?: string, flush?: boolean, ariaLabel?: string
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, setOpen: Function}}
 */
export function createDetailsCard(props = {}) {
  let current = { ...props };

  const marker = el('span', { class: 'details-card__marker', 'aria-hidden': 'true' }, '›');
  const titleNode = el('span', { class: 'details-card__title' }, current.title || '');
  const hintNode = el('span', { class: 'u-faint' });
  const summary = el('summary', {}, marker, titleNode, el('span', { class: 'u-spacer' }), hintNode);
  const body = el('div', { class: 'details-card__body' });
  const root = el('details', { class: `details-card${current.flush ? ' details-card--flush' : ''}` }, summary, body);

  /**
   * 渲染内容。
   */
  function renderContent() {
    clear(body);
    const content = current.content;
    if (content instanceof HTMLElement) body.appendChild(content);
    else if (typeof content === 'string') setText(body, content);
    body.hidden = !current.content;
  }

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    const wasOpen = root.open;
    const openChanged = patch.open !== undefined && patch.open !== wasOpen;
    current = { ...current, ...patch };
    setText(titleNode, current.title || '');
    setText(hintNode, current.hint || '');
    hintNode.hidden = !current.hint;
    renderContent();
    // 用户手动开合过之后，不再被 update 强行改回去
    if (patch.open !== undefined) root.open = Boolean(patch.open);
    else if (openChanged) root.open = wasOpen;
    if (current.ariaLabel) summary.setAttribute('aria-label', current.ariaLabel);
    const cls = `details-card${current.flush ? ' details-card--flush' : ''}`;
    if (root.className !== cls) root.className = cls;
  }

  update({ open: Boolean(props.open) });

  return {
    el: root,
    update,
    /** 展开 / 收起。 */
    setOpen(open) {
      root.open = Boolean(open);
    },
    /** 当前是否展开。 */
    isOpen: () => root.open,
    /** 内容容器（调用方往里塞节点）。 */
    bodyEl: body,
    /** 原生 details 无自定义事件绑定，无需解绑。 */
    destroy() {},
  };
}

/** 便捷：造一个「技术细节」折叠块（§13.7 原始错误信息默认折叠）。 */
export function detailsCard(label, content, options = {}) {
  return createDetailsCard({
    title: label,
    content,
    flush: true,
    hint: options.hint || S.ERROR_DETAIL_LABEL,
    ...options,
  });
}
