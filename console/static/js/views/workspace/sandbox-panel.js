/**
 * sandbox-panel.js — 工作台「沙箱」折叠卡（2026-10-01 改版）
 *
 * 职责：
 *   1. 卡头一行状态（就绪 / 已改动 n 个文件 / 正在准备…）+ 状态点。
 *   2. 动作一行排开：打开沙箱目录 / 清空改动 / 重建沙箱；没有沙箱时空态给 [准备沙箱]。
 *   3. 路径、工作区、基线、开始时间、轮次、运行编号、日志、完整性自检全部收进「详情」
 *      折叠块 —— 术语不裸奔，第一眼只有状态和动作。
 *   4. 长操作显示进度 + 已用时间；进行中自动展开「详情」让日志可见，结束不强行收起。
 *   5. 没有沙箱时整卡自动展开（露出准备 CTA），建好沙箱自动收起给结果让位。
 *
 * 契约要点：run.sandbox / run.drive / run.baseline_digest 都是**字符串**；
 * 「已改动 n 个文件」来自 run.report.diff.files（跑过校验才有）。
 *
 * 依赖：core/*、components/*
 * 导出：createSandboxPanel(handlers) → { el, update, destroy, setOpen, copyPath }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createButton } from '../../components/button.js';
import { createCopyButton } from '../../components/copy-button.js';
import { createProgress } from '../../components/progress.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createEmptyState } from '../../components/empty-state.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createBadge } from '../../components/badge.js';
import { createSkeleton } from '../../components/skeleton.js';
import { relativeTime, fullTime } from '../../core/format.js';

/** 秒数 → 「12 分 34 秒」；够短就说秒，够长就说小时，别让人自己换算。 */
function humanSeconds(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const parts = [];
  if (hours) parts.push(`${hours} 小时`);
  if (minutes) parts.push(`${minutes} 分`);
  if (rest && !hours) parts.push(`${rest} 秒`);
  return parts.join(' ') || '不到 1 分钟';
}



/** 沙箱可用（模型可以动手 / 可以校验）的服务端状态。 */
const SANDBOX_OK = new Set(['ready', 'graded']);

/** 本卡新增文案。 */
const T = {
  CHANGED: '已改动 {n} 个文件',
  DETAILS: '详情',
  FACT_ROUND_STARTED: '本轮开始于',
  FACT_MODEL_WORK: '模型工作时长',
};

/**
 * 创建沙箱卡。
 * @param {{
 *   onPrepare: Function, onReset: Function, onRebuild: Function,
 *   onOpenDir: Function, onReload: Function
 * }} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, setOpen: Function, copyPath: Function}}
 */
