/**
 * report-modal.js — 「校验报告」独立窗口（2026-10-02 工作台二次优化）
 *
 * 完整校验报告从对话流挪进这个窗口（components/modal.js：role=dialog / aria-modal /
 * 焦点圈禁 / Esc 与 × 关闭 / 关闭后焦点还原）：
 *   红绿横幅 → 分组明细（绿组一句话，红组可展开失败摘要）→ 回归 / 越界 / 噪声 /
 *   相似度 / diff 统计 → 与上一轮对比 → 评分执行详情 → 校验日志 → 参考解（已揭晓时）
 *   → 「下一步」引导。
 * footer 常驻与校验结果相关的出口：继续对话（本轮分数作废）/ 查看参考解 /
 * 导出报告 JSON；禁用态与原因由编排层从 ⋯ 菜单的同一套判定里取（出口不许条件隐藏，
 * 点不动时必须说得出为什么）。
 *
 * 对话流里只留一行轻量结果条（report-node.js），点「查看完整报告」reopen 本窗口——
 * 发生过校验这个事实与回看入口都不丢。校验完成只给 polite 播报 + 结果条标记，
 * 不自动弹本窗口（§13.2）；唯一的主动打开路径是用户点「查看完整报告」，
 * 以及用户主动「查看参考解」成功后把补丁正文直接呈上来（那是这次点击要的东西）。
 *
 * 窗口内容是打开时刻的快照：内容不自动替换（用户正在读的半句话不该被抽走）。
 * 但窗口开着期间这一轮可能又跑出一次校验——那时窗口顶部出现一条 polite 的
 * 「有新结果，点击刷新」（role=status，不抢焦点），点一下才替换正文。
 * 揭晓参考解成功后由编排层重开一版带补丁的窗口。
 *
 * 样式：modal 挂在 document.body 上、不在 .ws 子树里，所以报告内容的样式单独挂在
 * .ws-report-modal 根类下（workspace.css 末尾一节），与「选择器一律挂 .ws」是同一个
 * 不外泄意图。
 *
 * 契约要点（消费的 run.report 结构对照 harness/grade.py + harness/report.py）：
 *   score / passed / invalidated / invalid_reason / p2p_broken / error
 *   groups[]{id, title, weight, passed, cases[], total, passed_count}
 *   regressions[] / violations[] / noise[] / similarity{} / diff{} / comparison{} /
 *   next_hint{} / summary{groups[], green, red} / checks[]；run.log 是校验日志。
 *
 * 依赖：core/*、components/*
 * 导出：openReportModal(options) → modal handle（见 components/modal.js）
 */

import { el, setText, patchList } from '../../core/dom.js';
import { S, t } from '../../core/strings.js';
import { openModal } from '../../components/modal.js';
import { createButton } from '../../components/button.js';
import { createDetailsCard } from '../../components/details-card.js';
import { createStatusDot } from '../../components/status-dot.js';
import { createBadge } from '../../components/badge.js';
import { createResultMark, kindForReport } from '../../components/result-mark.js';
import { percent } from '../../core/format.js';

/** 本窗口新增文案（strings.js 冻结，新增一律走本地常量）。 */
const T = {
  MODAL_TITLE: '校验报告 · 第 {n} 轮',
  MODAL_TITLE_NO_ROUND: '校验报告',
  NEW_RESULT: '有新结果，点击刷新',
  // 「下一步」引导：主按钮仍在底部操作栏，这里只指路（与历史内联版同一套话）
  NEXT_TO_PROMOTE: '下一步：底部操作栏点「进入第 {n} 轮」，继续解下一级提示。',
  NEXT_TO_FIX: '下一步：回到对话里把失败组告诉模型让它接着改，改完点底部操作栏的「重新校验」。',
  NEXT_TO_REVEAL: '机会已用完。下一步：底部操作栏点「查看参考解」对照锚解（看过之后成绩永不进台账）。',
  NEXT_TO_GRADE: '下一步：模型改完后，点底部操作栏的「运行校验」。',
};

