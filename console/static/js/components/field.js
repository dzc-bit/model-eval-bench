/**
 * field.js — 表单行（模型档案与设置页共用，§12.12）
 *
 * 状态清单：默认 / 必填未填（aria-required）/ 校验错误（aria-invalid + aria-describedby）/ 禁用。
 * 键盘路径：label 与控件用 `for` / `id` 关联，Tab 一次即到控件。
 * ARIA 要点：
 *   - `<label for>` 必绑（§12.12 label 关联）
 *   - 说明文字与错误文字都挂 `aria-describedby`（控件级 aria-describedby 可多值空格分隔）
 *   - 必填：`aria-required="true"` + label 上的可见星号与文字
 *   - 出错时 `aria-invalid="true"`
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createField(props) → { el, update, destroy, getValue, setValue, focus, getControl }
 */

import { el, setText, on } from '../core/dom.js';

/** 递增 id。 */
let seq = 0;

/**
 * 创建表单行。
 *
 * @param {{
 *   label: string, name?: string, type?: string, value?: string,
 *   placeholder?: string, hint?: string, error?: string, required?: boolean,
 *   options?: Array<{value: string, label: string}>,   // 传了就是 select
 *   rows?: number, disabled?: boolean, onInput?: (value: string) => void,
 *   onChange?: (value: string) => void, autocomplete?: string
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, getValue: Function, setValue: Function, focus: Function, getControl: Function}}
 */
export function createField(props = {}) {
  seq += 1;
  const controlId = props.name ? `field-${props.name}` : `field-${seq}`;
  const hintId = `${controlId}-hint`;
  const errorId = `${controlId}-error`;

  let current = { ...props };
  let offInput = null;
  let offChange = null;

  const labelText = el('span', { class: 'field__label-text' }, current.label || '');
  const requiredMark = el('span', { class: 'field__required', 'aria-hidden': 'true' }, '＊');
  requiredMark.hidden = !current.required;
  const labelNode = el('label', { class: 'field__label', for: controlId }, labelText, requiredMark);

  /** @type {HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement} */
  let control;
  /** select 当前选项集合的签名，用来判断要不要真的重建 <option>。 */
  let optionsSig = '';
  if (Array.isArray(current.options)) {
    control = el('select', { id: controlId, name: current.name || '' });
    current.options.forEach((opt) => {
      control.appendChild(el('option', { value: opt.value }, opt.label));
    });
    optionsSig = current.options.map((o) => `${o.value} ${o.label}`).join('');
    control.value = current.value || '';
  } else if (current.type === 'textarea') {
    control = el('textarea', {
      id: controlId,
      name: current.name || '',
      rows: current.rows || 3,
      placeholder: current.placeholder || '',
      value: current.value || '',
    });
    control.value = current.value || '';
  } else {
    control = el('input', {
      id: controlId,
      name: current.name || '',
      type: current.type || 'text',
      placeholder: current.placeholder || '',
      value: current.value || '',
      autocomplete: current.autocomplete || 'off',
    });
    control.value = current.value || '';
  }

  const hint = el('div', { class: 'field__hint', id: hintId });
  const error = el('div', { class: 'field__error', id: errorId, role: 'alert' });

  const root = el('div', { class: 'field' }, labelNode, control, hint, error);

  /**
   * 重新计算 aria-describedby（说明 + 错误，按需拼接）。
   */
  function syncDescribedBy() {
    const ids = [];
    if (current.hint) ids.push(hintId);
    if (current.error) ids.push(errorId);
    if (ids.length) control.setAttribute('aria-describedby', ids.join(' '));
    else control.removeAttribute('aria-describedby');
  }

  /**
   * 重建 select 的选项列表。
   *
   * 只在「选项集合真的变了」时动 DOM（用 value+label 拼签名比对），
   * 并且先记住当前选中的值：重建 <option> 会把 select 的 value 打回第一项，
   * 键盘正在选的时候被打断最难受（§11.2 #14）。
   *
   * @param {Array<{value: string, label: string}>} options
   */
  function syncOptions(options) {
    const sig = options.map((o) => `${o.value}${o.label}`).join('');
    if (sig === optionsSig) return;
    const wasFocused = document.activeElement === control;
    const prev = control.value;
    control.textContent = '';
    options.forEach((opt) => {
      control.appendChild(el('option', { value: opt.value }, opt.label));
    });
    // 旧值还在就恢复；旧值已被删掉就交给调用方通过 value 补丁决定
    if (options.some((o) => o.value === prev)) control.value = prev;
    optionsSig = sig;
    if (wasFocused) control.focus();
  }

  /**
   * 差异更新（不重建控件，输入焦点不丢，§11.2 #14）。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };

    if (patch.label !== undefined) setText(labelText, current.label || '');
    requiredMark.hidden = !current.required;
    if (current.required) control.setAttribute('aria-required', 'true');
    else control.removeAttribute('aria-required');

    if (current.hint !== undefined) {
      setText(hint, current.hint || '');
      hint.hidden = !current.hint;
    }
    if (current.error !== undefined) {
      setText(error, current.error || '');
      error.hidden = !current.error;
      if (current.error) control.setAttribute('aria-invalid', 'true');
      else control.removeAttribute('aria-invalid');
    }
    syncDescribedBy();

    control.disabled = Boolean(current.disabled);

    if (Array.isArray(current.options)) {
      // select 的选项是异步到的（模型档案、评分树…），必须真的重建 <option>，
      // 只写 value 的话下拉永远停在占位项上。
      if (Array.isArray(patch.options)) syncOptions(patch.options);
      if (patch.value !== undefined) control.value = current.value || '';
    } else if (patch.value !== undefined) {
      const next = current.value === null || current.value === undefined ? '' : String(current.value);
      // 正在输入时不覆盖用户键入的值
      if (document.activeElement !== control || patch.force) control.value = next;
    }
  }

  if (typeof current.onInput === 'function') {
    offInput = on(control, 'input', () => current.onInput(control.value));
  }
  if (typeof current.onChange === 'function') {
    offChange = on(control, 'change', () => current.onChange(control.value));
  }

  update({});

  return {
    el: root,
    update,
    /** 读当前值。 */
    getValue: () => control.value,
    /** 写值（不触发 input 事件）。 */
    setValue(value) {
      update({ value, force: true });
    },
    /** 聚焦控件。 */
    focus() {
      control.focus();
    },
    /** 拿到原生控件（需要原生 API 时用）。 */
    getControl: () => control,
    /** 解绑事件。 */
    destroy() {
      if (offInput) offInput();
      if (offChange) offChange();
    },
  };
}
