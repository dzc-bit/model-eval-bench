/**
 * dom.js — 元素工厂与列表复用（§10.3）
 *
 * 职责：
 *   1. `el(tag, props, ...children)` 元素工厂：文本一律 textContent，属性白名单，事件自动挂。
 *   2. `patchList(container, items, keyFn, create, update)` key 化列表复用：避免整表重建，
 *      轮询时不会丢焦点、丢滚动（§11.2 #3、#14）。
 *   3. `frag()` / `clear()` / `on()` 三个小工具。
 *
 * 依赖：无。
 * 导出：el, frag, clear, patchList, setText, setAttr, on, text
 *
 * 安全纪律（§10.3 / §11.2 #5）：
 *   - 禁止用业务数据拼 innerHTML。本模块**不提供任何写入 innerHTML 的入口**，
 *     props 里的 `html` 字段会被直接忽略并告警；全站渲染走 textContent。
 *   - 唯一被设计文档允许的例外是「纯静态模板集中一处」，本项目实测不需要，未使用。
 */

/**
 * 属性白名单：只允许这些属性名直接落到 DOM 上。
 * 未列出的属性会被忽略，避免误写（如 innerHTML、onclick 字符串）。
 */
const ALLOWED_ATTRS = new Set([
  'id', 'class', 'className', 'type', 'role', 'title', 'lang', 'dir', 'hidden',
  'tabindex', 'href', 'target', 'rel', 'download', 'name', 'value', 'placeholder',
  'for', 'required', 'readonly', 'disabled', 'checked', 'selected', 'multiple',
  'autocomplete', 'spellcheck', 'autocapitalize', 'inputmode', 'enterkeyhint',
  'min', 'max', 'step', 'minlength', 'maxlength', 'rows', 'cols', 'wrap',
  'scope', 'colspan', 'rowspan', 'headers', 'open', 'start', 'colspan',
  'width', 'height', 'srcset', 'sizes', 'loading', 'decoding', 'draggable',
]);

/** 作为 property（而非 attribute）设置的布尔/值属性。 */
const PROPERTY_PROPS = new Set([
  'value', 'checked', 'selected', 'disabled', 'readOnly', 'multiple',
  'indeterminate', 'open', 'textContent',
]);

/** 已告警过的未知属性名，避免刷屏。 */
const warnedKeys = new Set();

/**
 * 告警一次未知属性/被禁字段。
 * @param {string} key
 * @param {string} reason
 */
function warnOnce(key, reason) {
  if (warnedKeys.has(key)) return;
  warnedKeys.add(key);
  if (typeof console !== 'undefined' && typeof console.warn === 'function') {
    console.warn(`[dom] ${reason}：${key}（已忽略）`);
  }
}

/**
 * 递归把 children 规整成节点数组。
 * 字符串 → 文本节点（走 textContent 语义，不会被解析成 HTML）。
 * @param {unknown} child
 * @param {Node[]} out
 */
function appendChild(child, out) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    child.forEach((c) => appendChild(c, out));
    return;
  }
  if (typeof child === 'string' || typeof child === 'number') {
    out.push(document.createTextNode(String(child)));
    return;
  }
  if (child instanceof Node) {
    out.push(child);
    return;
  }
  // 其它对象（含 Promise）一律忽略，避免把 [object Object] 写进界面
  warnOnce('child', 'el() 收到了无法渲染的子元素类型（已忽略）');
}

/**
 * 元素工厂。
 *
 * @example
 *   el('button', { class: 'btn btn--primary', onClick: fn, 'aria-label': '复制' }, '复制')
 *   el('p', { class: 'u-muted' }, '文本 ', el('strong', {}, '12 秒'))
 *
 * @param {string} tag 标签名
 * @param {object} [props] 属性表；`onXxx` 自动 addEventListener；`text` 走 textContent；
 *                        `style` 为对象；`dataset` 为对象；`ref` 为回调
 * @param {...unknown} children 子节点（字符串按文本处理）
 * @returns {HTMLElement}
 */
