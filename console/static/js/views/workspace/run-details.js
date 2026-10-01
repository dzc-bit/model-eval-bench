/**
 * run-details.js — 对话流末尾的两个折叠节点：「运行详情」与「本轮备注」
 * （2026-10-02 对话流改版；由旧 sandbox-panel 的详情区与 run-bar 迁移而来）
 *
 * - 运行详情（#ws-region-sandbox）：路径、本轮开始时间、模型工作时长、轮次、
 *   运行编号、基线指纹、完整性自检、沙箱操作日志。术语不裸奔，全部收在折叠节点里。
 * - 本轮备注（#ws-region-run）：textarea + 保存，随这一轮记录落盘
 *   （POST /api/runs/{id}/note）。**轮询不覆盖用户正在输入的草稿**（§11.2 #14）：
 *   只有换了运行记录（run_id 变了）才从服务端同步一次备注。
 *
 * 依赖：core/*、components/*
 * 导出：createRunDetails(handlers) → { el, update, destroy, setDetailsOpen, setNotesOpen, copyPath }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createButton } from '../../components/button.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createField } from '../../components/field.js';
import { relativeTime, fullTime } from '../../core/format.js';

/** 秒数 → 「12 分 34 秒」；够短就说秒，够长就说小时，别让人自己换算。 */
function humanSeconds(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const parts = [];
  if (hours) parts.push(`${hours} 小时`);
  if (minutes) parts.push(`${minutes} 分`);
  if (rest && !hours) parts.push(`${rest} 秒`);
  return parts.join(' ') || '不到 1 分钟';
}

/** 本节点新增文案。 */
const T = {
  DETAILS_TITLE: '运行详情',
  FACT_ROUND_STARTED: '本轮开始于',
  FACT_MODEL_WORK: '模型工作时长',
  NOTES_ASIDE: '随这一轮记录保存',
};

/**
 * 创建运行详情 + 本轮备注两个节点（一个宿主元素，编排层只挂一次）。
 * @param {{onNotesSave: (note: string) => void}} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function,
 *            setDetailsOpen: Function, setNotesOpen: Function, copyPath: Function}}
 */
