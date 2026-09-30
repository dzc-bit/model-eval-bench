/**
 * main.js — 装配层（§2 文件清单、§10.6 错误处理）
 *
 * 职责：
 *   1. 建外壳：页头导航、模拟数据横幅、连接状态条、全局错误条。
 *   2. 建全局 store（模型档案列表 + 界面偏好），供各视图订阅。
 *   3. 接 router：路由变化 → 销毁旧视图 → 挂载新视图 → 焦点移到该视图 h1（§10.3）。
 *   4. 按需加载：记分板与帮助页用动态 import() 拆分，首屏不必下载（§10.5）。
 *   5. 全局错误兜底：`window.onerror` + `unhandledrejection` → 顶部错误条 + toast + 控制台（§10.6）。
 *   6. 全局单键快捷键：`?` 打开快捷键表；Esc 兜底关闭最上层浮层。
 *   7. 在线/离线监听与本机存储降级监听（§13.6、§11.2 #9）。
 *
 * 依赖：core/*、components/*、views/task-library.js、views/workspace.js
 * 导出：无（页面入口脚本）
 *
 * 分层纪律：main 只做装配，不写业务逻辑；业务判定一律留在 views 里。
 * 视图生命周期统一遵守 `createX(props) → {el, update(state), destroy()}`（§10.4）。
 */

import { el, clear, on } from './core/dom.js';
import { S, t } from './core/strings.js';
import { createStore } from './core/store.js';
import { createRouter, buildHash } from './core/router.js';
import { api, ApiError, errorTitle, errorBody } from './core/api.js';
import { storage, STORAGE_KEYS, DEFAULT_PREFS } from './core/storage.js';
import { announce, focusHeading, isEditableTarget, restoreFocus } from './core/a11y.js';
import { createToastHost, showToast } from './components/toast.js';
import { openModal, closeTopModal, hasOpenModal } from './components/modal.js';
import { createStatusDot } from './components/status-dot.js';
import { createButton } from './components/button.js';
import { createTaskLibrary } from './views/task-library.js';
import { createWorkspace } from './views/workspace.js';

/** 导航项：name → 文案。顺序即页签顺序。 */
const NAV_ITEMS = [
  { name: 'tasks', label: S.NAV_TASKS },
  { name: 'workspace', label: S.NAV_WORKSPACE },
  { name: 'batch', label: S.NAV_BATCH },
  { name: 'scoreboard', label: S.NAV_SCOREBOARD },
  { name: 'models', label: S.NAV_MODELS },
  { name: 'settings', label: S.NAV_SETTINGS },
  { name: 'help', label: S.NAV_HELP },
];

// ==================================================================
// 外壳节点
// ==================================================================

const statusHost = document.getElementById('status-host');
const navEl = document.getElementById('app-nav');
const rootEl = document.getElementById('app-root');

/** 路由器句柄（只给 navigate 用，不进 store，避免状态树里混进带方法的对象）。 */
let router = null;

/** 顶部错误条节点，null 表示当前没显示。 */
let errorBar = null;
/** 连接状态条节点。 */
let connBar = null;

/** 当前挂载的视图句柄。 */
let currentView = null;
/** 当前路由标识，用来识别「同一路由的重复通知」。 */
let currentRouteKey = '';

/** 按需加载的视图模块缓存，避免重复 import。 */
let scoreboardMod = null;
let helpMod = null;

/** 模型档案是否已经拿到过（决定工作台能否直接挂载）。 */
let modelsReady = false;

/**
 * 应用级状态：只放「跨视图都要用」的东西，业务状态仍归各视图自己的 store。
 */
const app = createStore({
  models: [],
  prefs: readPrefs(),
  lastTask: storage.get(STORAGE_KEYS.LAST_TASK, '') || '',
  route: { name: '', params: {} },
});

/**
 * 读本机界面偏好，容错到默认值。
 * @returns {object}
 */
function readPrefs() {
  const raw = storage.get(STORAGE_KEYS.PREFERENCES, null);
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_PREFS };
  return { ...DEFAULT_PREFS, ...raw };
}

// ==================================================================
// 启动
// ==================================================================

