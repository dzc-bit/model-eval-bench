/**
 * storage.js — localStorage 封装（§10.3）
 *
 * 职责：
 *   1. 统一键前缀 `evalconsole:` 与 schema 版本号，升级时旧键自动作废。
 *   2. 隐私模式 / 容量满 / 写入被拒时整体降级为内存态，页面不崩（§11.2 #9）。
 *   3. 提供按「作用域」分组的读写，便于按任务记轮次、滚动位置（§13.5）。
 *
 * 依赖：无。
 * 导出：SCHEMA_VERSION、storage（{ available, persistent, get, set, remove, clearAll, keys, useMemory }）
 *
 * 设计说明：
 *   - 写入一律 JSON 序列化；读取失败（脏数据）返回默认值而不是抛错。
 *   - clearAll 只清本前缀的键，不动同源下其它应用的数据。
 */

/** schema 版本：数据结构不兼容变更时 +1，旧键将被忽略并覆盖。 */
export const SCHEMA_VERSION = 2;

/** 统一键前缀（§10.3 硬要求）。 */
const PREFIX = 'evalconsole:';

/** 当前版本前缀，例如 `evalconsole:v2:`。 */
const KEY_PREFIX = `${PREFIX}v${SCHEMA_VERSION}:`;

/** 降级用的内存态映射；localStorage 不可用时顶上。 */
const memoryStore = new Map();

/** localStorage 是否真正可写（隐私模式下存在但 setItem 抛错的情况很常见）。 */
let persistent = probe();

/** 已向上层汇报过的「存储不可用」标记，避免同一错误反复弹。 */
let degradedNotified = false;

/**
 * 探测 localStorage 是否可写。
 * @returns {boolean}
 */
function probe() {
  try {
    if (typeof localStorage === 'undefined' || localStorage === null) return false;
    const probeKey = `${KEY_PREFIX}__probe__`;
    localStorage.setItem(probeKey, '1');
    localStorage.removeItem(probeKey);
    return true;
  } catch {
    return false;
  }
}

/**
 * 把逻辑键拼成带前缀与版本的物理键。
 * @param {string} key
 * @returns {string}
 */
function fullKey(key) {
  return `${KEY_PREFIX}${key}`;
}

/**
 * 报一次「已降级到内存态」。只报一次，避免用户被重复打扰。
 */
function reportDegraded() {
  if (degradedNotified) return;
  degradedNotified = true;
  if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
    window.dispatchEvent(
      new CustomEvent('evalconsole:storage-degraded', {
        detail: { code: 'STORAGE_FULL' },
      }),
    );
  }
  if (typeof console !== 'undefined' && typeof console.warn === 'function') {
    console.warn('[storage] localStorage 不可用，已降级为内存态：本次会话内有效，刷新后丢失。');
  }
}

