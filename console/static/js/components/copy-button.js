/**
 * copy-button.js — 复制按钮（§11.1 / §9 重点交互 ① / §11.2 #8）
 *
 * 状态清单：idle → copying → copied（约 2s）→ idle；失败态 failed（带手动复制指引）
 * 键盘路径：原生按钮，Tab 可达，Enter / Space 触发。
 * ARIA 要点：
 *   - 结果通过 toast 的 live region 播报（"已复制" / "复制失败，请手动选择"）。
 *   - 失败时按钮文本回滚为"复制失败"（乐观反馈的失败分支，§13.2）。
 *   - 图标按钮走 aria-label。
 *
 * 三级降级（§11.2 #8）：
 *   1. navigator.clipboard.writeText（现代浏览器，需要安全上下文与权限）
 *   2. document.execCommand('copy') + 离屏 textarea（老浏览器 / 非安全上下文）
 *   3. 选中文本 + 明确提示手动 Ctrl+C（仍然给得出可用的文本，不让用户空手而归）
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createCopyButton(props) → { el, update, destroy, copy }
 */

import { el, setText, on } from '../core/dom.js';
import { S, t } from '../core/strings.js';
import { showToast } from './toast.js';

/** 「已复制」态停留时长（毫秒）。 */
const COPIED_MS = 2000;

/** 第三级降级造出来的临时 textarea；同一时刻最多一个，新的顶掉旧的。 */
let scratchArea = null;

/**
 * 第一级：Clipboard API。
 * @param {string} text
 * @returns {Promise<boolean>} 是否成功
 */
