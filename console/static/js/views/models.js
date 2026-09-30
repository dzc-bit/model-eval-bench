/**
 * models.js — 模型档案视图
 *
 * 职责：模型档案的增 / 改 / 删。档案只用于记账与统计（§15 备注），评测台不代发消息。
 *
 * 状态：loading / ready / empty / error；表单态：新建 / 编辑 / 校验错误。
 * 键盘：所有字段可 Tab；label 关联；错误 aria-describedby；删除走二次确认（默认焦点在取消）。
 * ARIA：必填 aria-required、错误 role="alert"、删除按钮 aria-label 带档案名（§12.11 / §12.12）。
 *
 * 依赖：core/*、components/*
 * 导出：createModels(props) → { el, destroy, el_h1, getModels }
 */

import { el, setText, patchList } from '../core/dom.js';
import { S, t, PROTOCOL_NAMES, API_MODE_NAMES } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { announce } from '../core/a11y.js';
import { createField } from '../components/field.js';
import { createButton } from '../components/button.js';
import { createSkeleton } from '../components/skeleton.js';
import { createEmptyState } from '../components/empty-state.js';
import { confirmDialog } from '../components/confirm-dialog.js';
import { showToast } from '../components/toast.js';
import { createBadge } from '../components/badge.js';

/** 协议选项。 */
const PROTOCOL_OPTIONS = Object.entries(PROTOCOL_NAMES).map(([value, label]) => ({ value, label }));
/** OpenAI 兼容 endpoint；native 仅用于非 OpenAI 协议。 */
const OPENAI_API_MODE_OPTIONS = ['responses', 'chat_completions', 'completions']
  .map((value) => ({ value, label: API_MODE_NAMES[value] }));
const NATIVE_API_MODE_OPTIONS = [{ value: 'native', label: API_MODE_NAMES.native }];

/** 档案编号合法性：小写字母、数字、连字符。 */
const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

/**
 * 创建模型档案视图。
 * @param {{onChange?: (models: Array) => void}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement, getModels: Function}}
 */
