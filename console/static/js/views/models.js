/**
 * models.js — 模型档案视图
 *
 * 职责：模型档案的增 / 改 / 删。档案绑定工作台内置对话和成绩统计；明文密钥只在服务端环境变量中。
 *
 * 状态：loading / ready / empty / error；表单态：新建 / 编辑 / 校验错误。
 * 键盘：所有字段可 Tab；label 关联；错误 aria-describedby；删除走二次确认（默认焦点在取消）。
 * ARIA：必填 aria-required、错误 role="alert"、删除按钮 aria-label 带档案名（§12.11 / §12.12）。
 *
 * 依赖：core/*、components/*
 * 导出：createModels(props) → { el, destroy, el_h1, getModels }
 */

import { el, setText, patchList, clear } from '../core/dom.js';
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
const OPENAI_ENDPOINT_PATHS = {
  responses: '/responses',
  chat_completions: '/chat/completions',
  completions: '/completions',
};

/** 档案编号合法性：小写字母、数字、连字符。 */
const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
// 与服务端 upsert_model 的 key_env 校验同规则：环境变量名，不是密钥本身
const KEY_ENV_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

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
  const formHost = el('section', {
    class: 'models__editor panel',
    'aria-labelledby': 'models-form-title',
    hidden: true,
  });

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.MODELS_DESC)),
      el('div', { class: 'view__actions' },
        createButton({
          label: S.MODELS_FORM_NEW,
          onClick: () => startCreate({ show: true }),
        }).el,
      ),
    ),
    el('div', { class: 'models__layout' },
      el('section', { class: 'models__catalog', 'aria-labelledby': 'models-list-title' },
        el('div', { class: 'models__section-head' },
          el('h2', { id: 'models-list-title' }, '已配置档案'),
        ),
        listHost,
      ),
      formHost,
    ),
  );

  // ---- 表单 ----
  /** 用户是否手动改过档案编号；没动过就跟着模型名自动建议。 */
  let idTouched = false;
  const idField = createField({
    label: S.MODELS_FIELD_ID,
    name: 'model-id',
    required: true,
    hint: S.MODELS_FIELD_ID_HINT,
    onInput: () => { idTouched = true; },
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
    onChange: () => updateEndpointPreview(),
  });
  const modelField = createField({
    label: S.MODELS_FIELD_MODEL,
    name: 'model-name',
    required: true,
    onInput: (value) => {
      // 新建且编号没手动改过时跟着模型名建议，用户必填的只剩模型名、URL 和密钥
      if (!editingId && !idTouched) idField.update({ value: suggestId(value), error: '' });
    },
  });
  const urlField = createField({
    label: 'API 根地址',
    name: 'model-url',
    type: 'url',
    placeholder: 'https://',
    onInput: () => updateEndpointPreview(),
  });
  const apiKeyField = createField({
    label: S.MODELS_FIELD_KEY,
    name: 'model-key',
    type: 'password',
    placeholder: S.MODELS_FIELD_KEY_PLACEHOLDER,
    hint: S.MODELS_FIELD_KEY_HINT,
    autocomplete: 'new-password',
  });
  const noteField = createField({
    label: S.MODELS_FIELD_NOTE,
    name: 'model-note',
    type: 'textarea',
    rows: 2,
  });
  const keyEnvField = createField({
    label: S.MODELS_FIELD_KEY_ENV,
    name: 'model-key-env',
    hint: S.MODELS_FIELD_KEY_ENV_HINT,
    placeholder: 'OPENAI_API_KEY',
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

  const formTitle = el('h2', { class: 'panel__title', id: 'models-form-title' }, S.MODELS_FORM_NEW);
  const formError = el('p', { class: 'field__error', role: 'alert' });
  const endpointPreview = el('code', { class: 'models__endpoint-value' });

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
    updateEndpointPreview();
  }

  /** 显示将由 API 根地址与 OpenAI endpoint 路径组成的请求地址。 */
  function updateEndpointPreview() {
    const protocol = String(protocolField.getValue() || '').toLowerCase();
    const baseUrl = String(urlField.getValue() || '').trim().replace(/\/+$/, '');
    if (protocol !== 'openai') {
      setText(endpointPreview, '供应商原生接口');
      return;
    }
    const mode = apiModeField.getValue() || 'chat_completions';
    const path = OPENAI_ENDPOINT_PATHS[mode] || OPENAI_ENDPOINT_PATHS.chat_completions;
    setText(endpointPreview, `POST ${baseUrl}${path}`);
  }

  /** 由模型名建议档案编号：小写字母数字连字符，数字开头补 m- 前缀。 */
  function suggestId(modelName) {
    const slug = String(modelName || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (!slug) return '';
    return /^[0-9]/.test(slug) ? `m-${slug}` : slug;
  }

  /** 将编辑区滚到粘性导航下方，避免窄屏时标题与首个字段被遮挡。 */
  function revealEditor() {
    const header = document.querySelector('.app-header');
    const headerBottom = header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
    const gap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-3')) || 12;
    const editorTop = formHost.getBoundingClientRect().top + window.scrollY;
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
    window.scrollTo({ top: Math.max(0, editorTop - headerBottom - gap), behavior });
  }

  function buildForm() {
    formHost.textContent = '';
    formHost.appendChild(formTitle);
    formHost.appendChild(formError);
    formHost.appendChild(
      el(
        'div',
        { class: 'models__form' },
        el('div', { class: 'models__form-grid' },
          el('fieldset', { class: 'models__fieldset' },
            el('legend', {}, '档案身份'),
            el('div', { class: 'models__form-row' }, idField.el, modelField.el),
          ),
          el('fieldset', { class: 'models__fieldset' },
            el('legend', {}, '连接'),
            el('div', { class: 'models__form-row' }, urlField.el, apiKeyField.el),
            el('div', { class: 'models__endpoint-preview' },
              el('span', { class: 'models__endpoint-label' }, '实际请求地址'),
              endpointPreview,
            ),
          ),
          el('details', { class: 'models__advanced' },
            el('summary', { class: 'models__advanced-summary' }, S.MODELS_FORM_ADVANCED),
            el('div', { class: 'models__form-row' }, protocolField.el, apiModeField.el),
            keyEnvField.el,
            noteField.el,
            el('p', { class: 'u-faint' }, S.MODELS_KEY_PRIVACY),
          ),
        ),
        el('div', { class: 'models__form-actions' }, saveBtn.el, cancelEditBtn.el),
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
    const keyEnv = keyEnvField.getValue().trim();
    if (keyEnv && !KEY_ENV_PATTERN.test(keyEnv)) {
      keyEnvField.update({ error: S.MODELS_FIELD_KEY_ENV_INVALID });
      ok = false;
    } else {
      keyEnvField.update({ error: '' });
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
      key_env: keyEnvField.getValue().trim(),
      note: noteField.getValue(),
      // 粘贴了新密钥才传 api_key；留空则后端保留已存密钥。config.json 只存脱敏值
      key_masked: editingKeyMasked,
      api_key: apiKeyField.getValue().trim(),
    };
    if (editingId && editingId !== payload.id) payload.previous_id = editingId;

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
      const cannedTitle = errorTitle(code);
      // 服务端对表单类错误会返回一句具体中文（如「key_env 必须是合法的服务端环境变量名。」），
      // 比按码查到的通用标题更能指出错在哪个字段，优先展示
      const backendMessage = err instanceof ApiError ? String(err.message || '').trim() : '';
      const title = backendMessage || cannedTitle;
      const detail = errorBody(code);
      setText(formError, detail);
      showToast({ message: title, detail, kind: 'error' });
    } finally {
      saveBtn.update({ loading: false });
    }
  }

  /**
   * 进入新建态。
   */
  function startCreate({ show = false } = {}) {
    editingId = null;
    editingKeyMasked = '';
    idTouched = false;
    setText(formTitle, S.MODELS_FORM_NEW);
    idField.update({ value: '', error: '' });
    protocolField.update({ value: 'openai' });
    syncApiModeControl('openai', 'chat_completions');
    modelField.update({ value: '', error: '' });
    urlField.update({ value: '' });
    noteField.update({ value: '' });
    keyEnvField.update({ value: '' });
    apiKeyField.update({ value: '', error: '' });
    cancelEditBtn.el.hidden = true;
    formHost.hidden = !show;
    updateEndpointPreview();
    if (show) revealEditor();
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
    idTouched = true; // 编辑既有档案：编号已定，不跟着模型名自动改
    idField.update({ value: m.id, error: '' });
    protocolField.update({ value: m.protocol || 'openai' });
    syncApiModeControl(m.protocol || 'openai', m.api_mode || 'chat_completions');
    modelField.update({ value: m.model || '', error: '' });
    urlField.update({ value: m.base_url || '' });
    noteField.update({ value: m.note || '' });
    keyEnvField.update({ value: m.key_env || '' });
    apiKeyField.update({ value: '', error: '' }); // 留空 = 保留已保存的密钥
    cancelEditBtn.el.hidden = false;
    formHost.hidden = false;
    updateEndpointPreview();
    revealEditor();
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
    const protocol = String(m.protocol || 'openai').toLowerCase();
    const mode = protocol === 'openai' ? (m.api_mode || 'chat_completions') : 'native';
    const baseUrl = String(m.base_url || '').trim().replace(/\/+$/, '');
    const endpoint = protocol === 'openai'
      ? `POST ${baseUrl}${OPENAI_ENDPOINT_PATHS[mode] || OPENAI_ENDPOINT_PATHS.chat_completions}`
      : '路由由供应商协议决定';
    return el(
      'li',
      { class: 'model-row' },
      el('div', { class: 'model-row__main' },
        el('div', { class: 'model-row__identity' },
          el('strong', { class: 'model-row__model' }, m.model || '—'),
          el('span', { class: 'model-row__id' }, `ID · ${m.id}`),
        ),
        createBadge({
          label: PROTOCOL_NAMES[protocol] || protocol || S.PROTOCOL_CUSTOM,
          variant: 'info',
          glyph: '·',
        }).el,
      ),
      el('dl', { class: 'model-row__details' },
        el('div', { class: 'model-row__detail model-row__detail--endpoint' },
          el('dt', {}, '接口形态'),
          el('dd', {},
            el('span', { class: 'model-row__mode' }, API_MODE_NAMES[mode] || API_MODE_NAMES.native),
            el('code', { class: 'model-row__endpoint' }, endpoint),
          ),
        ),
        el('div', { class: 'model-row__detail' },
          el('dt', {}, 'API 根地址'),
          el('dd', { class: 'model-row__value' }, m.base_url || '—'),
        ),
        m.key_masked ? el('div', { class: 'model-row__detail' },
          el('dt', {}, S.MODELS_FIELD_KEY),
          el('dd', { class: 'model-row__value' }, m.key_masked),
        ) : null,
        m.note ? el('div', { class: 'model-row__detail model-row__detail--note' },
          el('dt', {}, S.MODELS_FIELD_NOTE),
          el('dd', {}, m.note),
        ) : null,
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

  /** 同上：patchList 复用行节点，档案被改过就必须原地重画那一行。 */
  const rowSignatures = new WeakMap();

  function createRow(m) {
    const node = renderRow(m);
    rowSignatures.set(node, JSON.stringify(m));
    return node;
  }

  function refreshRow(node, m) {
    const signature = JSON.stringify(m);
    if (rowSignatures.get(node) === signature) return;
    rowSignatures.set(node, signature);
    const rebuilt = renderRow(m);
    node.className = rebuilt.className;
    clear(node);
    while (rebuilt.firstChild) node.appendChild(rebuilt.firstChild);
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
          actions: [createButton({ label: S.MODELS_FORM_NEW, variant: 'primary', onClick: () => startCreate({ show: true }) }).el],
        }).el,
      );
      return;
    }
    const ul = el('ul', { class: 'models__list' });
    patchList(ul, models, (m) => m.id, createRow, refreshRow);
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
      [idField, protocolField, apiModeField, modelField, urlField, keyEnvField, noteField, saveBtn, cancelEditBtn].forEach((c) => c.destroy());
    },
  };
}
