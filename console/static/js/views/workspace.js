/**
 * workspace.js — 工作台编排层（2026-10-02 对话流改版）
 *
 * 目标形态（对话流为主轴）：
 *   [状态栏]   一条细的常驻栏：任务编号+标题 · 档位徽标 · 第 n 轮/共 m 次 ·
 *              模型档案下拉 · 运行状态一句话。路径/哈希/运行编号一律不进状态栏。
 *   [改动正文] 菜单「查看改动」弹出的内联区（按需拉取，不轮询）。
 *   [对话流]   页面主轴：任务与提示词节点 → 用户/模型消息 → 工具调用紧凑行 →
 *              校验结果条（轻量一行，完整报告在「校验报告」独立窗口）→
 *              运行详情 / 本轮备注（折叠节点）。
 *   [底部]     操作栏（每时刻一个主按钮 + 显式「结束本轮并回收沙箱」 + ⋯ 更多操作）
 *              + 对话输入区。
 *
 * 职责（编排，不写展示细节）：
 *   1. 全部后端动作：准备 / 清空 / 重建 / 校验 / 下一轮 / 揭晓 / 作废 / 回收 / 废弃 /
 *      导出 / 备注 / 看改动。
 *   2. 状态机 → dock：每个时刻算出一个主按钮与全量次级菜单（菜单项常列，不可用的
 *      禁用并把原因写在项里——出口不许条件隐藏）。
 *   3. 轮询只 patch 变化的区域；页面隐藏自动暂停（core/poller）。
 *   4. 快捷键 C / G / R / 1 / 2 / 3（? 与 Esc 属于全局，在 main.js）。
 *   5. 状态持久化：上次查看轮次、每任务×档案的 run_id、任务节点开合与滚动位置。
 *
 * 生命周期（§10.4）：createWorkspace(props) → { el, destroy, focusRegion, ... }
 *
 * ── 契约要点（对照 console/server.py 与 harness/runs.py）────────────────
 *   POST /api/runs                     同步，阻塞到沙箱铺好为止 → 用 api.longPost
 *   GET  /api/runs/{id}                run_view：sandbox/baseline_digest 都是字符串
 *   POST /api/runs/{id}/grade          异步，立刻返回 {status:'grading'} → 靠轮询看结果
 *   POST /api/runs/{id}/promote        {run_id, attempt, can_promote}
 *   POST /api/runs/{id}/reveal         {run_id, patch, notice}
 *   POST /api/runs/{id}/note           请求体 {note}（单数）
 *   POST /api/runs/{id}/diff           {diff: 补丁正文}
 *   POST /api/sandbox/reset|rebuild    同步
 *   run.status ∈ preparing|ready|grading|graded|error|queued
 * ──────────────────────────────────────────────────────────────────────
 */

import { el, setText, on } from '../core/dom.js';
import { S, t } from '../core/strings.js';
import { api, ApiError, errorTitle, errorBody } from '../core/api.js';
import { createPoller } from '../core/poller.js';
import { createStore } from '../core/store.js';
import { storage } from '../core/storage.js';
import {
  announce,
  isEditableTarget,
  scrollBelowStickyHeader,
} from '../core/a11y.js';
import { tierBadge } from '../components/badge.js';
import { confirmDialog } from '../components/confirm-dialog.js';
import { showToast } from '../components/toast.js';
import { createButton } from '../components/button.js';
import { createDetailsCard } from '../components/details-card.js';
import { createField } from '../components/field.js';
import { createStatusDot } from '../components/status-dot.js';
import { createTaskNode } from './workspace/task-node.js';
import { createChatStream } from './workspace/chat-stream.js';
import { createReportNode } from './workspace/report-node.js';
import { openReportModal, openGradeModal } from './workspace/report-modal.js';
import { createRunDetails } from './workspace/run-details.js';
import { createDock } from './workspace/dock.js';

/**
 * 服务端「还在忙」的运行状态：只有这些状态才轮询（§13.6）。
 * 清空 / 重建是同步请求，忙在本地 busy 上，不进这个集合。
 */
const BUSY_STATUS = new Set(['preparing', 'grading', 'queued']);

/** 沙箱可用（模型可以动手 / 可以校验）的服务端状态。 */
const SANDBOX_OK = new Set(['ready', 'graded']);

/** 心跳间隔（毫秒）：驱动长操作的「已用时间」。 */
const TICK_MS = 1000;

/** 改版新增文案（strings.js 本轮冻结，新增一律走本地常量）。 */
const T = {
  ROUND_N_OF_M: '第 {n} 轮 / 共 {m} 次',
  ATTEMPTS_ONLY: '共 {m} 次机会',
  SHOW_DIFF: '查看改动',
  HIDE_DIFF: '收起改动',
  GRADE_DONE_TOAST: '校验完成：通过 {pass}/{total}',
  RESTART_TITLE: '用现存档案重开一轮？',
  RESTART_BODY: '这一轮绑定的档案已删除，改不了它的归属：会用你选的档案为这道题新建一轮记录。'
    + '旧记录还留在磁盘上（记分板只收「结束本轮」写进台账的成绩，旧记录不在榜上），'
    + '想清掉就点「废弃本轮（真删，不留成绩）」。',
  RESTART_DONE: '新一轮已就绪，可以在对话里发提示词了',
  // 状态栏状态词
  ST_IDLE: '还没开始',
  ST_FOREIGN: '当前查看的记录属于档案「{model}」，与所选档案不一致，只能看不能改',
  ST_REVEALED: '已揭晓参考解，成绩不进台账',
  // 主按钮（每时刻一个；其余出口在 ⋯ 菜单）
  P_PREPARE: '准备沙箱',
  P_PREPARING: '正在准备沙箱…',
  P_PREPARING_REASON: '首次准备会铺开整个沙箱副本，一般几秒到几十秒。',
  /** 提示词按钮带等级：它发的是「第 n 级」那一份，不是笼统的「当前提示词」。 */
  P_SEND_PROMPT_LEVEL: '发送第 {n} 级提示词',
  P_NEED_PROMPT: '这一轮的提示词还没读到（任务详情读取失败）：在「任务与提示词」节点里点「重试」，读回来再发送。',
  P_GRADE: '运行校验',
  P_REGRADING: '校验进行中…',
  P_GRADING_REASON: '服务端正在跑隐藏用例，跑完自动出分。',
  // 常驻校验按钮：第一次校验没过之后它必须还在（用户口径，2026-10-02）
  G_GRADE: '运行校验',
  G_REGRADE: '重新校验',
  P_PROMOTE: '进入第 {n} 轮',
  P_REVEAL: '查看参考解',
  P_BACK_TASKS: '换一题',
  P_REBUILD: '重建沙箱',
  P_RELOAD: '重新载入',
  P_LOADING: '正在载入…',
  P_NEED_MODEL: '先在顶部状态栏选择一个模型档案。',
  P_REMOTE_BUSY: '模型仍在处理上一条消息：等它停下再继续，否则评的是写了一半的沙箱。',
  P_LOCAL_BUSY: '有操作正在进行，稍等。',
  P_NEED_SANDBOX: '沙箱还没就绪。',
  P_NEED_ACT: '模型还没有动过手：先把提示词发给它，改动落进沙箱后再校验。',
  P_FOREIGN: '这条记录属于档案「{model}」，不是当前选中的那个',
  // 「结束本轮」常驻按钮：工作台唯一的收尾出口
  FINISH: '结束本轮',
  FINISH_NO_RUN: '还没有运行记录。',
  FINISH_NO_SANDBOX: '沙箱已回收；点它照样结束本轮（记台账 + 删记录）。',
  FINISH_BUSY: '有操作正在进行，稍等。',
  // 「结束本轮」确认框
  FINISH_TITLE: '结束这一轮？',
  FINISH_BODY: '本轮成绩会写入台账，记分板与排行榜按最高分那条展示；'
    + '运行记录、对话、评分报告与 diff、沙箱与评分树全部删除，不可恢复。'
    + '结束后工作台回到干净空态，下次再跑这道题是全新一轮。',
  FINISH_BODY_NO_SCORE: '这一轮没有可计入台账的成绩（还没跑校验 / 已作废 / 已揭晓参考解）：'
    + '只删除记录、对话与沙箱，不留成绩。',
  FINISH_BODY_REVEALED: '这一轮已揭晓过参考解，成绩按规则不进台账：'
    + '只删除记录、对话与沙箱，不留成绩。',
  FINISHED_LEDGERED: '本轮成绩已记入台账',
  FINISHED_PLAIN: '本轮已结束',
  // ⋯ 菜单
  M_COPY_PATH: '复制沙箱路径',
  M_FILL_PROMPT: '把当前提示词填进输入框',
  M_FILL_PROMPT_REASON: '填进输入框后可以先改再发；不用它就直接点主按钮发送原文。',
  M_FILL_PROMPT_NO_TEXT: '这一轮的提示词还没读到，先点上方的「重试」。',
  M_RESET: '清空改动',
  M_REBUILD: '重建沙箱（回到基线）',
  M_REBUILD_REASON: '会作废已有成绩并归档本轮证据。',
  M_REGRADE: '重新校验',
  M_REOPEN: '继续对话（本轮分数作废）',
  M_REVEAL: '查看参考解',
  M_EXPORT: '导出报告 JSON',
  M_DISCARD: '废弃本轮（真删，不留成绩）',
  M_NO_RUN: '还没有运行记录。',
  M_NO_SANDBOX: '沙箱不存在或已回收。',
  M_NO_REPORT: '还没有校验报告。',
  M_ALREADY_REVEALED: '已揭晓过参考解。',
  M_DISCARD_BUSY: '对话或校验进行中，等它停下再删除。',
  DISCARD_TITLE: '废弃并彻底抹掉这一轮？',
  DISCARD_BODY: '运行记录、对话（含纪元归档）、评分报告与 diff、沙箱与评分树全部删除，'
    + '不进台账、不留任何痕迹，不可恢复。'
    + '与「结束本轮」的唯一区别就是这一条：跑错了不想让这次进榜时用它。',
  DISCARDED: '这一轮已彻底抹掉',
  MODEL_NOTE_NONE: '还没有模型档案。先到「模型档案」页新增一个，再回来选。',
  MODEL_NOTE_LOAD_FAILED: '模型档案读取失败：{reason}。可以点「重试」再读一次。',
};

/**
 * 创建工作台。
 *
 * @param {{
 *   taskId: string,
 *   region?: string,
 *   runId?: string,
 *   navigate?: (name: string, params: object, opts?: object) => void,
 *   models?: Array,
 *   modelsError?: string,
 *   subscribeModels?: (handler: (models: Array, modelsError: string) => void) => (() => void),
 *   reloadModels?: () => Promise<void>,
 *   prefs?: object
 * }} props
 * @returns {{el: HTMLElement, destroy: Function, focusRegion: Function, restoreScroll: Function, el_h1: HTMLElement, actions: object}}
 */