export function el(tag, props = null, ...children) {
  const node = document.createElement(tag);
  if (props && typeof props === 'object') {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined) continue;

      if (key === 'text') {
        node.textContent = String(value);
        continue;
      }
      if (key === 'html') {
        warnOnce('html', '禁止用 innerHTML 渲染内容，请改用 text 或子节点');
        continue;
      }
      if (key === 'dataset' && typeof value === 'object') {
        Object.entries(value).forEach(([k, v]) => {
          if (v === null || v === undefined) return;
          node.dataset[k] = String(v);
        });
        continue;
      }
      if (key === 'style' && typeof value === 'object') {
        Object.assign(node.style, value);
        continue;
      }
      if (key === 'ref' && typeof value === 'function') {
        value(node);
        continue;
      }
      if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
        continue;
      }
      if (key.startsWith('aria-') || key.startsWith('data-')) {
        node.setAttribute(key, String(value));
        continue;
      }
      if (key === 'class') {
        node.setAttribute('class', String(value));
        continue;
      }
      if (PROPERTY_PROPS.has(key)) {
        node[key] = value;
        if (key === 'value' || key === 'open') {
          // 部分属性需要同时落到 attribute 才能被序列化/查询
          if (value === true || key === 'value') node.setAttribute(key, String(value));
        }
        continue;
      }
      if (ALLOWED_ATTRS.has(key)) {
        node.setAttribute(key, value === true ? '' : String(value));
        continue;
      }
      warnOnce(key, 'el() 收到不在白名单里的属性');
    }
  }
  const out = [];
  children.forEach((c) => appendChild(c, out));
  out.forEach((c) => node.appendChild(c));
  return node;
}

/**
 * 创建文档片段，把一批子节点收成一段（批量插入只触发一次重排）。
 * @param {...unknown} children
 * @returns {DocumentFragment}
 */
export function frag(...children) {
  const f = document.createDocumentFragment();
  const out = [];
  children.forEach((c) => appendChild(c, out));
  out.forEach((c) => f.appendChild(c));
  return f;
}

/**
 * 清空容器（只摘子节点，不动容器本身）。
 * @param {Element} container
 */
export function clear(container) {
  if (!container) return;
  while (container.firstChild) container.removeChild(container.firstChild);
}

/**
 * 设置文本（等价 textContent，顺手保证 null 变空串）。
 * @param {Element} node
 * @param {unknown} value
 * @returns {Element} node
 */
export function setText(node, value) {
  if (node) node.textContent = value === null || value === undefined ? '' : String(value);
  return node;
}

/**
 * 设置属性；值为 null/undefined/false 时移除属性。
 * @param {Element} node
 * @param {string} name
 * @param {unknown} value
 * @returns {Element} node
 */
export function setAttr(node, name, value) {
  if (!node) return node;
  if (value === null || value === undefined || value === false) node.removeAttribute(name);
  else node.setAttribute(name, value === true ? '' : String(value));
  return node;
}

/**
 * 绑定事件并返回解绑函数（供 destroy 使用，§10.4 / §11.2 #13）。
 * @param {EventTarget} target
 * @param {string} type
 * @param {EventListener} handler
 * @param {object|boolean} [options]
 * @returns {() => void} 解绑函数
 */
export function on(target, type, handler, options) {
  target.addEventListener(type, handler, options);
  return () => target.removeEventListener(type, handler, options);
}

/** 容器 → 列表控制器，避免每次 update 都新建一套记录表。 */
const listControllers = new WeakMap();

/**
 * key 化列表复用。
 *
 * 语义：
 *   - 用 keyFn(item) 得到稳定 key；相同 key 的记录**只创建一次**，之后走 update 差异更新。
 *   - 顺序变化时按最小移动原则重排 DOM，不整表重建（不丢焦点、不丢滚动）。
 *   - 消失的记录会调用 create 返回值的 destroy()（若提供），然后摘除节点。
 *   - create 返回 HTMLElement 时，视为无状态节点，update 直接收 (node, item, index)。
 *   - create 返回 {el, update, destroy} 时，update 收 (record, item, index)，
 *     record 形如 { el, key, index, api }。
 *
 * @param {Element} container
 * @param {Array} items 数据数组
 * @param {(item: any, index: number) => string|number} keyFn
 * @param {(item: any, index: number) => HTMLElement|{el: HTMLElement, update?: Function, destroy?: Function}} create
 * @param {(target: any, item: any, index: number) => void} [update]
 * @returns {{ clear: () => void, size: () => number }}
 */
