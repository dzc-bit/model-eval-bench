/**
 * a11y.js — 无障碍工具（§10.3 / §12）
 *
 * 职责：
 *   1. 全局唯一 live region 管理器：普通状态 polite，错误 assertive。
 *   2. `trapFocus(container)` 焦点陷阱，返回释放函数。
 *   3. `restoreFocus(el)` 关闭浮层后把焦点还回触发元素。
 *   4. `focusHeading(el)` 路由切换后把焦点移到视图 h1（配合 tabindex="-1"）。
 *   5. `isEditableTarget(node)` 供快捷键判断「焦点是否在输入控件」。
 *
 * 依赖：无（只操作 DOM 与 aria 属性）。
 * 导出：announce, trapFocus, restoreFocus, focusHeading, isEditableTarget, focusables
 *
 * 纪律：
 *   - 全站只有这一处 live region（toast 容器是组件自带的第二个，见 toast.js 注释），
 *     避免同一句话被读两遍。
 *   - 播报内容去重：连续两次相同文本会被 screen reader 忽略，这里做一次去重节流。
 */

import { S } from './strings.js';

/** 可聚焦元素选择器（不含 disabled / aria-hidden 的容器）。 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/** 可编辑元素选择器。 */
const EDITABLE_SELECTOR = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

/** live region 宿主（懒建，全局唯一）。 */
let liveHost = null;
/** polite 播报区。 */
let politeRegion = null;
/** assertive 播报区。 */
let assertiveRegion = null;

/** 上一次播报内容与时间，用于去重节流。 */
let lastAnnounce = { text: '', at: 0 };

/** 当前保存的「返回焦点」目标。 */
let restoreTarget = null;

/**
 * 懒建全局 live region。
 * @returns {void}
 */
function ensureLiveRegion() {
  if (liveHost && document.body.contains(liveHost)) return;
  politeRegion = document.createElement('div');
  politeRegion.className = 'visually-hidden';
  politeRegion.setAttribute('role', 'status');
  politeRegion.setAttribute('aria-live', 'polite');
  politeRegion.setAttribute('aria-atomic', 'true');

  assertiveRegion = document.createElement('div');
  assertiveRegion.className = 'visually-hidden';
  assertiveRegion.setAttribute('role', 'alert');
  assertiveRegion.setAttribute('aria-live', 'assertive');
  assertiveRegion.setAttribute('aria-atomic', 'true');

  liveHost = document.createElement('div');
  liveHost.dataset.evalconsoleLive = '1';
  liveHost.appendChild(politeRegion);
  liveHost.appendChild(assertiveRegion);
  document.body.appendChild(liveHost);
}

/**
 * 播报一条状态消息（§12.6）。
 * @param {string} text 中文文案
 * @param {{assertive?: boolean}} [opts] assertive=true 表示错误类，立即打断
 * @returns {void}
 */
export function announce(text, opts = {}) {
  if (!text) return;
  ensureLiveRegion();
  const assertive = Boolean(opts.assertive);
  const region = assertive ? assertiveRegion : politeRegion;
  const now = Date.now();
  // 去重节流：1.5 秒内的同一句话不重复播报（读屏软件本身也会吞掉重复内容）
  if (lastAnnounce.text === text && now - lastAnnounce.at < 1500) return;
  lastAnnounce = { text, at: now };
  // 先清空再写入，确保相同内容也会被重新朗读
  region.textContent = '';
  // 用一个极短的间隔让读屏软件观察到 DOM 变化
  window.setTimeout(() => {
    region.textContent = text;
  }, 40);
}

/**
 * 取出容器内所有可聚焦元素（过滤掉隐藏的）。
 * @param {Element} container
 * @returns {HTMLElement[]}
 */
export function focusables(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter((node) => {
    if (node.hasAttribute('disabled')) return false;
    if (node.getAttribute('aria-hidden') === 'true') return false;
    // offsetParent 为 null 在 fixed 定位里不可靠，改用几何信息兜底
    const rect = node.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      return node === document.activeElement;
    }
    return true;
  });
}

/**
 * 焦点陷阱：把 Tab 键锁在 container 内。
 *
 * @param {Element} container 浮层根节点
 * @param {{initialFocus?: Element, returnFocus?: Element}} [opts]
 *        initialFocus 指定初始焦点（缺省取第一个可聚焦元素或容器本身）
 *        returnFocus 关闭时焦点还给谁（缺省取当前 activeElement 快照）
 * @returns {() => void} 释放函数：解绑 keydown，并把焦点还给 returnFocus
 */