/**
 * 组装整页。所有定义都在下面，这里是唯一入口调用点。
 * @returns {void}
 */
function boot() {
  if (!statusHost || !navEl || !rootEl) {
    if (typeof console !== 'undefined' && typeof console.error === 'function') {
      console.error('[main] 页面骨架缺少 #app-root / #app-nav / #status-host，无法挂载视图。');
    }
    return;
  }

  createToastHost();
  mountStatusBars();
  mountNav();

  router = createRouter({ onChange: onRouteChange });

  on(window, 'online', handleOnline);
  on(window, 'offline', handleOffline);
  on(window, 'error', handleWindowError);
  on(window, 'unhandledrejection', handleRejection);
  on(window, 'evalconsole:storage-degraded', handleStorageDegraded);
  on(document, 'keydown', onGlobalKeydown);

  // 模型档案是工作台的下拉数据源，先拉一次；工作台路由会等它到位再挂载
  loadModels().finally(() => {
    router.start();
  });
}

// ==================================================================
// 状态条（§13.6 / §10.6）
// ==================================================================

/**
 * 挂顶部的常驻条：模拟数据横幅（仅 ?mock=1）。
 * @returns {void}
 */
function mountStatusBars() {
  if (!api.isMock()) return;
  statusHost.appendChild(el('div', { class: 'conn-bar', role: 'note' }, el('span', {}, S.MOCK_BANNER)));
}

/**
 * 显示顶部错误条（同一时刻只显示一条，新的顶掉旧的）。
 *
 * @param {string} title 发生了什么
 * @param {string} [body] 影响 + 下一步
 * @param {string} [code] 错误码，只为排查方便显示在末尾
 * @returns {void}
 */
function showErrorBar(title, body, code) {
  hideErrorBar();
  const text = el('span', { class: 'error-bar__text' });
  text.appendChild(el('strong', {}, title));
  if (body) text.appendChild(el('span', {}, ` ${body}`));
  if (code) text.appendChild(el('span', { class: 'u-faint' }, ` （${code}）`));
  const closeBtn = createButton({
    label: S.ACTION_CLOSE,
    size: 'sm',
    variant: 'ghost',
    onClick: hideErrorBar,
  });
  errorBar = el('div', { class: 'error-bar', role: 'alert' }, text, el('span', { class: 'u-spacer' }), closeBtn.el);
  statusHost.insertBefore(errorBar, statusHost.firstChild);
}

/**
 * 撤掉顶部错误条。
 * @returns {void}
 */
function hideErrorBar() {
  if (!errorBar) return;
  const node = errorBar;
  errorBar = null;
  if (node.parentNode) node.parentNode.removeChild(node);
}

/**
 * 连接状态条：离线挂黄条，恢复后撤掉并播报（§13.6）。
 * @param {boolean} online
 * @returns {void}
 */
function setConnBar(online) {
  if (online) {
    if (connBar) {
      if (connBar.parentNode) connBar.parentNode.removeChild(connBar);
      connBar = null;
      announce(S.APP_ONLINE);
      showToast({ message: S.APP_ONLINE, kind: 'success', duration: 3000 });
    }
    return;
  }
  if (connBar) return;
  const dot = createStatusDot({ kind: 'error', text: S.STATE_OFFLINE, title: S.APP_OFFLINE_BODY });
  connBar = el('div', { class: 'conn-bar', role: 'status' }, dot.el, el('span', {}, S.APP_OFFLINE_BODY));
  statusHost.appendChild(connBar);
}

/** @returns {void} */
function handleOffline() {
  setConnBar(false);
}

/** @returns {void} */
function handleOnline() {
  setConnBar(true);
}

/** @returns {void} */
function handleStorageDegraded() {
  showToast({
    message: S.APP_STORAGE_DEGRADED_TITLE,
    detail: S.APP_STORAGE_DEGRADED_BODY,
    kind: 'warn',
    duration: 9000,
  });
}

// ==================================================================
// 全局错误兜底（§10.6）
// ==================================================================

/**
 * 脚本级错误。资源加载失败也会冒泡到 window 的 error 事件上，
 * 那种事件没有 message / error，直接忽略，否则会误报。
 *
 * @param {Event|Error} event
 * @param {string} [message]
 * @param {string} [source]
 * @param {number} [lineno]
 * @returns {void}
 */