export const storage = {
  /** localStorage 是否可用。false 表示处于内存态。 */
  get available() {
    return persistent;
  },

  /** 内存态别名，语义上更直白：persistent=false 时用它。 */
  get persistent() {
    return persistent;
  },

  /**
   * 读取一个键。
   * @param {string} key 逻辑键（不含前缀）
   * @param {*} [fallback] 缺省值 / 解析失败时的回落值
   * @returns {*}
   */
  get(key, fallback = null) {
    const physical = fullKey(key);
    try {
      if (!persistent) {
        return memoryStore.has(physical) ? memoryStore.get(physical) : fallback;
      }
      const raw = localStorage.getItem(physical);
      if (raw === null) return fallback;
      return JSON.parse(raw);
    } catch {
      // 脏数据：清掉这把钥匙，避免每次读都失败
      try {
        if (persistent) localStorage.removeItem(physical);
        else memoryStore.delete(physical);
      } catch {
        /* 清理失败也不影响返回默认值 */
      }
      return fallback;
    }
  },

  /**
   * 写入一个键。
   * @param {string} key 逻辑键
   * @param {*} value 任意可 JSON 序列化的值
   * @returns {boolean} 是否真的落盘（false = 已降级为内存态）
   */
  set(key, value) {
    const physical = fullKey(key);
    let raw;
    try {
      raw = JSON.stringify(value);
    } catch {
      return false; // 循环引用等不可序列化数据，丢弃
    }
    if (!persistent) {
      memoryStore.set(physical, value);
      return false;
    }
    try {
      localStorage.setItem(physical, raw);
      return true;
    } catch {
      // 容量满 / 权限被拒：降级到内存态，本次会话仍可用（§11.2 #9）
      reportDegraded();
      persistent = false;
      memoryStore.set(physical, value);
      return false;
    }
  },

  /**
   * 删除一个键。
   * @param {string} key
   */
  remove(key) {
    const physical = fullKey(key);
    memoryStore.delete(physical);
    if (!persistent) return;
    try {
      localStorage.removeItem(physical);
    } catch {
      /* 删不掉就留着，读取时仍会走降级分支 */
    }
  },

  /**
   * 列出本模块管理下的所有逻辑键（去掉前缀与版本）。
   * @returns {string[]}
   */
  keys() {
    const out = [];
    const strip = (k) => k.slice(KEY_PREFIX.length);
    memoryStore.forEach((_, k) => {
      if (typeof k === 'string' && k.startsWith(KEY_PREFIX)) out.push(strip(k));
    });
    if (!persistent) return out;
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i);
        if (typeof k === 'string' && k.startsWith(KEY_PREFIX)) out.push(strip(k));
      }
    } catch {
      /* 枚举失败就返回内存态里那部分 */
    }
    return Array.from(new Set(out));
  },

  /**
   * 清除本模块管理的全部键（含旧版本残留）。
   * 不会碰同源下其它前缀的数据。
   * @returns {number} 实际删除的条数
   */
  clearAll() {
    const removed = new Set();
    Array.from(memoryStore.keys()).forEach((k) => {
      if (typeof k === 'string' && k.startsWith(PREFIX)) {
        memoryStore.delete(k);
        removed.add(k);
      }
    });
    if (persistent) {
      try {
        const doomed = [];
        for (let i = 0; i < localStorage.length; i += 1) {
          const k = localStorage.key(i);
          if (typeof k === 'string' && k.startsWith(PREFIX)) doomed.push(k);
        }
        doomed.forEach((k) => {
          localStorage.removeItem(k);
          removed.add(k);
        });
      } catch {
        /* 拿不到就只清内存态 */
      }
    }
    // 清完重新探测一次：清完可能恢复了写入能力
    persistent = probe();
    return removed.size;
  },

  /**
   * 作用域化读写：storage.scoped('ws', 'T2-04').get('round') → 'ws:T2-04:round'
   * 用途：按任务记轮次与滚动位置（§13.5）。
   * @param {...string} parts 作用域片段
   * @returns {{get: Function, set: Function, remove: Function}}
   */
  scoped(...parts) {
    const scope = parts.filter(Boolean).join(':');
    const key = (leaf) => (scope ? `${scope}:${leaf}` : leaf);
    return {
      get: (leaf, fallback = null) => storage.get(key(leaf), fallback),
      set: (leaf, value) => storage.set(key(leaf), value),
      remove: (leaf) => storage.remove(key(leaf)),
    };
  },
};

/** 设置页用的键名常量，避免各视图各写一份字符串。 */
export const STORAGE_KEYS = {
  PREFERENCES: 'prefs',
  LAST_TASK: 'last-task',
  LAST_MODEL: 'last-model',
  GUIDE_DISMISSED: 'guide-dismissed',
  ONBOARDED: 'onboarded',
  WORKSPACE: 'workspace',
  MODELS: 'models-cache',
  MOCK_FLAG: 'mock-flag',
};

/** 默认界面偏好。 */
export const DEFAULT_PREFS = {
  autoExpandLog: false,
  confirmDestructive: true,
  contributeStats: true,
};
