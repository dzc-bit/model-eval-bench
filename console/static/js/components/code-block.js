/**
 * code-block.js — 代码 / 提示词展示块（§11.1）
 *
 * 状态清单：静态 | 选中（用户手动选中文本时高亮提示）| 提供全选复制。
 * 键盘路径：`<pre tabindex="0">` 可 Tab 到，浏览器会把整块内容作为可滚动区域处理；
 *   「全选复制」按钮可 Tab 到并 Enter 触发。
 * ARIA 要点：`role="region"` + `aria-label`（说明这块是什么），
 *   预内容用等宽字体 + `overflow-x: auto`（200% 缩放不破版，§12.14）。
 *
 * 依赖：core/dom.js、components/copy-button.js
 * 导出：createCodeBlock(props) → { el, update, destroy, getText, getPre }
 */

import { el, setText } from '../core/dom.js';
import { createCopyButton } from './copy-button.js';
import { S } from '../core/strings.js';

/**
 * 创建代码块。
 *
 * @param {{
 *   title?: string, text?: string, ariaLabel?: string,
 *   copyLabel?: string, showCopy?: boolean, maxHeight?: string
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, getText: Function, getPre: Function}}
 */
export function createCodeBlock(props = {}) {
  let current = { ...props };

  const titleNode = el('span', { class: 'code-block__title' }, current.title || '');
  const copyBtn = createCopyButton({
    label: S.ACTION_SELECT_ALL,
    copiedLabel: `${S.ACTION_SELECT_ALL}✓`,
    size: 'sm',
    variant: 'ghost',
    getText: () => getText(),
    sourceEl: () => pre,
    successMessage: () => S.COPY_OK,
  });

  const head = el('div', { class: 'code-block__head' }, titleNode, el('span', { class: 'u-spacer' }), copyBtn.el);
  const pre = el('pre', { class: 'code-block__pre', tabindex: '0' });
  const code = el('code', {}, '');
  pre.appendChild(code);

  const root = el('div', { class: 'code-block', role: 'region' }, head, pre);

  /**
   * 当前文本。
   * @returns {string}
   */
  function getText() {
    return current.text || '';
  }

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    setText(titleNode, current.title || '');
    if (current.title) head.hidden = false;
    else head.hidden = true;
    copyBtn.update({
      label: current.copyLabel || S.ACTION_SELECT_ALL,
      getText: () => getText(),
    });
    copyBtn.el.hidden = current.showCopy === false;
    setText(code, getText());
    if (current.ariaLabel) root.setAttribute('aria-label', current.ariaLabel);
    else if (current.title) root.setAttribute('aria-label', current.title);
    if (current.maxHeight) pre.style.maxHeight = current.maxHeight;
  }

  update({});

  return {
    el: root,
    update,
    getText,
    /** 给 copy-button 的第三级降级用。 */
    getPre: () => pre,
    /** 解绑复制按钮。 */
    destroy() {
      copyBtn.destroy();
    },
  };
}
