/**
 * chat-panel.js — 工作台内置模型对话。
 *
 * 对话由服务端代理当前运行绑定的模型档案。工具调用按轮折叠成一行摘要，
 * 展开后才看到每次调用的参数与返回原文（超长截断，全文在 chat.jsonl）；
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

/** 工具返回在折叠体里最多原样展示的字符数；超长截断，完整数据在 chat.jsonl。 */
const TOOL_OUTPUT_PREVIEW_CAP = 4000;
const TOOL_ARGS_PREVIEW_CAP = 300;

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

  function formatToolArguments(value) {
    if (!value) return '';
    try {
      return JSON.stringify(JSON.parse(value));
    } catch {
      return value;
    }
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

  function renderMessages() {
    clear(messageList);
    if (!messages.length) {
      messageList.hidden = true;
      emptyMessage.hidden = !currentRunId;
      return;
    }
    emptyMessage.hidden = true;
    messageList.hidden = false;
    let roundNumber = 0;
    for (let index = 0; index < messages.length; index += 1) {
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
        messageList.appendChild(toolRoundNode(message, grouped, roundNumber));
        continue;
      }
      messageList.appendChild(textMessageNode(message));
    }
    messageList.scrollTop = messageList.scrollHeight;
  }

  /** 普通文本消息（用户提问、模型正文、错误提示）。 */
  function textMessageNode(message) {
    const role = ROLE_LABELS[message.role] || message.role;
    const kind = `chat__message--${message.role}`;
    const reasoning = message.reasoning
      ? el('details', { class: 'chat__reasoning', open: true },
        el('summary', {}, S.CHAT_REASONING || '模型推理摘要（由服务商提供）'),
        el('div', { class: 'chat__message-content chat__reasoning-content' }, message.reasoning))
      : (message.role === 'assistant' && !message.status
        ? el('p', { class: 'chat__message-status' }, S.CHAT_REASONING_MISSING) : null);
    return el('article', { class: `chat__message ${kind}` },
      el('div', { class: 'chat__message-meta' }, message.name ? `${role} · ${message.name}` : role),
      reasoning,
      el('div', { class: 'chat__message-content' }, message.content || '—'),
      message.status ? el('div', { class: 'chat__message-status' }, message.status) : null,
    );
  }

  /** 按工具名聚合的摘要行：read_file ×4、run_command ×2。 */
  function toolSummaryLine(calls) {
    const counts = new Map();
    calls.forEach((call) => counts.set(call.name, (counts.get(call.name) || 0) + 1));
    return [...counts.entries()].map(([name, count]) => (count > 1 ? `${name} ×${count}` : name)).join('、');
  }

  function truncate(text, cap) {
    const value = String(text || '');
    return value.length > cap ? `${value.slice(0, cap)}…` : value;
  }

  /**
   * 一个工具轮：默认折叠成一行摘要，展开后看每次调用的参数与返回。
   * @param {object|null} assistant 带工具调用的助手消息；null 表示落单的工具返回
   * @param {Array<object>} toolMessages 本轮的工具返回消息
   * @param {number} roundNumber 展示用轮次
   */
  function toolRoundNode(assistant, toolMessages, roundNumber) {
    const calls = assistant ? assistant.toolCalls : [];
    const roundId = assistant ? assistant.id : `orphan-${toolMessages[0] ? toolMessages[0].id : roundNumber}`;
    const bodyChildren = [];
    calls.forEach((call, position) => {
      const result = toolMessages[position];
      bodyChildren.push(
        el('div', { class: 'chat__toolcall' },
          el('div', { class: 'chat__toolcall-head' },
            el('strong', {}, call.name),
            el('span', { class: 'chat__toolcall-args' }, truncate(formatToolArguments(call.arguments), TOOL_ARGS_PREVIEW_CAP)),
          ),
          result
            ? el('pre', { class: 'chat__toolout' }, truncate(String(result.content || ''), TOOL_OUTPUT_PREVIEW_CAP))
            : el('div', { class: 'chat__toolout chat__toolout--pending' }, '等待工具返回…'),
        ),
      );
    });
    if (!calls.length || toolMessages.length > calls.length) {
      toolMessages.slice(calls.length).forEach((extra) => {
        bodyChildren.push(el('div', { class: 'chat__toolcall' },
          el('div', { class: 'chat__toolcall-head' }, el('strong', {}, extra.name || '工具')),
          el('pre', { class: 'chat__toolout' }, truncate(String(extra.content || ''), TOOL_OUTPUT_PREVIEW_CAP)),
        ));
      });
    }
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
      bodyChildren,
    );
  }

  function setStatus(kind, text) {
    statusDot.update({ kind, text });
    setText(statusText, text || '');
  }

  function setEnabled(enabled) {
    enabled = enabled && currentRun?.status === 'ready' && !remoteBusy;
    draft.disabled = !enabled || sending;
    sendBtn.update({ disabled: !enabled || sending || !draft.value.trim(), loading: sending, busyLabel: S.CHAT_SENDING || '正在处理' });
    usePromptBtn.update({ disabled: !enabled });
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
