/**
 * prompt-panel.js — 工作台「提示词」区（§9 重点交互 ①）
 *
 * 职责：
 *   1. 轮次切换（tabs）：已解锁可回看，未解锁的页签 aria-disabled 并说明原因。
 *   2. 展示「接线说明」与「当前轮提示词」两块文本。
 *   3. 三条复制路径：复制全部 / 复制接线说明 / 复制第 N 级提示词，
 *      三级降级（clipboard → execCommand → 手动选择），结果播报（§11.2 #8）。
 *   4. 复制内容与当前轮次严格对应：切轮次后旧的「已复制」态会被清掉。
 *
 * 状态：loading / empty（未准备沙箱）/ ready / error。
 * 键盘：页签方向键 + roving tabindex；复制按钮 Enter / Space；焦点不因轮询丢失。
 * ARIA：tablist 语义 + aria-selected + aria-controls；每块文本 role="region" + aria-label。
 *
 * 依赖：core/*、components/*
 * 导出：createPromptPanel(handlers) → { el, update, destroy }
 */

import { el, setText, clear } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createTabs } from '../../components/tabs.js';
import { createCodeBlock } from '../../components/code-block.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createEmptyState } from '../../components/empty-state.js';
import { createSkeleton } from '../../components/skeleton.js';
import { createButton } from '../../components/button.js';

