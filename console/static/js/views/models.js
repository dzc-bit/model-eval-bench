/**
 * models.js — 模型档案视图（供应商 → 模型清单两层结构）
 *
 * 设计依据：specs/模型配置重构-2026-10-01.md，对齐 DeepSeek Harness 的
 * provider / model 分层——端点、协议、密钥属于**供应商**；上下文容量与输出
 * 上限属于**模型**。同一供应商下加模型不必重复填 base_url，密钥也只需一把。
 *
 * 页面结构：
 *   供应商卡（展示名 / 协议 / 端点 / 密钥状态 / 连接测试）
 *     └─ 卡内模型清单（id / 展示名 / 上下文 / 输出上限）
 *   编辑用弹窗：上半段供应商信息，下半段模型清单（可增删行）+ 从端点拉取
 *
 * 依赖：core/*、components/*
 * 导出：createModels(props) → { el, destroy, el_h1, getModels }
 */

import { el, setText, clear, patchList } from '../core/dom.js';
import { S, t, PROTOCOL_NAMES, API_MODE_NAMES } from '../core/strings.js';
import { api, errorTitle, errorBody } from '../core/api.js';
import { announce } from '../core/a11y.js';
import { createButton } from '../components/button.js';
import { createField } from '../components/field.js';
import { createBadge } from '../components/badge.js';
import { createEmptyState } from '../components/empty-state.js';
import { createSkeleton } from '../components/skeleton.js';
import { createIcon } from '../components/icons.js';
import { openModal } from '../components/modal.js';
import { confirmDialog } from '../components/confirm-dialog.js';

/** 本视图文案（strings.js 本轮冻结，新增一律走本地常量）。 */
const T = {
  DESC: '一个供应商下可以放多个模型，它们共用接口地址与密钥。',

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
  KEY_MISSING: '未配置密钥',
  MODEL_COUNT: '{n} 个模型',
  NO_MODELS: '还没有模型，点「从端点拉取」或「添加一行」。',

  FIELD_ID: '供应商编号',
  FIELD_ID_HINT: '英文标识，用作 run 记录里模型名的前缀。',
  FIELD_ID_READONLY_HINT: '保存后不可修改。',
  FIELD_DISPLAY: '供应商名称',
  FIELD_DISPLAY_REQUIRED: '请填写供应商名称。',
  FIELD_URL: '接口地址',
  FIELD_URL_HINT: '该供应商下所有模型共用。',
  FIELD_URL_REQUIRED: '请填写接口地址。',
  FIELD_URL_INVALID: '要以 http:// 或 https:// 开头。',
  FIELD_KEY_PLACEHOLDER_NEW: '粘贴 API 密钥',
  FIELD_KEY_PLACEHOLDER_KEEP: '留空则保持已存密钥',
  FIELD_KEY_HINT: '只保存在本机，不会进 git。该供应商下的模型共用这一把。',
  FIELD_KEY_MISSING_HINT: '这个供应商还没有可用密钥。填一把再拉取模型。',

  FIELD_MODELS: '模型清单',
  FIELD_MODELS_HINT: '模型名填「请求时发给服务商的名称」。显示名与容量在每行的展开项里，留空就用默认。',
  MODEL_ID: '模型名',
  MODEL_NAME: '显示名',
  MODEL_CTX: '上下文窗口',
  MODEL_MAX: '输出上限',
  MODEL_ADD: '添加一行',
  MODEL_REMOVE: '删除这一行',
  MODEL_ID_REQUIRED: '模型名不能为空。',
  MODEL_ID_DUP: '同一个供应商下模型名不能重复。',
  MODEL_INHERIT: '默认',
  MODEL_ADVANCED: '显示名与容量',

  DISCOVER: '从端点拉取',
  DISCOVERING: '正在拉取…',
  DISCOVER_TITLE: '端点返回的模型',
  DISCOVER_EMPTY: '端点没有返回任何模型。',
  DISCOVER_HINT: '勾选要加进清单的。已在清单里的不会再列一遍。',
  DISCOVER_ADD: '加入清单（{n}）',
  DISCOVER_CONFIGURED: '已在清单',
  DISCOVER_SEARCH_PLACEHOLDER: '搜索模型名…',
  DISCOVER_SELECT_ALL: '全选当前结果',
  DISCOVER_NO_MATCH: '没有匹配的模型。',
  DISCOVER_FAIL: '拉取失败：{reason}',
  DISCOVER_NEED_KEY: '先填 API 密钥再拉取；填完不再改动的话，保存前也能拉到。',

  FORM_INVALID: '表单还有错误，请看标红的字段。',
  TIME_JUST_NOW: '刚刚',
  TIME_MINUTES: '{n} 分钟前',
  TIME_HOURS: '{n} 小时前',
  DELETE_TITLE: '删除供应商「{id}」？',
  DELETE_BODY_1: '它下面的 {n} 个模型会一起消失，已粘贴的密钥也会清除。',
  DELETE_BODY_2: '历史记录仍然保留，但记分板会把它当作未知档案。这不能撤销。',
  FORM_NEW: '新增供应商',
  FORM_EDIT: '编辑供应商',
  MIGRATED: '旧版按模型平铺的配置已按接口地址合并成供应商，保存后新结构生效。',
  PRIVACY: '密钥只存在本机，不入 git。',
};

