/**
 * confirm-dialog.js — 二次确认（§11.1 / §13.3）
 *
 * 状态清单：关闭 → 打开（默认焦点在「取消」）→ 确认 / 取消 → 关闭。
 * 键盘路径：Tab 在浮层内循环；Esc = 取消（等价于点「取消」）。
 * ARIA 要点：
 *   - 破坏性确认用 `role="alertdialog"`（读屏会加重语气），普通确认用 `dialog`。
 *   - `aria-modal="true"` + `aria-labelledby`（标题）+ `aria-describedby`（正文）。
 *   - 破坏性操作默认焦点在「取消」，防止手快直接 Enter 掉进陷阱（§13.3）。
 *
 * 依赖：components/modal.js、components/button.js、core/dom.js、core/strings.js
 * 导出：confirmDialog(options) → Promise<boolean>
 */

import { el } from '../core/dom.js';
import { openModal } from './modal.js';
import { createButton } from './button.js';
import { S } from '../core/strings.js';

/** 递增 id，给 aria-describedby 用。 */
let seq = 0;

/**
 * 弹出确认框。
 *
 * @param {{
 *   title: string,
 *   messages?: string[],
 *   list?: string[],
 *   confirmLabel?: string,
 *   cancelLabel?: string,
 *   danger?: boolean,
 *   onConfirm?: () => void|Promise<void>,
 *   confirmBusyLabel?: string
 * }} options
 * @returns {Promise<boolean>} 确认 true；取消 / Esc / 点遮罩 false
 */
export function confirmDialog(options = {}) {
  const {
    title,
    messages = [],
    list = [],
    confirmLabel = S.CONFIRM_DEFAULT_OK,
    cancelLabel = S.CONFIRM_DEFAULT_CANCEL,
    danger = false,
    onConfirm = null,
    confirmBusyLabel = '',
  } = options;

  return new Promise((resolve) => {
    seq += 1;
    const bodyId = `confirm-body-${seq}`;
    let settled = false;

    const body = el('div', { class: 'u-stack', id: bodyId });
    messages.forEach((msg) => body.appendChild(el('p', {}, msg)));
    if (list.length) {
      body.appendChild(
        el('ul', { class: 'modal__list' }, ...list.map((item) => el('li', {}, item))),
      );
    }

    const cancelBtn = createButton({ label: cancelLabel, variant: 'default' });
    const confirmBtn = createButton({
      label: confirmLabel,
      variant: danger ? 'danger' : 'primary',
    });

    const handle = openModal({
      title,
      body,
      variant: danger ? 'danger' : 'default',
      // 破坏性操作不允许误点遮罩关掉后当作"没发生"
      closeOnBackdrop: !danger,
      // 破坏性操作默认焦点在「取消」（§13.3）
      initialFocus: cancelBtn.el,
      footer: [cancelBtn.el, confirmBtn.el],
      onClose: (reason) => {
        if (settled) return;
        settled = true;
        // reason === 'button' = 点了确认；其余（escape / backdrop / cancel）都算取消
        resolve(reason === 'button');
        cancelBtn.destroy();
        confirmBtn.destroy();
      },
    });

    // 破坏性确认用 alertdialog，让读屏软件加重语气
    const dialog = handle.el.querySelector('[role="dialog"]');
    if (dialog && danger) dialog.setAttribute('role', 'alertdialog');
    if (dialog) dialog.setAttribute('aria-describedby', bodyId);

    cancelBtn.el.addEventListener('click', () => handle.close('cancel'));
    confirmBtn.el.addEventListener('click', async () => {
      if (typeof onConfirm !== 'function') {
        handle.close('button');
        return;
      }
      // 请求期间禁用两个按钮，防重复提交（§11.2 #4）
      confirmBtn.update({ loading: true, busyLabel: confirmBusyLabel || confirmLabel });
      cancelBtn.update({ disabled: true });
      try {
        await onConfirm();
        handle.close('button');
      } catch {
        // 失败不关窗：交回控制权，由调用方 toast 说明发生了什么
        confirmBtn.update({ loading: false });
        cancelBtn.update({ disabled: false });
      }
    });
  });
}