function handleWindowError(event, message, source, lineno) {
  if (event instanceof ErrorEvent) {
    showGlobalError(event.error || new Error(event.message || '未知脚本错误'), event.filename || '');
    if (typeof console !== 'undefined' && typeof console.error === 'function') {
      console.error('[main] 未捕获的脚本错误：', event.error, source, lineno);
    }
    return;
  }
  // 兜底：某些环境不提供 ErrorEvent，只给 message
  if (message) {
    showGlobalError(new Error(String(message)), source ? `${source}:${lineno}` : '');
  }
}

/**
 * 未处理的 Promise 拒绝。视图切换时主动 abort 的请求不算错误。
 * @param {PromiseRejectionEvent} event
 * @returns {void}
 */
function handleRejection(event) {
  const reason = event && event.reason ? event.reason : null;
  const err = reason instanceof Error ? reason : new Error(String(reason || '未处理的 Promise 拒绝'));
  if (err.name === 'AbortError') return;
  if (reason instanceof ApiError && reason.code === 'ABORTED') return;
  showGlobalError(err, 'unhandledrejection');
  if (typeof console !== 'undefined' && typeof console.error === 'function') {
    console.error('[main] 未处理的 Promise 拒绝：', err);
  }
}

/**
 * 统一的「页面级错误」呈现：错误条 + toast，两者都是 assertive。
 * @param {Error} err
 * @param {string} [code]
 * @returns {void}
 */
function showGlobalError(err, code) {
  const isApi = err instanceof ApiError;
  const title = isApi ? errorTitle(err.code) : S.APP_UNEXPECTED_TITLE;
  const body = isApi ? errorBody(err.code) : S.APP_UNEXPECTED_BODY;
  showErrorBar(title, body, code || (isApi ? err.code : ''));
  showToast({ message: title, detail: body, kind: 'error', duration: 10000 });
}

// ==================================================================
// 导航（§12.3）
// ==================================================================

/**
 * 建页头导航。链接走 hash，浏览器前进/后退天然可用。
 * @returns {void}
 */
function mountNav() {
  clear(navEl);
  NAV_ITEMS.forEach((item) => {
    navEl.appendChild(el('a', { class: 'app-nav__link', href: buildHash(item.name) }, item.label));
  });
  syncNav();
  app.subscribe((s) => s.lastTask, () => syncNav());
}

/**
 * 同步 `aria-current` 与「工作台」入口的可用性。
 * 「工作台」直达上次任务；没有上次任务时置灰并给出原因文案。
 * @param {{route?: {name: string, params?: object}, lastTask?: string}} [snapshot]
 * @returns {void}
 */
