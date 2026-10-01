/**
 * task-library.js — 任务库视图
 *
 * 职责：
 *   1. 列出任务包，按档位筛选、按关键词搜索（§13.1 的"下一步"入口）。
 *   2. 首启三步引导，可关闭且永久记住（§13.1）。
 *   3. loading / empty / error 三态，error 态给「重试」。
 *   4. 任务卡用 key 化复用，筛选不重建整页。
 *
 * 状态：loading / ready / empty / error。
 * 键盘：卡片内「进入工作台」按钮可 Tab；引导的「不再显示」可 Tab。
 * ARIA：每张卡是一个 listitem；筛选器用 label 关联；结果数用 live region 播报。
 *
 * 依赖：core/*、components/*
 * 导出：createTaskLibrary(props) → { el, destroy, el_h1 }
 */

import { el, setText, patchList, clear } from '../core/dom.js';
import { S, t, normalizeTier, TIER_NAMES } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { storage, STORAGE_KEYS } from '../core/storage.js';
import { announce } from '../core/a11y.js';
import { createButton } from '../components/button.js';
import { createBadge } from '../components/badge.js';
import { createSkeleton } from '../components/skeleton.js';
import { createEmptyState } from '../components/empty-state.js';
import { createField } from '../components/field.js';

/** 本视图新增文案（strings.js 只读，此处放本轮改造的新句子）。 */
const T = {
  /** 档位徽标：`初级 · 试 1 次`（spec 三、任务库 §2）。 */
  TIER_BADGE: '{tier} · 试 {n} 次',
};

/** 档位筛选项（值用归一后的档位名，见 core/strings.js 的 normalizeTier）。tier 用于 chip 上的色点。 */
const TIER_FILTERS = [
  { value: '', label: S.LIB_FILTER_ALL },
  { value: 'primary', label: S.LIB_TIER_T1, tier: 'primary' },
  { value: 'medium', label: S.LIB_TIER_T2, tier: 'medium' },
  { value: 'hard', label: S.LIB_TIER_T3, tier: 'hard' },
  { value: 'king', label: S.LIB_TIER_T4, tier: 'king' },
];

/** 档位 → 徽标符号（与颜色构成三重编码，同 components/badge.js 的约定）。 */
const TIER_GLYPHS = { primary: '●', medium: '◆', hard: '▲', king: '★' };

/** 档位 → 徽章变体类（badge--tier-tN，颜色在 task-library.css 里按官方色板覆盖）。 */
const TIER_VARIANTS = { primary: 'tier-t1', medium: 'tier-t2', hard: 'tier-t3', king: 'tier-t4' };

/** 档位 → 卡片顶部发丝线的修饰类（task-library.css）。 */
const TIER_CARD_CLASS = {
  primary: 'task-card--t1',
  medium: 'task-card--t2',
  hard: 'task-card--t3',
  king: 'task-card--t4',
};

