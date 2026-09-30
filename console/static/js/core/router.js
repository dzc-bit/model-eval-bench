/**
 * router.js — hash 路由（§10.3）
 *
 * 职责：
 *   1. 解析并监听 `location.hash`，支持七条路由：
 *      `#/tasks`、`#/workspace/<taskId>`、`#/batch`、`#/scoreboard`、`#/models`、`#/settings`、`#/help`。
 *   2. 未知路由回退到任务库（并把地址改回 `#/tasks`，避免坏地址留在历史里）。
 *   3. 工作台支持可选的子区域段 `#/workspace/<taskId>/<region>`，刷新后回到同一区域（§13.5）。
 *   4. 切换后把焦点移到该视图 h1（由 onChange 回调里的 a11y.focusHeading 完成）。
 *
 * 依赖：无。
 * 导出：createRouter, ROUTES, matchRoute, parseHash, buildHash
 *
 * 设计说明：
 *   - 只用 hash，不碰 History API：`file://` 之外的环境都能刷新回到原处，
 *     且后端 server.py 只需把所有路径都回 index.html。
 *   - 同一路由重复导航不重复触发（避免点同一个导航项时空转一遍视图）。
 */

/** 路由表：name → 匹配函数。 */
export const ROUTES = {
  tasks: { name: 'tasks', pattern: ['tasks'] },
  workspace: { name: 'workspace', pattern: ['workspace', ':taskId', ':region?', ':runId?'] },
  batch: { name: 'batch', pattern: ['batch'] },
  scoreboard: { name: 'scoreboard', pattern: ['scoreboard'] },
  models: { name: 'models', pattern: ['models'] },
  settings: { name: 'settings', pattern: ['settings'] },
  help: { name: 'help', pattern: ['help'] },
};

/** 默认路由（未知路由回退到这里，§10.3）。 */
export const FALLBACK_ROUTE = 'tasks';

/** 工作台允许的子区域名。 */
export const WORKSPACE_REGIONS = ['prompt', 'sandbox', 'grade', 'run', 'chat'];

/**
 * 把 hash 拆成片段数组。
 * @param {string} hash 形如 `#/workspace/T1-01/grade`
 * @returns {string[]} 已解码的片段
 */
export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  if (!raw) return [];
  return raw
    .split('/')
    .map((seg) => {
      try {
        return decodeURIComponent(seg);
      } catch {
        return seg;
      }
    })
    .filter((seg) => seg.length > 0);
}

/**
 * 按路由表匹配片段。
 * @param {string[]} segments
 * @returns {{name: string, params: object}|null}
 */
export function matchRoute(segments) {
  for (const route of Object.values(ROUTES)) {
    const params = matchPattern(route.pattern, segments);
    if (params) return { name: route.name, params };
  }
  return null;
}

/**
 * 单条模式匹配：`['workspace', ':taskId', ':region?', ':runId?']`。
 * @param {string[]} pattern
 * @param {string[]} segments
 * @returns {object|null}
 */
function matchPattern(pattern, segments) {
  const params = {};
  let i = 0;
  for (const part of pattern) {
    if (typeof part === 'string' && part.startsWith(':')) {
      const optional = part.endsWith('?');
      const key = optional ? part.slice(1, -1) : part.slice(1);
      if (i >= segments.length) {
        if (optional) continue;
        return null;
      }
      params[key] = segments[i];
      i += 1;
    } else {
      if (segments[i] !== part) return null;
      i += 1;
    }
  }
  if (i < segments.length) return null;
  return params;
}

/**
 * 构造 hash 字符串。
 * @param {string} name 路由名
 * @param {object} [params] 路由参数
 * @returns {string}
 */
export function buildHash(name, params = {}) {
  const route = ROUTES[name];
  if (!route) return `#/${FALLBACK_ROUTE}`;
  const segments = route.pattern.map((part) => {
    if (typeof part !== 'string' || !part.startsWith(':')) return part;
    const optional = part.endsWith('?');
    const key = optional ? part.slice(1, -1) : part.slice(1);
    const value = params[key];
    if (value === undefined || value === null || value === '') {
      return optional ? null : '';
    }
    return encodeURIComponent(String(value));
  });
  return `#/${segments.filter((s) => s !== null && s !== '').join('/')}`;
}

