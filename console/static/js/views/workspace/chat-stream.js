/**
 * chat-stream.js — 工作台对话流（2026-10-02 二次优化：工具调用平铺紧凑行）
 *
 * 对话流是工作台的页面主轴，所有条目平铺、按时间顺序混排在同一列里（参考主流
 * agent 聊天界面，不再按「工具轮 n」分组套两层）：
 *   用户消息 → 「◐ 思考 · 持续约 n 秒」折叠行 → 模型正文 → 工具调用紧凑行
 *   （一行一条：工具图标 + 中文名 + 关键参数内联预览，默认折叠，点开看完整
 *   入参/返回）→ 收尾总结（内联，不 sticky）。
 * 「任务与提示词」节点在 task-node.js；校验结果条在 report-node.js（完整报告
 * 在 report-modal.js 独立窗口）；输入区（composer）由编排层挂在底部操作栏下方，
 * 本模块只管它的行为。
 *
 * 工具调用展示纪律（任务书）：
 *   - 每次调用一行：左侧小图标 + 工具中文名（read_file→读文件 等）+ 关键参数
 *     内联预览（命令一行截断、文件路径取 basename）；
 *   - 失败的行尾带「失败」标记：图标 + 文字 + 颜色三重编码，不靠颜色单传（§12）；
 *   - 默认折叠，点击展开看完整入参与返回（超长截断，完整数据在该轮运行目录的
 *     chat.jsonl）。
 *
 * 对话由服务端代理当前运行绑定的模型档案；前端不保存或接触 API 密钥。
 *
 * 依赖：core/*、components/*
 * 导出：createChatStream(handlers) → { el, composerEl, update, setDraft, sendText,
 *         focusComposer, destroy }
 */

import { el, clear, on, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { api, ApiError, errorBody, errorTitle } from '../../core/api.js';
import { createButton } from '../../components/button.js';
import { createEmptyState } from '../../components/empty-state.js';
import { showToast } from '../../components/toast.js';

/** 改版新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  DISABLED_NO_SANDBOX: '还没有沙箱，先在底部操作栏点「准备沙箱」。',
  DISABLED_STATUS: '这一轮还不能对话：沙箱没就绪或已经收束，等状态变成「就绪」。',
  STATUS_GRADED: '本轮已校验，仍可追问让模型接着改',
  // 空态：为什么空 + 下一步做什么（§13.1），每个只带一个动作
  EMPTY_NO_RUN_TITLE: '这一轮还没有开始',
  EMPTY_NO_RUN_DESC: '在底部操作栏点「准备沙箱」，模型才有一个只属于它自己的工作目录可改。',
  EMPTY_NO_MESSAGES_TITLE: '还没有消息',
  EMPTY_NO_MESSAGES_DESC: '把第 1 级提示词发给模型，它就会动手改沙箱；没动手时校验只会按「未改动」判 0 分。',
  EMPTY_SEND_PROMPT: '发送当前提示词',
  EMPTY_GONE_TITLE: '这一轮的档案已被删除',
  EMPTY_GONE_DESC: '历史可以回看，但这条档案已经发不出去。在顶部状态栏改选一个现存档案，再重开一轮；'
    + '旧记录的成绩可以在「更多操作」里用「继续对话（本轮分数作废）」摘掉。',
  EMPTY_RESTART: '用现存档案重开一轮',
  EMPTY_RESTART_NEEDS_PICK: '先在顶部状态栏选一个现存档案',
  // 思考折叠行：「◐ 思考 · 持续约 n 秒」，时长估不出来就「· n 字」
  REASONING_LINE: '思考 · {meta}',
  REASONING_SECONDS: '持续约 {n} 秒',
  REASONING_MINUTES: '持续约 {n} 分钟',
  REASONING_CHARS: '{n} 字',
  // 工具调用紧凑行
  TOOL_IN: '入参',
  TOOL_OUT: '返回',
  TOOL_TRUNCATED: '……（界面只显示前 {n} 字，完整内容在这一轮运行目录的 chat.jsonl）',
  TOOL_FAILED: '✕ 失败',
  TOOL_PENDING: '等待返回…',
  TOOL_ORPHAN: '工具返回',
};

/** 工具返回/入参在界面上的最大展示字符数；完整数据在该轮运行目录的 chat.jsonl。 */
const TOOL_TEXT_CAP = 8000;

const ROLE_LABELS = {
  user: '你',
  assistant: '模型',
  tool: '工具',
  system: '系统',
};

/**
 * 工具名 → 中文名 + 左侧小图标（图形符号，不是 emoji）。
 * 没登记的按原名显示，图标退化为通用齿轮。
 */
const TOOL_META = {
  read_file: { label: '读文件', icon: '¶' },
  write_file: { label: '写文件', icon: '✎' },
  run_command: { label: '终端', icon: '❯' },
  list_files: { label: '列目录', icon: '☰' },
};
const TOOL_META_FALLBACK = { label: '', icon: '⚙' };

/** 用户展开过的思考行 / 工具调用行（按消息 id 记住，轮询重渲染时不塌回去）。 */
const expandedNodes = new Set();

/** 已经收束、不再接收新消息的运行状态。 */
const CLOSED_STATUS = new Set(['cancelled', 'error']);

/**
 * 可以继续对话的服务端状态：已校验（graded）的这一轮仍然要能追问模型、让它接着改沙箱。
 * 原先只认 'ready'，跑完一次校验后输入框和发送按钮就永久变灰且不给原因。
 */
const CHAT_OK = new Set(['ready', 'graded']);

/**
 * 从工具调用参数里挑「关键参数」做内联预览：
 * 命令类取命令第一行（一行放不下由 CSS 截断）；路径类取 basename；
 * 都没有就取第一个字符串值（长得像路径的照样取 basename）。
 * @param {string} toolName 工具名（决定偏好哪类参数）
 * @param {string} rawArguments JSON 字符串
 * @returns {string}
 */
function keyParamOf(toolName, rawArguments) {
  const raw = String(rawArguments || '');
  if (!raw) return '';
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      const command = ['command', 'cmd'].map((key) => parsed[key]).find((v) => typeof v === 'string' && v);
      if (command) return String(command).split('\n')[0];
      const path = ['path', 'file'].map((key) => parsed[key]).find((v) => typeof v === 'string' && v);
      if (path) return baseName(path);
      const query = ['query', 'pattern'].map((key) => parsed[key]).find((v) => typeof v === 'string' && v);
      if (query) return query;
      for (const value of Object.values(parsed)) {
        if (typeof value === 'string' && value) {
          return /[\\/]/.test(value) ? baseName(value) : value.length > 80 ? `${value.slice(0, 80)}…` : value;
        }
      }
    }
  } catch {
    /* 参数不是 JSON：下面直接截原文 */
  }
  void toolName;
  return raw.length > 80 ? `${raw.slice(0, 80)}…` : raw;
}

