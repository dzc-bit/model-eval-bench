/**
 * dock.js — 工作台底部操作栏（2026-10-02 对话流改版）
 *
 * 职责：
 *   1. 每个时刻只有**一个主按钮**（按状态机切换：发送提示词 → 运行校验 →
 *      进入下一轮 / 查看参考解），由编排层算好 {label, onClick, disabled, reason} 传进来。
 *   2. 显式的「结束本轮并回收沙箱」（红线：不能只留折叠起来的回收）——常驻、
 *      不可用时禁用并把原因写在按钮上。
 *   3. 其余全部出口收进「⋯ 更多操作」菜单：菜单项一律常列，不可用的禁用 + 原因
 *      写在项里（红线：出口不许条件隐藏，可以收进菜单，不许删到找不到）。
 *
 * 菜单行为：点触发钮/Esc/点外面开合；打开后焦点落到第一个可用项；关闭后焦点还给
 * 触发钮；方向键上下在项间移动。菜单向上弹出（dock 钉在视口底部）。
 *
 * 依赖：core/dom.js、core/strings.js、components/button.js
 * 导出：createDock() → { el, update, destroy, closeMenu }
 */

import { el, clear, on } from '../../core/dom.js';
import { S } from '../../core/strings.js';
import { createButton } from '../../components/button.js';

/** 本组件新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  MENU_TRIGGER: '更多操作',
  MENU_LABEL: '更多操作菜单',
};

/**
 * 创建底部操作栏。
 * @returns {{el: HTMLElement, update: Function, destroy: Function, closeMenu: Function}}
 */