function syncNav(snapshot = app.getState()) {
  const { route = { name: '', params: {} }, lastTask = '' } = snapshot;
  const links = navEl.querySelectorAll('.app-nav__link');
  // 工作台路由携带的任务是当前事实来源。store 的写入是微任务调度，
  // 因此不能依赖刚 setState 后立即读取到的旧 route/lastTask。
  const workspaceTask = route.name === 'workspace' && route.params && route.params.taskId;
  const activeTask = workspaceTask || lastTask;
  NAV_ITEMS.forEach((item, i) => {
    const link = links[i];
    if (!link) return;

    if (route.name === item.name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');

    if (item.name !== 'workspace') {
      link.setAttribute('href', buildHash(item.name));
      link.removeAttribute('aria-disabled');
      link.removeAttribute('title');
      return;
    }
    if (activeTask) {
      link.setAttribute('href', buildHash('workspace', { taskId: activeTask }));
      link.removeAttribute('aria-disabled');
      link.removeAttribute('title');
    } else {
      link.setAttribute('href', buildHash('tasks'));
      link.setAttribute('aria-disabled', 'true');
      link.setAttribute('title', S.NAV_WORKSPACE_EMPTY);
    }
  });
}

// ==================================================================
// 路由 → 视图（§10.3 / §10.4）
// ==================================================================

/**
 * 路由变化：销毁旧视图 → 挂载新视图 → 焦点移到本视图 h1。
 * @param {{name: string, params: object, hash: string}} route
 * @returns {void}
 */
function onRouteChange(route) {
  const nextRoute = { name: route.name, params: route.params };
  const state = app.getState();
  const routeTask = route.name === 'workspace' && route.params && route.params.taskId;
  const nextLastTask = routeTask || state.lastTask || storage.get(STORAGE_KEYS.LAST_TASK, '') || '';
  // Keep the selected task available to the top-level nav after a direct hash
  // visit or a task-card click. Persisting here also covers non-card entry paths.
  if (routeTask && routeTask !== state.lastTask) storage.set(STORAGE_KEYS.LAST_TASK, routeTask);
  app.setState({ route: nextRoute, lastTask: nextLastTask });
  // setState notifies on a microtask; use the route snapshot for this render so
  // the active tab never lags one click behind the visible view.
  syncNav({ route: nextRoute, lastTask: nextLastTask });

  const key = `${route.name}#${JSON.stringify(route.params)}`;
  if (key === currentRouteKey && currentView) return;
  currentRouteKey = key;

  destroyCurrentView();
  window.scrollTo(0, 0);

  // 工作台要拿模型档案当档案下拉的数据源，没到位就先挂一次，数据到了再重挂
  if (route.name === 'workspace' && !modelsReady) {
    mountView(route);
    loadModels().then(() => {
      if (currentRouteKey === key && currentView) remount(route);
    });
    return;
  }

  mountView(route);
}

/**
 * 挂载当前路由对应的视图。
 *
 * @param {{name: string, params: object}} route
 * @returns {void}
 */
function mountView(route) {
  let view;
  try {
    view = createView(route);
  } catch (err) {
    renderBootFailure(err);
    return;
  }

  currentView = view;
  clear(rootEl);
  rootEl.appendChild(view.el);

  // 焦点移到本视图 h1；工作台优先回到上次区域与滚动位置（§13.5）
  if (route.name === 'workspace' && route.params.region && typeof view.focusRegion === 'function') {
    view.focusRegion(route.params.region);
  } else {
    focusHeading(view.el_h1);
    if (route.name === 'workspace' && typeof view.restoreScroll === 'function') view.restoreScroll();
  }

  announce(t(S.ANNOUNCE_ROUTE, { page: pageLabel(route.name) }));
}

/**
 * 占位视图就位、数据到位后重挂一次。
 * @param {{name: string, params: object}} route
 * @returns {void}
 */
function remount(route) {
  destroyCurrentView();
  mountView(route);
}

/**
 * 路由名 → 中文页名（播报用）。
 * @param {string} name
 * @returns {string}
 */
function pageLabel(name) {
  const item = NAV_ITEMS.find((n) => n.name === name);
  return item ? item.label : S.APP_NAME;
}

/**
 * 按路由名创建视图。记分板与帮助页走动态 import()，不进首屏包（§10.5）。
 *
 * @param {{name: string, params: object}} route
 * @returns {{el: HTMLElement, el_h1: HTMLElement, destroy: Function}}
 */
function createView(route) {
  const state = app.getState();
  switch (route.name) {
    case 'tasks':
      return createTaskLibrary({ navigate });
    case 'workspace':
      return createWorkspace({
        taskId: route.params.taskId,
        region: route.params.region,
        navigate,
        models: state.models,
        prefs: state.prefs,
      });
    case 'scoreboard':
      return createLazyView('记分板', () => import('./views/scoreboard.js'), (m) => m.createScoreboard({ navigate }), S.ANNOUNCE_SB_LOADED);
    case 'batch':
      return createLazyView(S.NAV_BATCH, () => import('./views/batch.js'), (m) => m.createBatch({ navigate }), null);
    case 'models':
      return createModelsView();
    case 'settings':
      return createSettingsView();
    case 'help':
      return createLazyView('帮助', () => import('./views/help.js'), (m) => m.createHelp({}), null);
    default:
      return createTaskLibrary({ navigate });
  }
}

/**
 * 按需加载的视图：先渲染标题占位，模块到位后原地替换。
 * 视图切走时用 alive 标志放弃这次替换，避免往已销毁的 DOM 上写。
 *
 * @param {string} label 中文页名
 * @param {() => Promise<object>} loader 动态 import
 * @param {(mod: object) => object} factory 用模块建视图
 * @param {string|null} [announceText] 到位后的播报文案
 * @returns {{el: HTMLElement, el_h1: HTMLElement, destroy: Function}}
 */
function createLazyView(label, loader, factory, announceText) {
  const host = el('div', { class: 'view' }, el('p', { class: 'u-muted' }, S.ACTION_LOADING));
  const h1 = el('h1', { tabindex: '-1' }, label);
  let inner = null;
  let alive = true;

  const shell = {
    el: host,
    el_h1: h1,
    destroy() {
      alive = false;
      if (inner) inner.destroy();
    },
  };

  Promise.resolve()
    .then(loader)
    .then((mod) => {
      if (!alive) return;
      if (announceText) announce(announceText);
      inner = factory(mod);
      clear(host);
      host.appendChild(inner.el);
      focusHeading(inner.el_h1);
    })
    .catch((err) => {
      if (!alive) return;
      showGlobalError(err, 'lazy-import');
    });

  return shell;
}

/**
 * 模型档案视图：变化时同步给全局 store，工作台立即能用新列表。
 * @returns {{el: HTMLElement, el_h1: HTMLElement, destroy: Function}}
 */
function createModelsView() {
  const host = el('div', { class: 'view' });
  const h1 = el('h1', { tabindex: '-1' }, S.NAV_MODELS);
  let inner = null;
  let alive = true;

  import('./views/models.js')
    .then((m) => {
      if (!alive) return;
      inner = m.createModels({ onChange: onModelsChanged });
      clear(host);
      host.appendChild(inner.el);
      focusHeading(inner.el_h1);
    })
    .catch((err) => {
      if (alive) showGlobalError(err, 'lazy-import');
    });

  return {
    el: host,
    el_h1: h1,
    destroy() {
      alive = false;
      if (inner) inner.destroy();
    },
  };
}

/**
 * 设置视图：偏好变化同步给全局 store。
 * @returns {{el: HTMLElement, el_h1: HTMLElement, destroy: Function}}
 */
function createSettingsView() {
  const host = el('div', { class: 'view' });
  const h1 = el('h1', { tabindex: '-1' }, S.NAV_SETTINGS);
  let inner = null;
  let alive = true;

  import('./views/settings.js')
    .then((m) => {
      if (!alive) return;
      inner = m.createSettings({ onPrefsChange: onPrefsChanged });
      clear(host);
      host.appendChild(inner.el);
      focusHeading(inner.el_h1);
    })
    .catch((err) => {
      if (alive) showGlobalError(err, 'lazy-import');
    });

  return {
    el: host,
    el_h1: h1,
    destroy() {
      alive = false;
      if (inner) inner.destroy();
    },
  };
}

/**
 * 装配失败兜底：把话说清楚，而不是留一块白屏。
 * @param {Error} err
 * @returns {void}
 */
function renderBootFailure(err) {
  clear(rootEl);
  const h1 = el('h1', { tabindex: '-1' }, S.APP_BOOT_FAIL_TITLE);
  rootEl.appendChild(
    el(
      'div',
      { class: 'view' },
      h1,
      el(
        'div',
        { class: 'card' },
        el('p', {}, S.APP_BOOT_FAIL_BODY),
        createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => window.location.reload() }).el,
      ),
    ),
  );
  showErrorBar(S.APP_BOOT_FAIL_TITLE, S.APP_BOOT_FAIL_BODY);
  if (typeof console !== 'undefined' && typeof console.error === 'function') {
    console.error('[main] 视图装配失败：', err);
  }
  focusHeading(h1);
}

