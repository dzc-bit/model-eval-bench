/**
 * grade-panel.js — 工作台「校验」折叠卡（2026-10-01 改版）
 *
 * 职责：
 *   1. [运行校验]（本卡唯一强调按钮，G 键保留）→ 实时进度（已用时间）→ 可折叠实时日志。
 *   2. 校验完成跳出结果横幅：通过=绿 tint / 未过=红 tint，总分 + 分组概要在第一眼位置；
 *      横幅内直接给 [进入下一轮]，机会用尽则给一句说明。
 *   3. 分组明细按行渲染（绿组一句话，红组可展开失败摘要），其后依次是回归 / 越界 / 噪声 /
 *      相似度 / diff / 与上一轮对比 / 评分执行详情 / 参考解，全部沿用原有渲染器。
 *   4. 校验完成不跳页：结果区出现「新结果」标记并 polite 播报（§13.2）。
 *
 * 状态：empty（没跑过）/ running / done / error。
 * 键盘：日志与明细折叠用原生 details；结果区 tabindex="-1" 供播报后跳转。
 * ARIA：progressbar + 中文时间文本；组状态「图标 + 文字 + 颜色」三重编码；失败明细 role="list"。
 *
 * 契约要点（消费的 run.report 结构对照 harness/grade.py + harness/report.py）：
 *   score / passed / invalidated / invalid_reason / p2p_broken / error
 *   groups[]{id, title, weight, passed, cases[], total, passed_count}
 *   regressions[] / violations[] / noise[] / similarity{} / diff{} / comparison{} /
 *   next_hint{} / summary{groups[], green, red} / checks[]
 *
 * 依赖：core/*、components/*
 * 导出：createGradePanel(handlers) → { el, update, destroy, setOpen, focusResult, doGrade }
 */

import { el, setText, patchList, clear } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createButton } from '../../components/button.js';
import { createProgress } from '../../components/progress.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createEmptyState } from '../../components/empty-state.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createBadge } from '../../components/badge.js';
import { createResultMark, kindForReport } from '../../components/result-mark.js';
import { percent } from '../../core/format.js';

/** 沙箱可校验的服务端状态。 */
const SANDBOX_OK = new Set(['ready', 'graded']);

/**
 * 创建校验卡。
 * @param {{
 *   onGrade: Function, onPromote: Function, onReveal: Function,
 *   onExport: Function, onGoChat: Function, onReload: Function
 * }} handlers
 * @returns {{el: HTMLElement, update: Function, destroy: Function, setOpen: Function, focusResult: Function, doGrade: Function}}
 */