export function createDock() {
  const primaryBtn = createButton({
    label: '',
    variant: 'primary',
    onClick: (event) => {
      const action = current.primary;
      if (action && typeof action.onClick === 'function') action.onClick(event);
    },
  });
  const finishBtn = createButton({
    label: '',
    variant: 'ghost',
    onClick: (event) => {
      const action = current.finish;
      if (action && typeof action.onClick === 'function') action.onClick(event);
    },
  });
  const menuBtn = createButton({
    label: T.MENU_TRIGGER,
    variant: 'ghost',
    onClick: () => toggleMenu(),
  });
  menuBtn.getButton().setAttribute('aria-haspopup', 'menu');
  menuBtn.getButton().setAttribute('aria-expanded', 'false');

  const menuList = el('div', { class: 'ws-menu', role: 'menu', 'aria-label': T.MENU_LABEL, hidden: true });
  const menuWrap = el('div', { class: 'ws-dock__menu-wrap' }, menuBtn.el, menuList);

  const root = el(
    'div',
    { class: 'ws-dock', role: 'group', 'aria-label': S.WS_TITLE || '工作台操作' },
    primaryBtn.el,
    finishBtn.el,
    el('span', { class: 'u-spacer' }),
    menuWrap,
  );

  let current = { primary: {}, finish: {}, menuItems: [] };
  let menuOpen = false;
  /** 菜单项的 onClick 列表（渲染顺序与 DOM 一致，闭包里按序号取）。 */
  let itemHandlers = [];

  function isOpen() {
    return menuOpen;
  }

  function openMenu() {
    if (menuOpen) return;
    menuOpen = true;
    menuList.hidden = false;
    menuBtn.getButton().setAttribute('aria-expanded', 'true');
    const first = menuList.querySelector('button:not([aria-disabled="true"])');
    if (first) first.focus();
  }

  function closeMenu({ refocus = false } = {}) {
    if (!menuOpen) return;
    menuOpen = false;
    menuList.hidden = true;
    menuBtn.getButton().setAttribute('aria-expanded', 'false');
    if (refocus) menuBtn.getButton().focus();
  }

  function toggleMenu() {
    if (menuOpen) closeMenu({ refocus: true });
    else openMenu();
  }

  /**
   * 菜单项键盘路径：Esc 关、上下方向键在项间移动（含禁用项，读屏才能读到原因）、
   * Home/End 跳首尾。
   * @param {KeyboardEvent} event
   */
  function onMenuKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu({ refocus: true });
      return;
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp' && event.key !== 'Home' && event.key !== 'End') return;
    const items = Array.from(menuList.querySelectorAll('button'));
    if (!items.length) return;
    event.preventDefault();
    const index = items.indexOf(document.activeElement);
    let next = index;
    if (event.key === 'ArrowDown') next = index < 0 ? 0 : (index + 1) % items.length;
    else if (event.key === 'ArrowUp') next = index <= 0 ? items.length - 1 : index - 1;
    else if (event.key === 'Home') next = 0;
    else next = items.length - 1;
    items[next].focus();
  }

  const offMenuKeydown = on(menuList, 'keydown', onMenuKeydown);
  // 点菜单外面就关上；捕获得比 click 早，避免触发钮自己先被当成「外面」
  const offDocDown = on(document, 'pointerdown', (event) => {
    if (!menuOpen) return;
    if (menuWrap.contains(event.target)) return;
    closeMenu();
  }, { capture: true });

  /**
   * 渲染菜单项：常列全量出口，禁用项给 reason（红线 1）。
   * @param {Array<{key: string, label: string, danger?: boolean, disabled?: boolean, reason?: string, onClick?: Function, ariaLabel?: string}>} items
   */
  function renderMenu(items) {
    itemHandlers = [];
    clear(menuList);
    items.forEach((item, index) => {
      itemHandlers.push(typeof item.onClick === 'function' ? item.onClick : null);
      const reasonNode = item.reason
        ? el('span', { class: 'ws-menu__reason' }, item.reason)
        : null;
      const reasonId = item.reason ? `ws-menu-reason-${index}` : '';
      if (reasonId && reasonNode) reasonNode.id = reasonId;
      const btn = el(
        'button',
        {
          type: 'button',
          class: `ws-menu__item${item.danger ? ' ws-menu__item--danger' : ''}${item.disabled ? ' ws-menu__item--disabled' : ''}`,
          role: 'menuitem',
          // 用 aria-disabled 而不是原生 disabled：禁用项保持可聚焦，
          // 读屏与键盘用户才能读到「为什么点不动」（红线 1 的完整含义）
          'aria-disabled': item.disabled ? 'true' : 'false',
          'aria-label': item.ariaLabel || '',
          'aria-describedby': reasonId,
          onClick: () => {
            if (item.disabled) return;
            const handler = itemHandlers[index];
            closeMenu();
            if (handler) handler();
          },
        },
        el('span', { class: 'ws-menu__label' }, item.label),
        reasonNode,
      );
      // aria-label 为空串时移除，让可见标签自己说话
      if (!item.ariaLabel) btn.removeAttribute('aria-label');
      if (!reasonId) btn.removeAttribute('aria-describedby');
      menuList.appendChild(btn);
    });
  }

  /**
   * 差异更新：签名没变就不动 DOM（轮询不能把打开的菜单/焦点吞掉）。
   * @param {{primary?: object, finish?: object, menuItems?: Array}} next
   */
  function update(next = {}) {
    const prev = current;
    current = {
      primary: next.primary || {},
      finish: next.finish || {},
      menuItems: Array.isArray(next.menuItems) ? next.menuItems : [],
    };
    const p = current.primary;
    primaryBtn.update({
      label: p.label || '',
      disabled: Boolean(p.disabled),
      reason: p.reason || '',
      loading: Boolean(p.loading),
      busyLabel: p.busyLabel || p.label || '',
      kbd: p.kbd || '',
      title: p.title || '',
    });
    const f = current.finish;
    finishBtn.update({
      label: f.label || '',
      disabled: Boolean(f.disabled),
      reason: f.reason || '',
      loading: Boolean(f.loading),
      busyLabel: f.busyLabel || f.label || '',
    });
    const sig = current.menuItems
      .map((i) => `${i.key}|${i.label}|${i.disabled ? 1 : 0}|${i.reason || ''}`)
      .join('#');
    const prevSig = (prev.menuItems || [])
      .map((i) => `${i.key}|${i.label}|${i.disabled ? 1 : 0}|${i.reason || ''}`)
      .join('#');
    if (sig !== prevSig) renderMenu(current.menuItems);
  }

  return {
    el: root,
    update,
    closeMenu,
    /** 菜单当前是否开着（编排层切换状态时要先收菜单，避免项指向旧状态）。 */
    isOpen,
    destroy() {
      offMenuKeydown();
      offDocDown();
      primaryBtn.destroy();
      finishBtn.destroy();
      menuBtn.destroy();
    },
  };
}
