/**
 * task-node.js — 对话流顶部的「任务与提示词」折叠节点（2026-10-02 对话流改版；
 * 由旧 prompt-panel 迁移而来，从侧栏卡片变成对话流的第一个节点）
 *
 * 职责：
 *   1. 题目一段话简介：默认 3 行截断 + [展开]。
 *   2. 轮次切换（页签）：已解锁可回看，未解锁 aria-disabled 并说明原因。
 *   3. 当前轮提示词默认收起成一行摘要 + [发送到对话] [展开查看]；
 *      正文永不直接铺整段全文（展开后也有独立折叠态）。
 *   4. 「用外部模型跑？」折叠块：一句话说明 + 复制「说明与提示词」（三级降级）。
 *
 * 纪律：
 *   - 正文展开/收起与简介展开状态不被轮询覆盖，只在换轮时把正文收回默认（收起）。
 *   - 复制内容与当前轮次严格对应：切轮次后旧的「已复制」态作废。
 *   - 内置对话只发提示词正文（工作区约束由服务端注入）；「复制」才是导出全量的路径。
 *   - 默认开合由编排层控制（没跑起来的一轮默认展开），用户手动开合不被轮询覆盖。
 *
 * 依赖：core/*、components/*
 * 导出：createTaskNode(handlers) → { el, update, destroy, setOpen, copyPrompt, getPrompt }
 */

import { el, setText, clear } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createTabs } from '../../components/tabs.js';
import { createCodeBlock } from '../../components/code-block.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createEmptyState } from '../../components/empty-state.js';
import { createSkeleton } from '../../components/skeleton.js';
import { createButton } from '../../components/button.js';
import { createDetailsCard } from '../../components/details-card.js';

/** 本节点新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  CARD_TITLE: '任务与提示词',
  SEND_TO_CHAT: '发送到对话',
  EXPAND_BODY: '展开查看',
  COLLAPSE_BODY: '收起正文',
  CHARS: '· {n} 字',
  EXTERNAL_HINT: '复制提示词，到模型官网的对话里粘贴使用；回来后把改动同步进沙箱即可。',
  NO_PROMPT: '这一轮还没有可用的提示词。',
  NO_RUN_ASIDE: '还没有沙箱',
  SEND_NO_PROMPT: '这一轮还没有可用的提示词。',
  SEND_NO_SANDBOX: '先准备沙箱，再把提示词发给模型。',
  SEND_BUSY: '模型仍在处理上一条消息。',
  SEND_GONE: '这一轮绑定的模型档案已被删除。',
  // ponytail: 是否需要「展开」用字数估算（90 字 ≈ 3 行 × 30 字），没做 DOM 测量；
  // 要更准可在 rAF 后比较 scrollHeight 与 clientHeight。
  BRIEF_CLAMP_CHARS: 90,
};

/**
 * 创建「任务与提示词」节点。
 * @param {{
 *   onRoundChange: (n: number) => void,
 *   onSendPrompt: (text: string) => void,
 *   onReload: () => void,
 * }} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, setOpen: Function, copyPrompt: Function, getPrompt: Function}}
 */
