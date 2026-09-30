/**
 * scoreboard.js — 记分板视图（重内容，动态 import 延迟加载）
 *
 * 职责：
 *   1. 行=任务、列=模型的矩阵；单元格显示 pass@1 / 尝试数 / 得分 / Wilson 区间。
 *   2. 偏离目标带的行给「加码 / 降档」提示（§6.4）。
 *   3. 导出 CSV（走后端 format=csv，拿到文本后本地 Blob 下载）。
 *   4. 揭晓过的轮次单列标注，不参与通过率主统计（§16）。
 *
 * 状态：loading / ready / empty / error。
 * 键盘：表头排序按钮可 Tab + Enter；排序 aria-sort 同步。
 * ARIA：`<caption>`、scope=col / scope=row、aria-sort（§12.9）。
 *
 * 依赖：core/*、components/*
 * 导出：createScoreboard(props) → { el, destroy, el_h1 }
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
import { tierBadge } from '../components/badge.js';
import { percent } from '../core/format.js';

/**
 * 创建记分板。
 * @param {{navigate?: Function}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
export function createScoreboard(props = {}) {
  const { navigate } = props;
  const scope = api.scope();

  let data = { models: [], matrix: [], totals: null, note: '', generated_at: '' };
  let loading = true;
  let error = null;
  let sort = { key: 'task', dir: 'asc' };

  const h1 = el('h1', { tabindex: '-1' }, S.SB_TITLE);
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
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.SB_DESC)),
    ),
    toolbar,
    bodyHost,
    legend,
  );

  /**
   * 单元格渲染。
   *
   * 契约（harness/runs.py `_cell_stats`）：
   * `{trials, pass1, pass_any, pass_rate, avg_score, ci_low, ci_high, revealed}`。
   * 颜色不单独承载语义：红绿同时给符号与文字（§12.8）。
   *
   * @param {object} row 矩阵行 `{task, title, tier, target_band, cells}`
   * @param {string} modelId
   * @returns {HTMLElement}
   */
  function renderCell(row, modelId) {
    const cell = (row.cells || {})[modelId];
    if (!cell || !cell.trials) {
      return el('div', { class: 'sb__cell' }, el('span', { class: 'u-faint' }, S.SB_CELL_NO_DATA));
    }
    const offband = isOffBand(row, cell);

    return el(
      'div',
      { class: 'sb__cell' },
      el(
        'span',
        { class: 'sb__cell-main' },
        el('span', { 'aria-hidden': 'true' }, cell.pass1 > 0 ? '✓' : '✕'),
        ' ',
        cell.pass1 > 0 ? S.SB_CELL_PASS : S.SB_CELL_NO_PASS,
      ),
      el(
        'span',
        { class: 'sb__cell-sub' },
        `${t(S.SB_CELL_TRIES, { n: cell.pass1 })} / ${t(S.SB_CELL_TRIES_TOTAL, { n: cell.trials })} · ${cell.avg_score}`,
      ),
      el(
        'span',
        { class: 'sb__cell-sub' },
        t(S.SB_CELL_WILSON, { low: percent(cell.ci_low), high: percent(cell.ci_high) }),
      ),
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
  }

  /**
   * 取这一行的目标通过率区间（比例或百分数都归一成 0~1）。
   * @param {object} row
   * @returns {number[]|null}
   */
  function bandOf(row) {
    const raw = row.target_band;
    if (!Array.isArray(raw) || raw.length < 2) return null;
    const scale = Number(raw[1]) <= 1 ? 1 : 0.01;
    return [Number(raw[0]) * scale, Number(raw[1]) * scale];
  }

  /**
   * 通过率是否落在目标带之外（题太难或太容易，§6.4）。
   * @param {object} row
   * @param {object} cell
   * @returns {boolean}
   */
  function isOffBand(row, cell) {
    const band = bandOf(row);
    if (!band || !cell.trials) return false;
    return cell.pass_rate < band[0] || cell.pass_rate > band[1];
  }

  /**
   * 建表。列 = 任务 + 每个模型一列；行 = 矩阵里的每道题。
   */
  function renderTable() {
    const models = data.models || [];
    const columns = [
      {
        key: 'task',
        label: S.NAV_TASKS,
        sortable: true,
        value: (row) => String(row.task || ''),
        render: (row) =>
          el(
            'div',
            { class: 'u-row-tight' },
            el('span', { class: 'task-card__id' }, row.task),
            tierBadge(row.tier, {}).el,
            el('span', {}, row.title),
          ),
      },
    ].concat(
      models.map((m) => ({
        key: m,
        label: m,
        sortable: true,
        value: (row) => {
          const cell = (row.cells || {})[m];
          if (!cell || !cell.trials) return -1;
          return cell.pass_rate;
        },
        render: (row) => renderCell(row, m),
      })),
    );

    const rows = (data.matrix || []).slice().sort((a, b) => {
      const col = columns.find((c) => c.key === sort.key);
      const pick = col && col.value ? col.value : (row) => String(row[sort.key] || '');
      const av = pick(a);
      const bv = pick(b);
      const cmp = typeof av === 'number' && typeof bv === 'number'
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sort.dir === 'asc' ? cmp : -cmp;
    });

    return createTable({
      caption: S.SB_DESC,
      columns,
      rows,
      rowKey: (row) => row.task,
      sort,
      onSort: (key, dir) => {
        sort = { key, dir };
        rerender();
      },
      empty: { title: S.SB_EMPTY, desc: S.SB_EMPTY_DESC },
      maxRows: 100,
    });
  }

  /**
   * 合计行（后端 totals）：整体 pass@1 与揭晓轮次。
   * @returns {HTMLElement|null}
   */
  function renderTotals() {
    const totals = data.totals;
    if (!totals || !totals.trials) return null;
    return el(
      'dl',
      { class: 'kv kv--inline' },
      el('dt', {}, S.SB_TOTAL_LABEL),
      el(
        'dd',
        {},
        `${t(S.SB_CELL_TRIES, { n: totals.pass1 })} / ${t(S.SB_CELL_TRIES_TOTAL, { n: totals.trials })}`,
        ' · ',
        t(S.SB_CELL_WILSON, { low: percent(totals.ci_low), high: percent(totals.ci_high) }),
        totals.revealed ? ` · ${t(S.SB_CELL_REVEALED, { n: totals.revealed })}` : '',
      ),
    );
  }

  /** 重建表格（排序变化时）。 */
  function rerender() {
    const focusedKey = document.activeElement && document.activeElement.closest('button');
    const wasSortButton = Boolean(focusedKey && focusedKey.classList.contains('table__sort'));
    clear(bodyHost);
    bodyHost.appendChild(renderTable().el);
    if (wasSortButton) {
      const first = bodyHost.querySelector('.table__sort');
      if (first) first.focus();
    }
  }

  /**
   * 三态渲染。
   */
  function render() {
    clear(bodyHost);
    if (loading) {
      bodyHost.appendChild(
        createSkeleton({ rows: 5, variant: 'row', label: `${S.STATE_LOADING}：${S.SB_LOADING_DESC}` }).el,
      );
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
    if (!data.matrix || data.matrix.length === 0) {
      bodyHost.appendChild(
        createEmptyState({
          title: S.SB_EMPTY,
          desc: S.SB_EMPTY_DESC,
          actions: [
            createButton({
              label: S.NAV_TASKS,
              variant: 'primary',
              onClick: () => navigate && navigate('tasks'),
            }).el,
          ],
        }).el,
      );
      return;
    }
    const totals = renderTotals();
    if (totals) bodyHost.appendChild(totals);
    if (data.note) bodyHost.appendChild(el('p', { class: 'u-faint' }, data.note));
    bodyHost.appendChild(renderTable().el);
  }

  /**
   * 导出 CSV。
   */
  async function doExport() {
    exportBtn.update({ loading: true, busyLabel: S.ACTION_LOADING });
    try {
      const csv = await api.text('/scoreboard', { scope, params: { format: 'csv' } });
      if (!csv) throw new ApiError('EXPORT_FAILED');
      api.download(`scoreboard-${dateStamp()}.csv`, csv, 'text/csv;charset=utf-8');
      showToast({ message: S.SB_EXPORT_DONE, kind: 'success', duration: 4000 });
    } catch (err) {
      showToast({ message: errorTitle('EXPORT_FAILED'), detail: errorBody('EXPORT_FAILED'), kind: 'error' });
    } finally {
      exportBtn.update({ loading: false });
    }
  }

  /**
   * 拉取记分板。
   */
  async function load() {
    loading = true;
    error = null;
    render();
    try {
      const res = await api.get('/scoreboard', { scope, params: { format: 'json' } });
      data = {
        models: (res && res.models) || [],
        matrix: (res && res.matrix) || [],
        totals: (res && res.totals) || null,
        note: (res && res.note) || '',
        generated_at: (res && res.generated_at) || '',
      };
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
    /** 解绑 + 取消在途请求。 */
    destroy() {
      scope.cancelAll();
      exportBtn.destroy();
      refreshBtn.destroy();
    },
  };
}

/**
 * 日期戳（导出文件名用）。
 * @returns {string}
 */
function dateStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
}
