/**
 * modal.js — 模态对话框（§11.1 / §12.5 / §11.2 #12）
 *
 * 状态清单：关闭 → 打开（焦点入内）→ 关闭（焦点还原）。
 * 键盘路径：Tab / Shift+Tab 在浮层内循环；Esc 关闭；关闭按钮可聚焦。
 * ARIA 要点：
 *   - 根 `role="dialog"` `aria-modal="true"` `aria-labelledby`（指标题）。
 *   - 背景内容置 `inert` + `aria-hidden="true"`，读屏不会跑到后面去。
 *   - 初始焦点入内；关闭后焦点还原到触发元素（a11y.trapFocus 的 returnFocus）。
 *
 * 模态堆叠策略（§11.2 #12）：**同屏最多一个**。打开新的先关旧的，Esc 行为始终明确。
 *
 * 依赖：core/dom.js、core/a11y.js、core/strings.js
 * 导出：openModal(options) → { el, close, destroy, setBusy, isOpen }
 */

import { el, setText, on, clear } from '../core/dom.js';
import { trapFocus } from '../core/a11y.js';
import { S } from '../core/strings.js';

/** 当前打开的模态；打开新模态前先关掉它。 */
let currentModal = null;
/** 打开过的模态栈（用于 Esc 从最上层关闭）。 */
const stack = [];

/** 递增 id，供 aria-labelledby 关联。 */
let seq = 0;

/**
 * 打开一个模态。
 *
 * @param {{
 *   title: string,
 *   body?: HTMLElement|string|Array,
 *   footer?: Array<HTMLElement>,
 *   onClose?: (reason: string) => void,
 *   closeOnBackdrop?: boolean,
 *   initialFocus?: HTMLElement,
 *   variant?: 'default'|'danger'|'wide',
 *   describedBy?: string
 * }} options
 * @returns {{el: HTMLElement, close: Function, destroy: Function, setBusy: Function, isOpen: Function, bodyEl: HTMLElement}}
 */