/**
 * 路径取 basename：内联预览只要认出「哪个文件」，目录链太占行宽。
 * @param {string} path
 * @returns {string}
 */
function baseName(path) {
  const clean = String(path || '').replace(/[\\/]+$/, '');
  const segments = clean.split(/[\\/]/).filter(Boolean);
  return segments.length ? segments[segments.length - 1] : clean;
}

/** 截断过长的入参/返回正文，并标注完整内容在哪。 */
function capToolText(text) {
  const raw = String(text || '');
  if (raw.length <= TOOL_TEXT_CAP) return raw;
  return `${raw.slice(0, TOOL_TEXT_CAP)}\n${t(T.TOOL_TRUNCATED, { n: TOOL_TEXT_CAP })}`;
}

/**
 * 创建对话流。
 * @param {{
 *   scope?: object,
 *   onPrepare?: Function,
 *   onSendPrompt?: Function,
 *   onRestartWithModel?: (preferredId: string) => void,
 * }} [handlers]
 */
export function createChatStream(handlers = {}) {
  const ownsScope = !handlers.scope;
  const scope = handlers.scope || api.scope();
  let currentRunId = '';
  let currentRun = null;
  let messages = [];
  let loading = false;
  let sending = false;
  /** 服务端还有一轮发送在跑（浏览器刷新/离开后线程不会断），此时对话显示“模型仍在处理”。 */
  let remoteBusy = false;
  /** 这一轮绑定的模型档案已被删除：历史可以回看，但不能再发。 */
  let profileGone = false;
  let requestSeq = 0;
  let progressTimer = null;
  let remoteTimer = null;
  /** 状态栏档案下拉当前值（空态「重开一轮」按钮的可用性要看它）。 */
  let pickedModelId = '';

  // ==================== 消息流 ====================
  const statusText = el('span', { class: 'u-faint', role: 'status', 'aria-live': 'polite' });
  const messageList = el('div', {
    class: 'ws-stream__msgs',
    role: 'log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
    tabindex: '0',
  });
  const emptyPrepareBtn = createButton({
    label: S.SANDBOX_PREPARE,
    variant: 'ghost',
    size: 'sm',
    onClick: () => handlers.onPrepare && handlers.onPrepare(),
  });
  const emptyPromptBtn = createButton({
    label: T.EMPTY_SEND_PROMPT,
    variant: 'ghost',
    size: 'sm',
    onClick: () => handlers.onSendPrompt && handlers.onSendPrompt(),
  });
  const emptyRestartBtn = createButton({
    label: T.EMPTY_RESTART,
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      // 状态栏下拉里当前显示的档案一起带过去：档案被删的 run 打开时编排层 store 里
      // 是空的，只读 store 会让人「明明看到了一个现存档案，点了却说没选」。
      if (handlers.onRestartWithModel) handlers.onRestartWithModel(pickedModelId);
    },
  });
  const chatEmpty = createEmptyState({ icon: 'inbox', title: T.EMPTY_NO_RUN_TITLE, desc: T.EMPTY_NO_RUN_DESC });
  const errorMessage = el('p', { class: 'ws-stream__error', role: 'alert', hidden: true });

  const streamRoot = el(
    'section',
    { class: 'ws-stream ws-region', id: 'ws-region-chat', 'aria-label': S.CHAT_TITLE || '内置对话' },
    chatEmpty.el,
    errorMessage,
    messageList,
  );

  // ==================== 输入区（挂到页面底部，由编排层放置） ====================
  const draft = el('textarea', {
    id: 'workspace-chat-message',
    class: 'ws-composer__input',
    rows: 4,
    name: 'workspace-chat-message',
    placeholder: S.CHAT_INPUT_PLACEHOLDER || '输入消息，让模型继续处理当前沙箱',
    'aria-label': S.CHAT_INPUT_LABEL || '发送给模型的消息',
    disabled: true,
  });
  const usePromptBtn = createButton({
    label: S.CHAT_USE_PROMPT || '填入当前提示词',
    variant: 'ghost',
    size: 'sm',
    onClick: () => handlers.onFillPrompt && handlers.onFillPrompt(),
  });
  const sendBtn = createButton({
    label: S.CHAT_SEND || '发送',
    // 全页唯一实心主按钮在底部操作栏；对话的发送钮保持默认态，不与它抢强调位
    variant: 'default',
    onClick: () => send(),
  });
  const composerHint = el('span', { class: 'u-faint ws-composer__hint' }, S.CHAT_TOOL_HINT || '模型可在当前沙箱内读写文件并运行检查。');
  const composer = el(
    'form',
    { class: 'ws-composer', onSubmit: (event) => { event.preventDefault(); send(); } },
    el('label', { class: 'visually-hidden', for: 'workspace-chat-message' }, S.CHAT_INPUT_LABEL || '发送给模型的消息'),
    draft,
    el('div', { class: 'ws-composer__foot' }, statusText, composerHint, el('span', { class: 'u-spacer' }), usePromptBtn.el, sendBtn.el),
  );

  // ==================== 消息归一化 ====================
  function normalizeMessages(next) {
    if (!Array.isArray(next)) return [];
    return next
      .filter((item) => item && typeof item === 'object')
      .map((item, index) => ({
        id: String(item.id || `${item.role || 'message'}-${index}`),
        role: String(item.role || 'assistant'),
        content: String(item.content || item.text || ''),
        reasoning: [item.reasoning_content, item.reasoning].find((value) => typeof value === 'string' && value) || '',
        name: String(item.name || item.tool || ''),
        toolCalls: normalizeToolCalls(item.tool_calls || item.toolCalls),
        toolCallId: String(item.tool_call_id || item.toolCallId || ''),
        status: String(item.status || ''),
        errorCode: String(item.error_code || item.errorCode || ''),
        created_at: item.created_at || '',
      }));
  }

  function normalizeToolCalls(value) {
    if (!Array.isArray(value)) return [];
    return value
      .filter((call) => call && typeof call === 'object')
      .map((call) => {
        const fn = call.function && typeof call.function === 'object' ? call.function : call;
        return {
          id: String(call.id || ''),
          name: String(fn.name || call.name || '未知工具'),
          arguments: String(fn.arguments || call.arguments || ''),
        };
      });
  }

  function formatError(err) {
    const code = err instanceof ApiError ? err.code : 'INTERNAL';
    const title = errorTitle(code);
    if (!(err instanceof ApiError)) return `${title}：${String(err || errorBody(code))}`;
    const backendMessage = String(err.message || '').trim();
    const rawDetail = err.body && typeof err.body.detail === 'string' ? err.body.detail.trim() : '';
    const detail = rawDetail && rawDetail !== backendMessage ? rawDetail.slice(0, 2000) : '';
    const parts = [];
    if (backendMessage && backendMessage !== title) parts.push(backendMessage);
    if (detail) parts.push(`${S.ERROR_DETAIL_LABEL || '技术细节'}：${detail}`);
    if (!parts.length) parts.push(errorBody(code));
    return `${title}：${parts.join('；')}`;
  }

  function messageKey(item) {
    return `${item.id}|${item.role}|${item.content}|${item.reasoning}|${item.name}|${item.toolCallId}|${item.status}|${JSON.stringify(item.toolCalls || [])}`;
  }

  function mergeMessages(next) {
    const merged = [];
    const seen = new Set();
    [...messages, ...normalizeMessages(next)].forEach((item) => {
      const key = messageKey(item);
      if (seen.has(key)) return;
      seen.add(key);
      merged.push(item);
    });
    messages = merged;
  }

  /**
   * 该不该强调收尾总结：只有当对话**确实以这条结尾**时才强调。
   * 一旦后面又出现新消息（进入下一轮、模型又在调工具、报错行），它就是历史，
   * 继续强调会抢正在发生的事的视觉权重。
   * @param {Array} list
   * @returns {number}
   */
  function finalSummaryIndex(list) {
    const last = list[list.length - 1];
    if (!last || last.role !== 'assistant' || last.status || last.toolCalls.length) return -1;
    return String(last.content || '').trim() ? list.length - 1 : -1;
  }

  /**
   * 空态：说清「为什么空」并只给一个下一步动作。
   */
  function renderEmpty() {
    const show = !currentRunId || !messages.length;
    chatEmpty.el.hidden = !show;
    if (!show) return;
    if (profileGone) {
      // 死档案的 run 打开时下拉是空的（选项里没有它），不先选就点不动——
      // 那就把按钮标成禁用并说清缺什么，而不是让人点一下只弹一句「先选一个」。
      const picked = String(pickedModelId || '');
      emptyRestartBtn.update({ disabled: !picked, reason: picked ? '' : T.EMPTY_RESTART_NEEDS_PICK });
      chatEmpty.update({
        icon: 'alert',
        title: T.EMPTY_GONE_TITLE,
        desc: T.EMPTY_GONE_DESC,
        actions: [emptyRestartBtn.el],
      });
      return;
    }
    if (currentRunId) {
      chatEmpty.update({
        icon: 'clock',
        title: T.EMPTY_NO_MESSAGES_TITLE,
        desc: T.EMPTY_NO_MESSAGES_DESC,
        actions: [emptyPromptBtn.el],
      });
      return;
    }
    chatEmpty.update({
      icon: 'inbox',
      title: T.EMPTY_NO_RUN_TITLE,
      desc: T.EMPTY_NO_RUN_DESC,
      actions: [emptyPrepareBtn.el],
    });
  }

  /** 页面是否已经接近底部：接近时新内容到了才自动跟滚，翻历史时不拽回去。 */
  function nearBottom() {
    const container = streamRoot.closest('.app-main') || document.scrollingElement;
    if (!container) return true;
    return container.scrollHeight - container.scrollTop - container.clientHeight < 200;
  }

  /** 把对话流末尾滚进视野（发送成功 / 新内容到达且本来就在底部时）。 */
  function scrollToEnd() {
    window.requestAnimationFrame(() => {
      const last = messageList.lastElementChild;
      if (last && typeof last.scrollIntoView === 'function') {
        last.scrollIntoView({ block: 'end', behavior: 'auto' });
      }
    });
  }

  function renderMessages({ follow = false } = {}) {
    const wasNearBottom = nearBottom();
    clear(messageList);
    if (!messages.length) {
      messageList.hidden = true;
      renderEmpty();
      return;
    }
    chatEmpty.el.hidden = true;
    messageList.hidden = false;
    const summaryIndex = finalSummaryIndex(messages);
    for (let index = 0; index < messages.length; index += 1) {
      const message = messages[index];
      if (message.role === 'tool') {
        // 落单的工具返回（前面没有带 tool_calls 的助手消息）也要看得见
        messageList.appendChild(toolLineNode(null, message, `orphan:${message.id}`));
        continue;
      }
      if (message.role === 'assistant') {
        const previous = index > 0 ? messages[index - 1] : null;
        const thinking = reasoningRow(message, previous);
        if (thinking) messageList.appendChild(thinking);
        // 带工具调用的助手消息常带一句正文（「我先看一下文件」）：平铺后它不再是
        // 哪张卡的标题，该作为普通模型正文出现在调用行之前。
        if (String(message.content || '').trim()) {
          messageList.appendChild(textMessageNode(message, index === summaryIndex));
        }
        if (message.toolCalls.length) {
          // 紧跟其后的 tool 消息是这批调用的返回：优先按 tool_call_id 对号，
          // 对不上再按顺序兜底（历史记录里没有 tool_call_id 的老数据）。
          const results = [];
          let cursor = index + 1;
          while (cursor < messages.length && messages[cursor].role === 'tool') {
            results.push(messages[cursor]);
            cursor += 1;
          }
          index = cursor - 1;
          const used = new Set();
          message.toolCalls.forEach((call, position) => {
            let hit = -1;
            if (call.id) {
              hit = results.findIndex((r, ri) => !used.has(ri) && r.toolCallId && r.toolCallId === call.id);
            }
            if (hit < 0) hit = results.findIndex((r, ri) => !used.has(ri));
            const result = hit >= 0 ? results[hit] : null;
            if (hit >= 0) used.add(hit);
            messageList.appendChild(toolLineNode(call, result, `call:${message.id}:${call.id || position}`));
          });
          // 多出来的落单返回（没有对应调用记录）也要看得见
          results.forEach((extra, extraIndex) => {
            if (!used.has(extraIndex)) {
              messageList.appendChild(toolLineNode(null, extra, `call:${message.id}:extra-${extraIndex}`));
            }
          });
        }
        continue;
      }
      messageList.appendChild(textMessageNode(message, index === summaryIndex));
    }
    if (follow || wasNearBottom) scrollToEnd();
  }

  /**
   * 思考时长的估算：本消息与前一条消息的落盘时间差（chat.jsonl 每条都带
   * created_at）。差值不可用（缺时间戳 / 太小 / 大得像隔了一次会话）就回退到字数。
   * @param {object} message
   * @param {object|null} previous
   * @returns {string}
   */
  function reasoningMeta(message, previous) {
    const end = Date.parse(String(message.created_at || ''));
    const start = previous ? Date.parse(String(previous.created_at || '')) : NaN;
    const seconds = Number.isFinite(end) && Number.isFinite(start) ? Math.round((end - start) / 1000) : 0;
    if (seconds >= 2 && seconds <= 3600) {
      return seconds >= 120
        ? t(T.REASONING_MINUTES, { n: Math.round(seconds / 60) })
        : t(T.REASONING_SECONDS, { n: seconds });
    }
    return t(T.REASONING_CHARS, { n: message.reasoning.length });
  }

  /**
   * 模型思考：一行「◐ 思考 · 持续约 n 秒」（估不出时长就「· n 字」），默认折叠，
   * 点开读全文。展开状态按消息 id 记住，轮询重渲染不塌回去。
   */
  function reasoningRow(message, previous) {
    if (!message.reasoning) return null;
    const nodeId = `reasoning:${message.id}`;
    return el('details', {
      class: 'ws-reasoning',
      open: expandedNodes.has(nodeId),
      onToggle: (event) => {
        if (event.target.open) expandedNodes.add(nodeId);
        else expandedNodes.delete(nodeId);
      },
    },
      el('summary', { class: 'ws-reasoning__summary' },
        el('span', { class: 'ws-reasoning__glyph', 'aria-hidden': 'true' }, '◐'),
        el('span', {}, t(T.REASONING_LINE, { meta: reasoningMeta(message, previous) }))),
      el('div', { class: 'ws-stream__text ws-reasoning__content' }, message.reasoning));
  }

  /** 普通文本消息（用户提问、模型正文、错误提示）。 */
  function textMessageNode(message, isFinal) {
    const role = ROLE_LABELS[message.role] || message.role;
    const kind = `ws-msg--${message.role}`;
    return el('article', { class: `ws-msg ${kind}${isFinal ? ' ws-msg--final' : ''}` },
      el('div', { class: 'ws-msg__meta' },
        isFinal ? S.CHAT_FINAL_SUMMARY : (message.name ? `${role} · ${message.name}` : role)),
      el('div', { class: 'ws-stream__text' }, message.content || '—'),
      message.status ? el('div', { class: 'ws-msg__status' }, message.status) : null,
    );
  }

  /** 工具返回把失败放进 {"error": ...}；按此给每次调用标成功/失败。 */
  function toolResultFailed(message) {
    if (!message) return false;
    try {
      const value = JSON.parse(String(message.content || ''));
      return Boolean(value && typeof value === 'object' && value.error);
    } catch {
      return false;
    }
  }

  /**
   * 一次工具调用 = 对话流里的一行紧凑条目（平铺，不再套「工具轮」分组卡）：
   * 小图标 + 工具中文名 + 关键参数内联预览，失败的行尾带「✕ 失败」（图标 + 文字 +
   * 颜色三重编码）。默认折叠，点开看完整入参与返回（超长截断，完整数据在该轮
   * 运行目录的 chat.jsonl）。
   * @param {object|null} call 调用（落单的工具返回时为 null）
   * @param {object|null} result 对应的工具返回消息
   * @param {string} nodeId 展开状态记忆键
   */
  function toolLineNode(call, result, nodeId) {
    const rawName = call ? call.name : (result ? result.name : '');
    const meta = TOOL_META[rawName] || { ...TOOL_META_FALLBACK, label: rawName || T.TOOL_ORPHAN };
    const failed = toolResultFailed(result);
    // 调用已发出、返回还没落盘（发送轮询中途）：行尾给一个等待标记而不是假装成功
    const pending = Boolean(call) && !result;
    const param = call ? keyParamOf(call.name, call.arguments) : '';
    const body = el('div', { class: 'ws-toolline__body' });
    if (call) {
      body.appendChild(el('h4', { class: 'ws-toolline__heading' }, T.TOOL_IN));
      body.appendChild(el('pre', { class: 'ws-toolline__pre', tabindex: '0' }, capToolText(call.arguments || '—')));
    }
    if (result) {
      body.appendChild(el('h4', { class: 'ws-toolline__heading' }, T.TOOL_OUT));
      body.appendChild(el('pre', { class: 'ws-toolline__pre', tabindex: '0' }, capToolText(result.content || '—')));
    }
    if (!call && !result) {
      body.appendChild(el('p', { class: 'u-faint' }, S.CHAT_TOOL_CALL_EMPTY));
    }
    return el('details', {
      class: `ws-toolline${failed ? ' ws-toolline--fail' : ''}`,
      open: expandedNodes.has(nodeId),
      onToggle: (event) => {
        if (event.target.open) expandedNodes.add(nodeId);
        else expandedNodes.delete(nodeId);
      },
    },
      el('summary', { class: 'ws-toolline__summary' },
        el('span', { class: 'ws-toolline__icon', 'aria-hidden': 'true' }, meta.icon),
        el('span', { class: 'ws-toolline__name' }, meta.label),
        param ? el('span', { class: 'ws-toolline__param u-mono' }, param) : null,
        el('span', { class: 'u-spacer' }),
        failed ? el('span', { class: 'ws-toolline__fail' }, T.TOOL_FAILED) : null,
        pending ? el('span', { class: 'ws-toolline__pending' }, T.TOOL_PENDING) : null),
      body,
    );
  }

  // ==================== 状态与可用性 ====================
  function setStatus(text) {
    setText(statusText, text || '');
    statusText.hidden = !text;
  }

  /**
   * 状态行说清「输入框为什么锁着」。
   * 输入框按 run 状态与远端忙碌锁定，状态行却一律写「对话就绪」时，
   * 人只能靠猜——已交卷的那一轮就是这么被当成卡住的。
   */
  function syncStatus() {
    if (sending) {
      setStatus(S.CHAT_SENDING || '模型处理中');
      return;
    }
    if (remoteBusy) {
      setStatus(S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
      return;
    }
    if (profileGone) {
      setStatus(S.CHAT_MODEL_GONE);
      return;
    }
    const status = currentRun && currentRun.status ? String(currentRun.status) : '';
    if (status === 'graded') {
      setStatus(T.STATUS_GRADED);
      return;
    }
    if (status && !CHAT_OK.has(status)) {
      setStatus(CLOSED_STATUS.has(status) ? S.CHAT_STATUS_CLOSED : S.CHAT_STATUS_NOT_READY);
      return;
    }
    setStatus('');
  }

  /**
   * 输入框 / 发送按钮的可用性与「不可用的原因」。
   *
   * 可用 = 有运行记录 + 沙箱状态在 CHAT_OK（ready / graded）+ 这一轮绑定的模型档案
   * 还在 + 没有请求在途。不可用时必须把原因写在按钮旁边（createButton 的 reason 会
   * 渲染成可见文字并挂 aria-describedby），否则使用者只会读成「对话框坏了」。
   */
  function setEnabled(enabled) {
    const status = currentRun && currentRun.status ? String(currentRun.status) : '';
    const sandboxOk = CHAT_OK.has(status);
    const editable = Boolean(enabled) && Boolean(currentRunId) && sandboxOk && !remoteBusy && !sending && !profileGone;
    draft.disabled = !editable;
    const reason = !currentRunId
      ? T.DISABLED_NO_SANDBOX
      : profileGone
        ? S.CHAT_MODEL_GONE
        : remoteBusy
          ? S.CHAT_REMOTE_BUSY
          : !sandboxOk
            ? T.DISABLED_STATUS
            : '';
    // 发送另加一条：草稿为空时不给点（send() 内部本来也会挡住空消息）
    const canSend = editable && Boolean(draft.value.trim());
    sendBtn.update({
      disabled: !canSend,
      loading: sending,
      busyLabel: S.CHAT_SENDING,
      reason,
    });
    usePromptBtn.update({ disabled: !editable });
  }

  /**
   * 服务端仍有发送线程在跑（比如浏览器中途刷新过）：
   * 轮询对话记录直到它收束，期间消息照常落位、输入框保持不可用。
   */
  function watchRemoteSend(runId, seq) {
    clearTimeout(remoteTimer);
    async function tick() {
      if (runId !== currentRunId || seq !== requestSeq) return;
      try {
        const data = await api.get(`/runs/${encodeURIComponent(runId)}/chat`, { scope });
        if (runId !== currentRunId || seq !== requestSeq) return;
        if (Array.isArray(data?.messages)) {
          const next = normalizeMessages(data.messages);
          if (next.map(messageKey).join('\n') !== messages.map(messageKey).join('\n')) {
            messages = next;
            renderMessages();
          }
        }
        if (data?.chat_busy) {
          remoteTimer = setTimeout(tick, 1500);
          return;
        }
      } catch { /* 网络抖动就下一轮再试 */ }
      remoteBusy = false;
      errorMessage.hidden = true;
      setText(errorMessage, '');
      syncStatus();
      setEnabled(true);
      renderMessages();
    }
    remoteTimer = setTimeout(tick, 1500);
  }

  async function loadHistory(runId) {
    const seq = ++requestSeq;
    loading = true;
    setStatus(S.CHAT_LOADING || '正在加载对话');
    setText(errorMessage, '');
    errorMessage.hidden = true;
    renderMessages();
    try {
      const data = await api.get(`/runs/${encodeURIComponent(runId)}/chat`, { scope });
      if (seq !== requestSeq || runId !== currentRunId) return;
      messages = normalizeMessages(data && data.messages);
      loading = false;
      remoteBusy = Boolean(data && data.chat_busy);
      profileGone = Boolean(data && data.model && data.model.gone);
      // 把"为什么不能发"常驻写在输入框下面，而不是一闪而过的 toast
      setText(composerHint, profileGone ? S.CHAT_MODEL_GONE : S.CHAT_TOOL_HINT);
      if (remoteBusy) {
        setStatus(S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
        watchRemoteSend(runId, seq);
      } else {
        syncStatus();
      }
      renderMessages();
    } catch (err) {
      if (seq !== requestSeq || runId !== currentRunId) return;
      loading = false;
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      setText(errorMessage, formatError(err));
      errorMessage.hidden = false;
      setStatus(S.CHAT_STATUS_ERROR || '对话不可用');
      renderMessages();
    } finally {
      if (seq === requestSeq) setEnabled(Boolean(currentRunId) && !loading);
    }
  }

  async function send() {
    const text = draft.value.trim();
    if (!currentRunId || !text || loading) return false;
    if (profileGone) {
      showToast({ message: S.CHAT_MODEL_GONE, detail: S.CHAT_MODEL_GONE_DETAIL, kind: 'warn', duration: 8000 });
      return false;
    }
    if (sending || remoteBusy) {
      // 上一条还在服务端跑（一轮可能几十次工具调用）。必须当场说明并留住草稿：
      // 只在界面上留一个气泡、消息永远发不出去，看起来就像对话死了。
      setStatus(S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
      showToast({
        message: S.CHAT_SEND_BLOCKED,
        detail: S.CHAT_SEND_BLOCKED_DESC,
        kind: 'warn',
        duration: 8000,
      });
      return false;
    }
    const runId = currentRunId;
    const seq = requestSeq;
    let finished = false;
    setText(errorMessage, '');
    errorMessage.hidden = true;
    mergeMessages([{ id: `local-${Date.now()}`, role: 'user', content: text }]);
    draft.value = '';
    sending = true;
    setStatus(S.CHAT_SENDING || '模型处理中');
    renderMessages({ follow: true });
    setEnabled(true);
    // 长请求期间轮询已落盘的模型/工具消息；当前是逐回合刷新，不是逐 token 流式输出。
    async function refreshProgress() {
      if (finished || seq !== requestSeq) return;
      try {
        const data = await api.get(`/runs/${encodeURIComponent(runId)}/chat`, { scope });
        if (finished || seq !== requestSeq) return;
        // 服务端仍在这一轮里：即使本条请求中途到期，对话也没死，据此锁定输入。
        remoteBusy = Boolean(data?.chat_busy);
        const next = normalizeMessages(data?.messages);
        if (next.length && next.map(messageKey).join('\n') !== messages.map(messageKey).join('\n')) {
          messages = next;
          renderMessages();
        }
      } catch { /* 这条发送请求仍是错误展示的唯一权威来源 */ }
      if (!finished && seq === requestSeq) progressTimer = setTimeout(refreshProgress, 1200);
    }
    progressTimer = setTimeout(refreshProgress, 1200);
    try {
      const data = await api.post(`/runs/${encodeURIComponent(runId)}/chat`, { message: text }, { scope });
      if (seq !== requestSeq || runId !== currentRunId) return;
      if (data && Array.isArray(data.messages)) messages = normalizeMessages(data.messages);
      else if (data && data.message) mergeMessages([data.message]);
      if (data && data.chat_busy) {
        // 服务端已经收下这条消息、在后台线程里跑完整工具闭环：交回轮询接回结果。
        // 这条路径上没有任何一层需要为模型留超时，所以也不会再出现「请求超时」假错。
        remoteBusy = true;
        setStatus(S.CHAT_SENDING || '模型处理中');
        renderMessages();
        watchRemoteSend(runId, requestSeq);
        return true;
      }
      remoteBusy = false;
      renderMessages({ follow: true });
      return true;
    } catch (err) {
      if (seq !== requestSeq || runId !== currentRunId) return;
      const code = err instanceof ApiError ? err.code : 'INTERNAL';
      if (code === 'TIMEOUT' || code === 'ABORTED') {
        // 这条请求到期或被取消，不等于模型那一轮失败：服务端按契约继续跑完，
        // 所以交回远端轮询，而不是报一个看起来像失败的错。
        remoteBusy = true;
        setText(errorMessage, S.CHAT_DETACHED_HINT);
        errorMessage.hidden = false;
        setStatus(S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
        showToast({
          message: S.CHAT_DETACHED_TITLE,
          detail: S.CHAT_REMOTE_BUSY_DETAIL,
          kind: 'warn',
          duration: 9000,
        });
        renderMessages();
        watchRemoteSend(runId, requestSeq);
        return false;
      }
      const detail = formatError(err);
      setText(errorMessage, detail);
      errorMessage.hidden = false;
      setStatus(S.CHAT_STATUS_ERROR || '对话不可用');
      showToast({ message: errorTitle(code), detail, kind: 'error', duration: 7000 });
      renderMessages();
      return false;
    } finally {
      finished = true;
      if (seq === requestSeq && runId === currentRunId) {
        clearTimeout(progressTimer);
        sending = false;
        // 错误提示还挂着就不要覆盖它；否则按当前 run 状态与远端忙碌重说一遍。
        if (errorMessage.hidden) syncStatus();
        setEnabled(Boolean(currentRunId));
        if (currentRunId) draft.focus();
      }
    }
  }

  function onDraftInput() {
    setEnabled(Boolean(currentRunId));
  }

  function onDraftKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  }

  const offInput = on(draft, 'input', onDraftInput);
  const offKeydown = on(draft, 'keydown', onDraftKeydown);

  // ==================== 对外 ====================
  /**
   * 差异更新。
   * @param {{run?: object|null, pickedModelId?: string}} state
   */
  function update(state = {}) {
    if (state.pickedModelId !== undefined) pickedModelId = String(state.pickedModelId || '');
    if (state.run !== undefined) currentRun = state.run;
    const nextRunId = currentRun && currentRun.run_id ? String(currentRun.run_id) : '';
    if (nextRunId !== currentRunId) {
      clearTimeout(progressTimer);
      clearTimeout(remoteTimer);
      sending = false;
      loading = false;
      remoteBusy = false;
      profileGone = false;
      setText(composerHint, S.CHAT_TOOL_HINT);
      currentRunId = nextRunId;
      messages = [];
      requestSeq += 1;
      draft.value = '';
      setStatus(currentRunId ? (S.CHAT_LOADING || '正在加载对话') : '');
      if (currentRunId) loadHistory(currentRunId);
    }
    const ready = Boolean(currentRunId) && !loading;
    messageList.hidden = !ready || !messages.length;
    composer.hidden = !Boolean(currentRunId);
    errorMessage.hidden = !currentRunId || errorMessage.textContent === '';
    setEnabled(ready);
    renderMessages();
  }

  function setDraft(value) {
    draft.value = String(value || '');
    if (currentRunId) {
      draft.focus();
      setEnabled(Boolean(currentRunId) && !loading);
    }
  }

  /** 由外部直接提交文本（任务节点/空态/底部主按钮的「发送当前提示词」共用）。 */
  function sendText(value) {
    const text = String(value || '').trim();
    if (!text) return Promise.resolve(false);
    draft.value = text;
    setEnabled(Boolean(currentRunId) && !loading);
    return send();
  }

  update({ run: null });

  return {
    el: streamRoot,
    composerEl: composer,
    update,
    setDraft,
    sendText,
    /** 焦点送进输入框（「去对话里追问」等引导用）。 */
    focusComposer() {
      draft.focus();
    },
    destroy() {
      clearTimeout(progressTimer);
      clearTimeout(remoteTimer);
      requestSeq += 1;
      if (ownsScope) scope.cancelAll();
      offInput();
      offKeydown();
      chatEmpty.destroy();
      emptyPrepareBtn.destroy();
      emptyPromptBtn.destroy();
      emptyRestartBtn.destroy();
      usePromptBtn.destroy();
      sendBtn.destroy();
    },
  };
}
