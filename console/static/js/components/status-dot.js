/**
 * status-dot.js — 状态点（在线 / 离线 / 异常，§11.1 / §12.8）
 *
 * 状态清单：ok（在线 / 通过）| warn（注意 / 进行中）| error（离线 / 失败）| busy（正在跑）| idle（未知）。
 * 键盘路径：不可聚焦（随所在段落的文字被读屏念出）。
 * ARIA 要点：**点 + 符号 + 文字三重编码**，颜色不单独承载语义（§12.8）。
 *
 * 依赖：core/dom.js
 * 导出：createStatusDot(props) → { el, update, destroy }, statusDot(kind, text)
 */

import { el, setText } from '../core/dom.js';

/** 状态 → 符号。 */
const GLYPHS = { ok: '✓', warn: '!', error: '✕', busy: '●', idle: '○' };

/**
 * 创建状态点。
 *
 * @param {{kind?: 'ok'|'warn'|'error'|'busy'|'idle', text?: string, title?: string}} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createStatusDot(props = {}) {
  let current = { ...props };

  const dot = el('span', { class: 'status-dot__dot', 'aria-hidden': 'true' });
  const glyph = el('span', { class: 'status-dot__glyph', 'aria-hidden': 'true' }, '');
  const text = el('span', { class: 'status-dot__text' }, '');
  const node = el('span', { class: 'status-dot' }, dot, glyph, text);

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    const kind = current.kind || 'idle';
    const cls = ['status-dot', `status-dot--${kind}`].join(' ');
    if (node.className !== cls) node.className = cls;
    setText(glyph, GLYPHS[kind] || GLYPHS.idle);
    setText(text, current.text || '');
    if (current.title) node.setAttribute('title', current.title);
    else node.removeAttribute('title');
  }

  update({});

  return {
    el: node,
    update,
    /** 纯文本节点，无需解绑。 */
    destroy() {},
  };
}

/**
 * 快捷工厂。
 * @param {'ok'|'warn'|'error'|'busy'|'idle'} kind
 * @param {string} text
 * @param {string} [title]
 * @returns {HTMLElement}
 */
export function statusDot(kind, text, title) {
  return createStatusDot({ kind, text, title }).el;
}
