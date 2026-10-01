/**
 * models.js — 模型档案视图（2026-10 改版，specs/ui-revamp-2026-10-01.md §二）
 *
 * 信息架构：
 *   页头：标题 + 一句话说明 + [新增档案]（全页唯一强调按钮）。
 *   档案卡两行：行1 模型名（主字）+ 服务商徽标 + 密钥状态 chip + 幽灵动作组
 *              [测试连接][编辑][删除]；行2 base_url（mono 一行截断）。
 *   卡上有本会话内的最近一次连接测试结果（一行：✓ 全过 / ✗ 具体原因）。
 *   新增/编辑走 openModal 弹窗表单；保存成功后自动触发一次测试连接并标在卡上。
 *
 * 状态：loading / ready / empty / error；表单态：新建 / 编辑 / 校验错误。
 * 键盘：弹窗焦点圈定、Esc 关闭、关闭后焦点还原；删除走二次确认（默认焦点在取消）。
 * ARIA：必填 aria-required、错误 role="alert"、动作按钮 aria-label 带档案名。
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
import { openModal } from '../components/modal.js';

/** 本视图改版新增文案（strings.js 只读，新增词集中在这里；可复用的沿用 S.*）。 */
const T = {
  DESC: '新增档案后，工作台就能用它对话与跑分；密钥只保存在这台电脑上，不会进 git。',
  TEST: '测试连接',
  TESTING: '正在测试连接…',
  TEST_OK: '✓ {n} 项检查全过 · {time}',
  TEST_OK_FALLBACK: '✓ 全部通过 · {time}',
  TEST_FAIL: '✗ {stage}：{detail}',
  TEST_FAIL_NET: '✗ {title}',
  TEST_ANNOUNCE_OK: '连接测试通过。',
  TEST_ANNOUNCE_FAIL: '连接测试未通过。',
  TEST_ANNOUNCE_START: '正在测试连接。',
  KEY_PRESENT: '已存密钥',
  KEY_MISSING: '未配置',
  FIELD_MODEL: '模型名称',
  FIELD_MODEL_HINT: '调用时填的模型名，与服务商文档里的 model 一致。',
  FIELD_MODEL_REQUIRED: '请填写模型名称。',
  FIELD_URL: '接口地址',
  FIELD_URL_HINT: '一般以 /v1 结尾。',
  FIELD_URL_REQUIRED: '请填写接口地址。',
  FIELD_URL_INVALID: '接口地址要以 http:// 或 https:// 开头。',
  FIELD_KEY_HINT: '只保存在这台电脑上，不会进 git；编辑时留空表示保留已存密钥。',
  FIELD_ID_READONLY_HINT: '档案编号保存后不可修改。',
  FORM_INVALID: '表单还有错误，请看标红的字段。',
};

/** doctor 档位 → 中文（POST /api/models/test 返回的 stages[].id）。 */
const STAGE_LABELS = { key: '密钥', base_url: '接口地址', reach: '连接', model: '模型名' };

/** 协议选项。 */
const PROTOCOL_OPTIONS = Object.entries(PROTOCOL_NAMES).map(([value, label]) => ({ value, label }));
/** OpenAI 兼容 endpoint；native 仅用于非 OpenAI 协议。 */
const OPENAI_API_MODE_OPTIONS = ['responses', 'chat_completions', 'completions']
  .map((value) => ({ value, label: API_MODE_NAMES[value] }));
const NATIVE_API_MODE_OPTIONS = [{ value: 'native', label: API_MODE_NAMES.native }];

