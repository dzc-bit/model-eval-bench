/**
 * table.js — 数据表格（记分板用，§11.1 / §12.9 / §10.5）
 *
 * 状态清单：空 / 有数据 / 排序中（aria-sort）/ 行数超限（截断 + 「显示全部」）。
 * 键盘路径：表头排序按钮 Tab 可达，Enter / Space 切换排序方向。
 * ARIA 要点：
 *   - `<caption>` 必存在（说明这张表是什么）
 *   - 每列 `<th scope="col">`；单元格行首列 `scope="row"`
 *   - `aria-sort="ascending|descending|none"` 反映真实排序状态
 *   - 空态用一行 colspan 的单元格，给出"为什么空 + 下一步"
 *
 * 长列表：默认截断 100 行 + 「显示全部」（§10.5 / §11.2 #7）。
 *
 * 依赖：core/dom.js、core/strings.js
 * 导出：createTable(props) → { el, update, destroy, setRows }
 */

import { el, setText, on, clear, patchList } from '../core/dom.js';
import { S } from '../core/strings.js';

/** 默认最多显示的行数（§10.5）。 */
const DEFAULT_MAX_ROWS = 100;

/**
 * 创建表格。
 *
 * @param {{
 *   caption: string,
 *   columns: Array<{key: string, label: string, sortable?: boolean, numeric?: boolean, render?: (row: any, index: number) => HTMLElement|string}>,
 *   rows: Array,
 *   rowKey?: (row: any, index: number) => string|number,
 *   sort?: {key: string, dir: 'asc'|'desc'}|null,
 *   onSort?: (key: string, dir: 'asc'|'desc') => void,
 *   empty?: {title: string, desc?: string, action?: HTMLElement},
 *   maxRows?: number,
 *   onShowAll?: (() => void)|null
 * }} props
 * @returns {{el: HTMLElement, update: Function, destroy: Function}}
 */
