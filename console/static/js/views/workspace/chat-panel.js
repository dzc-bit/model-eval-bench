/**
 * chat-panel.js — 工作台内置模型对话。
 *
 * 对话由服务端代理当前运行绑定的模型档案。工具调用按轮折叠成一行摘要，
 * 展开后只列出这一轮调了哪些工具、各几次（参数与返回原文留在 chat.jsonl）；
 * 服务商返回公开思维链就展示，没有则直接突出正文。
 * 前端不保存或接触 API 密钥。
 */

import { el, clear, on, setText } from '../../core/dom.js';
import { S } from '../../core/strings.js';
import { api, ApiError, errorBody, errorTitle } from '../../core/api.js';
import { createButton } from '../../components/button.js';
import { createStatusDot } from '../../components/status-dot.js';
import { showToast } from '../../components/toast.js';

const ROLE_LABELS = {
  user: '你',
  assistant: '模型',
  tool: '工具',
  system: '系统',
};

/** 展开的工具轮（按消息 id 记住，轮询重渲染时不塌回去）。 */
const expandedRounds = new Set();

/**
 * 可以继续对话的服务端状态。
 * 与 sandbox-panel.js 的 SANDBOX_OK / workspace.js 的同名集对齐：
 * 已校验（graded）的这一轮仍然要能追问模型、让它接着改沙箱。
 * 原先只认 'ready'，跑完一次校验后输入框和发送按钮就永久变灰且不给原因。
 */
const CHAT_OK = new Set(['ready', 'graded']);

/**
 * 创建内置对话面板。
 * @param {{scope?: object, onUsePrompt?: Function}} [handlers]
 * @returns {{el: HTMLElement, update: Function, setDraft: Function, destroy: Function}}
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
  let requestSeq = 0;
  let progressTimer = null;
  let remoteTimer = null;

  const runLabel = el('p', { class: 'u-faint chat__run' });
  const statusText = el('span', { class: 'u-faint', role: 'status', 'aria-live': 'polite' });
  const statusDot = createStatusDot({ kind: 'idle', text: S.CHAT_STATUS_IDLE || '未连接' });
  const messageList = el('div', {
    class: 'chat__messages',
    role: 'log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
    tabindex: '0',
  });
  const emptyMessage = el('p', { class: 'u-faint chat__empty' }, S.CHAT_EMPTY || '准备沙箱后开始对话。');
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
    el('div', { class: 'chat__composer-foot' }, composerHint, el('span', { class: 'u-spacer' }), sendBtn.el),
  );

  const root = el(
    'section',
    { class: 'panel ws-region ws-region--chat', id: 'ws-region-chat', 'aria-labelledby': 'ws-chat-title' },
    el('div', { class: 'panel__head chat__head' },
      el('div', { class: 'chat__title-wrap' },
        el('h2', { class: 'panel__title', id: 'ws-chat-title' }, S.CHAT_TITLE || '内置对话'),
        runLabel,
      ),
      el('span', { class: 'u-spacer' }),
      statusDot.el,
      statusText,
      usePromptBtn.el,
    ),
    emptyMessage,
    errorMessage,
    messageList,
    composer,
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
  function finalSummaryIndex(list) {
    for (let index = list.length - 1; index >= 0; index -= 1) {
      const item = list[index];
      if (item.role !== 'assistant' || item.status || item.toolCalls.length) continue;
      if (String(item.content || '').trim()) return index;
    }
    return -1;
  }

  function renderMessages() {
    clear(messageList);
    if (!messages.length) {
      messageList.hidden = true;
      emptyMessage.hidden = !currentRunId;
      return;
    }
    emptyMessage.hidden = true;
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
    const editable = Boolean(enabled) && Boolean(currentRunId) && sandboxOk && !remoteBusy && !sending;
    draft.disabled = !editable;
    const reason = !currentRunId
      ? S.CHAT_DISABLED_NO_SANDBOX
      : remoteBusy
        ? S.CHAT_REMOTE_BUSY
        : !sandboxOk
          ? S.CHAT_DISABLED_STATUS
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
      setStatus('ok', S.CHAT_STATUS_READY || '对话就绪');
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
      if (remoteBusy) {
        setStatus('busy', S.CHAT_REMOTE_BUSY || '模型仍在处理上一条消息…');
        watchRemoteSend(runId, seq);
      } else {
        setStatus('ok', S.CHAT_STATUS_READY || '对话就绪');
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
    if (!currentRunId || !text || loading || sending) return false;
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
      const data = await api.longPost(`/runs/${encodeURIComponent(runId)}/chat`, { message: text }, { scope });
      if (seq !== requestSeq || runId !== currentRunId) return;
      if (data && Array.isArray(data.messages)) messages = normalizeMessages(data.messages);
      else if (data && data.message) mergeMessages([data.message]);
      setStatus('ok', S.CHAT_STATUS_READY || '对话就绪');
      renderMessages();
      return true;
    } catch (err) {
      if (seq !== requestSeq || runId !== currentRunId) return;
      const code = err instanceof ApiError ? err.code : 'INTERNAL';
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

  function update({ run } = {}) {
    if (run !== undefined) currentRun = run;
    const nextRunId = currentRun && currentRun.run_id ? String(currentRun.run_id) : '';
    if (nextRunId !== currentRunId) {
      clearTimeout(progressTimer);
      clearTimeout(remoteTimer);
      sending = false;
      loading = false;
      remoteBusy = false;
      currentRunId = nextRunId;
      messages = [];
      requestSeq += 1;
      draft.value = '';
      setText(runLabel, currentRunId ? `${currentRun.model || '模型'} · ${currentRunId}` : '');
      runLabel.hidden = !currentRunId;
      setStatus(currentRunId ? 'busy' : 'idle', currentRunId ? (S.CHAT_LOADING || '正在加载对话') : (S.CHAT_STATUS_IDLE || '未连接'));
      if (currentRunId) loadHistory(currentRunId);
    }
    const ready = Boolean(currentRunId) && !loading;
    emptyMessage.hidden = Boolean(currentRunId);
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
      usePromptBtn.destroy();
      sendBtn.destroy();
    },
  };
}
