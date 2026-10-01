/**
 * settings.js — 设置视图
 *
 * 职责：
 *   1. 展示 `/api/health` 的环境自检（Python / pytest / Node / 受测仓库 / 盘符池 / 磁盘）。
 *   2. 界面偏好（校验时自动展开日志、破坏性操作确认、参与统计）——写 localStorage。
 *   3. 清除本机界面状态（带二次确认，列清会清什么、不会清什么）。
 *   4. 运行静态自检（§10.7）；后端未实现时给明确说明而不是空白。
 *
 * 状态：loading / ready / error，各区块独立三态。
 * 键盘：开关用 checkbox（原生、可 Tab）；按钮可 Tab；破坏性操作走 alertdialog。
 * ARIA：checkbox 有 label 关联；自检结果用 details 折叠（§13.7 技术细节默认折叠）。
 *
 * 依赖：core/*、components/*
 * 导出：createSettings(props) → { el, destroy, el_h1, getPrefs }
 */

import { el, clear } from '../core/dom.js';
import { S, t } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { storage, STORAGE_KEYS, DEFAULT_PREFS } from '../core/storage.js';
import { announce } from '../core/a11y.js';
import { createSkeleton } from '../components/skeleton.js';
import { createEmptyState } from '../components/empty-state.js';
import { createDetailsCard } from '../components/details-card.js';
import { createButton } from '../components/button.js';
import { createStatusDot } from '../components/status-dot.js';
import { confirmDialog } from '../components/confirm-dialog.js';
import { showToast } from '../components/toast.js';

/**
 * 创建设置视图。
 * @param {{onPrefsChange?: (prefs: object) => void}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement, getPrefs: Function}}
 */
