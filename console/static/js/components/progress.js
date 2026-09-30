/**
 * progress.js — 进度条（§11.1 / §12.7）
 *
 * 状态清单：不确定（进行中，aria-busy）/ 确定（0~100%）/ 完成 / 失败。
 * 键盘路径：不可聚焦，随所在区域的文字被读出。
 * ARIA 要点：
 *   - 确定进度：`role="progressbar"` + `aria-valuenow/min/max` + 中文文本「校验中 12 秒 / 预计 30 秒」
 *   - 不确定进度：**不伪造百分比**，只用 `aria-busy="true"` + 「已用 12 秒，剩余时间未知」
 *   - 文本始终可见（§12.7「加载态：aria-busy + 中文文本」）
 *
 * 依赖：core/dom.js、core/format.js、core/strings.js
 * 导出：createProgress(props) → { el, update, destroy }
 */

import { el, setText } from '../core/dom.js';
import { clock } from '../core/format.js';
import { S, t } from '../core/strings.js';

/**
 * 创建进度组件。
 *
 * @param {{
 *   label?: string, determinate?: boolean, value?: number, max?: number,
 *   elapsed?: number, total?: number|null, state?: 'running'|'ok'|'error'|'idle'
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createProgress(props = {}) {
  let current = { ...props };

  const label = el('span', { class: 'progress__label' }, current.label || S.PROGRESS_IDLE);
  const time = el('span', { class: 'progress__time' });
  const bar = el('div', { class: 'progress__bar' });
  const track = el('div', { class: 'progress__track' }, bar);
  const root = el(
    'div',
    { class: 'progress', role: 'progressbar' },
    el('div', { class: 'progress__head' }, label, time),
    track,
  );

  /**
   * 差异更新。
   * @param {object} patch
   */
  function update(patch = {}) {
    current = { ...current, ...patch };
    const state = current.state || 'idle';
    const determinate = Boolean(current.determinate) && typeof current.value === 'number';
    const max = current.max || 100;
    const value = Math.max(0, Math.min(current.value || 0, max));

    const cls = [
      'progress',
      state === 'ok' ? 'progress--ok' : '',
      state === 'error' ? 'progress--error' : '',
      !determinate && state === 'running' ? 'progress--indeterminate' : '',
    ]
      .filter(Boolean)
      .join(' ');
    if (root.className !== cls) root.className = cls;

    setText(label, current.label || S.PROGRESS_IDLE);

    // 进度语义
    if (state === 'running') root.setAttribute('aria-busy', 'true');
    else root.removeAttribute('aria-busy');

    if (determinate) {
      root.setAttribute('aria-valuemin', '0');
      root.setAttribute('aria-valuemax', String(max));
      root.setAttribute('aria-valuenow', String(Math.round(value)));
      bar.style.width = `${(value / max) * 100}%`;
    } else if (state === 'running') {
      // 不确定进度不伪造百分比（§12.7）
      root.removeAttribute('aria-valuenow');
      root.setAttribute('aria-valuetext', S.STATE_RUNNING);
      bar.style.width = '';
    } else {
      root.removeAttribute('aria-valuenow');
      root.setAttribute('aria-valuetext', state === 'ok' ? S.STATE_DONE : S.STATE_IDLE);
      bar.style.width = state === 'ok' ? '100%' : '0%';
    }

    // 时间文本：已用 + 预计（预计未知时明说未知，不编数字）
    const elapsed = Number(current.elapsed);
    if (Number.isFinite(elapsed) && elapsed >= 0) {
      setText(
        time,
        Number.isFinite(current.total) && current.total > 0
          ? t(S.GRADE_ELAPSED, { time: clock(elapsed), total: clock(current.total) })
          : t(S.GRADE_ELAPSED_UNKNOWN, { time: clock(elapsed) }),
      );
    } else {
      setText(time, '');
    }

    // 进度条的文本副本给读屏
    root.setAttribute(
      'aria-label',
      `${current.label || S.PROGRESS_IDLE}${time.textContent ? `，${time.textContent}` : ''}`,
    );
  }

  update({});

  return {
    el: root,
    update,
    /** 纯展示组件，无需解绑。 */
    destroy() {},
  };
}
