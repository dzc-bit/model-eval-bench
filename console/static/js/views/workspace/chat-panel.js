/**
 * chat-panel.js — 人工记录与模型之间的外部交流。
 *
 * 本工具不调用模型 API；答复与过程摘要由用户粘贴或填写，并按 run 保存在本机。
 */

import { el, setText } from '../../core/dom.js';
import { storage } from '../../core/storage.js';
import { createButton } from '../../components/button.js';
import { createField } from '../../components/field.js';

const STORAGE_PREFIX = 'workspace-chat:';

export function createChatPanel(handlers = {}) {
  let currentRunId = '';
  let currentRun = null;

  const replyField = createField({
    name: 'manual-model-reply',
    label: '模型答复（粘贴）',
    type: 'textarea',
    rows: 10,
    placeholder: '把模型实际返回的答复粘贴到这里',
    onInput: persist,
  });
  const summaryField = createField({
    name: 'manual-process-summary',
    label: '过程摘要（手动填写）',
    type: 'textarea',
    rows: 10,
    placeholder: '记录已验证的改动、结果和下一步',
    onInput: persist,
  });
  const runLabel = el('p', { class: 'u-faint chat__run' });
  const saveStatus = el('span', { class: 'u-faint', role: 'status', 'aria-live': 'polite' });
  const copyPromptBtn = createButton({
    label: '复制当前提示词',
    variant: 'ghost',
    onClick: () => handlers.onCopyPrompt && handlers.onCopyPrompt(),
  });
  const emptyMessage = el('p', { class: 'u-faint' }, '准备沙箱后，可在此记录与外部模型的实际交流。');
  const fields = el('div', { class: 'chat__fields' }, replyField.el, summaryField.el);
  const root = el(
    'section',
    { class: 'panel ws-region ws-region--chat', id: 'ws-region-chat', 'aria-labelledby': 'ws-chat-title' },
    el('div', { class: 'panel__head' },
      el('h2', { class: 'panel__title', id: 'ws-chat-title' }, '对话记录'),
      el('span', { class: 'u-spacer' }),
      saveStatus,
      copyPromptBtn.el,
    ),
    runLabel,
    emptyMessage,
    fields,
  );

  function persist() {
    if (!currentRunId) return;
    storage.set(`${STORAGE_PREFIX}${currentRunId}`, {
      reply: replyField.getValue(),
      summary: summaryField.getValue(),
    });
    setText(saveStatus, '已保存在本机');
  }

  function update({ run } = {}) {
    if (run !== undefined) currentRun = run;
    const nextRunId = currentRun && currentRun.run_id ? String(currentRun.run_id) : '';
    const changed = nextRunId !== currentRunId;
    if (changed) {
      currentRunId = nextRunId;
      const saved = currentRunId ? storage.get(`${STORAGE_PREFIX}${currentRunId}`, {}) : {};
      const record = saved && typeof saved === 'object' ? saved : {};
      replyField.setValue(record.reply || '');
      summaryField.setValue(record.summary || '');
      setText(saveStatus, currentRunId ? '本机草稿' : '');
    }

    const ready = Boolean(currentRunId);
    emptyMessage.hidden = ready;
    fields.hidden = !ready;
    replyField.update({ disabled: !ready });
    summaryField.update({ disabled: !ready });
    copyPromptBtn.update({ disabled: !ready });
    setText(runLabel, ready ? `${currentRun.model || '模型'} · ${currentRun.run_id}` : '');
    runLabel.hidden = !ready;
  }

  update({ run: null });

  return {
    el: root,
    update,
    destroy() {
      replyField.destroy();
      summaryField.destroy();
      copyPromptBtn.destroy();
    },
  };
}
