/**
 * sandbox-panel.js — 工作台「沙箱」区（§9 重点交互 ②）
 *
 * 职责：
 *   1. 准备沙箱 / 打开目录 / 复制路径 / 清空改动 / 重建沙箱 五个动作。
 *   2. 「清空改动」是本区的第一眼可见按钮（§9 验收标准），带二次确认。
 *   3. 长操作显示进度 + 已用时间 + 可折叠日志（§13.2）。
 *   4. 展示基线完整性结论（来自报告里的 baseline_problems，§4.4）。
 *
 * 状态：empty（无沙箱）/ preparing / ready / resetting / rebuilding / grading / error。
 * 键盘：所有按钮可 Tab；日志折叠是原生 details，Enter / Space 展开。
 * ARIA：进度 role="progressbar"；日志折叠后内容对读屏隐藏；清空/重建用 alertdialog 二次确认。
 *
 * 契约要点：run.sandbox / run.drive / run.baseline_digest 三个字段都是**字符串**。
 * 后端没有暴露沙箱准备过程的日志接口，所以本区日志记的是本机真实发过的每一步
 * （见 console/static/NOTES.md 的契约缺口一节）。
 *
 * 依赖：core/*、components/*
 * 导出：createSandboxPanel(handlers) → { el, update, destroy, doReset, doPrepare }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createButton } from '../../components/button.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createProgress } from '../../components/progress.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createEmptyState } from '../../components/empty-state.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createSkeleton } from '../../components/skeleton.js';

/** 沙箱可用（模型可以动手）的服务端状态。 */
const SANDBOX_OK = new Set(['ready', 'graded']);

/**
 * 创建沙箱区。
 * @param {{
 *   onPrepare: Function, onReset: Function, onRebuild: Function,
 *   onOpenDir: Function, onCopyPath: Function, onReload: Function
 * }} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, doReset: Function, doPrepare: Function}}
 */
