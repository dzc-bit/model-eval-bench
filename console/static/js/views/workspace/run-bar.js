/**
 * run-bar.js — 工作台「本轮信息」区（运行区）
 *
 * 职责：
 *   1. 模型档案选择（每轮绑定一个档案，记分板按「任务 × 模型」统计）。
 *   2. 本轮备注（只在本机保存，随这轮记录走；契约是 POST /api/runs/{id}/note）。
 *   3. 本轮 diff 统计（文件数 / 新增行 / 删除行，取自报告里的 report.diff）。
 *   4. 开始时间、当前轮次 / 总轮次、运行编号、运行状态。
 *
 * 状态：空（未选档案）/ 已选 / 保存中 / 保存成功。
 * 键盘：档案下拉可 Tab；备注文本域可 Tab；输入控件聚焦时单键快捷键失效（由 a11y 判定）。
 * ARIA：label 关联 + 说明 aria-describedby + 必填 aria-required（§12.12）。
 * 纪律：**不因轮询重建输入控件**（§11.2 #14），输入状态保留在组件内部。
 *
 * 契约要点：run.note（单数）、run.attempts_allowed、run.created_at 都是标量字段。
 *   改动正文走 POST /api/runs/{id}/diff → {diff}，由 workspace 编排后回调本组件展示。
 *
 * 依赖：core/*、components/*
 * 导出：createRunBar(handlers) → { el, update, destroy, getNote, copyPath, showDiff }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { errorTitle } from '../../core/api.js';
import { createField } from '../../components/field.js';
import { createButton } from '../../components/button.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createBadge } from '../../components/badge.js';
import { createDetailsCard } from '../../components/details-card.js';
import { relativeTime, fullTime } from '../../core/format.js';

/**
 * 创建运行区。
 * @param {{
 *   onModelChange: (id: string) => void,
 *   onNotesSave: (note: string) => void,
 *   onShowDiff: () => void,
 *   onReloadModels?: () => void
 * }} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, getNote: Function, copyPath: Function, showDiff: Function}}
 */
