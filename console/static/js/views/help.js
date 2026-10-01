/**
 * help.js — 帮助视图（重内容，动态 import 延迟加载）
 *
 * 排版模型照 Markdown 文档的层级来：
 *   h1 页面标题 → h2 章节（左侧目录一级）→ h3 小节 → 段落 / 列表 / 表格 / 提示块
 * 阅读顺序即文档顺序：正文单栏到底，只有等宽短条目（术语表）用多栏。
 *
 * 渲染纪律：本页**零业务数据**，全部是常量文案。仍然一律走 `el()` + textContent，
 * 不用 innerHTML（§10.3 允许的静态模板例外在本项目实测不需要，故一律不写）。
 *
 * 状态：静态单页。滚动时左侧目录高亮当前章节（IntersectionObserver）。
 * 键盘：目录是页内锚点，Tab 可达；正文 heading 层级与视觉顺序一致。
 * ARIA：目录 nav + aria-label；每节 h2 带 id；提示块用 role=note。
 *
 * 依赖：core/*、components/*
 * 导出：createHelp(props) → { el, destroy, el_h1 }
 */

import { el, on } from '../core/dom.js';
import { S } from '../core/strings.js';
import { focusHeading } from '../core/a11y.js';



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

/** 术语表，按语义分三组：跑一轮 / 沙箱与隔离 / 评分与结果。 */
const TERM_GROUPS = [
  {
    id: 'help-term-run',
    title: '跑一轮',
    terms: [
      ['轮次', '同一道题的第几次尝试。初级 1 轮、中级 2 轮、高级与王者各 3 轮。'],
      ['级别', '提示词的详细程度，分 1/2/3 级。轮次推进才解锁更详细的级别。'],
      ['提示词', '发送给内置模型的任务描述，逐级增加信息量，但都不给文件名。'],
      ['接线说明', '每次都固定的一段话，告诉模型工作目录就是沙箱根目录。'],
      ['揭晓', '查看参考解。揭晓过的轮次不计入通过率统计。'],
    ],
  },
  {
    id: 'help-term-sandbox',
    title: '沙箱与隔离',
    terms: [
      ['沙箱', '受测仓库的独立拷贝，放在 sandbox_root 下的普通文件夹里，模型只能通过内置工具在这里改。'],
      ['基线', '沙箱生成时的初始状态。指纹用于校验前比对，判断沙箱有没有被外部动过。'],
      ['越界', '改了 tests、配置或依赖清单。这些改动不进评分树，只记违规。'],
    ],
  },
  {
    id: 'help-term-score',
    title: '评分与结果',
    terms: [
      ['校验', '在评分树里跑仓库原始测试加隐藏测试，产出分组红绿与部分分。'],
      ['组', '一个对外出口或一条不变量。分组的红绿构成这一轮的成绩。'],
      ['用例', '组里的单个测试。一条用例红，它所在的组就是红的。'],
      ['部分分', '通过组权重占比。用来区分「只修了一半」。'],
      ['回归（p2p）', '既有用例的绿线。回归被破坏说明引入了新问题，本轮作废。'],
      ['记分板', '按模型档案分区查看任务成绩与 pass@k。'],
    ],
  },
];

/** 术语分组在目录里的二级条目（从 TERM_GROUPS 派生，避免两处手写）。 */
const TERM_GROUP_TOC = TERM_GROUPS.map((g) => ({ id: g.id, label: g.title }));

