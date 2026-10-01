/**
 * workspace.js — 工作台视图（§9 / §13）
 *
 * 职责：
 *   1. 题头：任务 ID、档位、尝试指示灯 ①②③、沙箱盘符、基线指纹。
 *   2. 四个区域（prompt / sandbox / grade / run）按 §10.5 区域渲染：轮询只 patch 变化的 region。
 *   3. 编排全部后端动作：准备 / 清空 / 重建 / 校验 / 下一轮 / 揭晓 / 导出 / 备注 / 看 diff。
 *   4. 快捷键 C / G / R / 1 / 2 / 3（/? 与 Esc 属于全局，在 main.js）。
 *   5. 状态持久化：上次查看轮次、每任务的 run_id 与滚动位置（§13.5）。
 *   6. 轮询：只在服务端说「进行中」时轮询；页面隐藏自动暂停（core/poller）。
 *
 * 生命周期（§10.4）：createWorkspace(props) → { el, destroy, focusRegion, ... }
 *   - destroy：解绑事件、停表、清心跳、取消在途请求
 *
 * 依赖：core/*、components/*、views/workspace/*
 * 导出：createWorkspace(props)
 *
 * ── 契约要点（对照 console/server.py 与 harness/runs.py）────────────────
 *   POST /api/runs                     同步，阻塞到沙箱铺好为止 → 用 api.longPost
 *   GET  /api/runs/{id}                run_view：sandbox/drive/baseline_digest 都是**字符串**
 *   POST /api/runs/{id}/grade          异步，立刻返回 {status:'grading'} → 靠轮询看结果
 *   POST /api/runs/{id}/promote        {run_id, attempt, can_promote}
 *   POST /api/runs/{id}/reveal         {run_id, patch, notice}
 *   POST /api/runs/{id}/note           请求体 {note}（单数，路径也单数）
 *   POST /api/runs/{id}/diff           {diff: 补丁正文}
 *   POST /api/sandbox/reset            同步，返回 {seconds, cleaned, ...}
 *   POST /api/sandbox/rebuild          同步，返回 {run_id, sandbox, drive}
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
  pageScrollTop,
  revealIfCoveredByStickyTop,
  scrollBelowStickyHeader,
  setPageScroll,
} from '../core/a11y.js';
import { tierBadge } from '../components/badge.js';
import { createStatusDot } from '../components/status-dot.js';
import { confirmDialog } from '../components/confirm-dialog.js';
import { showToast } from '../components/toast.js';
import { createButton } from '../components/button.js';
import { createPromptPanel } from './workspace/prompt-panel.js';
import { createChatPanel } from './workspace/chat-panel.js';
import { createSandboxPanel } from './workspace/sandbox-panel.js';
import { createGradePanel } from './workspace/grade-panel.js';
import { createRunBar } from './workspace/run-bar.js';

/**
 * 服务端「还在忙」的运行状态：只有这些状态才轮询（§13.6）。
 * 清空 / 重建是同步请求，忙在本地 busy 上，不进这个集合。
 */
const BUSY_STATUS = new Set(['preparing', 'grading', 'queued']);

/** 沙箱可用（模型可以动手 / 可以校验）的状态。 */
const SANDBOX_OK = new Set(['ready', 'graded']);

/** 服务端 status → 中文状态词。 */
const STATUS_TEXT = {
  preparing: () => S.RUN_STATUS_PREPARING,
  ready: () => S.RUN_STATUS_READY,
  grading: () => S.RUN_STATUS_GRADING,
  graded: () => S.RUN_STATUS_GRADED,
  queued: () => S.RUN_STATUS_QUEUED,
  error: () => S.RUN_STATUS_ERROR,
};