/**
 * 创建任务库。
 *
 * @param {{navigate: (name: string, params?: object) => void}} props
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
export function createTaskLibrary(props = {}) {
  const { navigate } = props;
  const scope = api.scope();

  let tasks = [];
  let loading = true;
  let error = null;
  let filterTier = '';
  let keyword = '';

  const h1 = el('h1', { tabindex: '-1' }, S.LIB_TITLE);
  const listEl = el('ul', { class: 'lib__grid' });
  const bodyHost = el('div', { class: 'u-stack' });
  const countLine = el('p', { class: 'u-faint', role: 'status', 'aria-live': 'polite' });

  // ---- 首启三步引导 ----
  const guideDismissed = readGuideDismissed();
  const guideDismissBtn = createButton({
    label: S.GUIDE_DISMISS,
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      guide.hidden = true;
      storage.set(STORAGE_KEYS.GUIDE_DISMISSED, true);
      announce('已关闭首次引导。');
    },
  });
  const guide = el(
    'section',
    { class: 'guide', 'aria-labelledby': 'guide-title' },
    el(
      'div',
      { class: 'guide__head' },
      el('div', {},
        el('h2', { id: 'guide-title' }, S.GUIDE_TITLE),
        el('p', { class: 'guide__desc' }, S.GUIDE_DESC),
      ),
      el('span', { class: 'u-spacer' }),
      guideDismissBtn.el,
    ),
  );
  [
    { n: 1, title: S.GUIDE_STEP_1_TITLE, desc: S.GUIDE_STEP_1_DESC, action: S.GUIDE_STEP_1_ACTION, go: () => scrollToList() },
    { n: 2, title: S.GUIDE_STEP_2_TITLE, desc: S.GUIDE_STEP_2_DESC, action: S.GUIDE_STEP_2_ACTION, go: () => enterTask(tasks[0] && tasks[0].id) },
    { n: 3, title: S.GUIDE_STEP_3_TITLE, desc: S.GUIDE_STEP_3_DESC, action: S.GUIDE_STEP_3_ACTION, go: () => enterTask(tasks[0] && tasks[0].id, 'prompt') },
  ].forEach((step) => {
    guide.appendChild(
      el(
        'div',
        { class: 'guide__step' },
        el('div', { class: 'u-row-tight' },
          el('span', { class: 'guide__num', 'aria-hidden': 'true' }, String(step.n)),
          el('span', { class: 'guide__step-title' }, step.title),
        ),
        el('p', { class: 'guide__step-desc' }, step.desc),
        createButton({ label: step.action, size: 'sm', onClick: step.go }).el,
      ),
    );
  });
  guide.hidden = guideDismissed;

  // ---- 工具条 ----
  /**
   * 档位筛选：五个单选 chip 的分段控件（spec 三、任务库 §1）。
   * 用原生 radio（同 name 一组）拿免费的箭头键导航，视觉全部自定义。
   */
  function createTierChips() {
    const group = el('div', { class: 'lib__chips', role: 'radiogroup', 'aria-label': S.LIB_FILTER_TIER });
    const inputs = TIER_FILTERS.map((f) => {
      const input = el('input', {
        class: 'lib__chip-input',
        type: 'radio',
        name: 'lib-tier-filter',
        value: f.value,
        checked: f.value === '',
        onChange: () => {
          filterTier = f.value;
          renderList();
        },
      });
      group.appendChild(
        el(
          'label',
          { class: 'lib__chip', 'data-tier': f.tier },
          input,
          el('span', { class: 'lib__chip-face' }, f.label),
        ),
      );
      return input;
    });
    return {
      el: group,
      /** 程序化选中某个档位（空状态「显示全部」用）。 */
      setValue(value) {
        const input = inputs.find((i) => i.value === value);
        if (input) input.checked = true;
      },
    };
  }
  const tierChips = createTierChips();

  const searchField = createField({
    label: S.LIB_SEARCH_LABEL,
    name: 'task-search',
    type: 'search',
    placeholder: S.LIB_SEARCH_PLACEHOLDER,
    onInput: (value) => {
      keyword = value;
      renderList();
    },
  });
  searchField.el.style.flex = '1 1 260px';

  const refreshBtn = createButton({
    label: S.ACTION_REFRESH,
    variant: 'ghost',
    onClick: () => load(),
  });

  const toolbar = el(
    'div',
    { class: 'lib__toolbar' },
    tierChips.el,
    searchField.el,
    el('span', { class: 'u-spacer' }),
    refreshBtn.el,
  );

  const root = el(
    'div',
    { class: 'view lib' },
    el(
      'div',
      { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.LIB_DESC)),
    ),
    guide,
    toolbar,
    countLine,
    listEl,
    bodyHost,
  );

  /**
   * 进入工作台。
   * @param {string|undefined} id
   * @param {string} [region]
   */
  function enterTask(id, region) {
    if (!id) return;
    storage.set('last-task', id);
    navigate('workspace', region ? { taskId: id, region } : { taskId: id });
  }

  /** 滚到任务列表。 */
  function scrollToList() {
    listEl.scrollIntoView({ behavior: 'auto', block: 'start' });
    const first = listEl.querySelector('button');
    if (first) first.focus();
  }

  /**
   * 档位徽标：`初级 · 试 1 次`（徽标文案见 spec 三、任务库 §2）。
   * @param {string} tier 归一后的档位名
   * @param {string} tierName 档位中文名
   * @param {number} [attempts]
   */
  function tierBadgeEl(tier, tierName, attempts) {
    return createBadge({
      label: attempts ? t(T.TIER_BADGE, { tier: tierName, n: attempts }) : tierName,
      variant: TIER_VARIANTS[tier] || 'muted',
      glyph: TIER_GLYPHS[tier] || '·',
    }).el;
  }

  /**
   * 单张任务卡。
   *
   * 层次（spec 三、任务库 §3）：标题是主角，「考察：…」紧跟标题；
   * 目标通过率/校准/历史最好这类元数据收成脚注小字。
   * 整卡可点进工作台（鼠标）；键盘走卡内「进入工作台」按钮。
   *
   * @param {object} task
   * @returns {HTMLElement}
   */
  function renderCard(task) {
    const node = el('li', {
      class: 'task-card',
      onClick: (ev) => {
        if (ev.target.closest('button, a, input, select, label')) return;
        const sel = typeof getSelection === 'function' ? String(getSelection()) : '';
        if (sel) return; // 正在选中文字时不触发跳转
        enterTask(task.id);
      },
    });

    function build(card) {
      const history = card.history || {};
      const hasHistory = Number(history.runs || 0) > 0;
      const best = Number(history.best_score || 0);
      const band = normalizeBand(card.target_band);
      const tier = normalizeTier(card.tier);
      const tierName = (TIER_NAMES[tier] || { label: tier }).label;
      const cardClass = ['task-card', TIER_CARD_CLASS[tier]].filter(Boolean).join(' ');
      if (node.className !== cardClass) node.className = cardClass;
      const metaParts = [
        band ? t(S.LIB_CARD_TARGET_BAND, band) : '',
        card.calibrated ? S.LIB_CARD_CALIBRATED : S.LIB_CARD_NOT_CALIBRATED,
        hasHistory ? t(S.LIB_CARD_HISTORY, { score: best }) : S.LIB_CARD_HISTORY_NONE,
      ].filter(Boolean);
      // replaceChildren() 按 DOM 规范会把 null 转成字符串 "null" 印到界面上，
      // 所以可选块必须先过滤，不能像 el() 那样直接传 null。
      node.replaceChildren(
        ...[
          el(
            'div',
            { class: 'task-card__top' },
            el('span', { class: 'task-card__id' }, card.id),
            el('span', { class: 'u-spacer' }),
            tierBadgeEl(tier, tierName, card.attempts),
          ),
          el('p', { class: 'task-card__title' }, card.title),
          card.summary
            ? el('p', { class: 'task-card__goal' }, t(S.LIB_CARD_GOAL, { goal: card.summary }))
            : null,
          card.symptom
            ? el('p', { class: 'task-card__symptom' }, card.symptom)
            : null,
          metaParts.length
            ? el('p', { class: 'task-card__meta' }, metaParts.join(' · '))
            : null,
          el(
            'div',
            { class: 'task-card__foot' },
            el('span', { class: 'u-spacer' }),
            createButton({
              label: S.LIB_CARD_LEADERBOARD,
              variant: 'ghost',
              size: 'sm',
              onClick: () => navigate && navigate('leaderboard', { taskId: card.id }),
            }).el,
            createButton({
              label: S.LIB_CARD_ENTER,
              variant: 'ghost',
              size: 'sm',
              onClick: () => enterTask(card.id),
            }).el,
          ),
        ].filter(Boolean),
      );
    }

    build(task);
    // 卡片内容含历史成绩/校准徽章：刷新后必须跟着数据更新，不能停在旧渲染
    return { el: node, update: (next) => build(next) };
  }

  /**
   * 卡片必须能被原地刷新。
   * patchList 按题目 id 复用节点，跑完一轮回到任务库时成绩、校准标记、档位都变了；
   * 只在建卡那一刻画一次，页面就会永远停在旧数字上。
   */
  const cardSignatures = new WeakMap();

  function createCard(task) {
    const node = renderCard(task);
    cardSignatures.set(node, JSON.stringify(task));
    return node;
  }

  function refreshCard(node, task) {
    const signature = JSON.stringify(task);
    if (cardSignatures.get(node) === signature) return;
    cardSignatures.set(node, signature);
    const rebuilt = renderCard(task);
    node.className = rebuilt.className;
    clear(node);
    while (rebuilt.firstChild) node.appendChild(rebuilt.firstChild);
  }

  /**
   * 目标通过率区间：后端可能给 0.3~0.6（比例）或 30~60（百分数），两种都归一成百分数文案。
   * @param {unknown} raw
   * @returns {{low: string, high: string}|null}
   */
  function normalizeBand(raw) {
    if (!Array.isArray(raw) || raw.length < 2) return null;
    const scale = Number(raw[1]) <= 1 ? 100 : 1;
    return { low: `${Math.round(Number(raw[0]) * scale)}%`, high: `${Math.round(Number(raw[1]) * scale)}%` };
  }

  /**
   * 过滤后的任务列表。
   * @returns {Array}
   */
  function filtered() {
    const kw = keyword.trim().toLowerCase();
    return tasks.filter((task) => {
      if (filterTier && normalizeTier(task.tier) !== filterTier) return false;
      if (!kw) return true;
      return (
        String(task.id).toLowerCase().includes(kw) ||
        String(task.title).toLowerCase().includes(kw) ||
        String(task.symptom || '').toLowerCase().includes(kw)
      );
    });
  }

  /**
   * 渲染列表 + 计数 + 空态。
   */
  function renderList() {
    const list = filtered();
    setText(countLine, t(S.LIB_COUNT, { n: list.length }));
    if (list.length === 0) {
      patchList(listEl, [], (x) => x, () => el('li'), () => {});
      listEl.hidden = true;
      if (!bodyHost.querySelector('.empty-state')) {
        bodyHost.textContent = '';
        bodyHost.appendChild(
          createEmptyState({
            title: S.LIB_SEARCH_EMPTY,
            desc: S.LIB_SEARCH_EMPTY_DESC,
            actions: [
              createButton({
                label: S.LIB_FILTER_ALL,
                onClick: () => {
                  filterTier = '';
                  keyword = '';
                  tierChips.setValue('');
                  searchField.setValue('');
                  renderList();
                },
              }).el,
            ],
          }).el,
        );
      }
      return;
    }
    listEl.hidden = false;
    bodyHost.textContent = '';
    patchList(listEl, list, (task) => task.id, createCard, refreshCard);
  }

  /**
   * 全量渲染（三态）。
   */
  function render() {
    bodyHost.textContent = '';
    if (loading) {
      countLine.textContent = '';
      bodyHost.appendChild(
        createSkeleton({ rows: 4, variant: 'card', label: `${S.STATE_LOADING}：${S.LIB_LOADING_DESC}` }).el,
      );
      return;
    }
    if (error) {
      countLine.textContent = '';
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
    renderList();
  }

  /**
   * 拉取任务列表。
   */
  async function load() {
    loading = true;
    error = null;
    render();
    try {
      const data = await api.get('/tasks', { scope });
      tasks = (data && data.tasks) || [];
      loading = false;
      render();
      announce(t(S.ANNOUNCE_TASKS_LOADED, { n: tasks.length }));
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
    /** 重新拉取。 */
    refresh: load,
    /** 解绑 + 取消在途请求（§10.4）。 */
    destroy() {
      scope.cancelAll();
      searchField.destroy();
      refreshBtn.destroy();
      guideDismissBtn.destroy();
    },
  };
}

/** 接受当前布尔值及旧版序列化字符串，避免已有偏好失效。 */
function readGuideDismissed() {
  const value = storage.get(STORAGE_KEYS.GUIDE_DISMISSED, false);
  return value === true || value === 1 || value === 'true';
}
