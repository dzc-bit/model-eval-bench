/**
 * run-bar.js — 工作台「本轮备注」折叠卡（2026-10-01 改版后本文件只剩备注）
 *
 * 改版去向（信息架构见 specs/ui-revamp-2026-10-01.md §一）：
 *   - 模型档案选择迁入内置对话卡头（chat-panel.js）；
 *   - 开始时间 / 轮次 / 运行编号 / 运行状态迁入沙箱卡「详情」折叠块（sandbox-panel.js）；
 *   - 改动统计与改动正文迁往题头「查看改动」（workspace.js）。
 *   本卡只负责本轮备注：textarea + 保存，随这一轮记录一起保存（POST /api/runs/{id}/note）。
 *
 * 纪律：**轮询不覆盖用户正在输入的草稿**（§11.2 #14）——只有换了运行记录（run_id 变了）
 * 才从服务端同步一次备注；保存中禁用保存按钮并显示忙态。
 *
 * 依赖：core/*、components/*
 * 导出：createRunBar(handlers) → { el, update, destroy, getNote }
 */

import { el } from '../../core/dom.js';
import { S } from '../../core/strings.js';
import { createField } from '../../components/field.js';
import { createButton } from '../../components/button.js';

/** 本卡新增文案。 */
const T = {
  ASIDE: '只保存在本机',
};

/**
 * 创建本轮备注卡。
 * @param {{onNotesSave: (note: string) => void}} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, getNote: Function}}
 */
export function createRunBar(handlers) {
  let current = { run: null, busy: '', loading: true };
  /** 备注草稿：轮询不覆盖，只有换了一轮才从服务端同步（§11.2 #14）。 */
  let noteDraft = '';
  /** 草稿属于哪一轮（run_id）。 */
  let draftRunId = null;

  const noteField = createField({
    label: S.RUN_NOTES_LABEL,
    name: 'run-note',
    type: 'textarea',
    rows: 4,
    placeholder: S.RUN_NOTES_PLACEHOLDER,
    hint: S.RUN_NOTES_HINT,
    onInput: (value) => {
      noteDraft = value;
    },
  });

  const saveBtn = createButton({
    label: S.RUN_SAVE_NOTES,
    onClick: () => {
      if (handlers.onNotesSave) handlers.onNotesSave(noteField.getValue());
    },
  });

  const body = el(
    'div',
    { class: 'ws-card__body ws-notes__body' },
    noteField.el,
    el('div', { class: 'u-row' }, saveBtn.el),
  );

  const title = el('h2', { class: 'ws-card__title', id: 'ws-run-title' }, S.RUN_NOTES_LABEL);
  const cardAside = el('span', { class: 'ws-card__aside u-faint' }, T.ASIDE);
  const chevron = el('span', { class: 'ws-card__chevron', 'aria-hidden': 'true' }, '›');
  const root = el(
    'details',
    { class: 'ws-card ws-region ws-region--run', id: 'ws-region-run' },
    el('summary', { class: 'ws-card__summary' }, title, el('span', { class: 'u-spacer' }), cardAside, chevron),
    body,
  );

  /**

   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    const run = current.run;

    const serverNote = run ? run.note || '' : '';
    const runId = run ? run.run_id : null;
    if (runId !== draftRunId) {
      draftRunId = runId;
      noteDraft = serverNote;
      noteField.setValue(serverNote);

    }

    saveBtn.update({
      loading: current.busy === 'notes',
      busyLabel: S.ACTION_SAVED,
      disabled: !run,
      reason: run ? '' : S.ERR_NO_RUN,
    });
  }

  update({});

  return {
    el: root,
    update,
    /** 读备注草稿（工作台编排层暂不需要，保留给快捷键与测试）。 */
    getNote: () => noteDraft,

    },
    /** 解绑（§10.4）。 */
    destroy() {
      noteField.destroy();
      saveBtn.destroy();
    },
  };
}