export function trapFocus(container, opts = {}) {
  if (!container) return () => {};

  const previous = document.activeElement;
  const returnTarget = opts.returnFocus || (previous instanceof HTMLElement ? previous : null);

  const onKeydown = (event) => {
    if (event.key !== 'Tab') return;
    const items = focusables(container);
    if (items.length === 0) {
      // 一个可聚焦元素都没有：把焦点钉在容器上，Tab 不动
      event.preventDefault();
      container.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const current = document.activeElement;
    if (event.shiftKey) {
      if (current === first || !container.contains(current)) {
        event.preventDefault();
        last.focus();
      }
    } else if (current === last || !container.contains(current)) {
      event.preventDefault();
      first.focus();
    }
  };

  document.addEventListener('keydown', onKeydown, true);

  const target =
    (opts.initialFocus && container.contains(opts.initialFocus) && opts.initialFocus) ||
    focusables(container)[0] ||
    container;
  if (target === container && !container.hasAttribute('tabindex')) {
    container.setAttribute('tabindex', '-1');
  }
  // 先同步聚焦：容器已经在 DOM 里的话一次就成了。
  // 对游离节点 focus() 会悄悄落到 body 上，那种情况再补一帧重试。
  // 不能只靠 rAF —— 后台标签页里 rAF 会被浏览器节流甚至停发，焦点就永远进不去浮层。
  function moveFocus() {
    try {
      target.focus({ preventScroll: true });
    } catch {
      target.focus();
    }
  }
  moveFocus();
  if (document.activeElement !== target) {
    // 两条重试通道：宏任务（点击事件栈里被浏览器忽略的 focus）+ 一帧后（样式未就绪）。
    // 只押 rAF 的话，后台标签页 / 部分内嵌视图根本不发帧，焦点就永远进不去浮层。
    window.setTimeout(() => {
      if (document.activeElement !== target) moveFocus();
      window.requestAnimationFrame(() => {
        if (document.activeElement !== target) moveFocus();
      });
    }, 0);
  }

  return function release() {
    document.removeEventListener('keydown', onKeydown, true);
    if (returnTarget && document.contains(returnTarget)) {
      try {
        returnTarget.focus({ preventScroll: true });
      } catch {
        returnTarget.focus();
      }
    }
  };
}

/**
 * 记录一个「关闭后要还给焦点」的元素。
 * @param {Element|null} node
 * @returns {void}
 */
export function rememberFocus(node) {
  restoreTarget = node instanceof HTMLElement ? node : null;
}

/**
 * 把焦点还给最近记录的触发元素（浮层关闭时用，§12.5）。
 * @param {Element} [fallback] 记录缺失时的备选目标
 * @returns {void}
 */
export function restoreFocus(fallback) {
  const target = (restoreTarget && document.contains(restoreTarget) && restoreTarget) || fallback;
  restoreTarget = null;
  if (!(target instanceof HTMLElement)) return;
  try {
    target.focus({ preventScroll: true });
  } catch {
    target.focus();
  }
}

/**
 * 把焦点移到视图标题（路由切换后调用，§10.3 / §12.1）。
 *
 * 做法：确保 tabindex="-1"，focus，并让读屏软件念出整段标题。
 * @param {HTMLElement|null} heading 该视图的 h1
 * @returns {void}
 */
export function focusHeading(heading) {
  if (!(heading instanceof HTMLElement)) return;
  if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
  try {
    heading.focus({ preventScroll: true });
  } catch {
    heading.focus();
  }
  heading.scrollIntoView({ block: 'start', behavior: 'auto' });
}

/**
 * 焦点是否落在可编辑控件上（快捷键判定用，§13.4 / §11.2 #11）。
 * @param {EventTarget|null} target 默认取 document.activeElement
 * @returns {boolean}
 */
export function isEditableTarget(target) {
  const node = target instanceof Element ? target : document.activeElement;
  if (!node) return false;
  if (node.isContentEditable) return true;
  return Boolean(node.closest && node.closest(EDITABLE_SELECTOR));
}

/** 供视图显示「快捷键帮助」时复用的播报文案。 */
export const SHORTCUT_HELP_ANNOUNCE = S.HELP_SHORTCUT_TITLE;
