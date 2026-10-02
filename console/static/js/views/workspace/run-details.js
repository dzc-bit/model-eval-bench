/**
 * run-details.js — 状态栏两个图标 + 各自的小窗口（2026-10-02 四改）
 *
 * 四改前的形态是对话流末尾的两个折叠节点（运行详情 / 本轮备注）。它们把对话列
 * 往下推了两行，而内容（路径、时长、完整性、备注框）本来就是「想看才看」的东西，
 * 用户口径：**收到顶部状态栏当图标，点开是小窗口**，纵向空间全部让给对话。
 *
 * 本模块因此同时提供两件东西：
 *   1. `toolsEl`：两个图标按钮（由编排层挂进状态栏），无运行记录时禁用并写原因
 *      ——出口不许条件隐藏（红线 1），只是从「常驻折叠节点」变成「常驻图标」。
 *   2. 两个小窗口（components/modal.js 的 slim 变体）：运行详情 / 本轮备注。
 *      正文节点归本模块所有，关窗只是把它从 DOM 上摘下来，下次开窗原地挂回，
 *      所以窗口开着与关着时 `update()` 都是同一套差异更新。
 *
 * 区域锚点 `#ws-region-sandbox` / `#ws-region-run` 仍是书签契约（§13.5）：它们现在
 * 挂在小窗口的正文根上，`focusRegion('sandbox'|'run')` = 开窗（见 workspace.js）。
 *
 * 依赖：core/*、components/*
 * 导出：createRunDetails(handlers) → { toolsEl, update, destroy, openDetails, openNotes,
 *        isOpen, copyPath }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { openModal } from '../../components/modal.js';
import { createIcon } from '../../components/icons.js';
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
  NOTES_ASIDE: '随这一轮记录保存',
  TOOLS_LABEL: '运行工具',
  NO_RUN: '还没有运行记录。',
  NOTE_EMPTY: '这一轮还没有备注。',
  NOTE_HAS: '这一轮已有备注。',
  NOTE_OPEN_HINT: '备注跟着这一轮记录走：写完点「保存备注」才落盘，关窗不会丢草稿。',
  NO_RUN_HINT: '还没有运行记录：先在底部操作栏点「准备沙箱」，这条记录才有路径、时长与日志可看。',
};

/**
 * 创建状态栏工具图标 + 两个小窗口（一个宿主元素，编排层只挂一次）。
 * @param {{onNotesSave: (note: string) => void}} handlers
 * @returns {{toolsEl: HTMLElement, update: Function, destroy: Function,
 *            openDetails: Function, openNotes: Function, isOpen: Function, copyPath: Function}}
 */
