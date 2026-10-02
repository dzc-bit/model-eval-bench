/**
 * report-node.js — 对话流里的校验结果条（2026-10-02 工作台二次优化；完整报告移入 report-modal.js）
 *
 * 对话流里只留一个**轻量结果条**，完整报告在「校验报告」独立窗口（report-modal.js）：
 *   - 进行中：状态点 + 进度 + 已用时间 + 实时日志（运行中默认展开，结束自动收起）——
 *     校验是异步长操作，进行中的反馈必须留在流里，不能等出分才知道它在跑；
 *   - 完成：一行结果条 = ✓/✗ + 部分分 + 分组概要（+ ● 新结果）+「查看完整报告」。
 *     结果条是「发生过校验」这个事实与回看入口的常列出口（红线：出口不许消失），
 *     点击 reopen 报告窗口；校验完成只给 polite 播报 + 结果条，不自动弹窗（§13.2）。
 *
 * 动作不在本节点：每个时刻的唯一主按钮在底部操作栏（dock.js）；与结果相关的
 * 次要出口（作废 / 揭晓 / 导出）在报告窗口 footer 与 ⋯ 菜单各有一份。
 *
 * 状态：hidden（没跑过也不在跑）/ running / done。
 *
 * 契约要点：run.report.score / passed / invalidated / p2p_broken / error /
 *   summary{green, groups[]}；run.log 是校验日志（status ∈ grading/graded/error 时才有）。
 *
 * 依赖：core/*、components/*
 * 导出：createReportNode(handlers) → { el, update, destroy, focusResult }
 */

import { el, setText } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createProgress } from '../../components/progress.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createButton } from '../../components/button.js';
import { kindForReport } from '../../components/result-mark.js';
import { percent } from '../../core/format.js';

/** 改版新增文案（strings.js 冻结，新增走本地常量）。 */
const T = {
  RUNNING_TITLE: '校验进行中',
  RESULT_ANCHOR: '校验结果',
  VIEW_FULL_REPORT: '查看完整报告',
  VIEW_FULL_REPORT_HINT: '红绿横幅、分组明细、失败摘要、diff 统计、执行详情与校验日志都在里面',
  // 结果条的一句话结论：作废/出错优先于通过/未过
  BAR_INVALID: '本轮作废：{reason}',
  BAR_ERROR: '校验过程出错',
  BAR_SUMMARY: '{pass}/{total} 组通过',
};

/**
 * 创建校验结果条节点。
 * @param {{onOpenReport?: Function}} [handlers] onOpenReport：打开「校验报告」窗口
 * @returns {{el: HTMLElement, update: Function, destroy: Function, focusResult: Function}}
 */
