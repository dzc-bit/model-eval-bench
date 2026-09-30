/**
 * store.js — 单一状态树（§10.3）
 *
 * 职责：
 *   1. 持有全应用唯一状态树，提供 getState / setState / subscribe。
 *   2. setState 走「浅合并」：顶层 key 覆盖，不做深合并，避免误改嵌套对象。
 *   3. 一帧内（同一微任务批次）多次 setState 合并为一次通知，订阅者不会被打断多次。
 *   4. 订阅按选择器粒度触发：selector 返回值用 Object.is 比较，变了才通知。
 *   5. 订阅者回调签名 handler(next, prev)；无选择器时 next/prev 为整棵状态。
 *
 * 依赖：无。
 * 导出：createStore(initial) → { getState, setState, subscribe, batch, reset }
 *
 * 使用约束：
 *   - 组件内部禁止持有全局状态，数据一律通过 update(props) 注入（§10.4）。
 *   - 跨模块通信只走 store 订阅或事件，不直接互相调用（§10.2）。
 */

/**
 * @typedef {(state: object) => unknown} Selector 选择器：返回该订阅关心的那一片状态
 * @typedef {(next: unknown, prev: unknown) => void} Handler 通知回调
 */

/** @returns {object} 浅拷贝一份状态，避免外部直接改内部引用 */
function shallowCopy(state) {
  return { ...state };
}

/**
 * 浅比较选择器结果：Object.is 失败时退化为 NaN 安全的相等判断。
 * @param {unknown} a
 * @param {unknown} b
 * @returns {boolean}
 */
function sameValue(a, b) {
  if (Object.is(a, b)) return true;
  // NaN !== NaN 的特例：两次算出 NaN 视为没变，避免无谓重绘
  return Number.isNaN(a) && Number.isNaN(b);
}

/**
 * 创建状态容器。
 * @param {object} initialState 初始状态树
 * @returns {{getState: () => object, setState: (patch: object) => void, subscribe: (selector: ?Selector, handler: Handler) => () => void, batch: (fn: () => void) => void, reset: (next?: object) => void}}
 */
export function createStore(initialState = {}) {
  /** 当前状态（对外只暴露副本） */
  let state = shallowCopy(initialState);

  /** 订阅表：每条 { selector, handler, last, active } */
  const subscribers = new Set();

  /** 是否有已排队的微任务通知 */
  let notifyQueued = false;

  /** 同一批次内累积的补丁（后写的同 key 覆盖先写的） */
  let pendingPatch = null;

  /** 正在通知时为 true，防止重入导致重复派发 */
  let notifying = false;

  /**
   * 派发一批变更：逐个比较选择器结果，变了才调 handler(next, prev)。
   * @param {object} prevState 上一份状态
   * @param {object} nextState 新状态
   */
  function flush(prevState, nextState) {
    // 拷贝一份再遍历：允许 handler 在回调里退订/订阅，不影响本轮
    const list = Array.from(subscribers);
    for (const sub of list) {
      if (!sub.active) continue;
      const nextValue = sub.selector ? sub.selector(nextState) : nextState;
      const prevValue = sub.selector ? sub.selector(prevState) : prevState;
      if (sameValue(nextValue, sub.last)) continue;
      sub.last = nextValue;
      try {
        sub.handler(nextValue, prevValue);
      } catch (err) {
        // 订阅者报错不能拖垮状态树：交给全局错误处理（main.js 挂 window.onerror）
        reportSubscriberError(err);
      }
    }
  }

  /**
   * 微任务末尾统一派发。
   */
  function scheduleNotify() {
    if (notifyQueued) return;
    notifyQueued = true;
    queueMicrotask(() => {
      notifyQueued = false;
      if (!pendingPatch) return;
      const patch = pendingPatch;
      pendingPatch = null;
      const prevState = state;
      const nextState = { ...state, ...patch };
      state = nextState;
      if (notifying) return; // 重入保护：外层通知结束后自行处理
      notifying = true;
      try {
        flush(prevState, nextState);
      } finally {
        notifying = false;
      }
    });
  }

  return {
    /**
     * 读取当前状态（返回浅拷贝，改它不会影响 store）。
     * @returns {object}
     */
    getState() {
      return state;
    },

    /**
     * 浅合并更新；同一微任务批次内的多次调用会合并成一次通知。
     * @param {object} patch 顶层 key → 新值
     */
    setState(patch) {
      if (!patch || typeof patch !== 'object') return;
      pendingPatch = { ...(pendingPatch || {}), ...patch };
      scheduleNotify();
    },

    /**
     * 订阅状态变化。
     * @param {Selector|null} selector 选择器；传 null 表示关心整棵状态
     * @param {Handler} handler 回调，签名 (next, prev)
     * @returns {() => void} 退订函数
     */
    subscribe(selector, handler) {
      if (typeof handler !== 'function') return () => {};
      const sub = {
        selector: typeof selector === 'function' ? selector : null,
        handler,
        last: undefined,
        active: true,
      };
      sub.last = sub.selector ? sub.selector(state) : state;
      subscribers.add(sub);
      return () => {
        sub.active = false;
        subscribers.delete(sub);
      };
    },

    /**
     * 把多次 setState 包进一个同步块（派发仍发生在微任务末尾）。
     * 用途：一次交互里同时改多个顶层字段时，语义上它们是同一次变更。
     * @param {() => void} fn
     */
    batch(fn) {
      if (typeof fn !== 'function') return;
      fn();
    },

    /**
     * 整体替换状态（切任务 / 登出这类场景），并立即走一次通知。
     * @param {object} [next]
     */
    reset(next = {}) {
      const prevState = state;
      state = shallowCopy(next);
      pendingPatch = null;
      notifying = true;
      try {
        flush(prevState, state);
      } finally {
        notifying = false;
      }
    },
  };
}

/**
 * 订阅者回调抛错时的兜底上报。
 * @param {unknown} err
 */
function reportSubscriberError(err) {
  if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
    window.dispatchEvent(
      new CustomEvent('evalconsole:store-error', { detail: { error: err } }),
    );
  }
  // 不再向上抛：状态树是全局基础设施，一个订阅者出错不该中断其它订阅者
  if (typeof console !== 'undefined' && typeof console.error === 'function') {
    console.error('[store] 订阅回调抛出异常：', err);
  }
}
