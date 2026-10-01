/**
 * format.js — 中文格式化工具（§10.3）
 *
 * 职责：相对时间、绝对时间、耗时、百分比、千分位数字的中文显示。
 * 依赖：无。
 * 导出：relativeTime, absoluteTime, fullTime, duration, percent, number, clock
 *
 * 纪律（§11.2 #10 / §13.7）：
 *   - 时间一律本地时区，绝不做 UTC 假设。
 *   - 相对时间旁边总有 title 属性的绝对时间（由调用方负责挂 title）。
 */

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;

/**
 * 把任意输入转成毫秒时间戳；无法解析返回 NaN。
 * @param {number|string|Date} value
 * @returns {number}
 */
export function toTime(value) {
  if (value === null || value === undefined || value === '') return NaN;
  if (value instanceof Date) return value.getTime();
  if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
  const parsed = Date.parse(String(value));
  return Number.isNaN(parsed) ? NaN : parsed;
}

/**
 * 相对时间：「刚刚 / 3 分钟前 / 2 天前 / 2025-12-01」（超过 30 天退化为日期）
 * @param {number|string|Date} value 时间戳、ISO 字符串或 Date
 * @param {number} [now] 当前时间（测试可注入）
 * @returns {string}
 */
export function relativeTime(value, now = Date.now()) {
  const ts = toTime(value);
  if (Number.isNaN(ts)) return '时间未知';
  const diff = Math.floor((now - ts) / 1000);
  if (diff < 0) return '刚刚';
  if (diff < 45) return '刚刚';
  if (diff < MINUTE * 2) return '1 分钟前';
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`;
  if (diff < DAY * 2) return `${Math.floor(diff / HOUR)} 小时前`;
  if (diff < DAY * 30) return `${Math.floor(diff / DAY)} 天前`;
  if (diff < WEEK * 5) return `${Math.floor(diff / WEEK)} 周前`;
  return absoluteTime(ts);
}

/**
 * 绝对时间（本地时区）：「2026-09-29 20:15」
 * @param {number|string|Date} value
 * @returns {string}
 */
export function absoluteTime(value) {
  const ts = toTime(value);
  if (Number.isNaN(ts)) return '时间未知';
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/**
 * 精确到秒的绝对时间：「2026-09-29 20:15:32」
 * @param {number|string|Date} value
 * @returns {string}
 */
export function fullTime(value) {
  const ts = toTime(value);
  if (Number.isNaN(ts)) return '时间未知';
  const d = new Date(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return `${absoluteTime(ts)}:${pad(d.getSeconds())}`;
}

/**
 * 耗时：把秒数说成中文短句。
 *   0.8 → 「不足 1 秒」；12 → 「12 秒」；95 → 「1 分 35 秒」；3700 → 「1 小时 1 分」
 * @param {number} seconds
 * @returns {string}
 */
export function duration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return '时间未知';
  if (s < 1) return '不足 1 秒';
  if (s < MINUTE) return `${Math.round(s)} 秒`;
  if (s < HOUR) {
    const m = Math.floor(s / MINUTE);
    const rest = Math.round(s % MINUTE);
    return rest > 0 ? `${m} 分 ${rest} 秒` : `${m} 分`;
  }
  const h = Math.floor(s / HOUR);
  const m = Math.round((s % HOUR) / MINUTE);
  if (m > 0) return `${h} 小时 ${m} 分`;
  return `${h} 小时`;
}

/**
 * 百分比：0.625 → 「62.5%」；整数时省略小数。
 * @param {number} ratio 0~1 之间的小数
 * @param {number} [digits] 小数位数，默认最多 1 位
 * @returns {string}
 */
export function percent(ratio, digits = 1) {
  const n = Number(ratio);
  if (!Number.isFinite(n)) return '—';
  const value = n <= 1 ? n * 100 : n;
  const fixed = value.toFixed(digits);
  const trimmed = fixed.replace(/\.0+$/, '');
  return `${trimmed}%`;
}

/**
 * 数字千分位：12345 → 「12,345」
 * @param {number} value
 * @returns {string}
 */
export function number(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('zh-CN');
}

/**
 * 时间戳 → "HH:MM:SS"，给计时器用（比 duration 更适合每秒刷新）。
 * @param {number} seconds
 * @returns {string}
 */
export function clock(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = (n) => String(n).padStart(2, '0');
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}
