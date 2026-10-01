/**
 * report-node.js — 对话流里的校验结果节点（2026-10-02 对话流改版；由旧 grade-panel 迁移而来）
 *
 * 校验报告是对话流里的一个结果节点，内联展示、不做弹窗：
 *   - 进行中：进度 + 已用时间 + 实时日志（运行中默认展开，结束自动收起）；
 *   - 完成：结果横幅（总分 + 红绿概要，通过=绿 tint / 未过=红 tint）→ 分组明细
 *     （绿组一句话，红组可展开失败摘要）→ 回归 / 越界 / 噪声 / 相似度 / diff /
 *     与上一轮对比 / 评分执行详情 / 参考解 → 「下一步」引导一句话；
 *   - 动作不在本节点：每个时刻的唯一主按钮在底部操作栏（dock.js），本节点末尾的
 *     引导文案告诉读者它叫什么。
 *
 * 状态：hidden（没跑过也不在跑）/ running / done / error。
 *
 * 契约要点（消费的 run.report 结构对照 harness/grade.py + harness/report.py）：
 *   score / passed / invalidated / invalid_reason / p2p_broken / error
 *   groups[]{id, title, weight, passed, cases[], total, passed_count}
 *   regressions[] / violations[] / noise[] / similarity{} / diff{} / comparison{} /
 *   next_hint{} / summary{groups[], green, red} / checks[]
 *
 * 依赖：core/*、components/*
 * 导出：createReportNode() → { el, update, destroy, focusResult }
 */

import { el, setText, patchList } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { createProgress } from '../../components/progress.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createBadge } from '../../components/badge.js';
import { createResultMark, kindForReport } from '../../components/result-mark.js';
import { percent } from '../../core/format.js';

/** 改版新增文案（strings.js 冻结，新增走本地常量）。 */
const T = {
  RUNNING_TITLE: '校验进行中',
  // 「下一步」引导：动作本身在底部操作栏（每时刻一个主按钮），这里只指路
  NEXT_TO_PROMOTE: '下一步：底部操作栏点「进入第 {n} 轮」，继续解下一级提示。',
  NEXT_TO_FIX: '下一步：在下方对话里把失败组告诉模型让它接着改，改完点底部操作栏的「重新校验」。',
  NEXT_TO_REVEAL: '机会已用完。下一步：底部操作栏点「查看参考解」对照锚解（看过之后成绩不进排行榜）。',
  NEXT_TO_GRADE: '下一步：模型改完后，点底部操作栏的「运行校验」。',
  RESULT_ANCHOR: '校验结果',
};

/**
 * 创建校验结果节点。
 * @returns {{el: HTMLElement, update: Function, destroy: Function, focusResult: Function}}
 */
export function createReportNode() {
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

  const title = el('span', { class: 'ws-node__title' }, T.RESULT_ANCHOR);
  const aside = el('span', { class: 'ws-node__aside u-faint u-truncate' });
  const bodyHost = el('div', { class: 'ws-node__body' });
  const root = el(
    'section',
    { class: 'ws-node ws-report ws-region', id: 'ws-region-grade', 'aria-label': S.GRADE_TITLE, hidden: true },
    el('div', { class: 'ws-report__head' }, title, el('span', { class: 'u-spacer' }), aside),
    bodyHost,
  );

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

  /** 决定这一行要不要重画：结论、计数或失败清单变了才算变。 */
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

    let signature = groupSignature(group);
    build(group);
    return {
      el: node,
      // 每次轮询都会调到这里：结论没变就不重建，否则用户刚展开的失败清单会被
      // 轮询吞掉、焦点也会跟着丢。
      update: (g) => {
        const next = groupSignature(g);
        if (next === signature) return;
        signature = next;
        build(g);
      },
    };
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
   * 「下一步」引导一句话：动作在底部操作栏（每时刻一个主按钮），这里只指路。
   * @param {object} report
   * @param {object} run
   * @returns {HTMLElement}
   */
  function renderGuidance(report, run) {
    const attempt = Number(run && run.attempt) || 1;
    const allowed = Number(run && run.attempts_allowed) || attempt;
    const gradedAttempts = ((run && run.rounds) || []).map((r) => Number(r.attempt));
    const canPromote = gradedAttempts.includes(attempt) && attempt < allowed;
    let text;
    if (report.invalidated || report.error) {
      text = T.NEXT_TO_FIX;
    } else if (report.passed && canPromote) {
      text = t(T.NEXT_TO_PROMOTE, { n: attempt + 1 });
    } else if (report.passed && attempt >= allowed) {
      text = T.NEXT_TO_REVEAL;
    } else if (report.passed) {
      text = t(T.NEXT_TO_PROMOTE, { n: attempt + 1 });
    } else {
      text = T.NEXT_TO_FIX;
    }
    return el('p', { class: 'ws-report__guidance', role: 'note' }, text);
  }

  /**
   * 渲染结果区：横幅（总分 + 概要）→ 分组明细 → 其余诊断折叠块 → 下一步引导。
   * @param {object} report
   * @param {object} run
   */
  function renderResult(report, run) {
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

    // 结果横幅：通过=绿 tint，未过/作废=红 tint（第一眼必须是它）
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

    const attempt = Number(run && run.attempt) || 1;
    const attemptsAllowed = Number(run && run.attempts_allowed) || attempt;
    if (attempt >= attemptsAllowed && !report.invalidated) {
      resultHost.appendChild(el('p', { class: 'u-faint ws-grade-exhausted' }, S.GRADE_PROMOTE_EXHAUSTED));
    }

    // 分组明细按组 id 复用节点。renderGroup 交回的是 {el, update}，patchList 会优先走
    // api.update，重跑校验后从红转绿的组当场改结论。
    patchList(groupList, groups, (g) => g.id, (group) => renderGroup(group));
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

    // 下一步建议（服务端 hint 作补充说明；引导文案在本节点末尾）
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

    // 「下一步」引导（内联在对话流里，不做弹窗、不做 sticky）
    resultHost.appendChild(renderGuidance(report, run));
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

    // 节点头：跑完显示分数，进行中显示状态
    setText(
      aside,
      running
        ? S.GRADE_RUNNING
        : report
          ? percent((report.score || 0) / 100)
          : '',
    );

    if (running) {
      bodyHost.appendChild(
        el('div', { class: 'u-row' },
          createStatusDot({ kind: 'busy', text: T.RUNNING_TITLE }).el,
          el('span', { class: 'u-faint' }, S.GRADE_RUNNING)),
      );
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

    if (report) {
      renderResult(report, run);
      bodyHost.appendChild(resultHost);
    }

    // 日志：契约里 run.log 是校验日志（status ∈ grading/graded/error 时才有）
    renderLog(running, (run && run.log) || []);
    bodyHost.appendChild(logCard.el);
  }

  update({});

  return {
    el: root,
    update,
    /** 结果区拿到焦点（结果提醒用；区域本身 tabindex="-1"，不进 Tab 序）。 */
    focusResult() {
      root.hidden = false;
      resultHost.focus({ preventScroll: true });
    },
    /** 解绑（§10.4）。 */
    destroy() {
      progress.destroy();
      logCard.destroy();
    },
  };
}
