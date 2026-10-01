/**
 * batch.js — 批量跑批视图（并发跑「多题 × 多模型」）
 *
 * 职责：
 *   1. 选一组合任务 + 一组模型，做成笛卡尔积，一次排进后台并发执行。
 *   2. 并发数可调，上限由本地配置控制。
 *   3. 实时进度：逐条展示 排队/准备沙箱/校验中/已完成/失败，带得分与通过标记。
 *   4. 结果用 result-mark 动画标记每一条的成败（与工作台同一套视觉语言）。
 *   5. 可取消：已开跑的跑完当前一步，未开始的不再派发。
 *
 * 状态：loading / ready / running / empty / error。
 * 键盘：任务与模型是多选列表，原生 checkbox 可 Tab + Space。
 * ARIA：进度用 role="status" 播报；每条结果是 listitem；颜色不单独承载语义。
 *
 * 契约（console/server.py）：
 *   POST /api/batches            {tasks[], models[], attempt, concurrency} → 批次
 *   GET  /api/batches/{id}       批次进度
 *   POST /api/batches/{id}/cancel
 *
 * 依赖：core/*、components/*
 * 导出：createBatch(props) → { el, destroy, el_h1 }
 */

import { el, clear, setText } from '../core/dom.js';
import { S } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { announce } from '../core/a11y.js';
import { createButton } from '../components/button.js';
import { createCopyButton } from '../components/copy-button.js';
import { createField } from '../components/field.js';
import { createSkeleton } from '../components/skeleton.js';
import { createEmptyState } from '../components/empty-state.js';
import { createStatusDot } from '../components/status-dot.js';
import { createResultMark } from '../components/result-mark.js';
import { showToast } from '../components/toast.js';

/** 轮询间隔（毫秒）。 */
const POLL_MS = 2000;
/** 单条状态 → 中文标签与状态点种类。 */
const ITEM_STATE = {
  pending: { text: '排队中', kind: 'idle' },
  preparing: { text: '正在准备沙箱', kind: 'busy' },
  ready: { text: '工作区就绪，等待评分', kind: 'ok' },
  grading: { text: '正在校验', kind: 'busy' },
  graded: { text: '已完成', kind: 'ok' },
  error: { text: '失败', kind: 'error' },
  cancelled: { text: '已取消', kind: 'warn' },
};

/**
 * 已废弃机制的历史错误文案。盘符池（Q:/R:/S: + subst）已从 harness 移除，
 * 旧批次快照里的这类报错只读不改，展示层据此补一句说明，避免用户以为还在用盘符。
 */
const RETIRED_MECHANISM_RE = /盘符|都已占用|subst|E_DRIVE_UNAVAILABLE/i;

/**
 * 这条批次记录是否来自已删除的模型档案。
 *
 * 清单还没拉成功时返回 false：那种情况下 models 为空并不代表档案都被删了。
 * @param {Array<{id: string}>} models
 * @param {boolean} loaded
 * @param {string} id
 * @returns {boolean}
 */
function isModelGone(models, loaded, id) {
  if (!loaded || !id) return false;
  return !models.some((model) => String(model.id) === String(id));
}

/**
 * 给历史报错补上「机制已废弃」的注记；非此类文案返回空串。
 * @param {string} text
 * @returns {string}
 */
function retiredMechanismNote(text) {
  return RETIRED_MECHANISM_RE.test(String(text || '')) ? S.BATCH_LEGACY_MECHANISM_NOTE : '';
}

