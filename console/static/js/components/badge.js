/**
 * badge.js — 徽章（档位标签，§11.1 / §12.8）
 *
 * 状态清单：静态（初级 / 中级 / 高级 / 通用 / 成功 / 危险 / 中和）。
 * 键盘路径：不可聚焦（纯文本标签），但作为元素内文字会被读屏念出。
 * ARIA 要点：**颜色 + 文本 + 符号三重编码**，颜色绝不单独承载语义（§12.8）。
 *   例：初级 = ● + "初级" + 绿；中级 = ◆ + "中级" + 琥珀；高级 = ▲ + "高级" + 红。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createBadge(props) → { el, update, destroy }, tierBadge(tier, extra)
 */

import { el, setText } from '../core/dom.js';
import { S, t, TIER_NAMES } from '../core/strings.js';

/** 档位 → 符号（与颜色一起构成三重编码）。primary 是后端 packs 的初级值，与 easy 同义。 */
const TIER_GLYPHS = { easy: '●', primary: '●', medium: '◆', hard: '▲', king: '★' };

/** 通用变体 → 符号。 */
const VARIANT_GLYPHS = {
  success: '✓',
  danger: '✕',
  info: 'i',
  warn: '!',
  muted: '',
};

/**
 * 创建徽章。
 *
 * @param {{label: string, variant?: string, glyph?: string, title?: string}} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createBadge(props = {}) {
  let current = { ...props };

  const glyph = el('span', { class: 'badge__glyph', 'aria-hidden': 'true' }, current.glyph || '');
  const text = el('span', { class: 'badge__text' }, current.label || '');
  const node = el('span', { class: 'badge' }, glyph, text);

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    setText(glyph, current.glyph || VARIANT_GLYPHS[current.variant] || '');
    setText(text, current.label || '');
    const cls = ['badge', current.variant ? `badge--${current.variant}` : ''].filter(Boolean).join(' ');
    if (node.className !== cls) node.className = cls;
    if (current.title) node.setAttribute('title', current.title);
    else node.removeAttribute('title');
  }

  update({});

  return {
    el: node,
    update,
    /** destroy：纯文本节点无需解绑。 */
    destroy() {},
  };
}

/**
 * 档位徽章的快捷工厂。
 * @param {'easy'|'medium'|'hard'|string} tier
 * @param {{attempts?: number, title?: string}} [extra]
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function tierBadge(tier, extra = {}) {
  const known = TIER_NAMES[tier] || { label: tier || S.STATE_UNKNOWN, variant: null };
  const variant = known.variant ? `tier-${known.variant}` : 'muted';
  const attempts = extra.attempts ? ` · ${t(S.LIB_CARD_ATTEMPTS, { n: extra.attempts })}` : '';
  return createBadge({
    label: `${known.label}${attempts}`,
    variant,
    glyph: TIER_GLYPHS[tier] || '·',
    title: extra.title || '',
  });
}
