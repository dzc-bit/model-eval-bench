/**
 * scoreboard.js — 按模型档案查看记分板
 *
 * 排行榜按题目展示竞争结果；记分板按模型档案展示跨题稳定性。后端仍返回
 * 原有任务×模型矩阵，视图在档案切换后只渲染当前模型的一列，避免把不同
 * 模型的成绩挤在一张难以阅读的宽表里。
 */

import { el, clear } from '../core/dom.js';
import { S, t } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { announce } from '../core/a11y.js';
import { createTable } from '../components/table.js';
import { createButton } from '../components/button.js';
import { createSkeleton } from '../components/skeleton.js';
import { createEmptyState } from '../components/empty-state.js';
import { showToast } from '../components/toast.js';
import { confirmDialog } from '../components/confirm-dialog.js';
import { tierBadge } from '../components/badge.js';
import { percent } from '../core/format.js';

/**
 * 创建记分板。
 * @param {{navigate?: Function, modelId?: string}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
/**
 * 模型档案的身份色：按 id 哈希从六个低饱和暖色里稳定取一个（index）。
 * 与 models.css 的 PROVIDER_HUES 同一套色板——同一模型在记分板和档案页
 * 是同一个颜色，跨页扫读时靠颜色就能对上。
 */
const SB_HUES = ['#86b58c', '#d9b08c', '#8fa9d9', '#c48fb8', '#d9c08c', '#c49a8f'];

