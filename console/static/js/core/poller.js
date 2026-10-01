/**
 * poller.js — 通用轮询器（§10.3 / §13.6）
 *
 * 职责：
 *   1. 只在「有进行中任务」时轮询，空闲自动停表（enabled 由调用方给）。
 *   2. `document.hidden` 时暂停，页面重新可见时**立即**拉一次。
 *   3. 失败指数退避：1s → 2s → 5s 封顶。
 *   4. 请求序号守卫：丢弃过期响应，杜绝旧响应覆盖新状态（§11.2 #1）。
 *   5. 返回 { start, stop, destroy, triggerNow, setEnabled, state }；destroy 必清定时器。
 *
 * 依赖：无。
 * 导出：createPoller
 *
 * 使用约定：
 *   - fn 抛错视为本次失败：退避重试，并把 lastError 暴露给调用方显示状态条。
 *   - fn 内部应当自己尊重 scope 取消信号（视图 destroy 时调 poller.destroy() 即可）。
 */

import { ApiError } from './api.js';

/** 退避阶梯（毫秒），§10.3 / §13.6 指定。 */
const BACKOFF_STEPS = [1000, 2000, 5000];

/** 请求最小间隔：上一次结束后至少隔这么久再发下一次（§10.3「进行中 500ms」）。 */
const MIN_INTERVAL = 500;

/**
 * 创建轮询器。
 *
 * @param {{
 *   fn: (ctx: {seq: number}) => Promise<any>,
 *   interval?: number,
 *   enabled?: () => boolean | Promise<boolean>,
 *   onError?: (err: Error, consecutive: number) => void,
 *   onStateChange?: (state: object) => void
 * }} options
 * @returns {{start: Function, stop: Function, destroy: Function, triggerNow: Function, setEnabled: Function, getState: Function}}
 */