export function createTaskNode(handlers) {
  let current = {
    loading: true,
    run: null,
    task: null,
    round: 1,
    error: null,
    /** 发送是否可用与原因（对话在飞 / 档案已删 / 没有沙箱），由编排层算好。 */
    sendDisabled: true,
    sendReason: '',
  };
  /** 上一次渲染的轮次：换轮时提示词正文收回收起态，旧的「已复制」态作废。 */
  let lastRound = 1;
  /** 提示词正文是否展开（用户的选择，轮询不覆盖）。 */
  let bodyOpen = false;
  /** 题目简介是否展开。 */
  let briefOpen = false;

  // ---- 题目简介 ----
  const briefText = el('p', { class: 'ws-brief' });
  const briefBtn = createButton({
    label: S.ACTION_EXPAND,
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      briefOpen = !briefOpen;
      renderBrief();
    },
  });
  // createButton 根节点自带 inline-flex，直接 hidden 藏不掉，套一层容器再切
  const briefActions = el('div', { class: 'u-row' }, briefBtn.el);
  const briefWrap = el('div', { class: 'u-stack ws-brief-wrap' }, briefText, briefActions);

  function renderBrief() {
    const summary = (current.task && current.task.summary) || '';
    briefWrap.hidden = !summary;
    setText(briefText, summary);
    briefText.classList.toggle('ws-brief--clamp', !briefOpen);
    briefBtn.update({ label: briefOpen ? S.ACTION_COLLAPSE : S.ACTION_EXPAND });
    briefActions.hidden = !summary || summary.length <= T.BRIEF_CLAMP_CHARS;
  }

  // ---- 轮次页签 ----
  const tabs = createTabs({
    label: S.PROMPT_ROUND_TAB,
    tabs: [],
    onChange: (id) => {
      const n = Number(id);
      if (Number.isFinite(n) && handlers.onRoundChange) handlers.onRoundChange(n);
    },
  });

  // ---- 当前轮提示词：一行摘要 + 动作；正文默认收起 ----
  const promptTitle = el('span', { class: 'ws-prompt-row__title' });
  const sendBtn = createButton({
    label: T.SEND_TO_CHAT,
    size: 'sm',
    disabled: true,
    onClick: () => handlers.onSendPrompt(bodyText()),
  });
  const bodyToggle = createButton({
    label: T.EXPAND_BODY,
    variant: 'ghost',
    size: 'sm',
    onClick: () => {
      bodyOpen = !bodyOpen;
      renderPromptBody();
    },
  });
  const promptRow = el(
    'div',
    { class: 'ws-prompt-row' },
    promptTitle,
    el('span', { class: 'u-spacer' }),
    sendBtn.el,
    bodyToggle.el,
  );

  const bodyCode = createCodeBlock({ title: '', showCopy: false, text: '', ariaLabel: S.PROMPT_ROUND_1 });
  const copyBodyBtn = createCopyButton({
    label: S.PROMPT_COPY_BODY,
    size: 'sm',
    getText: () => bodyText(),
    successMessage: () => t(S.COPY_OK_BODY, { n: current.round }),
    sourceEl: () => bodyCode.getPre(),
  });
  const promptBody = el(
    'div',
    { class: 'u-stack ws-prompt-body', id: 'ws-prompt-body', hidden: true },
    bodyCode.el,
    el('div', { class: 'u-row' }, copyBodyBtn.el),
  );

  function renderPromptBody() {
    promptBody.hidden = !bodyOpen;
    bodyToggle.update({ label: bodyOpen ? T.COLLAPSE_BODY : T.EXPAND_BODY });
    bodyToggle.getButton().setAttribute('aria-expanded', String(bodyOpen));
    bodyToggle.getButton().setAttribute('aria-controls', 'ws-prompt-body');
  }

  // ---- 用外部模型跑？ ----
  const wiringCode = createCodeBlock({
    title: S.PROMPT_WIRING_TITLE,
    showCopy: false,
    text: '',
    ariaLabel: S.PROMPT_WIRING_TITLE,
  });
  // 不给 sourceEl：导出内容是「说明 + 提示词」拼接文本，页面上没有对应节点，
  // 第三级剪贴板降级会自建 textarea 装全量文本（见 copy-button.js）。
  const externalCopyBtn = createCopyButton({
    label: S.PROMPT_COPY_WIRING,
    size: 'sm',
    getText: () => composeAll(),
    successMessage: () => t(S.COPY_OK_ALL, { n: current.round, len: composeAll().length }),
  });
  const wiringCard = createDetailsCard({
    title: S.PROMPT_WIRING_TITLE,
    open: false,
    content: el(
      'div',
      { class: 'u-stack' },
      el('p', { class: 'u-muted' }, T.EXTERNAL_HINT),
      wiringCode.el,
      el('div', { class: 'u-row' }, externalCopyBtn.el),
    ),
  });

  // ---- 节点骨架：details/summary，卡头即折叠开关 ----
  const cardAside = el('span', { class: 'ws-node__aside u-faint u-truncate' });
  const chevron = el('span', { class: 'ws-node__chevron', 'aria-hidden': 'true' }, '›');
  const cardBody = el('div', { class: 'ws-node__body' });
  const root = el(
    'details',
    { class: 'ws-node ws-tasknode ws-region', id: 'ws-region-prompt' },
    el('summary', { class: 'ws-node__summary' },
      el('span', { class: 'ws-node__title' }, T.CARD_TITLE),
      el('span', { class: 'u-spacer' }),
      cardAside,
      chevron),
    cardBody,
  );

  const skeleton = createSkeleton({ rows: 2, variant: 'card', label: S.STATE_LOADING });
  const errorState = createEmptyState({
    title: S.ERR_LOAD,
    desc: S.ERR_LOAD_BODY,
    alert: true,
    // 重试只做只读回读，绝不顺手触发准备/校验这类写操作
    actions: [createButton({ label: S.ACTION_RETRY, onClick: () => handlers.onReload() }).el],
  });

  /**
   * 「用外部模型跑」的说明文本（契约：任务详情带 wiring_note 固定文案，与轮次无关）。
   * @returns {string}
   */
  function wiringText() {
    return (current.task && current.task.wiring_note) || '';
  }

  /**
   * 当前轮提示词正文（严格对应 current.round；越界一律空串）。
   * @returns {string}
   */
  function bodyText() {
    const prompts = (current.task && current.task.prompts) || [];
    const hit = prompts.find((p) => Number(p.level) === Number(current.round));
    return hit ? String(hit.text || '') : '';
  }

  /**
   * 合并导出文本：说明 + 分隔 + 当前轮提示词。
   * @returns {string}
   */
  function composeAll() {
    const w = wiringText();
    const b = bodyText();
    if (!w) return b;
    if (!b) return w;
    return `${w}\n\n---\n\n${b}`;
  }

  /**
   * 轮次页签定义：后端只回传已解锁的级，「出现即已解锁」。
   * @returns {Array}
   */
  function tabDefs() {
    const run = current.run;
    const max = (current.task && current.task.attempts) || (run && run.attempts_allowed) || 1;
    const levels = ((current.task && current.task.prompts) || []).map((p) => Number(p.level));
    const unlocked = levels.length ? levels : run ? [Number(run.attempt)] : [];
    const out = [];
    for (let i = 1; i <= max; i += 1) {
      const isUnlocked = unlocked.includes(i);
      out.push({
        id: String(i),
        label: t(S.PROMPT_ROUND_GENERIC, { n: i }),
        icon: isUnlocked ? '●' : '○',
        disabled: !isUnlocked,
        badge: run && Number(run.attempt) === i ? S.PROMPT_ROUND_USED_MARK : '',
        hint: isUnlocked ? '' : t(S.PROMPT_ROUND_LOCKED_TITLE, { n: i, prev: Math.max(1, i - 1) }),
      });
    }
    return out;
  }

  /**
   * 轮次对应的小标题（第 1 级症状 / 第 2 级不一致清单 / 第 3 级不变量与否决项）。
   * @param {number} n
   * @returns {string}
   */
  function levelTitle(n) {
    if (n === 2) return t(S.PROMPT_BODY_TITLE_2, { n });
    if (n === 3) return t(S.PROMPT_BODY_TITLE_3, { n });
    return t(S.PROMPT_BODY_TITLE, { n });
  }

  /**
   * 差异更新：只改文本与显隐，不重建根节点（轮询不丢输入与折叠态）。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };

    // 换轮：正文收回收起态（复制按钮的「已复制」态随 label 重置作废）
    if (current.round !== lastRound) {
      bodyOpen = false;
      lastRound = current.round;
      copyBodyBtn.update({ label: t(S.PROMPT_COPY_BODY, { n: current.round }) });
      externalCopyBtn.update({ getText: composeAll });
    }

    // 卡头摘要行：收起时也能读到当前轮与字数
    const text = bodyText();
    if (current.error) setText(cardAside, S.ERR_LOAD);
    else if (current.loading) setText(cardAside, S.STATE_LOADING);
    else if (!current.task) setText(cardAside, '');
    else setText(cardAside, text ? `${levelTitle(current.round)} ${t(T.CHARS, { n: text.length })}` : T.NO_RUN_ASIDE);

    clear(cardBody);
    if (current.error) {
      // 错误不能藏在收起的节点里
      root.open = true;
      errorState.update({});
      cardBody.appendChild(errorState.el);
      return;
    }
    if (current.loading) {
      cardBody.appendChild(skeleton.el);
      return;
    }

    renderBrief();
    cardBody.appendChild(briefWrap);
    tabs.update({ tabs: tabDefs(), selected: String(current.round), label: S.PROMPT_ROUND_TAB });
    cardBody.appendChild(tabs.el);

    setText(promptTitle, text ? `${levelTitle(current.round)} ${t(T.CHARS, { n: text.length })}` : T.NO_PROMPT);
    // 禁用必须带原因：只把按钮变灰，用户读到的是「这个按钮坏了」
    sendBtn.update({
      disabled: !text || Boolean(current.sendDisabled),
      reason: !text ? T.SEND_NO_PROMPT : current.sendReason || '',
    });
    renderPromptBody();
    cardBody.appendChild(promptRow);
    cardBody.appendChild(promptBody);

    bodyCode.update({ text, title: levelTitle(current.round), ariaLabel: levelTitle(current.round) });
    copyBodyBtn.update({ getText: bodyText, label: t(S.PROMPT_COPY_BODY, { n: current.round }) });
    wiringCode.update({ text: wiringText(), title: S.PROMPT_WIRING_TITLE });
    cardBody.appendChild(wiringCard.el);
  }

  update({});

  return {
    el: root,
    update,
    /** 展开/收起节点（焦点跳转前先展开，别把人滚到一个关着的节点上）。 */
    setOpen(open) {
      root.open = Boolean(open);
    },
    /** 快捷键 C：导出「说明 + 当前轮提示词」。 */
    copyPrompt: () => externalCopyBtn.copy(),
    /** 给内置对话的发送内容：只有当前轮提示词正文（约束由服务端注入）。 */
    getPrompt: () => bodyText(),
    /** 解绑（§10.4）。 */
    destroy() {
      tabs.destroy();
      copyBodyBtn.destroy();
      externalCopyBtn.destroy();
      wiringCode.destroy();
      bodyCode.destroy();
      briefBtn.destroy();
      sendBtn.destroy();
      bodyToggle.destroy();
      errorState.destroy();
      skeleton.destroy();
    },
  };
}