export function createReportNode(handlers = {}) {
  let current = {
    run: null,
    busy: '',
    elapsed: 0,
    newResult: false,
    revealed: null,
    modelMismatch: false,
  };
  /** 上一次是否在跑：只在状态翻转时自动开合日志卡。 */
  let lastRunning = null;

  const root = el('section', {
    class: 'ws-node ws-report ws-region',
    id: 'ws-region-grade',
    'aria-label': S.GRADE_TITLE,
    hidden: true,
  });

  // ---- 进行中：状态点 + 进度 + 实时日志 ----
  const progress = createProgress({ label: S.PROGRESS_IDLE, state: 'idle' });
  const logBox = el('pre', { class: 'ws-log', tabindex: '0', role: 'region' });
  logBox.setAttribute('aria-label', S.GRADE_LOG_TITLE);
  const logCount = el('span', { class: 'u-faint' });
  const logCard = createDetailsCard({ title: S.GRADE_LOG_TITLE, content: logBox, open: false });
  const runningBox = el(
    'div',
    { class: 'ws-report__running', hidden: true },
    el('div', { class: 'u-row' },
      createStatusDot({ kind: 'busy', text: T.RUNNING_TITLE }).el,
      el('span', { class: 'u-faint' }, S.GRADE_RUNNING)),
    progress.el,
    logCard.el,
  );

  // ---- 完成：一行轻量结果条 ----
  const barGlyph = el('span', { class: 'ws-report__bar-glyph', 'aria-hidden': 'true' }, '');
  const barScore = el('span', { class: 'ws-report__bar-score' }, '');
  const barSummary = el('span', { class: 'ws-report__bar-summary u-faint' }, '');
  const newFlag = el('span', { class: 'grade__new-flag' }, `● ${S.GRADE_NEW_RESULT}`);
  newFlag.hidden = true;
  const openBtn = createButton({
    label: T.VIEW_FULL_REPORT,
    variant: 'ghost',
    size: 'sm',
    title: T.VIEW_FULL_REPORT_HINT,
    onClick: () => {
      if (typeof handlers.onOpenReport === 'function') handlers.onOpenReport();
    },
  });
  const bar = el(
    'div',
    { class: 'ws-report__bar', tabindex: '-1', role: 'region', 'aria-label': T.RESULT_ANCHOR, hidden: true },
    barGlyph,
    barScore,
    barSummary,
    newFlag,
    el('span', { class: 'u-spacer' }),
    openBtn.el,
  );

  root.replaceChildren(runningBox, bar);

  /**
   * 渲染校验日志：进行中默认展开、结束后自动收起（§13.2）；中途手动开合不被覆盖。
   * @param {boolean} running
   * @param {string[]} lines
   */
  function renderLog(running, lines) {
    setText(logBox, lines.length ? lines.join('\n') : S.GRADE_LOG_EMPTY);
    setText(logCount, lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '');
    const changed = lastRunning !== running;
    lastRunning = running;
    logCard.update({ content: logBox, hint: logCount.textContent });
    if (changed) logCard.setOpen(running);
    if (running) logBox.scrollTop = logBox.scrollHeight;
  }

  /**
   * 渲染完成态结果条：✓/✗ + 部分分 + 分组概要 + 新结果标记 + 查看完整报告。
   * 图标 + 文字 + 颜色三重编码（§12：颜色不单传语义）。
   * @param {object} report
   * @param {object} run
   */
  function renderBar(report, run) {
    const groups = report.groups || [];
    const sum = report.summary || {};
    const green = typeof sum.green === 'number' ? sum.green : groups.filter((g) => g.passed).length;
    const failed = kindForReport(report) === 'fail';
    bar.classList.toggle('ws-report__bar--pass', !failed && Boolean(report.passed));
    bar.classList.toggle('ws-report__bar--fail', failed || !report.passed);
    setText(barGlyph, report.passed && !failed ? '✓' : '✕');
    setText(barScore, percent((report.score || 0) / 100));
    let summary;
    if (report.invalidated) {
      summary = t(T.BAR_INVALID, { reason: report.invalid_reason || '' });
    } else if (report.error) {
      summary = T.BAR_ERROR;
    } else if (report.p2p_broken) {
      summary = S.GRADE_DONE_VOID;
    } else {
      summary = t(T.BAR_SUMMARY, { pass: green, total: groups.length });
    }
    setText(barSummary, summary);
    newFlag.hidden = !current.newResult;
    void run;
  }

  /**
   * 差异更新。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };

    const run = current.run;
    const report = run ? run.report : null;
    // 校验是异步的：服务端 status=grading 才是真在跑
    const running = current.busy === 'grade' || Boolean(run && run.status === 'grading');

    // 没跑过也不在跑：整个节点隐藏（动作在底部操作栏，空态不需要再占一屏）
    if (!run || (!report && !running)) {
      root.hidden = true;
      return;
    }
    root.hidden = false;

    runningBox.hidden = !running;
    bar.hidden = running;

    if (running) {
      progress.update({
        state: 'running',
        determinate: false,
        label: S.PROGRESS_GRADE,
        elapsed: Math.floor((current.elapsed || 0) / 1000),
        total: null,
      });
      renderLog(true, (run && run.log) || []);
    } else {
      progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
      renderLog(false, (run && run.log) || []);
      if (report) renderBar(report, run);
    }
  }

  update({});

  return {
    el: root,
    update,
    /** 结果条拿到焦点（结果提醒用；结果条本身 tabindex="-1"，不进 Tab 序）。 */
    focusResult() {
      root.hidden = false;
      if (!bar.hidden) bar.focus({ preventScroll: true });
    },
    /** 解绑（§10.4）。 */
    destroy() {
      progress.destroy();
      logCard.destroy();
      openBtn.destroy();
    },
  };
}