export function createRunBar(handlers) {
  let current = { run: null, models: [], modelsError: '', modelId: '', busy: '', error: null, loading: true };
  /** 备注草稿：轮询不覆盖用户正在输入的内容，只有换了一轮才从服务端同步（§11.2 #14）。 */
  let noteDraft = '';
  /** 草稿属于哪一轮（run_id）。 */
  let draftRunId = null;
  /** 改动正文的当前状态：{loading, text, error} */
  let diffView = { loading: false, text: '', error: '' };

  const modelField = createField({
    label: S.RUN_MODEL_LABEL,
    name: 'run-model',
    type: 'select',
    required: true,
    options: [{ value: '', label: S.RUN_MODEL_EMPTY }],
    hint: S.RUN_MODEL_HINT,
    onChange: (value) => {
      if (handlers.onModelChange) handlers.onModelChange(value);
    },
  });

  const noteField = createField({
    label: S.RUN_NOTES_LABEL,
    name: 'run-note',
    type: 'textarea',
    rows: 3,
    placeholder: S.RUN_NOTES_PLACEHOLDER,
    hint: S.RUN_NOTES_HINT,
    onInput: (value) => {
      noteDraft = value;
    },
  });

  // ---- 档案列表为空时的可见原因 + 重读入口 ----
  // 空下拉以前既不说为什么空、也不给第二次机会：启动时那一次 GET /api/models
  // 失败就整个会话都选不了模型。这里把原因摊开，并提供只读的重试。
  const modelNote = el('p', { class: 'u-faint run__model-note', role: 'status' });
  const modelRetryBtn = createButton({
    label: S.ACTION_RETRY,
    size: 'sm',
    variant: 'ghost',
    onClick: () => {
      if (!handlers.onReloadModels) return;
      // 重读是一次网络请求，按钮自己担一个忙态，免得点完看着没反应又点一次。
      modelRetryBtn.update({ loading: true, busyLabel: S.ACTION_LOADING });
      Promise.resolve(handlers.onReloadModels())
        .catch(() => {})
        .finally(() => modelRetryBtn.update({ loading: false }));
    },
  });
  modelNote.hidden = true;
  // 重试按钮先装进一个无类名容器再整体切 hidden：createButton 的根节点自带
  // inline 的 display（inline-flex），作者层样式永远盖过浏览器 UA 的
  // [hidden]{display:none}，直接 hidden 那个节点是藏不掉的。
  const modelRetryHost = el('div', {}, modelRetryBtn.el);
  modelRetryHost.hidden = true;
  const saveNoteBtn = createButton({
    label: S.RUN_SAVE_NOTES,
    size: 'sm',
    onClick: () => {
      if (handlers.onNotesSave) handlers.onNotesSave(noteField.getValue());
    },
  });

  // ---- 本轮改动统计 ----
  // 原先是"数字在上、标签在下"的三列，标签还写死在 JS 里；
  // 数字缺失时三个「—」看着像「— + −」，对应关系完全读不出来。
  // 现在每项直接渲染成一句人话（"新增 12 行"），标签来自 strings.js。
  const diffFiles = el('span', { class: 'run__diff-item' }, '—');
  const diffAdd = el('span', { class: 'run__diff-item run__diff-item--add' }, '—');
  const diffDel = el('span', { class: 'run__diff-item run__diff-item--del' }, '—');
  const diffBlock = el(
    'div',
    { class: 'u-stack', style: { gap: 'var(--space-2)' } },
    el('span', { class: 'u-faint' }, S.RUN_DIFF_TITLE),
    el(
      'div',
      { class: 'run__diff' },
      diffFiles,
      diffAdd,
      diffDel,
    ),
  );

  // ---- 改动正文（按需拉取，不轮询） ----
  const diffText = el('pre', { class: 'code-block__pre', tabindex: '0' });
  const diffCard = createDetailsCard({
    title: S.RUN_DIFF_BODY,
    content: diffText,
    open: false,
  });
  const showDiffBtn = createButton({
    label: S.RUN_DIFF_SHOW,
    size: 'sm',
    variant: 'ghost',
    onClick: () => {
      diffCard.setOpen(true);
      if (handlers.onShowDiff) handlers.onShowDiff();
    },
  });

  // ---- 元信息 ----
  const startedValue = el('span', { class: 'ws-fact__value' }, '—');
  const attemptValue = el('span', { class: 'ws-fact__value' }, '—');
  const runIdValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const statusValue = el('span', { class: 'ws-fact__value' }, '—');
  const metaBlock = el(
    'div',
    { class: 'run__grid' },
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.RUN_STARTED_LABEL), startedValue),
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.RUN_ATTEMPT_LABEL), attemptValue),
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.RUN_STATUS_LABEL), statusValue),
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.RUN_RUN_ID_LABEL), runIdValue),
  );

  const revealedFlag = createBadge({ label: S.WS_REVEALED_FLAG, variant: 'danger', glyph: '✕' });
  const calibrationFlag = createBadge({ label: S.RUN_CALIBRATION, variant: 'info', glyph: 'i' });
  const flagHost = el('div', { class: 'u-row' });

  const copyPathBtn = createCopyButton({
    label: S.SANDBOX_COPY_PATH,
    size: 'sm',
    getText: () => (current.run ? current.run.sandbox || '' : ''),
    successMessage: () => S.SANDBOX_PATH_COPIED,
  });

  const root = el(
    'section',
    { class: 'panel ws-region ws-region--run', id: 'ws-region-run', 'aria-labelledby': 'ws-run-title' },
    el(
      'div',
      { class: 'panel__head' },
      el('h2', { class: 'panel__title', id: 'ws-run-title' }, S.RUN_TITLE),
      el('span', { class: 'u-spacer' }),
      flagHost,
    ),
    el(
      'div',
      { class: 'panel__body run__body' },
      el('div', { class: 'run__col' }, modelField.el, modelNote, modelRetryHost, metaBlock),
      el(
        'div',
        { class: 'run__col' },
        noteField.el,
        // 备注的保存与改动的查看/复制是一组动作，同一行，不再散在两列
        el('div', { class: 'u-row run__actions' }, saveNoteBtn.el, el('span', { class: 'u-spacer' }), showDiffBtn.el, copyPathBtn.el),
        diffBlock,
        diffCard.el,
      ),
    ),
  );

  /**
   * 重建档案下拉选项。
   * @param {Array} models
   * @param {string} selected
   */
  function syncModelOptions(models, selected) {
    const options = [{ value: '', label: S.RUN_MODEL_EMPTY }].concat(
      models.map((m) => {
        // 档案只有一个 id 时（config.json 里 id 和 model 都是 "1"），
        // 下拉框显示光秃秃的"1"，使用者不知道它是什么。补可辨识信息或明说未配置。
        const detail = m.model && m.model !== m.id ? m.model : (m.note || '');
        return {
          value: m.id,
          label: detail ? `${m.id}（${detail}）` : `${m.id}（${S.RUN_MODEL_UNSET}）`,
        };
      }),
    );
    // 档案列表没变就不动 select，避免打断键盘选择（§11.2 #14）
    const sig = options.map((o) => o.value).join('|');
    if (sig === modelField.__sig) {
      if (selected !== undefined && selected !== modelField.getValue()) modelField.setValue(selected);
      return;
    }
    modelField.__sig = sig;
    modelField.update({ options, value: selected ?? '' });
  }

  /**
   * 服务端 status → 中文状态词。
   * @param {string} status
   * @returns {string}
   */
  function statusText(status) {
    if (status === 'preparing') return S.RUN_STATUS_PREPARING;
    if (status === 'ready') return S.RUN_STATUS_READY;
    if (status === 'queued') return S.RUN_STATUS_QUEUED;
    if (status === 'grading') return S.RUN_STATUS_GRADING;
    if (status === 'graded') return S.RUN_STATUS_GRADED;
    if (status === 'error') return S.RUN_STATUS_ERROR;
    return S.STATE_UNKNOWN;
  }

  /**
   * 刷新改动正文折叠块。
   */
  function renderDiffBody() {
    if (diffView.loading) {
      setText(diffText, S.RUN_DIFF_LOADING);
    } else if (diffView.error) {
      setText(diffText, diffView.error);
    } else {
      setText(diffText, diffView.text || S.RUN_DIFF_EMPTY);
    }
    diffCard.update({ content: diffText, hint: diffView.loading ? S.STATE_LOADING : '' });
  }

  /**
   * 差异更新。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    const run = current.run;

    syncModelOptions(current.models || [], current.modelId || '');

    // 必填校验：没选档案时给可见错误（§12.12）
    const needModel = Boolean(run) || current.busy === 'prepare';
    modelField.update({
      error: needModel && !current.modelId ? S.RUN_MODEL_REQUIRED : '',
    });

    // 档案为空时把原因摊开：读取失败 ≠ 真的没有档案，两者给不同的话与不同的出口。
    // 首屏数据还没落定（workspace 正在取任务）时先别急着下「没有档案」的结论。
    const modelCount = (current.models || []).length;
    const modelsFailed = Boolean(current.modelsError);
    const modelsPending = Boolean(current.loading) && !modelsFailed;
    if (modelCount === 0 && !modelsPending) {
      setText(modelNote, modelsFailed
        ? t(S.RUN_MODEL_LOAD_FAILED, { reason: errorTitle(current.modelsError) })
        : S.RUN_MODEL_NONE);
      modelNote.hidden = false;
    } else {
      setText(modelNote, '');
      modelNote.hidden = true;
    }
    // 只有「读失败」才值得重试；确实一个档案都没有就该去模型页新增，不摆一个没用的按钮。
    modelRetryHost.hidden = !(modelsFailed && modelCount === 0);

    // 备注：只有切换到另一轮时才从服务端同步，平时绝不覆盖用户草稿
    const serverNote = run ? run.note || '' : '';
    const runId = run ? run.run_id : null;
    if (runId !== draftRunId) {
      draftRunId = runId;
      noteDraft = serverNote;
      noteField.setValue(serverNote);
      // 换了一轮就作废上一轮拉到的 diff 正文
      diffView = { loading: false, text: '', error: '' };
      renderDiffBody();
    }

    // diff 统计：契约里只有跑完校验后报告里才有 diff 数字
    const diff = (run && run.report && run.report.diff) || null;
    const n = (v) => (v === undefined || v === null ? '—' : String(v));
    setText(diffFiles, t(S.RUN_DIFF_FILES, { n: n(diff && diff.files) }));
    setText(diffAdd, t(S.RUN_DIFF_ADD, { n: n(diff && diff.added_lines) }));
    setText(diffDel, t(S.RUN_DIFF_DEL, { n: n(diff && diff.removed_lines) }));
    diffBlock.setAttribute(
      'aria-label',
      diff
        ? `${S.RUN_DIFF_TITLE}：${t(S.RUN_DIFF_FILES, { n: diff.files || 0 })}，${t(S.RUN_DIFF_ADD, { n: diff.added_lines || 0 })}，${t(S.RUN_DIFF_DEL, { n: diff.removed_lines || 0 })}`
        : S.RUN_DIFF_PENDING,
    );
    // 理由文案只在「保存备注」一处展示；查看改动同批禁用，避免同屏重复两行"这一轮还不存在"
    showDiffBtn.update({ disabled: !run, reason: '' });
    showDiffBtn.el.title = run ? '' : S.ERR_NO_RUN;
    copyPathBtn.update({ getText: () => (current.run ? current.run.sandbox || '' : '') });

    // 元信息
    if (run) {
      setText(startedValue, relativeTime(run.created_at));
      startedValue.title = fullTime(run.created_at);
      setText(attemptValue, `${run.attempt} / ${run.attempts_allowed}`);
      setText(statusValue, statusText(run.status));
      setText(runIdValue, run.run_id);
    } else {
      setText(startedValue, '—');
      setText(attemptValue, '—');
      setText(statusValue, '—');
      setText(runIdValue, '—');
    }

    // 标记：揭晓 / 校准
    flagHost.textContent = '';
    if (run && run.revealed) {
      flagHost.appendChild(revealedFlag.el);
      flagHost.appendChild(el('span', { class: 'u-faint' }, S.WS_REVEALED_NOTE));
    }
    if (run && run.calibration) {
      flagHost.appendChild(calibrationFlag.el);
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
    /** 读备注草稿。 */
    getNote: () => noteDraft,
    /**
     * 复制沙箱路径（三级降级，§11.2 #8）。
     * @param {string} path
     * @returns {Promise<boolean>}
     */
    async copyPath(path) {
      copyPathBtn.update({ getText: () => path || '' });
      return copyPathBtn.copy();
    },
    /**
     * 由 workspace 回调：写入改动正文的加载态。
     * @param {{loading?: boolean, text?: string, error?: string}} view
     */
    showDiff(view) {
      diffView = {
        loading: Boolean(view && view.loading),
        text: (view && view.text) || '',
        error: (view && view.error) || '',
      };
      diffCard.setOpen(true);
      renderDiffBody();
    },
    /** 解绑（§10.4）。 */
    destroy() {
      modelField.destroy();
      noteField.destroy();
      saveNoteBtn.destroy();
      modelRetryBtn.destroy();
      showDiffBtn.destroy();
      copyPathBtn.destroy();
      diffCard.destroy();
      revealedFlag.destroy();
      calibrationFlag.destroy();
    },
  };
}