/**
 * 销毁当前视图（解绑事件、停轮询、取消在途请求，全由视图自己负责，§10.4）。
 * @returns {void}
 */
function destroyCurrentView() {
  if (!currentView) return;
  try {
    currentView.destroy();
  } catch (err) {
    if (typeof console !== 'undefined' && typeof console.error === 'function') {
      console.error('[main] 视图销毁时出错：', err);
    }
  }
  currentView = null;
}

// ==================================================================
// 跨视图数据：模型档案（§10.2 跨模块只走 store）
// ==================================================================

/**
 * 拉一次模型档案。拉不到不算致命：工作台会显示「先在模型档案页新增」的空态。
 * @returns {Promise<void>}
 */
function loadModels() {
  const scope = api.scope();
  return api
    .get('/models', { scope })
    .then((res) => {
      const list = Array.isArray(res) ? res : (res && res.models) || [];
      app.setState({ models: list });
      modelsReady = true;
    })
    .catch((err) => {
      if (err instanceof ApiError && err.code === 'OFFLINE') setConnBar(false);
      app.setState({ models: [] });
      modelsReady = true; // 失败也放行，让页面给出可操作的空态而不是一直转圈
    })
    .finally(() => scope.cancelAll());
}

/**
 * 模型页改动后的回调：同步 store，再从服务端回读一次保证一致。
 * @param {Array} models
 * @returns {void}
 */
