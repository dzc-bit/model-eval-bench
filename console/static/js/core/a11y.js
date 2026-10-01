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
 * 导出：announce, trapFocus, restoreFocus, focusHeading, scrollBelowStickyHeader,
 *       revealIfCoveredByStickyTop, pageScrollTop, resetPageScroll,
 *       setPageScroll, isEditableTarget, focusables
 *
 * 纪律：
 *   - 全站只有这一处 live region（toast 容器是组件自带的第二个，见 toast.js 注释），
 *     避免同一句话被读两遍。
 *   - 播报内容去重：连续两次相同文本会被 screen reader 忽略，这里做一次去重节流。
 */

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
 * 把焦点还给指定元素（快捷键面板/浮层关闭后用，§12.5）。
 * @param {Element} [fallback] 要接收焦点的目标
 * @returns {void}
 */
export function restoreFocus(fallback) {
  if (!(fallback instanceof HTMLElement)) return;
  try {
    fallback.focus({ preventScroll: true });
  } catch {
    fallback.focus();
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
  scrollBelowStickyHeader(heading);
}

/** 可能钉在视口顶部、挡住滚动目标的条状节点（选择器，按出现顺序找）。 */
const STICKY_TOP_CANDIDATES = ['.error-bar', '.conn-bar', '.app-header'];

/** 目标元素顶部预留的呼吸空隙（像素）。 */
const STICKY_TOP_GAP = 8;

/**
 * 找到真正负责滚动的容器。
 *
 * 外壳有两种现实，都得支持：
 *   1. 文档滚动（窗口滚动条）——document.scrollingElement 自己动；
 *   2. 主内容区 .app-main 内部滚动（overflow: clip auto + 固定高度，
 *      为的是让粘性表头有滚动容器）——这时 window.scrollTo 是空操作，
 *      跳转必须落到那个容器上。
 * @param {HTMLElement} target 目标元素
 * @returns {Element|null} 可滚动祖先；没有就返回 null（交给窗口）
 */
function findScrollContainer(target) {
  let node = target && target.parentElement ? target.parentElement : null;
  while (node && node !== document.body) {
    const cs = window.getComputedStyle(node);
    const overflowY = cs.overflowY;
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 1) {
      return node;
    }
    node = node.parentElement;
  }
  const root = document.scrollingElement || document.documentElement;
  return root && root.scrollHeight > root.clientHeight + 1 ? root : null;
}

/**
 * 这个 sticky / fixed 节点当前是否真的钉在顶部。
 *
 * sticky 只能在自己的包含块里钉住：`#status-host` 只装了错误条这一行，
 * 行高 == 条高，没有可钉住的余量，于是它随文档一起滚走，永远挡不到内容。
 * 只认「计算样式是 sticky/fixed + 父容器比它高（有余量）+ 顶边贴住容器上沿」的节点。
 *
 * @param {HTMLElement} node 候选条
 * @returns {boolean}
 */
function isPinnedToTop(node) {
  const position = window.getComputedStyle(node).position;
  if (position !== 'sticky' && position !== 'fixed') return false;
  const rect = node.getBoundingClientRect();
  if (rect.height <= 0) return false;
  if (rect.top > 1 || rect.bottom < 1) return false;
  if (position === 'fixed') return true;
  const parent = node.parentElement;
  if (!parent) return false;
  return parent.getBoundingClientRect().height - rect.height >= 2;
}

/**
 * 在滚动容器上沿方向量出真正盖住目标的顶部条高度。
 * @param {HTMLElement} target 即将滚到顶部的目标元素
 * @param {number} containerTop 滚动容器可视区上沿（视口坐标）
 * @returns {number}
 */
function stickyCoverHeight(target, containerTop) {
  const bounds = target.getBoundingClientRect();
  let cover = 0;
  for (const selector of STICKY_TOP_CANDIDATES) {
    const node = document.querySelector(selector);
    if (!(node instanceof HTMLElement)) continue;
    if (!isPinnedToTop(node)) continue;
    const rect = node.getBoundingClientRect();
    // 横向不重叠 = 那是左侧边栏，挡不到主内容
    if (rect.right <= bounds.left + 1 || rect.left >= bounds.right - 1) continue;
    cover = Math.max(cover, Math.min(rect.bottom - containerTop, rect.height));
  }
  return Math.max(0, cover);
}

/**
 * 当前页面滚动位置（自动适配「文档滚动」与「.app-main 内部滚动」两种外壳）。
 * @param {HTMLElement} [reference] 用哪个元素定位滚动容器；缺省取外壳主内容区
 * @returns {number}
 */
export function pageScrollTop(reference) {
  const container = findScrollContainer(reference || document.getElementById('app-root') || document.body);
  return container ? container.scrollTop : (window.scrollY || 0);
}

/**
 * 把页面滚动清零（切换视图时用，§10.3）。
 * @param {HTMLElement} [reference]
 * @returns {void}
 */
export function resetPageScroll(reference) {
  const container = findScrollContainer(reference || document.getElementById('app-root') || document.body);
  if (container) container.scrollTop = 0;
  window.scrollTo(0, 0);
}

/**
 * 把页面滚到指定位置（§13.5 恢复上次位置）。
 * @param {number} y 滚动偏移
 * @param {HTMLElement} [reference]
 * @returns {void}
 */
export function setPageScroll(y, reference) {
  const top = Number(y) || 0;
  const container = findScrollContainer(reference || document.getElementById('app-root') || document.body);
  if (container) container.scrollTop = top;
  else window.scrollTo(0, top);
}

/**
 * 目标被顶部粘性条压住（或已经滚出容器上沿）时才把它挪到条下方；否则一动不动。
 * @param {HTMLElement} target
 * @returns {boolean} 有没有真的挪动
 */
export function revealIfCoveredByStickyTop(target) {
  if (!(target instanceof HTMLElement)) return false;
  const container = findScrollContainer(target);
  const containerTop = container ? Math.max(0, container.getBoundingClientRect().top) : 0;
  const rect = target.getBoundingClientRect();
  const cover = stickyCoverHeight(target, containerTop);
  if (rect.top <= containerTop - 1) {
    scrollBelowStickyHeader(target);
    return true;
  }
  if (cover <= 0 || rect.top >= containerTop + cover) return false;
  scrollBelowStickyHeader(target);
  return true;
}

/**
 * 将页面目标放在粘性导航下方，适配导航随视口换行后的实际高度。
 * @param {HTMLElement} target 页面中的目标元素
 * @returns {void}
 */
export function scrollBelowStickyHeader(target) {
  if (!(target instanceof HTMLElement)) return;
  const container = findScrollContainer(target);
  const containerTop = container ? Math.max(0, container.getBoundingClientRect().top) : 0;
  const offset = stickyCoverHeight(target, containerTop);
  const delta = target.getBoundingClientRect().top - containerTop - offset - STICKY_TOP_GAP;
  if (Math.abs(delta) < 1) return;
  const options = { top: delta, behavior: 'auto' };
  if (container) container.scrollBy(options);
  else window.scrollBy(options);
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
