/**
 * button.js — 按钮组件（§11.1）
 *
 * 状态清单：
 *   default | hover | active | focus-visible | loading | disabled | disabled-with-reason
 * 键盘路径：原生 `<button type="button">`，Tab 可达，Enter / Space 触发；图标按钮走 aria-label。
 * ARIA 要点：
 *   - loading 时 `aria-busy="true"` + 原生 disabled（防重复提交，§11.2 #4）
 *   - disabled 必带原因文本，并用 `aria-describedby` 关联到可见的说明节点
 *   - 图标按钮（无可见文字）必须有 aria-label（§12.11）
 *   - loading 时文本换成"正在…"，读屏能听出当前在做什么
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createButton(props) → { el, update, destroy, focus, getEl }
 */

import { el, setText, on } from '../core/dom.js';

/** 递增 id，给 aria-describedby 用。 */
let seq = 0;

/**
 * 创建按钮。
 *
 * @param {{
 *   label?: string, variant?: 'default'|'primary'|'danger'|'ghost',
 *   size?: 'sm'|'md'|'lg', icon?: string, iconNode?: Element, kbd?: string,
 *   loading?: boolean, loadingLabel?: string, busyLabel?: string,
 *   disabled?: boolean, reason?: string, reasonTone?: 'warn'|'danger'|'success',
 *   ariaLabel?: string, title?: string, block?: boolean, pressed?: boolean,
 *   onClick?: (ev: MouseEvent) => void
 * }} [props]
 *   `icon` 是文本字形（▶ ↗ ⧉），`iconNode` 是真正的图形节点（components/icons.js 的
 *   SVG）。状态栏那两个图标按钮要有线稿图标，字形的「ⓘ」看起来像坏掉的界面；
 *   两者同时给时以 `iconNode` 为准，且都算「图标按钮」（无可见文字时套 btn--icon）。
 * @returns {{el: HTMLElement, update: Function, destroy: Function, focus: Function}}
 */
export function createButton(props = {}) {
  seq += 1;
  const reasonId = `btn-reason-${seq}`;

  let current = { ...props };
  let onClickHandler = typeof props.onClick === 'function' ? props.onClick : null;

  const iconSpan = el('span', { class: 'btn__icon', 'aria-hidden': 'true' }, props.icon || '');
  const labelSpan = el('span', { class: 'btn__label' }, props.label || '');
  let kbdSpan = props.kbd ? el('kbd', { class: 'btn__kbd', 'aria-hidden': 'true' }, props.kbd) : null;
  const spinner = el('span', { class: 'btn__spinner', 'aria-hidden': 'true' });

  const button = el(
    'button',
    { type: 'button', class: 'btn' },
    iconSpan,
    labelSpan,
    kbdSpan,
  );

  const reasonNode = el('span', { class: 'btn-note', role: 'note' });
  const wrapper = el('span', { class: 'u-inline' }, button, reasonNode);
  // wrapper 用行内块布局，内部按钮仍然撑满
  wrapper.style.display = 'inline-flex';
  wrapper.style.flexDirection = 'column';
  wrapper.style.alignItems = 'flex-start';

  /**
   * 点击事件：loading / disabled 期间直接吞掉，天然防重复提交（§11.2 #4）。
   */
  const offClick = on(button, 'click', (event) => {
    const s = readState();
    if (s.loading || s.disabled) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (onClickHandler) onClickHandler(event);
  });

  /**
   * 从 props 里解析出内部状态快照（update 传的补丁优先）。
   * @returns {{loading: boolean, disabled: boolean, label: string, reason: string}}
   */
  function readState(overrides) {
    const merged = { ...current, ...(overrides || {}) };
    return {
      loading: Boolean(merged.loading),
      disabled: Boolean(merged.disabled),
      label: merged.loading
        ? merged.busyLabel || merged.loadingLabel || merged.label || ''
        : merged.label || '',
      reason: merged.reason || '',
      reasonTone: merged.reasonTone || '',
    };
  }

  /**
   * 差异更新：只改文本 / 类名 / 属性，不重建根节点（§10.4）。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    if (typeof patch.onClick === 'function' || patch.onClick === null) {
      onClickHandler = typeof patch.onClick === 'function' ? patch.onClick : null;
    }
    const next = readState();

    setText(labelSpan, next.label);
    // 图形节点只在换了的时候重挂：轮询里每次重建 SVG 会让按钮闪一下
    if (current.iconNode) {
      if (iconSpan.firstChild !== current.iconNode) {
        iconSpan.textContent = '';
        iconSpan.appendChild(current.iconNode);
      }
    } else {
      setText(iconSpan, current.icon || '');
    }
    if (current.kbd) {
      if (!kbdSpan) {
        kbdSpan = el('kbd', { class: 'btn__kbd', 'aria-hidden': 'true' }, current.kbd);
        button.appendChild(kbdSpan);
      } else {
        setText(kbdSpan, current.kbd);
      }
    }

    // 类名
    const hasIcon = Boolean(current.icon || current.iconNode);
    const className = [
      'btn',
      current.variant && current.variant !== 'default' ? `btn--${current.variant}` : '',
      current.size && current.size !== 'md' ? `btn--${current.size}` : '',
      hasIcon && !current.label ? 'btn--icon' : '',
      current.block ? 'btn--block' : '',
    ]
      .filter(Boolean)
      .join(' ');
    if (button.className !== className) button.className = className;

    // 状态属性
    button.disabled = next.loading || next.disabled;
    button.setAttribute('aria-busy', next.loading ? 'true' : 'false');
    if (current.pressed !== undefined) {
      button.setAttribute('aria-pressed', current.pressed ? 'true' : 'false');
    }
    if (current.ariaLabel) button.setAttribute('aria-label', current.ariaLabel);
    else button.removeAttribute('aria-label');
    if (current.title) button.setAttribute('title', current.title);
    else button.removeAttribute('title');
    button.setAttribute('type', current.type || 'button');

    // 加载指示
    if (next.loading && !spinner.isConnected) {
      button.insertBefore(spinner, labelSpan);
    } else if (!next.loading && spinner.isConnected) {
      button.removeChild(spinner);
    }

    // 禁用原因：可见文本 + aria-describedby 双重关联（§11.1 button）
    if (next.reason) {
      setText(reasonNode, next.reason);
      reasonNode.className = `btn-note${next.reasonTone ? ` btn-note--${next.reasonTone}` : ''}`;
      reasonNode.id = reasonId;
      reasonNode.hidden = false;
      button.setAttribute('aria-describedby', reasonId);
    } else {
      reasonNode.textContent = '';
      reasonNode.hidden = true;
      button.removeAttribute('aria-describedby');
    }
  }

  update({});

  return {
    el: wrapper,
    update,
    /**
     * 程序化聚焦（快捷键触发按钮时用）。
     */
    focus() {
      button.focus();
    },
    /** 拿到真正的 button 元素（需要原生 API 时用）。 */
    getButton: () => button,
    /** 解绑事件（§10.4 destroy 纪律）。 */
    destroy() {
      offClick();
    },
  };
}