/** 心跳间隔（毫秒）：驱动长操作的「已用时间」。 */
const TICK_MS = 1000;

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
    /** 本机长操作：prepare / reset / rebuild / grade / notes */
    busy: '',
    error: null,
    /** 沙箱区日志：本机真实发过的每一步（后端不提供沙箱日志，见 NOTES.md） */
    opLog: [],
    /** 当前长操作已用毫秒（心跳累加） */
    elapsed: 0,
    newResult: false,
    /** 查看参考解的返回：{patch, notice} */
    revealed: null,
  });

  const wsStore = storage.scoped('ws', taskId);
  const offHandlers = [];
  /** 心跳句柄：有长操作时才存在 */
  let tickTimer = null;
  /** 已经写进地址栏的区域：跳转回来时不再重复改 hash，避免「路由 → 跳转 → 路由」打转。 */
  let lastUrlRegion = '';

  // 模型档案是异步到达的：首屏可能晚于本视图、模型页改完会再推一次、启动那次请求
  // 可能整个失败。只吃创建时的快照会让下拉永久停在「尚未选择档案」且没有原因，
  // 所以像 main.js 订阅 lastTask 那样订阅档案（退订交给 destroy，§10.4）。
  if (typeof subscribeModels === 'function') {
    offHandlers.push(
      subscribeModels((list, code) => {
        patch({ models: Array.isArray(list) ? list : [], modelsError: code || '' });
      }),
    );
  }

  // ==================== 题头 ====================
  const h1 = el('h1', { tabindex: '-1' });
  const idBadge = el('span', { class: 'task-card__id' }, taskId);
  const tierHost = el('span');
  const attemptsHost = el('span', { class: 'ws-attempts' });
  const headFacts = el('div', { class: 'ws-head__facts' });
  const driveFact = el('span', { class: 'ws-fact__value' }, S.WS_NO_DRIVE);
  const hashFact = el('span', { class: 'ws-fact__value' }, '—');
  const connDot = createStatusDot({ kind: 'busy', text: S.STATE_LOADING });
  // 五个跳转原先是五个同权重赤陶幽灵按钮平铺一行，看着像坏掉的导航。
  // 收成分段控件：整体视觉权重低于面板标题，并带一个"跳转到"前缀标签。
  const jumpRow = el(
    'div',
    { class: 'ws-jumps', role: 'group', 'aria-label': S.WS_JUMP_LABEL },
    el('span', { class: 'ws-jumps__label' }, S.WS_JUMP_LABEL),
    createButton({ label: S.WS_JUMP_PROMPT, size: 'sm', variant: 'ghost', onClick: () => focusRegion('prompt') }).el,
    createButton({ label: S.WS_JUMP_CHAT, size: 'sm', variant: 'ghost', onClick: () => focusRegion('chat') }).el,
    createButton({ label: S.WS_JUMP_SANDBOX, size: 'sm', variant: 'ghost', onClick: () => focusRegion('sandbox') }).el,
    createButton({ label: S.WS_JUMP_GRADE, size: 'sm', variant: 'ghost', onClick: () => focusRegion('grade') }).el,
    createButton({ label: S.WS_JUMP_RUN, size: 'sm', variant: 'ghost', onClick: () => focusRegion('run') }).el,
  );

  const head = el(
    'header',
    { class: 'ws-head' },
    el('div', { class: 'ws-head__main' },
      h1,
      el('div', { class: 'ws-head__meta' }, idBadge, tierHost, attemptsHost, connDot.el),
    ),
    headFacts,
    jumpRow,
  );
  headFacts.appendChild(
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.WS_DRIVE_LABEL), driveFact),
  );
  headFacts.appendChild(
    el('div', { class: 'ws-fact' }, el('span', { class: 'ws-fact__label' }, S.WS_BASELINE_LABEL), hashFact),
  );

  // ==================== 四个区域 ====================
  /**
   * 只读回读：任务详情 + 当前运行记录。
   * 「重试」按钮只做这件事——绝不能顺手触发准备 / 校验这类写操作，
   * 否则一次读取失败后的重试会悄悄开出新的一轮。
   * @returns {Promise<void>}
   */
  function reloadState() {
    return load();
  }

  /** @type {{onPrepare: Function, onReset: Function, onRebuild: Function, onOpenDir: Function, onCopyPath: Function, onReload: Function}} */
  const sandboxHandlers = {
    onPrepare: () => doPrepare(),
    onReset: () => doReset(),
    onRebuild: () => doRebuild(),
    onOpenDir: () => doOpenDir(),
    onCopyPath: () => doCopyPath(),
    onReload: () => reloadState(),
  };
  /** @type {{onRoundChange: Function, onGoSandbox: Function, onReload: Function}} */
  const promptHandlers = {
    onRoundChange: (n) => doRoundChange(n),
    onGoSandbox: () => focusRegion('sandbox'),
    onReload: () => reloadState(),
  };
  /** @type {{onGrade: Function, onPromote: Function, onReveal: Function, onExport: Function, onGoPrompt: Function, onReload: Function}} */
  const gradeHandlers = {
    onGrade: () => doGrade(),
    onPromote: () => doPromote(),
    onReveal: () => doReveal(),
    onExport: () => doExport(),
    onGoPrompt: () => focusRegion('prompt'),
    onReload: () => reloadState(),
  };
  /** @type {{onModelChange: Function, onNotesSave: Function, onShowDiff: Function, onReloadModels: Function}} */
  const runHandlers = {
    onModelChange: (id) => {
      store.setState({ modelId: id });
      storage.set('last-model', id);
    },
    onNotesSave: (note) => saveNote(note),
    onShowDiff: () => doShowDiff(),
    // 档案读取失败后由「本轮信息」区给一个重读按钮：只 GET /api/models，不写任何东西
    onReloadModels: () => (typeof reloadModels === 'function' ? reloadModels() : reloadState()),
  };

  const promptPanel = createPromptPanel(promptHandlers);
  const chatPanel = createChatPanel({
    scope,
    onUsePrompt: () => chatPanel.sendText(promptPanel.getPrompt()),
  });
  const sandboxPanel = createSandboxPanel(sandboxHandlers);
  const gradePanel = createGradePanel(gradeHandlers);
  const runBar = createRunBar(runHandlers);

  const regions = el('div', { class: 'ws-regions' }, promptPanel.el, sandboxPanel.el, chatPanel.el, gradePanel.el, runBar.el);
  const root = el('div', { class: 'view ws' }, head, regions);

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
      return Boolean(s.run) && BUSY_STATUS.has(s.run.status);
    },
    onError: (err, times) => {
      if (err instanceof ApiError && err.code === 'OFFLINE') {
        connDot.update({ kind: 'error', text: S.STATE_OFFLINE });
        if (times === 1) {
          showToast({ message: errorTitle('OFFLINE'), detail: errorBody('OFFLINE'), kind: 'error', duration: 8000 });
        }
      } else if (err instanceof ApiError && err.code !== 'ABORTED') {
        connDot.update({ kind: 'warn', text: errorTitle(err.code) });
      }
    },
  });

  // ==================== 状态订阅（区域级，§10.5） ====================
  offHandlers.push(
    store.subscribe(null, (next, prev) => {
      renderHead(next);

      const runChanged = next.run !== prev.run;
      const busyChanged = next.busy !== prev.busy;
      const loadingChanged = next.loading !== prev.loading;
      const errorChanged = next.error !== prev.error;
      const revealedChanged = next.revealed !== prev.revealed;
      const newResultChanged = next.newResult !== prev.newResult;
      const modelChanged = next.modelId !== prev.modelId
        || next.models !== prev.models
        || next.modelsError !== prev.modelsError;
      // 心跳只推进进度数字：不重建任何输入控件（§11.2 #14）
      const tickOnly = next.elapsed !== prev.elapsed
        && !runChanged && !busyChanged && !loadingChanged && !errorChanged
        && !revealedChanged && !newResultChanged && !modelChanged;

      if (loadingChanged || errorChanged) {
        promptPanel.update({ loading: next.loading, error: next.error, run: next.run, task: next.task, round: next.round });
        chatPanel.update({ run: next.run });
        sandboxPanel.update({
          loading: next.loading, error: next.error, run: next.run, busy: next.busy,
          opLog: next.opLog, elapsed: next.elapsed,
        });
        gradePanel.update({
          loading: next.loading, error: next.error, run: next.run, busy: next.busy,
          elapsed: next.elapsed, newResult: next.newResult, revealed: next.revealed,
        });
        runBar.update({
          loading: next.loading, run: next.run, models: next.models, modelId: next.modelId,
          modelsError: next.modelsError, busy: next.busy,
        });
        return;
      }

      if (tickOnly) {
        sandboxPanel.update({ elapsed: next.elapsed, busy: next.busy });
        gradePanel.update({ elapsed: next.elapsed, busy: next.busy, run: next.run });
        return;
      }

      if (runChanged || busyChanged) {
        if (runChanged) chatPanel.update({ run: next.run });
        sandboxPanel.update({
          run: next.run,
          busy: next.busy,
          opLog: next.opLog,
          elapsed: next.elapsed,
        });
        gradePanel.update({
          run: next.run,
          busy: next.busy,
          elapsed: next.elapsed,
          newResult: next.newResult,
          revealed: next.revealed,
        });
        // 档案在视图挂载之后才到达时，runBar 也要能拿到最新的列表与错误码
        runBar.update({ run: next.run, busy: next.busy, models: next.models, modelId: next.modelId, modelsError: next.modelsError });
      }
      if (runChanged || next.task !== prev.task || next.round !== prev.round) {
        promptPanel.update({ run: next.run, task: next.task, round: next.round });
      }
      if (modelChanged) {
        runBar.update({ models: next.models, modelId: next.modelId, modelsError: next.modelsError });
      }
      if (revealedChanged) {
        gradePanel.update({ revealed: next.revealed });
      }
      if (newResultChanged && next.newResult) {
        gradePanel.update({ newResult: true });
      }
    }),
  );

  /**
   * 渲染题头。
   * @param {object} next
   */
  function renderHead(next) {
    const task = next.task;
    setText(h1, task ? `${task.id} · ${task.title}` : taskId);
    setText(idBadge, taskId);
    tierHost.textContent = '';
    tierHost.appendChild(tierBadge(task ? task.tier : '', { attempts: task ? task.attempts : 0 }).el);

    // 尝试指示灯 ①②③：点形 + 数字 + 文字标题，不靠颜色单独承载语义
    attemptsHost.textContent = '';
    attemptsHost.setAttribute('aria-label', S.WS_ATTEMPTS_LABEL);
    const max = next.run ? next.run.attempts_allowed : task ? task.attempts : 1;
    const cur = next.run ? next.run.attempt : 0;
    const unlocked = unlockedLevels(next);
    for (let i = 1; i <= max; i += 1) {
      const stateName = i === cur ? 'current' : i < cur ? 'used' : unlocked.includes(i) ? 'unlocked' : 'locked';
      attemptsHost.appendChild(
        el('span', { class: 'ws-attempt', dataset: { state: stateName }, 'aria-hidden': 'true' }, String(i)),
      );
    }
    const attemptText = next.run
      ? t(S.WS_ATTEMPT_CURRENT, { n: next.run.attempt })
      : `${S.WS_ATTEMPTS_LABEL} ${cur}/${max}`;
    attemptsHost.setAttribute('title', attemptText);

    // run.sandbox / run.drive / run.baseline_digest 在契约里都是字符串（不是对象）
    setText(driveFact, (next.run && next.run.drive) || S.WS_NO_DRIVE);
    setText(hashFact, (next.run && next.run.baseline_digest) || '—');

    const run = next.run;
    // 状态点只在「真的在取数」时才算 busy：busy 会带上 pulse 动画
    // （components.css 的 .status-dot--busy），而「没有 run」是刚进工作台的
    // 正常空状态，跟加载没关系。原先写死 kind:'busy' 且从不看 loading，
    // 「正在加载」就永远挂着不消失。
    if (next.loading) connDot.update({ kind: 'busy', text: S.STATE_LOADING });
    else if (next.error) connDot.update({ kind: 'error', text: S.ERR_LOAD });
    else if (!run) connDot.update({ kind: 'idle', text: S.SANDBOX_NO_RUN, title: S.ERR_NO_RUN_BODY });
    else if (BUSY_STATUS.has(run.status)) connDot.update({ kind: 'busy', text: (STATUS_TEXT[run.status] || STATUS_TEXT.preparing)() });
    else if (run.status === 'error') connDot.update({ kind: 'error', text: S.RUN_STATUS_ERROR });
    else connDot.update({ kind: 'ok', text: (STATUS_TEXT[run.status] || STATUS_TEXT.ready)() });
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
      focusRegion('run');
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
      wsStore.set('run_id', runId);
      logOp(t(S.SANDBOX_PREPARE_DONE, { path: res.sandbox || '' }));
      await loadRun(runId);
      patch({ busy: '', elapsed: 0 });
    } catch (err) {
      reportError(err, '准备沙箱');
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
      applyRun(run);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'NO_RUN') {
        patch({ run: null, busy: '' });
        return;
      }
      throw err;
    }
  }

  /**
   * 把一次拉取结果写进 store（只做差异更新，§10.5），并处理阶段推进播报。
   * @param {object} run
   */
  function applyRun(run) {
    const prev = store.getState();
    const prevStatus = prev.run ? prev.run.status : '';
    const serverBusy = BUSY_STATUS.has(run.status);
    const next = { run, newResult: false };

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
      next.newResult = true; // 结果区出现「新结果」标记，不跳页不抢焦点
    } else if (prevStatus === 'grading' && run.status === 'error') {
      announce(S.ANNOUNCE_GRADE_FAILED);
    } else if (!prevStatus && run.status === 'ready') {
      announce(S.ANNOUNCE_SANDBOX_READY);
    }
    if (run.status === 'grading' && prevStatus !== 'grading') {
      announce(S.ANNOUNCE_GRADE_STARTED);
    }

    patch(next);
    wsStore.set('run_id', run.run_id);
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
      await loadTask();
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
   */
  async function doGrade() {
    const s = store.getState();
    if (!s.run) {
      showToast({ message: S.ERR_NO_SANDBOX, detail: S.ERR_NO_SANDBOX_BODY, kind: 'warn', duration: 6000 });
      focusRegion('sandbox');
      return;
    }
    if (s.busy) return;
    // 乐观状态要可回滚：POST 失败后 run.status 停在 'grading' 会让按钮永久禁用、
    // 心跳永久累加，且轮询只在成功路径启动，UI 就永远卡死。
    const runBefore = s.run;
    if (s.run.chat_busy) {
      showToast({ message: S.CHAT_REMOTE_BUSY, detail: S.CHAT_REMOTE_BUSY_DETAIL, kind: 'warn', duration: 8000 });
      focusRegion('chat');
      return;
    }
    patch({ busy: 'grade', newResult: false, elapsed: 0, run: { ...s.run, status: 'grading' } });
    announce(S.ANNOUNCE_GRADE_STARTED);
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
    const ok = await confirmDialog({
      title: t(S.CONFIRM_PROMOTE_TITLE, { n: nextLevel }),
      messages: [t(S.CONFIRM_PROMOTE_BODY_1, { n: nextLevel }), S.CONFIRM_PROMOTE_BODY_2],
      confirmLabel: t(S.GRADE_PROMOTE, { n: nextLevel }),
      cancelLabel: S.CONFIRM_DEFAULT_CANCEL,
      danger: true,
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
      await loadTask();
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
    } catch (err) {
      reportError(err, '查看参考解');
    }
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

  /**
   * 查看本轮改动正文（POST /api/runs/{id}/diff）。
   * @returns {Promise<void>}
   */
  async function doShowDiff() {
    const s = store.getState();
    if (!s.run) return;
    runBar.showDiff({ loading: true, text: '', error: '' });
    try {
      const res = await api.post(`/runs/${encodeURIComponent(s.run.run_id)}/diff`, {}, { scope });
      const text = typeof res.diff === 'string' ? res.diff : '';
      runBar.showDiff({ loading: false, text, error: '' });
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'INTERNAL';
      runBar.showDiff({ loading: false, text: '', error: errorBody(code) });
    }
  }

  /**
   * 打开沙箱目录。
   * 契约缺口：后端没有这个接口，所以不做注定 404 的请求，
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
    void doCopyPath();
  }

  /**
   * 复制沙箱路径（三级降级由 run-bar 的复制按钮负责，这里只做兜底播报）。
   */
  async function doCopyPath() {
    const s = store.getState();
    const path = s.run ? s.run.sandbox : '';
    if (!path) return;
    await runBar.copyPath(path);
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
   * @param {'prompt'|'chat'|'sandbox'|'grade'|'run'} region
   */
  function focusRegion(region) {
    const node = root.querySelector(`#ws-region-${region}`);
    if (!node) return;
    const target = node.querySelector('h2') || node;
    if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
    scrollBelowStickyHeader(node);
    wsStore.set('region', region);
    if (region === lastUrlRegion) return;
    lastUrlRegion = region;
    const activeRunId = store.getState().run && store.getState().run.run_id;
    const params = { taskId, region };
    if (region === 'chat' && (activeRunId || routeRunId)) params.runId = activeRunId || routeRunId;
    if (navigate) navigate('workspace', params, { replace: true });
  }

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
      promptPanel.copyPrompt();
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

  // ==================== 滚动位置持久化（§13.5） ====================
  // 外壳现在可能是「主内容区自己滚动」（.app-main 带 overflow），而 scroll 事件
  // 不冒泡。用捕获阶段挂在 document 上才能同时收到窗口滚动和容器滚动，
  // 否则刷新后「回到上次位置」会静默失效。
  offHandlers.push(
    on(
      document,
      'scroll',
      () => {
        window.requestAnimationFrame(() => {
          wsStore.set('scroll', pageScrollTop(root));
        });
      },
      { capture: true, passive: true },
    ),
  );

  // ==================== 启动 ====================
  /**
   * 载入任务详情（meta + 已解锁提示词）。
   * @returns {Promise<void>}
   */
  async function loadTask(runId = '') {
    const query = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
    const task = await api.get(`/tasks/${encodeURIComponent(taskId)}${query}`, { scope });
    patch({ task });
    return task;
  }

  /**
   * 首次加载：任务详情 → 上次的 run_id。
   * @returns {Promise<void>}
   */
  async function load() {
    patch({ loading: true, error: null });
    const lastRunId = routeRunId || wsStore.get('run_id', '');
    try {
      const task = await loadTask(lastRunId);
      const taskRound = task.run && Number(task.run.attempt);
      const round = taskRound > 0 ? taskRound : Number(wsStore.get('round', 1)) || 1;
      patch({ loading: false, round });
      if (taskRound > 0) wsStore.set('round', round);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'ABORTED') return;
      patch({ loading: false, error: { code: err.code || 'INTERNAL' } });
      reportError(err, '载入任务');
      return;
    }

    // 这一任务是否已有运行记录：有就接上，没有就显示空态（等用户点「准备沙箱」）
    if (lastRunId) {
      try {
        await loadRun(lastRunId);
        ensureTicker();
        if (store.getState().run && BUSY_STATUS.has(store.getState().run.status)) poller.start();
      } catch {
        wsStore.remove('run_id');
      }
    }
  }

  load();

  // ==================== 对外 ====================
  return {
    el: root,
    /** 视图标题，供 router 聚焦（§12.1 每视图一个 h1）。 */
    el_h1: h1,
    /** 跳到指定区域。 */
    focusRegion,
    /** 记住这一轮 run_id，刷新后能接上。 */
    rememberRun(runId) {
      if (runId) wsStore.set('run_id', runId);
    },
    /**
     * 恢复上次的滚动位置（§13.5）。
     * @returns {boolean} 有没有可恢复的位置
     */
    restoreScroll() {
      const y = Number(wsStore.get('scroll', 0)) || 0;
      if (y <= 0) return false;
      window.requestAnimationFrame(() => {
        setPageScroll(y, root);
        // 只有「标题被顶部粘性条压住」时才挪一下。桌面宽度 .app-header 是左侧粘性
        // 侧栏（height:100vh），拿它的 bottom 当遮挡高度会永远判定成被遮住，
        // 于是刚恢复好的滚动位置又被拽回标题处。
        revealIfCoveredByStickyTop(h1);
      });
      return true;
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
      promptPanel.destroy();
      chatPanel.destroy();
      sandboxPanel.destroy();
      gradePanel.destroy();
      runBar.destroy();
      connDot.destroy();
    },
  };
}