export function openModal(options = {}) {
  // 模态堆叠规避：先关掉已打开的
  if (currentModal && currentModal.isOpen()) {
    currentModal.close('replaced');
  }

  seq += 1;
  const titleId = `modal-title-${seq}`;
  const bodyId = `modal-body-${seq}`;

  const titleNode = el('h2', { class: 'modal__title', id: titleId }, options.title || S.MODAL_LOADING_TITLE);

  const closeBtn = el(
    'button',
    {
      type: 'button',
      class: 'btn btn--ghost btn--sm btn--icon modal__close',
      'aria-label': S.MODAL_CLOSE_LABEL,
    },
    '×',
  );

  const bodyEl = el('div', { class: 'modal__body', id: bodyId, tabindex: '-1' });
  const bodyContent = options.body;
  if (bodyContent instanceof HTMLElement) bodyEl.appendChild(bodyContent);
  else if (typeof bodyContent === 'string') setText(bodyEl, bodyContent);
  else if (Array.isArray(bodyContent)) bodyContent.forEach((n) => n && bodyEl.appendChild(n));

  const dialog = el(
    'div',
    {
      class: `modal${options.variant === 'danger' ? ' modal--danger' : ''}${options.variant === 'wide' ? ' modal--wide' : ''}`,
      role: 'dialog',
      'aria-modal': 'true',
      'aria-labelledby': titleId,
      tabindex: '-1',
    },
    el('div', { class: 'modal__head' }, titleNode, closeBtn),
    bodyEl,
  );

  if (Array.isArray(options.footer) && options.footer.length) {
    dialog.appendChild(el('div', { class: 'modal__foot' }, ...options.footer));
  }

  const backdrop = el('div', { class: 'modal__backdrop' }, dialog);
  if (options.closeOnBackdrop !== false) {
    // 只有点在浮层外的遮罩上才关，浮层内部点击不关
    on(backdrop, 'mousedown', (ev) => {
      if (ev.target === backdrop) close('backdrop');
    });
  }

  /** 背景元素：打开时置 inert，关闭时还原。 */
  const appRoot = document.getElementById('app-root') || document.getElementById('app');
  const siblings = Array.from(document.body.children).filter(
    (n) => n !== backdrop && n.id !== 'evalconsole-live' && !n.hasAttribute('data-evalconsole-live'),
  );
  const previousInert = siblings.map((n) => n.hasAttribute('inert'));
  const previousAria = siblings.map((n) => n.getAttribute('aria-hidden'));
  siblings.forEach((n) => {
    if ('inert' in n) n.inert = true;
    n.setAttribute('aria-hidden', 'true');
  });
  void appRoot;

  const offCloseClick = on(closeBtn, 'click', () => close('button'));

  /** Esc 关闭。 */
  const onKeydown = (ev) => {
    if (ev.key !== 'Escape') return;
    ev.preventDefault();
    ev.stopPropagation();
    close('escape');
  };
  document.addEventListener('keydown', onKeydown, true);

  document.body.appendChild(backdrop);

  // 必须先插入 DOM 再锁焦点：对游离节点调 focus() 会落到 body 上，
  // 插入后焦点不会自己回来，读屏用户就"进不去"这个浮层。
  const releaseFocus = trapFocus(dialog, { initialFocus: options.initialFocus });

  let open = true;

  /**
   * 事件处理器统一用的关闭入口。
   *
   * `close` 的真身是 handle 上的方法；这里不包一层的话，下面几个处理器里写的
   * `close(...)` 会解析到 `window.close` —— 调用不报错但也关不掉浮层，
   * 于是 × / Esc / 点遮罩全都失效。
   */
  const close = (reason) => handle.close(reason);

  const handle = {
    el: backdrop,
    bodyEl,
    /**
     * 关闭模态。
     * @param {string} [reason] closed / escape / backdrop / button / replaced
     */
    close(reason = 'programmatic') {
      if (!open) return;
      open = false;
      offCloseClick();
      document.removeEventListener('keydown', onKeydown, true);
      releaseFocus();
      siblings.forEach((n, i) => {
        if ('inert' in n) n.inert = previousInert[i];
        const aria = previousAria[i];
        if (aria === null) n.removeAttribute('aria-hidden');
        else n.setAttribute('aria-hidden', aria);
      });
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      const idx = stack.indexOf(handle);
      if (idx >= 0) stack.splice(idx, 1);
      if (currentModal === handle) currentModal = stack[stack.length - 1] || null;
      if (typeof options.onClose === 'function') {
        try {
          options.onClose(reason);
        } catch {
          /* 关闭回调出错不影响已完成的关闭 */
        }
      }
    },

    /**
     * 忙碌态：禁用关闭按钮（长操作期间防止误关）。
     * @param {boolean} busy
     * @param {string} [busyText]
     */
    setBusy(busy, busyText = '') {
      closeBtn.disabled = Boolean(busy);
      if (busyText) setText(titleNode, busyText);
    },

    /** 替换正文内容。 */
    setBody(content) {
      clear(bodyEl);
      if (content instanceof HTMLElement) bodyEl.appendChild(content);
      else if (typeof content === 'string') setText(bodyEl, content);
      else if (Array.isArray(content)) content.forEach((n) => n && bodyEl.appendChild(n));
    },

    /** 更新标题。 */
    setTitle(next) {
      setText(titleNode, next);
    },

    isOpen: () => open,

    /** destroy 等价于 close 一次（组件生命周期统一接口）。 */
    destroy() {
      handle.close('destroy');
    },
  };

  stack.push(handle);
  currentModal = handle;
  return handle;
}

/**
 * 是否存在打开中的模态（快捷键 Esc 判定用）。
 * @returns {boolean}
 */
export function hasOpenModal() {
  return stack.length > 0;
}

/**
 * 关闭最上层模态（全局 Esc 用）。
 * @returns {boolean} 是否真的关掉了一个
 */
export function closeTopModal() {
  const top = stack[stack.length - 1];
  if (!top) return false;
  top.close('escape');
  return true;
}
