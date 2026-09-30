/**
 * chat-panel.js — 工作台内置模型对话。
 *
 * 对话由服务端代理当前运行绑定的模型档案。模型的工具调用、文件改动和
 * 检查结果也作为消息显示；前端不保存或接触 API 密钥。
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
  let requestSeq = 0;
  let progressTimer = null;

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
    messages.forEach((message) => {
      const role = ROLE_LABELS[message.role] || message.role;
      const kind = message.role === 'tool' ? 'chat__message--tool' : `chat__message--${message.role}`;
      const meta = message.name ? `${role} · ${message.name}` : role;
      const body = message.content || (message.toolCalls.length ? (S.CHAT_TOOL_CALL_EMPTY || '模型请求使用受限工具。') : '—');
      const reasoning = message.reasoning
        ? el('details', { class: 'chat__reasoning', open: true },
          el('summary', {}, S.CHAT_REASONING || '模型推理摘要（由服务商提供）'),
          el('div', { class: 'chat__message-content chat__reasoning-content' }, message.reasoning))
        : (message.role === 'assistant' && !message.status
          ? el('p', { class: 'chat__message-status' }, S.CHAT_REASONING_MISSING) : null);
      const toolCalls = message.toolCalls.length
        ? el('div', { class: 'chat__message-status' },
          `${S.CHAT_TOOL_CALL || '工具调用'}：${message.toolCalls.map((call) => {
            const args = formatToolArguments(call.arguments);
            return args ? `${call.name}(${args})` : call.name;
          }).join('、')}`)
        : null;
      messageList.appendChild(
        el('article', { class: `chat__message ${kind}` },
          el('div', { class: 'chat__message-meta' }, meta),
          reasoning,
          el('div', { class: 'chat__message-content' }, body),
          toolCalls,
          message.status ? el('div', { class: 'chat__message-status' }, message.status) : null,
        ),
      );
    });
    messageList.scrollTop = messageList.scrollHeight;
  }

  function setStatus(kind, text) {
    statusDot.update({ kind, text });
    setText(statusText, text || '');
  }

  function setEnabled(enabled) {
    enabled = enabled && currentRun?.status === 'ready';
    draft.disabled = !enabled || sending;
    sendBtn.update({ disabled: !enabled || sending || !draft.value.trim(), loading: sending, busyLabel: S.CHAT_SENDING || '正在处理' });
    usePromptBtn.update({ disabled: !enabled });
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
      setStatus('ok', S.CHAT_STATUS_READY || '对话就绪');
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
      sending = false;
      loading = false;
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
