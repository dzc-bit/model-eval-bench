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
 * 图形：一律走 components/icons.js 的内联 SVG。以前这里把 `icon` 当**文字**渲染
 * （`'○'` 塞进一个带边框的圆角方块），看起来像控件坏了而不是设计（§.empty-state 明确禁止）；
 * 现在 `icon` 传的是图标名，认不出的名字回落 `inbox`，保证永远有图形。
 * 结构：图形 → 标题 → 说明 → 最多一个动作，垂直居中（§.empty-state）。
 *
 * 依赖：core/dom.js、core/strings.js、components/icons.js
 * 导出：createEmptyState(props) → { el, update, destroy }
 */

import { el, setText, clear } from '../core/dom.js';
import { S } from '../core/strings.js';
import { createIcon, resolveIcon } from './icons.js';

/**
 * 创建空态。
 *
 * @param {{
 *   title: string, desc?: string, icon?: string,
 *   actions?: HTMLElement[], alert?: boolean
 * }} props
 *   `icon` 取 components/icons.js 的图标名（见该文件 ICON_NAMES：
 *   inbox / folder / chart / tasks / podium / chat / play / scoreboard /
 *   chip / sliders / help / clock 等），缺省或写错都用 `inbox`。
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createEmptyState(props = {}) {
  let current = { ...props };

  // 图形挂在 .empty-state__icon 里，尺寸与颜色由 CSS 控制
  const icon = el('div', { class: 'empty-state__icon', 'aria-hidden': 'true' });
  const title = el('p', { class: 'empty-state__title' });
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

  /** 上一次真正渲染出来的图标名，用来避免每次 update 都重建 SVG 节点。 */
  let renderedIcon = '';

  /**
   * 换图标：只在名字真的变了时重建节点。
   * @param {string} name
   */
  function renderIcon(name) {
    if (renderedIcon === name) return;
    renderedIcon = name;
    clear(icon);
    icon.appendChild(createIcon(name));
  }

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    renderIcon(resolveIcon(current.icon));
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