/** 新结果提示的轮询间隔（毫秒）。窗口开着时才在跑。 */
const NEW_RESULT_POLL_MS = 2500;

/**
 * 报告指纹：只看「这一次校验产出了什么」，不看时间戳。
 * 重新校验会整体换一份 report.json，用它比对就能分辨「换了新报告」与「同一份」。
 * @param {object|null} report
 * @returns {string}
 */
function reportFingerprint(report) {
  if (!report) return '';
  const rounds = Array.isArray(report.rounds) ? report.rounds.length : 0;
  return [
    report.generated_at || '',
    report.score,
    report.passed ? 1 : 0,
    report.invalidated ? 1 : 0,
    (report.groups || []).length,
    rounds,
  ].join('|');
}

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
  }

  build(group);
  return { el: node, update: build };
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
 * 渲染参考解（揭晓后；补丁正文只在揭晓当次下发，刷新后此处不再出现——与历史内联版一致）。
 * @param {object|null} revealed {patch, notice}
 * @returns {HTMLElement|null}
 */
function renderReveal(revealed) {
  if (!revealed) return null;
  const refBody = el(
    'div',
    { class: 'u-stack' },
    el('p', {}, S.GRADE_REVEAL_DONE),
    revealed.notice ? el('p', { class: 'u-muted' }, t(S.GRADE_REVEAL_NOTICE, { notice: revealed.notice })) : null,
    el('pre', { class: 'code-block__pre', tabindex: '0' }, revealed.patch || ''),
  );
  return createDetailsCard({
    title: S.GRADE_REVEAL,
    content: refBody,
    open: true,
    hint: S.WS_REVEALED_FLAG,
  }).el;
}

/**
 * 渲染校验日志（快照，默认收起；运行中的实时日志在对话流结果条下方的进度区）。
 * @param {string[]} lines
 * @returns {HTMLElement}
 */