/**
 * 创建批量跑批视图。
 *
 * @param {{navigate?: Function}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
export function createBatch(props = {}) {
  const scope = api.scope();

  let tasks = [];
  let models = [];
  let modelsLoaded = false;
  let loading = true;
  let error = null;

  let selectedTasks = new Set();
  let selectedModels = new Set();
  let concurrency = 3;
  let autoSend = false;
  let batch = null;
  let pollTimer = null;
  let pollInFlight = false;
  let pollToken = 0;
  const itemViews = new Map();
  let progressView = null;

  const h1 = el('h1', { tabindex: '-1' }, S.BATCH_TITLE);
  const setupHost = el('div', { class: 'batch__setup' });
  const progressHost = el('div', { class: 'batch__progress' });

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, '为每个题目和模型准备独立工作区。进入对应工作台发送提示词、查看接口返回的推理内容，并提交评分。')),
    ),
    setupHost,
    progressHost,
  );

  // ---------------------------------------------------------------- 选择区

  const taskListEl = el('div', { class: 'batch__pick', role: 'group', 'aria-label': S.BATCH_PICK_TASKS });
  const modelListEl = el('div', { class: 'batch__pick', role: 'group', 'aria-label': S.BATCH_PICK_MODELS });
  const summaryEl = el('p', { class: 'u-faint', role: 'status', 'aria-live': 'polite' });

  const concurrencyField = createField({
    label: S.BATCH_CONCURRENCY,
    name: 'batch-concurrency',
    type: 'select',
    hint: S.BATCH_CONCURRENCY_HINT,
    options: [
      { value: '1', label: '1（串行，最稳）' },
      { value: '2', label: '2' },
      { value: '3', label: '3（推荐）' },
      { value: '4', label: '4（超过配置上限会被夹住）' },
    ],
    onChange: (value) => {
      concurrency = Number(value) || 1;
    },
  });
  concurrencyField.setValue('3');

  // 无人值守开关：默认关，跑批照旧只准备沙箱、由人发送与校验
  const autoSendBox = el('input', {
    type: 'checkbox',
    id: 'batch-auto-send',
    onChange: (event) => { autoSend = event.target.checked; },
  });
  const autoSendRow = el(
    'label',
    { class: 'batch__pick-row', for: 'batch-auto-send' },
    autoSendBox,
    el('span', { class: 'batch__pick-label' }, S.BATCH_AUTO_SEND),
    el('span', { class: 'u-faint' }, S.BATCH_AUTO_SEND_HINT),
  );

  const startBtn = createButton({
    label: S.BATCH_START,
    variant: 'primary',
    busyLabel: S.BATCH_STARTING,
    onClick: () => startBatch(),
  });
  const cancelBtn = createButton({
    label: S.BATCH_CANCEL,
    variant: 'danger',
    onClick: () => cancelBatch(),
  });
  const refreshBtn = createButton({
    label: S.ACTION_REFRESH,
    variant: 'ghost',
    onClick: () => load(),
  });

  const controls = el(
    'div',
    { class: 'batch__controls' },
    concurrencyField.el,
    autoSendRow,
    el('span', { class: 'u-spacer' }),
    refreshBtn.el,
    cancelBtn.el,
    startBtn.el,
  );

  setupHost.appendChild(
    el('section', { class: 'panel' },
      el('h2', { class: 'panel__title' }, S.BATCH_SETUP_TITLE),
      el('p', { class: 'u-faint' }, '勾选题与模型后，为每个组合准备独立工作区。就绪后打开对应工作台，在内置对话中让模型操作；完成后启动评分，评分结束会自动回收工作区。'),
      el('div', { class: 'batch__pickers' },
        el('div', {}, el('h3', { class: 'batch__pick-title' }, S.BATCH_PICK_TASKS), taskListEl),
        el('div', {}, el('h3', { class: 'batch__pick-title' }, S.BATCH_PICK_MODELS), modelListEl),
      ),
      summaryEl,
      controls,
    ),
  );

  /**
   * 选中项 → 文字摘要。
   */
  function refreshSummary() {
    const n = selectedTasks.size * selectedModels.size;
    setText(summaryEl, `${selectedTasks.size} 题 × ${selectedModels.size} 模型 = ${n} 个独立会话；同一题可同时分配给多个模型。${S.BATCH_DRIVE_NOTE}`);
    const active = batch && (batch.status === 'running' || batch.status === 'cancelling');
    const ok = n > 0 && !active;
    startBtn.update({ disabled: !ok, reason: ok ? '' : S.BATCH_NEED_PICK });
  }

  /**
   * 一条可选中的项（checkbox + 文案）。
   * @param {{id: string, label: string, sub?: string}} item
   * @param {Set<string>} set
   * @returns {HTMLElement}
   */
  function pickRow(item, set) {
    const id = `batch-pick-${item.id}`;
    const box = el('input', {
      type: 'checkbox',
      id,
      checked: set.has(item.id),
      onChange: (event) => {
        if (event.target.checked) set.add(item.id);
        else set.delete(item.id);
        refreshSummary();
      },
    });
    return el(
      'label',
      { class: 'batch__pick-row', for: id },
      box,
      el('span', { class: 'batch__pick-id' }, item.id),
      el('span', { class: 'batch__pick-label' }, item.label),
      item.sub ? el('span', { class: 'u-faint' }, item.sub) : null,
    );
  }

  /** 渲染题与模型的勾选列表。 */
  function renderPickers() {
    clear(taskListEl);
    clear(modelListEl);
    tasks.forEach((task) => {
      taskListEl.appendChild(pickRow(
        { id: task.id, label: task.title || '', sub: task.tier || '' }, selectedTasks));
    });
    models.forEach((model) => {
      modelListEl.appendChild(pickRow(
        { id: model.id, label: model.name || model.model || '', sub: model.base_url || '' },
        selectedModels));
    });
    refreshSummary();
  }

  // ---------------------------------------------------------------- 进度区

  /**
   * 单条进度行。
   * @param {object} item
   * @returns {HTMLElement}
   */
  function renderItem(item) {
    let view = itemViews.get(item.index);
    if (!view) {
      view = createItemView(item);
      itemViews.set(item.index, view);
    }
    view.update(item);
    return view.el;
  }

  function createItemView(initialItem) {
    let current = initialItem;
    let gradeRequested = false;
    let eventSignature = '';
    const state = ITEM_STATE[initialItem.status] || { text: initialItem.status, kind: 'idle' };
    const mark = createResultMark({ kind: 'idle', size: 28, animate: false, label: '' });
    const statusDot = createStatusDot({ kind: state.kind, text: state.text });
    const score = el('span', { class: 'batch__item-score', hidden: true });
    const runId = el('span', { class: 'u-mono u-faint', hidden: true });
    const path = el('span', { class: 'u-mono u-faint', hidden: true, style: { overflowWrap: 'anywhere' } });
    const openLink = el('a', {
      class: 'btn btn--primary', target: '_blank', rel: 'noopener', hidden: true,
    }, '打开工作台');
    const gradeBtn = createButton({
      label: '启动评分',
      size: 'sm',
      disabled: true,
      onClick: async () => {
        if (!current.run_id || current.status !== 'ready' || gradeRequested) return;
        gradeBtn.update({ loading: true, busyLabel: '正在启动评分' });
        try {
          await api.post(`/runs/${encodeURIComponent(current.run_id)}/grade`, {}, { scope });
          gradeRequested = true;
          showToast({ message: '评分已启动', detail: `${current.task} × ${current.model}`, kind: 'success' });
        } catch (err) {
          const code = err instanceof ApiError ? err.code : 'INTERNAL';
          if (code === 'RUN_BUSY' || code === 'BUSY' || err?.backendCode === 'E_RUN_BUSY') {
            gradeRequested = true;
          }
          showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error' });
        } finally {
          gradeBtn.update({ loading: false, disabled: current.status !== 'ready' || gradeRequested });
        }
      },
    });
    const promptPre = el('pre', { class: 'code-block__pre', tabindex: '0' });
    const promptCopy = createCopyButton({
      label: '复制当前轮提示词',
      size: 'sm',
      getText: () => String(current.prompt || ''),
      sourceEl: () => promptPre,
      successMessage: () => '已复制当前轮提示词',
    });
    const promptDetails = el('details', { class: 'batch__session-prompt' },
      el('summary', {}, '当前轮题目提示词'),
      el('div', { class: 'u-row', style: { justifyContent: 'flex-end', margin: 'var(--space-2) 0' } }, promptCopy.el),
      promptPre,
    );
    const events = el('ol', { class: 'batch__events' });
    const eventDetails = el('details', {}, el('summary', {}, '公开进度记录'), events);
    const errorNode = el('span', { class: 'batch__item-error', hidden: true });
    const legacyNote = el('span', { class: 'u-faint', hidden: true });
    const modelGoneBadge = el('span', {
      class: 'badge badge--muted', hidden: true, title: S.BATCH_ITEM_MODEL_GONE_HINT,
    }, S.BATCH_ITEM_MODEL_GONE);
    const main = el('div', { class: 'batch__item-main' },
      el('span', { class: 'batch__item-name' }, `${initialItem.task} × ${initialItem.model}`),
      modelGoneBadge,
      initialItem.title ? el('span', { class: 'u-faint' }, initialItem.title) : null,
      errorNode,
      legacyNote,
    );
    const head = el('div', { class: 'u-row', style: { alignItems: 'center', flexWrap: 'wrap' } },
      mark.el, main, el('span', { class: 'u-spacer' }), score, statusDot.el);
    const actions = el('div', { class: 'u-row', style: { alignItems: 'center', flexWrap: 'wrap' } },
      openLink, gradeBtn.el, runId, path);
    const card = el('li', {
      class: `batch__item batch__item--${initialItem.status}`,
      style: { display: 'flex', flexDirection: 'column', alignItems: 'stretch', minWidth: '0' },
    }, head, actions, promptDetails, eventDetails);

    return {
      el: card,
      update(item) {
        current = item;
        const stateNow = ITEM_STATE[item.status] || { text: item.status, kind: 'idle' };
        const done = item.status === 'graded' || item.status === 'error' || item.status === 'cancelled';
        const markKind = item.status === 'error' ? 'fail'
          : item.status === 'graded' ? (item.passed ? 'pass' : 'fail')
            : item.status === 'pending' || item.status === 'cancelled' ? 'idle' : 'busy';
        mark.update({ kind: markKind, animate: done, label: '' });
        statusDot.update({ kind: stateNow.kind, text: stateNow.text });
        card.className = `batch__item batch__item--${item.status}`;
        score.hidden = item.status !== 'graded';
        setText(score, `${item.score === null || item.score === undefined ? '—' : item.score} 分`);
        setText(runId, item.run_id ? `运行：${item.run_id}` : '');
        runId.hidden = !item.run_id;
        setText(path, item.sandbox ? `目录：${item.sandbox}` : '');
        path.hidden = !item.sandbox;
        openLink.hidden = !item.run_id;
        if (item.run_id) {
          openLink.href = `#/workspace/${encodeURIComponent(item.task)}/chat/${encodeURIComponent(item.run_id)}`;
          if (item.status !== 'ready') gradeRequested = gradeRequested || item.status === 'grading'
            || item.status === 'graded' || item.status === 'error';
          gradeBtn.update({ disabled: item.status !== 'ready' || gradeRequested });
        } else {
          gradeBtn.update({ disabled: true });
        }
        promptPre.textContent = item.prompt || '这道题没有配置当前轮提示词。';
        promptCopy.update({ getText: () => String(current.prompt || '') });
        const nextEvents = item.events || [];
        const nextSignature = nextEvents.map((entry) => `${entry.at}|${entry.kind}|${entry.message}`).join('\n');
        if (nextSignature !== eventSignature) {
          eventSignature = nextSignature;
          clear(events);
          nextEvents.forEach((entry) => {
            const note = retiredMechanismNote(entry.message);
            events.appendChild(el('li', {},
              `${entry.at || ''} ${entry.message || ''}`.trim(),
              note ? el('span', { class: 'u-faint' }, note) : null));
          });
        }
        errorNode.hidden = !item.error;
        setText(errorNode, item.error || '');
        const note = retiredMechanismNote(item.error);
        legacyNote.hidden = !note;
        setText(legacyNote, note);
        const gone = isModelGone(models, modelsLoaded, item.model);
        modelGoneBadge.hidden = !gone;
      },
      destroy() {
        mark.destroy();
        statusDot.destroy();
        gradeBtn.destroy();
        promptCopy.destroy();
      },
    };
  }

  /** 渲染批次进度。 */
  function renderProgress() {
    if (!batch) {
      if (!progressHost.firstChild) {
        progressHost.appendChild(el('section', { class: 'panel' }, createEmptyState({
          title: S.BATCH_EMPTY_TITLE,
          desc: S.BATCH_EMPTY_DESC,
        }).el));
      }
      return;
    }

    if (!progressView || progressView.batchId !== batch.batch_id) {
      itemViews.forEach((view) => view.destroy());
      itemViews.clear();
      clear(progressHost);

      const count = el('span', { class: 'batch__count' });
      const runningValue = el('span');
      const readyValue = el('span');
      const queuedValue = el('span');
      const passedValue = el('span');
      const concurrencyValue = el('span', { class: 'u-faint' });
      const batchState = createStatusDot({ kind: 'busy', text: S.BATCH_STATUS_RUNNING });
      const fill = el('div', { class: 'batch__bar-fill' });
      const bar = el('div', {
        class: 'batch__bar',
        role: 'progressbar',
        'aria-valuenow': '0',
        'aria-valuemin': '0',
        'aria-valuemax': String(batch.total || 0),
        'aria-label': S.BATCH_PROGRESS_LABEL,
      }, fill);
      const head = el('div', { class: 'batch__head' },
        el('div', { class: 'u-stack', style: { gap: '2px' } },
          el('span', { class: 'u-faint' }, S.BATCH_PROGRESS_LABEL), count),
        el('div', { class: 'batch__stats' },
          runningValue, readyValue, queuedValue, passedValue, concurrencyValue, batchState.el),
        el('span', { class: 'u-spacer' }),
        el('span', { class: 'u-mono u-faint' }, batch.batch_id || ''),
      );
      const list = el('ul', {
        class: 'batch__list',
        style: {
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 340px), 1fr))',
          gap: 'var(--space-3)',
          padding: '0',
        },
      });
      const section = el('section', { class: 'panel' }, head, bar, list);
      if (batch.problems && batch.problems.length) {
        const probs = el('ul', { class: 'batch__problems' });
        batch.problems.forEach((problem) => probs.appendChild(el('li', {}, problem)));
        section.appendChild(el('div', { class: 'batch__problems-wrap' },
          el('h3', { class: 'batch__pick-title' }, S.BATCH_PROBLEMS_TITLE), probs));
      }
      progressHost.appendChild(section);
      progressView = {
        batchId: batch.batch_id,
        count, runningValue, readyValue, queuedValue, passedValue,
        concurrencyValue, batchState, bar, fill, list,
      };
    }

    const total = batch.total || 0;
    const done = batch.done || 0;
    const passed = batch.passed || 0;
    const running = batch.running || 0;
    const ready = (batch.items || []).filter((item) => item.status === 'ready').length;
    const queued = batch.queued === undefined
      ? (batch.items || []).filter((item) => item.status === 'pending').length
      : batch.queued;
    const pct = total ? Math.round((done / total) * 100) : 0;
    setText(progressView.count, `${done} / ${total}`);
    setText(progressView.runningValue, `${S.BATCH_RUNNING} ${running}`);
    setText(progressView.readyValue, `就绪 ${ready}`);
    setText(progressView.queuedValue, `排队 ${queued}`);
    setText(progressView.passedValue, `${S.BATCH_PASSED} ${passed}`);
    setText(progressView.concurrencyValue, `并发 ${batch.concurrency}`);
    progressView.batchState.update({
      kind: batch.status === 'finished' ? 'ok' : batch.status === 'cancelled' ? 'warn' : 'busy',
      text: batch.status === 'finished' ? S.BATCH_STATUS_DONE
        : batch.status === 'cancelled' ? S.BATCH_STATUS_CANCELLED
          : batch.status === 'cancelling' ? S.BATCH_STATUS_CANCELLING : S.BATCH_STATUS_RUNNING,
    });
    progressView.bar.setAttribute('aria-valuenow', String(done));
    progressView.bar.setAttribute('aria-valuemax', String(total));
    progressView.fill.style.width = `${pct}%`;
    (batch.items || []).forEach((item) => {
      const card = renderItem(item);
      if (card.parentNode !== progressView.list) progressView.list.appendChild(card);
    });

    // 结束态不再轮询，并恢复"开始"按钮
    const active = batch.status === 'running' || batch.status === 'cancelling';
    const cancelling = batch.status === 'cancelling';
    cancelBtn.update({
      disabled: !active,
      loading: cancelling,
      busyLabel: '正在停止批次',
      reason: active ? '' : S.BATCH_NOT_RUNNING,
    });
    startBtn.update({ disabled: active, reason: active ? S.BATCH_ALREADY_RUNNING : '' });
  }

  // ---------------------------------------------------------------- 动作

  /**
   * 拉任务与模型清单。
   */
  async function load() {
    loading = true;
    error = null;
    try {
      const [taskRes, modelRes] = await Promise.all([
        api.get('/tasks', { scope }),
        api.get('/models', { scope }),
      ]);
      tasks = (taskRes && taskRes.tasks) || [];
      models = (modelRes && modelRes.models) || [];
      loading = false;
      modelsLoaded = true;
      // 默认全选题、选第一个模型，减少点击
      if (!selectedTasks.size) tasks.forEach((t) => selectedTasks.add(t.id));
      if (!selectedModels.size && models.length) selectedModels.add(models[0].id);
      renderPickers();
    } catch (err) {
      loading = false;
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      error = err.code || 'LOAD_FAILED';
      clear(setupHost);
      setupHost.appendChild(
        el('section', { class: 'panel' },
          createEmptyState({
            title: errorTitle(error),
            desc: errorBody(error),
            alert: true,
            actions: [createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => load() }).el],
          }).el,
        ),
      );
    }
  }

  /**
   * 起一批。
   */
  async function startBatch() {
    const items = [];
    selectedTasks.forEach((task) => {
      selectedModels.forEach((model) => items.push({ task, model }));
    });
    if (!items.length) {
      showToast({ message: S.BATCH_NEED_PICK, kind: 'warn' });
      return;
    }
    startBtn.update({ loading: true });
    try {
      const res = await api.post('/batches', { items, concurrency, auto_send: autoSend }, { scope });
      batch = res;
      announce(`${S.BATCH_STARTED}：${batch.total} 条`);
      showToast({ message: S.BATCH_STARTED, detail: `${batch.total} 条，并发 ${batch.concurrency}`, kind: 'success' });
      renderProgress();
      startPolling();
    } catch (err) {
      const title = err instanceof ApiError ? errorTitle(err.code) : S.APP_UNEXPECTED_TITLE;
      const body = err instanceof ApiError ? errorBody(err.code) : '';
      showToast({ message: title, detail: body, kind: 'error', duration: 9000 });
    } finally {
      startBtn.update({ loading: false });
      refreshSummary();
    }
  }

  /**
   * 取一次批次进度。
   */
  async function poll(token = pollToken) {
    if (!batch || !batch.batch_id) return;
    if (token !== pollToken || pollInFlight) return;
    const requestedBatchId = batch.batch_id;
    pollInFlight = true;
    try {
      const res = await api.get(`/batches/${encodeURIComponent(requestedBatchId)}`, { scope });
      // 取消、切批或销毁视图后，旧响应不能覆盖当前状态。
      if (token !== pollToken || !batch || batch.batch_id !== requestedBatchId) return;
      batch = res;
      renderProgress();
      if (batch.status === 'finished' || batch.status === 'cancelled') {
        stopPolling();
        announce(`${S.BATCH_FINISHED}：${batch.done} / ${batch.total}，通过 ${batch.passed}`);
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ABORTED') return;
    } finally {
      pollInFlight = false;
    }
  }

  /** 起轮询。 */
  function startPolling() {
    stopPolling();
    const token = pollToken;
    const tick = async () => {
      if (token !== pollToken) return;
      await poll(token);
      if (token !== pollToken || !batch
          || (batch.status !== 'running' && batch.status !== 'cancelling')) return;
      pollTimer = window.setTimeout(tick, POLL_MS);
    };
    pollTimer = window.setTimeout(tick, 0);
  }

  /** 停轮询。 */
  function stopPolling() {
    pollToken += 1;
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  /** 请求取消。 */
  async function cancelBatch() {
    if (!batch || !batch.batch_id) return;
    const batchId = batch.batch_id;
    // 先把 UI 推到 cancelling，并使在途 GET 失效；服务端响应回来前也不能再次提交。
    stopPolling();
    batch = { ...batch, status: 'cancelling' };
    renderProgress();
    try {
      await api.post(`/batches/${encodeURIComponent(batchId)}/cancel`, {}, { scope });
      showToast({ message: S.BATCH_CANCELLED, kind: 'warn' });
      if (batch && batch.batch_id === batchId) {
        startPolling();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        showToast({ message: errorTitle(err.code), detail: errorBody(err.code), kind: 'error' });
      }
      // 取消请求失败时恢复对同一批次的观察，避免 UI 永久停在 cancelling。
      if (batch && batch.batch_id === batchId) {
        batch = { ...batch, status: 'running' };
        renderProgress();
        startPolling();
      }
    }
  }

  // 首屏：拉清单；如果已有批次在跑，直接接上进度
  load().then(async () => {
    try {
      const res = await api.get('/batches', { scope });
      const list = (res && res.batches) || [];
      const active = list.find((b) => b.status === 'running' || b.status === 'cancelling');
      if (active) {
        batch = active;
        renderProgress();
        startPolling();
      } else if (list.length) {
        batch = list[0];
        renderProgress();
      }
    } catch {
      /* 没有批次也不影响使用 */
    }
  });

  renderProgress();

  return {
    el: root,
    el_h1: h1,
    /** 解绑（§10.4）。 */
    destroy() {
      stopPolling();
      scope.cancelAll();
      itemViews.forEach((view) => view.destroy());
      itemViews.clear();
      if (progressView) progressView.batchState.destroy();
      concurrencyField.destroy();
      startBtn.destroy();
      cancelBtn.destroy();
      refreshBtn.destroy();
    },
  };
}