export function createGradePanel(handlers) {
  let current = {
    loading: true,
    run: null,
    busy: '',
    error: null,
    elapsed: 0,
    newResult: false,
    revealed: null,
  };
  /** 上一次是否在跑：只在状态翻转时自动开合日志卡。 */
  let lastRunning = null;

  // ---- 卡片骨架：默认展开（结果是工作台的主出口） ----
  const title = el('h2', { class: 'ws-card__title', id: 'ws-grade-title' }, S.GRADE_TITLE);
  const cardAside = el('span', { class: 'ws-card__aside u-faint u-truncate' }, S.GRADE_EMPTY);
  const chevron = el('span', { class: 'ws-card__chevron', 'aria-hidden': 'true' }, '›');
  const summary = el(
    'summary',
    { class: 'ws-card__summary' },
    title,
    el('span', { class: 'u-spacer' }),
    cardAside,
    chevron,
  );
  const bodyHost = el('div', { class: 'ws-card__body' });
  const root = el(
    'details',
    { class: 'ws-card ws-region', id: 'ws-region-grade', open: true },
    summary,
    bodyHost,
  );

  const gradeDesc = el('p', { class: 'u-faint ws-grade-desc' }, S.GRADE_DESC);

  // ---- 动作 ----
  const gradeBtn = createButton({
    label: S.GRADE_RUN,
    variant: 'primary',
    icon: '▶',
    kbd: 'G',
    busyLabel: S.GRADE_RUNNING,
    onClick: () => handlers.onGrade(),
  });
  const promoteBtn = createButton({
    label: S.GRADE_PROMOTE,
    icon: '→',
    onClick: () => handlers.onPromote(),
  });
  const revealBtn = createButton({
    label: S.GRADE_REVEAL,
    variant: 'ghost',
    onClick: () => handlers.onReveal(),
  });
  const reopenBtn = createButton({
    label: S.GRADE_REOPEN,
    variant: 'ghost',
    onClick: () => handlers.onReopen(),
  });
  const exportBtn = createButton({
    label: S.GRADE_EXPORT,
    variant: 'ghost',
    size: 'sm',
    disabled: true,
    reason: S.GRADE_EMPTY,
    onClick: () => handlers.onExport(),
  });
  const actionRow = el('div', { class: 'u-row ws-grade__actions' }, gradeBtn.el, el('span', { class: 'u-spacer' }), revealBtn.el, exportBtn.el);

  // ---- 进度与日志 ----
  const progress = createProgress({ label: S.PROGRESS_IDLE, state: 'idle' });
  const logBox = el('pre', { class: 'ws-log', tabindex: '0', role: 'region' });
  logBox.setAttribute('aria-label', S.GRADE_LOG_TITLE);
  const logCount = el('span', { class: 'u-faint' });
  const logCard = createDetailsCard({ title: S.GRADE_LOG_TITLE, content: logBox, open: false });

  // ---- 结果 ----
  const resultHost = el('div', { class: 'u-stack', tabindex: '-1' });
  resultHost.setAttribute('role', 'region');
  resultHost.setAttribute('aria-label', S.GRADE_RESULT_TITLE);
  const groupList = el('ul', { class: 'grade__groups' });
  const newFlag = el('span', { class: 'grade__new-flag' }, `● ${S.GRADE_NEW_RESULT}`);

  const emptyState = createEmptyState({
    icon: 'chart',
    title: S.GRADE_EMPTY,
    desc: S.GRADE_EMPTY_DESC,
    // 原先标签写「沙箱」而动作跳提示词区，标签和动作对不上；改版后主流程在对话卡，
    // 空态直接引导去内置对话，且用 ghost 不与 [运行校验] 抢强调位。
    actions: [createButton({ label: S.GRADE_EMPTY_ACTION, variant: 'ghost', onClick: () => handlers.onGoChat() }).el],
  });

  /**
   * 组状态 → 语义（图标 + 文字 + 颜色三重编码）。
   * @param {boolean} passed
   * @returns {{cls: string, glyph: string, text: string, kind: string}}
   */
  function groupSemantics(passed) {
    return passed
      ? { cls: 'group-card group-card--pass', glyph: '✓', text: S.GRADE_GROUP_PASS, kind: 'ok' }
      : { cls: 'group-card group-card--fail', glyph: '✕', text: S.GRADE_GROUP_FAIL, kind: 'error' };
  }

  /**
   * 渲染一个分组（按行，不套框——分组是列表行不是容器卡）。
   * @param {object} group 报告里的一个 group
   * @returns {{el: HTMLElement, update: Function}}
   */
  function renderGroup(group) {
    const node = el('li', { class: 'group-card' });

    function build(g) {
      // 组 id 不变时 patchList 会复用本节点，靠 build 重建内容；
      // 重建前后按 summary 文本恢复 details 的展开状态。
      const wasOpen = new Map();
      node.querySelectorAll('details').forEach((d) => {
        if (d.open) wasOpen.set((d.querySelector('summary') || {}).textContent || '', true);
      });
      const sem = groupSemantics(g.passed);
      const failed = (g.cases || []).filter((c) => c.outcome !== 'passed');
      const list = el('div', { class: 'group-card__body' });
      const head = el(
        'div',
        { class: 'group-card__head' },
        el('span', { class: 'group-card__glyph', 'aria-hidden': 'true' }, sem.glyph),
        el('span', { class: 'group-card__name' }, g.title || g.id),
        createStatusDot({ kind: sem.kind, text: sem.text }).el,
        el('span', { class: 'group-card__weight' }, t(S.GRADE_GROUP_WEIGHT, { n: g.weight || 0 })),
        el('span', { class: 'u-faint' }, `${g.passed_count || 0} / ${g.total || 0}`),
        failed.length
          ? createBadge({ label: t(S.GRADE_GROUP_FAILURES, { n: failed.length }), variant: 'danger', glyph: '✕' }).el
          : null,
      );

      if (failed.length === 0) {
        list.appendChild(el('p', { class: 'u-faint' }, S.GRADE_GROUP_PASS_SUMMARY));
      } else {
        // 红组可展开失败摘要（§9 验收标准）
        const failList = el('ul', { class: 'fail-list' });
        failed.forEach((c) => {
          const detail = c.detail
            ? createDetailsCard({ title: S.GRADE_CASE_DETAIL, content: c.detail, open: false, flush: true }).el
            : null;
          failList.appendChild(
            el(
              'li',
              { class: 'fail-item' },
              el(
                'div',
                { class: 'fail-item__name u-mono' },
                t(S.GRADE_CASE_NODE, { id: c.node_id || '' }),
                c.duration === undefined
                  ? null
                  : el('span', { class: 'u-faint' }, `　${t(S.GRADE_CASE_TIME, { time: c.duration })}`),
              ),
              el('div', { class: 'fail-item__msg' }, c.message || ''),
              detail,
            ),
          );
        });
        list.appendChild(
          createDetailsCard({
            title: `${S.ACTION_EXPAND}（${t(S.GRADE_GROUP_FAILURES, { n: failed.length })}）`,
            content: failList,
            open: false,
          }).el,
        );
      }
      node.className = sem.cls;
      node.replaceChildren(head, list);
      node.querySelectorAll('details').forEach((d) => {
        if (wasOpen.get((d.querySelector('summary') || {}).textContent || '')) d.open = true;
      });
    }

    build(group);
    return { el: node, update: (g) => build(g) };
  }

  /** 决定这张卡片要不要重画：结论、计数或失败清单变了才算变。 */
  function groupSignature(group) {
    const failed = (group.cases || []).filter((c) => c.outcome !== 'passed');
    return [
      group.passed,
      group.passed_count,
      group.total,
      group.weight,
      failed.map((c) => `${c.node_id}=${c.message || ''}`).join('|'),
    ].join('#');
  }

  /**
   * 组卡片：节点上记一份内容签名。
   * patchList 按组 id 复用节点，重跑校验后同一组会从红转绿；只在建卡那一刻画一次
   * 就会让旧结论永远挂在页面上（分数已更新、卡片还写着失败）。
   */
  const groupSignatures = new WeakMap();

  function renderGroup(group) {
    const node = buildGroupCard(group);
    groupSignatures.set(node, groupSignature(group));
    return node;
  }

  /** 原地震换成新结论：签名没变就不动，保住用户展开的失败清单。 */
  function refreshGroup(node, group) {
    const signature = groupSignature(group);
    if (groupSignatures.get(node) === signature) return;
    groupSignatures.set(node, signature);
    const rebuilt = buildGroupCard(group);
    node.className = rebuilt.className;
    clear(node);
    while (rebuilt.firstChild) node.appendChild(rebuilt.firstChild);
  }

  /**
   * 渲染「越界改动」块；没有越界时给一行绿色结论。
   * @param {object} report
   * @returns {HTMLElement}
   */
  function renderViolations(report) {
    const violations = report.violations || [];
    if (!violations.length) {
      return el(
        'div',
        { class: 'u-row' },
        el('span', { 'aria-hidden': 'true' }, '✓'),
        el('span', { class: 'u-faint' }, S.GRADE_VIOLATIONS_NONE),
      );
    }
    const list = el('ul', { class: 'integrity-list' });
    violations.forEach((v) => {
      list.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, '✕'),
          el(
            'span',
            { class: 'u-mono' },
            t(S.GRADE_VIOLATIONS_ITEM, { path: v.path || '', reason: v.reason || v.change || '' }),
          ),
        ),
      );
    });
    return createDetailsCard({
      title: `${S.GRADE_VIOLATIONS_TITLE}（${violations.length}）`,
      content: list,
      open: true,
    }).el;
  }

  /**
   * 渲染「被破坏的既有用例」块。
   * @param {object} report
   * @returns {HTMLElement}
   */
  function renderRegressions(report) {
    const list = el('ul', { class: 'integrity-list' });
    (report.regressions || []).forEach((r) => {
      list.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, '✕'),
          el('span', { class: 'u-mono' }, t(S.GRADE_REGRESSION_ITEM, { id: r.node_id || '', msg: r.message || '' })),
        ),
      );
    });
    return createDetailsCard({
      title: `${S.GRADE_REGRESSION_TITLE}（${(report.regressions || []).length}）`,
      content: list,
      open: true,
    }).el;
  }

  /**
   * 渲染「评分执行详情」折叠块。
   * @param {object} report
   * @returns {HTMLElement|null}
   */
  function renderChecks(report) {
    const checks = report.checks || [];
    if (!checks.length) return null;
    const list = el('ul', { class: 'integrity-list' });
    checks.forEach((c) => {
      list.appendChild(
        el(
          'li',
          { class: 'integrity-item' },
          el('span', { 'aria-hidden': 'true' }, c.timed_out ? '!' : c.returncode === 0 ? '✓' : '✕'),
          el(
            'span',
            { class: 'u-stack', style: { gap: '2px' } },
            el('span', {}, t(S.GRADE_CHECK_ITEM, {
              kind: c.kind || '',
              code: c.returncode === undefined || c.returncode === null ? '—' : c.returncode,
              time: c.duration_s === undefined ? 0 : c.duration_s,
            })),
            el('span', { class: 'u-faint u-mono' }, c.command || ''),
            c.timed_out ? el('span', { class: 'u-faint' }, S.GRADE_CHECK_TIMEOUT) : null,
            c.summary ? el('span', { class: 'u-faint u-mono' }, c.summary) : null,
            c.log_tail
              ? createDetailsCard({ title: S.ERROR_DETAIL_LABEL, content: c.log_tail, open: false, flush: true }).el
              : null,
          ),
        ),
      );
    });
    return createDetailsCard({ title: S.GRADE_CHECKS_TITLE, content: list, open: false }).el;
  }

  /**
   * 渲染与上一轮的对比。
   * @param {object} report
   * @returns {HTMLElement|null}
   */
  function renderComparison(report) {
    const cmp = report.comparison || {};
    if (!cmp.has_previous) return null;
    const box = el('div', { class: 'u-stack' });
    box.appendChild(
      el(
        'p',
        { class: 'u-faint' },
        t(S.GRADE_PREV_ROUND, { n: cmp.previous_attempt === undefined ? 0 : cmp.previous_attempt, score: cmp.previous_score === undefined ? 0 : cmp.previous_score }),
      ),
    );
    if ((cmp.turned_green || []).length) {
      box.appendChild(el('p', { class: 'u-faint' }, t(S.GRADE_TURNED_GREEN, { list: cmp.turned_green.join('、') })));
    }
    if ((cmp.stayed_red || []).length) {
      box.appendChild(el('p', { class: 'u-faint' }, t(S.GRADE_STAYED_RED, { list: cmp.stayed_red.join('、') })));
    }
    if ((cmp.regressed || []).length) {
      box.appendChild(el('p', { class: 'u-faint' }, t(S.GRADE_REGRESSED, { list: cmp.regressed.join('、') })));
    }
    return createDetailsCard({ title: S.GRADE_COMPARE_TITLE, content: box, open: false, hint: '' }).el;
  }

  /**
   * 渲染参考解（揭晓后）。
   * @returns {HTMLElement|null}
   */
  function renderReveal() {
    if (!current.revealed) return null;
    const refBody = el(
      'div',
      { class: 'u-stack' },
      el('p', {}, S.GRADE_REVEAL_DONE),
      current.revealed.notice ? el('p', { class: 'u-muted' }, t(S.GRADE_REVEAL_NOTICE, { notice: current.revealed.notice })) : null,
      el('pre', { class: 'code-block__pre', tabindex: '0' }, current.revealed.patch || ''),
    );
    return createDetailsCard({
      title: S.GRADE_REVEAL,
      content: refBody,
      open: true,
      hint: S.WS_REVEALED_FLAG,
    }).el;
  }

  /**
   * 渲染结果区：横幅（总分 + 概要 + 进入下一轮）→ 分组明细 → 其余诊断折叠块。
   * @param {object} report
   * @param {{canPromote: boolean, next: number, exhausted: boolean}} promote
   * @param {boolean} running
   * @param {string} busy
   */
  function renderResult(report, promote, running, busy) {
    resultHost.textContent = '';
    const groups = report.groups || [];
    const sum = report.summary || {};
    const green = typeof sum.green === 'number' ? sum.green : groups.filter((g) => g.passed).length;

    // 一句话结论（部分分 / 作废 / 全过）
    const summaryText = report.invalidated
      ? t(S.GRADE_INVALID_REASON, { reason: report.invalid_reason || '' })
      : report.p2p_broken
        ? S.GRADE_DONE_VOID
        : report.passed
          ? S.GRADE_DONE_PASS
          : t(S.GRADE_DONE_PARTIAL, { n: report.score || 0, m: groups.length - green });

    // 结果标记：对勾 / 叉 / 半环，带描边动画（形状 + 文字，颜色不是唯一信号）
    const mark = createResultMark({
      kind: kindForReport(report),
      label: summaryText,
      size: 48,
      animate: true,
      title: `${S.GRADE_SCORE_LABEL}：${percent((report.score || 0) / 100)}`,
    });

    // 结果横幅：通过=绿 tint，未过/作废=红 tint（第一眼必须是它，§一.③）
    const banner = el(
      'div',
      { class: 'ws-grade-banner', dataset: { tone: report.passed ? 'pass' : 'fail' } },
      mark.el,
      el(
        'div',
        { class: 'u-stack ws-grade-banner__score-wrap' },
        el('span', { class: 'u-faint' }, S.GRADE_SCORE_LABEL),
        el('span', { class: 'ws-grade-banner__score' }, percent((report.score || 0) / 100)),
      ),
      el(
        'div',
        { class: 'u-stack ws-grade-banner__summary' },
        el('span', { class: 'u-faint' }, S.GRADE_RESULT_TITLE),
        el('span', {}, t(S.GRADE_GROUP_SUMMARY, { pass: green, total: groups.length })),
      ),
      el('span', { class: 'u-spacer' }),
      current.newResult ? newFlag : null,
    );
    resultHost.appendChild(banner);
    resultHost.appendChild(el('p', { class: 'u-muted ws-grade-banner__note' }, summaryText));

    // 「进入下一轮」只信当前轮实时状态（逐轮校验过 + 还有剩余机会），不信旧报告里的 next_hint
    if (promote.canPromote) {
      promoteBtn.update({
        label: t(S.GRADE_PROMOTE, { n: promote.next }),
        disabled: running || Boolean(busy),
      });
      resultHost.appendChild(el('div', { class: 'u-row' }, promoteBtn.el));
    } else if (promote.exhausted) {
      resultHost.appendChild(el('p', { class: 'u-faint ws-grade-exhausted' }, S.GRADE_PROMOTE_EXHAUSTED));
    }

    // 分组明细（key 化复用；refreshGroup 让重跑后从红转绿的组当场改结论，
    // 只建不刷会让分数已更新、卡片还写着失败）
    patchList(groupList, groups, (g) => g.id, (group) => renderGroup(group), refreshGroup);
    resultHost.appendChild(el('div', {}, el('h3', { class: 'section-title' }, S.GRADE_RESULT_TITLE), groupList));

    // 回归
    const p2pOk = !report.p2p_broken;
    resultHost.appendChild(
      el(
        'div',
        { class: 'u-row' },
        el('span', { class: 'u-faint' }, S.GRADE_P2P_TITLE),
        createStatusDot({ kind: p2pOk ? 'ok' : 'error', text: p2pOk ? S.GRADE_P2P_OK_SHORT : S.GRADE_P2P_BROKEN_SHORT }).el,
      ),
    );
    if (!p2pOk) resultHost.appendChild(renderRegressions(report));

    // 越界改动
    resultHost.appendChild(renderViolations(report));

    // 噪声（不判越界，但值得让人看见）
    const noise = report.noise || [];
    if (noise.length) {
      const list = el('ul', { class: 'integrity-list' });
      noise.forEach((n) => {
        const path = typeof n === 'string' ? n : n.path || '';
        list.appendChild(el('li', { class: 'integrity-item' }, el('span', { 'aria-hidden': 'true' }, '·'), el('span', { class: 'u-mono' }, t(S.GRADE_NOISE_ITEM, { path }))));
      });
      resultHost.appendChild(createDetailsCard({ title: `${S.GRADE_NOISE_TITLE}（${noise.length}）`, content: list, open: false }).el);
    }

    // 相似度标记
    const sim = report.similarity || {};
    if (sim.checked) {
      const high = Boolean(sim.flagged);
      resultHost.appendChild(
        el(
          'div',
          { class: 'u-row' },
          createStatusDot({
            kind: high ? 'error' : 'ok',
            text: high
              ? t(S.GRADE_SIMILARITY_HIGH, { n: sim.max_ratio || 0 })
              : t(S.GRADE_SIMILARITY, { n: sim.max_ratio || 0 }),
          }).el,
        ),
      );
    }

    // diff 统计
    const diff = report.diff || {};
    if (diff.files) {
      const diffBox = el(
        'div',
        { class: 'u-stack' },
        el('p', { class: 'u-faint' }, t(S.GRADE_DIFF_LINE, {
          changed: diff.changed_lines || 0,
          add: diff.added_lines || 0,
          del: diff.removed_lines || 0,
          cap: diff.line_cap || 0,
        })),
        diff.over_cap ? el('p', { class: 'u-faint' }, S.GRADE_DIFF_OVER_CAP) : null,
      );
      resultHost.appendChild(createDetailsCard({ title: S.GRADE_DIFF_TITLE, content: diffBox, open: false }).el);
    }

    // 与上一轮对比
    const cmp = renderComparison(report);
    if (cmp) resultHost.appendChild(cmp);

    // 评分执行详情
    const checks = renderChecks(report);
    if (checks) resultHost.appendChild(checks);

    // 校验过程本身出错
    if (report.error) {
      resultHost.appendChild(
        el(
          'div',
          { class: 'callout callout--error', role: 'note' },
          el('strong', {}, S.GRADE_ERROR_TITLE),
          el('p', {}, t(S.GRADE_ERROR_BODY, { msg: report.error })),
        ),
      );
    }

    // 下一步建议（服务端 hint 作补充说明；主按钮已在横幅里）
    const hint = report.next_hint || {};
    if (hint.label) {
      resultHost.appendChild(
        el(
          'div',
          { class: 'callout' },
          el('strong', {}, t(S.GRADE_NEXT_HINT, { label: hint.label })),
          hint.reason ? el('p', { class: 'u-muted' }, t(S.GRADE_NEXT_REASON, { reason: hint.reason })) : null,
        ),
      );
    }

    // 参考解
    const ref = renderReveal();
    if (ref) resultHost.appendChild(ref);
  }

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
   * 差异更新。
   * @param {object} state
   */
  function update(state) {
    current = { ...current, ...state };
    bodyHost.textContent = '';

    if (current.loading) {
      setText(cardAside, '');
      bodyHost.appendChild(createStatusDot({ kind: 'busy', text: S.STATE_LOADING }).el);
      return;
    }
    if (current.error) {
      root.open = true; // 错误不能藏在收起的卡里
      setText(cardAside, S.ERR_LOAD);
      bodyHost.appendChild(
        createEmptyState({
          title: S.ERR_LOAD,
          desc: S.ERR_LOAD_BODY,
          alert: true,
          // 重试 = 只读回读。挂 onGrade 会变成「读取失败 → 点重试」直接提交一次校验。
          actions: [createButton({ label: S.ACTION_RETRY, onClick: () => handlers.onReload() }).el],
        }).el,
      );
      return;
    }

    const run = current.run;
    const report = run ? run.report : null;
    const hasRun = Boolean(run);
    const busy = current.busy || '';
    // 校验是异步的：服务端 status=grading 才是真在跑
    const running = busy === 'grade' || (hasRun && run.status === 'grading');
    const sandboxOk = hasRun && SANDBOX_OK.has(run.status);
    const hasReport = Boolean(report);

    // 卡头摘要行：跑完显示分数，进行中显示状态；空态留空——空态说明就在卡里，
    // 卡头再重复一遍只会让折叠标题变吵。
    setText(
      cardAside,
      running
        ? S.GRADE_RUNNING
        : hasReport
          ? percent((report.score || 0) / 100)
          : '',
    );

    // 动作区
    // 运行校验的可用性：必须沙箱就绪、服务端没有还在跑的对话线程，
    // 而且模型真的动过手——刚建好沙箱就点校验，只会按「未改动」判 0，白烧一次机会。
    const chatBusy = Boolean(run && run.chat_busy);
    const modelActed = !run || run.model_acted !== false;
    actionRow.textContent = '';
    actionRow.appendChild(gradeBtn.el);
    // 「进入下一轮」按当前轮次的实时状态判断，不信旧报告里的 next_hint：
    // 必须当前轮已经评分（逐轮校验）且还有剩余机会，按钮才会出现——
    // 否则进入第 2 轮后，旧报告会把按钮重新标成「进入第 3 轮」
    const currentAttempt = Number(run && run.attempt) || 1;
    const attemptsAllowed = Number(run && run.attempts_allowed) || currentAttempt;
    const gradedAttempts = ((run && run.rounds) || []).map((r) => Number(r.attempt));
    const canPromote = hasReport && gradedAttempts.includes(currentAttempt) && currentAttempt < attemptsAllowed;
    if (canPromote) {
      promoteBtn.update({
        label: t(S.GRADE_PROMOTE, { n: currentAttempt + 1 }),
        disabled: running || Boolean(busy),
      });
      actionRow.appendChild(promoteBtn.el);
    }
    if (hasReport && !run.revealed && !current.revealed) {
      revealBtn.update({ label: S.GRADE_REVEAL, disabled: running });
      actionRow.appendChild(revealBtn.el);
    }
    // 误校验的补救口：本轮分数作废、退回可对话状态，模型改完再重新校验。
    // 已揭晓参考解的轮次不给这个口（后端同样拒绝）。
    if (hasReport && run && run.status === 'graded' && !run.revealed) {
      reopenBtn.update({ disabled: running || Boolean(busy) || chatBusy });
      actionRow.appendChild(reopenBtn.el);
    }
    if (hasReport) {
      exportBtn.update({ disabled: false });
      actionRow.appendChild(exportBtn.el);
    } else {
      exportBtn.update({ disabled: true, reason: S.GRADE_EMPTY });
    }

    gradeBtn.update({
      label: hasReport ? S.GRADE_RERUN : S.GRADE_RUN,
      loading: running,
      busyLabel: S.GRADE_RUNNING,
      disabled: !sandboxOk || running || Boolean(busy) || chatBusy || !modelActed,
      reason: !hasRun
        ? S.ERR_NO_SANDBOX
        : running
          ? S.GRADE_RUNNING
          : chatBusy
            ? S.CHAT_REMOTE_BUSY
            : !modelActed
              ? S.GRADE_NEED_MODEL_FIRST
              : !sandboxOk
                ? S.SANDBOX_PREPARING
                : '',
    });
    revealBtn.update({ disabled: running || Boolean(busy) || !hasReport || Boolean(run && run.revealed) || Boolean(current.revealed) });
    exportBtn.update({ disabled: !hasReport, reason: hasReport ? '' : S.GRADE_EMPTY });

    bodyHost.appendChild(gradeDesc);
    bodyHost.appendChild(actionRow);

    if (!hasRun) {
      emptyState.update({});
      bodyHost.appendChild(emptyState.el);
      return;
    }

    // 进度
    if (running) {
      progress.update({
        state: 'running',
        determinate: false,
        label: S.PROGRESS_GRADE,
        elapsed: Math.floor((current.elapsed || 0) / 1000),
        total: null,
      });
      bodyHost.appendChild(progress.el);
    } else {
      progress.update({ state: 'idle', label: S.PROGRESS_IDLE });
    }

    if (hasReport) {
      renderResult(
        report,
        { canPromote, next: currentAttempt + 1, exhausted: !canPromote && currentAttempt >= attemptsAllowed },
        running,
        busy,
      );
      bodyHost.appendChild(resultHost);
    } else if (!running) {
      emptyState.update({});
      bodyHost.appendChild(emptyState.el);
    }

    // 日志：契约里 run.log 是校验日志（status ∈ grading/graded/error 时才有）
    renderLog(running, (run && run.log) || []);
    bodyHost.appendChild(logCard.el);
  }

  update({});

  return {
    el: root,
    update,
    /** 展开/收起卡片（校验完成的结果提醒会先把卡展开）。 */
    setOpen(open) {
      root.open = Boolean(open);
    },
    /** 结果区拿到焦点（结果提醒用；区域本身 tabindex="-1"，不进 Tab 序）。 */
    focusResult() {
      resultHost.focus({ preventScroll: true });
    },
    /** 供快捷键 G 触发。 */
    doGrade: () => handlers.onGrade(),
    /** 解绑（§10.4）。 */
    destroy() {
      [gradeBtn, promoteBtn, revealBtn, reopenBtn, exportBtn].forEach((b) => b.destroy());
      progress.destroy();
      logCard.destroy();
      emptyState.destroy();
    },
  };
}
