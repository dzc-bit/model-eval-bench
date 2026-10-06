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

/** 本视图新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  SB_CELL_BEST: '最高分 {best} · 均分 {avg}',
  SB_PROFILE_BEST: '最高分 {best}',
  SB_CELL_ATTEMPTS: '结束过 {n} 次',
  SB_PROFILE_DELETE_TITLE: '删除模型档案「{id}」？',
  SB_PROFILE_DELETE: '删除模型档案',
  SB_PROFILE_DELETED: '档案「{id}」已删除',
  SB_PROFILE_BUSY_SKIP: '{n} 条记录正被对话/校验占用，这次没有删除',
  SB_PROFILE_DELETE_BODY: '档案、已保存的密钥、名下 {n} 条运行记录与 {m} 条成绩会一并彻底删除，不可恢复。',
  SB_PROFILE_DELETE_BODY_RUNS: '档案、已保存的密钥与名下 {n} 条运行记录会一并彻底删除，不可恢复。',
  SB_PROFILE_DELETE_BODY_ENTRIES: '档案、已保存的密钥与名下 {m} 条成绩会一并彻底删除，不可恢复。',
  SB_PROFILE_DELETE_BODY_NONE: '档案与已保存的密钥会一起删除。它名下没有运行记录与成绩。',
  SB_PROFILE_DELETE_HINT: '删除后这一列从记分板与排行榜上一并消失；之后需要到「模型档案」页重新新建。',
  SB_PROFILE_TRIALS: '结束 {n} 次 · pass@1 {pass}/{n}',
  SB_PROFILE_TASKS_PASSED: '做对 {pass}/{n} 题',
  // 格子主判定看「最好一次是否全绿」；pass@1 降为副标（多轮题的 pass@1 天然偏低）
  SB_CELL_BEST_PASS: '做对',
  SB_CELL_BEST_FAIL: '未做对',
  SB_CELL_PASS1: '第 1 轮全绿 {n} / {m}',
  SB_CELL_BEST_ROUND: '最好一次在第 {n} 轮',
  // 越界作废留痕：不计分、不进平均，但「试过、被作废」必须可见
  SB_CELL_OOB_ONLY: '仅有越界作废的尝试',
  SB_CELL_OOB_SUB: '越界作废 {n} 次（不计分）',
  SB_CELL_OOB_TITLE: '模型改动越出允许范围，整轮作废；只留痕，不参与分数统计',
  SB_LEGEND_BEST: '格子第一眼看「最好一次有没有全绿」；pass@1 是稳定性副标（中级以上题目有多次机会）',
};

/**
 * 身份色槽位数量：与 views.js 的 HUE_SLOTS 同一份色板——同一模型在记分板和
 * 档案页是同一个颜色，跨页扫读时靠颜色就能对上。
 * 颜色本身由 CSS 的 `.sb__profile[data-hue="n"]` 从 tokens 取，JS 只算色号。
 */