async function viaClipboard(text) {
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * 第二级：execCommand('copy')。
 * @param {string} text
 * @returns {boolean} 是否成功
 */
function viaExecCommand(text) {
  try {
    if (typeof document.execCommand !== 'function') return false;
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.setAttribute('aria-hidden', 'true');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '-9999px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return Boolean(ok);
  } catch {
    return false;
  }
}

/**
 * 第三级：把源元素里的文本选中，交给用户自己按 Ctrl+C。
 *
 * 源元素给不出来（比如「复制全部」是把两段文本拼起来的，页面上没有对应节点）时，
 * 就造一个**留在页面里**的只读 textarea，把文本放进去并全选聚焦 —— 焦点在它上面，
 * 用户按 Ctrl+C 就能拿到。不能复制完就摘掉，那样选区会跟着消失，提示就成了空话。
 *
 * @param {HTMLElement|null} sourceEl 承载可复制文本的元素（一般是 code-block 的 pre）
 * @param {string} text 本次想复制的文本（源元素缺失时写进临时 textarea）
 * @returns {boolean} 是否成功选中文本
 */
function selectSourceText(sourceEl, text) {
  try {
    const sel = window.getSelection();
    if (sourceEl) {
      const range = document.createRange();
      range.selectNodeContents(sourceEl);
      sel.removeAllRanges();
      sel.addRange(range);
      sourceEl.focus({ preventScroll: true });
      return true;
    }
    if (!text) return false;
    if (scratchArea) scratchArea.remove();
    const ta = document.createElement('textarea');
    ta.className = 'copy-fallback-area';
    ta.readOnly = true;
    ta.setAttribute('aria-label', S.COPY_MANUAL_HINT);
    ta.value = text;
    document.body.appendChild(ta);
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(0, ta.value.length);
    ta.addEventListener('blur', () => {
      // 焦点离开说明用户已经复制完（或转去做别的了），收掉临时框
      window.setTimeout(() => {
        if (document.activeElement !== ta) ta.remove();
      }, 300);
    });
    scratchArea = ta;
    return true;
  } catch {
    return false;
  }
}

/**
 * 创建复制按钮。
 *
 * @param {{
 *   getText: () => string,
 *   label?: string, copiedLabel?: string, failedLabel?: string,
 *   ariaLabel?: string, variant?: string, size?: string, icon?: string, block?: boolean,
 *   sourceEl?: () => HTMLElement|null,
 *   successMessage?: (text: string) => string,
 *   onCopied?: (text: string) => void,
 *   onFailed?: (reason: 'permission'|'empty') => void
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, copy: Function}}
 */
export function createCopyButton(props = {}) {
  let current = { ...props };
  /** @type {'idle'|'copying'|'copied'|'failed'} */
  let state = 'idle';
  let timer = null;

  const iconSpan = el('span', { class: 'btn__icon', 'aria-hidden': 'true' }, props.icon || '⧉');
  const labelSpan = el('span', { class: 'btn__label' }, props.label || S.ACTION_COPY);
  const button = el('button', { type: 'button', class: 'btn copy-btn' }, iconSpan, labelSpan);
  button.dataset.state = 'idle';

  const hintNode = el('span', { class: 'copy-fallback', role: 'note' });
  hintNode.hidden = true;

  const wrapper = el('span', { class: 'u-row-tight' });
  wrapper.style.position = 'relative';
  wrapper.style.display = 'inline-flex';
  wrapper.appendChild(button);
  wrapper.appendChild(hintNode);

  /**
   * 切状态并刷新按钮外观。
   * @param {'idle'|'copying'|'copied'|'failed'} next
   */
  function setState(next) {
    state = next;
    button.dataset.state = next;
    if (next === 'copied') {
      setText(labelSpan, current.copiedLabel || `${S.ACTION_COPY}✓`);
      setText(iconSpan, '✓');
      showToast({ message: successText(), kind: 'success', duration: 2600 });
      if (typeof current.onCopied === 'function') current.onCopied(textOf());
    } else if (next === 'failed') {
      setText(labelSpan, current.failedLabel || S.COPY_FAIL);
      setText(iconSpan, '!');
      showToast({ message: S.COPY_FAIL, detail: S.COPY_FAIL_NEXT, kind: 'error', duration: 9000 });
      if (typeof current.onFailed === 'function') current.onFailed('permission');
    } else if (next === 'copying') {
      setText(labelSpan, S.ACTION_COPY);
      setText(iconSpan, '…');
    } else {
      setText(labelSpan, current.label || S.ACTION_COPY);
      setText(iconSpan, current.icon || '⧉');
    }
  }

  /** 取要复制的文本。 */
  function textOf() {
    if (typeof current.getText !== 'function') return '';
    const text = current.getText();
    return typeof text === 'string' ? text : '';
  }

  /** 成功播报文案。 */
  function successText() {
    if (typeof current.successMessage === 'function') return current.successMessage(textOf());
    return S.COPY_OK;
  }

  /**
   * 执行复制（三级降级）。
   * @returns {Promise<boolean>}
   */
  async function copy() {
    if (state === 'copying') return false;
    const text = textOf();
    if (!text.trim()) {
      showToast({ message: S.COPY_EMPTY, kind: 'warn', duration: 3000 });
      if (typeof current.onFailed === 'function') current.onFailed('empty');
      return false;
    }

    setState('copying');
    let ok = await viaClipboard(text);
    if (!ok) ok = viaExecCommand(text);
    if (ok) {
      setState('copied');
      hideHint();
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        timer = null;
        setState('idle');
      }, COPIED_MS);
      return true;
    }

    // 第三级：选中源文本并给出手动指引
    const sourceEl = typeof current.sourceEl === 'function' ? current.sourceEl() : current.sourceEl || null;
    const selected = selectSourceText(sourceEl, text);
    setState('failed');
    if (selected) {
      setText(hintNode, S.COPY_MANUAL_HINT);
      hintNode.hidden = false;
    } else {
      hideHint();
    }
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = null;
      hideHint();
      setState('idle');
    }, COPIED_MS);
    return false;
  }

  /** 收起手动复制提示。 */
  function hideHint() {
    hintNode.hidden = true;
    hintNode.textContent = '';
  }

  const offClick = on(button, 'click', () => {
    copy();
  });

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    if (state === 'idle') setText(labelSpan, current.label || S.ACTION_COPY);
    if (current.ariaLabel) button.setAttribute('aria-label', current.ariaLabel);
    const className = [
      'btn',
      'copy-btn',
      current.variant && current.variant !== 'default' ? `btn--${current.variant}` : '',
      current.size && current.size !== 'md' ? `btn--${current.size}` : '',
      current.block ? 'btn--block' : '',
    ]
      .filter(Boolean)
      .join(' ');
    if (button.className !== className) button.className = className;
  }

  update({});

  return {
    el: wrapper,
    update,
    /** 供快捷键 C 直接调用。 */
    copy,
    /** 解绑事件 + 清定时器（§10.4）。 */
    destroy() {
      offClick();
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
      hideHint();
    },
    /** 当前状态，测试与调试用。 */
    getState: () => state,
  };
}

/** 便捷：生成「复制第 N 级提示词」的成功文案。 */
export function promptCopiedMessage(level) {
  return t(S.COPY_OK_BODY, { n: level });
}
