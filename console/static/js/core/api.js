/**
 * api.js — 后端请求封装（§10.3）
 *
 * 职责：
 *   1. JSON 编解码 + 统一错误类型 `ApiError {code, message, detail}`。
 *   2. AbortController 超时：默认 30s，校验类 300s。
 *   3. 离线检测：navigator.onLine === false 或连接被拒 → 一律给「本地服务未连接」并可重试。
 *   4. 请求作用域（scope）：页面切换时 `scope.cancelAll()` 取消在途请求（§11.2 #2）。
 *   5. 永不抛裸异常：所有出口都包成 ApiError。
 *
 * 依赖：core/strings.js（错误码 → 中文文案）。
 * 导出：ApiError, api（get/post/patch/del/request/download/scope）, isMockEnabled
 *
 * 契约（§15）：所有请求只打本机 /api/*，绝不访问外部域名。
 * Mock：URL 带 ?mock=1 时，动态 import('./mock.js') 顶替传输层，
 *       真实 fetch 代码路径一行不改（见 pickTransport）。
 */

import { S, errorTitle, errorBody, normalizeCode } from './strings.js';

/** 默认超时（毫秒）。 */
const DEFAULT_TIMEOUT = 30_000;
/** 校验类请求超时（毫秒）：§5.4 每题 grade_timeout_s 默认 240s。 */
const GRADE_TIMEOUT = 300_000;

/** API 根路径；只打本机。 */
const API_BASE = '/api';

/**
 * 统一错误类型。
 */
export class ApiError extends Error {
  /**
   * @param {string} code 稳定错误码（前端据此映射中文文案）
   * @param {string} [message] 中文短标题
   * @param {string} [detail] 中文详情正文（发生 + 影响 + 下一步）
   * @param {object} [extra] 附加信息（原始响应、状态码等）
   */
  constructor(code, message, detail, extra = {}) {
    super(message || errorTitle(code));
    this.name = 'ApiError';
    this.code = code || 'INTERNAL';
    this.message = message || errorTitle(this.code);
    this.detail = detail || errorBody(this.code, extra.vars);
    Object.assign(this, extra);
  }
}

/** 是否 mock 模式。 */
let mockEnabled = false;
try {
  mockEnabled = new URLSearchParams(window.location.search).get('mock') === '1';
} catch {
  mockEnabled = false;
}

/**
 * mock 模式判定（供开发提示条使用）。
 * @returns {boolean}
 */
export function isMockEnabled() {
  return mockEnabled;
}

/** 当前生效的传输层；mock 模式下是 mock 模块提供的假 fetch。 */
let transport = typeof fetch === 'function' ? fetch.bind(window) : null;

// 开发期：URL 带 ?mock=1 就顶替传输层。真实 fetch 路径完全不动。
if (mockEnabled) {
  try {
    const mod = await import('./mock.js');
    transport = mod.transport;
  } catch (err) {
    transport = typeof fetch === 'function' ? fetch.bind(window) : null;
    if (typeof console !== 'undefined' && typeof console.warn === 'function') {
      console.warn('[api] mock 模块载入失败，回退到真实请求：', err);
    }
  }
}

/**
 * 当前是否处于离线（浏览器层面判定）。
 * @returns {boolean}
 */
function browserOffline() {
  return typeof navigator !== 'undefined' && navigator.onLine === false;
}

/**
 * 解析响应体；后端统一返回 JSON。
 * @param {Response} response
 * @returns {Promise<any>}
 */
async function parseBody(response) {
  const type = response.headers ? response.headers.get('content-type') || '' : '';
  if (response.status === 204) return null;
  if (type.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      throw new ApiError('PARSE_ERROR', undefined, undefined, { status: response.status });
    }
  }
  try {
    const text = await response.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return { raw: text };
    }
  } catch {
    throw new ApiError('PARSE_ERROR', undefined, undefined, { status: response.status });
  }
}

/**
 * 把后端错误响应翻译成 ApiError。
 *
 * 后端契约（§15 / harness/errors.py）：失败一律返回 `{code, message, detail?}`，
 * `code` 是 E_* 稳定码，`message` 已经是中文。归一化成前端短码后，
 * 短标题用后端那句具体中文，详情正文用前端按码写好的「发生 + 影响 + 下一步」。
 *
 * @param {Response} response
 * @param {any} body 已解析的响应体
 * @returns {ApiError}
 */
