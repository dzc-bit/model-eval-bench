/**
 * batch.js — 批量跑批视图（并发跑「多题 × 多模型」）
 *
 * 职责：
 *   1. 选一组合任务 + 一组模型，做成笛卡尔积，一次排进后台并发执行。
 *   2. 并发数可调，上限是盘符池大小（盘符即沙箱槽位，后端会夹住）。
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
  grading: { text: '正在校验', kind: 'busy' },
  graded: { text: '已完成', kind: 'ok' },
  error: { text: '失败', kind: 'error' },
  cancelled: { text: '已取消', kind: 'warn' },
};

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
  let loading = true;
  let error = null;

  let selectedTasks = new Set();
  let selectedModels = new Set();
  let concurrency = 3;
  let batch = null;
  let pollTimer = null;

  const h1 = el('h1', { tabindex: '-1' }, S.BATCH_TITLE);
  const setupHost = el('div', { class: 'batch__setup' });
  const progressHost = el('div', { class: 'batch__progress' });

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.BATCH_DESC)),
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
      { value: '3', label: '3（等于盘符池大小，推荐）' },
      { value: '4', label: '4（超过盘符数会被夹到上限）' },
    ],
    onChange: (value) => {
      concurrency = Number(value) || 1;
    },
  });
  concurrencyField.setValue('3');

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
    el('span', { class: 'u-spacer' }),
    refreshBtn.el,
    cancelBtn.el,
    startBtn.el,
  );

  setupHost.appendChild(
    el('section', { class: 'panel' },
      el('h2', { class: 'panel__title' }, S.BATCH_SETUP_TITLE),
      el('p', { class: 'u-faint' }, S.BATCH_SETUP_DESC),
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
    setText(summaryEl, `${selectedTasks.size} 题 × ${selectedModels.size} 模型 = ${n} 条；${S.BATCH_DRIVE_NOTE}`);
    const ok = n > 0 && !batch;
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
    const state = ITEM_STATE[item.status] || { text: item.status, kind: 'idle' };
    const done = item.status === 'graded' || item.status === 'error';
    const markKind = item.status === 'error' ? 'fail'
      : item.status === 'graded' ? (item.passed ? 'pass' : 'fail')
        : item.status === 'pending' ? 'idle' : 'busy';

    const mark = createResultMark({
      kind: markKind,
      size: 28,
      animate: done,
      label: '',
    });

    const right = el('div', { class: 'batch__item-right' });
    if (item.status === 'graded') {
      right.appendChild(el('span', { class: 'batch__item-score' },
        `${item.score === null || item.score === undefined ? '—' : item.score} 分`));
    }
    right.appendChild(createStatusDot({ kind: state.kind, text: state.text }).el);

    return el(
      'li',
      { class: `batch__item batch__item--${item.status}` },
      mark.el,
      el('div', { class: 'batch__item-main' },
        el('span', { class: 'batch__item-name' }, `${item.task} × ${item.model}`),
        item.title ? el('span', { class: 'u-faint' }, item.title) : null,
        item.error ? el('span', { class: 'batch__item-error' }, item.error) : null,
      ),
      right,
    );
  }

  /** 渲染批次进度。 */
  function renderProgress() {
    clear(progressHost);
    if (!batch) {
      progressHost.appendChild(
        el('section', { class: 'panel' },
          createEmptyState({
            title: S.BATCH_EMPTY_TITLE,
            desc: S.BATCH_EMPTY_DESC,
          }).el,
        ),
      );
      return;
    }

    const total = batch.total || 0;
    const done = batch.done || 0;
    const passed = batch.passed || 0;
    const running = batch.running || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;

    const bar = el('div', {
      class: 'batch__bar',
      role: 'progressbar',
      'aria-valuenow': String(done),
      'aria-valuemin': '0',
      'aria-valuemax': String(total),
      'aria-label': S.BATCH_PROGRESS_LABEL,
    }, el('div', { class: 'batch__bar-fill', style: { width: `${pct}%` } }));

    const head = el('div', { class: 'batch__head' },
      el('div', { class: 'u-stack', style: { gap: '2px' } },
        el('span', { class: 'u-faint' }, S.BATCH_PROGRESS_LABEL),
        el('span', { class: 'batch__count' }, `${done} / ${total}`),
      ),
      el('div', { class: 'batch__stats' },
        el('span', {}, `${S.BATCH_RUNNING} ${running}`),
        el('span', {}, `${S.BATCH_PASSED} ${passed}`),
        el('span', { class: 'u-faint' }, `并发 ${batch.concurrency}`),
        createStatusDot({
          kind: batch.status === 'finished' ? 'ok' : batch.status === 'cancelled' ? 'warn' : 'busy',
          text: batch.status === 'finished' ? S.BATCH_STATUS_DONE
            : batch.status === 'cancelled' ? S.BATCH_STATUS_CANCELLED : S.BATCH_STATUS_RUNNING,
        }).el,
      ),
      el('span', { class: 'u-spacer' }),
      el('span', { class: 'u-mono u-faint' }, batch.batch_id || ''),
    );

    const list = el('ul', { class: 'batch__list' });
    (batch.items || []).forEach((item) => list.appendChild(renderItem(item)));

    const section = el('section', { class: 'panel' }, head, bar, list);

    if (batch.problems && batch.problems.length) {
      const probs = el('ul', { class: 'batch__problems' });
      batch.problems.forEach((p) => probs.appendChild(el('li', {}, p)));
      section.appendChild(el('div', { class: 'batch__problems-wrap' },
        el('h3', { class: 'batch__pick-title' }, S.BATCH_PROBLEMS_TITLE), probs));
    }

    progressHost.appendChild(section);

    // 结束态不再轮询，并恢复"开始"按钮
    const active = batch.status === 'running' || batch.status === 'cancelling';
    cancelBtn.update({ disabled: !active, reason: active ? '' : S.BATCH_NOT_RUNNING });
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
      const res = await api.post('/batches', { items, concurrency }, { scope });
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
  async function poll() {
    if (!batch || !batch.batch_id) return;
    try {
      const res = await api.get(`/batches/${encodeURIComponent(batch.batch_id)}`, { scope });
      batch = res;
      renderProgress();
      if (batch.status === 'finished' || batch.status === 'cancelled') {
        stopPolling();
        announce(`${S.BATCH_FINISHED}：${batch.done} / ${batch.total}，通过 ${batch.passed}`);
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ABORTED') return;
    }
  }

  /** 起轮询。 */
  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(poll, POLL_MS);
  }

  /** 停轮询。 */
  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  /** 请求取消。 */
  async function cancelBatch() {
    if (!batch || !batch.batch_id) return;
    try {
      await api.post(`/batches/${encodeURIComponent(batch.batch_id)}/cancel`, {}, { scope });
      showToast({ message: S.BATCH_CANCELLED, kind: 'warn' });
      poll();
    } catch (err) {
      if (err instanceof ApiError) {
        showToast({ message: errorTitle(err.code), detail: errorBody(err.code), kind: 'error' });
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
      concurrencyField.destroy();
      startBtn.destroy();
      cancelBtn.destroy();
      refreshBtn.destroy();
    },
  };
}