export function createSandboxPanel(handlers) {
  let current = { loading: true, run: null, busy: '', error: null, opLog: [], elapsed: 0 };
  /** 上一次「有没有沙箱」：翻转时才自动开合整卡，用户手动的开合不被轮询覆盖。 */
  let lastHadRun = null;
  /** 上一次是否在长操作中：只在开始那一刻自动展开详情（日志可见）。 */
  let lastBusy = false;
  /** 日志折叠块上一次的运行态：只在翻转时改 open。 */
  let lastLogRunning = null;
  /** 上一次渲染过的完整性问题清单签名：没变就不重画。 */
  let integritySig = null;

  // ---- 卡片骨架 ----
  const title = el('h2', { class: 'ws-card__title', id: 'ws-sandbox-title' }, S.SANDBOX_TITLE);
  const statusDot = createStatusDot({ kind: 'idle', text: S.SANDBOX_NO_RUN });
  const cardAside = el('span', { class: 'ws-card__aside u-faint u-truncate' }, S.SANDBOX_NO_RUN);
  const chevron = el('span', { class: 'ws-card__chevron', 'aria-hidden': 'true' }, '›');
  const summary = el(
    'summary',
    { class: 'ws-card__summary' },
    title,
    statusDot.el,
    el('span', { class: 'u-spacer' }),
    cardAside,
    chevron,
  );
  const bodyHost = el('div', { class: 'ws-card__body' });
  const root = el('details', { class: 'ws-card ws-region', id: 'ws-region-sandbox' }, summary, bodyHost);

  // ---- 动作 ----
  // 清空改动：本卡第一眼可见的动作（§9），用默认样式不用 danger，免得吓到常规换模型
  const resetBtn = createButton({
    label: S.SANDBOX_RESET,
    icon: '↺',
    onClick: () => handlers.onReset(),
  });
  const openDirBtn = createButton({
    label: S.SANDBOX_OPEN_DIR,
    icon: '↗',
    onClick: () => handlers.onOpenDir(),
  });
  const rebuildBtn = createButton({
    label: S.SANDBOX_REBUILD,
    variant: 'ghost',
    busyLabel: S.SANDBOX_REBUILDING,
    onClick: () => handlers.onRebuild(),
  });
  // 回收沙箱：交完卷后把占着磁盘的工作区关掉（记录与报告留在 runs/）。
  // 校验完的单轮 run 以前只有「清空改动 / 重建沙箱」，两者都会再占一遍磁盘。
  const releaseBtn = createButton({
    label: S.BATCH_RELEASE_SANDBOX,
    variant: 'ghost',
    busyLabel: S.BATCH_RELEASING,
    onClick: () => handlers.onRelease(),
  });
  const actionRow = el(
    'div',
    { class: 'u-row ws-sandbox__actions' },
    openDirBtn.el,
    resetBtn.el,
    rebuildBtn.el,
    releaseBtn.el,
  );

  // 空态里的「准备沙箱」必须是**另一个**按钮实例：同一 DOM 节点没法同时挂在
  // 空态和长操作流程里，appendChild 会把它搬走，沙箱建好后就找不到了。
  const emptyPrepareBtn = createButton({
    label: S.SANDBOX_PREPARE,
    variant: 'primary',
    busyLabel: S.SANDBOX_PREPARING,
    onClick: () => handlers.onPrepare(),
  });
  const emptyState = createEmptyState({
    icon: 'folder',
    title: S.SANDBOX_NO_RUN,
    desc: S.SANDBOX_NO_RUN_DESC,
    actions: [emptyPrepareBtn.el],
  });
  const skeleton = createSkeleton({ rows: 2, variant: 'row', label: S.STATE_LOADING });
  const errorState = createEmptyState({
    title: S.ERR_LOAD,
    desc: S.ERR_LOAD_BODY,
    alert: true,
    // 重试只做只读回读：挂 onPrepare 会变成「读取失败 → 点重试」直接开出新一轮
    actions: [createButton({ label: S.ACTION_RETRY, onClick: () => handlers.onReload() }).el],
  });

  // ---- 进度 ----
  const progress = createProgress({ label: S.PROGRESS_IDLE, state: 'idle' });

  // ---- 详情折叠块：事实 + 完整性 + 日志 ----
  const copyPathBtn = createCopyButton({
    label: S.SANDBOX_COPY_PATH,
    size: 'sm',
    getText: () => (current.run ? current.run.sandbox || '' : ''),
    successMessage: () => S.SANDBOX_PATH_COPIED,
  });
  const pathValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const driveValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const hashValue = el('span', { class: 'ws-fact__value u-mono' }, '—');
  const startedValue = el('span', { class: 'ws-fact__value' }, '—');
  const workValue = el('span', { class: 'ws-fact__value' }, '—');
  const attemptValue = el('span', { class: 'ws-fact__value' }, '—');
  const runIdValue = el('span', { class: 'ws-fact__value u-mono' }, '—');

  /**
   * 一格事实（label 在上、值在下）。
   * @param {string} label
   * @param {HTMLElement} value
   * @returns {HTMLElement}
   */
  function fact(label, value) {
    return el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, label), value);
  }

  const facts = el(
    'div',
    { class: 'ws-facts' },
    fact(S.SANDBOX_PATH_LABEL, el('span', { class: 'u-row u-row-tight' }, pathValue, copyPathBtn.el)),
    fact(S.SANDBOX_DRIVE_LABEL, driveValue),
    fact(T.FACT_ROUND_STARTED, startedValue),
    fact(T.FACT_MODEL_WORK, workValue),
    fact(S.RUN_ATTEMPT_LABEL, attemptValue),
    fact(S.RUN_RUN_ID_LABEL, runIdValue),
    fact(S.SANDBOX_BASELINE_LABEL, hashValue),
  );

  const integrityList = el('ul', { class: 'integrity-list' });
  const integrityCard = createDetailsCard({ title: S.SANDBOX_INTEGRITY_TITLE, content: integrityList, open: false });

  const logBox = el('pre', { class: 'ws-log', tabindex: '0', role: 'region' });
  logBox.setAttribute('aria-label', S.SANDBOX_LOG_TITLE);
  const logCount = el('span', { class: 'u-faint' });
  const logCard = createDetailsCard({ title: S.SANDBOX_LOG_TITLE, content: logBox, open: false });

  const detailBody = el('div', { class: 'u-stack' }, facts, integrityCard.el, logCard.el);
  const detailCard = createDetailsCard({ title: T.DETAILS, content: detailBody, open: false });

  /**
   * 服务端状态 → 状态点类型。
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
    return S.RUN_STATUS_READY;
  }

  /**
   * 卡头摘要一句话：可用状态优先报「改了几个文件」，其次报服务端状态词。
   * 没有沙箱时留空——卡头的状态点和卡内空态已经说清楚了，标题旁再挂一遍
   * 「还没有沙箱」会变成同一句话出现三次。
   * @param {object} run
   * @returns {string}
   */
  function asideText(run) {
    const diff = run.report && run.report.diff;
    const files = diff ? Number(diff.files || 0) : 0;
    if (SANDBOX_OK.has(run.status) && files > 0) return t(T.CHANGED, { n: files });
    if (!SANDBOX_OK.has(run.status)) return '';
    return statusText(run.status);
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
   * 渲染本机操作日志：只在「开始 / 结束」翻转时改 open（开始展开、结束收起），
   * 用户中途手动开合过就不再被轮询覆盖（§13.2）。
   * @param {boolean} running
   */
  function renderLog(running) {
    const changed = lastLogRunning !== running;
    lastLogRunning = running;
    const lines = current.opLog || [];
    setText(logBox, lines.length ? lines.join('\n') : S.SANDBOX_LOG_EMPTY);
    setText(logCount, lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '');
    logCard.update({ content: logBox, hint: logCount.textContent });
    if (changed) logCard.setOpen(running);
    if (running) logBox.scrollTop = logBox.scrollHeight;
  }

  /**
   * 渲染基线完整性：结论来自报告里的 baseline_problems（没有独立完整性接口）。
   * 只在结论变化时重画，避免轮询把用户展开的卡片又折回去。
   * @param {object} run
   */
  function renderIntegrity(run) {
    const problems = run.report ? run.report.baseline_problems || [] : null;
    const sig = problems
      ? problems.map((p) => (p && (p.message || p.path)) || '').join('\n')
      : 'pending';
    if (sig === integritySig) return;
    integritySig = sig;
    if (!problems) {
      integrityCard.update({ content: el('p', { class: 'u-faint' }, S.SANDBOX_INTEGRITY_EMPTY) });
      return;
    }
    const list = el('ul', { class: 'integrity-list' });
    if (problems.length) {
      problems.forEach((p) => {
        list.appendChild(
          el(
            'li',
            { class: 'integrity-item' },
            el('span', { 'aria-hidden': 'true' }, '✕'),
            el('span', {}, (p && (p.message || p.path)) || ''),
          ),
        );
      });
      integrityCard.update({ title: `${S.SANDBOX_INTEGRITY_TITLE}（${problems.length}）`, content: list, open: true });
    } else {
      list.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, '✓'),
          el('span', {}, S.SANDBOX_INTEGRITY_OK),
        ),
      );
      integrityCard.update({ title: S.SANDBOX_INTEGRITY_TITLE, content: list, open: false });
    }
  }

  /**
   * 渲染详情数据（只在有 run 时调用）。
   * @param {object} run
   */
  function renderFacts(run) {
    setText(pathValue, run.sandbox || '—');
    setText(driveValue, run.drive || '—');
    setText(hashValue, run.baseline_digest || '—');
    setText(runIdValue, run.run_id || '—');
    // 「本轮开始于」而不是「记录建号于」：重建/清空之后 created_at 仍是几个月前，
    // 用它显示出来的时间会把上一个模型和所有挂机时间累计进来。
    const roundStart = run.round_started_at || run.created_at;
    if (roundStart) {
      setText(startedValue, relativeTime(roundStart));
      startedValue.title = fullTime(roundStart);
    } else {
      setText(startedValue, '—');
      startedValue.removeAttribute('title');
    }
    if (typeof run.model_work_seconds === 'number') {
      setText(workValue, humanSeconds(run.model_work_seconds));
      workValue.title = '只算模型被叫起来干活的时长（含工具轮），不含你思考与挂机的时间';
    } else {
      setText(workValue, '—');
      workValue.removeAttribute('title');
    }
    setText(attemptValue, `${run.attempt} / ${run.attempts_allowed}`);
    copyPathBtn.update({ getText: () => run.sandbox || '' });
  }

  /**
   * 差异更新。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    bodyHost.textContent = '';

    // 整卡自动开合只在「还没有 run」时展开露出准备 CTA；以前沙箱建好就自动收起，
    // 把清空/重建/回收这一卡的主内容一起藏掉，用户要找出口就得手动展开。
    const hadRun = Boolean(current.run);
    if (lastHadRun !== hadRun) {
      lastHadRun = hadRun;
      if (!hadRun) root.open = true;
    }

    if (current.loading) {
      setText(cardAside, S.STATE_LOADING);
      statusDot.update({ kind: 'busy', text: S.STATE_LOADING });
      bodyHost.appendChild(skeleton.el);
      return;
    }

    if (current.error) {
      root.open = true; // 错误不能藏在收起的卡里
      setText(cardAside, S.ERR_LOAD);
      statusDot.update({ kind: 'error', text: S.ERR_LOAD });
      errorState.update({});
      bodyHost.appendChild(errorState.el);
      return;
    }

    const run = current.run;
    const busy = current.busy || '';

    // 首次「准备沙箱」是**同步**长请求（POST /api/runs 阻塞到沙箱铺完，上限 180 秒），
    // 这期间 run 仍是 null。忙态与进度条必须在这个分支就画出来，否则读起来像「点了没反应」。
    if (!run) {
      const starting = busy === 'prepare' || busy === 'reset' || busy === 'rebuild';
      const preparing = busy === 'prepare';
      statusDot.update({ kind: starting ? 'busy' : 'idle', text: starting ? progressLabel(busy) : S.SANDBOX_NO_RUN });
      // 摘要只在长操作时报进度；静止的空态留给卡内空态说明，卡头不重复。
      setText(cardAside, starting ? progressLabel(busy) : '');
      emptyState.update({});
      bodyHost.appendChild(emptyState.el);
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
        bodyHost.appendChild(progress.el);
      } else {
        progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
      }
      renderLog(starting);
      bodyHost.appendChild(detailCard.el);
      return;
    }

    // ---- 有 run ----
    const sandboxOk = SANDBOX_OK.has(run.status);
    statusDot.update({ kind: dotKind(run.status), text: statusText(run.status) });
    setText(cardAside, asideText(run));

    if (run.revealed) {
      bodyHost.appendChild(
        el(
          'div',
          { class: 'u-row' },
          createBadge({ label: S.WS_REVEALED_FLAG, variant: 'danger', glyph: '✕' }).el,
          el('span', { class: 'u-faint' }, S.WS_REVEALED_NOTE),
        ),
      );
    }
    bodyHost.appendChild(actionRow);
    renderFacts(run);

    // 按钮状态：任何长操作期间统一禁用并说明原因
    const preparing = busy === 'prepare';
    const mutating = busy === 'reset' || busy === 'rebuild' || busy === 'release';
    const grading = busy === 'grade' || run.status === 'grading';
    openDirBtn.update({
      // 回收之后 run.sandbox 是空的：以前按钮照样可点，点了静默 return（像坏了）
      disabled: !run.sandbox || mutating || grading || preparing,
      reason: mutating || grading
        ? S.SANDBOX_GRADING
        : preparing
          ? S.SANDBOX_PREPARING
          : !run.sandbox
            ? S.ERR_NO_SANDBOX
            : '',
    });
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
            : S.RUN_STATUS_PREPARING,
    });
    rebuildBtn.update({
      loading: busy === 'rebuild',
      busyLabel: S.SANDBOX_REBUILDING,
      disabled: mutating || grading || preparing,
      reason: grading ? S.SANDBOX_GRADING : '',
    });
    releaseBtn.update({
      loading: busy === 'release',
      busyLabel: S.BATCH_RELEASING,
      disabled: !run.sandbox || mutating || grading || preparing,
      reason: !run.sandbox
        ? S.ERR_NO_SANDBOX
        : grading
          ? S.SANDBOX_GRADING
          : preparing
            ? S.SANDBOX_PREPARING
            : '',
    });

    // 进度：长操作期间显示已用时间（§13.2）
    const isBusy = preparing || mutating || grading;
    if (isBusy) {
      progress.update({
        state: 'running',
        determinate: false,
        label: progressLabel(busy),
        elapsed: Math.floor((current.elapsed || 0) / 1000),
        total: null,
      });
      bodyHost.appendChild(progress.el);
    } else {
      progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
    }

    // 开始长操作的那一刻自动展开详情（日志要看得见）；结束不强行收起
    if (lastBusy !== isBusy) {
      lastBusy = isBusy;
      if (isBusy) detailCard.setOpen(true);
    }

    renderLog(isBusy);
    renderIntegrity(run);
    bodyHost.appendChild(detailCard.el);
  }

  update({});

  return {
    el: root,
    update,
    /** 展开/收起卡片。 */
    setOpen(open) {
      root.open = Boolean(open);
    },
    /**
     * 复制沙箱路径（工作台「打开目录」动作的三级降级兜底）。
     * @param {string} path
     * @returns {Promise<boolean>}
     */
    async copyPath(path) {
      copyPathBtn.update({ getText: () => path || '' });
      return copyPathBtn.copy();
    },
    /** 解绑（§10.4）。 */
    destroy() {
      [emptyPrepareBtn, openDirBtn, resetBtn, rebuildBtn].forEach((b) => b.destroy());
      copyPathBtn.destroy();
      progress.destroy();
      integrityCard.destroy();
      logCard.destroy();
      detailCard.destroy();
      emptyState.destroy();
      errorState.destroy();
      skeleton.destroy();
      statusDot.destroy();
    },
  };
}