/** 字符串 → 稳定的 32 位哈希（与 models.js 的 hashId 同实现）。 */
function hashId(id) {
  let h = 5381;
  const str = String(id || '');
  for (let i = 0; i < str.length; i += 1) {
    h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

function providerHueOf(id) {
  return hashId(id) % SB_HUES.length;
}

export function createScoreboard(props = {}) {
  const { navigate } = props;
  const scope = api.scope();

  let data = { models: [], matrix: [], note: '', generated_at: '' };
  let selectedModel = props.modelId ? String(props.modelId) : '';
  let loading = true;
  let error = null;
  let sort = { key: 'task', dir: 'asc' };
  let tableView = null;

  const h1 = el('h1', { tabindex: '-1' }, S.SB_TITLE);
  const profilesHost = el('section', { class: 'sb__profile-panel', 'aria-labelledby': 'sb-profile-title' });
  const bodyHost = el('div', { class: 'u-stack' });

  const exportBtn = createButton({
    label: S.SB_TOOLBAR_EXPORT,
    variant: 'primary',
    onClick: () => doExport(),
  });
  const refreshBtn = createButton({ label: S.SB_TOOLBAR_REFRESH, variant: 'ghost', onClick: () => load() });

  const toolbar = el(
    'div',
    { class: 'sb__toolbar' },
    el('span', { class: 'u-spacer' }),
    refreshBtn.el,
    exportBtn.el,
  );

  const legend = el(
    'div',
    { class: 'sb__legend' },
    el('span', {}, S.SB_LEGEND_PASS),
    el('span', {}, S.SB_LEGEND_REVEAL),
  );

  const root = el(
    'div',
    { class: 'view scoreboard' },
    el(
      'div',
      { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.SB_DESC)),
    ),
    profilesHost,
    toolbar,
    bodyHost,
    legend,
  );

  function aggregate(modelId) {
    const cells = (data.matrix || [])
      .map((row) => (row.cells || {})[modelId])
      .filter((cell) => cell && Number(cell.trials) > 0);
    const trials = cells.reduce((sum, cell) => sum + Number(cell.trials || 0), 0);
    const pass1 = cells.reduce((sum, cell) => sum + Number(cell.pass1 || 0), 0);
    const revealed = cells.reduce((sum, cell) => sum + Number(cell.revealed || 0), 0);
    return {
      trials,
      pass1,
      revealed,
      passRate: trials ? pass1 / trials : 0,
    };
  }

  function renderProfiles() {
    clear(profilesHost);
    if (!data.models.length) return;
    if (!selectedModel || !data.models.includes(selectedModel)) selectedModel = String(data.models[0]);

    const list = el('nav', { class: 'sb__profiles', 'aria-label': S.SB_PROFILE_LABEL });
    data.models.forEach((modelId) => {
      const stats = aggregate(modelId);
      const item = el('div', { class: 'sb__profile-item' });
      const hue = providerHueOf(modelId);
      const link = el(
        'a',
        {
          class: 'sb__profile',
          href: `#/scoreboard/${encodeURIComponent(String(modelId))}`,
          dataset: { hue: String(hue) },
        },
        el('span', { class: 'sb__profile-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'sb__profile-body' },
          el('span', { class: 'sb__profile-name' }, modelId),
          el('span', { class: 'sb__profile-meta' }, t(S.SB_PROFILE_TRIALS, { pass: stats.pass1, trials: stats.trials })),
        ),
      );
      if (String(modelId) === selectedModel) link.setAttribute('aria-current', 'page');
      item.appendChild(link);
      const delBtn = createButton({
        label: '×',
        variant: 'ghost',
        size: 'sm',
        ariaLabel: `${S.SB_PROFILE_DELETE || '删除模型档案'}：${modelId}`,
        onClick: () => deleteProfile(modelId),
      });
      delBtn.el.classList.add('sb__profile-del');
      item.appendChild(delBtn.el);
      list.appendChild(item);
    });

    profilesHost.appendChild(el('h2', { id: 'sb-profile-title' }, S.SB_PROFILE_LABEL));
    profilesHost.appendChild(el('p', { class: 'sb__profile-desc' }, S.SB_PROFILE_DESC));
    profilesHost.appendChild(list);
  }

  function renderProfileSummary() {
    // 摘要只报「数」，不再重复模型名——芯片行上每颗都写着名字，
    // 下面表格的列头也是它。同一个名字在 40px 内出现三次是噪音。
    if (!selectedModel) return null;
    const stats = aggregate(selectedModel);
    if (!stats.trials) {
      return el(
        'div',
        { class: 'sb__profile-summary', role: 'status' },
        el('span', {}, S.SB_CELL_NO_DATA),
      );
    }
    return el(
      'div',
      { class: 'sb__profile-summary', role: 'status' },
      el('span', {}, t(S.SB_PROFILE_TRIALS, { pass: stats.pass1, trials: stats.trials })),
      el('span', {}, t(S.SB_PROFILE_RATE, { rate: percent(stats.passRate) })),
      stats.revealed ? el('span', {}, t(S.SB_CELL_REVEALED, { n: stats.revealed })) : null,
    );
  }

  function renderCell(row, modelId) {
    const cell = (row.cells || {})[modelId];
    if (!cell || !cell.trials) {
      // 只揭晓过、还没计入主统计的运行：明确标注，而不是让人误以为没测过
      if (cell && cell.revealed) {
        return el(
          'div',
          { class: 'sb__cell' },
          el('span', { class: 'badge badge--muted' }, t(S.SB_CELL_REVEALED, { n: cell.revealed })),
        );
      }
      return el('div', { class: 'sb__cell' }, el('span', { class: 'u-faint' }, S.SB_CELL_NO_DATA));
    }
    const offband = isOffBand(row, cell);
    const node = el(
      'div',
      { class: 'sb__cell' },
      el(
        'span',
        { class: 'sb__cell-main' },
        el('span', { 'aria-hidden': 'true' }, cell.pass1 > 0 ? '✓' : '✕'),
        ' ',
        cell.pass1 > 0 ? S.SB_CELL_PASS : S.SB_CELL_NO_PASS,
      ),
      el('span', { class: 'sb__cell-sub' }, `${t(S.SB_CELL_TRIES, { n: cell.pass1 })} / ${t(S.SB_CELL_TRIES_TOTAL, { n: cell.trials })} · ${cell.avg_score}`),
      el('span', { class: 'sb__cell-sub' }, t(S.SB_CELL_WILSON, { low: percent(cell.ci_low), high: percent(cell.ci_high) })),
      cell.revealed
        ? el('span', { class: 'badge badge--muted' }, t(S.SB_CELL_REVEALED, { n: cell.revealed }))
        : null,
      offband
        ? el(
            'span',
            {
              class: 'badge badge--warn',
              title: t(S.SB_OFFBAND_DESC, { low: percent(bandOf(row)[0]), high: percent(bandOf(row)[1]) }),
            },
            `⚠ ${S.SB_OFFBAND}`,
          )
        : null,
    );
    const runIds = (cell.run_ids || []).filter(Boolean);
    if (runIds.length) {
      node.appendChild(
        createButton({
          label: S.SB_RUN_DELETE || '删除记录',
          variant: 'ghost',
          size: 'sm',
          ariaLabel: `${S.SB_RUN_DELETE || '删除记录'}：${row.task} × ${modelId}`,
          onClick: () => deleteCellRuns(row, cell),
        }).el,
      );
    }
    return node;
  }

  /**
   * 删除模型档案：档案本身与已存密钥同步删除，名下运行记录一并移入隔离区，
   * 记分板的档案芯片随之消失。
   * @param {string} modelId 档案编号
   */
  async function deleteProfile(modelId) {
    const runIds = (data.matrix || [])
      .map((row) => (row.cells || {})[modelId])
      .flatMap((cell) => (cell && cell.run_ids) || [])
      .filter(Boolean);
    const ok = await confirmDialog({
      title: t(S.SB_PROFILE_DELETE_TITLE || '删除模型档案「{id}」？', { id: modelId }),
      messages: [
        runIds.length
          ? t(S.SB_PROFILE_DELETE_RUNS || '它名下的 {n} 条运行记录会一并移入隔离区（runs/_quarantine/manual-deletes/，可手工恢复），记分板不再显示这一列。', { n: runIds.length })
          : (S.SB_PROFILE_DELETE_NO_RUNS || '它名下没有运行记录。'),
        S.SB_PROFILE_DELETE_WARN || '档案本身与已保存的密钥会同步删除；之后需要到「模型档案」页重新新建。',
      ],
      confirmLabel: S.ACTION_DELETE || '删除',
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL || '取消',
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del('/models', { params: { id: modelId, with_runs: '1' } });
      const removed = (res && res.removed_runs || []).length;
      const skipped = (res && res.skipped_busy || []).length;
      showToast({
        message: t(S.SB_PROFILE_DELETED || '档案「{id}」已删除', { id: modelId }),
        detail: removed ? t(S.SB_RUN_DELETED || '已删除 {n} 条运行记录', { n: removed }) : '',
        kind: 'success',
        duration: 5000,
      });
      if (skipped) {
        showToast({ message: t(S.SB_PROFILE_BUSY_SKIP || '{n} 条记录正被对话/校验占用，这次没有删除', { n: skipped }), kind: 'warn', duration: 6000 });
      }
      if (String(selectedModel) === String(modelId)) selectedModel = '';
      await load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'ACTION_FAILED';
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error', duration: 7000 });
    }
  }

  /**
   * 删除一格背后的运行记录（逐条调用 DELETE，服务端会把记录目录移入隔离区）。
   * @param {object} row 记分板行
   * @param {object} cell 单元格统计数据
   */
  async function deleteCellRuns(row, cell) {
    const runIds = (cell.run_ids || []).filter(Boolean);
    if (!runIds.length) return;
    const ok = await confirmDialog({
      title: runIds.length > 1
        ? t(S.SB_RUN_DELETE_MANY || '删除这 {n} 条运行记录？', { n: runIds.length })
        : (S.SB_RUN_DELETE_ONE || '删除这条运行记录？'),
      messages: [
        `将删除：${runIds.join('、')}`,
        S.SB_RUN_DELETE_ARCHIVE || '记录目录会移入 runs/_quarantine/manual-deletes/（可在文件管理器手工恢复），对话记录与评分报告随目录一起归档。',
        S.SB_RUN_DELETE_SANDBOX || '关联的沙箱副本会一并清理；统计里会立刻消失。',
      ],
      confirmLabel: S.ACTION_DELETE || '删除',
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL || '取消',
      danger: true,
    });
    if (!ok) return;
    try {
      for (const runId of runIds) {
        await api.del(`/runs/${encodeURIComponent(runId)}`);
      }
      showToast({ message: t(S.SB_RUN_DELETED || '已删除 {n} 条运行记录', { n: runIds.length }), kind: 'success', duration: 4000 });
      announce(t(S.SB_RUN_DELETED || '已删除 {n} 条运行记录', { n: runIds.length }));
      await load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'ACTION_FAILED';
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error', duration: 7000 });
    }
  }

  function bandOf(row) {
    const raw = row.target_band;
    if (!Array.isArray(raw) || raw.length < 2) return null;
    const scale = Number(raw[1]) <= 1 ? 1 : 0.01;
    return [Number(raw[0]) * scale, Number(raw[1]) * scale];
  }

  function isOffBand(row, cell) {
    const band = bandOf(row);
    if (!band || !cell.trials) return false;
    return cell.pass_rate < band[0] || cell.pass_rate > band[1];
  }

  function renderTable() {
    const modelId = selectedModel;
    const columns = [
      {
        key: 'task',
        label: S.NAV_TASKS,
        sortable: true,
        value: (row) => String(row.task || ''),
        render: (row) => el(
          'div',
          { class: 'u-row-tight' },
          el('span', { class: 'task-card__id' }, row.task),
          tierBadge(row.tier, {}).el,
          el('span', {}, row.title),
        ),
      },
      {
        key: 'model',
        label: S.SB_TABLE_SCORE_COL,
        sortable: true,
        value: (row) => {
          const cell = (row.cells || {})[modelId];
          return cell && cell.trials ? cell.pass_rate : -1;
        },
        render: (row) => renderCell(row, modelId),
      },
    ];
    const rows = (data.matrix || []).slice().sort((a, b) => {
      const col = columns.find((item) => item.key === sort.key) || columns[0];
      const av = col.value(a);
      const bv = col.value(b);
      const cmp = typeof av === 'number' && typeof bv === 'number'
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sort.dir === 'asc' ? cmp : -cmp;
    });
    tableView = createTable({
      caption: `${S.SB_TITLE} · ${modelId}`,
      columns,
      rows,
      rowKey: (row) => row.task,
      sort,
      onSort: (key, dir) => {
        sort = { key, dir };
        rerenderTable();
      },
      empty: { title: S.SB_EMPTY, desc: S.SB_EMPTY_DESC },
      maxRows: 100,
    });
    return tableView.el;
  }

  function rerenderTable() {
    if (tableView) tableView.destroy();
    clear(bodyHost);
    const summary = renderProfileSummary();
    if (summary) bodyHost.appendChild(summary);
    bodyHost.appendChild(renderTable());
    if (data.note) bodyHost.appendChild(el('p', { class: 'u-faint' }, data.note));
  }

  function render() {
    if (tableView) {
      tableView.destroy();
      tableView = null;
    }
    clear(profilesHost);
    clear(bodyHost);
    if (loading) {
      bodyHost.appendChild(createSkeleton({ rows: 5, variant: 'row', label: `${S.STATE_LOADING}：${S.SB_LOADING_DESC}` }).el);
      return;
    }
    if (error) {
      bodyHost.appendChild(
        createEmptyState({
          title: errorTitle(error),
          desc: errorBody(error),
          alert: true,
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => load() }).el],
        }).el,
      );
      return;
    }
    renderProfiles();
    if (!data.models.length) {
      bodyHost.appendChild(
        createEmptyState({
          title: S.SB_PROFILE_EMPTY,
          desc: S.SB_PROFILE_EMPTY_DESC,
          actions: [createButton({ label: S.NAV_MODELS, variant: 'primary', onClick: () => navigate && navigate('models') }).el],
        }).el,
      );
      return;
    }
    const summary = renderProfileSummary();
    if (summary) bodyHost.appendChild(summary);
    if (!data.matrix || data.matrix.length === 0) {
      bodyHost.appendChild(createEmptyState({ title: S.SB_EMPTY, desc: S.SB_EMPTY_DESC }).el);
      return;
    }
    bodyHost.setAttribute('id', 'scoreboard-model-results');
    bodyHost.appendChild(renderTable());
    if (data.note) bodyHost.appendChild(el('p', { class: 'u-faint' }, data.note));
  }

  async function doExport() {
    exportBtn.update({ loading: true, busyLabel: S.ACTION_LOADING });
    try {
      const csv = await api.text('/scoreboard', { scope, params: { format: 'csv' } });
      if (!csv) throw new ApiError('EXPORT_FAILED');
      api.download(`scoreboard-${dateStamp()}.csv`, csv, 'text/csv;charset=utf-8');
      showToast({ message: S.SB_EXPORT_DONE, kind: 'success', duration: 4000 });
    } catch (err) {
      const code = (err && err.code) || 'EXPORT_FAILED';
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error' });
    } finally {
      exportBtn.update({ loading: false });
    }
  }

  async function load() {
    loading = true;
    error = null;
    render();
    try {
      const res = await api.get('/scoreboard', { scope, params: { format: 'json' } });
      data = {
        models: Array.isArray(res && res.models) ? res.models.map(String) : [],
        matrix: Array.isArray(res && res.matrix) ? res.matrix : [],
        note: (res && res.note) || '',
        generated_at: (res && res.generated_at) || '',
      };
      const requestedModel = selectedModel;
      if (requestedModel && !data.models.includes(requestedModel)) {
        selectedModel = data.models[0] || '';
        if (typeof navigate === 'function') {
          navigate(
            'scoreboard',
            selectedModel ? { modelId: selectedModel } : {},
            { replace: true },
          );
          return;
        }
      }
      if (!selectedModel) selectedModel = data.models[0] || '';
      loading = false;
      render();
      announce(S.ANNOUNCE_SB_LOADED);
    } catch (err) {
      loading = false;
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      error = err.code || 'LOAD_FAILED';
      render();
    }
  }

  load();

  return {
    el: root,
    el_h1: h1,
    destroy() {
      scope.cancelAll();
      if (tableView) tableView.destroy();
      exportBtn.destroy();
      refreshBtn.destroy();
    },
  };
}

function dateStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}