export function createSandboxPanel(handlers) {
  let current = { loading: true, run: null, busy: '', error: null, opLog: [], elapsed: 0 };

  const title = el('h2', { class: 'panel__title', id: 'ws-sandbox-title' }, S.SANDBOX_TITLE);
  const headExtra = el('div', { class: 'u-row' });
  const statusHost = el('div', { class: 'sandbox__status' });
  const root = el(
    'section',
    { class: 'panel ws-region', id: 'ws-region-sandbox', 'aria-labelledby': 'ws-sandbox-title' },
    el('div', { class: 'panel__head' }, title, el('span', { class: 'u-spacer' }), headExtra),
    el('p', { class: 'u-faint' }, S.SANDBOX_DESC),
    statusHost,
  );

  // ---- 动作按钮 ----
  const prepareBtn = createButton({
    label: S.SANDBOX_PREPARE,
    variant: 'primary',
    busyLabel: S.SANDBOX_PREPARING,
    onClick: () => handlers.onPrepare(),
  });
  const openDirBtn = createButton({
    label: S.SANDBOX_OPEN_DIR,
    icon: '↗',
    onClick: () => handlers.onOpenDir(),
  });
  const copyPathBtn = createCopyButton({
    label: S.SANDBOX_COPY_PATH,
    icon: '⧉',
    getText: () => (current.run ? current.run.sandbox || '' : ''),
    successMessage: () => S.SANDBOX_PATH_COPIED,
    sourceEl: () => pathValue,
  });
  // 清空改动：工作台第一眼可见（§9），用次强调但不用 danger，免得吓到常规换模型
  const resetBtn = createButton({
    label: S.SANDBOX_RESET,
    icon: '↺',
    reason: S.SANDBOX_IN_USE,
    onClick: () => handlers.onReset(),
  });
  const rebuildBtn = createButton({
    label: S.SANDBOX_REBUILD,
    variant: 'ghost',
    busyLabel: S.SANDBOX_REBUILDING,
    onClick: () => handlers.onRebuild(),
  });
  const actionRow = el(
    'div',
    { class: 'sandbox__actions' },
    prepareBtn.el,
    openDirBtn.el,
    copyPathBtn.el,
    resetBtn.el,
    rebuildBtn.el,
  );

  // ---- 状态与事实 ----
  const statusDot = createStatusDot({ kind: 'idle', text: S.SANDBOX_NO_RUN });
  const driveValue = el('span', { class: 'ws-fact__value' }, S.WS_NO_DRIVE);
  const pathValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const hashValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const facts = el(
    'div',
    { class: 'sandbox__grid' },
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.SANDBOX_DRIVE_LABEL), driveValue),
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.SANDBOX_PATH_LABEL), pathValue),
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.SANDBOX_BASELINE_LABEL), hashValue),
  );

  const integrityList = el('ul', { class: 'integrity-list' });
  const integrityCard = createDetailsCard({
    title: S.SANDBOX_INTEGRITY_TITLE,
    content: integrityList,
    open: false,
  });
  let integrityShown = false;
  /** 上一次渲染过的问题清单签名：变了才重画。 */
  let integritySig = null;

  // ---- 进度 + 日志 ----
  const progress = createProgress({ label: S.PROGRESS_IDLE, state: 'idle' });
  const logBox = el('pre', { class: 'sandbox__log', tabindex: '0', role: 'region' });
  logBox.setAttribute('aria-label', S.SANDBOX_LOG_TITLE);
  const logCard = createDetailsCard({
    title: S.SANDBOX_LOG_TITLE,
    content: logBox,
    open: false,
  });
  const logCount = el('span', { class: 'u-faint' });
  /** 上一次是否在进行中：只在状态翻转时才自动开合日志卡。 */
  let lastRunning = null;

  // 空态里的「准备沙箱」必须是**另一个**按钮实例：同一个 DOM 节点没法同时挂在
  // actionRow 和空态下，appendChild 会把它从 actionRow 搬走，于是沙箱建好之后
  // 工具条上的准备按钮就永久消失了。
  const emptyPrepareBtn = createButton({
    label: S.SANDBOX_PREPARE,
    variant: 'primary',
    onClick: () => handlers.onPrepare(),
  });
  const emptyState = createEmptyState({
    icon: 'folder',
    title: S.SANDBOX_NO_RUN,
    desc: S.SANDBOX_NO_RUN_DESC,
    actions: [emptyPrepareBtn.el],
  });
  const skeleton = createSkeleton({ rows: 2, variant: 'row', label: S.STATE_LOADING });

  /**
   * 状态 → status-dot 的 kind。
   * @param {string} status
   * @returns {string}
   */
  function dotKind(status) {
    if (SANDBOX_OK.has(status)) return 'ok';
    if (status === 'preparing' || status === 'grading' || status === 'queued') return 'busy';
    if (status === 'error') return 'error';
    return 'idle';
  }

  /**
   * 服务端状态 → 中文状态词。
   * @param {string} status
   * @returns {string}
   */
  function statusText(status) {
    if (status === 'preparing') return S.RUN_STATUS_PREPARING;
    if (status === 'queued') return S.RUN_STATUS_QUEUED;
    if (status === 'grading') return S.RUN_STATUS_GRADING;
    if (status === 'graded') return S.RUN_STATUS_GRADED;
    if (status === 'error') return S.RUN_STATUS_ERROR;
    return S.SANDBOX_PREPARED;
  }

  /**
   * 本机长操作 → 进度条文案。
   * @param {string} busy
   * @returns {string}
   */
  function progressLabel(busy) {
    if (busy === 'reset') return S.PROGRESS_RESET;
    if (busy === 'rebuild') return S.PROGRESS_REBUILD;
    if (busy === 'grade') return S.PROGRESS_GRADE;
    return S.PROGRESS_PREPARE;
  }

  /**
   * 渲染本机操作日志：进行中默认展开，结束后自动收起（§13.2）。
   * 只在「开始 / 结束」这两个时刻改 open，用户中途手动开合过就不再被覆盖。
   * @param {boolean} running
   */
  function renderLog(running) {
    const lines = current.opLog || [];
    setText(logBox, lines.length ? lines.join('\n') : S.SANDBOX_LOG_EMPTY);
    setText(logCount, lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '');
    const changed = lastRunning !== running;
    lastRunning = running;
    logCard.update({
      open: changed ? running : undefined,
      content: logBox,
      hint: logCount.textContent,
    });
    if (running) logBox.scrollTop = logBox.scrollHeight;
  }

  /**
   * 渲染基线完整性：结论来自报告里的 baseline_problems（契约里没有独立的完整性接口）。
   * @param {object} run
   */
  function renderIntegrity(run) {
    if (!run.report) {
      integrityShown = false;
      integritySig = null;
      return;
    }
    integrityShown = true;
    const problems = run.report.baseline_problems || [];
    // 只有问题清单变了才重画，避免轮询把用户展开的卡片又折回去
    const sig = problems.map((p) => `${(p && (p.message || p.path)) || ''}`).join('\n');
    if (sig === integritySig) return;
    integritySig = sig;
    integrityList.textContent = '';
    if (problems.length) {
      problems.forEach((p) => {
        integrityList.appendChild(
          el(
            'li',
            { class: 'integrity-item' },
            el('span', { 'aria-hidden': 'true' }, '✕'),
            el('span', {}, (p && (p.message || p.path)) || ''),
          ),
        );
      });
      integrityCard.update({
        title: `${S.SANDBOX_INTEGRITY_TITLE}（${problems.length}）`,
        content: integrityList,
        open: true,
      });
    } else {
      integrityList.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, '✓'),
          el('span', {}, S.SANDBOX_INTEGRITY_OK),
        ),
      );
      integrityCard.update({ title: S.SANDBOX_INTEGRITY_TITLE, content: integrityList, open: false });
    }
  }

  /**
   * 差异更新。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    statusHost.textContent = '';
    headExtra.textContent = '';

    if (current.loading) {
      statusHost.appendChild(skeleton.el);
      return;
    }

    if (current.error) {
      statusHost.appendChild(
        createEmptyState({
          title: S.ERR_LOAD,
          desc: S.ERR_LOAD_BODY,
          alert: true,
          // 重试只做只读回读。原先挂的是 handlers.onPrepare()，等于「读取失败 →
          // 点重试」直接开出一轮新的沙箱准备（写操作），和按钮语义对不上。
          actions: [createButton({ label: S.ACTION_RETRY, onClick: () => handlers.onReload() }).el],
        }).el,
      );
      return;
    }

    const run = current.run;
    const busy = current.busy || '';

    // 动作区：始终在第一屏可见
    if (run) {
      statusHost.appendChild(actionRow);
    } else {
      emptyState.update({});
      statusHost.appendChild(emptyState.el);
    }

    if (!run) {
      // 首次「准备沙箱」是**同步**长请求（POST /api/runs 阻塞到沙箱铺完，
      // config.json 的 timeouts.prepare_s 上限 180 秒），这期间 run 仍然是 null。
      // 忙态与进度条必须在这个分支里就画出来：下面那段只有 run 存在才走得到，
      // 原先在这里直接 return，导致第一次准备时按钮标签和进度条永远不更新，
      // 使用者读到的是「点了没反应」。
      const starting = busy === 'prepare' || busy === 'reset' || busy === 'rebuild';
      const preparing = busy === 'prepare';
      statusDot.update({
        kind: starting ? 'busy' : 'idle',
        text: starting ? progressLabel(busy) : S.SANDBOX_NO_RUN,
      });
      headExtra.appendChild(statusDot.el);
      emptyPrepareBtn.update({
        loading: preparing,
        busyLabel: S.SANDBOX_PREPARING,
        disabled: starting,
        reason: preparing ? S.SANDBOX_PREPARING : '',
      });
      if (starting) {
        progress.update({
          state: 'running',
          determinate: false,
          label: progressLabel(busy),
          elapsed: Math.floor((current.elapsed || 0) / 1000),
          total: null,
        });
        statusHost.appendChild(progress.el);
      } else {
        progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
      }
      renderLog(starting);
      statusHost.appendChild(logCard.el);
      return;
    }

    // 状态行
    const sandboxOk = SANDBOX_OK.has(run.status);
    statusDot.update({ kind: dotKind(run.status), text: statusText(run.status) });
    headExtra.appendChild(statusDot.el);
    if (run.revealed) {
      headExtra.appendChild(el('span', { class: 'badge badge--danger' }, S.WS_REVEALED_FLAG));
    }

    statusHost.appendChild(facts);
    setText(driveValue, run.sandbox || S.WS_NO_DRIVE);
    setText(pathValue, run.sandbox || '—');
    setText(hashValue, run.baseline_digest || '—');

    // 按钮状态
    const preparing = busy === 'prepare';
    const mutating = busy === 'reset' || busy === 'rebuild';
    const grading = busy === 'grade' || run.status === 'grading';

    prepareBtn.update({
      loading: preparing,
      busyLabel: S.SANDBOX_PREPARING,
      label: run ? S.SANDBOX_PREPARED : S.SANDBOX_PREPARE,
      disabled: mutating || grading || preparing,
      reason: mutating || grading ? S.SANDBOX_IN_USE : '',
    });
    openDirBtn.update({ disabled: mutating || grading, reason: mutating || grading ? S.SANDBOX_IN_USE : '' });
    copyPathBtn.update({ label: S.SANDBOX_COPY_PATH, getText: () => run.sandbox || '' });
    // 清空改动：沙箱不在、或正忙时禁用，并说明原因
    resetBtn.update({
      loading: busy === 'reset',
      busyLabel: S.SANDBOX_RESETTING,
      disabled: !sandboxOk || mutating || grading || preparing,
      reason: !run.sandbox
        ? S.ERR_NO_SANDBOX
        : grading
          ? S.SANDBOX_GRADING
          : sandboxOk
            ? ''
            : S.SANDBOX_IN_USE,
    });
    rebuildBtn.update({
      loading: busy === 'rebuild',
      busyLabel: S.SANDBOX_REBUILDING,
      disabled: mutating || grading,
      reason: grading ? S.SANDBOX_GRADING : '',
    });

    // 进度：长操作期间显示已用时间（§13.2）
    const isProgressing = preparing || mutating || grading;
    if (isProgressing) {
      progress.update({
        state: 'running',
        determinate: false,
        label: progressLabel(busy),
        elapsed: Math.floor((current.elapsed || 0) / 1000),
        total: null,
      });
      statusHost.appendChild(progress.el);
    } else {
      progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
    }

    renderLog(isProgressing);
    statusHost.appendChild(logCard.el);

    renderIntegrity(run);
    if (integrityShown) statusHost.appendChild(integrityCard.el);
  }

  update({});

  return {
    el: root,
    update,
    /** 供快捷键 R 触发清空。 */
    doReset: () => handlers.onReset(),
    /** 供空态按钮触发准备。 */
    doPrepare: () => handlers.onPrepare(),
    /** 解绑（§10.4）。 */
    destroy() {
      [prepareBtn, emptyPrepareBtn, openDirBtn, resetBtn, rebuildBtn].forEach((b) => b.destroy());
      copyPathBtn.destroy();
      progress.destroy();
      logCard.destroy();
      integrityCard.destroy();
      emptyState.destroy();
      skeleton.destroy();
      statusDot.destroy();
    },
  };
}