/** 目录项：一级章节，编号在渲染时按顺序生成。 */
const TOC = [
  { id: 'help-flow', label: S.HELP_FLOW_TITLE },
  { id: 'help-sandbox', label: S.HELP_SANDBOX_TITLE },
  { id: 'help-grade', label: S.HELP_GRADE_TITLE },
  { id: 'help-shortcut', label: S.HELP_SHORTCUT_TITLE },
  { id: 'help-a11y', label: S.HELP_A11Y_TITLE },
  { id: 'help-trouble', label: S.HELP_TROUBLE_TITLE },
  { id: 'help-term', label: S.HELP_TERM_TITLE },
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

  // ---- 目录：一级章节 + 顺序编号 ----
  const tocList = el('ul', { class: 'help__toc-list' });
  const tocLinks = new Map();
  TOC.forEach((item, index) => {
    const link = el(
      'a',
      { class: 'help__toc-link', href: `#${item.id}` },
      el('span', { class: 'help__toc-num', 'aria-hidden': 'true' }, String(index + 1)),
      el('span', { class: 'help__toc-text' }, item.label),
    );
    tocLinks.set(item.id, link);
    tocList.appendChild(el('li', { class: 'help__toc-item' }, link));
  });
  const toc = el(
    'nav',
    { class: 'help__toc', 'aria-label': S.HELP_TOC_TITLE },
    el('h2', { class: 'help__toc-title' }, S.HELP_TOC_TITLE),
    tocList,
  );

  /**
   * 滚动时高亮当前章节。
   *
   * 按几何位置算，不用 IntersectionObserver：章节高度差异大，
   * 观察带无论宽窄都会出错（宽了把下一节纳进来，窄了矮章节落不进去）。
   * 这里取「在阅读线之上的最后一个标题」——规则单一、结果可预测。
   */
  function trackActiveSection() {
    const anchors = TOC
      .map((item) => ({ id: item.id, node: root.querySelector(`#${item.id}`) }))
      .filter((a) => a.node);
    if (!anchors.length) return null;

    let raf = null;
    const paint = () => {
      raf = null;
      // 阅读线取视口 1/4 处：太靠上（如 120px）时，滚到章节开头那一刻
      // 标题还没越过线，高亮会停在上一节。1/4 处既能跟上滚动，
      // 又不会因为一节很长而提前跳到下一节。
      const line = Math.max(120, Math.round(window.innerHeight * 0.25));
      let active = anchors[0].id;
      for (const a of anchors) {
        if (a.node.getBoundingClientRect().top <= line) active = a.id;
        else break;
      }
      tocLinks.forEach((link, id) => {
        if (id === active) link.setAttribute('aria-current', 'true');
        else link.removeAttribute('aria-current');
      });
    };
    // rAF 节流：滚动事件每帧最多算一次，布局读取不叠加
    const onScroll = () => { if (raf === null) raf = requestAnimationFrame(paint); };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    paint();
    return {
      disconnect() {
        if (raf !== null) cancelAnimationFrame(raf);
        window.removeEventListener('scroll', onScroll);
        window.removeEventListener('resize', onScroll);
      },
    };
  }

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
   * 造一节（h2 层级，进左侧目录）。
   * @param {string} id
   * @param {string} title
   * @param {Array} children
   * @returns {HTMLElement}
   */
  function section(id, title, children) {
    const h2 = el('h2', { id, tabindex: '-1' }, title);
    // id 留在 h2 上（页内锚点要精确落到标题），section 另挂 data 供滚动高亮观察：
    // 观察 46px 高的标题节点时，它很容易整个落在收窄后的观察区之外，一次都不触发。
    return el(
      'section',
      { class: 'help__section', dataset: { section: id } },
      h2,
      el('div', { class: 'help__prose' }, ...children),
    );
  }

  /**
   * 小节标题（h3 层级，不进目录——目录只保留一级）。
   * @param {string} title
   * @returns {HTMLElement}
   */
  function sub(title) {
    return el('h3', { class: 'help__sub' }, title);
  }

  /** 提示块：一段需要被看见但不打断阅读的补充说明。 */
  function tip(text, tone = 'info') {
    return el('p', { class: 'help__tip', dataset: { tone }, role: 'note' }, text);
  }

  /** 编号步骤。 */
  function steps(items) {
    return el('ol', { class: 'help__steps' }, ...items.map((t) => el('li', {}, t)));
  }

  /** 无序要点。 */
  function bullets(items) {
    return el('ul', { class: 'help__bullets' }, ...items.map((t) => el('li', {}, t)));
  }

  // ==================== 一轮评测的完整流程 ====================
  const flowSection = section('help-flow', S.HELP_FLOW_TITLE, [
    el('p', {}, S.HELP_FLOW_DESC),
    tip(S.HELP_FLOW_WHY),
    sub(S.HELP_FLOW_SUB_START),
    steps([
      S.HELP_STEP_1,
      S.HELP_STEP_2,
      S.HELP_STEP_3,
      S.HELP_STEP_4,
      S.HELP_STEP_5,
      S.HELP_STEP_6,
    ]),
    tip(S.HELP_FLOW_TIP, 'tip'),
  ]);

  // ==================== 沙箱到底是什么 ====================
  const sandboxSection = section('help-sandbox', S.HELP_SANDBOX_TITLE, [
    el('p', {}, S.HELP_SANDBOX_DESC),
    sub(S.HELP_SANDBOX_SUB_RULES),
    bullets([S.HELP_SANDBOX_WHY_1, S.HELP_SANDBOX_WHY_2, S.HELP_SANDBOX_WHY_3]),
    sub(S.HELP_SANDBOX_SUB_LIFECYCLE),
    steps([S.HELP_SANDBOX_LIFE_1, S.HELP_SANDBOX_LIFE_2, S.HELP_SANDBOX_LIFE_3, S.HELP_SANDBOX_LIFE_4]),
  ]);

  // ==================== 怎么读校验结果 ====================
  const gradeSection = section('help-grade', S.HELP_GRADE_TITLE, [
    el('p', {}, S.HELP_GRADE_DESC),
    sub(S.HELP_GRADE_SUB_GROUPS),
    bullets([S.HELP_GRADE_TERM_1, S.HELP_GRADE_TERM_4]),
    sub(S.HELP_GRADE_SUB_SCORE),
    el('p', {}, S.HELP_GRADE_SCORE_DESC),
    tip(S.HELP_GRADE_TERM_3),
    sub(S.HELP_GRADE_SUB_RED),
    bullets([S.HELP_GRADE_RED_1, S.HELP_GRADE_RED_2, S.HELP_GRADE_RED_3]),
  ]);

  // ==================== 快捷键 ====================
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
    sub(S.HELP_SHORTCUT_SUB_NOTE),
    el('p', {}, S.HELP_SHORTCUT_NOTE),
  ]);

  // ==================== 无障碍与键盘 ====================
  const a11ySection = section('help-a11y', S.HELP_A11Y_TITLE, [
    el('p', {}, S.HELP_A11Y_DESC),
    bullets([S.HELP_A11Y_1, S.HELP_A11Y_2, S.HELP_A11Y_3, S.HELP_A11Y_4, S.HELP_A11Y_5]),
  ]);

  // ==================== 排障 ====================
  const troubleSection = section('help-trouble', S.HELP_TROUBLE_TITLE, [
    el('p', {}, S.HELP_TROUBLE_DESC),
    tip(S.HELP_TROUBLE_QUICK, 'tip'),
    sub(S.HELP_TROUBLE_SUB_QUICK),
    steps([
      S.HELP_TROUBLE_1,
      S.HELP_TROUBLE_2,
      S.HELP_TROUBLE_3,
      S.HELP_TROUBLE_4,
      S.HELP_TROUBLE_5,
    ]),
  ]);

  // ==================== 术语表 ====================
  // 按语义分三组，每组一个 h3：14 个词平铺成一列时，找某个词只能靠逐行扫。
  const termBlocks = TERM_GROUPS.map((group) => {
    const list = el('ul', { class: 'term-list' },
      ...group.terms.map(([term, desc]) =>
        el('li', { class: 'term-list__item' },
          el('span', { class: 'term-list__term' }, term),
          el('div', {}, desc),
        ),
      ),
    );
    return el('div', { class: 'help__term-group' }, sub(group.title), list);
  });
  const termSection = section('help-term', S.HELP_TERM_TITLE, [
    el('p', {}, S.HELP_TERM_DESC),
    tip(S.HELP_TERM_NOTE),
    ...termBlocks,
  ]);

  const body = el(
    'div',
    { class: 'help__body' },
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

  // 观察器要等 root 真正进文档才谈得上「进入视口」——创建时它还是个游离节点，
  // 直接 observe 会一次回调都不触发。延到下一帧，那时 main.js 已把它挂好。
  let sectionObserver = null;
  const rafId = typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame(() => { sectionObserver = trackActiveSection(); })
    : null;

  return {
    el: root,
    el_h1: h1,
    /** 解绑。 */
    destroy() {
      if (rafId !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(rafId);
      if (sectionObserver) sectionObserver.disconnect();
      offHandlers.forEach((off) => off());
      offHandlers.length = 0;
    },
  };
}

/** 供 main.js 复用：快捷键表（? 打开的对话框直接渲染它）。 */
export const SHORTCUT_TABLE = SHORTCUTS;
