/**
 * tabs.js — 选项卡（轮次切换用，§11.1 / §12.10）
 *
 * 状态清单：未选中 / 选中 / 锁定（aria-disabled）/ 禁用。
 * 键盘路径：
 *   - Tab 进入 tablist 时只停在一个页签上（roving tabindex）
 *   - ← → 在页签间移动并选中；Home / End 跳首尾
 *   - 锁定页签不响应方向键选中，焦点仍可经过并听到"未解锁"说明
 * ARIA 要点：`role="tablist"` / `role="tab"` / `role="tabpanel"`；
 *   `aria-selected`、`aria-controls`、roving tabindex（选中项 0，其余 -1）、`aria-disabled` + 内联说明。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createTabs(props) → { el, update, destroy, select, getSelected }
 */

import { el, setText, on, clear } from '../core/dom.js';
import { S } from '../core/strings.js';

/** 递增 id。 */
let seq = 0;

/**
 * 创建选项卡。
 *
 * @param {{
 *   tabs: Array<{id: string, label: string, icon?: string, disabled?: boolean, hint?: string, badge?: string}>,
 *   selected?: string,
 *   label?: string,                     // tablist 的可访问名
 *   onChange?: (id: string) => void
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function, select: Function, getSelected: Function}}
 */
export function createTabs(props = {}) {
  seq += 1;
  const baseId = `tabs-${seq}`;
  let current = { ...props };
  let selected = props.selected || (props.tabs[0] && props.tabs[0].id) || '';
  let offKeydown = null;
  let offClicks = [];
  /** @type {Map<string, {tab: HTMLElement, panel: HTMLElement, hint: HTMLElement}>} */
  const entries = new Map();

  const list = el('div', {
    class: 'tabs__list',
    role: 'tablist',
    'aria-label': props.label || S.PROMPT_ROUND_TAB,
  });
  const panel = el('div', {
    class: 'tabs__panel',
    role: 'tabpanel',
    tabindex: '0',
  });
  const root = el('div', { class: 'tabs' }, list, panel);

  /**
   * 只接受一次「可用」的页签 id。
   * @param {string} id
   * @returns {string}
   */
  function resolveSelectable(id) {
    const found = (current.tabs || []).find((t) => t.id === id);
    if (found && !found.disabled) return id;
    const firstEnabled = (current.tabs || []).find((t) => !t.disabled);
    return firstEnabled ? firstEnabled.id : id;
  }

  /**
   * 选中某个页签（roving tabindex + aria-selected + panel 关联）。
   * @param {string} id
   * @param {{focus?: boolean, silent?: boolean}} [opts]
   */
  function select(id, opts = {}) {
    const next = resolveSelectable(id);
    if (next === selected && !opts.force) {
      if (opts.focus) {
        const e = entries.get(selected);
        if (e) e.tab.focus();
      }
      return;
    }
    selected = next;
    entries.forEach((entry, key) => {
      const isOn = key === selected;
      entry.tab.setAttribute('aria-selected', isOn ? 'true' : 'false');
      entry.tab.tabIndex = isOn ? 0 : -1;
      if (isOn) entry.tab.removeAttribute('aria-disabled');
      entry.panel.hidden = !isOn;
    });
    const active = entries.get(selected);
    if (active) {
      panel.setAttribute('aria-labelledby', active.tab.id);
      if (opts.focus) active.tab.focus();
    }
    if (!opts.silent && typeof current.onChange === 'function') current.onChange(selected);
  }

  /**
   * 重建页签与面板。页签集合变化时才重建（轮询不碰这里，不会打断焦点）。
   * @param {Array} tabs
   */
  function buildTabs(tabs) {
    offClicks.forEach((off) => off());
    offClicks = [];
    entries.clear();
    clear(list);
    clear(panel);
    offKeydown = null;

    tabs.forEach((t) => {
      const tabId = `${baseId}-tab-${t.id}`;
      const panelId = `${baseId}-panel-${t.id}`;
      const hintId = `${baseId}-hint-${t.id}`;

      const icon = el('span', { class: 'tabs__tab-icon', 'aria-hidden': 'true' }, t.icon || '');
      const label = el('span', { class: 'tabs__tab-label' }, t.label);
      const badge = t.badge
        ? el('span', { class: 'badge badge--muted', 'aria-hidden': 'true' }, t.badge)
        : null;
      const tab = el(
        'button',
        {
          type: 'button',
          class: 'tabs__tab',
          id: tabId,
          role: 'tab',
          'aria-controls': panelId,
        },
        icon,
        label,
        badge,
      );
      if (t.disabled) {
        tab.setAttribute('aria-disabled', 'true');
        tab.setAttribute('aria-describedby', hintId);
        tab.disabled = false; // 保持可聚焦，让读屏用户能听到"为什么不能点"
      }

      const hint = el('span', { class: 'visually-hidden', id: hintId }, t.hint || S.TAB_LOCKED_HINT);
      const tabPanel = el('div', {
        class: 'tabs__panel',
        id: panelId,
        role: 'tabpanel',
        'aria-labelledby': tabId,
        tabindex: '0',
      });

      offClicks.push(
        on(tab, 'click', () => {
          if (t.disabled) {
            // 锁定页签不切换；说明已经在 aria-describedby 里
            tab.focus();
            return;
          }
          select(t.id);
        }),
      );
      offClicks.push(
        on(tab, 'keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            if (t.disabled) {
              ev.preventDefault();
              return;
            }
            ev.preventDefault();
            select(t.id);
          }
        }),
      );

      list.appendChild(tab);
      panel.appendChild(tabPanel);
      // 提示文本只在真的需要时才进 DOM：解锁的页签没有 hint，
      // 挂一个带占位符的兜底文案会变成读屏里的噪声（也是 innerText 里的脏字）。
      if (t.hint || t.disabled) panel.appendChild(hint);
      entries.set(t.id, { tab, panel: tabPanel, hint });
    });

    // 方向键 / Home / End（roving tabindex，§12.10）
    offKeydown = on(list, 'keydown', (ev) => {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (!keys.includes(ev.key)) return;
      const ids = tabs.map((t) => t.id);
      const idx = ids.indexOf(selected);
      if (idx === -1) return;
      let nextIdx = idx;
      if (ev.key === 'ArrowLeft') nextIdx = (idx - 1 + ids.length) % ids.length;
      if (ev.key === 'ArrowRight') nextIdx = (idx + 1) % ids.length;
      if (ev.key === 'Home') nextIdx = 0;
      if (ev.key === 'End') nextIdx = ids.length - 1;
      ev.preventDefault();
      const nextId = ids[nextIdx];
      const target = tabs.find((t) => t.id === nextId);
      if (target && !target.disabled) {
        select(nextId, { focus: true });
      } else {
        // 落到锁定页签：只移动焦点，不切换选中
        const e = entries.get(nextId);
        if (e) e.tab.focus();
      }
    });
  }

  /**
   * 差异更新。
   * @param {object} patch { tabs, selected, onChange, label }
   */
  function update(patch = {}) {
    const prevTabs = current.tabs || [];
    const nextTabs = patch.tabs || prevTabs;
    const sameShape =
      nextTabs.length === prevTabs.length &&
      nextTabs.every((t, i) => {
        const p = prevTabs[i];
        return (
          p &&
          p.id === t.id &&
          p.label === t.label &&
          p.icon === t.icon &&
          p.disabled === t.disabled &&
          p.badge === t.badge &&
          p.hint === t.hint
        );
      });

    current = { ...current, ...patch };

    if (!sameShape) {
      buildTabs(nextTabs);
      // 页签集合变了：若原选中项已不存在，选第一个可用的
      if (!nextTabs.some((t) => t.id === selected && !t.disabled)) {
        selected = '';
      }
    }
    if (current.label) list.setAttribute('aria-label', current.label);
    if (patch.selected !== undefined) selected = patch.selected;
    select(selected, { silent: true, force: true });
  }

  update({});

  return {
    el: root,
    update,
    /** 选中某页签。 */
    select: (id, opts) => select(id, opts),
    /** 当前选中的页签 id。 */
    getSelected: () => selected,
    /**
     * 把面板内容塞进当前选中的页签。
     * @param {HTMLElement|string} content
     */
    setPanelContent(content) {
      const entry = entries.get(selected);
      if (!entry) return;
      clear(entry.panel);
      if (content instanceof HTMLElement) entry.panel.appendChild(content);
      else setText(entry.panel, content);
    },
    /** 解绑事件（§10.4）。 */
    destroy() {
      offClicks.forEach((off) => off());
      offClicks = [];
      if (offKeydown) {
        offKeydown();
        offKeydown = null;
      }
    },
  };
}