function onModelsChanged(models) {
  app.setState({ models: Array.isArray(models) ? models.slice() : [] });
  loadModels();
}

/**
 * 设置页偏好变化后的回调。
 * @param {object} prefs
 * @returns {void}
 */
function onPrefsChanged(prefs) {
  app.setState({ prefs: { ...app.getState().prefs, ...(prefs || {}) } });
}

/**
 * 路由导航的统一入口，传给各视图。
 *
 * @param {string} name
 * @param {object} [params]
 * @param {{replace?: boolean}} [opts] replace=true 不留历史记录
 * @returns {void}
 */
function navigate(name, params = {}, opts = {}) {
  if (!router) return;
  router.navigate(name, params, opts);
}

// ==================================================================
// 全局单键快捷键（§13.4）
// ==================================================================

/**
 * 只处理 `?`；Esc 兜底关掉最上层浮层。
 * 焦点在输入控件里时单键快捷键一律失效；Ctrl/Cmd/Alt 组合一律放行。
 * @param {KeyboardEvent} event
 * @returns {void}
 */
function onGlobalKeydown(event) {
  if (event.defaultPrevented) return;
  if (event.key === 'Escape') {
    if (hasOpenModal()) closeTopModal();
    return;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (isEditableTarget(event.target)) return;
  if (event.key === '?') {
    event.preventDefault();
    openShortcutHelp();
  }
}

/** 打开快捷键表之前记住的焦点，关闭后还回去。 */
let focusBeforeShortcut = null;

/**
 * 打开快捷键表（`?`）。表本身来自帮助页模块，按需加载。
 * @returns {void}
 */
function openShortcutHelp() {
  const loaded = helpMod ? Promise.resolve(helpMod) : import('./views/help.js');
  loaded
    .then((m) => {
      helpMod = m;
      if (hasOpenModal()) return;
      focusBeforeShortcut = document.activeElement;
      openModal({
        title: S.HELP_SHORTCUT_TITLE,
        body: [el('p', {}, S.HELP_SHORTCUT_DESC), buildShortcutTable(m.SHORTCUT_TABLE || [])],
        onClose: () => {
          restoreFocus(focusBeforeShortcut);
          focusBeforeShortcut = null;
        },
      });
    })
    .catch((err) => showGlobalError(err, 'shortcut-help'));
}

/**
 * 快捷键表（纯常量 + 业务无关，用 el() 构建，不涉及 innerHTML）。
 * @param {Array<{key: string, action: string, when: string}>} rows
 * @returns {HTMLElement}
 */
function buildShortcutTable(rows) {
  return el(
    'table',
    { class: 'table' },
    el('caption', { class: 'visually-hidden' }, S.HELP_SHORTCUT_TITLE),
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_KEY),
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_ACTION),
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_WHEN),
      ),
    ),
    el(
      'tbody',
      {},
      ...rows.map((r) =>
        el(
          'tr',
          {},
          el('th', { scope: 'row' }, el('kbd', { class: 'key' }, r.key)),
          el('td', {}, r.action),
          el('td', { class: 'u-muted' }, r.when),
        ),
      ),
    ),
  );
}

// ==================================================================
// 入口
// ==================================================================

boot();