function renderLog(lines) {
  const logBox = el('pre', { class: 'ws-log', tabindex: '0', role: 'region' });
  logBox.setAttribute('aria-label', S.GRADE_LOG_TITLE);
  setText(logBox, lines.length ? lines.join('\n') : S.GRADE_LOG_EMPTY);
  return createDetailsCard({
    title: S.GRADE_LOG_TITLE,
    content: logBox,
    open: false,
    hint: lines.length ? t(S.LOG_LINES_COUNT, { n: lines.length }) : '',
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
 * 拼装窗口正文：横幅 → 分组明细 → 诊断折叠块 → 日志 → 参考解 → 下一步引导。
 * @param {{report: object, run: object, revealed: object|null, newResult: boolean}} args
 * @returns {HTMLElement}
 */
function buildReportBody({ report, run, revealed, newResult }) {
  const body = el('div', { class: 'ws-report-modal u-stack' });
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
  body.appendChild(el(
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
    newResult ? el('span', { class: 'grade__new-flag' }, `● ${S.GRADE_NEW_RESULT}`) : null,
  ));
  body.appendChild(el('p', { class: 'u-muted ws-grade-banner__note' }, summaryText));

  const attempt = Number(run && run.attempt) || 1;
  const attemptsAllowed = Number(run && run.attempts_allowed) || attempt;
  if (attempt >= attemptsAllowed && !report.invalidated) {
    body.appendChild(el('p', { class: 'u-faint ws-grade-exhausted' }, S.GRADE_PROMOTE_EXHAUSTED));
  }

  // 分组明细
  const groupList = el('ul', { class: 'grade__groups' });
  patchList(groupList, groups, (g) => g.id, (group) => renderGroup(group));
  body.appendChild(el('div', {}, el('h3', { class: 'section-title' }, S.GRADE_RESULT_TITLE), groupList));

  // 回归
  const p2pOk = !report.p2p_broken;
  body.appendChild(
    el(
      'div',
      { class: 'u-row' },
      el('span', { class: 'u-faint' }, S.GRADE_P2P_TITLE),
      createStatusDot({ kind: p2pOk ? 'ok' : 'error', text: p2pOk ? S.GRADE_P2P_OK_SHORT : S.GRADE_P2P_BROKEN_SHORT }).el,
    ),
  );
  if (!p2pOk) body.appendChild(renderRegressions(report));

  // 越界改动
  body.appendChild(renderViolations(report));

  // 噪声（不判越界，但值得让人看见）
  const noise = report.noise || [];
  if (noise.length) {
    const list = el('ul', { class: 'integrity-list' });
    noise.forEach((n) => {
      const path = typeof n === 'string' ? n : n.path || '';
      list.appendChild(el('li', { class: 'integrity-item' }, el('span', { 'aria-hidden': 'true' }, '·'), el('span', { class: 'u-mono' }, t(S.GRADE_NOISE_ITEM, { path }))));
    });
    body.appendChild(createDetailsCard({ title: `${S.GRADE_NOISE_TITLE}（${noise.length}）`, content: list, open: false }).el);
  }

  // 相似度标记
  const sim = report.similarity || {};
  if (sim.checked) {
    const high = Boolean(sim.flagged);
    body.appendChild(
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
    body.appendChild(createDetailsCard({ title: S.GRADE_DIFF_TITLE, content: diffBox, open: false }).el);
  }

  // 与上一轮对比
  const cmp = renderComparison(report);
  if (cmp) body.appendChild(cmp);

  // 评分执行详情
  const checks = renderChecks(report);
  if (checks) body.appendChild(checks);

  // 校验过程本身出错
  if (report.error) {
    body.appendChild(
      el(
        'div',
        { class: 'callout callout--error', role: 'note' },
        el('strong', {}, S.GRADE_ERROR_TITLE),
        el('p', {}, t(S.GRADE_ERROR_BODY, { msg: report.error })),
      ),
    );
  }

  // 校验日志（快照）
  body.appendChild(renderLog((run && run.log) || []));

  // 下一步建议（服务端 hint 作补充说明；引导文案在窗口末尾）
  const hint = report.next_hint || {};
  if (hint.label) {
    body.appendChild(
      el(
        'div',
        { class: 'callout' },
        el('strong', {}, t(S.GRADE_NEXT_HINT, { label: hint.label })),
        hint.reason ? el('p', { class: 'u-muted' }, t(S.GRADE_NEXT_REASON, { reason: hint.reason })) : null,
      ),
    );
  }

  // 参考解
  const ref = renderReveal(revealed);
  if (ref) body.appendChild(ref);

  // 「下一步」引导（窗口末尾；动作在底部操作栏与本窗口 footer）
  body.appendChild(renderGuidance(report, run));

  return body;
}

/**
 * 打开「校验报告」窗口。
 *
 * @param {{
 *   run: object,
 *   revealed?: object|null,
 *   newResult?: boolean,
 *   reloadRun?: () => Promise<object|null>,
 *   actions?: Array<{key?: string, label: string, disabled?: boolean, reason?: string,
 *     variant?: string, danger?: boolean, keepOpen?: boolean, onClick?: Function}>
 * }} options
 *   actions：footer 出口按钮。keepOpen=true 的动作（导出）点完不关窗；其余动作
 *   （作废 / 揭晓）自带二次确认，确认框会把本窗口顶掉（modal.js 同屏最多一个），
 *   所以不在此处先关。
 *   reloadRun：重新读一次运行记录。传了就开「有新结果」轮询（见 watchNewResult），
 *   不传就完全没有窗内轮询（旧行为）。
 * @returns {object|null} modal handle；没有报告可展示时返回 null
 */
export function openReportModal(options = {}) {
  const run = options.run || null;
  const report = run && run.report;
  if (!run || !report) return null;
  const attempt = Number(run.attempt) || 0;

  /** 本窗口内建组件的销毁句柄（modal 关窗即整树移除，只需解绑按钮事件）。 */
  const footerButtons = [];
  /** modal 句柄在下面的 onClick 闭包里才用得到，先声明后赋值。 */
  let modal = null;

  const footer = (Array.isArray(options.actions) ? options.actions : []).map((action) => {
    const btn = createButton({
      label: action.label || '',
      variant: action.variant || 'default',
      size: 'sm',
      disabled: Boolean(action.disabled),
      reason: action.reason || '',
      onClick: () => {
        if (action.keepOpen) {
          if (typeof action.onClick === 'function') action.onClick();
          return;
        }
        // 不 keepOpen 的出口：先关报告窗再进动作，确认框/新窗口都不会压着报告窗
        if (modal) modal.close('action');
        if (typeof action.onClick === 'function') action.onClick();
      },
    });
    footerButtons.push(btn);
    return btn.el;
  });

  const body = buildReportBody({
    report,
    run,
    revealed: options.revealed || null,
    newResult: Boolean(options.newResult),
  });

  /**
   * 新结果提示条（2026-10-02，原问题 6）。
   *
   * 窗口是打开时刻的快照，但开窗期间这一轮可能又跑完一次校验。正文绝不自动替换
   * ——用户正在读的半句话不该被抽走——只在顶部挂一条 polite 提示，点一下才刷新。
   * role=status 让读屏在末尾播报，不抢焦点（没有 tabindex，不 autoFocus）。
   *
   * @param {object} host 报告正文根节点
   * @param {string} initialFingerprint 开窗时那份报告的指纹
   */
  function watchNewResult(host, initialFingerprint) {
    const runId = String((run && run.run_id) || '');
    if (!runId || typeof options.reloadRun !== 'function') return;

    let latest = initialFingerprint;
    let banner = null;
    let pending = false;
    let stopped = false;
    let timer = 0;

    function stop() {
      if (timer) {
        window.clearTimeout(timer);
        timer = 0;
      }
    }

    function showBanner() {
      if (banner) return;
      const btn = createButton({
        label: T.NEW_RESULT,
        variant: 'ghost',
        size: 'sm',
        onClick: async () => {
          const fresh = await options.reloadRun();
          if (!fresh || !fresh.report) return;
          latest = reportFingerprint(fresh.report);
          if (banner) banner.remove();
          banner = null;
          host.replaceChildren(buildReportBody({
            report: fresh.report,
            run: fresh,
            revealed: options.revealed || null,
            newResult: true,
          }));
        },
      });
      footerButtons.push(btn);
      banner = el(
        'div',
        { class: 'ws-report-modal__new-result', role: 'status' },
        btn.el,
        el('span', { class: 'u-faint' }, S.GRADE_NEW_RESULT_HINT),
      );
      host.insertBefore(banner, host.firstChild);
    }

    async function tick() {
      timer = 0;
      if (pending) return;
      pending = true;
      try {
        const fresh = await options.reloadRun();
        if (fresh && fresh.report && reportFingerprint(fresh.report) !== latest) showBanner();
      } catch {
        /* 读失败就下一轮再试：报告窗是快照，不该因为一次轮询失败报错 */
      } finally {
        pending = false;
      }
      if (!stopped) timer = window.setTimeout(tick, NEW_RESULT_POLL_MS);
    }

    timer = window.setTimeout(tick, NEW_RESULT_POLL_MS);
    return () => {
      stopped = true;
      stop();
    };
  }

  const stopWatch = watchNewResult(body, reportFingerprint(report));

  modal = openModal({
    title: attempt > 0 ? t(T.MODAL_TITLE, { n: attempt }) : T.MODAL_TITLE_NO_ROUND,
    body,
    footer,
    variant: 'wide',
    onClose: () => {
      stopWatch();
      footerButtons.forEach((btn) => btn.destroy());
    },
  });
  return modal;
}