export function createTable(props = {}) {
  let current = { ...props };
  let expanded = false;
  let headOffs = [];
  let footerNodes = null;

  const caption = el('caption', {}, current.caption || '');
  const headRow = el('tr', {});
  const headCell = el('thead', {}, headRow);
  const body = el('tbody', {});
  const footRow = el('tr', { class: 'table__empty-row' });
  const footCell = el('td', { colspan: Math.max(1, (current.columns || []).length) });
  footRow.appendChild(footCell);
  const foot = el('tfoot', { hidden: true }, footRow);

  const table = el('table', { class: 'table' }, caption, headCell, body, foot);
  const wrap = el('div', { class: 'table-wrap', tabindex: '0', role: 'region' }, table);
  wrap.setAttribute('aria-label', current.caption || S.STATE_EMPTY);

  const footerBar = el('div', { class: 'table__footer' });
  const root = el('div', { class: 'u-stack' }, wrap, footerBar);

  /**
   * 重建表头（列定义变化时才做）。
   */
  function buildHead() {
    headOffs.forEach((off) => off());
    headOffs = [];
    clear(headRow);
    (current.columns || []).forEach((col) => {
      const th = el('th', { scope: 'col', class: col.numeric ? 'table__num' : '' });
      if (col.sortable) {
        const btn = el(
          'button',
          { type: 'button', class: 'table__sort' },
          el('span', {}, col.label),
          el('span', { class: 'table__sort-mark', 'aria-hidden': 'true' }, ''),
        );
        const off = on(btn, 'click', () => {
          const cur = current.sort;
          const dir = cur && cur.key === col.key && cur.dir === 'asc' ? 'desc' : 'asc';
          if (typeof current.onSort === 'function') current.onSort(col.key, dir);
        });
        headOffs.push(off);
        th.appendChild(btn);
      } else {
        th.appendChild(el('span', {}, col.label));
      }
      headRow.appendChild(th);
    });
    footCell.setAttribute('colspan', String(Math.max(1, (current.columns || []).length)));
  }

  /**
   * 刷新 aria-sort（§12.9）。
   */
  function updateSortMarks() {
    const ths = Array.from(headRow.children);
    (current.columns || []).forEach((col, i) => {
      const th = ths[i];
      if (!th) return;
      if (!col.sortable) {
        th.removeAttribute('aria-sort');
        return;
      }
      const isOn = current.sort && current.sort.key === col.key;
      th.setAttribute('aria-sort', isOn ? (current.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
      const mark = th.querySelector('.table__sort-mark');
      if (mark) {
        setText(mark, isOn ? (current.sort.dir === 'asc' ? '▲' : '▼') : '');
      }
    });
  }

  /**
   * 取渲染后的可见行。
   * @returns {Array}
   */
  function visibleRows() {
    const rows = Array.isArray(current.rows) ? current.rows : [];
    const max = current.maxRows || DEFAULT_MAX_ROWS;
    if (expanded || rows.length <= max) return rows;
    return rows.slice(0, max);
  }

  /**
   * 渲染行内容（key 化复用，轮询不丢焦点）。
   */
  function renderRows() {
    const rows = visibleRows();
    const keyFn = current.rowKey || ((row, i) => (row && row.id) || i);

    if (rows.length === 0) {
      patchList(body, [], keyFn, () => el('tr'), () => {});
      foot.hidden = false;
      clear(footCell);
      const empty = current.empty || {};
      footCell.appendChild(el('div', { class: 'empty-state' },
        el('div', { class: 'empty-state__title' }, empty.title || S.STATE_EMPTY),
        empty.desc ? el('div', { class: 'empty-state__desc' }, empty.desc) : null,
        empty.action || null,
      ));
      return;
    }
    foot.hidden = true;

    const columns = current.columns || [];
    patchList(
      body,
      rows,
      keyFn,
      () => {
        const tr = el('tr', {});
        columns.forEach((col, ci) => {
          tr.appendChild(el(ci === 0 ? 'th' : 'td', ci === 0 ? { scope: 'row' } : {}));
        });
        return tr;
      },
      (tr, row, index) => {
        const cells = Array.from(tr.children);
        columns.forEach((col, ci) => {
          const cell = cells[ci];
          if (!cell) return;
          const value = col.render ? col.render(row, index) : row[col.key];
          if (cell.firstChild && cell.firstChild.nodeType === Node.TEXT_NODE && !col.render) {
            setText(cell, value);
          } else {
            clear(cell);
            if (value instanceof HTMLElement) cell.appendChild(value);
            else setText(cell, value);
          }
          if (col.numeric) cell.classList.add('table__num');
        });
      },
    );
  }

  /**
   * 刷新「显示全部 / 收起列表」条。
   */
  function renderFooterBar() {
    clear(footerBar);
    const rows = Array.isArray(current.rows) ? current.rows : [];
    const max = current.maxRows || DEFAULT_MAX_ROWS;
    if (rows.length <= max) return;
    if (!expanded) {
      const btn = el('button', { type: 'button', class: 'btn btn--sm' },
        `${S.ACTION_SHOW_ALL}（共 ${rows.length} 行）`);
      on(btn, 'click', () => {
        expanded = true;
        renderRows();
        renderFooterBar();
        if (typeof current.onShowAll === 'function') current.onShowAll();
      });
      footerBar.appendChild(btn);
      footerBar.appendChild(el('span', { class: 'u-faint' }, S.LOG_TRUNCATED));
    } else {
      const btn = el('button', { type: 'button', class: 'btn btn--sm' }, S.ACTION_SHOW_LESS);
      on(btn, 'click', () => {
        expanded = false;
        renderRows();
        renderFooterBar();
      });
      footerBar.appendChild(btn);
    }
  }

  /**
   * 差异更新：列没变就不动表头，行用 key 复用。
   * @param {object} patch
   */
  function update(patch = {}) {
    const prev = current;
    current = { ...current, ...patch };
    if (patch.caption !== undefined) setText(caption, current.caption || '');
    if (patch.caption !== undefined) wrap.setAttribute('aria-label', current.caption || S.STATE_EMPTY);

    const columnsChanged =
      patch.columns &&
      JSON.stringify(patch.columns.map((c) => [c.key, c.label, c.sortable, c.numeric])) !==
        JSON.stringify((prev.columns || []).map((c) => [c.key, c.label, c.sortable, c.numeric]));
    if (columnsChanged) buildHead();
    updateSortMarks();
    renderRows();
    renderFooterBar();
  }

  buildHead();
  update({});

  return {
    el: root,
    update,
    /** 直接换数据源。 */
    setRows: (rows) => update({ rows }),
    /** 解绑事件（§10.4）。 */
    destroy() {
      headOffs.forEach((off) => off());
      headOffs = [];
      patchList(body, [], () => '', () => el('tr'), () => {});
    },
  };
}
