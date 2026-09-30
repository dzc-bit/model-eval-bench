/**
 * toast.js — 轻提示（§11.1）
 *
 * 状态清单：显示 → 自动消失 → 移除；手动关闭（关闭按钮可聚焦）。
 * 键盘路径：关闭按钮可 Tab 到、Enter / Space 关闭；不抢焦点（点「关闭」才动焦点）。
 * ARIA 要点：
 *   - 容器 `role="status"` `aria-live="polite"`；错误类单条用 `role="alert"`（assertive）。
 *   - toast 不抢焦点、不自动把焦点移走（§13.2：完成后不打断用户）。
 *
 * 与全局 live region 的关系（§12.6）：
 *   - a11y.js 维护的是「全局唯一 live region」，用于路由切换、校验开始/完成这类状态播报。
 *   - 本组件容器是它自己的 live region，专门承载「操作结果」（复制成功、校验完成、错误）。
 *   - 两者不重叠：showToast 内部**不再**调 a11y.announce，避免同一句话被读两遍。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createToastHost, showToast, destroyToastHost
 */

import { el, setText, on } from '../core/dom.js';
import { S } from '../core/strings.js';

/** 全局唯一宿主。 */
let host = null;

/** 同屏最多保留几条。 */
const MAX_TOASTS = 4;

/**
 * 创建（或复用）toast 宿主并挂到 body。
 * @returns {HTMLElement}
 */
export function createToastHost() {
  if (host && document.body.contains(host)) return host;
  host = el('div', { class: 'toast-host', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'false' });
  document.body.appendChild(host);
  return host;
}

/**
 * 销毁宿主（测试与页面卸载用）。
 * @returns {void}
 */
export function destroyToastHost() {
  if (host && host.parentNode) host.parentNode.removeChild(host);
  host = null;
}

/** 各类图标的文字符号（与颜色一起构成三重编码，§12.8）。 */
const ICONS = {
  success: '✓',
  error: '✕',
  warn: '!',
  info: 'i',
};

/**
 * 弹一条提示。
 *
 * @param {{
 *   message: string, detail?: string, kind?: 'success'|'error'|'warn'|'info',
 *   duration?: number, actionLabel?: string, onAction?: Function
 * }} options
 * @returns {{el: HTMLElement, close: () => void}}
 */
export function showToast(options = {}) {
  const {
    message,
    detail = '',
    kind = 'info',
    duration = kind === 'error' ? 9000 : 4000,
    actionLabel = '',
    onAction = null,
  } = options;

  const container = createToastHost();

  // 错误类用 assertive（role="alert"），其余走宿主的 polite
  if (kind === 'error') {
    container.setAttribute('aria-live', 'assertive');
  } else if (container.getAttribute('aria-live') === 'assertive') {
    container.setAttribute('aria-live', 'polite');
  }

  const icon = el('span', { class: 'toast__icon', 'aria-hidden': 'true' }, ICONS[kind] || ICONS.info);
  const text = el('span', { class: 'toast__text' }, message);
  const detailNode = el('span', { class: 'toast__detail' });
  if (detail) setText(detailNode, detail);

  const body = el('span', { class: 'toast__body' }, text, detailNode);

  const closeBtn = el(
    'button',
    { type: 'button', class: 'btn btn--ghost btn--sm toast__close', 'aria-label': S.ACTION_CLOSE },
    '×',
  );

  const node = el(
    'div',
    { class: `toast toast--${kind}`, role: kind === 'error' ? 'alert' : 'group', 'aria-label': message },
    icon,
    body,
    closeBtn,
  );

  let timer = null;
  const offClose = on(closeBtn, 'click', () => close());

  // 超过上限时先挤掉最老的
  while (container.children.length >= MAX_TOASTS) {
    const oldest = container.firstElementChild;
    if (!oldest) break;
    oldest.remove();
  }
  container.appendChild(node);

  if (duration > 0) {
    timer = window.setTimeout(() => close(), duration);
  }

  /**
   * 关闭这条提示。
   * @returns {void}
   */
  function close() {
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
    offClose();
    if (actionOff) actionOff();
    if (node.parentNode) node.parentNode.removeChild(node);
  }

  let actionOff = null;
  if (actionLabel) {
    const actionBtn = el('button', { type: 'button', class: 'btn btn--sm toast__action' }, actionLabel);
    actionOff = on(actionBtn, 'click', () => {
      close();
      if (typeof onAction === 'function') onAction();
    });
    body.appendChild(actionBtn);
  }

  return { el: node, close };
}