export function createPoller(options) {
  const {
    fn,
    interval = 1500,
    enabled = () => true,
    onError,
    onStateChange,
  } = options || {};

  /** 请求序号：每次发起 +1，响应回来时序号不匹配就丢弃。 */
  let seq = 0;
  /** 已接收的最新序号，防止更早的响应后到覆盖。 */
  let lastAppliedSeq = 0;
  /** 定时器句柄。 */
  let timer = null;
  /** 是否在跑。 */
  let running = false;
  /** 是否已销毁（destroy 之后不再有任何副作用）。 */
  let destroyed = false;
  /** 当前是否有一个请求在途。 */
  let inFlight = false;
  /** 距离下一次可以发请求的最早时刻。 */
  let nextAllowedAt = 0;
  /** 连续失败次数。 */
  let consecutiveFailures = 0;
  /** 最近一次错误。 */
  let lastError = null;
  /** 是否有请求正在飞（用于 stop 时不重排）。 */
  let lastResult = null;

  /** 视图可读的状态快照。 */
  const state = {
    running: false,
    inFlight: false,
    consecutiveFailures: 0,
    lastError: null,
    paused: false,
  };

  /**
   * 同步状态快照并通知外部。
   */
  function pushState() {
    state.running = running;
    state.inFlight = inFlight;
    state.consecutiveFailures = consecutiveFailures;
    state.lastError = lastError;
    state.paused = running && !isVisible();
    if (onStateChange) {
      try {
        onStateChange({ ...state });
      } catch {
        /* 状态回调出错不影响轮询本身 */
      }
    }
  }

  /**
   * 页面是否可见。
   * @returns {boolean}
   */
  function isVisible() {
    return typeof document === 'undefined' || document.visibilityState !== 'hidden';
  }

  /**
   * 退避时长。
   * @returns {number}
   */
  function backoffDelay() {
    const idx = Math.min(consecutiveFailures - 1, BACKOFF_STEPS.length - 1);
    return BACKOFF_STEPS[Math.max(0, idx)];
  }

  /**
   * 清掉定时器。
   */
  function clearTimer() {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  }

  /**
   * 排下一次执行。
   * @param {number} [delay] 指定延时；不给则按 interval / 退避计算
   */
  function schedule(delay) {
    clearTimer();
    if (destroyed || !running) return;
    const wait =
      delay === undefined
        ? consecutiveFailures > 0
          ? backoffDelay()
          : Math.max(interval, nextAllowedAt - Date.now(), 0)
        : delay;
    timer = window.setTimeout(() => {
      timer = null;
      tick();
    }, Math.max(0, wait));
  }

  /**
   * 判断当前是否应该发请求。
   * @returns {Promise<boolean>}
   */
  async function shouldPoll() {
    if (destroyed || !running) return false;
    if (!isVisible()) return false;
    if (inFlight) return false;
    if (Date.now() < nextAllowedAt) return false;
    try {
      const ok = await enabled();
      return ok !== false;
    } catch {
      return false;
    }
  }

  /**
   * 一轮执行。
   */
  async function tick() {
    if (destroyed || !running) return;
    if (inFlight) {
      // 上一次还没回来：不叠加请求，等它结束后由 finish 统一重排
      return;
    }
    const ok = await shouldPoll();
    if (destroyed || !running) return;
    if (!ok) {
      // 空闲：停表，等 enabled 再次为真或外部 triggerNow
      pushState();
      return;
    }

    const mySeq = (seq += 1);
    inFlight = true;
    pushState();

    try {
      const result = await fn({ seq: mySeq });
      // 请求序号守卫：过期响应直接丢弃，绝不覆盖更新的状态
      if (destroyed || mySeq <= lastAppliedSeq) {
        inFlight = false;
        schedule(0);
        return;
      }
      lastAppliedSeq = mySeq;
      inFlight = false;
      consecutiveFailures = 0;
      lastError = null;
      lastResult = result;
      nextAllowedAt = Date.now() + MIN_INTERVAL;
      pushState();
      schedule();
    } catch (err) {
      inFlight = false;
      // 被取消不算失败：不退避、不播报
      if (err instanceof ApiError && err.code === 'ABORTED') {
        pushState();
        return;
      }
      if (destroyed) return;
      // 同样用序号守卫：过期的失败不改变退避计数
      if (mySeq <= lastAppliedSeq) {
        schedule(0);
        return;
      }
      lastAppliedSeq = mySeq;
      consecutiveFailures += 1;
      lastError = err;
      pushState();
      if (onError) {
        try {
          onError(err, consecutiveFailures);
        } catch {
          /* 错误回调出错不影响退避逻辑 */
        }
      }
      schedule(backoffDelay());
    }
  }

  /**
   * 页面可见性变化：隐藏时暂停，可见时立即拉一次。
   */
  function onVisibilityChange() {
    if (destroyed) return;
    if (document.visibilityState === 'hidden') {
      clearTimer();
      pushState();
      return;
    }
    // 恢复可见：立即拉一次（§13.6），不等退避
    consecutiveFailures = 0;
    lastError = null;
    nextAllowedAt = 0;
    pushState();
    tick();
  }

  let visibilityBound = false;
  function bindVisibility() {
    if (visibilityBound || typeof document === 'undefined') return;
    document.addEventListener('visibilitychange', onVisibilityChange);
    visibilityBound = true;
  }
  function unbindVisibility() {
    if (!visibilityBound || typeof document === 'undefined') return;
    document.removeEventListener('visibilitychange', onVisibilityChange);
    visibilityBound = false;
  }

  return {
    /**
     * 开始轮询。
     */
    start() {
      if (destroyed) return;
      if (running) {
        // 已在轮询中（空闲停表后 enabled 重新为真）：重置退避并立即补一次，
        // 否则 start 被 running 守卫吞掉，第二次「运行校验」永远不会轮询。
        consecutiveFailures = 0;
        lastError = null;
        nextAllowedAt = 0;
        tick();
        return;
      }
      running = true;
      consecutiveFailures = 0;
      lastError = null;
      nextAllowedAt = 0;
      bindVisibility();
      pushState();
      tick();
    },

    /**
     * 停止轮询并清定时器（不销毁，enabled 变化后可再 start）。
     */
    stop() {
      running = false;
      clearTimer();
      pushState();
    },

    /**
     * 立即拉一次（忽略 interval 与退避），用于用户手动刷新。
     */
    triggerNow() {
      if (destroyed || !running) return;
      clearTimer();
      consecutiveFailures = 0;
      lastError = null;
      nextAllowedAt = 0;
      tick();
    },

    /**
     * 切换 enabled 谓词：空闲→进行中时立即补一次。
     * @param {() => boolean} next
     */
    setEnabled(next) {
      enabled = typeof next === 'function' ? next : () => false;
      if (!running) return;
      tick();
    },

    /**
     * 状态快照。
     * @returns {object}
     */
    getState() {
      return { ...state, lastResult };
    },

    /**
     * 彻底销毁：停表、解绑 visibilitychange（§10.4 destroy 纪律）。
     */
    destroy() {
      destroyed = true;
      running = false;
      clearTimer();
      unbindVisibility();
      inFlight = false;
      lastResult = null;
    },
  };
}
