/**
 * help.js — 帮助视图（重内容，动态 import 延迟加载）
 *
 * 职责：把 §3 流程、§4 沙箱、§5 校验读法、§13.4 快捷键、§12 无障碍、术语表写成中文说明。
 *
 * 渲染纪律：本页**零业务数据**，全部是常量文案。仍然一律走 `el()` + textContent，
 * 不用 innerHTML（§10.3 允许的静态模板例外在本项目实测不需要，故一律不写）。
 *
 * 状态：静态单页。
 * 键盘：左侧目录是页内锚点，Tab 可达；正文有 heading 层级，Tab 顺序与视觉顺序一致。
 * ARIA：目录用 nav + aria-label；每节有 h2 与 id；锚点跳转后焦点落到目标标题。
 *
 * 依赖：core/*、components/*
 * 导出：createHelp(props) → { el, destroy, el_h1 }
 */

import { el, on } from '../core/dom.js';
import { S } from '../core/strings.js';
import { focusHeading } from '../core/a11y.js';

/** 目录项。 */
const TOC = [
  { id: 'help-flow', label: S.HELP_FLOW_TITLE },
  { id: 'help-sandbox', label: S.HELP_SANDBOX_TITLE },
  { id: 'help-grade', label: S.HELP_GRADE_TITLE },
  { id: 'help-shortcut', label: S.HELP_SHORTCUT_TITLE },
  { id: 'help-a11y', label: S.HELP_A11Y_TITLE },
  { id: 'help-trouble', label: S.HELP_TROUBLE_TITLE },
  { id: 'help-term', label: S.HELP_TERM_TITLE },
];

/** 快捷键表。 */
const SHORTCUTS = [
  { key: 'C', action: S.HELP_SHORTCUT_C, when: S.HELP_SHORTCUT_C_WHEN },
  { key: 'G', action: S.HELP_SHORTCUT_G, when: S.HELP_SHORTCUT_G_WHEN },
  { key: 'R', action: S.HELP_SHORTCUT_R, when: S.HELP_SHORTCUT_R_WHEN },
  { key: '1', action: S.HELP_SHORTCUT_1, when: S.HELP_SHORTCUT_N_WHEN },
  { key: '2', action: S.HELP_SHORTCUT_2, when: S.HELP_SHORTCUT_N_WHEN },
  { key: '3', action: S.HELP_SHORTCUT_3, when: S.HELP_SHORTCUT_N_WHEN },
  { key: '?', action: S.HELP_SHORTCUT_QUESTION, when: S.HELP_SHORTCUT_QUESTION_WHEN },
  { key: 'Esc', action: S.HELP_SHORTCUT_ESC, when: S.HELP_SHORTCUT_ESC_WHEN },
];

/** 术语表。 */
const TERMS = [
  ['沙箱', '受测仓库的独立拷贝，脱敏后映射成盘符交给模型，模型只能在这里改。'],
  ['基线', '沙箱生成时的初始状态。哈希用于校验前比对，判断沙箱有没有被外部动过。'],
  ['轮次', '同一道题的第几次尝试。初级 1 轮、中级 2 轮、高级与王者各 3 轮。'],
  ['提示词', '贴给模型的任务描述，分三级，逐级增加信息量，但都不给文件名。'],
  ['接线说明', '每次都固定的一段话，告诉模型工作目录就是沙箱根目录。'],
  ['校验', '在评分树里跑仓库原始测试加隐藏测试，产出分组红绿与部分分。'],
  ['组', '一个对外出口或一条不变量。分组的红绿构成这一轮的成绩。'],
  ['部分分', '通过组权重占比。用来区分「只修了一半」。'],
  ['回归（p2p）', '既有用例的绿线。回归被破坏说明引入了新问题，本轮作废。'],
  ['越界', '改了 tests、配置或依赖清单。这些改动不进评分树，只记违规。'],
  ['揭晓', '查看参考解。揭晓过的轮次不计入通过率统计。'],
  ['记分板', '行是任务、列是模型的成绩矩阵。'],
];

/**
 * 创建帮助视图。
 * @param {{onShortcut?: Function}} [props]
 * @returns {{el: HTMLElement, destroy: Function, el_h1: HTMLElement}}
 */