/**
 * 创建提示词区。
 * @param {{onRoundChange: (n: number) => void, onGoSandbox: () => void, onReload: () => void}} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createPromptPanel(handlers) {
  let current = { loading: true, run: null, task: null, round: 1, error: null };
  /** 上一次渲染的轮次，用于判断"已复制"态是否需要作废。 */
  let lastRound = 1;

  const tabs = createTabs({
    label: S.PROMPT_ROUND_TAB,
    tabs: [],
    onChange: (id) => {
      const n = Number(id);
      if (Number.isFinite(n) && handlers.onRoundChange) handlers.onRoundChange(n);
    },
  });

  const copyAllBtn = createCopyButton({
    label: S.PROMPT_COPY_ALL,
    variant: 'primary',
    getText: () => composeAll(),
    successMessage: () => t(S.COPY_OK_ALL, { n: current.round, len: composeAll().length }),
  });

  const copyAllRow = el(
    'div',
    { class: 'prompt__block-head' },
    el('span', { class: 'prompt__block-title' }, S.PROMPT_DESC),
    el('span', { class: 'u-spacer' }),
    copyAllBtn.el,
  );
  const roundHint = el('p', { class: 'u-faint' });

  // ---- 接线说明块 ----
  const wiringCode = createCodeBlock({
    title: S.PROMPT_WIRING_TITLE,
    showCopy: false,
    text: '',
    ariaLabel: S.PROMPT_WIRING_TITLE,
  });
  const copyWiringBtn = createCopyButton({
    label: S.PROMPT_COPY_WIRING,
    size: 'sm',
    getText: () => wiringText(),
    successMessage: () => S.COPY_OK_WIRING,
    // 剪贴板被浏览器拒绝时，第三级降级要能真的选中源文本
    sourceEl: () => wiringCode.getPre(),
  });
  const wiringBlock = el(
    'div',
    { class: 'prompt__block' },
    el(
      'div',
      { class: 'prompt__block-head' },
      el('span', { class: 'prompt__block-title' }, S.PROMPT_WIRING_TITLE),
      el('span', { class: 'u-faint' }, S.PROMPT_WIRING_DESC),
      el('span', { class: 'u-spacer' }),
      copyWiringBtn.el,
    ),
    wiringCode.el,
  );

  // ---- 当前轮提示词块 ----
  const bodyCode = createCodeBlock({
    title: '',
    showCopy: false,
    text: '',
    ariaLabel: S.PROMPT_ROUND_1,
  });
  const copyBodyBtn = createCopyButton({
    label: S.PROMPT_COPY_BODY,
    size: 'sm',
    getText: () => bodyText(),
    successMessage: () => t(S.COPY_OK_BODY, { n: current.round }),
    sourceEl: () => bodyCode.getPre(),
  });
  const bodyTitle = el('span', { class: 'prompt__block-title' });
  const bodyBlock = el(
    'div',
    { class: 'prompt__block' },
    el('div', { class: 'prompt__block-head' }, bodyTitle, el('span', { class: 'u-spacer' }), copyBodyBtn.el),
    bodyCode.el,
  );

  const grid = el('div', { class: 'prompt__grid' }, copyAllRow, roundHint, tabs.el, wiringBlock, bodyBlock);

  const bodyArea = el('div', { class: 'u-stack' });
  const root = el(
    'section',
    { class: 'panel ws-region', id: 'ws-region-prompt', 'aria-labelledby': 'ws-prompt-title' },
    el(
      'div',
      { class: 'panel__head' },
      el('h2', { class: 'panel__title', id: 'ws-prompt-title' }, S.PROMPT_TITLE),
    ),
    bodyArea,
  );

  const emptyState = createEmptyState({
    title: S.PROMPT_EMPTY,
    desc: S.PROMPT_EMPTY_DESC,
    // 这是"跳到沙箱面板"的导航，不是执行准备动作；用 primary 会和真正的
    // 「准备沙箱」按钮在同屏出现两个实心主按钮，读起来像有两个可点的下一步
    actions: [createButton({ label: S.SANDBOX_PREPARE, variant: 'ghost', onClick: () => handlers.onGoSandbox() }).el],
  });
  const skeleton = createSkeleton({ rows: 2, variant: 'card', label: S.STATE_LOADING });
  const errorState = createEmptyState({
    title: S.ERR_LOAD,
    desc: S.ERR_LOAD_BODY,
    alert: true,
    // 重试只做只读回读：原先挂 onRoundChange(current.round)——切轮次不重新取数，
    // 读取失败时点它等于什么也没做。
    actions: [createButton({ label: S.ACTION_RETRY, onClick: () => handlers.onReload() }).el],
  });

  /**
   * 接线说明文本。
   *
   * 契约（server.py `api_task_detail`）：任务详情带 `wiring_note` 固定文案，
   * 每次都相同（设计文档 附录 A），与轮次无关。
   *
   * @returns {string}
   */
  function wiringText() {
    return (current.task && current.task.wiring_note) || '';
  }

  /**
   * 当前轮提示词文本（严格对应 current.round）。
   *
   * 契约：`prompts` 是 `[{level, text}]` 数组，后端只返回已解锁的级；
   * 越界取值一律返回空串，绝不把别的轮次内容当成当前轮（§9 重点交互 ①）。
   *
   * @returns {string}
   */
  function bodyText() {
    const prompts = (current.task && current.task.prompts) || [];
    const hit = prompts.find((p) => Number(p.level) === Number(current.round));
    return hit ? String(hit.text || '') : '';
  }

  /**
   * 合并复制：接线说明 + 分隔 + 当前轮提示词。
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
   * 轮次页签定义：后端只回传已解锁的级，所以「出现即已解锁」。
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
   * 轮次对应的提示词小标题（第 1 级症状 / 第 2 级不一致清单 / 第 3 级不变量与否决项）。
   * @param {number} n
   * @returns {string}
   */
  function levelTitle(n) {
    if (n === 1) return t(S.PROMPT_BODY_TITLE, { n });
    if (n === 2) return t(S.PROMPT_BODY_TITLE_2, { n });
    if (n === 3) return t(S.PROMPT_BODY_TITLE_3, { n });
    return t(S.PROMPT_BODY_TITLE, { n });
  }

  /**
   * 差异更新：只改文本与页签，不重建根节点。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    clear(bodyArea);

    if (current.error) {
      errorState.update({});
      bodyArea.appendChild(errorState.el);
      return;
    }
    if (current.loading) {
      bodyArea.appendChild(skeleton.el);
      return;
    }
    if (!current.run || !current.task) {
      emptyState.update({});
      bodyArea.appendChild(emptyState.el);
      return;
    }

    // 轮次切换后旧的"已复制"提示作废：保证复制内容与当前轮严格对应
    if (current.round !== lastRound) {
      copyAllBtn.update({ label: S.PROMPT_COPY_ALL });
      copyBodyBtn.update({ label: t(S.PROMPT_COPY_BODY, { n: current.round }) });
      lastRound = current.round;
    }

    bodyArea.appendChild(grid);
    setText(roundHint, t(S.PROMPT_ROUND_HINT, { n: current.round }));

    tabs.update({ tabs: tabDefs(), selected: String(current.round), label: S.PROMPT_ROUND_TAB });

    wiringCode.update({ text: wiringText(), title: S.PROMPT_WIRING_TITLE });
    copyWiringBtn.update({ getText: wiringText, label: S.PROMPT_COPY_WIRING });

    setText(bodyTitle, levelTitle(current.round));
    bodyCode.update({ text: bodyText(), title: levelTitle(current.round), ariaLabel: levelTitle(current.round) });
    copyBodyBtn.update({
      getText: bodyText,
      label: t(S.PROMPT_COPY_BODY, { n: current.round }),
    });
    copyAllBtn.update({
      getText: composeAll,
      label: S.PROMPT_COPY_ALL,
    });
  }

  update({});

  return {
    el: root,
    update,
    /** 供快捷键 C 触发复制全部。 */
    copyPrompt: () => copyAllBtn.copy(),
    /** 把当前轮的完整提示词交给工作台内置对话输入框。 */
    getPrompt: () => composeAll(),
    /** 解绑（§10.4）。 */
    destroy() {
      tabs.destroy();
      copyAllBtn.destroy();
      copyWiringBtn.destroy();
      copyBodyBtn.destroy();
      wiringCode.destroy();
      bodyCode.destroy();
    },
  };
}