/** doctor 档位 → 中文（POST /api/models/test 返回的 stages[].id）。 */
const STAGE_LABELS = { key: '密钥', base_url: '接口地址', reach: '连接', model: '模型名' };

/** 协议选项。 */
const PROTOCOL_OPTIONS = Object.entries(PROTOCOL_NAMES).map(([value, label]) => ({ value, label }));
/** OpenAI 兼容 endpoint；native 仅用于非 OpenAI 协议。 */
const OPENAI_API_MODE_OPTIONS = ['responses', 'chat_completions', 'completions']
  .map((value) => ({ value, label: API_MODE_NAMES[value] }));
const NATIVE_API_MODE_OPTIONS = [{ value: 'native', label: API_MODE_NAMES.native }];

/** 供应商编号合法性：小写字母、数字、连字符。 */
const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
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

  let providers = [];
  /** 展开后的扁平模型列表（工作台下拉用），随 providers 一起更新。 */
  let flatModels = [];
  let loading = true;
  let error = null;
  let migrated = false;
  /** 正在编辑的供应商 id；null 表示新建。 */
  let editingId = null;
  /** 编辑态下原条目的脱敏密钥，保存时原样带回（后端不保存明文）。 */
  let editingKeyMasked = '';
  /** 本会话内每个供应商的最近一次连接测试结果：id → {ok, n?, text, title?, at}。 */
  const testResults = new Map();
  /** 正在测试连接的供应商 id。 */
  const testing = new Set();
  /** 当前打开的表单弹窗句柄。 */
  let formModal = null;
  /** 表单里的模型行草稿：{key, id, name, context_window, max_tokens}。 */
  let modelRows = [];
  let rowSeq = 0;

  const h1 = el('h1', { tabindex: '-1' }, S.MODELS_TITLE);
  const listHost = el('div', { class: 'models__list' });
  const migrateNote = el('p', { class: 'models__migrated', role: 'note', hidden: true }, T.MIGRATED);

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
    migrateNote,
    listHost,
  );

  // ==================================================================
  // 表单（供应商 + 模型清单）
  // ==================================================================

  let idTouched = false;

  const idField = createField({
    label: T.FIELD_ID,
    name: 'provider-id',
    required: true,
    hint: T.FIELD_ID_HINT,
    onInput: () => {
      idTouched = true;
      idField.update({ error: '' });
    },
  });
  const displayField = createField({
    label: T.FIELD_DISPLAY,
    name: 'provider-display',
    required: true,
    hint: T.FIELD_DISPLAY_HINT,
    onInput: (value) => {
      // 新建且编号没手动改过时跟着名称建议
      if (!editingId && !idTouched) idField.update({ value: suggestId(value), error: '' });
    },
  });
  const protocolField = createField({
    label: S.MODELS_FIELD_PROTOCOL,
    name: 'provider-protocol',
    type: 'select',
    options: PROTOCOL_OPTIONS,
    value: 'openai',
    onChange: (value) => syncApiModeControl(value),
  });
  const apiModeField = createField({
    label: S.MODELS_FIELD_API_MODE,
    name: 'provider-api-mode',
    type: 'select',
    options: OPENAI_API_MODE_OPTIONS,
    value: 'chat_completions',
    hint: S.MODELS_FIELD_API_MODE_HINT,
  });
  const urlField = createField({
    label: T.FIELD_URL,
    name: 'provider-url',
    type: 'url',
    required: true,
    placeholder: 'https://api.example.com/v1',
    hint: T.FIELD_URL_HINT,
    onInput: () => urlField.update({ error: '' }),
  });
  const apiKeyField = createField({
    label: S.MODELS_FIELD_KEY,
    name: 'provider-key',
    type: 'password',
    placeholder: T.FIELD_KEY_PLACEHOLDER_NEW,
    hint: T.FIELD_KEY_HINT,
  });

  // ---- 模型清单编辑区 ----
  const rowsHost = el('div', { class: 'models-form__rows' });
  const rowsError = el('p', { class: 'field__error', role: 'alert' });

  /**
   * 造一个带可见标签的输入格。
   * 标签直接挂在每个输入上方，不用「表头 + 网格对齐」——那种排法在窄屏与
   * 长模型名下都会错位，而且第一眼看不出哪一列是什么。
   */
  function labeledInput(row, key, opts) {
    const input = el('input', {
      class: 'models-form__cell-input',
      type: opts.type || 'text',
      value: row[key] == null ? '' : String(row[key]),
      placeholder: opts.placeholder || '',
      'aria-label': opts.label,
    });
    input.addEventListener('input', () => {
      row[key] = input.value;
      rowsError.textContent = '';
    });
    return el(
      'label',
      { class: 'models-form__cell' + (opts.wide ? ' models-form__cell--wide' : '') },
      el('span', { class: 'models-form__cell-label' }, opts.label),
      input,
    );
  }

  /**
   * 渲染一行模型（卡片式：一行一张小卡，自带标签与删除）。
   * @param {{key:number,id:string,name:string,context_window:string,max_tokens:string}} row
   */
  function renderRow(row) {
    const removeBtn = createButton({
      label: '',
      icon: '×',
      variant: 'ghost',
      size: 'sm',
      ariaLabel: T.MODEL_REMOVE,
      onClick: () => {
        modelRows = modelRows.filter((r) => r.key !== row.key);
        renderRows();
      },
    });
    const node = el(
      'div',
      { class: 'models-form__row', dataset: { key: String(row.key) } },
      el('div', { class: 'models-form__row-main' },
        labeledInput(row, 'id', { label: T.MODEL_ID, placeholder: 'cbcn/hy4-preview', wide: true }),
        removeBtn.el,
      ),
      el('details', { class: 'models-form__row-advanced' },
        el('summary', {}, T.MODEL_ADVANCED),
        el('div', { class: 'models-form__row-grid' },
          labeledInput(row, 'name', { label: T.MODEL_NAME, placeholder: T.MODEL_NAME_PLACEHOLDER }),
          labeledInput(row, 'context_window', { label: T.MODEL_CTX, type: 'number', placeholder: '留空跟随默认' }),
          labeledInput(row, 'max_tokens', { label: T.MODEL_MAX, type: 'number', placeholder: '留空跟随默认' }),
        ),
      ),
    );
    rowNodes.set(row.key, node);
    return node;
  }

  const rowNodes = new Map();

  function renderRows() {
    rowsHost.textContent = '';
    rowNodes.clear();
    modelRows.forEach((row) => rowsHost.appendChild(renderRow(row)));
    if (!modelRows.length) {
      rowsHost.appendChild(el('p', { class: 'u-faint' }, T.NO_MODELS));
    }
  }

  const addRowBtn = createButton({
    label: T.MODEL_ADD,
    variant: 'ghost',
    size: 'sm',
    icon: '+',
    onClick: () => {
      rowSeq += 1;
      modelRows.push({ key: rowSeq, id: '', name: '', context_window: '', max_tokens: '' });
      renderRows();
    },
  });

  const discoverBtn = createButton({
    label: T.DISCOVER,
    variant: 'ghost',
    size: 'sm',
    onClick: () => runDiscover(),
  });
  const discoverHost = el('div', { class: 'models-form__discover' });

  const modelsBlock = el(
    'div',
    { class: 'models-form__models' },
    el('div', { class: 'models-form__models-head' },
      el('h3', { class: 'models-form__subtitle' }, T.FIELD_MODELS),
      el('span', { class: 'u-spacer' }),
      discoverBtn.el,
      addRowBtn.el,
    ),
    el('p', { class: 'field__hint' }, T.FIELD_MODELS_HINT),
    rowsHost,
    rowsError,
    discoverHost,
  );

  const saveBtn = createButton({ label: S.MODELS_SAVE, variant: 'primary', onClick: () => save() });
  const cancelBtn = createButton({ label: S.ACTION_CANCEL, onClick: () => closeForm() });

  const formError = el('p', { class: 'field__error', role: 'alert' });
  const privacyNote = el('p', { class: 'u-faint models-form__privacy' }, T.PRIVACY);
  const formBody = el(
    'div',
    { class: 'models-form' },
    formError,
    displayField.el,
    urlField.el,
    apiKeyField.el,
    el('div', { class: 'models-form__pair' }, protocolField.el, apiModeField.el),
    idField.el,
    modelsBlock,
    privacyNote,
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

  /** 由名称建议供应商编号：小写字母数字连字符，数字开头补 p- 前缀。 */
  function suggestId(name) {
    const slug = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (!slug) return '';
    return /^[0-9]/.test(slug) ? `p-${slug}` : slug;
  }

  /**
   * 打开新增 / 编辑弹窗。
   * @param {object|null} p 供应商对象；null 表示新建
   */
  function openForm(p) {
    editingId = p ? p.id : null;
    editingKeyMasked = p ? (p.key_masked || '') : '';
    idTouched = Boolean(p);
    setText(formError, '');
    rowsError.textContent = '';
    discoverHost.textContent = '';

    displayField.update({ value: p ? (p.display_name || '') : '', error: '' });
    urlField.update({ value: p ? (p.base_url || '') : '', error: '' });
    // 留空 = 保留已存密钥。有已存密钥时才用「留空则保持」的占位符；
    // 没有的话说清楚要填——之前两种情况共用同一句占位符，
    // 用户以为不用填，结果拉取时发的是无密钥/旧密钥请求。
    const hasStoredKey = Boolean(p && p.key_present);
    apiKeyField.update({
      value: '',
      error: '',
      placeholder: hasStoredKey ? T.FIELD_KEY_PLACEHOLDER_KEEP : T.FIELD_KEY_PLACEHOLDER_NEW,
      hint: hasStoredKey ? T.FIELD_KEY_HINT : T.FIELD_KEY_MISSING_HINT,
    });
    idField.update({
      value: p ? p.id : '',
      error: '',
      disabled: Boolean(p),
      hint: p ? T.FIELD_ID_READONLY_HINT : T.FIELD_ID_HINT,
    });
    const protocol = p ? (p.protocol || 'openai') : 'openai';
    protocolField.update({ value: protocol });
    syncApiModeControl(protocol, p ? (p.api_mode || 'chat_completions') : 'chat_completions');

    // 模型行草稿：容量留空表示跟随默认（不把继承来的值回填成显式值，
    // 否则用户改默认值后老模型不会跟着变）
    modelRows = ((p && p.models) || []).map((m) => {
      rowSeq += 1;
      return {
        key: rowSeq,
        id: m.id || '',
        name: m.name && m.name !== m.id ? m.name : '',
        context_window: m.context_window == null ? '' : String(m.context_window),
        max_tokens: m.max_tokens == null ? '' : String(m.max_tokens),
      };
    });
    renderRows();

    formModal = openModal({
      title: p ? `${T.FORM_EDIT}：${p.id}` : T.FORM_NEW,
      body: formBody,
      footer: [cancelBtn.el, saveBtn.el],
      initialFocus: displayField.getControl(),
      onClose: () => { formModal = null; },
    });
  }

  function closeForm() {
    if (formModal) formModal.close('cancel');
  }

  /**
   * 从端点拉取模型清单（候选，不落盘）。
   * 对齐 DSH 的 discovery 语义：拉到的只是建议，勾选后才进配置。
   */
  async function runDiscover() {
    const baseUrl = urlField.getValue().trim();
    if (!baseUrl) {
      urlField.update({ error: T.FIELD_URL_REQUIRED });
      return;
    }
    urlField.update({ error: '' });
    // 拉取要用密钥。本机没存过、表单里也没填时先说清楚——
    // 否则请求会带着空密钥出去，回一个 401 让人以为是地址写错了。
    const typedKey = apiKeyField.getValue().trim();
    const editingProvider = providers.find((x) => x.id === editingId);
    const hasKey = Boolean(typedKey) || Boolean(editingProvider && editingProvider.key_present);
    if (!hasKey) {
      discoverHost.textContent = '';
      discoverHost.appendChild(el('p', { class: 'field__error', role: 'alert' }, T.DISCOVER_NEED_KEY));
      apiKeyField.update({ error: T.DISCOVER_NEED_KEY });
      return;
    }
    discoverHost.textContent = '';
    discoverBtn.update({ loading: true, busyLabel: T.DISCOVERING });
    try {
      const res = await api.post('/providers/discover', {
        id: idField.getValue().trim(),
        base_url: baseUrl,
        protocol: protocolField.getValue(),
        api_key: typedKey,
      }, { scope });
      renderDiscover(res && res.models ? res.models : []);
    } catch (err) {
      // 优先用服务端给的 message/detail：那里面写的是真实原因（连不上、HTTP 401…），
      // 只按 code 查通用文案会把「连不上端点」显示成「档案没有保存」，误导排查方向。
      const reason = (err && err.message) || errorTitle(err && err.code ? err.code : 'INTERNAL');
      const detail = (err && err.detail) || '';
      discoverHost.appendChild(
        el('div', { class: 'field__error', role: 'alert' },
          el('p', {}, T.DISCOVER_FAIL.replace('{reason}', reason)),
          detail ? el('p', { class: 'u-faint' }, detail) : null,
        ),
      );
    } finally {
      discoverBtn.update({ loading: false });
    }
  }

  /** 渲染拉取结果：勾选框列表，已存在的标注并禁用。 */
  function renderDiscover(candidates) {
    discoverHost.textContent = '';
    if (!candidates.length) {
      discoverHost.appendChild(el('p', { class: 'u-faint' }, T.DISCOVER_EMPTY));
      return;
    }
    const picked = new Set();
    const rows = [];
    const list = el('div', { class: 'models-form__discover-list' });

    const addPickedBtn = createButton({
      label: t(T.DISCOVER_ADD, { n: 0 }),
      variant: 'primary',
      size: 'sm',
      onClick: () => {
        candidates.filter((m) => picked.has(m.id)).forEach((m) => {
          rowSeq += 1;
          modelRows.push({
            key: rowSeq,
            id: m.id,
            name: '',
            context_window: m.context_window ? String(m.context_window) : '',
            max_tokens: m.max_tokens ? String(m.max_tokens) : '',
          });
        });
        renderRows();
        discoverHost.textContent = '';
      },
    });
    addPickedBtn.update({ disabled: true });

    /** 勾选状态变化后统一刷新按钮与全选态。 */
    const syncPicked = () => {
      addPickedBtn.update({ label: t(T.DISCOVER_ADD, { n: picked.size }), disabled: picked.size === 0 });
      const selectable = rows.filter((r) => !r.already);
      const allOn = selectable.length > 0 && selectable.every((r) => r.box.checked);
      selectAllBox.checked = allOn;
      selectAllBox.indeterminate = !allOn && picked.size > 0;
    };

    // 搜索：端点返回几十上百个模型时，逐个找太慢
    const searchInput = el('input', {
      class: 'models-form__discover-search',
      type: 'search',
      placeholder: T.DISCOVER_SEARCH_PLACEHOLDER,
      'aria-label': T.DISCOVER_SEARCH_PLACEHOLDER,
    });

    // 全选：只作用于当前**筛选后可见**的行。筛了 "claude" 再点全选，
    // 用户要的是"这批 claude 都要"，不是把没显示出来的也选上。
    const selectAllBox = el('input', {
      type: 'checkbox',
      class: 'models-form__discover-check',
      'aria-label': T.DISCOVER_SELECT_ALL,
    });
    const selectAllLabel = el(
      'label',
      { class: 'models-form__discover-selectall' },
      selectAllBox,
      el('span', {}, T.DISCOVER_SELECT_ALL),
    );

    candidates.forEach((m) => {
      const already = Boolean(m.configured) || modelRows.some((r) => r.id === m.id);
      const box = el('input', {
        type: 'checkbox',
        class: 'models-form__discover-check',
        'aria-label': m.id,
        disabled: already,
      });
      if (!already) {
        box.addEventListener('change', () => {
          if (box.checked) picked.add(m.id);
          else picked.delete(m.id);
          syncPicked();
        });
      }
      const row = el(
        'label',
        { class: 'models-form__discover-row' + (already ? ' is-configured' : '') },
        box,
        el('span', { class: 'models-form__discover-id' }, m.id),
        el('span', { class: 'u-faint' },
          [m.context_window ? `上下文 ${fmtTokens(m.context_window)}` : '',
           m.max_tokens ? `输出 ${fmtTokens(m.max_tokens)}` : ''].filter(Boolean).join(' · ')),
        already ? createBadge({ label: T.DISCOVER_CONFIGURED, variant: 'neutral' }).el : null,
      );
      rows.push({ id: m.id, lower: m.id.toLowerCase(), box, node: row, already });
      list.appendChild(row);
    });

    /** 按关键词筛行：只改显隐，不重建节点（重建会丢掉已勾选状态）。 */
    const applyFilter = () => {
      const q = searchInput.value.trim().toLowerCase();
      let visible = 0;
      rows.forEach((r) => {
        const hit = !q || r.lower.includes(q);
        r.node.hidden = !hit;
        if (hit) visible += 1;
      });
      emptyHint.hidden = visible > 0;
      syncPicked();
    };
    searchInput.addEventListener('input', applyFilter);

    // 全选：切换当前可见且未在清单里的行
    selectAllBox.addEventListener('change', () => {
      const on = selectAllBox.checked;
      rows.forEach((r) => {
        if (r.already || r.node.hidden) return;
        r.box.checked = on;
        if (on) picked.add(r.id);
        else picked.delete(r.id);
      });
      syncPicked();
    });

    const emptyHint = el('p', { class: 'u-faint', hidden: true }, T.DISCOVER_NO_MATCH);

    discoverHost.append(
      el('div', { class: 'models-form__discover-head' },
        el('h4', {}, T.DISCOVER_TITLE),
        el('span', { class: 'u-faint' }, T.DISCOVER_HINT),
      ),
      el('div', { class: 'models-form__discover-bar' }, searchInput, selectAllLabel),
      list,
      emptyHint,
      el('div', { class: 'u-row' }, addPickedBtn.el),
    );
    syncPicked();
  }

  /** 大数字可读化：262144 → 256k。 */
  function fmtTokens(n) {
    const v = Number(n) || 0;
    if (v >= 1000 && v % 1000 === 0) return `${v / 1000}k`;
    if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
    return String(v);
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
    } else if (!editingId && providers.some((p) => p.id === id)) {
      idField.update({ error: S.MODELS_FIELD_ID_DUP });
      ok = false;
    }
    if (!displayField.getValue().trim()) {
      displayField.update({ error: T.FIELD_DISPLAY_REQUIRED });
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
    // 模型清单：至少一行、每行都要有模型名、不能重复
    const ids = modelRows.map((r) => String(r.id || '').trim()).filter(Boolean);
    if (!ids.length) {
      rowsError.textContent = T.MODEL_ID_REQUIRED;
      ok = false;
    } else if (new Set(ids).size !== ids.length) {
      rowsError.textContent = T.MODEL_ID_DUP;
      ok = false;
    } else if (modelRows.some((r) => !String(r.id || '').trim())) {
      rowsError.textContent = T.MODEL_ID_REQUIRED;
      ok = false;
    }
    return ok;
  }

  /** 保存供应商；成功后关弹窗、刷新列表并自动测一次连接。 */
  async function save() {
    setText(formError, '');
    if (!validate()) {
      announce(T.FORM_INVALID, { assertive: true });
      return;
    }
    const payload = {
      id: idField.getValue().trim(),
      display_name: displayField.getValue().trim(),
      protocol: protocolField.getValue(),
      api_mode: apiModeField.getValue(),
      base_url: urlField.getValue().trim(),
      models: modelRows
        .filter((r) => String(r.id || '').trim())
        .map((r) => ({
          id: String(r.id).trim(),
          name: String(r.name || '').trim(),
          context_window: String(r.context_window || '').trim() || null,
          max_tokens: String(r.max_tokens || '').trim() || null,
        })),
      // 粘贴了新密钥才传 api_key；留空则后端保留已存密钥。config.json 只存脱敏值
      key_masked: editingKeyMasked,
      api_key: apiKeyField.getValue().trim(),
      previous_id: editingId || '',
    };

    saveBtn.update({ loading: true, busyLabel: S.ACTION_SAVED });
    try {
      // 契约：POST 新建、PATCH 覆盖，id 一律走请求体，没有 /providers/{id} 这样的路径
      if (editingId) {
        await api.patch('/providers', payload, { scope });
      } else {
        await api.post('/providers', payload, { scope });
      }
      closeForm();
      await load();
      announce(S.MODELS_SAVED);
      // 保存后自动测一次连接：结果标在卡上，失败不弹 toast（免得打扰）
      runTest(payload.id, { quiet: true });
    } catch (err) {
      setText(formError, err && err.code ? errorTitle(err.code) : errorTitle('INTERNAL'));
      saveBtn.update({ loading: false });
    }
  }

  // ==================================================================
  // 连接测试
  // ==================================================================

  /** 相对时间：刚刚 / N 分钟前 / N 小时前。 */
  function timeAgo(ts) {
    const diff = Date.now() - Number(ts || 0);
    if (diff < 60_000) return T.TIME_JUST_NOW;
    if (diff < 3_600_000) return t(T.TIME_MINUTES, { n: Math.floor(diff / 60_000) });
    return t(T.TIME_HOURS, { n: Math.floor(diff / 3_600_000) });
  }

  /**
   * 跑一次分档体检。
   * @param {string} id 供应商 id
   * @param {{quiet?: boolean}} [opts]
   */
  async function runTest(id, { quiet = false } = {}) {
    if (testing.has(id)) return;
    testing.add(id);
    render();
    if (!quiet) announce(T.TEST_ANNOUNCE_START);
    try {
      // 体检接口按「档案」粒度：取该供应商下的第一个模型作为代表
      const provider = providers.find((p) => p.id === id);
      const first = provider && provider.models && provider.models[0];
      const res = await api.post('/models/test', { id: first ? first.id : id }, { scope });
      const stages = (res && res.stages) || [];
      const bad = stages.find((s) => !s.ok);
      if (res && res.ok) {
        testResults.set(id, { ok: true, n: stages.length, at: Date.now() });
        if (!quiet) announce(T.TEST_ANNOUNCE_OK);
      } else {
        const stage = bad ? (STAGE_LABELS[bad.id] || bad.id) : '';
        const detail = bad ? String(bad.detail || '').slice(0, 80) : '';
        testResults.set(id, {
          ok: false,
          text: bad ? T.TEST_FAIL.replace('{stage}', stage).replace('{detail}', detail)
                    : T.TEST_FAIL_NET.replace('{title}', errorTitle((res && res.error) || 'INTERNAL')),
          title: bad ? [bad.detail, bad.hint].filter(Boolean).join(' — ') : '',
          at: Date.now(),
        });
        if (!quiet) announce(T.TEST_ANNOUNCE_FAIL);
      }
    } catch (err) {
      testResults.set(id, {
        ok: false,
        text: T.TEST_FAIL_NET.replace('{title}', errorTitle(err && err.code ? err.code : 'INTERNAL')),
        at: Date.now(),
      });
      if (!quiet) announce(T.TEST_ANNOUNCE_FAIL);
    } finally {
      testing.delete(id);
      render();
    }
  }

  /** 卡上的测试结果一行。 */
  function renderTestLine(state) {
    if (!state) return null;
    const text = state.ok
      ? (state.n ? t(T.TEST_OK, { n: state.n, time: timeAgo(state.at) })
                 : t(T.TEST_OK_FALLBACK, { time: timeAgo(state.at) }))
      : state.text;
    const node = el(
      'p',
      { class: 'model-card__test' + (state.ok ? ' model-card__test--ok' : ' model-card__test--fail'), role: 'status' },
      text,
    );
    if (state.title) node.title = state.title;
    return node;
  }

  // ==================================================================
  // 列表渲染
  // ==================================================================

  /** 一张供应商卡：头部一行信息 + 动作，下面是模型清单。 */
  function renderCard(p) {
    const keyBadge = createBadge({
      label: p.key_present ? T.KEY_PRESENT : T.KEY_MISSING,
      variant: p.key_present ? 'success' : 'neutral',
      glyph: p.key_present ? '✓' : '',
    });
    const actions = el('div', { class: 'model-card__actions' });
    const testBtn = createButton({
      label: testing.has(p.id) ? T.TESTING : T.TEST,
      variant: 'ghost',
      size: 'sm',
      loading: testing.has(p.id),
      onClick: () => runTest(p.id),
    });
    const editBtn = createButton({
      label: S.MODELS_FORM_EDIT,
      variant: 'ghost',
      size: 'sm',
      onClick: () => openForm(p),
    });
    const delBtn = createButton({
      label: S.ACTION_DELETE,
      variant: 'ghost',
      size: 'sm',
      onClick: () => remove(p),
    });
    actions.append(testBtn.el, editBtn.el, delBtn.el);

    const modelList = el(
      'ul',
      { class: 'model-card__models' },
      ...(p.models || []).map((m) => {
        // 容量显示：与供应商默认值相同的标「默认」，一眼看出哪些是继承来的
        const ctxInherit = m.context_window === p.default_context_window;
        const maxInherit = m.max_tokens === p.default_max_tokens;
        return el(
          'li',
          { class: 'model-card__model' },
          el('span', { class: 'model-card__model-id' }, m.id),
          m.name && m.name !== m.id ? el('span', { class: 'model-card__model-name u-faint' }, m.name) : null,
          el('span', { class: 'model-card__model-cap u-faint' },
            `上下文 ${fmtTokens(m.context_window)}${ctxInherit ? `（${T.MODEL_INHERIT}）` : ''} · ` +
            `输出 ${fmtTokens(m.max_tokens)}${maxInherit ? `（${T.MODEL_INHERIT}）` : ''}`),
        );
      }),
    );

    const testLine = renderTestLine(testResults.get(p.id));
    return el(
      'article',
      { class: 'model-card' },
      el('div', { class: 'model-card__row1' },
        el('h2', { class: 'model-card__name' }, p.display_name || p.id),
        createBadge({ label: PROTOCOL_NAMES[p.protocol] || p.protocol, variant: 'neutral' }).el,
        keyBadge.el,
        el('span', { class: 'u-spacer' }),
        actions,
      ),
      el('p', { class: 'model-card__url u-mono' }, p.base_url || '—'),
      el('div', { class: 'model-card__models-head' },
        el('span', { class: 'u-faint' }, t(T.MODEL_COUNT, { n: (p.models || []).length })),
      ),
      modelList,
      testLine,
    );
  }

  const rowSignatures = new WeakMap();

  function createRow(p) {
    const node = renderCard(p);
    rowSignatures.set(node, JSON.stringify(p));
    return node;
  }

  function refreshRow(node, p) {
    const signature = JSON.stringify(p);
    if (rowSignatures.get(node) === signature) return;
    rowSignatures.set(node, signature);
    const rebuilt = renderCard(p);
    node.className = rebuilt.className;
    clear(node);
    while (rebuilt.firstChild) node.appendChild(rebuilt.firstChild);
  }

  /** 三态渲染列表。 */
  function render() {
    listHost.textContent = '';
    migrateNote.hidden = !migrated;
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
          // 页头「新增供应商」是全页唯一强调按钮，这里用幽灵态避免双实心
          actions: [createButton({ label: S.ACTION_RETRY, variant: 'ghost', onClick: () => load() }).el],
        }).el,
      );
      return;
    }
    if (providers.length === 0) {
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
    patchList(ul, providers, (p) => p.id, createRow, refreshRow);
    listHost.appendChild(ul);
  }

  /** 拉取供应商列表（含展开后的扁平模型，供工作台下拉用）。 */
  async function load() {
    loading = true;
    error = null;
    render();
    try {
      const [provRes, modelRes] = await Promise.all([
        api.get('/providers', { scope }),
        api.get('/models', { scope }),
      ]);
      providers = (provRes && provRes.providers) || [];
      migrated = Boolean(provRes && provRes.migrated);
      flatModels = (modelRes && modelRes.models) || [];
      loading = false;
      render();
      if (typeof onChange === 'function') onChange(flatModels);
    } catch (err) {
      loading = false;
      error = err && err.code ? err.code : 'INTERNAL';
      // 临时诊断：渲染阶段的异常会被这里吞掉，打出来才知道真实原因
      if (typeof console !== 'undefined' && console.error) {
        console.error('[models] load 失败：', err);
      }
      render();
    }
  }

  /**
   * 删除供应商（连同它的模型与密钥）。
   * @param {object} p
   */
  async function remove(p) {
    const n = (p.models || []).length;
    const ok = await confirmDialog({
      title: t(T.DELETE_TITLE, { id: p.display_name || p.id }),
      messages: [
        t(T.DELETE_BODY_1, { n }),
        T.DELETE_BODY_2,
      ],
      confirmLabel: S.ACTION_DELETE,
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del('/providers', { scope, params: { id: p.id } });
      await load();
      announce(S.MODELS_DELETED);
    } catch (err) {
      announce(errorTitle(err && err.code ? err.code : 'INTERNAL'), { assertive: true });
    }
  }

  load();

  return {
    el: root,
    el_h1: h1,
    /** 解绑：关掉可能开着的弹窗，避免残留。 */
    destroy() {
      if (formModal) formModal.close('cancel');
      formModal = null;
    },
    /** 展开后的扁平模型列表（工作台下拉用）。 */
    getModels: () => flatModels,
  };
}