function toApiError(response, body) {
  const raw = (body && typeof body.code === 'string' && body.code) || '';
  const code = raw ? normalizeCode(raw) : httpToCode(response.status);
  const vars = {};
  if (body && typeof body.timeout_s === 'number') vars.n = body.timeout_s;
  if (body && body.detail && typeof body.detail === 'object' && body.detail.timeout_s) {
    vars.n = body.detail.timeout_s;
  }
  return new ApiError(
    code,
    (body && typeof body.message === 'string' && body.message) || undefined,
    undefined,
    { status: response.status, body, vars, backendCode: raw || code },
  );
}

/**
 * HTTP 状态码 → 前端错误码兜底（后端没给 code 时用）。
 * @param {number} status
 * @returns {string}
 */
function httpToCode(status) {
  if (status === 404) return 'NOT_FOUND';
  if (status === 403) return 'FORBIDDEN';
  if (status === 409) return 'BUSY';
  if (status === 408) return 'TIMEOUT';
  if (status === 413 || status === 507) return 'STORAGE_FULL';
  if (status >= 500) return 'INTERNAL';
  return 'ACTION_FAILED';
}

/**
 * 创建一个请求作用域：视图在挂载时创建、销毁时 cancelAll()。
 * @returns {{signal: AbortSignal, cancelAll: () => void, count: () => number}}
 */
function createScope() {
  const controller = new AbortController();
  let pending = 0;
  const children = new Set();

  return {
    signal: controller.signal,
    cancelAll() {
      children.forEach((c) => {
        try {
          c.abort();
        } catch {
          /* 已结束的请求忽略 */
        }
      });
      children.clear();
      pending = 0;
    },
    count: () => pending,
    /** 内部：登记一个子控制器 */
    _track(child) {
      children.add(child);
      pending += 1;
    },
    /** 内部：注销一个子控制器 */
    _untrack(child) {
      if (children.delete(child)) pending -= 1;
    },
  };
}

/**
 * 底层请求实现。
 * @param {{method: string, path: string, body?: any, timeout?: number, scope?: object, signal?: AbortSignal, raw?: boolean}} options
 * @returns {Promise<any>}
 */
async function request(options) {
  const { method, path, body, timeout = DEFAULT_TIMEOUT, scope, raw = false } = options;
  const url = path.startsWith('/api') ? path : `${API_BASE}${path.startsWith('/') ? '' : '/'}${path}`;

  if (browserOffline()) {
    throw new ApiError('OFFLINE', undefined, undefined, { path });
  }
  if (!transport) {
    throw new ApiError('OFFLINE', undefined, undefined, { path });
  }

  // 双重超时：AbortController + 计时器；外层还要叠加作用域的取消信号
  const controller = new AbortController();
  if (scope && scope._track) scope._track(controller);

  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);

  const onOuterAbort = () => controller.abort();
  if (scope && scope.signal) {
    if (scope.signal.aborted) controller.abort();
    else scope.signal.addEventListener('abort', onOuterAbort, { once: true });
  }
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener('abort', onOuterAbort, { once: true });
  }

  let response;
  try {
    const init = {
      method,
      signal: controller.signal,
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      credentials: 'same-origin',
    };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    response = await transport(url, init);
  } catch (err) {
    if (scope && scope._untrack) scope._untrack(controller);
    window.clearTimeout(timer);
    cleanupSignals();

    if (timedOut) {
      throw new ApiError('TIMEOUT', undefined, undefined, {
        path,
        vars: { n: Math.round(timeout / 1000) },
      });
    }
    if (err && (err.name === 'AbortError' || err.code === 20)) {
      // 页面切换导致的中止：不打扰用户，下次进入会重新拉
      throw new ApiError('ABORTED', undefined, undefined, { path, silent: true });
    }
    // fetch 抛 TypeError 基本都是「连不上 127.0.0.1:8899」
    throw new ApiError('OFFLINE', undefined, undefined, { path, cause: String(err && err.message) });
  } finally {
    cleanupSignals();
  }

  window.clearTimeout(timer);
  if (scope && scope._untrack) scope._untrack(controller);

  if (!response) {
    throw new ApiError('INTERNAL', undefined, undefined, { path });
  }

  // mock 模式允许直接返回普通对象
  if (raw && typeof response !== 'object') {
    return response;
  }
  if (response && response.__mockResponse) {
    return response.data;
  }

  if (!response.ok) {
    const errBody = await parseBody(response).catch(() => null);
    throw toApiError(response, errBody);
  }
  if (raw) {
    return { response };
  }
  return parseBody(response);

  /** 清理挂在外部 signal 上的监听，避免作用域长期持有 */
  function cleanupSignals() {
    if (scope && scope.signal) scope.signal.removeEventListener('abort', onOuterAbort);
    if (options.signal) options.signal.removeEventListener('abort', onOuterAbort);
  }
}