export function createModels(props = {}) {
  const { onChange } = props;
  const scope = api.scope();

  let models = [];
  let loading = true;
  let error = null;
  /** 正在编辑的档案 id；null 表示新建。 */
  let editingId = null;
  /** 编辑态下原档案的脱敏密钥，保存时原样带回（后端不保存明文）。 */
  let editingKeyMasked = '';

  const h1 = el('h1', { tabindex: '-1' }, S.MODELS_TITLE);
  const listHost = el('div', { class: 'models__list' });
  const formHost = el('div', { class: 'panel' });

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.MODELS_DESC)),
      el('div', { class: 'view__actions' },
        createButton({
          label: S.MODELS_FORM_NEW,
          onClick: () => startCreate(),
        }).el,
      ),
    ),
    el('div', { class: 'models__layout' },
      el('section', { class: 'panel' },
        el('h2', { class: 'panel__title' }, S.MODELS_TITLE),
        listHost,
      ),
      formHost,
    ),
  );

  // ---- 表单 ----
  const idField = createField({
    label: S.MODELS_FIELD_ID,
    name: 'model-id',
    required: true,
    hint: S.MODELS_FIELD_ID_HINT,
  });
  const protocolField = createField({
    label: S.MODELS_FIELD_PROTOCOL,
    name: 'model-protocol',
    type: 'select',
    options: PROTOCOL_OPTIONS,
    value: 'openai',
    onChange: (value) => syncApiModeControl(value),
  });
  const apiModeField = createField({
    label: S.MODELS_FIELD_API_MODE,
    name: 'model-api-mode',
    type: 'select',
    options: OPENAI_API_MODE_OPTIONS,
    value: 'chat_completions',
    hint: S.MODELS_FIELD_API_MODE_HINT,
  });
  const modelField = createField({
    label: S.MODELS_FIELD_MODEL,
    name: 'model-name',
    required: true,
  });
  const urlField = createField({
    label: S.MODELS_FIELD_BASE_URL,
    name: 'model-url',
    type: 'url',
    placeholder: 'https://',
  });
  const noteField = createField({
    label: S.MODELS_FIELD_NOTE,
    name: 'model-note',
    type: 'textarea',
    rows: 2,
  });

  const saveBtn = createButton({
    label: S.MODELS_SAVE,
    variant: 'primary',
    onClick: () => save(),
  });
  const cancelEditBtn = createButton({
    label: S.ACTION_CANCEL,
    onClick: () => startCreate(),
  });

  const formTitle = el('h2', { class: 'panel__title' }, S.MODELS_FORM_NEW);
  const formError = el('p', { class: 'field__error', role: 'alert' });

  /** 根据协议切换 endpoint 选择器，避免给非 OpenAI 档案留下歧义值。 */
  function syncApiModeControl(protocol, value) {
    const openai = String(protocol || '').toLowerCase() === 'openai';
    const options = openai ? OPENAI_API_MODE_OPTIONS : NATIVE_API_MODE_OPTIONS;
    const mode = openai
      ? (options.some((option) => option.value === value) ? value : 'chat_completions')
      : 'native';
    apiModeField.update({
      options,
      disabled: !openai,
      hint: openai ? S.MODELS_FIELD_API_MODE_HINT : S.MODELS_FIELD_API_MODE_NATIVE_HINT,
      value: mode,
    });
  }

  function buildForm() {
    formHost.textContent = '';
    formHost.appendChild(formTitle);
    formHost.appendChild(formError);
    formHost.appendChild(
      el(
        'div',
        { class: 'models__form' },
        idField.el,
        el('div', { class: 'models__form-row' }, protocolField.el, apiModeField.el, modelField.el),
        urlField.el,
        noteField.el,
        el('p', { class: 'u-faint' }, S.MODELS_FIELD_KEY_HINT),
        el('div', { class: 'u-row' }, saveBtn.el, cancelEditBtn.el),
      ),
    );
  }

  /**
   * 表单校验。
   * @returns {boolean}
   */
  function validate() {
    const id = idField.getValue().trim();
    let ok = true;
    if (!id) {
      idField.update({ error: S.MODELS_FIELD_ID_REQUIRED });
      ok = false;
    } else if (!ID_PATTERN.test(id)) {
      idField.update({ error: S.MODELS_FIELD_ID_INVALID });
      ok = false;
    } else if (editingId !== id && models.some((m) => m.id === id)) {
      idField.update({ error: S.MODELS_FIELD_ID_DUP });
      ok = false;
    } else {
      idField.update({ error: '' });
    }
    if (!modelField.getValue().trim()) {
      modelField.update({ error: S.MODELS_FIELD_ID_REQUIRED });
      ok = false;
    } else {
      modelField.update({ error: '' });
    }
    return ok;
  }

  /**
   * 保存档案。
   */
  async function save() {
    setText(formError, '');
    if (!validate()) {
      announce(S.MODELS_FIELD_ID_REQUIRED, { assertive: true });
      return;
    }
    const payload = {
      id: idField.getValue().trim(),
      protocol: protocolField.getValue(),
      api_mode: apiModeField.getValue(),
      model: modelField.getValue().trim(),
      base_url: urlField.getValue().trim(),
      note: noteField.getValue(),
      // 后端只保存脱敏后的 key_masked，明文密钥请手写进 config.json（见下方提示）
      key_masked: editingKeyMasked,
    };

    saveBtn.update({ loading: true, busyLabel: S.ACTION_SAVED });
    try {
      // 契约：POST 新建、PATCH 覆盖，id 一律走请求体，没有 /models/{id} 这样的路径
      if (editingId) {
        await api.patch('/models', payload, { scope });
        showToast({ message: t(S.MODELS_SAVED, { id: editingId }), kind: 'success', duration: 4000 });
      } else {
        await api.post('/models', payload, { scope });
        showToast({ message: t(S.MODELS_SAVED, { id: payload.id }), kind: 'success', duration: 4000 });
      }
      startCreate();
      await load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'SAVE_FAILED';
      setText(formError, errorBody(code));
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error' });
    } finally {
      saveBtn.update({ loading: false });
    }
  }

  /**
   * 进入新建态。
   */
  function startCreate() {
    editingId = null;
    editingKeyMasked = '';
    setText(formTitle, S.MODELS_FORM_NEW);
    idField.update({ value: '', error: '' });
    protocolField.update({ value: 'openai' });
    syncApiModeControl('openai', 'chat_completions');
    modelField.update({ value: '', error: '' });
    urlField.update({ value: '' });
    noteField.update({ value: '' });
    cancelEditBtn.el.hidden = true;
  }

  /**
   * 进入编辑态。
   * @param {string} id
   */
  function startEdit(id) {
    const m = models.find((x) => x.id === id);
    if (!m) return;
    editingId = id;
    editingKeyMasked = m.key_masked || '';
    setText(formTitle, `${S.MODELS_FORM_EDIT}：${m.id}`);
    idField.update({ value: m.id, error: '' });
    protocolField.update({ value: m.protocol || 'openai' });
    syncApiModeControl(m.protocol || 'openai', m.api_mode || 'chat_completions');
    modelField.update({ value: m.model || '', error: '' });
    urlField.update({ value: m.base_url || '' });
    noteField.update({ value: m.note || '' });
    cancelEditBtn.el.hidden = false;
    idField.focus();
  }

  /**
   * 删除档案（二次确认，破坏性操作默认焦点在取消）。
   * @param {string} id
   */
  async function remove(id) {
    const ok = await confirmDialog({
      title: t(S.MODELS_DELETE_CONFIRM_TITLE, { id }),
      messages: [S.MODELS_DELETE_CONFIRM_BODY_1, S.MODELS_DELETE_CONFIRM_BODY_2],
      confirmLabel: S.ACTION_DELETE,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    try {
      // 契约：DELETE /api/models?id=xxx
      await api.del('/models', { scope, params: { id } });
      showToast({ message: t(S.MODELS_DELETED, { id }), kind: 'success', duration: 4000 });
      if (editingId === id) startCreate();
      await load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'ACTION_FAILED';
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error' });
    }
  }

  /**
   * 档案行。
   * @param {object} m
   * @returns {HTMLElement}
   */
  function renderRow(m) {
    return el(
      'li',
      { class: 'model-row' },
      el('span', { class: 'model-row__id' }, m.id),
      createBadge({
        label: PROTOCOL_NAMES[m.protocol] || m.protocol || S.PROTOCOL_CUSTOM,
        variant: 'info',
        glyph: '·',
      }).el,
      el('span', { class: 'model-row__meta' },
        `${m.model || '—'}${m.base_url ? ` · ${m.base_url}` : ''}`,
        el('div', { class: 'u-faint' }, t(S.MODELS_ROW_API_MODE, {
          mode: API_MODE_NAMES[m.api_mode] || API_MODE_NAMES.native,
        })),
        m.key_masked ? el('div', { class: 'u-faint' }, t(S.MODELS_ROW_KEY, { masked: m.key_masked })) : null,
        m.note ? el('div', { class: 'u-faint' }, m.note) : null,
      ),
      el(
        'div',
        { class: 'model-row__actions' },
        createButton({ label: S.ACTION_EDIT, size: 'sm', ariaLabel: `${S.ACTION_EDIT} ${m.id}`, onClick: () => startEdit(m.id) }).el,
        createButton({
          label: S.ACTION_DELETE,
          size: 'sm',
          variant: 'danger',
          ariaLabel: `${S.ACTION_DELETE} ${m.id}`,
          onClick: () => remove(m.id),
        }).el,
      ),
    );
  }

  /**
   * 三态渲染列表。
   */
  function render() {
    listHost.textContent = '';
    if (loading) {
      listHost.appendChild(createSkeleton({ rows: 3, variant: 'row', label: S.MODELS_LOADING_DESC }).el);
      return;
    }
    if (error) {
      listHost.appendChild(
        createEmptyState({
          title: errorTitle(error),
          desc: errorBody(error),
          alert: true,
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'primary', onClick: () => load() }).el],
        }).el,
      );
      return;
    }
    if (models.length === 0) {
      listHost.appendChild(
        createEmptyState({
          title: S.MODELS_EMPTY,
          desc: S.MODELS_EMPTY_DESC,
          actions: [createButton({ label: S.MODELS_FORM_NEW, variant: 'primary', onClick: () => startCreate() }).el],
        }).el,
      );
      return;
    }
    const ul = el('ul', { class: 'models__list' });
    patchList(ul, models, (m) => m.id, renderRow, () => {});
    listHost.appendChild(ul);
  }

  /**
   * 拉取档案列表。
   */
  async function load() {
    loading = true;
    error = null;
    render();
    try {
      const res = await api.get('/models', { scope });
      models = (res && res.models) || [];
      loading = false;
      render();
      if (onChange) onChange(models);
    } catch (err) {
      loading = false;
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      error = err.code || 'LOAD_FAILED';
      render();
    }
  }

  buildForm();
  startCreate();
  load();

  return {
    el: root,
    el_h1: h1,
    /** 当前档案列表（供工作台绑定用）。 */
    getModels: () => models,
    /** 解绑 + 取消在途请求。 */
    destroy() {
      scope.cancelAll();
      [idField, protocolField, apiModeField, modelField, urlField, noteField, saveBtn, cancelEditBtn].forEach((c) => c.destroy());
    },
  };
}
