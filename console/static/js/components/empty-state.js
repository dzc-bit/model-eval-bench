/**
 * empty-state.js — 空态（§11.1 / §13.1）
 *
 * 状态清单：空（默认）| 带下一步动作 | 带次要动作。
 * 键盘路径：主按钮与次要按钮都可 Tab 到、Enter / Space 触发。
 * ARIA 要点：
 *   - 文案必须回答两件事：**为什么空** + **下一步做什么**（§13.1）。
 *   - 区域用 `role="status"`（温和），错误空态用 `role="alert"`。
 *   - 图标 aria-hidden，语义全靠文字。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createEmptyState(props) → { el, update, destroy }
 */

import { el, setText, clear } from '../core/dom.js';
import { S } from '../core/strings.js';

/**
 * 创建空态。
 *
 * @param {{
 *   title: string, desc?: string, icon?: string,
 *   actions?: HTMLElement[], alert?: boolean
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createEmptyState(props = {}) {
  let current = { ...props };

  const icon = el('div', { class: 'empty-state__icon', 'aria-hidden': 'true' }, current.icon || '○');
  const title = el('p', { class: 'empty-state__title' }, current.title || S.STATE_EMPTY);
  const desc = el('p', { class: 'empty-state__desc' });
  const actions = el('div', { class: 'empty-state__actions' });

  const root = el(
    'div',
    { class: 'empty-state', role: current.alert ? 'alert' : 'status' },
    icon,
    title,
    desc,
    actions,
  );

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    setText(icon, current.icon || '○');
    setText(title, current.title || S.STATE_EMPTY);
    if (current.desc) {
      setText(desc, current.desc);
      desc.hidden = false;
    } else {
      desc.hidden = true;
    }
    root.setAttribute('role', current.alert ? 'alert' : 'status');

    clear(actions);
    (current.actions || []).forEach((a) => {
      if (a) actions.appendChild(a);
    });
    actions.hidden = (current.actions || []).length === 0;
  }

  update({});

  return {
    el: root,
    update,
    /** 动作节点由调用方持有，这里只做挂载。 */
    destroy() {},
  };
}