export function createHelp(props = {}) {
  const { onShortcut } = props;
  const offHandlers = [];

  const h1 = el('h1', { tabindex: '-1' }, S.HELP_TITLE);

  // ---- 目录 ----
  const tocList = el('ul', { class: 'help__toc-list' });
  TOC.forEach((item) => {
    tocList.appendChild(
      el('li', {},
        el('a', { class: 'help__toc-link', href: `#${item.id}` }, item.label),
      ),
    );
  });
  const toc = el(
    'nav',
    { class: 'help__toc', 'aria-label': S.HELP_TOC_TITLE },
    el('h2', { class: 'section-title' }, S.HELP_TOC_TITLE),
    tocList,
  );

  // 目录点击后把焦点放到目标标题（§12.1 同款做法）
  tocList.addEventListener('click', (ev) => {
    const link = ev.target.closest('a');
    if (!link) return;
    const target = root.querySelector(link.getAttribute('href'));
    if (target) {
      ev.preventDefault();
      focusHeading(target);
      target.scrollIntoView({ behavior: 'auto', block: 'start' });
    }
  });

  /**
   * 造一节。
   * @param {string} id
   * @param {string} title
   * @param {Array} children
   * @returns {HTMLElement}
   */
  function section(id, title, children) {
    const h2 = el('h2', { id, tabindex: '-1' }, title);
    return el('section', { class: 'help__section' }, h2, el('div', { class: 'help__prose' }, ...children));
  }

  // ---- 流程 ----
  const flowSection = section('help-flow', S.HELP_FLOW_TITLE, [
    el('p', {}, S.HELP_FLOW_DESC),
    el('ol', {},
      el('li', {}, S.HELP_STEP_1),
      el('li', {}, S.HELP_STEP_2),
      el('li', {}, S.HELP_STEP_3),
      el('li', {}, S.HELP_STEP_4),
      el('li', {}, S.HELP_STEP_5),
      el('li', {}, S.HELP_STEP_6),
    ),
  ]);

  // ---- 沙箱 ----
  const sandboxSection = section('help-sandbox', S.HELP_SANDBOX_TITLE, [
    el('p', {}, S.HELP_SANDBOX_DESC),
    el('ul', {},
      el('li', {}, S.HELP_SANDBOX_WHY_1),
      el('li', {}, S.HELP_SANDBOX_WHY_2),
      el('li', {}, S.HELP_SANDBOX_WHY_3),
    ),
  ]);

  // ---- 校验读法 ----
  const gradeSection = section('help-grade', S.HELP_GRADE_TITLE, [
    el('p', {}, S.HELP_GRADE_DESC),
    el('ul', {},
      el('li', {}, S.HELP_GRADE_TERM_1),
      el('li', {}, S.HELP_GRADE_TERM_2),
      el('li', {}, S.HELP_GRADE_TERM_3),
      el('li', {}, S.HELP_GRADE_TERM_4),
    ),
  ]);

  // ---- 快捷键 ----
  const shortcutRows = SHORTCUTS.map((s) =>
    el('tr', {},
      el('th', { scope: 'row' }, el('kbd', { class: 'key' }, s.key)),
      el('td', {}, s.action),
      el('td', { class: 'u-muted' }, s.when),
    ),
  );
  const shortcutSection = section('help-shortcut', S.HELP_SHORTCUT_TITLE, [
    el('p', {}, S.HELP_SHORTCUT_DESC),
    el('table', { class: 'table help__kbd-table' },
      el('caption', { class: 'visually-hidden' }, S.HELP_SHORTCUT_TITLE),
      el('thead', {}, el('tr', {},
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_KEY),
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_ACTION),
        el('th', { scope: 'col' }, S.HELP_SHORTCUT_COL_WHEN),
      )),
      el('tbody', {}, ...shortcutRows),
    ),
  ]);

  // ---- 无障碍 ----
  const a11ySection = section('help-a11y', S.HELP_A11Y_TITLE, [
    el('p', {}, S.HELP_A11Y_DESC),
    el('ul', {},
      el('li', {}, S.HELP_A11Y_1),
      el('li', {}, S.HELP_A11Y_2),
      el('li', {}, S.HELP_A11Y_3),
      el('li', {}, S.HELP_A11Y_4),
      el('li', {}, S.HELP_A11Y_5),
    ),
  ]);

  // ---- 排障 ----
  const troubleSection = section('help-trouble', S.HELP_TROUBLE_TITLE, [
    el('p', {}, S.HELP_TROUBLE_DESC),
    el('ol', {},
      el('li', {}, S.HELP_TROUBLE_1),
      el('li', {}, S.HELP_TROUBLE_2),
      el('li', {}, S.HELP_TROUBLE_3),
      el('li', {}, S.HELP_TROUBLE_4),
      el('li', {}, S.HELP_TROUBLE_5),
    ),
  ]);

  // ---- 术语表 ----
  const termList = el('ul', { class: 'term-list' },
    ...TERMS.map(([term, desc]) =>
      el('li', { class: 'term-list__item' },
        el('span', { class: 'term-list__term' }, term),
        el('div', { class: 'u-muted' }, desc),
      ),
    ),
  );
  const termSection = section('help-term', S.HELP_TERM_TITLE, [
    el('p', {}, S.HELP_TERM_DESC),
    termList,
  ]);

  const body = el(
    'div',
    {},
    flowSection,
    sandboxSection,
    gradeSection,
    shortcutSection,
    a11ySection,
    troubleSection,
    termSection,
  );

  const root = el(
    'div',
    { class: 'view' },
    el('div', { class: 'view__head' },
      el('div', {}, h1, el('p', { class: 'view__desc' }, S.HELP_DESC)),
    ),
    el('div', { class: 'help__layout' }, toc, body),
  );

  if (typeof onShortcut === 'function') offHandlers.push(on(document, 'keydown', onShortcut));

  return {
    el: root,
    el_h1: h1,
    /** 解绑。 */
    destroy() {
      offHandlers.forEach((off) => off());
      offHandlers.length = 0;
    },
  };
}

/** 供 main.js 复用：快捷键表（? 打开的对话框直接渲染它）。 */
export const SHORTCUT_TABLE = SHORTCUTS;