/**
 * 创建路由器。
 *
 * @param {{onChange?: (route: {name: string, params: object, hash: string}) => void, onNavigate?: (route: object) => void}} [options]
 * @returns {{
 *   start: () => void,
 *   stop: () => void,
 *   getRoute: () => object,
 *   navigate: (name: string, params?: object, opts?: {replace?: boolean}) => void,
 *   refresh: () => void,
 *   destroy: () => void
 * }}
 */
export function createRouter(options = {}) {
  const { onChange, onNavigate } = options;
  let started = false;
  let current = null;
  let suppressNext = false;

  /**
   * 解析当前 hash 为路由对象。
   * @returns {{name: string, params: object, hash: string}}
   */
  function resolve() {
    const hash = window.location.hash || '';
    const segments = parseHash(hash);
    const matched = matchRoute(segments);
    if (!matched) {
      return { name: FALLBACK_ROUTE, params: {}, hash: `#/${FALLBACK_ROUTE}`, invalid: true };
    }
    const params = { ...matched.params };
    // 工作台子区域做白名单校验，非法值当作没写
    if (matched.name === 'workspace' && params.region && !WORKSPACE_REGIONS.includes(params.region)) {
      delete params.region;
      delete params.runId;
    }
    if (matched.name === 'workspace' && params.runId && params.region !== 'chat') {
      return { name: FALLBACK_ROUTE, params: {}, hash: `#/${FALLBACK_ROUTE}`, invalid: true };
    }
    return { name: matched.name, params, hash };
  }

  /**
   * 处理一次路由变化。
   * @param {{force?: boolean}} [opts]
   */
  function handleChange(opts = {}) {
    const next = resolve();
    const same =
      !opts.force &&
      current &&
      current.name === next.name &&
      JSON.stringify(current.params) === JSON.stringify(next.params);

    // 未知路由：把地址改回任务库，不留坏地址
    if (next.invalid && window.location.hash !== next.hash) {
      suppressNext = true;
      window.location.replace(`${window.location.pathname}${window.location.search}${next.hash}`);
      current = next;
      if (onChange) onChange(next);
      return;
    }

    current = next;
    if (same) return;
    if (onChange) onChange(next);
  }

  /** hashchange 处理器（引用保持稳定，方便解绑）。 */
  const onHashChange = () => {
    if (suppressNext) {
      suppressNext = false;
      handleChange();
      return;
    }
    handleChange();
  };

  return {
    /**
     * 开始监听。启动时若地址栏没有 hash，自动补上默认路由。
     */
    start() {
      if (started) return;
      started = true;
      if (!window.location.hash) {
        suppressNext = true;
        window.location.replace(`${window.location.pathname}${window.location.search}#/${FALLBACK_ROUTE}`);
      }
      window.addEventListener('hashchange', onHashChange);
      handleChange({ force: true });
    },

    /** 停止监听。 */
    stop() {
      if (!started) return;
      started = false;
      window.removeEventListener('hashchange', onHashChange);
    },

    /**
     * 当前路由快照。
     * @returns {{name: string, params: object, hash: string}}
     */
    getRoute() {
      if (!current) current = resolve();
      return current;
    },

    /**
     * 主动导航。
     * @param {string} name 路由名
     * @param {object} [params]
     * @param {{replace?: boolean}} [opts] replace=true 不留历史记录
     */
    navigate(name, params = {}, opts = {}) {
      const target = buildHash(name, params);
      if (target === window.location.hash) {
        // 同一地址：强制刷新一次（例如任务库点「刷新」）
        handleChange({ force: true });
        return;
      }
      if (opts.replace) {
        suppressNext = true;
        window.location.replace(`${window.location.pathname}${window.location.search}${target}`);
        handleChange();
      } else {
        window.location.hash = target;
        if (onNavigate) onNavigate({ name, params });
      }
    },

    /** 强制重新解析当前路由（数据变化后想重建视图时用）。 */
    refresh() {
      handleChange({ force: true });
    },

    /** 彻底销毁（解绑事件）。 */
    destroy() {
      this.stop();
      current = null;
    },
  };
}