export function createWorkspace(props = {}) {
  const {
    taskId,
    runId: routeRunId = '',
    navigate = () => {},
    models = [],
    modelsError = '',
    subscribeModels = null,
    reloadModels = null,
    prefs = {},
  } = props;
  const scope = api.scope();
  /**
   * 地址栏里那一段 run_id 的可写副本：`routeRunId` 来自 const 解构，改它会直接
   * 抛 TypeError（废弃成功后要把它摘掉，否则刷新生出来是个指向空记录的书签）。
   */
  let urlRunId = String(routeRunId || '');
  /** 工作台自己的状态树（区域级订阅，避免整页重绘）。 */
  const store = createStore({
    loading: true,
    task: null,
    run: null,
    models,
    /** 模型档案读取失败的错误码（空串 = 没失败）；下拉为空时要说得出原因。 */
    modelsError,
    modelId: storage.get('last-model', ''),
    round: 1,
    /** 本机长操作：prepare / reset / rebuild / grade / finish / discard / notes */
    busy: '',
    error: null,
    /**
     * 任务详情读取失败的原因码（空串 = 没失败）。
     *
     * 任务详情是提示词的**唯一**来源：读不到就没有东西可发。它必须是一个可见状态
     * （任务节点显示「读取失败 + 重试」、发送按钮禁用并写原因），不能只闪一条 toast
     * ——2026-10-02 用户报的「重建沙箱后提示词没有出现」就是这条路径静默失败的样子。
     */
    taskError: '',
    /** 沙箱区日志：本机真实发过的每一步（后端不提供沙箱日志，见 NOTES.md） */
    opLog: [],
    /** 当前长操作已用毫秒（心跳累加） */
    elapsed: 0,
    newResult: false,
    /** 查看参考解的返回：{patch, notice} */
    revealed: null,
    /** 「查看改动」内联区是否展开 */
    diffOpen: false,
  });

  const wsStore = storage.scoped('ws', taskId);
  /**
   * 「这道题的当前 run」必须按档案分键。
   * 共用一个键的话，换一个（尤其是新建的）模型档案进同一道题，接上的还是上一个
   * 档案跑出来的那条记录 —— 分数、分组、校验横幅全是别人的结果。
   */
  function runKey(modelId) {
    return 'run_id:' + String(modelId || '');
  }

  function rememberModelRun(modelId, runId) {
    if (modelId) wsStore.set(runKey(modelId), runId);
  }

  function recallModelRun(modelId) {
    return String(wsStore.get(runKey(modelId), '') || '');
  }
  const offHandlers = [];
  /** 心跳句柄：有长操作时才存在 */
  let tickTimer = null;
  /** 已经写进地址栏的区域：跳转回来时不再重复改 hash，避免「路由 → 跳转 → 路由」打转。 */
  let lastUrlRegion = '';
  /** 「校验」窗口句柄：点下校验就开，出分原地切换（关掉不影响校验）。 */
  let gradeModal = null;
  /**
   * 题面自动补读的次数上限。
   *
   * 「有 run 却没有题面」是一个能自我修复的故障（首屏那次读取失败 / 被取消），
   * 但补读本身会失败，所以给它一个上限，之后停在「任务节点显示读取失败 + 重试」
   * 的可见状态上——不能让补读自己打转，也不能留一个点了没反应的发送按钮。
   */
  let taskRetryCount = 0;

  // 模型档案是异步到达的：首屏可能晚于本视图、模型页改完会再推一次、启动那次请求
  // 可能整个失败。只吃创建时的快照会让下拉永久停在「尚未选择档案」且没有原因，
  // 所以像 main.js 订阅 lastTask 那样订阅档案（退订交给 destroy，§10.4）。
  if (typeof subscribeModels === 'function') {
    offHandlers.push(
      subscribeModels((list, code) => {
        const next = { models: Array.isArray(list) ? list : [], modelsError: code || '' };
        // 能默认的默认：只有一个档案时直接选上，从选题到开聊少一步
        const current = store.getState().modelId;
        if (!current && next.models.length === 1) {
          next.modelId = String(next.models[0].id || '');
          storage.set('last-model', next.modelId);
        }
        patch(next);
      }),
    );
  }

  // ==================== 状态栏 ====================
  const h1 = el('h1', { class: 'ws-statusbar__title', tabindex: '-1' });
  const tierHost = el('span', { class: 'ws-statusbar__tier' });
  const roundText = el('span', { class: 'ws-statusbar__round' });

  const modelField = createField({
    label: S.RUN_MODEL_LABEL,
    name: 'ws-model',
    type: 'select',
    options: [{ value: '', label: S.RUN_MODEL_EMPTY }],
    onChange: (value) => {
      store.setState({ modelId: value });
      storage.set('last-model', value);
      adoptModelRun(value);
    },
  });
  modelField.el.classList.add('ws-statusbar__model');
  const modelNote = el('p', { class: 'u-faint ws-statusbar__model-note', role: 'status' });
  modelNote.hidden = true;
  const modelRetryBtn = createButton({
    label: S.ACTION_RETRY,
    size: 'sm',
    variant: 'ghost',
    onClick: () => {
      if (typeof reloadModels !== 'function') return;
      // 重读是一次网络请求，按钮自己担一个忙态，免得点完看着没反应又点一次。
      modelRetryBtn.update({ loading: true, busyLabel: S.ACTION_LOADING });
      Promise.resolve(reloadModels())
        .catch(() => {})
        .finally(() => modelRetryBtn.update({ loading: false }));
    },
  });
  // createButton 根节点自带 inline-flex，直接 hidden 藏不掉：套一层容器再整体切
  const modelRetryHost = el('span', { class: 'ws-statusbar__model-retry' }, modelRetryBtn.el);
  modelRetryHost.hidden = true;

  // 运行详情与本轮备注先建：它们的图标要挂进下面那条状态栏（四改：从对话流末尾
  // 的两个折叠节点收成图标 + 小窗口）。
  const runDetails = createRunDetails({
    onNotesSave: (note) => saveNote(note),
  });

  const statusDot = createStatusDot({ kind: 'idle', text: T.ST_IDLE });
  const statusBar = el(
    'header',
    { class: 'ws-statusbar' },
    el('div', { class: 'ws-statusbar__main' },
      h1,
      el('div', { class: 'ws-statusbar__meta' }, tierHost, roundText)),
    el('div', { class: 'ws-statusbar__side' },
      // 运行详情 / 本轮备注：2026-10-02 四改从对话流末尾的两个折叠节点收成
      // 状态栏的两个图标，点开是小窗口（见 workspace/run-details.js）。
      runDetails.toolsEl,
      modelField.el,
      modelRetryHost,
      statusDot.el),
    modelNote,
  );

  // ==================== 改动正文（菜单「查看改动」弹出的内联区） ====================
  const diffText = el('pre', { class: 'code-block__pre ws-diff__pre', tabindex: '0', role: 'region' });
  diffText.setAttribute('aria-label', S.RUN_DIFF_BODY);
  const diffCard = createDetailsCard({ title: S.RUN_DIFF_TITLE, content: diffText, open: true });
  const diffWrap = el('div', { class: 'ws-diff', hidden: true }, diffCard.el);

  // ==================== 任务节点 / 对话流 / 结果节点 / 操作栏 ====================
  const taskNode = createTaskNode({
    onRoundChange: (n) => doRoundChange(n),
    onSendPrompt: (text) => chatStream.sendText(text),
    onReload: () => reloadState(),
  });
  const chatStream = createChatStream({
    scope,
    onPrepare: () => doPrepare(),
    onRestartWithModel: (preferredId) => doRestartWithModel(preferredId),
  });
  const reportNode = createReportNode({
    onOpenReport: () => openReport(),
  });
  const dock = createDock();

  const bottom = el('div', { class: 'ws-bottom' }, dock.el, chatStream.composerEl);

  /**
   * 唯一的滚动容器（2026-10-02 三改）。
   *
   * 旧版是整页 .app-main 在滚：滚动条跑到窗口最右端，离对话列很远；而且刷新后
   * 落在顶部，要手动往下拽一大段才看到最新一条。现在任务节点 / 对话流 / 校验结果
   * 都装进这个盒子，滚动条贴着对话列右缘，重开页面直接停在最新（见
   * restoreScroll 与 chatStream.scrollToLatest）。
   *
   * 四改：运行详情与本轮备注搬出这里（进状态栏图标 + 小窗口），纵向空间全留给对话。
   */
  const scrollBox = el(
    'div',
    { class: 'ws-scroll', id: 'ws-scroll', tabindex: '0' },
    diffWrap,
    taskNode.el,
    chatStream.el,
    reportNode.el,
    // 「回到底部」浮钮挂在滚动区末尾：sticky 才能钉在 scrollport 下沿（挂到 .ws 上
    // 会压在输入区上，挂成 absolute 会跟着内容滚走）。
    chatStream.jumpEl,
  );

  const root = el(
    'div',
    { class: 'view ws' },
    statusBar,
    scrollBox,
    bottom,
  );

  // ==================== 状态机 → dock ====================
  /**
   * 每个时刻算出一个主按钮：准备沙箱 → 发送提示词 → 运行校验 → 进入下一轮/查看参考解。
   * 不可用时禁用并把原因写在按钮上（出口不许条件隐藏）。
   * @param {object} s store 快照
   * @returns {{label: string, onClick?: Function, disabled?: boolean, reason?: string, loading?: boolean, busyLabel?: string, kbd?: string}}
   */
  function primaryAction(s) {
    const run = s.run;
    if (s.loading && !run) return { label: T.P_LOADING, disabled: true };
    if (s.error && !run) return { label: T.P_RELOAD, onClick: () => reloadState() };
    if (!run) {
      return {
        label: T.P_PREPARE,
        onClick: () => doPrepare(),
        disabled: !s.modelId || Boolean(s.busy),
        loading: s.busy === 'prepare',
        busyLabel: T.P_PREPARING,
        reason: !s.modelId ? T.P_NEED_MODEL : s.busy ? T.P_LOCAL_BUSY : '',
      };
    }
    const chatBusy = Boolean(run.chat_busy);
    const grading = s.busy === 'grade' || run.status === 'grading';
    const preparing = s.busy === 'prepare' || run.status === 'preparing';
    const acted = run.model_acted !== false;
    const attempt = Number(run.attempt) || 1;
    const allowed = Number(run.attempts_allowed) || attempt;
    const revealed = Boolean(run.revealed) || Boolean(s.revealed);
    // 沙箱可用 = 状态就绪且目录真实存在：已回收的 run 状态仍停在 ready/graded，
    // 但 sandbox 字段已清空，只看状态会把「回收后」的出口错放出来
    const sandboxOk = SANDBOX_OK.has(run.status) && Boolean(run.sandbox);
    const sandboxReason = run.sandbox ? T.P_NEED_SANDBOX : T.M_NO_SANDBOX;
    const localBusy = Boolean(s.busy);

    if (run.status === 'error' || run.status === 'cancelled') {
      return { label: T.P_REBUILD, onClick: () => doRebuild(), disabled: localBusy, reason: localBusy ? T.P_LOCAL_BUSY : '' };
    }
    if (preparing) return { label: T.P_PREPARING, disabled: true, reason: T.P_PREPARING_REASON };
    if (grading) return { label: T.P_REGRADING, disabled: true, reason: T.P_GRADING_REASON, loading: true, busyLabel: T.P_REGRADING };
    if (revealed) return { label: T.P_BACK_TASKS, onClick: () => navigate('tasks') };
    const busyReason = chatBusy ? T.P_REMOTE_BUSY : localBusy ? T.P_LOCAL_BUSY : '';
    // 「发送第 n 级提示词」：n 取当前查看的那一级。没有题面就没得发——按钮必须禁用
    // 并把原因写出来（旧版这种情况下点了完全没反应，看起来像按钮坏了）。
    const promptText = promptTextOf(s);
    const sendDisabled = !promptText;
    const sendReason = sendDisabled ? T.P_NEED_PROMPT : '';
    if (run.report) {
      if (!acted) {
        // 报告在、模型却没动手：那份 0 分是误点出来的，第一步是让它真的开工
        return {
          label: t(T.P_SEND_PROMPT_LEVEL, { n: Number(s.round) || 1 }),
          onClick: () => chatStream.sendText(promptText),
          disabled: sendDisabled || !s.modelId || Boolean(busyReason),
          reason: sendReason || busyReason || (s.modelId ? '' : T.P_NEED_MODEL),
        };
      }
      if (attempt < allowed) {
        return {
          label: t(T.P_PROMOTE, { n: attempt + 1 }),
          onClick: () => doPromote(),
          disabled: Boolean(busyReason),
          reason: busyReason,
        };
      }
      return {
        label: T.P_REVEAL,
        onClick: () => doReveal(),
        disabled: Boolean(busyReason),
        reason: busyReason,
      };
    }
    if (!acted) {
      return {
        label: t(T.P_SEND_PROMPT_LEVEL, { n: Number(s.round) || 1 }),
        onClick: () => chatStream.sendText(promptText),
        disabled: sendDisabled || !s.modelId || !sandboxOk || Boolean(busyReason),
        reason: sendReason
          || busyReason
          || (!s.modelId ? T.P_NEED_MODEL : !sandboxOk ? sandboxReason : ''),
      };
    }
    return {
      label: T.P_GRADE,
      kbd: 'G',
      onClick: () => doGrade(),
      disabled: Boolean(busyReason),
      reason: busyReason,
    };
  }

  /**
   * 显式的「结束本轮」（红线：不能只留折叠起来的回收）。
   *
   * 2026-10-02 起这是工作台**唯一**的收尾出口：记台账 + 真删整条记录，
   * 沙箱是记录的一部分，跟着一起删。不再要求沙箱存在——跑批回收过沙箱的
   * 记录照样要能结束。
   * @param {object} s store 快照
   */
  function finishAction(s) {
    const run = s.run;
    const chatBusy = Boolean(run && run.chat_busy);
    const grading = Boolean(run && (s.busy === 'grade' || run.status === 'grading'));
    const disabled = !run || Boolean(s.busy) || grading || chatBusy;
    return {
      label: T.FINISH,
      onClick: () => doFinish(),
      disabled,
      loading: s.busy === 'finish',
      reason: !run
        ? T.FINISH_NO_RUN
        : chatBusy
          ? T.P_REMOTE_BUSY
          : grading
            ? T.P_GRADING_REASON
            : s.busy
              ? T.FINISH_BUSY
              : '',
    };
  }

  /**
   * 显式的「运行校验 / 重新校验」常驻按钮（2026-10-02 用户口径）。
   *
   * 旧版校验只在「模型动手后」当主按钮出现一次，出了分主按钮就变成「进入第 n 轮」，
   * 校验只剩 ⋯ 菜单里那一项——中级以上题目本来就有 2–3 次机会，第一次没过之后
   * 用户想「让模型接着改，再校验一次」时按钮却消失了，这就是「校验按钮隐藏」。
   * 现在它与「结束本轮」并排常驻：能走的路一直摆着，不可用时禁用 + 写原因。
   * @param {object} s store 快照
   */
  function gradeAction(s) {
    const run = s.run;
    const item = menuItems(s).find((i) => i.key === 'regrade') || {};
    const label = run && run.report ? T.G_REGRADE : T.G_GRADE;
    const grading = Boolean(run && (s.busy === 'grade' || run.status === 'grading'));
    return {
      label,
      onClick: () => doGrade(),
      disabled: Boolean(item.disabled) || grading,
      reason: grading ? T.P_GRADING_REASON : (item.reason || ''),
      loading: grading || s.busy === 'grade',
      busyLabel: T.P_REGRADING,
      kbd: 'G',
    };
  }

  /**
   * ⋯ 菜单：全量次级出口常列，不可用的禁用 + 原因写在项里。
   * @param {object} s store 快照
   * @returns {Array}
   */
  function menuItems(s) {
    const run = s.run;
    const chatBusy = Boolean(run && run.chat_busy);
    const grading = Boolean(run && (s.busy === 'grade' || run.status === 'grading'));
    const preparing = Boolean(run && (s.busy === 'prepare' || run.status === 'preparing'));
    const mutating = ['reset', 'rebuild', 'finish', 'discard'].includes(s.busy);
    const acted = run ? run.model_acted !== false : false;
    const hasReport = Boolean(run && run.report);
    const revealed = Boolean(run && run.revealed) || Boolean(s.revealed);
    const sandboxOk = Boolean(run && SANDBOX_OK.has(run.status) && run.sandbox);
    const foreign = Boolean(s.modelMismatch);
    const foreignReason = foreign ? t(T.P_FOREIGN, { model: (run && run.model) || '（空）' }) : '';
    /** 当前查看的那一级提示词正文（「填进输入框」这条口子的载荷）。 */
    const promptText = promptTextOf(s);
    // 同 primaryAction：已回收的 run 状态还是 ready/graded，必须看 sandbox 字段
    const sandboxGoneReason = run && !run.sandbox ? T.M_NO_SANDBOX : T.P_NEED_SANDBOX;
    const busyReason = chatBusy
      ? T.P_REMOTE_BUSY
      : grading
        ? T.P_GRADING_REASON
        : preparing || mutating
          ? T.P_LOCAL_BUSY
          : '';
    return [
      {
        key: 'diff',
        label: s.diffOpen ? T.HIDE_DIFF : T.SHOW_DIFF,
        disabled: !run,
        reason: run ? '' : T.M_NO_RUN,
        onClick: () => toggleDiff(),
      },
      {
        key: 'copy-path',
        label: T.M_COPY_PATH,
        disabled: !run || !run.sandbox,
        reason: !run ? T.M_NO_RUN : !run.sandbox ? T.M_NO_SANDBOX : '',
        onClick: () => doOpenDir(),
      },
      {
        // 旧版这颗按钮长在输入区里、名字叫「发送当前提示词」，实际只往草稿里填字
        // ——与底部主按钮同名不同义，用户报的「发送按钮重复」就是它。现在它是菜单里
        // 一条名字与行为一致的口子（2026-10-02 四改）。
        key: 'fill-prompt',
        label: T.M_FILL_PROMPT,
        disabled: !run || !promptText,
        reason: !run
          ? T.M_NO_RUN
          : !promptText
            ? T.M_FILL_PROMPT_NO_TEXT
            : T.M_FILL_PROMPT_REASON,
        onClick: () => chatStream.setDraft(promptText),
      },
      {
        key: 'reset',
        label: T.M_RESET,
        disabled: !run || !sandboxOk || Boolean(busyReason) || foreign,
        reason: foreignReason || (!run ? T.M_NO_RUN : !sandboxOk ? sandboxGoneReason : busyReason),
        onClick: () => doReset(),
      },
      {
        key: 'rebuild',
        label: T.M_REBUILD,
        disabled: !run || Boolean(busyReason) || foreign,
        reason: foreignReason || (!run ? T.M_NO_RUN : busyReason || T.M_REBUILD_REASON),
        onClick: () => doRebuild(),
      },
      {
        key: 'regrade',
        label: T.M_REGRADE,
        disabled: !run || foreign || !sandboxOk || !acted || Boolean(busyReason),
        reason: foreignReason || (!run ? T.M_NO_RUN : busyReason || (!acted ? T.P_NEED_ACT : !sandboxOk ? sandboxGoneReason : '')),
        onClick: () => doGrade(),
      },
      {
        key: 'reopen',
        label: T.M_REOPEN,
        disabled: !run || foreign || !hasReport || revealed || Boolean(busyReason),
        reason: foreignReason || (!run ? T.M_NO_RUN : revealed ? T.M_ALREADY_REVEALED : !hasReport ? T.M_NO_REPORT : busyReason),
        onClick: () => doReopen(),
      },
      {
        key: 'reveal',
        label: T.M_REVEAL,
        disabled: !run || foreign || !hasReport || revealed || Boolean(busyReason),
        reason: foreignReason || (!run ? T.M_NO_RUN : revealed ? T.M_ALREADY_REVEALED : !hasReport ? T.M_NO_REPORT : busyReason),
        onClick: () => doReveal(),
      },
      {
        key: 'export',
        label: T.M_EXPORT,
        disabled: !hasReport,
        reason: hasReport ? '' : T.M_NO_REPORT,
        onClick: () => doExport(),
      },
      {
        key: 'discard',
        label: T.M_DISCARD,
        danger: true,
        disabled: !run || Boolean(busyReason),
        reason: !run ? T.M_NO_RUN : busyReason ? T.M_DISCARD_BUSY : '',
        onClick: () => doDiscard(),
      },
    ];
  }

  /** 状态机签名没变就不重推 dock（轮询不吞按钮焦点）。 */
  let dockSignature = '';

  /**
   * 渲染 dock（主按钮 + 结束本轮 + ⋯ 菜单）。
   * @param {object} s store 快照
   */
  function renderDock(s) {
    const primary = primaryAction(s);
    const grade = gradeAction(s);
    const finish = finishAction(s);
    const items = menuItems(s);
    const sig = [
      primary.label, primary.disabled ? 1 : 0, primary.reason || '', primary.loading ? 1 : 0,
      grade.label, grade.disabled ? 1 : 0, grade.reason || '', grade.loading ? 1 : 0,
      finish.disabled ? 1 : 0, finish.reason || '', finish.loading ? 1 : 0,
      items.map((i) => `${i.key}|${i.label}|${i.disabled ? 1 : 0}|${i.reason || ''}`).join('#'),
    ].join('|');
    if (sig === dockSignature) return;
    dockSignature = sig;
    dock.update({ primary, grade, finish, menuItems: items });
  }

  // ==================== 状态栏渲染 ====================
  /**
   * 服务端状态 → 状态栏的一句话。对话在飞优先于一切（它在解释「为什么都点不动」）。
   * @param {object} s store 快照
   */
  function statusInfo(s) {
    const run = s.run;
    if (!run) {
      if (s.busy === 'prepare') return { kind: 'busy', text: T.P_PREPARING };
      return { kind: 'idle', text: T.ST_IDLE };
    }
    if (run.chat_busy) return { kind: 'busy', text: S.CHAT_REMOTE_BUSY };
    if (s.busy === 'grade' || run.status === 'grading') return { kind: 'busy', text: S.RUN_STATUS_GRADING };
    if (s.busy === 'prepare' || run.status === 'preparing') return { kind: 'busy', text: S.RUN_STATUS_PREPARING };
    if (run.status === 'queued') return { kind: 'busy', text: S.RUN_STATUS_QUEUED };
    if (run.status === 'error') return { kind: 'error', text: S.RUN_STATUS_ERROR };
    if (s.modelMismatch) return { kind: 'warn', text: t(T.ST_FOREIGN, { model: run.model || '（空）' }) };
    if (run.revealed || s.revealed) return { kind: 'warn', text: T.ST_REVEALED };
    if (run.status === 'graded') {
      const score = run.report && typeof run.report.score === 'number' ? ` · ${Math.round(run.report.score)} 分` : '';
      return { kind: 'ok', text: `${S.RUN_STATUS_GRADED}${score}` };
    }
    return { kind: 'ok', text: S.RUN_STATUS_READY };
  }

  /**
   * 同步模型档案下拉选项。选项集合没变就不动 select，避免打断键盘选择（§11.2 #14）。
   * @param {Array} list
   * @param {string} selected
   */
  function syncModelOptions(list, selected) {
    const options = [{ value: '', label: S.RUN_MODEL_EMPTY }].concat(
      (list || []).map((m) => {
        // 详情取第一个与 id 不同名的字段：model → 显示名 → 供应商名 → 备注。
        // id 本身就是模型全名时（供应商结构下 id==model 是常态），别再补「未配置」——
        // 它已经配置好了，那四个字只会误导。
        const detail = (m.model && m.model !== m.id && m.model)
          || (m.name && m.name !== m.id && m.name)
          || m.provider_name
          || m.note
          || '';
        return { value: m.id, label: detail ? `${m.id}（${detail}）` : m.id };
      }),
    );
    const sig = options.map((o) => o.value).join('|');
    if (sig === modelField.__sig) {
      if (selected !== undefined && selected !== modelField.getValue()) modelField.setValue(selected);
      return;
    }
    modelField.__sig = sig;
    modelField.update({ options, value: selected ?? '' });
  }

  /**
   * 档案下拉为空时把原因摊开：读取失败 ≠ 真的没有档案，两种情况给不同的话与出口。
   * 首屏数据没落定（工作台还在取数）时先别下「没有档案」的结论。
   */
  function renderModelNote(s) {
    const count = (s.models || []).length;
    const failed = Boolean(s.modelsError);
    const pending = s.loading && !failed;
    if (count === 0 && !pending) {
      setText(modelNote, failed
        ? t(T.MODEL_NOTE_LOAD_FAILED, { reason: errorTitle(s.modelsError) })
        : T.MODEL_NOTE_NONE);
      modelNote.hidden = false;
    } else {
      setText(modelNote, '');
      modelNote.hidden = true;
    }
    // 只有「读取失败」才值得原地重试；确实一个档案都没有该去模型页新增
    modelRetryHost.hidden = !(failed && count === 0);
  }

  /**
   * 渲染状态栏（轮询带来的每次状态变化都会走到这里）。
   * @param {object} s store 快照
   */
  function renderStatusBar(s) {
    const task = s.task;
    const run = s.run;
    setText(h1, task ? `${task.id} · ${task.title}` : taskId);
    tierHost.textContent = '';
    tierHost.appendChild(tierBadge(task ? task.tier : '', { attempts: task ? task.attempts : 0 }).el);

    // 第 n 轮 / 共 m 次：数字走 tabular-nums。
    // 还没准备沙箱时没有「轮」可言，只报这题总共几次机会。
    const max = run ? run.attempts_allowed : task ? task.attempts : 1;
    setText(roundText, run ? t(T.ROUND_N_OF_M, { n: run.attempt, m: max }) : t(T.ATTEMPTS_ONLY, { m: max }));

    syncModelOptions(s.models, s.modelId);
    // 模型档案是准备沙箱的前提，没选就常驻写在字段上：以前只在点准备沙箱时
    // 闪一条 toast，用户回头找不到自己漏了什么。
    modelField.update({ error: s.modelId ? '' : S.RUN_MODEL_REQUIRED });
    renderModelNote(s);

    const info = statusInfo(s);
    statusDot.update({ kind: info.kind, text: info.text });
  }

  // ==================== 动作绑定 ====================
  /**
   * 只读回读：任务详情 + 当前运行记录。
   * 「重试」按钮只做这件事——绝不能顺手触发准备 / 校验这类写操作，
   * 否则一次读取失败后的重试会悄悄开出新的一轮。
   * @returns {Promise<void>}
   */
  function reloadState() {
    return load();
  }

  // ==================== 心跳（长操作的已用时间，§13.2） ====================
  /**
   * 按当前是否在忙，起停心跳。
   */
  function ensureTicker() {
    const s = store.getState();
    const need = Boolean(s.busy) || Boolean(s.run && BUSY_STATUS.has(s.run.status));
    if (need && !tickTimer) {
      tickTimer = window.setInterval(() => {
        store.setState({ elapsed: (store.getState().elapsed || 0) + TICK_MS });
      }, TICK_MS);
    } else if (!need && tickTimer) {
      window.clearInterval(tickTimer);
      tickTimer = null;
    }
  }

  /**
   * 写状态的唯一入口：顺手校准心跳，避免各处漏调。
   * @param {object} next
   */
  function patch(next) {
    store.setState(next);
    ensureTicker();
  }

  /**
   * 往沙箱日志追加一行（只记本机真实发生过的步骤）。
   * @param {string} line
   */
  function logOp(line) {
    const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    const lines = (store.getState().opLog || []).concat([`[${stamp}] ${line}`]);
    patch({ opLog: lines.slice(-200) });
  }

  // ==================== 轮询 ====================
  /**
   * 拉一次运行状态。
   * @param {{seq: number}} ctx poller 传回的请求序号
   * @returns {Promise<object>}
   */
  async function pollRun(ctx) {
    const state = store.getState();
    if (!state.run) return null;
    const data = await api.get(`/runs/${encodeURIComponent(state.run.run_id)}`, { scope });
    void ctx;
    // 拉回来就要写回状态树：poller 只负责「什么时候拉」，落地归 applyRun。
    // 不写回的话轮询就只是空转，进度永远停在「正在校验」。
    if (data) applyRun(data);
    return data;
  }

  const poller = createPoller({
    fn: pollRun,
    interval: 1200,
    enabled: () => {
      const s = store.getState();
      // 对话在飞（chat_busy）也必须轮询：批次线程代发的提示词结束时，run 状态
      // 一直是 ready（不在 BUSY_STATUS 里），没有这条「模型仍在作答」就永远刷
      // 不掉，直到手动刷新页面（2026-10-05 T1-02 实测）。
      return Boolean(s.run) && (BUSY_STATUS.has(s.run.status) || Boolean(s.run.chat_busy));
    },
    onError: (err, times) => {
      // 状态栏没有独立的轮询错误位，轮询失败改为 toast 报错（连接状态另有全局连接条负责）
      if (err instanceof ApiError && err.code === 'OFFLINE') {
        if (times === 1) {
          showToast({ message: errorTitle('OFFLINE'), detail: errorBody('OFFLINE'), kind: 'error', duration: 8000 });
        }
      } else if (err instanceof ApiError && err.code !== 'ABORTED') {
        showToast({ message: errorTitle(err.code), detail: errorBody(err.code), kind: 'error', duration: 8000 });
      }
    },
  });

  // ==================== 状态订阅（区域级，§10.5） ====================
  offHandlers.push(
    store.subscribe(null, (next, prev) => {
      renderStatusBar(next);
      renderDock(next);

      const runChanged = next.run !== prev.run;
      const busyChanged = next.busy !== prev.busy;
      const loadingChanged = next.loading !== prev.loading;
      const errorChanged = next.error !== prev.error;
      const revealedChanged = next.revealed !== prev.revealed;
      const newResultChanged = next.newResult !== prev.newResult;
      // 心跳只推进进度数字：不重建任何输入控件（§11.2 #14）
      const tickOnly = next.elapsed !== prev.elapsed
        && !runChanged && !busyChanged && !loadingChanged && !errorChanged
        && !revealedChanged && !newResultChanged;

      if (tickOnly) {
        reportNode.update({ elapsed: next.elapsed, busy: next.busy, run: next.run });
        return;
      }

      // 任务节点：还没开始的一轮默认展开（这是唯一的下一步），跑起来后收起到一行摘要；
      // 用户手动开合过之后不再覆盖（setOpen 只在 run 有无翻转时调用）。
      if (runChanged && Boolean(prev.run) !== Boolean(next.run)) {
        taskNode.setOpen(!next.run && wsStore.get('taskOpen', '') !== 'closed');
      }
      // 服务端仍有发送线程在跑（批次代发 / 手动发送 / 浏览器中途刷新过）：
      // 把轮询拉起来。run 状态多半停在 ready（不在 BUSY_STATUS），不拉的话
      // chat_busy 翻回 false 时没人去收尾，「模型仍在作答」会一直挂着。
      if (runChanged && next.run && next.run.chat_busy && !poller.getState().running) {
        poller.start();
      }
      const runForSend = next.run;
      const chatOk = runForSend && (runForSend.status === 'ready' || runForSend.status === 'graded');
      const promptOk = hasPrompt(next);
      // 题面读失败 / 这一级没正文，都要在节点里显形（展开 + 重试），不能只在 toast 里闪一下：
      // 它是「发送第 n 级提示词」能不能用的唯一依据。没有 error 时节点里就没有重试出口，
      // 而发送按钮的原因正指着那个出口。
      const taskFailure = next.taskError
        ? { code: next.taskError }
        : (!promptOk && next.run ? { code: 'LOAD_FAILED' } : null);
      taskNode.update({
        task: next.task,
        run: next.run,
        round: next.round,
        loading: next.loading,
        error: taskFailure,
        sendDisabled: !promptOk || !runForSend || !chatOk
          || Boolean(runForSend.chat_busy) || Boolean(runForSend.model_gone),
        sendReason: !promptOk
          ? T.P_NEED_PROMPT
          : !runForSend
            ? '先准备沙箱，再把提示词发给模型。'
            : runForSend.model_gone
              ? '这一轮绑定的模型档案已被删除。'
              : runForSend.chat_busy
                ? S.CHAT_REMOTE_BUSY
                : !chatOk
                  ? '这一轮还不能对话：沙箱没就绪或已经收束。'
                  : '',
      });
      // 题面缺了 / 落后了就自动补读一次（有上限）：补不上会停在任务节点的「读取失败 + 重试」。
      if (taskStale(next) && taskRetryCount < 2) {
        taskRetryCount += 1;
        refreshTask(next.run ? next.run.run_id : '');
      }
      chatStream.update({ run: next.run, pickedModelId: next.modelId, promptReady: promptOk });
      // 状态一变就可能长高（结果条、运行详情、任务节点展开）：对话在飞时把末尾带回来。
      // 跟不跟、跟到哪里全由 chatStream 判断，这里只负责喊一声。
      chatStream.followLatest();
      // newResult 只在翻转成 true 时下发：false 会把结果节点上「新结果」标记提前冲掉
      const reportState = {
        run: next.run,
        busy: next.busy,
        elapsed: next.elapsed,
        revealed: next.revealed,
        modelMismatch: next.modelMismatch,
      };
      if (next.newResult) reportState.newResult = true;
      reportNode.update(reportState);
      // 校验窗口开着就跟着状态走：进行中 → 日志/用时；出分 → 原地换成结果正文。
      if (gradeModal && gradeModal.isOpen()) {
        gradeModal.update({ run: next.run, elapsed: next.elapsed });
      }
      runDetails.update({ run: next.run, busy: next.busy, loading: next.loading, opLog: next.opLog });
    }),
  );

  /**
   * 当前这一轮要发的提示词正文（任务详情是它唯一的来源）。
   *
   * 与任务节点共用同一份数据：发送按钮的「能不能点」与「点了发什么」必须同源，
   * 否则会出现「按钮说能发、发出去的却是另一级」的错位。
   * @param {object} s store 快照
   * @returns {string}
   */
  function promptTextOf(s) {
    const level = Number(s.round) || 1;
    const hit = ((s.task && s.task.prompts) || []).find((p) => Number(p.level) === level);
    return hit ? String(hit.text || '') : '';
  }

  /**
   * 这一轮的提示词在不在本地。
   * @param {object} s store 快照
   * @returns {boolean}
   */
  function hasPrompt(s) {
    return Boolean(promptTextOf(s).trim());
  }

  /**
   * 题面是不是缺了 / 落后了：有 run 却没读到题面，或题面停在更低的提示词级
   * （进下一轮时那次重读失败了）。这两种情况都让「发送第 n 级提示词」变成哑按钮，
   * 所以订阅里发现就补读一次。
   * @param {object} s store 快照
   * @returns {boolean}
   */
  function taskStale(s) {
    if (s.loading || s.taskError) return false;
    if (!s.task) return Boolean(s.run);
    if (!s.run) return false;
    // 题面在、但这一级没有正文（级数对不上 / 服务端少给了）：一样没法发
    if (!hasPrompt(s)) return true;
    return Number(s.task.unlocked_prompts || 0) < Number(s.run.attempt || 1);
  }

  /**
   * 已解锁的提示词级。
   * 契约：任务详情只回传已解锁的 `prompts`，所以「出现即已解锁」。
   * @param {object} state
   * @returns {number[]}
   */
  function unlockedLevels(state) {
    const levels = ((state.task && state.task.prompts) || []).map((p) => Number(p.level));
    if (levels.length) return levels;
    return state.run ? [Number(state.run.attempt)] : [];
  }

  // ==================== 动作 ====================
  /**
   * 统一的错误呈现：顶部 toast（标题 + 详情）；开发模式把原始 code 打到 console。
   * @param {unknown} err
   * @param {string} [context]
   */
  function reportError(err, context) {
    const code = err instanceof ApiError ? err.code : 'INTERNAL';
    const title = errorTitle(code);
    const bodyText = errorBody(code, err instanceof ApiError ? err.vars : undefined);
    patch({ busy: '' });
    if (!(err instanceof ApiError && err.code === 'ABORTED')) {
      showToast({ message: title, detail: bodyText, kind: 'error', duration: 10000 });
    }
    if (context && typeof console !== 'undefined' && typeof console.warn === 'function') {
      console.warn(`[workspace] ${context}：${code}`);
    }
  }

  /**
   * 准备沙箱（第 1 轮）。
   * 契约：POST /api/runs 是**同步**的，会阻塞到沙箱铺完，所以按长请求处理。
   */
  async function doPrepare() {
    const s = store.getState();
    if (!s.modelId) {
      showToast({ message: S.RUN_MODEL_REQUIRED, kind: 'warn', duration: 5000 });
      modelField.focus();
      return;
    }
    if (s.busy) return;
    patch({ busy: 'prepare', error: null, elapsed: 0 });
    announce(S.ANNOUNCE_SANDBOX_PREPARING);
    logOp(t(S.SANDBOX_PREPARE_SUBMIT, { task: taskId, model: s.modelId, n: 1 }));
    try {
      const res = await api.longPost('/runs', { task: taskId, model: s.modelId, attempt: 1 }, { scope });
      const runId = res.run_id;
      storage.set('last-task', taskId);
      rememberModelRun(s.modelId, runId);
      logOp(t(S.SANDBOX_PREPARE_DONE, { path: res.sandbox || '' }));
      await loadRun(runId);
      // 新记录建好之后要按它读一遍题面：准备沙箱这条路径以前不碰任务详情，
      // 一旦首屏那次读取失败过，接下来就是「有沙箱、没有提示词」的死状态
      // （2026-10-02 用户报的现象）。
      await refreshTask(runId);
      patch({ busy: '', elapsed: 0 });
    } catch (err) {
      reportError(err, '准备沙箱');
    }
  }

  /**
   * 换档案 = 换一条时间线：接上这个档案在这道题上的最新记录，没有就回到空态。
   *
   * 不做这件事的话，新建一个档案进同一道题，面板会原样显示上一个档案的分数、
   * 分组和校验横幅——用户读到的是"这个模型已经考过了"。
   * @param {string} modelId
   * @returns {Promise<void>}
   */
  async function adoptModelRun(modelId) {
    if (!modelId) return;
    const s = store.getState();
    if (s.run && String(s.run.model || '') === modelId) return;
    try {
      const res = await api.get(
        `/runs?task=${encodeURIComponent(taskId)}&model=${encodeURIComponent(modelId)}`,
        { scope });
      const items = res.runs || [];
      const remembered = recallModelRun(modelId);
      const target = items.some((r) => r.run_id === remembered) ? remembered : (items[0] ? items[0].run_id : '');
      if (target) {
        await loadRun(target);
        ensureTicker();
        if (BUSY_STATUS.has((store.getState().run || {}).status)) poller.start();
        return;
      }
      patch({ run: null, revealed: null, modelMismatch: false });
      poller.stop();
    } catch (err) {
      reportError(err, '切换档案');
    }
  }

  /**
   * 拉取运行状态并写回 store。
   * @param {string} runId
   * @returns {Promise<void>}
   */
  async function loadRun(runId) {
    try {
      const run = await api.get(`/runs/${encodeURIComponent(runId)}`, { scope });
      // 已揭晓的参考解从运行记录回读（以前只在揭晓当次下发，刷新就没了）
      if (!store.getState().revealed) restoreRevealed(run);
      applyRun(run);
      return run;
    } catch (err) {
      if (err instanceof ApiError && err.code === 'NO_RUN') {
        patch({ run: null, busy: '' });
        return null;
      }
      throw err;
    }
  }

  /**
   * 把一次拉取结果写进 store（只做差异更新，§10.5），并处理阶段推进播报与结果提醒。
   * @param {object} run
   */
  function applyRun(run) {
    const prev = store.getState();
    const prevStatus = prev.run ? prev.run.status : '';
    // 换 run 或进下一轮：上一轮的改动正文当场作废（在途响应由 diffToken 比对丢掉）
    if (diffTokenOf(prev.run) !== diffTokenOf(run)) invalidateDiff();
    const serverBusy = BUSY_STATUS.has(run.status);
    const next = { run, newResult: false };
    // 这条记录是不是"当前选的档案"跑出来的：不是的话面板上的分数就是别人的成绩，
    // 只能看不能写（校验、进下一轮、揭晓、导出都会写进这条不属于它的记录）。
    const wanted = String(prev.modelId || '');
    next.modelMismatch = Boolean(wanted) && String(run.model || '') !== wanted;

    // 校验是异步的：busy 只在本机点下「运行校验」到服务端接手之间成立
    if (serverBusy) {
      next.busy = run.status === 'grading' ? 'grade' : 'prepare';
    } else if (prev.busy === 'grade' || prev.busy === 'prepare') {
      next.busy = '';
      next.elapsed = 0;
    }

    // 阶段切换播报（polite，不抢焦点，§13.2）
    if (prevStatus === 'grading' && run.status === 'graded') {
      const report = run.report || {};
      const sum = report.summary || {};
      announce(
        t(S.ANNOUNCE_GRADE_DONE, {
          n: report.score === undefined ? 0 : report.score,
          summary: t(S.GRADE_GROUP_SUMMARY, { pass: sum.green || 0, total: (sum.groups || []).length }),
        }),
      );
      next.newResult = true;
      showToast({
        message: t(T.GRADE_DONE_TOAST, { pass: sum.green || 0, total: (sum.groups || []).length }),
        kind: 'success',
        duration: 8000,
      });
      guideToResult();
    } else if (prevStatus === 'grading' && run.status === 'error') {
      announce(S.ANNOUNCE_GRADE_FAILED);
    } else if (!prevStatus && run.status === 'ready') {
      announce(S.ANNOUNCE_SANDBOX_READY);
    }
    if (run.status === 'grading' && prevStatus !== 'grading') {
      announce(S.ANNOUNCE_GRADE_STARTED);
    }

    patch(next);
    rememberModelRun(run.model, run.run_id);
  }

  /**
   * 打开「校验报告」独立窗口（结果条上「查看完整报告」的唯一去向）。
   * footer 出口（作废 / 揭晓 / 导出）直接复用 ⋯ 菜单的同一套状态机判定：
   * 禁用态与原因两边永远一致，不会出现「菜单里禁用了、窗口里还能点」的分叉。
   */
  function openReport() {
    const s = store.getState();
    if (!s.run || !s.run.report) return;
    openReportModal({
      run: s.run,
      revealed: s.revealed,
      newResult: Boolean(s.newResult),
      // 报告窗开着时这一轮可能又跑完一次校验：传这个进去，窗口顶部会出现
      // 一条 polite 的「有新结果，点击刷新」。正文不自动替换（§13.2）。
      reloadRun: () => api.get(`/runs/${encodeURIComponent(s.run.run_id)}`, { scope }),
      actions: reportActions(s),
    });
  }

  /**
   * 校验/报告两个窗口共用的 footer 出口：从 ⋯ 菜单的同一套判定里取，
   * 保证「菜单里禁用的，窗口里也点不动，理由还一样」。
   * @param {object} s store 快照
   * @returns {Array}
   */
  function reportActions(s) {
    const items = menuItems(s);
    const pick = (key) => items.find((i) => i.key === key) || {};
    const reopen = pick('reopen');
    const reveal = pick('reveal');
    const exportItem = pick('export');
    return [
      {
        key: 'reopen',
        label: reopen.label || T.M_REOPEN,
        disabled: Boolean(reopen.disabled),
        reason: reopen.reason || '',
        variant: 'default',
        onClick: () => doReopen(),
      },
      {
        key: 'reveal',
        label: reveal.label || T.M_REVEAL,
        disabled: Boolean(reveal.disabled),
        reason: reveal.reason || '',
        variant: 'default',
        onClick: () => doReveal(),
      },
      {
        key: 'export',
        label: exportItem.label || T.M_EXPORT,
        disabled: Boolean(exportItem.disabled),
        reason: exportItem.reason || '',
        variant: 'ghost',
        keepOpen: true,
        onClick: () => doExport(),
      },
    ];
  }

  /**
   * 打开「校验」窗口（2026-10-02 用户口径：只要进行校验就弹出来）。
   *
   * 用户点下校验的那一刻就要看到「这次在查什么」——本题的分组口径、每组守哪个
   * 出口、权重、回归与越界的规则；出分后同一个窗口原地换成结果正文。
   * 这不是 §13.2 说的「完成事件自动弹窗」：弹窗由用户自己的动作触发，
   * 结果条与「查看完整报告」照旧留一份回看入口。
   */
  function openGrade() {
    const s = store.getState();
    gradeModal = openGradeModal({
      run: s.run,
      plan: (s.task && s.task.check_plan) || [],
      revealed: s.revealed,
      elapsed: s.elapsed,
      actions: reportActions(s),
    });
    return gradeModal;
  }

  /**
   * 校验完成的结果提醒：滚到对话流里的结果条并聚焦它（红线：结果条内联在流里；
   * 完整报告在独立窗口，校验完成不自动弹窗、不抢焦点，§13.2）。
   * 焦点在输入框里时不抢焦点——toast 已经报了分数，不打断正在打字的人。
   */
  function guideToResult() {
    if (isEditableTarget(document.activeElement)) return;
    focusRegion('grade');
    reportNode.focusResult();
  }

  /**
   * 清空改动（带二次确认，§9 重点交互 ②）。
   * 确认文案已写明「只重置沙箱不动记录」。
   */
  async function doReset() {
    const s = store.getState();
    if (!s.run) {
      showToast({ message: S.ERR_NO_SANDBOX, detail: S.ERR_NO_SANDBOX_BODY, kind: 'warn', duration: 6000 });
      return;
    }
    if (s.busy) return;
    if (prefs.confirmDestructive === false) {
      await doResetNow();
      return;
    }
    const ok = await confirmDialog({
      title: S.CONFIRM_RESET_TITLE,
      messages: [S.CONFIRM_RESET_BODY_1, S.CONFIRM_RESET_BODY_2, S.CONFIRM_RESET_BODY_3],
      confirmLabel: S.SANDBOX_RESET,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (ok) await doResetNow();
  }

  /**
   * 真正执行清空（同步请求，秒级回基线）。
   */
  async function doResetNow() {
    const s = store.getState();
    if (!s.run) return;
    patch({ busy: 'reset', elapsed: 0 });
    announce(S.ANNOUNCE_SANDBOX_RESET_DONE_PENDING);
    try {
      const res = await api.post('/sandbox/reset', { run_id: s.run.run_id }, { scope });
      const cleaned = Number(res.cleaned || 0);
      const seconds = Number(res.seconds || 0);
      logOp(t(S.SANDBOX_RESET_CLEANED, { n: cleaned, time: seconds }));
      await loadRun(s.run.run_id);
      patch({ busy: '', elapsed: 0 });
      announce(S.ANNOUNCE_SANDBOX_RESET_DONE);
      // 完成后提示换模型（§9 重点交互 ②）
      showToast({ message: S.CONFIRM_RESET_DONE, detail: S.CONFIRM_RESET_DONE_HINT, kind: 'success', duration: 9000 });
    } catch (err) {
      reportError(err, '清空改动');
    }
  }

  /**
   * 重建沙箱（带二次确认）。
   * 回基线必须作废旧成绩：服务端 _archive_epoch + _void_rounds 之后，旧报告不再随
   * run_view 下发，对话流里的旧结果节点与旧错误列表跟着消失（红线 4）。
   */
  async function doRebuild() {
    const s = store.getState();
    if (!s.run) {
      showToast({ message: S.ERR_NO_SANDBOX, detail: S.ERR_NO_SANDBOX_BODY, kind: 'warn', duration: 6000 });
      return;
    }
    if (s.busy) return;
    if (prefs.confirmDestructive === false) {
      await doRebuildNow();
      return;
    }
    const ok = await confirmDialog({
      title: S.CONFIRM_REBUILD_TITLE,
      messages: [S.CONFIRM_REBUILD_BODY_1, S.CONFIRM_REBUILD_BODY_2, S.CONFIRM_REBUILD_BODY_3],
      confirmLabel: S.SANDBOX_REBUILD,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (ok) await doRebuildNow();
  }

  /**
   * 真正执行重建（同步请求）。
   */
  async function doRebuildNow() {
    const s = store.getState();
    if (!s.run) return;
    patch({ busy: 'rebuild', elapsed: 0 });
    logOp(t(S.SANDBOX_REBUILD_SUBMIT, { task: taskId }));
    try {
      const res = await api.post('/sandbox/rebuild', { run_id: s.run.run_id, task: taskId }, { scope });
      logOp(t(S.SANDBOX_REBUILD_DONE, { path: res.sandbox || '' }));
      await loadRun(s.run.run_id);
      // 题面必须按**这条记录**重读：不带 run_id 时服务端会拿「这道题最新的记录」
      // 去算已解锁的提示词级，重建之后可能读到另一条记录的级数（提示词对不上）。
      await refreshTask(s.run.run_id);
      patch({ busy: '', elapsed: 0 });
      announce(S.ANNOUNCE_SANDBOX_READY);
      showToast({ message: S.CONFIRM_REBUILD_DONE, kind: 'success', duration: 6000 });
    } catch (err) {
      reportError(err, '重建沙箱');
    }
  }

  /**
   * 运行校验。
   * 契约：POST /api/runs/{id}/grade **异步**返回，真实进度靠轮询 GET /api/runs/{id}。
   * 弹窗：点下去就开「校验」窗口（说明这次在查什么 + 实时进度），出分原地换结果。
   */
  async function doGrade() {
    const s = store.getState();
    if (!s.run) {
      showToast({ message: S.ERR_NO_SANDBOX, detail: S.ERR_NO_SANDBOX_BODY, kind: 'warn', duration: 6000 });
      return;
    }
    if (s.busy) return;
    // 乐观状态要可回滚：POST 失败后 run.status 停在 'grading' 会让按钮永久禁用、
    // 心跳永久累加，且轮询只在成功路径启动，UI 就永远卡死。
    const runBefore = s.run;
    if (s.run.chat_busy) {
      showToast({ message: S.CHAT_REMOTE_BUSY, detail: S.CHAT_REMOTE_BUSY_DETAIL, kind: 'warn', duration: 8000 });
      chatStream.focusComposer();
      return;
    }
    patch({ busy: 'grade', newResult: false, elapsed: 0, run: { ...s.run, status: 'grading' } });
    announce(S.ANNOUNCE_GRADE_STARTED);
    // 弹窗用乐观状态开：先看到「正在校验 + 这次查什么」，出分后原地换正文。
    openGrade();
    try {
      await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/grade`, {}, { scope });
      poller.start();
      ensureTicker();
    } catch (err) {
      patch({ busy: '', run: runBefore });
      reportError(err, '运行校验');
      // 失败必须把本地乐观状态打回服务端真相，否则计时器和日志区会永远停在「正在校验」
      patch({ busy: '', elapsed: 0 });
      try {
        await loadRun(s.run.run_id);
      } catch { /* 保留本地状态即可 */ }
      // 窗口里也要说明失败原因：只弹一条 toast 的话，窗口还写着「正在校验」。
      if (gradeModal && gradeModal.isOpen()) {
        const code = err instanceof ApiError ? err.code : 'INTERNAL';
        gradeModal.update({
          run: store.getState().run,
          elapsed: 0,
          error: `${errorTitle(code)}：${errorBody(code)}`,
        });
      }
    }
  }

  /**
   * 进入下一轮（解锁下一级提示词）。
   */
  async function doPromote() {
    const s = store.getState();
    if (!s.run) return;
    if (s.busy) return;
    // 机会用完就别弹确认框了，直接说明；不然用户会以为还没进过下一轮
    if (Number(s.run.attempt) >= Number(s.run.attempts_allowed)) {
      showToast({ message: S.GRADE_PROMOTE_EXHAUSTED, kind: 'warn', duration: 6000 });
      return;
    }
    const nextLevel = Number(s.run.attempt) + 1;
    if (prefs.confirmDestructive === false) {
      await promoteNow(nextLevel);
      return;
    }
    // 进入下一轮不清分、不删改动，不是破坏性操作，不该套 danger 红框；
    // 「最后一次机会」也只在真的是最后一次时才说。
    const isLast = nextLevel >= Number(s.run.attempts_allowed || 0);
    const ok = await confirmDialog({
      title: t(S.CONFIRM_PROMOTE_TITLE, { n: nextLevel }),
      messages: isLast
        ? [t(S.CONFIRM_PROMOTE_BODY_1, { n: nextLevel }), S.CONFIRM_PROMOTE_BODY_2]
        : [t(S.CONFIRM_PROMOTE_BODY_1, { n: nextLevel })],
      confirmLabel: t(S.GRADE_PROMOTE, { n: nextLevel }),
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
    });
    if (ok) await promoteNow(nextLevel);
  }

  /**
   * 真正解锁下一轮；解锁后要重拉任务详情才能拿到新一级的提示词。
   * @param {number} nextLevel
   */
  async function promoteNow(nextLevel) {
    const s = store.getState();
    if (!s.run) return;
    patch({ busy: 'grade', elapsed: 0 });
    try {
      const res = await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/promote`, {}, { scope });
      const level = Number(res.attempt) || nextLevel;
      await refreshTask(s.run.run_id);
      await loadRun(s.run.run_id);
      patch({ round: level, busy: '', elapsed: 0 });
      wsStore.set('round', level);
      announce(t(S.ANNOUNCE_ROUND_CHANGED, { n: level }));
      showToast({
        message: t(S.GRADE_PROMOTE, { n: level }),
        detail: res.can_promote ? '' : S.GRADE_PROMOTE_LAST,
        kind: 'success',
        duration: 5000,
      });
      focusRegion('prompt');
    } catch (err) {
      reportError(err, '进入下一轮');
    }
  }

  /**
   * 误校验的补救：作废本轮分数，退回可继续对话的状态。
   * 沙箱与模型已做的改动都保留，改完重新校验会记作新一轮结果。
   */
  async function doReopen() {
    const s = store.getState();
    if (!s.run || s.busy) return;
    const report = s.run.report || {};
    const shownScore = report.score === undefined || report.score === null ? '—' : report.score;
    const ok = await confirmDialog({
      title: S.GRADE_REOPEN_TITLE,
      messages: [t(S.GRADE_REOPEN_BODY, { n: shownScore })],
      confirmLabel: S.GRADE_REOPEN,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    patch({ busy: 'grade', elapsed: 0 });
    try {
      const res = await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/reopen`, {}, { scope });
      showToast({ message: S.GRADE_REOPEN_TITLE, detail: res.notice || '', kind: 'ok', duration: 7000 });
      await loadRun(s.run.run_id);
      patch({ busy: '', elapsed: 0 });
    } catch (err) {
      reportError(err, '继续对话');
    }
  }

  /**
   * 结束本轮：服务端先把这轮成绩写进台账，再真删整条记录。
   *
   * 结束之后工作台回到干净空态——这是刻意的：一条记录对应一次尝试，
   * 尝试已经结清，剩下的事（换档案、换题）都是新一轮，不该挂在旧记录上继续。
   */
  async function doFinish() {
    const s = store.getState();
    if (!s.run || s.busy) return;
    const runId = s.run.run_id;
    // 没有可记的成绩要在确认前就说清楚：点了「结束本轮」却发现数字没进榜，
    // 是最容易踩的一脚坑。
    let body = T.FINISH_BODY;
    if (s.run.revealed || s.revealed) {
      body = T.FINISH_BODY_REVEALED;
    } else if (!hasCountedRound(s.run)) {
      body = T.FINISH_BODY_NO_SCORE;
    }
    const ok = await confirmDialog({
      title: T.FINISH_TITLE,
      messages: [body],
      confirmLabel: T.FINISH,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    patch({ busy: 'finish', elapsed: 0 });
    const hadRunInUrl = Boolean(urlRunId);
    // 只有网络调用进 try：删除已经落盘成功，收尾步骤出岔子也不能报成「结束失败」。
    let res = null;
    try {
      res = await api.post(`/runs/${encodeURIComponent(runId)}/finish`, {}, { scope });
    } catch (err) {
      reportError(err, '结束本轮');
      patch({ busy: '', elapsed: 0 });
      return;
    }
    wsStore.remove(runKey(s.modelId));
    poller.stop();
    patch({ run: null, revealed: null, busy: '', elapsed: 0, modelMismatch: false });
    urlRunId = '';
    if (hadRunInUrl && navigate) navigate('workspace', { taskId, region: 'chat' }, { replace: true });
    const ledgered = !!(res && res.ledgered);
    announce(ledgered ? T.FINISHED_LEDGERED : T.FINISHED_PLAIN);
    showToast({
      message: ledgered ? T.FINISHED_LEDGERED : T.FINISHED_PLAIN,
      detail: (res && res.notice) || '',
      kind: 'success',
      duration: 8000,
    });
  }

  /**
   * 这一轮有没有能进台账的成绩：作废轮与判无效轮都不算。
   * 与服务端 record_run_result 同一套判据，避免确认框与实际写入不一致。
   * @param {object} run
   * @returns {boolean}
   */
  function hasCountedRound(run) {
    if (run.revealed) return false;
    return (run.rounds || []).some((rnd) => rnd && !rnd.voided && !rnd.invalidated);
  }

  /**
   * 废弃这一轮：真删记录、对话与沙箱，**不进台账**，不留任何痕迹。
   *
   * 与「结束本轮」的唯一区别就是进不进台账：跑错了不想让这次进榜时用它。
   * 磁盘后果完全一样（都是真删），台账是唯一能区分两者的痕迹。
   */
  async function doDiscard() {
    const s = store.getState();
    if (!s.run || s.busy) return;
    const runId = s.run.run_id;
    const ok = await confirmDialog({
      title: T.DISCARD_TITLE,
      messages: [T.DISCARD_BODY, `将删除：${runId}`],
      confirmLabel: T.M_DISCARD,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    patch({ busy: 'discard', elapsed: 0 });
    // 地址栏里那个 run_id 马上就要指向一条不存在的记录，删成之后把它摘掉。
    const hadRunInUrl = Boolean(urlRunId);
    // 只有网络调用进 try：删除已经落盘了，收尾步骤出岔子也不该报成「删除失败」，
    // 那会把一次成功的真删说成没删。
    try {
      await api.del(`/runs/${encodeURIComponent(runId)}`, { scope });
    } catch (err) {
      reportError(err, '废弃本轮');
      return;
    }
    wsStore.remove(runKey(s.modelId));
    poller.stop();
    patch({ run: null, revealed: null, busy: '', elapsed: 0, modelMismatch: false });
    urlRunId = '';
    if (hadRunInUrl && navigate) navigate('workspace', { taskId, region: 'chat' }, { replace: true });
    announce(T.DISCARDED);
    showToast({ message: T.DISCARDED, detail: runId, kind: 'success', duration: 6000 });
  }

  /**
   * 用现存档案为这道题重开一轮（对话流在「档案已删除」时给出的出口）。
   *
   * 不改写旧记录的 model 归属：run_id 与 runs/<任务>/<档案>/ 目录名里都带着档案名，
   * 改了就会让记录躺在死档案下却被算成活档案的成绩。旧记录交给「继续对话（本轮分数作废）」处理。
   */
  async function doRestartWithModel(preferredId) {
    const s = store.getState();
    const modelId = String(preferredId || s.modelId || '');
    if (!modelId) {
      showToast({ message: S.RUN_MODEL_REQUIRED, kind: 'warn', duration: 5000 });
      modelField.focus();
      return;
    }
    if (s.busy) return;
    const ok = await confirmDialog({
      title: T.RESTART_TITLE,
      messages: [T.RESTART_BODY],
      confirmLabel: S.SANDBOX_PREPARE,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
    });
    if (!ok) return;
    patch({ busy: 'prepare', error: null, elapsed: 0 });
    announce(S.ANNOUNCE_SANDBOX_PREPARING);
    try {
      const res = await api.longPost('/runs', { task: taskId, model: modelId, attempt: 1 }, { scope });
      storage.set('last-task', taskId);
      rememberModelRun(modelId, res.run_id);
      await loadRun(res.run_id);
      await refreshTask(res.run_id);
      patch({ busy: '', elapsed: 0 });
      showToast({ message: T.RESTART_DONE, detail: res.sandbox || '', kind: 'success', duration: 8000 });
    } catch (err) {
      reportError(err, '重开一轮');
    }
  }

  /**
   * 查看参考解（必须写明「已揭晓、不计入通过率统计」，§13.3）。
   */
  async function doReveal() {
    const s = store.getState();
    if (!s.run) return;
    if (s.busy) return;
    const ok = await confirmDialog({
      title: S.GRADE_REVEAL_TITLE,
      messages: [S.GRADE_REVEAL_BODY, S.GRADE_REVEAL_WARN_1, S.GRADE_REVEAL_WARN_2, S.GRADE_REVEAL_WARN_3],
      confirmLabel: S.GRADE_REVEAL,
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
    });
    if (!ok) return;
    patch({ busy: 'grade', elapsed: 0 });
    try {
      const res = await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/reveal`, {}, { scope });
      patch({ revealed: { patch: res.patch || '', notice: res.notice || '' }, busy: '' });
      await loadRun(s.run.run_id);
      showToast({ message: S.GRADE_REVEAL_DONE, kind: 'warn', duration: 8000 });
      // 参考解正文随校验报告住在独立窗口里：这次点击要的就是它，直接把窗口呈上来
      // （这是用户主动动作的即时结果，不是校验完成那种被动事件，不违反 §13.2）。
      openReport();
    } catch (err) {
      reportError(err, '查看参考解');
    }
  }

  /**
   * 从运行记录里回读已揭晓的参考解（2026-10-02，原问题 8）。
   *
   * 以前补丁只在揭晓那一次下发给前端，刷新页面就没了。现在服务端把参考解写进
   * 运行记录的 revealed.patch，run_view 每次都带上 revealed_patch，所以复盘时
   * 报告窗里还能再看到它。
   * @param {object} run run_view 快照
   * @returns {boolean} 拿到了参考解正文
   */
  function restoreRevealed(run) {
    if (!run || !run.revealed) return false;
    const text = String(run.revealed_patch || '');
    if (!text) return false;
    patch({ revealed: { patch: text, notice: S.WS_REVEALED_NOTE } });
    return true;
  }

  /**
   * 导出报告。
   * 契约缺口：后端没有 GET /api/runs/{id}/report，报告本来就随 run_view 一起回来，
   * 所以直接用已经拿到的 run.report 在本机生成 JSON（见 NOTES.md）。
   */
  async function doExport() {
    const s = store.getState();
    if (!s.run || !s.run.report) return;
    const run = s.run;
    const content = JSON.stringify(
      {
        run_id: run.run_id,
        task: run.task,
        model: run.model,
        attempt: run.attempt,
        attempts_allowed: run.attempts_allowed,
        revealed: run.revealed,
        note: run.note,
        baseline_digest: run.baseline_digest,
        sandbox: run.sandbox,
        drive: run.drive,
        created_at: run.created_at,
        report: run.report,
      },
      null,
      2,
    );
    try {
      api.download(`${run.run_id}-report.json`, content, 'application/json');
      showToast({ message: S.GRADE_EXPORT, kind: 'success', duration: 4000 });
    } catch (err) {
      reportError(err, '导出报告');
    }
  }

  /** 报告里 diff 统计的一句话（挂改动查看器的角标）。 */
  function diffStats(run) {
    const d = run && run.report && run.report.diff;
    if (!d) return '';
    return `${t(S.RUN_DIFF_FILES, { n: d.files || 0 })} · ${t(S.RUN_DIFF_ADD, { n: d.added_lines || 0 })} · ${t(S.RUN_DIFF_DEL, { n: d.removed_lines || 0 })}`;
  }

  /**
   * 改动正文归属的「运行 × 轮次」。diff.patch 是评分产物，进下一轮后服务端给的是
   * 新一轮的改动，旧正文留在面板上就会被读成「模型这一轮什么也没改 / 又改了同样的东西」。
   */
  let diffToken = '';

  /** @param {object|null} run @returns {string} */
  function diffTokenOf(run) {
    return run ? `${run.run_id}#${run.attempt}` : '';
  }

  /** 换轮或换 run 时作废已显示的改动正文与在途请求。 */
  function invalidateDiff() {
    diffToken = '';
    setText(diffText, '');
    diffCard.update({ hint: '' });
    diffWrap.hidden = true;
    if (store.getState().diffOpen) patch({ diffOpen: false });
  }

  /**
   * 菜单「查看改动」：开合改动正文。展开时按需拉取一次（不轮询）。
   */
  function toggleDiff() {
    const s = store.getState();
    if (!s.run) return;
    const open = !s.diffOpen;
    patch({ diffOpen: open });
    diffWrap.hidden = !open;
    diffCard.setOpen(true);
    if (open) doShowDiff();
  }

  /**
   * 拉取本轮改动正文（POST /api/runs/{id}/diff）。
   * @returns {Promise<void>}
   */
  async function doShowDiff() {
    const s = store.getState();
    if (!s.run) return;
    const token = diffTokenOf(s.run);
    diffToken = token;
    setText(diffText, S.RUN_DIFF_LOADING);
    diffCard.update({ hint: S.STATE_LOADING });
    try {
      const res = await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/diff`, {}, { scope });
      // 换轮 / 换 run 之后这份正文不再属于当前视图，迟到的响应必须丢掉
      if (diffToken !== token) return;
      // 取不到正文和「真的没有改动」是两件事，混在一起就会把故障说成模型没动手
      if (typeof res.diff !== 'string') {
        setText(diffText, S.RUN_DIFF_BAD_PAYLOAD);
        diffCard.update({ hint: '' });
        return;
      }
      setText(diffText, res.diff || S.RUN_DIFF_EMPTY);
      diffCard.update({ hint: diffStats(s.run) });
    } catch (err) {
      if (diffToken !== token) return;
      const code = err instanceof ApiError ? err.code : 'INTERNAL';
      setText(diffText, errorBody(code));
      diffCard.update({ hint: errorTitle(code) });
    }
  }

  /**
   * 复制沙箱路径。
   * 契约缺口：后端没有「打开目录」接口，所以不做注定 404 的请求，
   * 改为「复制路径 + 说明怎么手动打开」（见 NOTES.md）。
   */
  function doOpenDir() {
    const s = store.getState();
    const path = s.run ? s.run.sandbox : '';
    if (!path) return;
    showToast({
      message: `${S.SANDBOX_PATH_LABEL}：${path}`,
      detail: S.SANDBOX_OPEN_DIR_HINT,
      kind: 'warn',
      duration: 10000,
    });
    void runDetails.copyPath(path);
  }

  /**
   * 保存备注。
   * 契约：POST /api/runs/{id}/note，请求体字段是 {note}（单数）。
   * @param {string} note
   */
  async function saveNote(note) {
    const s = store.getState();
    if (!s.run) return;
    patch({ busy: 'notes' });
    try {
      await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/note`, { note: String(note || '') }, { scope });
      await loadRun(s.run.run_id);
      patch({ busy: '' });
      showToast({ message: S.RUN_NOTES_SAVED, kind: 'success', duration: 3000 });
    } catch (err) {
      reportError(err, '保存备注');
    }
  }

  /**
   * 切换轮次（只切查看，不动记录）。
   * @param {number} n
   */
  function doRoundChange(n) {
    const s = store.getState();
    if (!s.task) return;
    if (!unlockedLevels(s).includes(n)) {
      showToast({
        message: t(S.PROMPT_ROUND_LOCKED_TITLE, { n, prev: Math.max(1, n - 1) }),
        detail: t(S.PROMPT_ROUND_LOCKED_DESC, { n, prev: Math.max(1, n - 1) }),
        kind: 'warn',
        duration: 6000,
      });
      return;
    }
    patch({ round: n });
    wsStore.set('round', n);
    announce(t(S.ANNOUNCE_ROUND_CHANGED, { n }));
  }

  /**
   * 焦点跳到某个区域并把区域记进 hash（§13.5 刷新回到同一区域）。
   *
   * 地址栏只是「记录在哪一区」的书签：main.js 对同一任务 + 同一视图的身份变化
   * 做就地跳转，不再销毁重建视图。这里的 navigate 由 lastUrlRegion 兜一层，
   * 避免「写 hash → 路由回调 focusRegion → 又写 hash」的原地打转。
   *
   * 2026-10-02 四改：`sandbox`（运行详情）与 `run`（本轮备注）不再是对话流里的折叠
   * 节点，而是状态栏图标点开的小窗口——跳这两个区域 = 把小窗口开起来（区域锚点
   * 仍挂在窗口正文根上，书签语义不变）。
   * @param {'prompt'|'chat'|'sandbox'|'grade'|'run'} region
   */
  function focusRegion(region) {
    if (region === 'sandbox' || region === 'run') {
      const handle = region === 'sandbox' ? runDetails.openDetails() : runDetails.openNotes();
      if (handle) {
        wsStore.set('region', region);
        if (region !== lastUrlRegion) {
          lastUrlRegion = region;
          if (navigate) navigate('workspace', { taskId, region }, { replace: true });
        }
        return;
      }
      // 没有运行记录时窗口开不出来：退回对话区，别把人留在一个什么都没发生的位置
    }
    const node = root.querySelector(`#ws-region-${region}`);
    if (!node) return;
    // 折叠节点先展开再跳，别把人滚到一个关着的节点上
    if (region === 'prompt') taskNode.setOpen(true);
    const target = node.querySelector('h1, h2, summary') || node;
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
    scrollBelowStickyHeader(node);
    wsStore.set('region', region);
    if (region === lastUrlRegion) return;
    lastUrlRegion = region;
    const activeRunId = store.getState().run && store.getState().run.run_id;
    const params = { taskId, region };
    if (region === 'chat' && (activeRunId || urlRunId)) params.runId = activeRunId || urlRunId;
    if (navigate) navigate('workspace', params, { replace: true });
  }

  // 任务节点开合由用户掌控：手动开合记进 wsStore，轮询不覆盖
  offHandlers.push(
    on(root, 'toggle', (event) => {
      if (event.target && event.target.id === 'ws-region-prompt') {
        wsStore.set('taskOpen', event.target.open ? 'open' : 'closed');
      }
    }, { capture: true }),
  );

  // ==================== 快捷键（§13.4） ====================
  /**
   * 单键快捷键：焦点在输入控件里失效；Ctrl/Cmd 组合一律放行。
   * @param {KeyboardEvent} event
   */
  function onKeydown(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (isEditableTarget(event.target)) return; // §11.2 #11
    if (event.key === '1' || event.key === '2' || event.key === '3') {
      const n = Number(event.key);
      const s = store.getState();
      if (s.task && unlockedLevels(s).includes(n)) {
        event.preventDefault();
        doRoundChange(n);
      }
      return;
    }
    const key = event.key.toLowerCase();
    const s = store.getState();
    if (key === 'c') {
      if (!s.run) return;
      event.preventDefault();
      taskNode.copyPrompt();
    } else if (key === 'g') {
      if (!s.run || s.busy) return;
      event.preventDefault();
      doGrade();
    } else if (key === 'r') {
      if (!s.run) return;
      event.preventDefault();
      doReset();
    }
  }
  offHandlers.push(on(document, 'keydown', onKeydown));

  // ==================== 滚动位置（打开即最新） ====================
  // 2026-10-02 三改：工作台的滚动发生在对话列自己的 .ws-scroll 里，不再整页滚动。
  // 用户口径是「重新打开页面要落在最新一条上，而不是顶部」，所以这里不再恢复
  // 上次的像素偏移：有消息就直接落到最新（在途的历史由 chat-stream 拉完后自己跟），
  // 翻历史是本次会话内的事，刷新后回到最新才是期望行为。

  // ==================== 启动 ====================
  /**
   * 载入任务详情（meta + 已解锁提示词）。
   *
   * **一律带上当前 run_id**：服务端按它算「已解锁到第几级」。不带 run_id 时它会拿
   * 这道题最新的一条记录去算，重建 / 进下一轮之后就可能读到另一条记录的级数。
   * @param {string} runId
   * @returns {Promise<object>}
   */
  async function loadTask(runId = '') {
    const query = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
    const task = await api.get(`/tasks/${encodeURIComponent(taskId)}${query}`, { scope });
    patch({ task, taskError: '' });
    taskRetryCount = 0;
    return task;
  }

  /**
   * 容错版读题面：失败不抛，把原因落进 store（任务节点显示「读取失败 + 重试」，
   * 发送按钮禁用并把原因写在按钮上），不留「点了没反应」的哑按钮。
   * @param {string} runId
   * @returns {Promise<object|null>}
   */
  async function refreshTask(runId = '') {
    try {
      return await loadTask(runId);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ABORTED') return null;
      const code = err instanceof ApiError ? err.code : 'INTERNAL';
      patch({ taskError: code });
      if (typeof console !== 'undefined' && typeof console.warn === 'function') {
        console.warn(`[workspace] 读任务详情失败：${code}`);
      }
      return null;
    }
  }

  /**
   * 首屏加载：任务详情 → 上次的 run_id。
   * @returns {Promise<void>}
   */
  async function load() {
    patch({ loading: true, error: null, taskError: '' });
    const state0 = store.getState();
    const lastRunId = urlRunId || recallModelRun(state0.modelId);
    let task = null;
    try {
      task = await loadTask(lastRunId);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ABORTED') {
        // 在途请求被取消（切视图 / 重新载入）：不能把 loading 留在 true，
        // 否则工作台永久停在「正在载入…」，所有按钮都不可用且没有原因。
        patch({ loading: false });
        return;
      }
      // 陈旧书签（那条记录已经被删）不该连题面一起赔进去：摘掉这个 run_id 再读一次。
      // 以前这里整段退出，页面就变成「有 run、没有提示词」——用户报的现象之一。
      if (lastRunId && err instanceof ApiError && err.code === 'NO_RUN') {
        wsStore.remove(runKey(state0.modelId));
        try {
          task = await loadTask('');
        } catch {
          task = null;
        }
      }
      if (!task) {
        const code = err instanceof ApiError ? err.code : 'INTERNAL';
        patch({ loading: false, error: { code }, taskError: code });
        reportError(err, '载入任务');
        return;
      }
    }
    const taskRound = task.run && Number(task.run.attempt);
    const round = taskRound > 0 ? taskRound : Number(wsStore.get('round', 1)) || 1;
    patch({ loading: false, round });
    if (taskRound > 0) wsStore.set('round', round);

    // 这一任务是否已有运行记录：有就接上，没有就显示空态（等用户点「准备沙箱」）。
    // 记录从题面回读里取（题面就是按这条记录读的），取不到才回落到本地记住的那个。
    const runId = (task.run && task.run.run_id) || lastRunId;
    if (runId) {
      try {
        await loadRun(runId);
        ensureTicker();
        if (store.getState().run && BUSY_STATUS.has(store.getState().run.status)) poller.start();
      } catch {
        wsStore.remove(runKey(store.getState().modelId));
      }
    }
  }

  load();

  // 初始 dock / 状态栏（store 订阅在首次 patch 前就要有一版界面）
  renderStatusBar(store.getState());
  renderDock(store.getState());

  // ==================== 对外 ====================
  return {
    el: root,
    /** 视图标题，供 router 聚焦（§12.1 每视图一个 h1）。 */
    el_h1: h1,
    /** 跳到指定区域。 */
    focusRegion,
    /** 记住这一轮 run_id（按档案分键），刷新后能接上同一条时间线。 */
    rememberRun(runId) {
      const s = store.getState();
      if (runId) rememberModelRun(s.modelId, runId);
    },
    /**
     * 打开工作台时落在最新一条消息上（§13.5 刷新的新口径：对话流回到最新，
     * 而不是回到上次的像素位置——旧行为是刷新后停在顶部，要手动往下拽）。
     * @returns {boolean} 有没有落到最新
     */
    restoreScroll() {
      const ok = chatStream.scrollToLatest();
      if (!ok) scrollBox.scrollTop = 0;
      return ok;
    },
    /** 导出给快捷键 / 外部调用的动作集合。 */
    actions: { doGrade, doReset, doPrepare, doRoundChange, focusRegion },
    /**
     * 销毁：停表 + 清心跳 + 取消在途请求 + 解绑事件（§10.4 / §11.2 #2）。
     */
    destroy() {
      offHandlers.forEach((off) => off());
      offHandlers.length = 0;
      poller.destroy();
      if (tickTimer) {
        window.clearInterval(tickTimer);
        tickTimer = null;
      }
      scope.cancelAll();
      taskNode.destroy();
      chatStream.destroy();
      reportNode.destroy();
      runDetails.destroy();
      dock.destroy();
      modelField.destroy();
      modelRetryBtn.destroy();
      statusDot.destroy();
      diffCard.destroy();
    },
  };
}