export function patchList(container, items, keyFn, create, update) {
  if (!container) return { clear() {}, size: () => 0 };

  let controller = listControllers.get(container);
  if (!controller) {
    /** @type {Map<string, {key: string, el: HTMLElement, api: object|null, index: number}>} */
    const records = new Map();
    controller = {
      records,
      clear() {
        records.forEach((rec) => {
          if (rec.api && typeof rec.api.destroy === 'function') {
            try {
              rec.api.destroy();
            } catch (err) {
              warnDestroyError(err);
            }
          }
          if (rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
        });
        records.clear();
        clear(container);
      },
      size: () => records.size,
    };
    listControllers.set(container, controller);
  }

  const records = controller.records;
  const list = Array.isArray(items) ? items : [];
  const seen = new Set();

  list.forEach((item, index) => {
    const rawKey = keyFn(item, index);
    const key = `${typeof rawKey}:${String(rawKey)}`;
    seen.add(key);
    let rec = records.get(key);
    if (!rec) {
      const created = create(item, index);
      const elNode = created && created.el ? created.el : created;
      const api = created && created.el ? created : null;
      rec = { key, el: elNode, api, index };
      records.set(key, rec);
      container.appendChild(elNode);
    } else {
      rec.index = index;
    }
    // create() 交回 {el, update} 时，刷新走它自己的 update——第 5 个参数只是给
    // 「create 只回裸节点」的调用方兜底的，不能反过来把 api.update 挡在门外。
    if (rec.api && typeof rec.api.update === 'function') {
      try {
        rec.api.update(item, index, rec.el);
      } catch (err) {
        warnUpdateError(err, key);
      }
    } else if (typeof update === 'function') {
      try {
        update(rec.el, item, index);
      } catch (err) {
        warnUpdateError(err, key);
      }
    }
  });

  // 移除消失的记录
  records.forEach((rec, key) => {
    if (seen.has(key)) return;
    records.delete(key);
    if (rec.api && typeof rec.api.destroy === 'function') {
      try {
        rec.api.destroy();
      } catch (err) {
        warnDestroyError(err);
      }
    }
    if (rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
  });

  // 重排：只在顺序真的乱了时动 DOM，顺序一致就一个节点都不碰
  const desired = list.map((item, index) => {
    const rawKey = keyFn(item, index);
    return records.get(`${typeof rawKey}:${String(rawKey)}`);
  });
  let needsReorder = false;
  let cursor = container.firstChild;
  for (const rec of desired) {
    if (!rec) continue;
    if (rec.el === cursor) {
      cursor = cursor.nextSibling;
    } else {
      needsReorder = true;
      break;
    }
  }
  if (needsReorder) {
    const fragment = document.createDocumentFragment();
    desired.forEach((rec) => {
      if (rec) fragment.appendChild(rec.el);
    });
    // 容器里可能还有 update 之外的静态节点，保留在最后
    Array.from(container.childNodes).forEach((n) => fragment.appendChild(n));
    clear(container);
    container.appendChild(fragment);
  }

  return controller;
}

/**
 * 组件 destroy 抛错的兜底。
 * @param {unknown} err
 */
function warnDestroyError(err) {
  if (typeof console !== 'undefined' && typeof console.error === 'function') {
    console.error(`[dom] 列表项销毁时抛出异常：${err && err.message ? err.message : err}`);
  }
}

/**
 * 列表项 update 抛错的兜底（一个坏行不能带崩整张表）。
 * @param {unknown} err
 * @param {string} key
 */
function warnUpdateError(err, key) {
  if (typeof console !== 'undefined' && typeof console.error === 'function') {
    console.error(`[dom] 列表项 ${key} 更新失败：${err && err.message ? err.message : err}`);
  }
}