export function createRunDetails(handlers) {
  let current = { run: null, busy: '', loading: true, opLog: [] };
  /** 备注草稿：轮询不覆盖，只有换了一轮才从服务端同步（§11.2 #14）。 */
  let noteDraft = '';
  /** 草稿属于哪一轮（run_id）。 */
  let draftRunId = null;
  /** 上一次渲染过的完整性问题清单签名：没变就不重画。 */
  let integritySig = null;

  // ---- 运行详情：事实格 ----
  const copyPathBtn = createCopyButton({
    label: S.SANDBOX_COPY_PATH,
    size: 'sm',
    getText: () => (current.run ? current.run.sandbox || '' : ''),
    successMessage: () => S.SANDBOX_PATH_COPIED,
  });
  const pathValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const hashValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const startedValue = el('span', { class: 'ws-fact__value' }, '—');
  const workValue = el('span', { class: 'ws-fact__value' }, '—');
  const attemptValue = el('span', { class: 'ws-fact__value' }, '—');
  const runIdValue = el('span', { class: 'ws-fact__value u-mono' }, '—');

  /**
   * 一格事实（label 在上、值在下）。
   * @param {string} label
   * @param {HTMLElement} value
   * @returns {HTMLElement}
   */
  function fact(label, value) {
    return el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, label), value);
  }

  const facts = el(
    'div',
    { class: 'ws-facts' },
    fact(S.SANDBOX_PATH_LABEL, el('span', { class: 'u-row u-row-tight' }, pathValue, copyPathBtn.el)),
    fact(T.FACT_ROUND_STARTED, startedValue),
    fact(T.FACT_MODEL_WORK, workValue),
    fact(S.RUN_ATTEMPT_LABEL, attemptValue),
    fact(S.RUN_RUN_ID_LABEL, runIdValue),
    fact(S.SANDBOX_BASELINE_LABEL, hashValue),
  );

  const integrityList = el('ul', { class: 'integrity-list' });
  const integrityCard = createDetailsCard({ title: S.SANDBOX_INTEGRITY_TITLE, content: integrityList, open: false });

  const opLogBox = el('pre', { class: 'ws-log', tabindex: '0', role: 'region' });
  opLogBox.setAttribute('aria-label', S.SANDBOX_LOG_TITLE);
  const opLogCount = el('span', { class: 'u-faint' });
  const opLogCard = createDetailsCard({ title: S.SANDBOX_LOG_TITLE, content: opLogBox, open: false });

  const detailsBody = el('div', { class: 'ws-node__body' }, facts, integrityCard.el, opLogCard.el);
  const detailsAside = el('span', { class: 'ws-node__aside u-faint u-truncate' });
  const detailsNode = el(
    'details',
    { class: 'ws-node ws-region', id: 'ws-region-sandbox' },
    el('summary', { class: 'ws-node__summary' },
      el('span', { class: 'ws-node__title' }, T.DETAILS_TITLE),
      el('span', { class: 'u-spacer' }),
      detailsAside,
      el('span', { class: 'ws-node__chevron', 'aria-hidden': 'true' }, '›')),
    detailsBody,
  );

  // ---- 本轮备注 ----
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
  const saveNoteBtn = createButton({
    label: S.RUN_SAVE_NOTES,
    onClick: () => {
      if (handlers.onNotesSave) handlers.onNotesSave(noteField.getValue());
    },
  });
  const notesNode = el(
    'details',
    { class: 'ws-node ws-region', id: 'ws-region-run' },
    el('summary', { class: 'ws-node__summary' },
      el('span', { class: 'ws-node__title' }, S.RUN_NOTES_LABEL),
      el('span', { class: 'u-spacer' }),
      el('span', { class: 'ws-node__aside u-faint' }, T.NOTES_ASIDE),
      el('span', { class: 'ws-node__chevron', 'aria-hidden': 'true' }, '›')),
    el('div', { class: 'ws-node__body ws-notes__body' },
      noteField.el,
      el('div', { class: 'u-row' }, saveNoteBtn.el)),
  );

  const root = el('div', { class: 'ws-run-extras' }, detailsNode, notesNode);

  /**
   * 渲染基线完整性：结论来自报告里的 baseline_problems（没有独立完整性接口）。
   * 只在结论变化时重画，避免轮询把用户展开的卡片又折回去。
   * @param {object} run
   */
  function renderIntegrity(run) {
    const problems = run && run.report ? run.report.baseline_problems || [] : null;
    const sig = problems
      ? problems.map((p) => (p && (p.message || p.path)) || '').join('\n')
      : 'pending';
    if (sig === integritySig) return;
    integritySig = sig;
    if (!problems) {
      integrityCard.update({ content: el('p', { class: 'u-faint' }, S.SANDBOX_INTEGRITY_EMPTY) });
      return;
    }
    const list = el('ul', { class: 'integrity-list' });
    if (problems.length) {
      problems.forEach((p) => {
        list.appendChild(
          el(
            'li',
            { class: 'integrity-item' },
            el('span', { 'aria-hidden': 'true' }, '✕'),
            el('span', {}, (p && (p.message || p.path)) || ''),
          ),
        );
      });
      integrityCard.update({ title: `${S.SANDBOX_INTEGRITY_TITLE}（${problems.length}）`, content: list, open: true });
    } else {
      list.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, '✓'),
          el('span', {}, S.SANDBOX_INTEGRITY_OK),
        ),
      );
      integrityCard.update({ title: S.SANDBOX_INTEGRITY_TITLE, content: list, open: false });
    }
  }

  /**
   * 渲染事实格（只在有 run 时调用）。
   * @param {object} run
   */
  function renderFacts(run) {
    setText(pathValue, run.sandbox || '—');
    setText(hashValue, run.baseline_digest || '—');
    setText(runIdValue, run.run_id || '—');
    // 「本轮开始于」而不是「记录建号于」：重建/清空之后 created_at 仍是几个月前，
    // 用它显示出来的时间会把上一个模型和所有挂机时间累计进来。
    const roundStart = run.round_started_at || run.created_at;
    if (roundStart) {
      setText(startedValue, relativeTime(roundStart));
      startedValue.title = fullTime(roundStart);
    } else {
      setText(startedValue, '—');
      startedValue.removeAttribute('title');
    }
    if (typeof run.model_work_seconds === 'number') {
      setText(workValue, humanSeconds(run.model_work_seconds));
      workValue.title = '只算模型被叫起来干活的时长（含工具轮），不含你思考与挂机的时间';
    } else {
      setText(workValue, '—');
      workValue.removeAttribute('title');
    }
    setText(attemptValue, `${run.attempt} / ${run.attempts_allowed}`);
    copyPathBtn.update({ getText: () => run.sandbox || '' });
  }

  /**
   * 差异更新。
   * @param {{run?: object|null, busy?: string, loading?: boolean, opLog?: string[]}} state
   */
  function update(state = {}) {
    current = { ...current, ...state };
    const run = current.run;

    // 没有运行记录时整个宿主隐藏：详情与备注都依附于一条真实记录
    root.hidden = !run;
    if (!run) return;

    // 摘要行：路径存在与否一句话
    setText(detailsAside, run.sandbox ? '' : S.ERR_NO_SANDBOX);
    renderFacts(run);
    renderIntegrity(run);

    const lines = current.opLog || [];
    setText(opLogBox, lines.length ? lines.join('\n') : S.SANDBOX_LOG_EMPTY);
    setText(opLogCount, lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '');
    opLogCard.update({ content: opLogBox, hint: opLogCount.textContent });

    // 备注：只有换了一轮才从服务端同步草稿
    const serverNote = run ? run.note || '' : '';
    const runId = run ? run.run_id : null;
    if (runId !== draftRunId) {
      draftRunId = runId;
      noteDraft = serverNote;
      noteField.setValue(serverNote);
    }
    saveNoteBtn.update({
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
    /** 展开/收起运行详情（焦点跳转前先展开）。 */
    setDetailsOpen(open) {
      detailsNode.open = Boolean(open);
    },
    /** 展开/收起本轮备注。 */
    setNotesOpen(open) {
      notesNode.open = Boolean(open);
    },
    /**
     * 复制沙箱路径（菜单「复制沙箱路径」动作的承载体）。
     * @param {string} path
     * @returns {Promise<boolean>}
     */
    async copyPath(path) {
      copyPathBtn.update({ getText: () => path || '' });
      return copyPathBtn.copy();
    },
    /** 解绑（§10.4）。 */
    destroy() {
      copyPathBtn.destroy();
      integrityCard.destroy();
      opLogCard.destroy();
      noteField.destroy();
      saveNoteBtn.destroy();
    },
  };
}
