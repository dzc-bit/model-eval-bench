/**
 * chat-panel.js — 工作台内置模型对话。
 *
 * 对话由服务端代理当前运行绑定的模型档案。工具调用按轮折叠成一行摘要，
 * 展开后只列出这一轮调了哪些工具、各几次（参数与返回原文留在 chat.jsonl）；
 * 服务商返回公开思维链就展示，没有则直接突出正文。
 * 前端不保存或接触 API 密钥。
 */

import { el, clear, on, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { api, ApiError, errorBody, errorTitle } from '../../core/api.js';
import { createButton } from '../../components/button.js';
import { createField } from '../../components/field.js';
import { createStatusDot } from '../../components/status-dot.js';
import { showToast } from '../../components/toast.js';

/** 改版新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  // 说明行：说清"这一步你要做什么"（规格 §2.1）
  DESC: '让模型在当前沙箱里改代码。改完去右侧运行校验。',
  EMPTY_ACTION: '去准备沙箱',
  NO_MESSAGES: '还没有消息，发一条开始这一轮对话。',
  // 这些文案在 strings.js 里没有对应键（旧 run-bar 引用的是不存在的键，播报一直为空），
  // strings 本轮冻结，补在本地常量里。
  MODEL_NONE: '还没有模型档案。先到「模型档案」页新增一个，再回来选。',
  MODEL_LOAD_FAILED: '模型档案读取失败：{reason}。可以点「重试」再读一次。',
  DISABLED_NO_SANDBOX: '还没有沙箱，先去沙箱卡准备。',
  DISABLED_STATUS: '沙箱还没就绪，等状态变为「就绪」再继续对话。',
  // 档案已删（异常）：整句只出现一次，就在卡头的档案下拉旁边——那是处理它的地方。
  // 状态点与发送按钮只说"现在不能发"，不重复同一句话（规格 §2.2）。
  GONE_NOTE: '模型档案已删除：这一轮绑定的「{model}」已不存在，历史可以回看。先在下拉里换一个现存档案，再准备一轮。',
  GONE_STATUS: '暂不能发送消息',
  GONE_SEND_REASON: '先换一个现存档案',
  // 本轮实际绑定的模型（与下拉里"下一轮用哪个"是两件事，各说一次）
  RUN_MODEL: '本轮用：{model}',
};

const ROLE_LABELS = {
  user: '你',
  assistant: '模型',
  tool: '工具',
  system: '系统',
};

/** 展开的工具轮（按消息 id 记住，轮询重渲染时不塌回去）。 */
const expandedRounds = new Set();

/** 已经收束、不再接收新消息的运行状态。 */
const CLOSED_STATUS = new Set(['graded', 'cancelled', 'error']);

/**
 * 可以继续对话的服务端状态。
 * 与 sandbox-panel.js 的 SANDBOX_OK / workspace.js 的同名集对齐：
 * 已校验（graded）的这一轮仍然要能追问模型、让它接着改沙箱。
 * 原先只认 'ready'，跑完一次校验后输入框和发送按钮就永久变灰且不给原因。
 */
const CHAT_OK = new Set(['ready', 'graded']);

/**
 * 创建内置对话面板（对话是工作台主栏的视觉主角）。
 * @param {{
 *   scope?: object,
 *   onUsePrompt?: Function,
 *   onModelChange?: (id: string) => void,
 *   onReloadModels?: () => void,
 *   onGoSandbox?: () => void
 * }} [handlers]
 * @returns {{el: HTMLElement, update: Function, setDraft: Function, sendText: Function, destroy: Function}}
 */
export function createChatPanel(handlers = {}) {
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
  /** 模型档案（选择器从「本轮信息」区迁入对话卡头）。 */
  let models = [];
  let modelId = '';
  let modelsError = '';
  let modelsLoading = true;

  const runLabel = el('p', { class: 'u-faint chat__run' });
  // 状态点自带可见文字（"对话就绪 / 正在加载对话"），这份 statusText 只做
  // polite 播报源：视觉上隐藏，避免同一句状态在卡头印两遍（规格 §2.2）。
  const statusText = el('span', { class: 'visually-hidden', role: 'status', 'aria-live': 'polite' });
  const statusDot = createStatusDot({ kind: 'idle', text: S.CHAT_STATUS_IDLE || '未连接' });

  // ---- 模型档案下拉（卡头右侧）：选项/空态原因/重试从 run-bar 原样迁入 ----
  // 这里是「选一个档案开始对话」，不是必填表单字段：不挂必填星号与说明段，
  // 那一套属于表单页；卡头只需要一个紧凑选择器。
  const modelField = createField({
    label: S.RUN_MODEL_LABEL,
    name: 'chat-model',
    type: 'select',
    options: [{ value: '', label: S.RUN_MODEL_EMPTY }],
    onChange: (value) => {
      if (handlers.onModelChange) handlers.onModelChange(value);
    },
  });
  modelField.el.classList.add('chat__model');
  // 档案异常（读取失败 / 已删除）的唯一落点：紧贴选择器，因为出口就在那里。
  const modelNote = el('p', { class: 'u-faint chat__model-note', role: 'status' });
  const modelRetryBtn = createButton({
    label: S.ACTION_RETRY,
    size: 'sm',
    variant: 'ghost',
    onClick: () => {
      if (!handlers.onReloadModels) return;
      // 重读是一次网络请求，按钮自己担一个忙态，免得点完看着没反应又点一次。
      modelRetryBtn.update({ loading: true, busyLabel: S.ACTION_LOADING });
      Promise.resolve(handlers.onReloadModels())
        .catch(() => {})
        .finally(() => modelRetryBtn.update({ loading: false }));
    },
  });
  // createButton 根节点自带 inline-flex，直接 hidden 藏不掉：套一层容器再整体切
  const modelRetryHost = el('div', { class: 'chat__model-retry' }, modelRetryBtn.el);
  modelNote.hidden = true;
  modelRetryHost.hidden = true;
  const messageList = el('div', {
    class: 'chat__messages',
    role: 'log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
    tabindex: '0',
  });
  const emptyMessage = el('p', { class: 'u-faint chat__empty' }, S.CHAT_EMPTY || '准备沙箱后开始对话。');
  // 空态直接给主 CTA：还没有沙箱时一键跳去沙箱卡（规格 §一「每个空态直接给主 CTA」）
  const emptyActionBtn = createButton({
    label: T.EMPTY_ACTION,
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      if (handlers.onGoSandbox) handlers.onGoSandbox();
    },
  });
  const emptyAction = el('div', { class: 'chat__empty-action' }, emptyActionBtn.el);
  emptyAction.hidden = true;
  const errorMessage = el('p', { class: 'chat__error', role: 'alert', hidden: true });
  const draft = el('textarea', {
    id: 'workspace-chat-message',
    class: 'chat__composer-input',
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
    onClick: () => handlers.onUsePrompt && handlers.onUsePrompt(),
  });
  const sendBtn = createButton({
    label: S.CHAT_SEND || '发送',
    variant: 'primary',
    onClick: () => send(),
  });
  const composerHint = el('span', { class: 'u-faint chat__composer-hint' }, S.CHAT_TOOL_HINT || '模型可在当前沙箱内读写文件并运行检查。');
  const composer = el(
    'form',
    { class: 'chat__composer', onSubmit: (event) => { event.preventDefault(); send(); } },
    el('label', { class: 'visually-hidden', for: 'workspace-chat-message' }, S.CHAT_INPUT_LABEL || '发送给模型的消息'),
    draft,
    // 输入区两个动作：发送当前轮提示词（幽灵）+ 发送（主强调，全屏唯一）
    el('div', { class: 'chat__composer-foot' }, composerHint, el('span', { class: 'u-spacer' }), usePromptBtn.el, sendBtn.el),
  );

  // 卡头结构（规格 §3.1 的图）：
  //   第 1 行 = 标题 … [模型档案 ▾] [状态]
  //   第 2 行 = 说明行（整行铺开："让模型在当前沙箱里改代码。改完去右侧运行校验。"）
  //   第 3 行 = 档案异常（唯一落点，紧贴选择器）
  // 档案是"这一轮用哪个模型"的设置，不是对话内容，所以放卡头右侧、不回消息流。
  const headTitleRow = el(
    'div',
    { class: 'chat__title-row' },
    el('h2', { class: 'ws-card__title', id: 'ws-chat-title' }, S.CHAT_TITLE || '内置对话'),
    runLabel,
    el('span', { class: 'u-spacer' }),
    modelField.el,
    // 状态点与文字只留一份：setStatus 会同时写 statusDot 与 statusText，
    // 两者并排会把同一句话印两遍（这正是用户说的"信息重复"）。
    statusDot.el,
  );
  const headDesc = el('p', { class: 'ws-card__desc', id: 'ws-chat-desc' }, T.DESC);
  // 档案异常紧贴选择器下方，是它的唯一落点（规格 §2.2）。
  const headNotes = el('div', { class: 'chat__head-notes' }, modelNote, modelRetryHost);
  const root = el(
    'section',
    { class: 'ws-card ws-region ws-region--chat', id: 'ws-region-chat', 'aria-labelledby': 'ws-chat-title' },
    el(
      'div',
      { class: 'ws-card__head chat__head' },
      el('div', { class: 'chat__head-top' }, headTitleRow, headDesc),
      headNotes,
      statusText,
    ),
    el('div', { class: 'ws-card__body chat__body' },
      emptyMessage,
      emptyAction,
      errorMessage,
      messageList,
      composer,
    ),
  );

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
    return `${item.id}|${item.role}|${item.content}|${item.reasoning}|${item.name}|${item.status}|${JSON.stringify(item.toolCalls || [])}`;
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

  /** 最后一条「有正文、不带工具调用、不是错误」的助手消息就是本轮总结。 */
  /**
   * 该不该钉住收尾总结：只有当对话**确实以这条结尾**时才钉。
   * 一旦后面又出现新消息（进入下一轮、模型又在调工具、报错行），它就是历史，
   * 继续顶在列表上方会挡住正在发生的事，而读者要看的恰恰是正在发生的事。
   * @param {Array} list
   * @returns {number}
   */
  function finalSummaryIndex(list) {
    const last = list[list.length - 1];
    if (!last || last.role !== 'assistant' || last.status || last.toolCalls.length) return -1;
    return String(last.content || '').trim() ? list.length - 1 : -1;
  }

  /** 空态一句话 + 主 CTA 的可见性：没有沙箱给「去准备」，有沙箱没消息给「发一条」。 */
  function renderEmpty() {
    const show = !currentRunId || !messages.length;
    emptyMessage.hidden = !show;
    emptyAction.hidden = Boolean(currentRunId) || !show;
    if (show) setText(emptyMessage, currentRunId ? T.NO_MESSAGES : (S.CHAT_EMPTY || '准备沙箱后开始对话。'));
  }

  function renderMessages() {
    clear(messageList);
    if (!messages.length) {
      messageList.hidden = true;
      renderEmpty();
      return;
    }
    emptyMessage.hidden = true;
    emptyAction.hidden = true;
    messageList.hidden = false;
    const summaryIndex = finalSummaryIndex(messages);
    if (summaryIndex >= 0) {
      // 置顶展示收尾总结：列表刚渲染时可能还在 hidden，靠滚动定位不可靠，
      // 而「对话结束了什么」不能藏在 43 个折叠块下面。
      const pinned = textMessageNode(messages[summaryIndex], true);
      pinned.classList.add('chat__message--pinned');
      messageList.appendChild(pinned);
    }
    let roundNumber = 0;
    for (let index = 0; index < messages.length; index += 1) {
      if (index === summaryIndex) continue;
      const message = messages[index];
      if (message.role === 'tool') {
        // 理论上工具返回都跟在自己的调用轮里；落单时兜底折叠显示
        messageList.appendChild(toolRoundNode(null, [message], roundNumber + 1));
        continue;
      }
      if (message.role === 'assistant' && message.toolCalls.length) {
        roundNumber += 1;
        const grouped = [];
        let cursor = index + 1;
        while (cursor < messages.length && messages[cursor].role === 'tool') {
          grouped.push(messages[cursor]);
          cursor += 1;
        }
        index = cursor - 1;
        const thinking = reasoningNode(message);
        if (thinking) messageList.appendChild(thinking);
        messageList.appendChild(toolRoundNode(message, grouped, roundNumber));
        continue;
      }
      messageList.appendChild(textMessageNode(message, index === summaryIndex));
    }
    // 时间线滚到底（辅助定位最新回合）；总结本身已置顶，不依赖这一步。
    window.requestAnimationFrame(() => {
      messageList.scrollTop = messageList.scrollHeight;
    });
  }

  /** 服务商返回的思维链：独立成块、默认展开、可折叠（带工具调用的那一轮也要看得见）。 */
  function reasoningNode(message) {
    if (!message.reasoning) return null;
    return el('details', { class: 'chat__reasoning', open: true },
      el('summary', {}, S.CHAT_REASONING || '模型推理摘要（由服务商提供）'),
      el('div', { class: 'chat__message-content chat__reasoning-content' }, message.reasoning));
  }

  /** 普通文本消息（用户提问、模型正文、错误提示）。 */
  function textMessageNode(message, isFinal) {
    const role = ROLE_LABELS[message.role] || message.role;
    const kind = `chat__message--${message.role}`;
    // 服务商没返回思维链时不写空态提示，正文本身就是全部内容。
    const reasoning = reasoningNode(message);
    return el('article', { class: `chat__message ${kind}${isFinal ? ' chat__message--final' : ''}` },
      el('div', { class: 'chat__message-meta' },
        isFinal ? S.CHAT_FINAL_SUMMARY : (message.name ? `${role} · ${message.name}` : role)),
      isFinal ? null : reasoning,
      el('div', { class: 'chat__message-content' }, message.content || '—'),
      isFinal ? reasoning : null,
      message.status ? el('div', { class: 'chat__message-status' }, message.status) : null,
    );
  }

  /** 按工具名聚合的摘要行：read_file ×4、run_command ×2。 */
  function toolSummaryLine(calls) {
    const counts = new Map();
    calls.forEach((call) => counts.set(call.name, (counts.get(call.name) || 0) + 1));
    return [...counts.entries()].map(([name, count]) => (count > 1 ? `${name} ×${count}` : name)).join('、');
  }

  /** 工具返回把失败放进 {"error": ...}，界面只报「几次没成功」，不铺开原文。 */
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
   * 本轮工具名 → 调用次数与失败次数，按首次出现顺序。
   * 落单的工具返回（没有对应调用轮）也计入，名字取记录里的 name。
   */
  function toolTally(calls, toolMessages) {
    const rows = [];
    const indexByName = new Map();
    const bump = (name, failed) => {
      let row = indexByName.get(name);
      if (!row) {
        row = { name, count: 0, failed: 0 };
        indexByName.set(name, row);
        rows.push(row);
      }
      row.count += 1;
      if (failed) row.failed += 1;
    };
    calls.forEach((call, position) => bump(call.name || '未知工具', toolResultFailed(toolMessages[position])));
    toolMessages.slice(calls.length).forEach((extra) => bump(extra.name || '工具', toolResultFailed(extra)));
    return rows;
  }

  /**
   * 一个工具轮：默认折叠成一行摘要，展开后只列这一轮调了哪些工具、各几次。
   * 参数与返回原文不进界面（完整数据在该轮运行目录的 chat.jsonl）。
   * @param {object|null} assistant 带工具调用的助手消息；null 表示落单的工具返回
   * @param {Array<object>} toolMessages 本轮的工具返回消息
   * @param {number} roundNumber 展示用轮次
   */
  function toolRoundNode(assistant, toolMessages, roundNumber) {
    const calls = assistant ? assistant.toolCalls : [];
    const roundId = assistant ? assistant.id : `orphan-${toolMessages[0] ? toolMessages[0].id : roundNumber}`;
    const rows = toolTally(calls, toolMessages);
    const pending = Math.max(0, calls.length - toolMessages.length);
    const body = [
      el('ul', { class: 'chat__toolround-list' },
        rows.map((row) => el('li', { class: 'chat__toolrow' },
          el('span', { class: 'chat__toolrow-name' }, row.name),
          el('span', { class: 'chat__toolrow-count' }, `×${row.count}`),
          row.failed ? el('span', { class: 'chat__toolrow-failed' }, `${row.failed} ${S.CHAT_TOOL_ROUND_FAILED}`) : null,
        ))),
      pending ? el('p', { class: 'chat__toolrow-pending' }, `${pending} ${S.CHAT_TOOL_ROUND_PENDING}`) : null,
      el('p', { class: 'chat__toolround-hint' }, S.CHAT_TOOL_ROUND_HINT),
    ];
    const summary = assistant
      ? `${S.CHAT_TOOL_ROUND || '工具轮'} ${roundNumber} · ${S.CHAT_TOOL_CALL || '工具调用'} ×${calls.length}：${toolSummaryLine(calls)}`
      : `${toolMessages[0] ? toolMessages[0].name || '工具' : '工具'} 返回`;
    return el('details', {
      class: 'chat__toolround',
      open: expandedRounds.has(roundId),
      onToggle: (event) => {
        if (event.target.open) expandedRounds.add(roundId);
        else expandedRounds.delete(roundId);
      },
    },
      el('summary', { class: 'chat__toolround-summary' }, summary),
      body,
    );
  }

  function setStatus(kind, text) {
    statusDot.update({ kind, text });
    setText(statusText, text || '');
  }

  /**
   * 状态点说清「输入框为什么锁着」。
   * 输入框按 run 状态与远端忙碌锁定，状态点却一律写「对话就绪」时，
   * 人只能靠猜——已交卷的那一轮就是这么被当成卡住的。
   *
   * 纪律（规格 §2.2）：档案已删除的整句只在卡头的下拉旁说一次。
   * 这里只说"现在能不能发"，不复述同一句话。
   */
  function syncStatus() {
    if (sending) {
      setStatus('busy', S.CHAT_SENDING || '模型处理中');
      return;
    }
    if (remoteBusy) {
      setStatus('busy', S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
      return;
    }
    if (profileGone) {
      setStatus('idle', T.GONE_STATUS);
      return;
    }
    const status = currentRun && currentRun.status;
    if (status && status !== 'ready') {
      setStatus('idle', CLOSED_STATUS.has(status) ? S.CHAT_STATUS_CLOSED : S.CHAT_STATUS_NOT_READY);
      return;
    }
    setStatus('ok', S.CHAT_STATUS_READY || '对话就绪');
  }


  /**
   * 输入框 / 发送按钮的可用性与「不可用的原因」。
   *
   * 可用 = 有运行记录 + 沙箱状态在 CHAT_OK（ready / graded）+ 没有请求在途。
   * 不可用时必须把原因写在按钮旁边（createButton 的 reason 会渲染成可见文字并
   * 挂 aria-describedby），否则使用者只会读成「对话框坏了」。
   *
   * @param {boolean} enabled 调用方希望的可用状态
   * @returns {void}
   */
  function setEnabled(enabled) {
    const status = currentRun && currentRun.status ? String(currentRun.status) : '';
    const sandboxOk = CHAT_OK.has(status);
    const editable = Boolean(enabled) && Boolean(currentRunId) && sandboxOk && !remoteBusy && !sending && !profileGone;
    draft.disabled = !editable;
    // 档案已删除时不再复述整句（卡头下拉旁已说清），这里只给"下一步动作"。
    const reason = !currentRunId
      ? T.DISABLED_NO_SANDBOX
      : profileGone
        ? T.GONE_SEND_REASON
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
    setStatus('busy', S.CHAT_LOADING || '正在加载对话');
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
      // 档案异常的整句归卡头下拉旁的 modelNote（一处）；输入框下面只留中性提示，
      // 免得同一句"档案已删除"在卡片头尾各出现一次。
      setText(composerHint, S.CHAT_TOOL_HINT);
      renderModelNote();
      if (remoteBusy) {
        setStatus('busy', S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
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
      setStatus('error', S.CHAT_STATUS_ERROR || '对话不可用');
      renderMessages();
    } finally {
      if (seq === requestSeq) setEnabled(Boolean(currentRunId) && !loading);
    }
  }

  async function send() {
    const text = draft.value.trim();
    if (!currentRunId || !text || loading) return false;
    if (profileGone) {
      // 整句已经在卡头下拉旁；这里只把视线带回那个出口，不再复述。
      showToast({ message: T.GONE_SEND_REASON, detail: S.CHAT_MODEL_GONE_DETAIL, kind: 'warn', duration: 8000 });
      return false;
    }
    if (sending || remoteBusy) {
      // 上一条还在服务端跑（一轮可能几十次工具调用）。必须当场说明并留住草稿：
      // 只在界面上留一个气泡、消息永远发不出去，看起来就像对话死了。
      setStatus('busy', S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
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
    setStatus('busy', S.CHAT_SENDING || '模型处理中');
    renderMessages();
    setEnabled(true);
    // Show completed provider/tool turns while the next model request runs.
    // This reads persisted messages; it does not synthesize token streaming.
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
      } catch { /* The send request remains authoritative for error display. */ }
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
        setStatus('busy', S.CHAT_SENDING || '模型处理中');
        renderMessages();
        watchRemoteSend(runId, requestSeq);
        return true;
      }
      remoteBusy = false;
      renderMessages();
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
        setStatus('busy', S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
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
      setStatus('error', S.CHAT_STATUS_ERROR || '对话不可用');
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

  /**
   * 同步模型档案下拉选项。选项集合没变就不动 select，避免打断键盘选择（§11.2 #14）。
   * @param {Array} list
   * @param {string} selected
   */
  function syncModelOptions(list, selected) {
    const options = [{ value: '', label: S.RUN_MODEL_EMPTY }].concat(
      (list || []).map((m) => {
        // 档案只有一个 id 时（config.json 里 id 和 model 同名），下拉只显示光秃秃的
        // id 没法辨认，补一行备注或明说未配置。
        const detail = m.model && m.model !== m.id ? m.model : (m.note || '');
        return { value: m.id, label: detail ? `${m.id}（${detail}）` : `${m.id}（${S.RUN_MODEL_UNSET}）` };
      }),
    );
    const sig = options.map((o) => o.value).join('|');
    if (sig === modelField.__sig) {
      if (selected !== undefined && selected !== modelField.getValue()) modelField.setValue(selected);
      return;
    }
    modelField.__sig = sig;
    modelField.update({ options, value: selected ?? '' });
  }

  /**
   * 档案下拉为空时把原因摊开：读取失败 ≠ 真的没有档案 ≠ 本轮绑定的档案被删了，
   * 三种情况给不同的话与出口。首屏数据没落定（工作台还在取数）时先别下结论。
   *
   * 「模型档案已删除」整页只在这里出现一次（规格 §2.2）：它就贴在选择器下面，
   * 替代方案（换一个档案）也就在手边。状态点与发送按钮只说"能不能发"。
   */
  function renderModelNote() {
    const count = (models || []).length;
    const failed = Boolean(modelsError);
    const pending = modelsLoading && !failed;
    let text = '';
    if (profileGone) {
      // 本轮绑定的模型名进正文；下拉里选的是"下一轮用哪个"，两者不是一回事。
      text = t(T.GONE_NOTE, { model: (currentRun && currentRun.model) || S.RUN_MODEL_UNSET });
    } else if (count === 0 && !pending) {
      text = failed ? t(T.MODEL_LOAD_FAILED, { reason: errorTitle(modelsError) }) : T.MODEL_NONE;
    }
    setText(modelNote, text);
    modelNote.hidden = !text;
    modelNote.dataset.tone = profileGone ? 'gone' : 'warn';
    // 只有「读取失败」才值得原地重试；确实一个档案都没有该去模型页新增
    modelRetryHost.hidden = !(failed && count === 0);
  }
  function update(state = {}) {
    if (state.run !== undefined) currentRun = state.run;
    if (state.models !== undefined) models = Array.isArray(state.models) ? state.models : [];
    if (state.modelId !== undefined) modelId = String(state.modelId || '');
    if (state.modelsError !== undefined) modelsError = state.modelsError || '';
    if (state.loading !== undefined) modelsLoading = Boolean(state.loading);
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
      // 卡头只放模型名；运行编号属于运行元信息，悬停 runLabel 可看（收进 title）
      setText(runLabel, currentRunId ? (currentRun.model || S.RUN_MODEL_UNSET) : '');
      runLabel.hidden = !currentRunId;
      runLabel.title = currentRunId || '';
      setStatus(currentRunId ? 'busy' : 'idle', currentRunId ? (S.CHAT_LOADING || '正在加载对话') : (S.CHAT_STATUS_IDLE || '未连接'));
      if (currentRunId) loadHistory(currentRunId);
    }
    syncModelOptions(models, modelId);
    // 模型档案是准备沙箱的前提，没选就常驻写在字段上：以前只在点准备沙箱时
    // 闪一条 toast，用户回头找不到自己漏了什么。
    modelField.update({ error: modelId ? '' : S.RUN_MODEL_REQUIRED });
    renderModelNote();
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

  /** 由提示词区直接提交完整提示词，避免用户在两个区域之间复制粘贴。 */
  function sendText(value) {
    const text = String(value || '').trim();
    if (!text) return Promise.resolve(false);
    draft.value = text;
    setEnabled(Boolean(currentRunId) && !loading);
    return send();
  }

  update({ run: null });

  return {
    el: root,
    update,
    setDraft,
    sendText,
    destroy() {
      clearTimeout(progressTimer);
      requestSeq += 1;
      if (ownsScope) scope.cancelAll();
      offInput();
      offKeydown();
      statusDot.destroy();
      modelField.destroy();
      modelRetryBtn.destroy();
      emptyActionBtn.destroy();
      usePromptBtn.destroy();
      sendBtn.destroy();
    },
  };
}
