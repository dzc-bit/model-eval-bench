/**
 * leaderboard.js — 按题目查看排行榜
 *
 * 排行榜和记分板承担两种不同的阅读任务：这里回答「这道题谁完成得最好」；
 * 记分板回答「某个模型档案在所有题上的稳定性」。进入带 taskId 的路由后，
 * 页面只请求并展示这一道题的排名，便于分享、复查和从任务库直接跳转。
 */

import { el, clear } from '../core/dom.js';
import { S, t, normalizeTier, TIER_NAMES } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { announce } from '../core/a11y.js';
import { createButton } from '../components/button.js';
import { createEmptyState } from '../components/empty-state.js';
import { createSkeleton } from '../components/skeleton.js';
import { tierBadge } from '../components/badge.js';

/**
 * 创建排行榜视图。
 * @param {{navigate?: Function, taskId?: string}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
export function createLeaderboard(props = {}) {
  const { navigate } = props;
  const scope = api.scope();
  let selectedTaskId = props.taskId ? String(props.taskId) : '';
  let tasks = [];
  let board = null;
  let loading = true;
  let error = null;
  const pickerButtons = [];

  const h1 = el('h1', { tabindex: '-1' }, S.LEADERBOARD_TITLE);
  const bodyHost = el('div', { class: 'leaderboard__body' });
  const pickerHost = el('section', {
    class: 'leaderboard__picker',
    'aria-labelledby': 'leaderboard-picker-title',
  });
  const toolbar = el('div', { class: 'leaderboard__toolbar' });
  const refreshBtn = createButton({
    label: S.ACTION_REFRESH,
    variant: 'ghost',
    onClick: () => load(),
  });
  toolbar.appendChild(refreshBtn.el);

  const root = el(
    'div',
    { class: 'view leaderboard' },
    el(
      'div',
      { class: 'view__head leaderboard__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.LEADERBOARD_DESC)),
      toolbar,
    ),
    pickerHost,
    bodyHost,
  );

  function clearPickerButtons() {
    pickerButtons.splice(0).forEach((button) => button.destroy());
  }

  function taskMeta(task) {
    if (!task) return null;
    const tier = TIER_NAMES[normalizeTier(task.tier)] || TIER_NAMES.primary;
    return {
      tier: tier ? tier.label : String(task.tier || ''),
      attempts: Number(task.attempts || 0),
    };
  }

  function renderTaskPicker() {
    clear(pickerHost);
    clearPickerButtons();
    pickerHost.hidden = Boolean(selectedTaskId);
    if (selectedTaskId) return;

    pickerHost.appendChild(el('h2', { id: 'leaderboard-picker-title' }, S.LEADERBOARD_PICK_TITLE));
    pickerHost.appendChild(el('p', { class: 'leaderboard__section-desc' }, S.LEADERBOARD_PICK_DESC));

    if (!tasks.length) {
      pickerHost.appendChild(
        createEmptyState({
          title: S.LEADERBOARD_NO_TASKS,
          desc: S.LEADERBOARD_NO_TASKS_DESC,
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

    // 题目选择做成紧凑的选择行，而不是 12 张大卡：排行榜的主角是**排名**，
    // 题目只是前置条件。之前把任务卡铺满首屏，主次颠倒了——用户反馈
    // 「排行榜跟首页一样」，就是这个原因。
    const list = el('ul', { class: 'leaderboard__task-list' });
    tasks.forEach((task) => {
      const meta = taskMeta(task);
      const row = el(
        'button',
        {
          type: 'button',
          class: 'leaderboard__task-chip',
          'aria-label': `${task.id}　${task.title || task.id}`,
        },
        el('span', { class: 'leaderboard__task-chip-id' }, task.id),
        tierBadge(task.tier, {}).el,
        el('span', { class: 'leaderboard__task-chip-title' }, task.title || task.id),
        meta ? el('span', { class: 'leaderboard__task-chip-meta u-faint' }, t(S.LEADERBOARD_TASK_META, meta)) : null,
        el('span', { class: 'leaderboard__task-chip-go', 'aria-hidden': 'true' }, '→'),
      );
      row.addEventListener('click', () => navigate && navigate('leaderboard', { taskId: task.id }));
      pickerButtons.push({ el: row, destroy: () => {} });
      list.appendChild(el('li', {}, row));
    });
    pickerHost.appendChild(list);
  }

  function renderDuration(value) {
    if (value === null || value === undefined || value === '') return S.LEADERBOARD_NO_DURATION;
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return S.LEADERBOARD_NO_DURATION;
    if (seconds >= 60) {
      const minutes = Math.floor(seconds / 60);
      return t(S.LEADERBOARD_MINUTES, { m: minutes, s: (seconds - minutes * 60).toFixed(1) });
    }
    return t(S.LEADERBOARD_SECONDS, { n: seconds.toFixed(seconds < 10 ? 2 : 1) });
  }

  function renderPodium(entries) {
    const podiumEntries = entries.slice(0, 3);
    if (!podiumEntries.length) return null;
    const podium = el(
      'section',
      { class: 'leaderboard__podium', 'aria-labelledby': 'leaderboard-podium-title' },
      el('h2', { id: 'leaderboard-podium-title' }, S.LEADERBOARD_PODIUM_TITLE),
    );
    const list = el('ol', { class: 'leaderboard__podium-list' });
    podiumEntries.forEach((entry) => {
      list.appendChild(
        el(
          'li',
          { class: `leaderboard__podium-item leaderboard__podium-item--${entry.rank}` },
          el('span', { class: 'leaderboard__podium-rank', 'aria-label': t(S.LEADERBOARD_RANK, { n: entry.rank }) }, String(entry.rank)),
          el('strong', { class: 'leaderboard__podium-model' }, entry.model || '—'),
          el('span', { class: 'leaderboard__podium-score' }, t(S.LEADERBOARD_SCORE_VALUE, { n: entry.score })),
          el('span', { class: 'u-faint' }, `${t(S.LEADERBOARD_ROUNDS_VALUE, { n: entry.rounds })} · ${renderDuration(entry.duration_s)}`
            + (Number(entry.oob) > 0 ? ` · 越界作废 ${Number(entry.oob)} 次（不计分）` : '')),
        ),
      );
    });
    podium.appendChild(list);
    return podium;
  }

  function renderRankingList(entries) {
    const section = el(
      'section',
      { class: 'leaderboard__ranking', 'aria-labelledby': 'leaderboard-table-title' },
      el('h2', { id: 'leaderboard-table-title' }, S.LEADERBOARD_TABLE_TITLE),
    );
    const table = el(
      'table',
      { class: 'table leaderboard__table' },
      el('caption', { class: 'visually-hidden' }, S.LEADERBOARD_TABLE_TITLE),
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', { scope: 'col' }, '#'),
          el('th', { scope: 'col' }, S.LEADERBOARD_MODEL),
          el('th', { scope: 'col' }, S.LEADERBOARD_ROUNDS),
          el('th', { scope: 'col' }, S.LEADERBOARD_DURATION),
          el('th', { scope: 'col', class: 'table__num' }, S.LEADERBOARD_SCORE),
        ),
      ),
      el(
        'tbody',
        {},
        ...entries.map((entry) =>
          el(
            'tr',
            {},
            el('th', { scope: 'row', class: 'leaderboard__rank' }, String(entry.rank)),
            el('td', {}, entry.model || '—'),
            el('td', {}, t(S.LEADERBOARD_ROUNDS_VALUE, { n: entry.rounds })
              + (Number(entry.oob) > 0
                ? ` · 越界作废 ${Number(entry.oob)} 次（不计分）`
                : '')),
            el('td', { title: entry.wall_seconds
              ? `墙钟用时 ${renderDuration(entry.wall_seconds)}（含挂机与思考），排名按模型工作时间`
              : S.LEADERBOARD_DURATION_HINT }, renderDuration(entry.duration_s)),
            el('td', { class: 'table__num leaderboard__score' }, t(S.LEADERBOARD_SCORE_VALUE, { n: entry.score })),
          ),
        ),
      ),
    );
    const tableWrap = el(
      'div',
      {
        class: 'table-wrap leaderboard__table-wrap',
        tabindex: '0',
        role: 'region',
        'aria-label': S.LEADERBOARD_TABLE_TITLE,
      },
      table,
    );
    section.appendChild(tableWrap);
    return section;
  }

  function renderDetail() {
    clear(bodyHost);
    if (!board) return;
    const task = tasks.find((item) => String(item.id) === selectedTaskId);
    const meta = taskMeta(task);
    const backBtn = createButton({
      label: S.LEADERBOARD_BACK,
      variant: 'ghost',
      size: 'sm',
      onClick: () => navigate && navigate('leaderboard'),
    });
    bodyHost.appendChild(
      el(
        'div',
        { class: 'leaderboard__detail-head' },
        el(
          'div',
          {},
          el('span', { class: 'leaderboard__task-id' }, board.task || selectedTaskId),
          el('h2', {}, board.title || (task && task.title) || selectedTaskId),
          meta ? el('p', { class: 'u-faint' }, t(S.LEADERBOARD_TASK_META, meta)) : null,
        ),
        backBtn.el,
      ),
    );
    // 校准纪律（§9）：出题者不填校准数据。盲测回填前必须如实告知读者
    // 目标难度带只是出题侧预估，不能当成已验证的难度结论。
    if (task && task.calibrated === false) {
      const band = Array.isArray(task.target_band) && task.target_band.length === 2
        ? { low: `${Math.round(Number(task.target_band[0]) * 100)}%`, high: `${Math.round(Number(task.target_band[1]) * 100)}%` }
        : { low: '—', high: '—' };
      bodyHost.appendChild(
        el('p', { class: 'leaderboard__calibration-note', role: 'note' },
          el('strong', {}, '⚠ 难度未校准　'),
          t(S.LEADERBOARD_UNCALIBRATED, band),
        ),
      );
    }
    const entries = Array.isArray(board.entries) ? board.entries : [];
    if (!entries.length) {
      backBtn.destroy();
      backBtn.el.remove();   // destroy 只解绑事件；节点不摘会留下一个点了没反应的死按钮
      bodyHost.appendChild(
        createEmptyState({ title: S.LEADERBOARD_EMPTY, desc: S.LEADERBOARD_EMPTY_DESC }).el,
      );
      return;
    }
    const podium = renderPodium(entries);
    if (podium) bodyHost.appendChild(podium);
    bodyHost.appendChild(renderRankingList(entries));
  }

  function render() {
    renderTaskPicker();
    clear(bodyHost);
    if (loading) {
      bodyHost.appendChild(
        createSkeleton({ rows: 5, variant: selectedTaskId ? 'row' : 'card', label: `${S.STATE_LOADING}：${S.LEADERBOARD_LOADING_DESC}` }).el,
      );
      return;
    }
    if (error) {
      bodyHost.appendChild(
        createEmptyState({
          title: selectedTaskId ? S.LEADERBOARD_LOAD_ERROR : errorTitle(error),
          desc: selectedTaskId ? S.LEADERBOARD_LOAD_ERROR_DESC : errorBody(error),
          alert: true,
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => load() }).el],
        }).el,
      );
      return;
    }
    if (selectedTaskId) renderDetail();
  }

  async function load() {
    loading = true;
    error = null;
    board = null;
    render();
    try {
      const tasksResponse = await api.get('/tasks', { scope });
      tasks = Array.isArray(tasksResponse && tasksResponse.tasks) ? tasksResponse.tasks : [];
      if (selectedTaskId) {
        board = await api.get(`/tasks/${encodeURIComponent(selectedTaskId)}/leaderboard`, { scope });
      }
      loading = false;
      render();
      announce(S.ANNOUNCE_LEADERBOARD_LOADED);
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
      clearPickerButtons();
      refreshBtn.destroy();
    },
  };
}