/**
 * 对外的请求门面。
 */
export const api = {
  /** 创建请求作用域（视图生命周期用）。 */
  scope: createScope,

  /**
   * GET JSON。
   * @param {string} path 形如 `/tasks`
   * @param {{scope?: object, signal?: AbortSignal, timeout?: number, params?: object}} [opts]
   * @returns {Promise<any>}
   */
  get(path, opts = {}) {
    return request({ method: 'GET', path: withParams(path, opts.params), ...opts });
  },

  /**
   * POST JSON。
   * @param {string} path
   * @param {object} [body]
   * @param {object} [opts]
   * @returns {Promise<any>}
   */
  post(path, body, opts = {}) {
    return request({ method: 'POST', path, body, ...opts });
  },

  /**
   * PATCH JSON。
   * @param {string} path
   * @param {object} [body]
   * @param {object} [opts]
   * @returns {Promise<any>}
   */
  patch(path, body, opts = {}) {
    return request({ method: 'PATCH', path, body, ...opts });
  },

  /**
   * PUT JSON。
   * @param {string} path
   * @param {object} [body]
   * @param {object} [opts]
   * @returns {Promise<any>}
   */
  put(path, body, opts = {}) {
    return request({ method: 'PUT', path, body, ...opts });
  },

  /**
   * DELETE。
   * @param {string} path
   * @param {object} [opts]
   * @returns {Promise<any>}
   */
  del(path, opts = {}) {
    return request({ method: 'DELETE', path: withParams(path, opts.params), ...opts });
  },

  /**
   * 校验类长请求：超时放宽到 300s。
   * @param {string} path
   * @param {object} [opts]
   * @returns {Promise<any>}
   */
  longPost(path, body, opts = {}) {
    return request({ method: 'POST', path, body, timeout: GRADE_TIMEOUT, ...opts });
  },

  /**
   * 下载文本/CSV（记分板导出用），返回 Blob 前的字符串。
   * @param {string} path
   * @param {object} [opts]
   * @returns {Promise<string>}
   */
  async text(path, opts = {}) {
    const res = await request({ method: 'GET', path: withParams(path, opts.params), raw: true, ...opts });
    if (res && res.response) {
      try {
        return await res.response.text();
      } catch {
        throw new ApiError('EXPORT_FAILED', undefined, undefined, { path });
      }
    }
    return typeof res === 'string' ? res : '';
  },

  /**
   * 触发浏览器下载（纯本地 Blob，不产生额外网络请求）。
   * @param {string} filename 文件名
   * @param {string} content 文本内容
   * @param {string} [mime]
   * @returns {void}
   */
  download(filename, content, mime = 'text/plain;charset=utf-8') {
    try {
      const blob = new Blob([`﻿${content}`], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // 立刻 revoke 会让部分浏览器下载失败，延后一拍
      window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (err) {
      throw new ApiError('EXPORT_FAILED', undefined, undefined, { cause: String(err) });
    }
  },

  /** 供视图判断是否 mock 模式。 */
  isMock: isMockEnabled,

  /** 超时常量，供进度条显示预计时间。 */
  TIMEOUTS: { DEFAULT: DEFAULT_TIMEOUT, GRADE: GRADE_TIMEOUT },
};

/**
 * 拼接查询串。
 * @param {string} path
 * @param {object} [params]
 * @returns {string}
 */
function withParams(path, params) {
  if (!params || typeof params !== 'object') return path;
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === '') return;
    usp.set(k, String(v));
  });
  const qs = usp.toString();
  if (!qs) return path;
  return path.includes('?') ? `${path}&${qs}` : `${path}?${qs}`;
}

/** 便捷重导出，视图直接 import { S } 之外也能拿到错误文案。 */
export { errorTitle, errorBody };