export function createSettings(props = {}) {
  const { onPrefsChange } = props;
  const scope = api.scope();

  let prefs = { ...DEFAULT_PREFS, ...(storage.get('prefs', {}) || {}) };
  let health = null;
  let healthLoading = true;
  let healthError = null;
  let selfcheck = null;
  let selfcheckRunning = false;

  const h1 = el('h1', { tabindex: '-1' }, S.SETTINGS_TITLE);

  // ---- 自检区 ----
  const healthHost = el('div', { class: 'panel__body' });

  // ---- 偏好区 ----
  // 主题：跟随系统 / 浅色 / 深色。写入 localStorage('theme') 并即时切 data-theme。
  const savedTheme = (() => { try { return localStorage.getItem('theme') || ''; } catch { return ''; } })();
  // 主题用分段控件（三选一），不用下拉：选项少、互斥、且"当前是哪个"
  // 必须一眼看见——原生 select 在深色主题下是系统绘制的浅色面板，
  // 突兀且看不出当前值。
  const THEME_OPTIONS = [
    { value: '', label: S.SETTINGS_THEME_SYSTEM, hint: '跟随系统' },
    { value: 'light', label: S.SETTINGS_THEME_LIGHT, hint: '印纸' },
    { value: 'dark', label: S.SETTINGS_THEME_DARK, hint: '夜纸' },
  ];
  const themeChips = el('div', { class: 'settings__chips', role: 'radiogroup', 'aria-label': S.SETTINGS_PREF_THEME });
  const themeInputs = new Map();

  function applyTheme(value) {
    try {
      if (value) localStorage.setItem('theme', value);
      else localStorage.removeItem('theme');
    } catch { /* 存储不可用：仅本次生效 */ }
    // 「跟随系统」= 没有显式偏好：解析成具体主题，而不是删掉属性指望 CSS 媒体查询
    const resolved = value
      || ((window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light');
    document.documentElement.dataset.theme = resolved;
    themeInputs.forEach((input, v) => { input.checked = v === value; });
    announce(value ? `${S.SETTINGS_PREF_THEME}：${value}` : S.SETTINGS_THEME_SYSTEM);
  }

  THEME_OPTIONS.forEach((opt) => {
    const id = `theme-${opt.value || 'system'}`;
    const input = el('input', {
      type: 'radio',
      id,
      name: 'settings-theme',
      class: 'settings__chip-input',
      value: opt.value,
      checked: opt.value === savedTheme,
    });
    input.addEventListener('change', () => { if (input.checked) applyTheme(opt.value); });
    themeInputs.set(opt.value, input);
    themeChips.appendChild(
      el('label', { class: 'settings__chip', for: id },
        input,
        el('span', { class: 'settings__chip-face' }, opt.label),
      ),
    );
  });

  // 主题这一行的容器：标签 + 人话说明 + 分段控件
  const themeRow = el(
    'div',
    { class: 'settings__row' },
    el('div', { class: 'settings__row-main' },
      el('span', { class: 'settings__row-label' }, S.SETTINGS_PREF_THEME),
      el('span', { class: 'settings__row-hint' }, '改完立即生效，只影响这台机器的这个浏览器。'),
    ),
    themeChips,
  );

  const logToggle = makeToggle(S.SETTINGS_PREF_LOG, S.SETTINGS_PREF_LOG_HINT, 'autoExpandLog');
  const confirmToggle = makeToggle(S.SETTINGS_PREF_CONFIRM, S.SETTINGS_PREF_CONFIRM_HINT, 'confirmDestructive');
  const statsToggle = makeToggle(S.SETTINGS_PREF_STATS, S.SETTINGS_PREF_STATS_HINT, 'contributeStats');

  // 首启引导开关：strings.js 承诺过「可在设置页重新打开」，这里是兑现。
  // 「显示」= 清掉 dismissed 标记；「隐藏」= 写回 dismissed（任务库读取同一 key）。
  const guideToggle = makeToggle(
    '显示首启引导',
    '重新打开任务库顶部的三步指引（关闭后可在任务库「不再显示」里再关）',
    '__guide__',
  );
  // makeToggle 读写 prefs[key]，引导状态不在 prefs 里——手动接管这份输入。
  {
    const guideInput = guideToggle.el.querySelector('input');
    if (guideInput) {
      guideInput.checked = !storage.get(STORAGE_KEYS.GUIDE_DISMISSED, false);
      guideInput.addEventListener('change', () => {
        const dismissed = !guideInput.checked;
        storage.set(STORAGE_KEYS.GUIDE_DISMISSED, dismissed);
        if (dismissed) storage.set(STORAGE_KEYS.GUIDE_OPENED, false);
        announce(guideInput.checked ? '首启引导已打开。' : '首启引导已关闭。');
      });
    }
  }

  const prefsPanel = el(
    'section',
    { class: 'panel' },
    el('h2', { class: 'panel__title' }, S.SETTINGS_PREF_TITLE),
    el('p', { class: 'panel__desc' }, '这些偏好只保存在这台机器的浏览器里，换台电脑或清掉浏览器数据就回到默认。'),
    el('div', { class: 'panel__body settings__rows' },
      themeRow, guideToggle.el, logToggle.el, confirmToggle.el, statsToggle.el),
  );

  // ---- 本机数据区 ----
  const clearBtn = createButton({
    label: S.SETTINGS_CLEAR,
    variant: 'danger',
    onClick: () => clearLocal(),
  });
  const selfcheckBtn = createButton({
    label: S.SETTINGS_SELFTEST,
    onClick: () => runSelfcheck(),
  });
  const selfcheckHost = el('div', { class: 'u-stack' });

  // 本机数据：两个独立动作，各自说清「动了什么、不动什么」。
  // 旧版把它们塞进同一张卡、说明文字串在一起，读起来不知道哪个按钮对应哪段话。
  const dataPanel = el(
    'section',
    { class: 'panel settings__panel--data' },
    el('h2', { class: 'panel__title' }, S.SETTINGS_DATA_TITLE),
    el('p', { class: 'panel__desc' }, S.SETTINGS_DATA_DESC),
    el('div', { class: 'panel__body settings__actions' },
      el('div', { class: 'settings__action' },
        el('div', { class: 'settings__action-head' },
          el('h3', { class: 'settings__action-title' }, S.SETTINGS_CLEAR),
          el('p', { class: 'settings__action-hint' },
            '清掉上次停留的任务、轮次与各面板滚动位置，回到初始状态。'),
        ),
        clearBtn.el,
      ),
      el('div', { class: 'settings__action' },
        el('div', { class: 'settings__action-head' },
          el('h3', { class: 'settings__action-title' }, S.SETTINGS_SELFTEST),
          el('p', { class: 'settings__action-hint' }, S.SETTINGS_SELFTEST_HINT),
        ),
        selfcheckBtn.el,
      ),
      selfcheckHost,
    ),
  );

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.SETTINGS_DESC)),
    ),
    el('div', { class: 'settings__grid' },
      el('section', { class: 'panel' },
        el('h2', { class: 'panel__title' }, S.SETTINGS_HEALTH_TITLE),
        healthHost,
      ),
      prefsPanel,
    ),
    dataPanel,
  );

  /**
   * 造一个偏好开关（原生 checkbox + label 关联，§12.12）。
   * @param {string} label
   * @param {string} hint
   * @param {string} key
   * @returns {{el: HTMLElement, update: Function}}
   */
  /**
   * 造一个偏好开关。
   *
   * 布局：左边「标签 + 说明」两行，右边开关——说明是给"不知道这是什么意思"的人
   * 看的，必须贴着标签；写成一行小字挤在勾选框后面，读起来像附注而不是解释。
   * 开关本身用视觉化的 track + knob，不用原生 checkbox：原生方框在深色主题下
   * 是系统绘制的浅色块，和周围控件不是一套语言。
   *
   * @param {string} label 这一项叫什么（一行话）
   * @param {string} hint 它到底做什么（人话，不出现术语）
   * @param {string} key prefs 里的键
   * @returns {{el: HTMLElement, update: Function}}
   */
  function makeToggle(label, hint, key) {
    const id = `pref-${key}`;
    const input = el('input', { type: 'checkbox', id, class: 'settings__switch-input' });
    input.checked = Boolean(prefs[key]);
    input.addEventListener('change', () => {
      prefs = { ...prefs, [key]: input.checked };
      storage.set('prefs', prefs);
      if (onPrefsChange) onPrefsChange(prefs);
    });
    const wrap = el(
      'div',
      { class: 'settings__row' },
      el('label', { class: 'settings__row-main', for: id },
        el('span', { class: 'settings__row-label' }, label),
        hint ? el('span', { class: 'settings__row-hint' }, hint) : null,
      ),
      el('span', { class: 'settings__switch' }, input,
        el('span', { class: 'settings__switch-track', 'aria-hidden': 'true' })),
    );
    return {
      el: wrap,
      update: (value) => {
        input.checked = Boolean(value);
      },
    };
  }

  /**
   * 渲染环境自检。
   *
   * 契约（server.py `_build_health`）：
   * `{ok, checked_at, uptime_s, checks:[{id,label,ok,value}], warnings:[str], checkers:[str]}`。
   */
  function renderHealth() {
    clear(healthHost);
    if (healthLoading) {
      healthHost.appendChild(createSkeleton({ rows: 4, variant: 'row', label: S.SETTINGS_HEALTH_LOADING }).el);
      return;
    }
    if (healthError) {
      healthHost.appendChild(
        createEmptyState({
          title: errorTitle(healthError),
          desc: errorBody(healthError),
          alert: true,
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => loadHealth() }).el],
        }).el,
      );
      return;
    }
    if (!health) {
      healthHost.appendChild(createEmptyState({ title: S.STATE_UNKNOWN, desc: S.ERR_LOAD_BODY }).el);
      return;
    }

    const checks = Array.isArray(health.checks) ? health.checks : [];
    const failed = checks.filter((c) => !c.ok);
    const warnings = Array.isArray(health.warnings) ? health.warnings : [];

    healthHost.appendChild(
      createStatusDot({
        kind: health.ok ? 'ok' : 'error',
        text: health.ok
          ? S.SETTINGS_HEALTH_OK
          : t(S.SETTINGS_HEALTH_DEGRADED, { n: failed.length + warnings.length }),
      }).el,
    );

    // 检查项表：名称 / 结果 / 值，红绿用符号 + 文字双重编码（§12.8）
    healthHost.appendChild(
      el(
        'table',
        { class: 'table table--compact' },
        el('caption', { class: 'visually-hidden' }, S.SETTINGS_HEALTH_TITLE),
        el('thead', {},
          el('tr', {},
            el('th', { scope: 'col' }, S.SETTINGS_HEALTH_COL_ITEM),
            el('th', { scope: 'col' }, S.SETTINGS_HEALTH_COL_RESULT),
            el('th', { scope: 'col' }, S.SETTINGS_HEALTH_COL_VALUE),
          ),
        ),
        el(
          'tbody',
          {},
          ...checks.map((c) =>
            el(
              'tr',
              {},
              el('th', { scope: 'row' }, c.label || c.id),
              el('td', {},
                el('span', { class: `status-dot status-dot--${c.ok ? 'ok' : 'error'}` },
                  el('span', { class: 'status-dot__glyph', 'aria-hidden': 'true' }, c.ok ? '✓' : '✕'),
                  el('span', {}, c.ok ? S.SETTINGS_HEALTH_ITEM_OK : S.SETTINGS_HEALTH_ITEM_FAIL),
                ),
              ),
              el('td', { class: 'u-mono' }, c.value || '—'),
            ),
          ),
        ),
      ),
    );

    if (Array.isArray(health.checkers) && health.checkers.length) {
      healthHost.appendChild(
        el('p', { class: 'u-faint' },
          `${S.SETTINGS_HEALTH_CHECKERS}：${health.checkers.join('、')}`),
      );
    }

    if (warnings.length) {
      healthHost.appendChild(
        createDetailsCard({
          title: S.SETTINGS_HEALTH_WARNINGS,
          content: el('ul', { class: 'modal__list' }, ...warnings.map((w) => el('li', {}, w))),
          open: true,
        }).el,
      );
    }
  }

  /**
   * 拉环境自检。
   */
  async function loadHealth() {
    // 已经有数据时静默刷新：先清空再画骨架屏会让每次进设置页都闪一下白块，
    // 而 /health 在服务端有 10 秒缓存、通常毫秒级返回——骨架屏比数据活得还短，
    // 读起来就是「页面抽了一下」。只有首次加载（还没有数据）才显示骨架屏。
    const firstLoad = !health;
    healthLoading = firstLoad;
    healthError = null;
    if (firstLoad) renderHealth();
    try {
      health = await api.get('/health', { scope });
      healthLoading = false;
      renderHealth();
    } catch (err) {
      healthLoading = false;
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      healthError = err.code || 'LOAD_FAILED';
      renderHealth();
    }
  }

  /**
   * 运行静态自检（§10.7）。后端没实现时明确告知，不装作成功。
   */
  async function runSelfcheck() {
    selfcheckRunning = true;
    clear(selfcheckHost);
    selfcheckHost.appendChild(
      createStatusDot({ kind: 'busy', text: S.SETTINGS_SELFTEST_RUNNING }).el,
    );
    selfcheckBtn.update({ loading: true, busyLabel: S.SETTINGS_SELFTEST_RUNNING });
    try {
      const res = await api.post('/selfcheck', {}, { scope });
      selfcheck = res;
      renderSelfcheck(res);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'SELFTEST_FAILED';
      clear(selfcheckHost);
      selfcheckHost.appendChild(
        createEmptyState({
          title: S.ERR_SELFTEST_RUN,
          desc: `${S.ERR_SELFTEST_RUN_BODY}（${errorTitle(code)}）`,
          alert: true,
        }).el,
      );
    } finally {
      selfcheckRunning = false;
      selfcheckBtn.update({ loading: false });
    }
  }

  /**
   * 渲染自检结果。
   *
   * 契约（harness/selfcheck.py `scan`）：
   * `{generated_at, scanned_frontend_files, scanned_danger_files,
   *   issues:[{rule,title,level,message,file,line,excerpt}],
   *   summary:{error,warning,ok}}`
   *
   * @param {object} res
   */
  function renderSelfcheck(res) {
    clear(selfcheckHost);
    const issues = (res && res.issues) || [];
    const summary = (res && res.summary) || {};
    const errorCount = summary.error !== undefined ? summary.error : issues.filter((i) => i.level === 'error').length;
    const warnCount = summary.warning !== undefined ? summary.warning : issues.filter((i) => i.level === 'warning').length;

    selfcheckHost.appendChild(
      createStatusDot({
        kind: errorCount ? 'error' : warnCount ? 'warn' : 'ok',
        text: errorCount || warnCount
          ? t(S.SETTINGS_SELFTEST_ISSUES, { n: errorCount + warnCount })
          : S.SETTINGS_SELFTEST_OK,
      }).el,
    );

    selfcheckHost.appendChild(
      el(
        'p',
        { class: 'u-faint' },
        `${t(S.SETTINGS_SELFTEST_SCOPE, {
          js: res.scanned_frontend_files !== undefined ? res.scanned_frontend_files : '—',
          other: res.scanned_danger_files !== undefined ? res.scanned_danger_files : '—',
        })}（错误 ${errorCount}，提示 ${warnCount}）`,
      ),
    );

    if (issues.length) {
      const list = el(
        'ul',
        { class: 'modal__list' },
        ...issues.map((i) =>
          el(
            'li',
            { class: 'u-mono' },
            `${i.level === 'error' ? '✕' : '!'} ${i.file || ''}${i.line ? `:${i.line}` : ''} ${i.title || i.rule} —— ${i.message || ''}`,
          ),
        ),
      );
      selfcheckHost.appendChild(
        createDetailsCard({ title: S.ERROR_DETAIL_LABEL, content: list, open: true }).el,
      );
    }
  }

  /**
   * 清除本机界面状态。
   */
  async function clearLocal() {
    const ok = await confirmDialog({
      title: S.SETTINGS_CLEAR_CONFIRM_TITLE,
      messages: [S.SETTINGS_CLEAR_CONFIRM_BODY_1, S.SETTINGS_CLEAR_CONFIRM_BODY_2],
      confirmLabel: S.SETTINGS_CLEAR,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    const removed = storage.clearAll();
    prefs = { ...DEFAULT_PREFS };
    logToggle.update(prefs.autoExpandLog);
    confirmToggle.update(prefs.confirmDestructive);
    statsToggle.update(prefs.contributeStats);
    if (onPrefsChange) onPrefsChange(prefs);
    showToast({ message: `${S.SETTINGS_CLEAR_DONE}（${removed}）`, kind: 'success', duration: 5000 });
    announce(S.ANNOUNCE_CLEAR_DONE);
  }

  renderHealth();
  loadHealth();

  return {
    el: root,
    el_h1: h1,
    /** 当前偏好（工作台据此决定要不要二次确认）。 */
    getPrefs: () => ({ ...prefs }),
    /** 自检结果（调试用）。 */
    getSelfcheck: () => selfcheck,
    destroy() {
      scope.cancelAll();
      clearBtn.destroy();
      selfcheckBtn.destroy();
    },
  };
}