export function createRunDetails(handlers) {
  let current = { run: null, busy: '', loading: true, opLog: [] };
  /** 备注草稿：轮询不覆盖，只有换了一轮才从服务端同步（§11.2 #14）。 */
  let noteDraft = '';
  /** 草稿属于哪一轮（run_id）。 */
  let draftRunId = null;
  /** 上一次渲染过的完整性问题清单签名：没变就不重画。 */
  let integritySig = null;
  /** 两个小窗口的句柄（关掉即置空，下次点图标重开）。 */
  let detailsModal = null;
  let notesModal = null;

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

  /** 运行详情正文（开窗时挂进 modal，关窗时摘下来留在本模块）。 */
  const detailsEmpty = el('p', { class: 'u-faint', hidden: true }, T.NO_RUN_HINT);
  const detailsBody = el(
    'section',
    { class: 'ws-win ws-region', id: 'ws-region-sandbox', 'aria-label': T.DETAILS_TITLE },
    detailsEmpty,
    facts,
    integrityCard.el,
    opLogCard.el,
  );

  // ---- 本轮备注 ----
  const noteField = createField({
    label: S.RUN_NOTES_LABEL,
    name: 'run-note',
    type: 'textarea',
    rows: 6,
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
  // 窗口标题已经是「本轮备注」，字段自己的 label 再写一遍是重复：视觉上收掉，
  // 但留在无障碍树里（visually-hidden，不是 display:none——读屏还要靠它给输入框命名）。
  const noteLabel = noteField.el.querySelector('.field__label');
  if (noteLabel) noteLabel.classList.add('visually-hidden');
  /** 备注正文（开窗时挂进 modal）。 */
  const notesBody = el(
    'section',
    { class: 'ws-win ws-win--notes ws-region', id: 'ws-region-run', 'aria-label': S.RUN_NOTES_LABEL },
    el('p', { class: 'u-faint' }, T.NOTE_OPEN_HINT),
    noteField.el,
  );

  // ---- 状态栏图标 ----
  const detailsBtn = createButton({
    variant: 'ghost',
    size: 'sm',
    iconNode: createIcon('info'),
    ariaLabel: T.DETAILS_TITLE,
    title: T.DETAILS_TITLE,
    onClick: () => openDetails(),
  });
  const notesBtn = createButton({
    variant: 'ghost',
    size: 'sm',
    iconNode: createIcon('note'),
    ariaLabel: S.RUN_NOTES_LABEL,
    title: S.RUN_NOTES_LABEL,
    onClick: () => openNotes(),
  });
  const toolsEl = el(
    'div',
    { class: 'ws-tools', role: 'group', 'aria-label': T.TOOLS_LABEL },
    detailsBtn.el,
    notesBtn.el,
  );

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
    // 图标上挂一枚警示：有完整性问题的运行，不进窗口也该看得出来（形状 + 颜色）
    detailsBtn.el.classList.toggle('ws-tools__item--warn', Boolean(problems && problems.length));
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
   * 打开「运行详情」小窗口（图标点击 / focusRegion('sandbox') 的唯一去向）。
   *
   * 没有运行记录时也照开：窗口里说清「还没有记录、下一步点准备沙箱」，比把图标
   * 禁用掉更好——图标禁用只能在状态栏里挂一行原因文字，反而更吵（红线 1 的意思是
   * 「路要摆着且说得出为什么」，不是「必须把按钮画成灰的」）。
   * @returns {object} modal 句柄
   */
  function openDetails() {
    if (detailsModal && detailsModal.isOpen()) return detailsModal;
    detailsModal = openModal({
      title: T.DETAILS_TITLE,
      body: detailsBody,
      variant: 'slim',
      onClose: () => {
        detailsModal = null;
      },
    });
    return detailsModal;
  }

  /**
   * 打开「本轮备注」小窗口：正文 + 保存（保存后不关窗，方便接着改）。
   * @returns {object} modal 句柄
   */
  function openNotes() {
    if (notesModal && notesModal.isOpen()) {
      noteField.focus();
      return notesModal;
    }
    notesModal = openModal({
      title: S.RUN_NOTES_LABEL,
      body: notesBody,
      footer: [saveNoteBtn.el],
      variant: 'slim',
      initialFocus: current.run ? noteField.getControl() : null,
      onClose: () => {
        notesModal = null;
      },
    });
    return notesModal;
  }

  /**
   * 差异更新。
   * @param {{run?: object|null, busy?: string, loading?: boolean, opLog?: string[]}} state
   */
  function update(state = {}) {
    current = { ...current, ...state };
    const run = current.run;
    const hasRun = Boolean(run);

    // 图标不做条件隐藏也不禁用：点开就是这个小窗口，缺记录时窗口自己说清楚。
    // 只在图标上体现「这条记录有没有东西值得看」（有完整性问题的警示 / 有备注）。
    detailsEmpty.hidden = hasRun;
    if (!hasRun) {
      detailsBtn.el.classList.remove('ws-tools__item--warn');
      notesBtn.el.classList.remove('ws-tools__item--on');
      notesBtn.update({ title: T.NO_RUN });
      saveNoteBtn.update({ disabled: true, reason: T.NO_RUN, loading: false });
      return;
    }

    renderFacts(run);
    renderIntegrity(run);

    const lines = current.opLog || [];
    setText(opLogBox, lines.length ? lines.join('\n') : S.SANDBOX_LOG_EMPTY);
    setText(opLogCount, lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '');
    opLogCard.update({ content: opLogBox, hint: opLogCount.textContent });

    // 备注：只有换了一轮才从服务端同步草稿
    const serverNote = run.note || '';
    const runId = run.run_id;
    if (runId !== draftRunId) {
      draftRunId = runId;
      noteDraft = serverNote;
      noteField.setValue(serverNote);
    }
    // 图标上体现「这一轮有没有备注」：空备注时点开是一个空框，值得先看一眼
    notesBtn.el.classList.toggle('ws-tools__item--on', Boolean(String(serverNote).trim()));
    notesBtn.update({ title: String(serverNote).trim() ? T.NOTE_HAS : T.NOTE_EMPTY });
    saveNoteBtn.update({
      loading: current.busy === 'notes',
      busyLabel: S.ACTION_SAVED,
      disabled: false,
      reason: '',
    });
  }

  update({});

  return {
    toolsEl,
    update,
    openDetails,
    openNotes,
    /** 某个窗口是不是开着（编排层决定要不要把状态推给它）。 */
    isOpen: (which) => {
      const handle = which === 'notes' ? notesModal : detailsModal;
      return Boolean(handle && handle.isOpen());
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
      if (detailsModal) detailsModal.close('destroy');
      if (notesModal) notesModal.close('destroy');
      copyPathBtn.destroy();
      integrityCard.destroy();
      opLogCard.destroy();
      noteField.destroy();
      saveNoteBtn.destroy();
      detailsBtn.destroy();
      notesBtn.destroy();
    },
  };
}