/** 档案编号合法性：小写字母、数字、连字符。 */
const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
// 与服务端 upsert_model 的 key_env 校验同规则：环境变量名，不是密钥本身
const KEY_ENV_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;
// base_url 必须是显式的 http(s) 地址（与服务端 chat.has_usable_base_url 同口径）
const URL_PATTERN = /^https?:\/\//i;

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
  /** 本会话内每个档案的最近一次连接测试结果：id → {ok, n?, text, title?, at}。 */
  const testResults = new Map();
  /** 正在测试连接的档案 id。 */
  const testing = new Set();
  /** 当前打开的表单弹窗句柄。 */
  let formModal = null;

  const h1 = el('h1', { tabindex: '-1' }, S.MODELS_TITLE);
  const listHost = el('div', { class: 'models__list' });

  const addBtn = createButton({
    label: S.MODELS_FORM_NEW,
    variant: 'primary',
    onClick: () => openForm(null),
  });

  const root = el(
    'div',
    { class: 'view models' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, T.DESC)),
      el('div', { class: 'view__actions' }, addBtn.el),
    ),
    listHost,
  );

  // ---- 弹窗表单 ----
  /** 用户是否手动改过档案编号；没动过就跟着模型名自动建议（仅新建）。 */
  let idTouched = false;

  const idField = createField({
    label: S.MODELS_FIELD_ID,
    name: 'model-id',
    required: true,
    hint: S.MODELS_FIELD_ID_HINT,
    onInput: () => {
      idTouched = true;
      idField.update({ error: '' });
    },
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
    label: T.FIELD_MODEL,
    name: 'model-name',
    required: true,
    hint: T.FIELD_MODEL_HINT,
    onInput: (value) => {
      modelField.update({ error: '' });
      // 新建且编号没手动改过时跟着模型名建议
      if (!editingId && !idTouched) idField.update({ value: suggestId(value), error: '' });
    },
  });
  const urlField = createField({
    label: T.FIELD_URL,
    name: 'model-url',
    type: 'url',
    placeholder: 'https://api.example.com/v1',
    hint: T.FIELD_URL_HINT,
    onInput: () => urlField.update({ error: '' }),
  });
  const apiKeyField = createField({
    label: S.MODELS_FIELD_KEY,
    name: 'model-key',
    type: 'password',
    placeholder: S.MODELS_FIELD_KEY_PLACEHOLDER,
    hint: T.FIELD_KEY_HINT,
    autocomplete: 'new-password',
  });
  const keyEnvField = createField({
    label: S.MODELS_FIELD_KEY_ENV,
    name: 'model-key-env',
    hint: S.MODELS_FIELD_KEY_ENV_HINT,
    placeholder: 'OPENAI_API_KEY',
  });
  // 老档案可能存过备注，后端仍接收 note；不单占一屏，收进高级折叠
  const noteField = createField({
    label: S.MODELS_FIELD_NOTE,
    name: 'model-note',
    type: 'textarea',
    rows: 2,
  });

  const saveBtn = createButton({ label: S.MODELS_SAVE, variant: 'primary', onClick: () => save() });
  const cancelBtn = createButton({ label: S.ACTION_CANCEL, onClick: () => closeForm() });

  const formError = el('p', { class: 'field__error', role: 'alert' });
  const advancedFold = el(
    'details',
    { class: 'details-card models-form__advanced' },
    el('summary', {},
      el('span', { class: 'details-card__marker', 'aria-hidden': 'true' }, '▸'),
      S.MODELS_FORM_ADVANCED),
    el('div', { class: 'details-card__body' },
      protocolField.el,
      apiModeField.el,
      keyEnvField.el,
      noteField.el,
      el('p', { class: 'u-faint' }, S.MODELS_KEY_PRIVACY),
    ),
  );
  const formBody = el(
    'div',
    { class: 'models-form' },
    formError,
    modelField.el,
    urlField.el,
    apiKeyField.el,
    idField.el,
    advancedFold,
  );

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

  /** 由模型名建议档案编号：小写字母数字连字符，数字开头补 m- 前缀。 */
  function suggestId(modelName) {
    const slug = String(modelName || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (!slug) return '';
    return /^[0-9]/.test(slug) ? `m-${slug}` : slug;
  }

  /**
   * 打开新增 / 编辑弹窗。
   * @param {object|null} m 档案对象；null 表示新建
   */
  function openForm(m) {
    editingId = m ? m.id : null;
    editingKeyMasked = m ? (m.key_masked || '') : '';
    idTouched = Boolean(m);
    setText(formError, '');
    modelField.update({ value: m ? (m.model || '') : '', error: '' });
    urlField.update({ value: m ? (m.base_url || '') : '', error: '' });
    apiKeyField.update({ value: '', error: '' }); // 留空 = 保留已保存的密钥
    idField.update({
      value: m ? m.id : '',
      error: '',
      disabled: Boolean(m),
      hint: m ? T.FIELD_ID_READONLY_HINT : S.MODELS_FIELD_ID_HINT,
    });
    const protocol = m ? (m.protocol || 'openai') : 'openai';
    protocolField.update({ value: protocol });
    syncApiModeControl(protocol, m ? (m.api_mode || 'chat_completions') : 'chat_completions');
    keyEnvField.update({ value: m ? (m.key_env || '') : '', error: '' });
    noteField.update({ value: m ? (m.note || '') : '' });
    advancedFold.open = false; // 高级项默认收起
    formModal = openModal({
      title: m ? `${S.MODELS_FORM_EDIT}：${m.id}` : S.MODELS_FORM_NEW,
      body: formBody,
      footer: [cancelBtn.el, saveBtn.el],
      initialFocus: modelField.getControl(),
      onClose: () => { formModal = null; },
    });
  }

  function closeForm() {
    if (formModal) formModal.close('cancel');
  }

  /**
   * 表单校验（必填与格式都在前端给行内错误，不等后端）。
   * @returns {boolean}
   */
  function validate() {
    let ok = true;
    const id = idField.getValue().trim();
    if (!id) {
      idField.update({ error: S.MODELS_FIELD_ID_REQUIRED });
      ok = false;
    } else if (!ID_PATTERN.test(id)) {
      idField.update({ error: S.MODELS_FIELD_ID_INVALID });
      ok = false;
    } else if (!editingId && models.some((m) => m.id === id)) {
      idField.update({ error: S.MODELS_FIELD_ID_DUP });
      ok = false;
    }
    if (!modelField.getValue().trim()) {
      modelField.update({ error: T.FIELD_MODEL_REQUIRED });
      ok = false;
    }
    const url = urlField.getValue().trim();
    if (!url) {
      urlField.update({ error: T.FIELD_URL_REQUIRED });
      ok = false;
    } else if (!URL_PATTERN.test(url)) {
      urlField.update({ error: T.FIELD_URL_INVALID });
      ok = false;
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

  /** 保存档案；成功后关弹窗、刷新列表并自动测一次连接。 */
  async function save() {
    setText(formError, '');
    if (!validate()) {
      announce(T.FORM_INVALID, { assertive: true });
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

    saveBtn.update({ loading: true, busyLabel: S.ACTION_SAVED });
    try {
      // 契约：POST 新建、PATCH 覆盖，id 一律走请求体，没有 /models/{id} 这样的路径
      if (editingId) {
        await api.patch('/models', payload, { scope });
      } else {
        await api.post('/models', payload, { scope });
      }
      const savedId = payload.id;
      if (formModal) formModal.close('saved');
      showToast({ message: t(S.MODELS_SAVED, { id: savedId }), kind: 'success', duration: 4000 });
      await load();
      // 保存后自动测一次连接，结果标在卡上（失败不打断，quiet 模式不再弹错）
      runTest(savedId, { quiet: true });
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'SAVE_FAILED';
      // 服务端对表单类错误会返回一句具体中文（如「key_env 必须是合法的服务端环境变量名。」），
      // 比按码查到的通用标题更能指出错在哪个字段，优先展示
      const backendMessage = err instanceof ApiError ? String(err.message || '').trim() : '';
      const detail = errorBody(code);
      setText(formError, backendMessage || detail);
      showToast({ message: backendMessage || errorTitle(code), detail, kind: 'error' });
    } finally {
      saveBtn.update({ loading: false });
    }
  }

  /** 相对时间：测试结果行尾的「刚刚 / n 分钟前」。 */
  function timeAgo(ts) {
    const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
    if (s < 60) return '刚刚';
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
    return new Date(ts).toLocaleString();
  }

  /**
   * 测试连接（复用服务端 doctor：POST /api/models/test，只带已保存档案的 id）。
   * @param {string} id
   * @param {{quiet?: boolean}} [opts] quiet：保存后的自动测试，失败只标在卡上不弹 toast
   */
  async function runTest(id, { quiet = false } = {}) {
    if (testing.has(id)) return;
    testing.add(id);
    announce(T.TEST_ANNOUNCE_START);
    render();
    try {
      const res = await api.post('/models/test', { id }, { scope });
      const stages = Array.isArray(res && res.stages) ? res.stages : [];
      if (res && res.ok) {
        // 跳过的档位（ok=None）不算失败，也不计入全过数
        const n = stages.filter((s) => s && s.ok === true).length;
        testResults.set(id, { ok: true, n, at: Date.now() });
        announce(T.TEST_ANNOUNCE_OK);
      } else {
        const stage = stages.find((s) => s && s.ok === false) || {};
        const label = STAGE_LABELS[stage.id] || stage.id || '连接';
        const text = t(T.TEST_FAIL, { stage: label, detail: String(stage.detail || stage.hint || '未通过') });
        testResults.set(id, {
          ok: false,
          text,
          title: [stage.detail, stage.hint].filter(Boolean).join('；'),
          at: Date.now(),
        });
        announce(T.TEST_ANNOUNCE_FAIL);
        if (!quiet) showToast({ message: text, kind: 'error' });
      }
    } catch (err) {
      testing.delete(id);
      render();
      if (err instanceof ApiError && err.code === 'ABORTED') return; // 页面切换，静默
      const code = err instanceof ApiError ? err.code : 'ACTION_FAILED';
      const detail = errorBody(code);
      testResults.set(id, {
        ok: false,
        text: t(T.TEST_FAIL_NET, { title: errorTitle(code) }),
        title: detail,
        at: Date.now(),
      });
      announce(T.TEST_ANNOUNCE_FAIL);
      if (!quiet) showToast({ message: errorTitle(code), detail, kind: 'error' });
      return;
    }
    testing.delete(id);
    render();
  }

  /**
   * 删除档案（二次确认，破坏性操作默认焦点在取消）。
   * @param {object} m
   */
  async function remove(m) {
    const id = m.id;
    const ok = await confirmDialog({
      title: t(S.MODELS_DELETE_CONFIRM_TITLE, { id }),
      messages: [S.MODELS_DELETE_CONFIRM_BODY_1, S.MODELS_DELETE_CONFIRM_BODY_2],
      confirmLabel: S.ACTION_DELETE,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    try {
      // 契约：DELETE /api/models?id=xxx；不带 with_runs，跑分记录保留（确认框里已说明）
      await api.del('/models', { scope, params: { id } });
      testResults.delete(id);
      showToast({ message: t(S.MODELS_DELETED, { id }), kind: 'success', duration: 4000 });
      await load();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'ACTION_FAILED';
      showToast({ message: errorTitle(code), detail: errorBody(code), kind: 'error' });
    }
  }

  /** 测试结果行。 */
  function renderTestLine(state) {
    if (state.running) {
      return el('p', { class: 'model-card__test model-card__test--running', 'aria-busy': 'true' }, T.TESTING);
    }
    if (state.ok) {
      const time = timeAgo(state.at);
      const text = state.n
        ? t(T.TEST_OK, { n: state.n, time })
        : t(T.TEST_OK_FALLBACK, { time });
      return el('p', { class: 'model-card__test model-card__test--ok', title: text }, text);
    }
    return el('p', { class: 'model-card__test model-card__test--fail', title: state.title || state.text }, state.text);
  }

  /**
   * 档案卡（两行 + 可选测试结果行）。
   * @param {object} m
   * @returns {HTMLElement}
   */
  function renderRow(m) {
    const protocol = String(m.protocol || 'openai').toLowerCase();
    const state = testing.has(m.id) ? { running: true } : testResults.get(m.id);
    return el(
      'li',
      { class: 'model-card' },
      el('div', { class: 'model-card__row1' },
        el('strong', { class: 'model-card__name', title: m.model ? m.id : '' }, m.model || m.id || '—'),
        createBadge({
          label: PROTOCOL_NAMES[protocol] || protocol || S.PROTOCOL_CUSTOM,
          variant: 'info',
          glyph: '·',
        }).el,
        createBadge({
          label: m.key_masked ? T.KEY_PRESENT : T.KEY_MISSING,
          variant: m.key_masked ? 'success' : 'muted',
        }).el,
        el('div', { class: 'model-card__actions' },
          createButton({
            label: T.TEST,
            size: 'sm',
            variant: 'ghost',
            disabled: testing.has(m.id),
            ariaLabel: `${T.TEST}：${m.model || m.id}`,
            onClick: () => runTest(m.id),
          }).el,
          createButton({
            label: S.ACTION_EDIT,
            size: 'sm',
            variant: 'ghost',
            ariaLabel: `${S.ACTION_EDIT}：${m.id}`,
            onClick: () => openForm(m),
          }).el,
          createButton({
            label: S.ACTION_DELETE,
            size: 'sm',
            variant: 'ghost',
            ariaLabel: `${S.ACTION_DELETE}：${m.id}`,
            onClick: () => remove(m),
          }).el,
        ),
      ),
      el('div', { class: 'model-card__row2' },
        el('code', { class: 'model-card__url', title: m.base_url || '—' }, m.base_url || '—'),
      ),
      state ? renderTestLine(state) : null,
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

  /** 三态渲染列表。 */
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
          // 页头「新增档案」是全页唯一强调按钮，这里用幽灵态避免双实心
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'ghost', onClick: () => load() }).el],
        }).el,
      );
      return;
    }
    if (models.length === 0) {
      listHost.appendChild(
        createEmptyState({
          title: S.MODELS_EMPTY,
          desc: S.MODELS_EMPTY_DESC,
          actions: [createButton({ label: S.MODELS_FORM_NEW, variant: 'ghost', onClick: () => openForm(null) }).el],
        }).el,
      );
      return;
    }
    const ul = el('ul', { class: 'models__list' });
    patchList(ul, models, (m) => m.id, createRow, refreshRow);
    listHost.appendChild(ul);
  }

  /** 拉取档案列表。 */
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

  load();

  return {
    el: root,
    el_h1: h1,
    /** 当前档案列表（供工作台绑定用）。 */
    getModels: () => models,
    /** 解绑 + 关弹窗 + 取消在途请求。 */
    destroy() {
      if (formModal) formModal.destroy();
      scope.cancelAll();
      [idField, protocolField, apiModeField, modelField, urlField, apiKeyField, keyEnvField, noteField,
        saveBtn, cancelBtn, addBtn].forEach((c) => c.destroy());
    },
  };
}