const HUE_SLOTS = 6;

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
  return hashId(id) % HUE_SLOTS;
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
    el('span', {}, T.SB_LEGEND_BEST),
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
      .filter((cell) => cell && (Number(cell.attempts) > 0 || Number(cell.oob) > 0));
    const attempts = cells.reduce((sum, cell) => sum + Number(cell.attempts || 0), 0);
    // 越界作废的留痕不是分数，单独累计：它要可见，但绝不进 pass@1 / 均值
    const oob = cells.reduce((sum, cell) => sum + Number(cell.oob || 0), 0);
    const pass1 = cells.reduce((sum, cell) => sum + Number(cell.pass1 || 0), 0);
    const solved = cells.reduce((sum, cell) => sum + (cell.best_passed ? 1 : 0), 0);
    const best = cells.reduce((max, cell) => Math.max(max, Number(cell.best_score || 0)), 0);
    return {
      attempts,
      oob,
      pass1,
      solved,
      tasks: cells.length,
      bestScore: best,
      passRate: attempts ? pass1 / attempts : 0,
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
          el('span', { class: 'sb__profile-meta' }, t(T.SB_PROFILE_TRIALS, { pass: stats.pass1, n: stats.attempts })),
        ),
      );
      if (String(modelId) === selectedModel) link.setAttribute('aria-current', 'page');
      item.appendChild(link);
      const delBtn = createButton({
        label: '×',
        variant: 'ghost',
        size: 'sm',
        ariaLabel: `${T.SB_PROFILE_DELETE}：${modelId}`,
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
    if (!stats.attempts) {
      if (!stats.oob) {
        return el(
          'div',
          { class: 'sb__profile-summary', role: 'status' },
          el('span', {}, S.SB_CELL_NO_DATA),
        );
      }
      // 只有越界作废的尝试：不显示成「暂无数据」，把事实摆出来
      return el(
        'div',
        { class: 'sb__profile-summary', role: 'status' },
        el('span', {}, t(T.SB_CELL_OOB_SUB, { n: stats.oob })),
      );
    }
    return el(
      'div',
      { class: 'sb__profile-summary', role: 'status' },
      el('span', {}, t(T.SB_PROFILE_TASKS_PASSED, { pass: stats.solved, n: stats.tasks })),
      el('span', {}, t(T.SB_PROFILE_TRIALS, { pass: stats.pass1, n: stats.attempts })),
      el('span', {}, t(S.SB_PROFILE_RATE, { rate: percent(stats.passRate) })),
      el('span', {}, t(T.SB_PROFILE_BEST, { best: stats.bestScore })),
      stats.oob > 0
        ? el('span', { class: 'u-faint' }, t(T.SB_CELL_OOB_SUB, { n: stats.oob }))
        : null,
    );
  }

  /**
   * 一个 (任务 × 模型) 单元格。数据源是成绩台账：只有点过「结束本轮」的尝试
   * 才会出现在这里，台账里每次结束各留一条，格子展示最高分那条。
   *
   * 第一眼结论 = 「最高分那条有没有全绿」（best_passed），不是 pass@1：
   * 中级以上题目有 2–3 次机会，T3-08 就是第 1 轮 85.7 未过、第 2 轮 100 全绿，
   * 只报 pass@1 会让格子上顶一个 ✕，而台账里那条成绩其实是 passed=true。
   * pass@1 仍然是副标——它是模型稳定性的口径，不是"这道题做没做对"的答案。
   * @param {object} row 记分板行
   * @param {string} modelId 档案编号
   * @returns {HTMLElement}
   */
  function renderCell(row, modelId) {
    const cell = (row.cells || {})[modelId];
    if ((!cell || !cell.attempts) && !(cell && Number(cell.oob) > 0)) {
      return el(
        'div',
        { class: 'sb__cell' },
        el('span', { class: 'u-faint' }, S.SB_CELL_NO_DATA),
      );
    }
    // 这个格子只有越界作废的留痕（没有任何计分成绩）：它没有分数可比，
    // 但「试过、全被作废」必须是可见的事实，不能落成一片「暂无数据」。
    if (!cell || !cell.attempts) {
      return el(
        'div',
        { class: 'sb__cell' },
        el('span', { class: 'sb__cell-main' }, T.SB_CELL_OOB_ONLY),
        el(
          'span',
          { class: 'sb__cell-sub u-faint' },
          t(T.SB_CELL_OOB_SUB, { n: Number(cell.oob) || 0 }),
        ),
      );
    }
    const offband = isOffBand(row, cell);
    const solved = Boolean(cell.best_passed);
    return el(
      'div',
      { class: 'sb__cell' },
      el(
        'span',
        { class: 'sb__cell-main' },
        el('span', { 'aria-hidden': 'true' }, solved ? '✓' : '✕'),
        ' ',
        solved ? T.SB_CELL_BEST_PASS : T.SB_CELL_BEST_FAIL,
      ),
      el('span', { class: 'sb__cell-sub' },
        t(T.SB_CELL_PASS1, { n: cell.pass1 || 0, m: cell.attempts || 0 })
        + ` · ${t(S.SB_CELL_TRIES_TOTAL, { n: cell.attempts || 0 })}`),
      el('span', { class: 'sb__cell-sub' }, t(T.SB_CELL_BEST, { best: cell.best_score, avg: cell.avg_score })),
      Number(cell.oob) > 0
        ? el('span', { class: 'sb__cell-sub u-faint', title: T.SB_CELL_OOB_TITLE },
            t(T.SB_CELL_OOB_SUB, { n: Number(cell.oob) || 0 }))
        : null,
      // 「最好一次在第 n 轮」用的是 best_round（代表条目在第几轮拿到这个分），
      // 不是 best_rounds（那次尝试一共校验了几轮）——T2-04 是两轮、最高分在第 1 轮，
      // 用错字段会写成「最好一次在第 2 轮」。
      (cell.best_round || 0) > 1 || (cell.best_rounds || 0) > 1
        ? el('span', { class: 'sb__cell-sub u-faint' },
            t(T.SB_CELL_BEST_ROUND, { n: cell.best_round || cell.best_rounds }))
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
   * 删除模型档案 = 彻底删除：档案、已存密钥、名下运行记录与台账成绩一起真删。
   * 记分板与排行榜上该模型随之消失，没有"先留着记录"的选项了。
   * @param {string} modelId 档案编号
   */
  async function deleteProfile(modelId) {
    const cells = (data.matrix || []).map((row) => (row.cells || {})[modelId]).filter(Boolean);
    const runs = cells.reduce((sum, cell) => sum + Number(cell.attempts || 0), 0);
    let body = T.SB_PROFILE_DELETE_BODY_NONE;
    if (runs > 0) {
      body = t(T.SB_PROFILE_DELETE_BODY, { n: runs, m: 0 });
    }
    const ok = await confirmDialog({
      title: t(T.SB_PROFILE_DELETE_TITLE, { id: modelId }),
      messages: [body, T.SB_PROFILE_DELETE_HINT],
      confirmLabel: S.ACTION_DELETE || '删除',
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL || '取消',
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api.del('/models', { scope, params: { id: modelId } });
      const removed = (res && res.removed_runs || []).length;
      const removedEntries = (res && res.removed_entries || []).length;
      const skipped = (res && res.skipped_busy || []).length;
      showToast({
        message: t(T.SB_PROFILE_DELETED, { id: modelId }),
        detail: removed || removedEntries
          ? `已删除 ${removed} 条运行记录与 ${removedEntries} 条成绩。`
          : '',
        kind: 'success',
        duration: 5000,
      });
      if (skipped) {
        showToast({ message: t(T.SB_PROFILE_BUSY_SKIP, { n: skipped }), kind: 'warn', duration: 6000 });
      }
      if (String(selectedModel) === String(modelId)) selectedModel = '';
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
    if (!band || !cell.attempts) return false;
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
          return cell && cell.attempts ? cell.pass_rate : -1;
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
