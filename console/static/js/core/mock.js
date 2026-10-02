/**
 * mock.js — 开发用假传输层（仅 ?mock=1 时载入）
 *
 * 职责：在没有后端 server.py 的情况下，让前端能完整跑通所有界面与交互。
 * 依赖：无（刻意不 import 任何其它模块，保证「真实代码路径零改动」）。
 * 导出：transport（fetch 兼容的假函数）
 *
 * 工作方式：
 *   - 被 core/api.js 在检测到 ?mock=1 时动态 import，顶替 window.fetch。
 *   - 返回 `{ __mockResponse: true, data }`，api.js 见到标记就直接取 data。
 *   - 维护一份内存态（任务、运行记录、档案、成绩），并按真实耗时推进
 *     「准备沙箱 / 校验」的进度，用于验证进度条、日志、分组报告的渲染。
 *
 * 纪律：
 *   - 样例数据的**字段名与结构必须与 console/server.py + console/harness/* 完全一致**，
 *     否则 mock 模式通过、真实模式崩，等于没测。
 *   - 不实现、也不模拟的接口一律返回 404 + {code:'E_NOT_FOUND'}，用来验证前端错误路径。
 *   - 错误码用后端真实的 E_* 码，前端 core/strings.js 负责归一化。
 */

// ============================ 样例数据 ============================

const WIRING_NOTE = [
  '你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，',
  '它是唯一允许操作的位置，不要访问该盘之外的任何路径）。',
  '请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。',
].join('\n');

/** 任务 ID → 每一级提示词（与 packs 里 prompts.json 的结构一致）。 */
const PROMPTS = {
  'T1-01': {
    1: '现象一：推荐策略一个候选都筛不出来，页面上的候选列表永远是空的。\n'
      + '现象二：刚写进库的行，换手率显示全是 0%，但同一批数据里市值和 ST 标记又是正常的。\n\n'
      + '验收要求：修好之后，上面两处算出来必须和同一份数据里其它地方的口径保持一致。',
    2: '现在把"派生指标"这件事摊开说：换手率、市值、ST 标记各自被算了一遍，而且不是所有出口都用了同一遍的算法。\n'
      + '当前互相矛盾的地方有三处：行情适配层把换手率算成 0；选股条件注册表里同一个指标注册了两次；策略引擎的板块判定仍走老逻辑。\n\n'
      + '验收要求：把这几处收敛到同一份共享计算上，让所有出口读同一份结果。',
    3: '必须同时满足的不变量：\n'
      + '  1. 换手率 = 成交额 / 流通市值，流通市值为 0 或缺失时结果必须是"空"而不是 0；\n'
      + '  2. 指标在注册表里只允许注册一次，重复注册要报错而不是静默覆盖；\n'
      + '  3. 策略引擎判定板块时读注册表里那份派生值，不得自己再算一遍。\n\n'
      + '已被否决的思路（不要再提）：在策略引擎里单独补一段换手率计算；把 0 直接改成 None。',
  },
  'T2-04': {
    1: '现象一：覆盖表说某几天没有缺口，同步任务却每次都在重抓同一批三千只股票，第二天还是同样的结果。\n'
      + '现象二：某只当天根本没交易的票，缺口中位数被它拉高了，导致"没有缺口"的结论翻来覆去。\n\n'
      + '验收要求：覆盖表、同步阈值、单只股票的缺口明细、资金流这四个出口，对同一天的同一只股票必须给出同一个结论。',
    2: '把"缺口"这件事摊开说。现在系统里有四套各算各的口径：同步任务自己算阈值；零行交易日的票被当成正常票混进中位数；'
      + '资金流那条出口的豁免条件比覆盖表宽得多；快照那条出口读的是当天收盘的快照，取数时点不一致。\n\n'
      + '验收要求：抽一份共享语义出来，让四个出口都读它，而不是各修各的。',
    3: '必须同时满足的不变量：\n'
      + '  1. 缺口判定只有一份实现，阈值来自同一个配置入口；\n'
      + '  2. 当日零成交的票必须从缺口中位数里排除，且不能因此改变"当天是否有缺口"的结论；\n'
      + '  3. 资金流的豁免条件必须与覆盖表完全一致，不允许更宽；\n'
      + '  4. 四个出口取的是同一时点的快照，取数时点由一处决定。\n\n'
      + '已被否决的思路（不要再提）：把同步阈值改成硬编码常量；在每个出口外面套一层过滤。',
  },
  'T3-09': {
    1: '现象一：行情偶尔退回旧快照，而且退回之后不会自己恢复。\n'
      + '现象二：有一次慢请求之后，整条数据源链连续超时，后面全部拿不到数据。\n'
      + '现象三：前端在拿不到新快照时不会降级，界面一直空着。\n\n'
      + '日志摘录：\n[warn] snapshot worker 42 publish rejected: stale generation\n[error] source chain timeout after 3 attempts\n[info] ui: no snapshot received, keeping last known value = null',
    2: '把"实时快照"这条链摊开说：同一时刻只允许一个 worker 在拉，但 single-flight 的生命周期没有收口；'
      + '迟到的 worker 仍然可以发布结果，没有代际仲裁；前端在失败时不会降级重试。\n\n'
      + '验收要求：一次慢请求不能让整条链断掉，迟到结果必须被拦住。',
    3: '必须同时满足的不变量：\n'
      + '  1. single-flight 的锁必须在 worker 退出、异常、超时三条路径上都归还，且可重入；\n'
      + '  2. 每次拉取带代际号，发布时若代际已过期必须丢弃而不是覆盖；\n'
      + '  3. 一个数据源失败不得影响同链其它数据源；\n'
      + '  4. 前端必须有降级路径：有旧快照时用旧快照并标注数据时间。\n\n'
      + '已被否决的思路（不要再提）：把超时时间调长；发布前 sleep 一段再比时间戳。',
  },
  'T4-11': {
    1: '我遇到的问题\n\n这是一条同时服务桌面端和后台补数的本地数据链。最近并发变多后，表面上每个接口都还能返回，但同一件事实在不同出口给出了不同答案：\n\n1. 快速刷新时，刚出现的新行情会被更早发起、却更晚返回的旧结果盖回去；慢请求还会把后续刷新堵在后面。\n2. 请求超时后，迟到的碎片和诊断仍会混入下一次对外状态，部分成功也没有进入短周期探活。\n3. 两个补数进程同时写同一份库时，先写的行可能消失；写入越多越慢，统计和健康信息却继续显示旧数。\n4. 短暂的锁争用会让整批结果失败，重跑又成功，导致汇总数字和盘上真实内容无法对账。\n\n验收要求\n\n- 并发与乱序下，外部只看到最后一次成功的权威结果，旧结果、迟到片段不能越权；\n- 超时、取消、部分成功与后台工作结束的边界必须明确，通道不能排队堆积；\n- 两个真实进程写入后每一行都保留，写盘量与批次相称，不能平方放大；\n- 写入后的统计和健康状态立即反映同一份真实数据，短暂争用可自行恢复；\n- 不改测试，不添加绕过校验的开关，修根因并保持既有功能不回归。',
    2: '（第 2 级提示词——不一致清单）\n\n这不是一个单点故障，而是两条共享本地状态的链路各自保留了一份过时的事实：\n\n1. 行情通道把“外层等待结束”和“后台工作真正退出”当成同一个边界。前者先超时返回后，后续请求会以为通道空闲，实际却只能排在仍未结束的慢工作后面。\n2. 发布路径只在部分入口检查取消或截止时间，另一些入口仍把迟到的诊断和分块数据写入共享状态；页面又按常规周期轮询，错过了部分成功应有的加速探活。\n3. 快照是否更新有多套判据：有的只看数据时间，有的看完成顺序，有的看请求代际。乱序到达时，同一个结果可能在留存、接口和页面被判成不同的“最新”。\n4. 写入链的批次边界、分区现状、缓存失效和健康刷新各自维护。写入方若在内存里保存分区旧副本，另一进程的行会在整段覆盖时消失；每一小批又会重复重写此前数据。\n5. 写入后的统计仍可读到旧缓存，健康检查不参与刷新；锁等待失败则直接丢弃整批，因而“汇总成功”、“盘上行数”和“健康口径”互相矛盾。\n\n不变量清单\n\n- 一条通道的占用状态必须绑定后台工作的真实生命周期；迟到工作只能写入仍获授权的私有结果。\n- 快照更新必须由单调的权威顺序决定，不能让无序返回或缺少版本信息的结果回退已确认状态。\n- 分区的读取、合并、原子替换必须在同一互斥协议中完成；各写入方不得持有自己的分区快照。\n- 批量写入的次数、累计写盘量、缓存失效、健康刷新和锁重试必须共同指向同一个提交事实。',
    3: '（第 3 级提示词——不变量 + 否决项）\n\n必须同时成立的不变量：\n\n1. 后台生命周期：单飞通道只在后台工作真正结束后释放；外层超时不能让第二个工作排队，也不能让已取消工作的诊断或分块数据穿过共享状态边界。\n2. 权威仲裁：快照覆盖遵守单调递增的权威顺序；旧代际、无代际或相同边界的迟到结果不能覆盖已确认结果。部分成功要进入短周期降级探活，成功时间点不能向过去回跳。\n3. 单一分区协议：每次写入都在跨进程互斥内重新读取当前分区、合并并原子替换；并发写入的行集合必须守恒，批次越多也不能把同一段数据反复平方放大。\n4. 提交后可见：一次提交必须同步打穿所有派生统计，并让后台健康刷新如实报告进行中与完成后的事实；短暂锁争用必须使用有界重试，不能把数据错误翻译成整批失败。\n5. 跨层一致：同一次操作在页面、服务、脚本汇总、盘上行数和统计口径中只能有一个答案；任意一端看到旧事实都算失败。\n\n已被否决的思路：\n\n- 把超时预算调大或把所有请求都改成并行，只会延长拥塞或制造更多迟到写入；\n- 只在页面按时间丢弃旧数据，无法修复服务端的权威顺序与共享状态污染；\n- 让每个写入方保留自己的分区副本、用标记文件代替互斥，会继续覆盖另一方的数据；\n- 把锁超时吞掉当成功，或用无限循环重试，会让汇总长期失真或永久卡住；\n- 每次健康检查都同步做全量扫描，会让健康接口失去快速返回能力。\n\n请先画出两条链路的状态边界，再实现一个可复核的共享协议；不要用只让某一组测试变绿的特判。',
  },
};

/** 任务库条目，字段与 server.py `api_tasks` 一致。 */
const TASKS = [
  {
    id: 'T1-01', title: '三处派生口径不一致', tier: 'easy', attempts: 1,
    summary: '换手率、ST 标记、板块判定各算各的。',
    symptom: '推荐策略一个候选都筛不出；新写库的行换手率全是 0%。',
    tags: ['派生指标', '注册表'], repo_id: 'core',
    calibrated: true, target_band: [0.6, 0.85],
    allowed_paths: ['backend/app/services/', 'frontend/src/'],
    forbidden_paths: ['backend/tests/', 'packs/'],
  },
  {
    id: 'T1-02', title: '交易日历双端一致性', tier: 'easy', attempts: 1,
    summary: '后端与跨端进程对同一份交易日历给出不同结论。',
    symptom: '覆盖表把春节算成缺失交易日；跨年默认日期落在休市日。',
    tags: ['日历', '跨端'], repo_id: 'core',
    calibrated: true, target_band: [0.6, 0.85],
    allowed_paths: ['backend/app/calendar.py', 'frontend/src/'],
    forbidden_paths: ['backend/tests/'],
  },
  {
    id: 'T1-03', title: '错误码链路', tier: 'easy', attempts: 1,
    summary: '前端偶发显示通用错误，无法按码重试。',
    symptom: '错误码在跨进程传递时被压平成一段中文。',
    tags: ['错误码', '前端'], repo_id: 'core',
    calibrated: false, target_band: [0.6, 0.85],
    allowed_paths: ['backend/app/errors.py', 'frontend/src/'],
    forbidden_paths: ['backend/tests/'],
  },
  {
    id: 'T2-04', title: '缺口四出口一致性', tier: 'medium', attempts: 2,
    summary: '四个出口对同一天的同一只股票给出三种结论。',
    symptom: '覆盖表说没缺，同步每次都在重抓同一批 3000 只。',
    tags: ['缺口', '一致性', '中位数'], repo_id: 'core',
    calibrated: true, target_band: [0.25, 0.55],
    allowed_paths: ['backend/app/services/gap.py', 'backend/app/services/sync_jobs.py'],
    forbidden_paths: ['backend/tests/', 'packs/'],
  },
  {
    id: 'T2-05', title: '同步任务准入与失活回收', tier: 'medium', attempts: 2,
    summary: '同一批数据反复起 worker；偶发 409 后再也起不来。',
    symptom: '并发去重键没有在异常路径上释放。',
    tags: ['并发', '任务调度'], repo_id: 'core',
    calibrated: true, target_band: [0.25, 0.55],
    allowed_paths: ['backend/app/services/sync_jobs.py'],
    forbidden_paths: ['backend/tests/'],
  },
  {
    id: 'T3-09', title: '实时快照仲裁全链', tier: 'hard', attempts: 3,
    summary: '一次慢请求就能让整条源链断掉。',
    symptom: '行情偶发退回旧快照；一次慢请求后整条源链超时。',
    tags: ['并发', '仲裁', '前端降级'], repo_id: 'core',
    calibrated: true, target_band: [0.05, 0.25],
    allowed_paths: ['backend/app/sources/', 'frontend/src/'],
    forbidden_paths: ['backend/tests/'],
  },
  {
    id: 'T3-10', title: '跨进程写仓 + 攒批 + 服务健康', tier: 'hard', attempts: 3,
    summary: '外部脚本与桌面端同时写，分区损坏、越补越慢。',
    symptom: '分区损坏后补数反而更慢。',
    tags: ['并发', '写仓', '健康检查'], repo_id: 'core',
    calibrated: false, target_band: [0.05, 0.25],
    allowed_paths: ['backend/app/store/'],
    forbidden_paths: ['backend/tests/'],
  },
  {
    id: 'T4-11', title: '实时行情与写入链路同时失去唯一事实', tier: 'king', attempts: 3,
    summary: '压轴题：并发快照仲裁、降级刷新、跨进程写入与健康口径必须共同保持一致。',
    symptom: '行情会回退、写入会丢行、统计会滞后，单独修一条链仍会被另一条链的迟到或缓存状态打破。',
    tags: ['实时行情', '并发', '一致性', '跨进程', '王者'], repo_id: 'core',
    calibrated: false, target_metric: 'pass_at_3', target_band: [0, 0.02],
    allowed_paths: [
      'backend/astock_backtester/data/realtime.py',
      'backend/astock_backtester/data/warehouse.py',
      'backend/astock_backtester/data/operations.py',
      'backend/astock_backtester/service.py',
      'scripts/run-full-market-import.py',
      'frontend/src/marketRefresh.ts',
    ],
    forbidden_paths: ['tests/**', 'pyproject.toml', '**/conftest.py', 'frontend/vitest.config.ts', 'backend/astock_backtester/data/filelock.py', 'backend/astock_backtester/data/sync.py', 'backend/astock_backtester/ai/**', 'packs/**', 'console/**'],
  },
];

const MODELS = [
  { id: 'kimi-k3', protocol: 'openai', api_mode: 'chat_completions', base_url: 'https://api.moonshot.cn/v1', model: 'kimi-k3', key_masked: 'sk-****3f9a', note: '主力对照模型' },
  { id: 'gpt-5-codex', protocol: 'openai', api_mode: 'responses', base_url: 'https://api.openai.com/v1', model: 'gpt-5-codex', key_masked: 'sk-****7b21', note: '强模型上界' },
  { id: 'claude-sonnet', protocol: 'anthropic', api_mode: 'native', base_url: 'https://api.anthropic.com', model: 'claude-sonnet-4', key_masked: 'sk-****c04d', note: '' },
];

function normalizeApiMode(protocol, mode) {
  if (protocol !== 'openai') return 'native';
  return mode || 'chat_completions';
}

function validApiMode(protocol, mode) {
  return protocol !== 'openai' || ['responses', 'chat_completions', 'completions'].includes(mode);
}

/** 校验分组，字段与 harness/grade.py `_grade_groups` 的输出一致。 */
const GRADE_GROUPS = [
  {
    id: 'coverage_exit', title: '覆盖表出口', weight: 1, passed: true,
    cases: [
      { node_id: 'test_summary_missing_rows', outcome: 'passed', duration: 0.42, message: '', detail: '' },
    ],
    total: 1, passed_count: 1,
  },
  {
    id: 'per_symbol_exit', title: '单只股票明细出口', weight: 1, passed: false,
    cases: [
      {
        node_id: 'test_per_symbol_missing_dates', outcome: 'failed', duration: 0.31,
        message: '期望排除零成交票，实际中位数未排除\nE   assert 12 == 9',
        detail: 'self = <MedianSeries [2026-09-10, 2026-09-11, 2026-09-14]>\n'
          + 'E   assert 12 == 9\nE    +  where 12 = len(self)',
      },
      { node_id: 'test_per_symbol_window', outcome: 'passed', duration: 0.18, message: '', detail: '' },
    ],
    total: 2, passed_count: 1,
  },
  {
    id: 'sync_pool_exit', title: '同步阈值出口', weight: 1, passed: true,
    cases: [
      { node_id: 'test_incomplete_symbols_pool', outcome: 'passed', duration: 0.55, message: '', detail: '' },
    ],
    total: 1, passed_count: 1,
  },
  {
    id: 'flow_exit', title: '资金流出口', weight: 1, passed: false,
    cases: [
      {
        node_id: 'test_capital_flow_missing_symbols', outcome: 'failed', duration: 0.27,
        message: '资金流豁免集合比覆盖表多 37 只\nE   37 != 0',
        detail: 'E   AssertionError: 37 != 0',
      },
    ],
    total: 1, passed_count: 0,
  },
  {
    id: 'coherence', title: '四出口一致性', weight: 2, passed: false,
    cases: [
      {
        node_id: 'test_all_exits_agree_on_same_day', outcome: 'failed', duration: 1.02,
        message: '四出口在 2026-09-14 给出三种结论',
        detail: 'E   AssertionError: 2026-09-14 出现三种结论',
      },
      { node_id: 'test_exits_agree_when_all_empty', outcome: 'passed', duration: 0.21, message: '', detail: '' },
    ],
    total: 2, passed_count: 1,
  },
];

const KING_GRADE_GROUPS = [
  { id: 'idempotent_commit', title: '幂等写入与游标原子提交', weight: 2, node_id: 'test_commit_is_atomic', message: '游标已提交，但分片写入回滚' },
  { id: 'lease_generation', title: '租约代际隔离', weight: 2, node_id: 'test_stale_lease_cannot_commit', message: '过期租约仍覆盖新一代结果' },
  { id: 'cancel_recovery', title: '取消与恢复边界', weight: 2, node_id: 'test_cancel_releases_pending_state', message: '取消后 pending 记录未释放' },
  { id: 'bounded_retry', title: '有界退避与降级', weight: 1, node_id: 'test_retry_budget_is_bounded', message: '重试退避超过配置预算' },
];

function kingGradeGroups(attempt) {
  const passedByAttempt = {
    1: [],
    2: ['idempotent_commit'],
    3: ['idempotent_commit', 'lease_generation', 'bounded_retry'],
  };
  const passedIds = new Set(passedByAttempt[attempt] || []);
  return KING_GRADE_GROUPS.map((group) => {
    const passed = passedIds.has(group.id);
    return {
      id: group.id,
      title: group.title,
      weight: group.weight,
      passed,
      total: 1,
      passed_count: passed ? 1 : 0,
      cases: [{
        node_id: group.node_id,
        outcome: passed ? 'passed' : 'failed',
        duration: 0.5,
        message: passed ? '' : group.message,
        detail: '',
      }],
    };
  });
}

const GRADE_LOG_LINES = [
  '开始校验：T2-04 kimi-k3 第 1 轮',
  '检查越界：比对全树哈希，允许改动 backend/app/services/',
  '基线哈希一致，未发现越界改动',
  '运行隐藏用例 tests_hidden/test_gap_consistency.py',
  '  test_summary_missing_rows 通过',
  '  test_per_symbol_missing_dates 失败',
  '  test_incomplete_symbols_pool 通过',
  '  test_capital_flow_missing_symbols 失败',
  '  test_all_exits_agree_on_same_day 失败',
  '运行回归白名单：42 条既有用例',
  '  42 条全部保持绿色',
  '计算分组部分分与权重',
  '与上一轮对比：coverage_exit、sync_pool_exit 由红转绿',
  '校验结束，用时 6 秒',
];

const PREPARE_LOG_LINES = [
  '读取任务包 packs/core/tasks/T2-04/meta.json',
  '按白名单拷贝快照（backend / frontend / tests 裁剪 / scripts）',
  '应用脱敏：删除 AGENTS.md §9 §15 §18',
  '全树 grep 绝对路径：无命中',
  'git init + 单提交 baseline',
  'sandboxes\\T2-04__kimi-k3 工作区就绪',
  '沙箱就绪',
];

const REFERENCE_PATCH = [
  '--- a/backend/app/services/gap.py',
  '+++ b/backend/app/services/gap.py',
  '@@ 新增 GapOracle：缺口判定与阈值的唯一来源 @@',
  '+class GapOracle:',
  '+    def missing_symbols(self, trading_day, universe):',
  '+        ...',
  '--- a/backend/app/services/sync_jobs.py',
  '+++ b/backend/app/services/sync_jobs.py',
  '@@ 同步阈值改读 GapOracle，去掉本地常量 @@',
].join('\n');

const SAMPLE_DIFF = [
  'diff --git a/backend/app/services/gap.py b/backend/app/services/gap.py',
  '--- a/backend/app/services/gap.py',
  '+++ b/backend/app/services/gap.py',
  '@@ -40,6 +40,14 @@ class GapService:',
  '-    def median_gap(self, day, universe):',
  '+    def median_gap(self, day, universe):',
  '+        tradable = [s for s in universe if self.volume_of(day, s) > 0]',
  '+        return self.oracle.median_gap(day, tradable)',
  'diff --git a/backend/app/services/sync_jobs.py b/backend/app/services/sync_jobs.py',
  '--- a/backend/app/services/sync_jobs.py',
  '+++ b/backend/app/services/sync_jobs.py',
  '@@ -88,7 +88,7 @@ def decide_threshold(cfg):',
  '-    return cfg.get("gap_days", 3)',
  '+    return GapOracle(cfg).threshold',
].join('\n');

/** 记分板矩阵，字段与 server.py `api_scoreboard` 一致（数据源 = 成绩台账）。 */
const SCOREBOARD_MATRIX = [
  {
    task: 'T1-01', title: '三处派生口径不一致', tier: 'easy', target_band: [0.6, 0.85],
    cells: {
      'kimi-k3': { attempts: 2, pass1: 1, pass_rate: 0.5, best_score: 100, best_rounds: 1, avg_score: 100, last_at: '2026-09-29T20:00:00', entry_ids: ['res-000001', 'res-000002'] },
      'gpt-5-codex': { attempts: 1, pass1: 1, pass_rate: 1, best_score: 100, best_rounds: 1, avg_score: 100, last_at: '2026-09-29T20:10:00', entry_ids: ['res-000003'] },
    },
  },
  {
    task: 'T1-02', title: '交易日历双端一致性', tier: 'easy', target_band: [0.6, 0.85],
    cells: {
      'kimi-k3': { attempts: 1, pass1: 0, pass_rate: 0, best_score: 75, best_rounds: 2, avg_score: 75, last_at: '2026-09-29T20:20:00', entry_ids: ['res-000004'] },
    },
  },
  {
    task: 'T2-04', title: '缺口四出口一致性', tier: 'medium', target_band: [0.25, 0.55],
    cells: {
      'kimi-k3': { attempts: 2, pass1: 0, pass_rate: 0, best_score: 75, best_rounds: 2, avg_score: 67.5, last_at: '2026-09-29T20:30:00', entry_ids: ['res-000005', 'res-000006'] },
      'gpt-5-codex': { attempts: 2, pass1: 1, pass_rate: 0.5, best_score: 90, best_rounds: 1, avg_score: 85, last_at: '2026-09-29T20:40:00', entry_ids: ['res-000007', 'res-000008'] },
    },
  },
  {
    task: 'T3-09', title: '实时快照仲裁全链', tier: 'hard', target_band: [0.05, 0.25],
    cells: {
      'kimi-k3': { attempts: 3, pass1: 0, pass_rate: 0, best_score: 50, best_rounds: 3, avg_score: 43.3, last_at: '2026-09-29T20:50:00', entry_ids: ['res-000009', 'res-000010', 'res-000011'] },
      'gpt-5-codex': { attempts: 3, pass1: 1, pass_rate: 0.333, best_score: 60, best_rounds: 3, avg_score: 50, last_at: '2026-09-29T21:00:00', entry_ids: ['res-000012', 'res-000013', 'res-000014'] },
    },
  },
  {
    task: 'T4-11', title: '实时行情与写入链路同时失去唯一事实', tier: 'king', target_band: [0, 0.02],
    cells: {
      'kimi-k3': { attempts: 0, pass1: 0, pass_rate: 0, best_score: 0, best_rounds: 0, avg_score: 0, last_at: '', entry_ids: [] },
      'gpt-5-codex': { attempts: 0, pass1: 0, pass_rate: 0, best_score: 0, best_rounds: 0, avg_score: 0, last_at: '', entry_ids: [] },
    },
  },
];

const HEALTH = {
  ok: true,
  checked_at: '2026-09-29T21:40:00',
  uptime_s: 143,
  checks: [
    { id: 'python', label: 'Python', ok: true, value: '3.13.1' },
    { id: 'pytest', label: 'pytest', ok: true, value: '9.0.3' },
    { id: 'node', label: 'Node', ok: true, value: 'v24.19.0' },
    { id: 'repo', label: '仓库可读', ok: true, value: 'D:\\new model test' },
    { id: 'sandbox_root', label: '沙箱目录', ok: true, value: 'sandboxes（文件夹工作区）' },
    { id: 'packs', label: '任务包', ok: true, value: '7 个任务' },
  ],
  warnings: [],
  checkers: ['pytest', 'vitest'],
};

const SELFCHECK = {
  generated_at: '2026-09-29T21:41:00',
  scanned_frontend_files: 36,
  scanned_danger_files: 8,
  issues: [],
  summary: { error: 0, warning: 0, ok: 120 },
  rules: [
    { id: 'no-innerhtml', title: '禁止 innerHTML 渲染业务数据', level: 'error' },
    { id: 'no-eval', title: '禁止 eval / new Function', level: 'error' },
    { id: 'no-external-cdn', title: '禁止外链资源与第三方 CDN', level: 'error' },
  ],
};

// ============================ 内存态 ============================

/**
 * run_id → 运行记录。
 *
 * 真实后端的运行记录落盘，刷新后还在；mock 的 Map 是页面的内存态，一刷新就没了，
 * 前端「刷新回到同一轮」的验收就演不出来。所以借 sessionStorage 把 runs 存一份，
 * 每次请求处理完写回，模块加载时恢复。仅 mock 使用，不影响真实路径。
 */
const RUNS_STORE_KEY = 'evalconsole:mock:runs';
const runs = (function restoreRuns() {
  const m = new Map();
  try {
    const raw = window.sessionStorage.getItem(RUNS_STORE_KEY);
    if (raw) {
      JSON.parse(raw).forEach(([k, v]) => m.set(k, v));
    }
  } catch {
    /* 恢复失败就当没有历史 */
  }
  return m;
})();

/** 把 runs 写回 sessionStorage；写失败不影响请求结果。 */
function persistRuns() {
  try {
    window.sessionStorage.setItem(RUNS_STORE_KEY, JSON.stringify(Array.from(runs.entries())));
    window.sessionStorage.setItem(LEDGER_STORE_KEY, JSON.stringify(ledger));
  } catch {
    /* 存不下就算了（隐私模式 / 配额满） */
  }
}

/**
 * 成绩台账条目（2026-10-02「结束」语义）。
 *
 * 真实后端写在 runs/_results/ledger.json，与运行记录脱钩：点「结束本轮」先写一条
 * 台账再真删记录。mock 里同样保留一份，所以「结束之后榜单还有数」这条链路
 * 在 mock 模式下也演得出来；记分板与排行榜都从这份台账读数。
 */
const LEDGER_STORE_KEY = 'evalconsole:mock:ledger';
const ledger = (function restoreLedger() {
  try {
    const raw = window.sessionStorage.getItem(LEDGER_STORE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    }
  } catch {
    /* 恢复失败就当空台账 */
  }
  return SCOREBOARD_MATRIX.flatMap((row) =>
    Object.entries(row.cells).flatMap(([model, cell]) =>
      (cell.entry_ids || []).map((entryId) => ({
        entry_id: entryId,
        task: row.task,
        model,
        model_raw: model,
        source_run_id: '',
        origin: 'backfill',
        rounds: cell.best_rounds || 1,
        best_round: cell.best_rounds || 1,
        score: cell.best_score || 0,
        passed: cell.pass1 > 0,
        pass1: false,
        model_work_seconds: null,
        wall_seconds: null,
        graded_at: cell.last_at || '',
        ended_at: cell.last_at || '',
      })),
    ),
  );
}());

/** 模型档案 */
let models = MODELS.map((m) => ({ ...m }));
/** 任务 ID → 历史成绩（server.py `_task_history` 的形状） */
const history = {
  'T1-01': { runs: 3, models: ['kimi-k3', 'gpt-5-codex'], best_score: 100, last_at: '2026-09-28T20:11:00' },
  'T1-02': { runs: 1, models: ['kimi-k3'], best_score: 75, last_at: '2026-09-27T14:02:00' },
  'T2-04': { runs: 4, models: ['kimi-k3', 'gpt-5-codex'], best_score: 80, last_at: '2026-09-29T19:40:00' },
  'T3-09': { runs: 6, models: ['kimi-k3', 'gpt-5-codex'], best_score: 40, last_at: '2026-09-26T09:15:00' },
  'T4-11': { runs: 0, models: [], best_score: 0, last_at: '' },
};

// ============================ 工具 ============================

/** 让出主线程，模拟网络延迟。 */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** ISO 时间戳。 */
function isoNow(offsetMs = 0) {
  return new Date(Date.now() + offsetMs).toISOString();
}

/** 构造一个「假响应」：api.js 见到 __mockResponse 标记就直接取 data。 */
function ok(data) {
  return { __mockResponse: true, status: 200, data };
}

/** 构造一个「假错误响应」：让 api.js 走真正的错误映射路径（用后端真实的 E_* 码）。 */
function fail(status, code, message = '', extra = {}) {
  return {
    __mockResponse: false,
    status,
    ok: false,
    headers: { get: () => 'application/json' },
    async json() {
      return { code, message, ...extra };
    },
    async text() {
      return JSON.stringify({ code, message, ...extra });
    },
  };
}

/** 读取并解析请求体。 */
async function readBody(init) {
  if (!init || !init.body) return {};
  try {
    return JSON.parse(init.body);
  } catch {
    return {};
  }
}

/** 从 URL 里取路径部分（去掉 /api 前缀与查询串）。 */
function routeOf(url) {
  const u = String(url);
  const i = u.indexOf('?');
  const withoutQuery = i === -1 ? u : u.slice(0, i);
  return withoutQuery.replace(/^\/api\/?/, '').replace(/\/$/, '');
}

/** 从 URL 里取查询参数。 */
function queryOf(url) {
  return new URL(String(url), 'http://127.0.0.1').searchParams;
}

/**
 * 从 `runs/...` 路由里取出 run_id。
 *
 * routeOf 剥掉的只是 `/api` 前缀，`runs/` 还在里面，而 runs 这张 Map 的键是
 * 裸 run_id，所以每处都得先砍掉 `runs/` 前缀再 decode —— 少砍一次就查不到记录。
 * suffix 传 '' 取整条，传 '/grade' 之类先砍掉动作段。
 *
 * @param {string} route
 * @param {string} suffix 结尾的动作段（含前导斜杠），没有就传 ''
 * @returns {string}
 */
function runIdOf(route, suffix) {
  let rest = route.slice('runs/'.length);
  if (suffix) {
    if (rest.slice(-suffix.length) !== suffix) return '';
    rest = rest.slice(0, -suffix.length);
  }
  return decodeURIComponent(rest);
}

/** 按耗时推进的日志脚本：给定总时长与总行数，返回已产出的行。 */
function progressLines(lines, elapsedMs, totalMs, stepMs) {
  const n = Math.max(1, Math.min(lines.length, Math.floor((elapsedMs / totalMs) * lines.length) + 1));
  return lines.slice(0, n);
}

/** 时间戳目录名。 */
function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

// ============================ 任务 / 报告 ============================

/**
 * 按权重算部分分。
 * @param {Array} groups
 * @returns {{score: number, raw_score: number, total_weight: number, passed_weight: number}}
 */
function computeScore(groups) {
  const total = groups.reduce((sum, g) => sum + (Number(g.weight) || 0), 0);
  const passed = groups.reduce((sum, g) => sum + (g.passed ? Number(g.weight) || 0 : 0), 0);
  const raw = total ? Math.round((100 * passed) / total * 10) / 10 : 0;
  return { score: raw, raw_score: raw, total_weight: total, passed_weight: passed };
}

/**
 * 造一份完整的报告，字段与 harness/grade.py + harness/report.py 输出一致。
 * @param {object} run
 * @param {Array} groups
 * @param {object} opts
 * @returns {object}
 */
function buildReport(run, groups, opts = {}) {
  const scoring = computeScore(groups);
  const green = groups.filter((g) => g.passed).length;
  const red = groups.length - green;
  const passed = scoring.score >= 100;
  const attempt = Number(run.attempt) || 1;
  const allowed = Number(run.attempts_allowed) || 1;
  const invalidated = Boolean(opts.invalidated);
  const canPromote = !run.revealed && !invalidated && attempt < allowed && scoring.score < 100;

  let nextHint;
  if (invalidated) {
    nextHint = { action: 'fix', label: '先修回归与越界', reason: '本轮已作废，修好后再校验一次。', can_promote: false };
  } else if (scoring.score >= 100) {
    nextHint = { action: 'complete', label: '全绿，本题完成', reason: '所有分组都通过了。可以换个模型重来，或查看参考解收尾。', can_promote: false };
  } else if (run.revealed) {
    nextHint = { action: 'reveal', label: '已揭晓本轮', reason: '这一轮已看过参考解，按规则不计入通过率统计。点「清空改动」可以换个模型重来。', can_promote: false };
  } else if (attempt < allowed) {
    nextHint = { action: 'promote', label: `进入第 ${attempt + 1} 轮`, reason: `还有 ${allowed - attempt} 次尝试机会。下一级提示词会多给一层信息（仍不含文件名）。`, can_promote: true };
  } else {
    nextHint = { action: 'reveal', label: '轮次用尽', reason: `已经用完 ${allowed} 次机会。可以查看参考解（该轮标记为已揭晓，不计入统计），或换个模型重来。`, can_promote: false };
  }

  return {
    run_id: run.run_id,
    task: run.task,
    model: run.model,
    attempt,
    graded_at: isoNow(),
    duration_s: opts.duration_s === undefined ? 6 : opts.duration_s,
    score: scoring.score,
    raw_score: scoring.raw_score,
    total_weight: scoring.total_weight,
    passed_weight: scoring.passed_weight,
    passed,
    p2p_broken: Boolean(opts.p2p_broken),
    regressions: opts.regressions || [],
    groups,
    violations: opts.violations || [],
    noise: opts.noise || [],
    similarity: { checked: 1, flagged: false, max_ratio: 0.18, matches: [] },
    diff: opts.diff || { files: 6, added_lines: 214, removed_lines: 88, changed_lines: 302, line_cap: 800, over_cap: false },
    baseline_problems: opts.baseline_problems || [],
    checks: opts.checks || [
      { kind: 'pytest', command: 'python -m pytest -q tests_hidden', returncode: 1, duration_s: 5.2, timed_out: false, summary: '5 passed, 3 failed', notes: '', log_tail: '' },
    ],
    grade_dir: `D:\\new model test\\runs\\${run.run_id}\\grade`,
    error: opts.error || '',
    invalidated,
    invalid_reason: invalidated ? '破坏了既有通过用例（1 条），本轮作废' : '',
    task_title: (TASKS.find((t) => t.id === run.task) || {}).title || '',
    tier: (TASKS.find((t) => t.id === run.task) || {}).tier || '',
    attempts_allowed: allowed,
    allowed_paths: (TASKS.find((t) => t.id === run.task) || {}).allowed_paths || [],
    comparison: opts.comparison || { has_previous: false, previous_attempt: null, previous_score: null, turned_green: [], stayed_red: [], regressed: [] },
    next_hint: nextHint,
    summary: {
      groups: groups.map((g) => ({
        id: g.id,
        title: g.title,
        weight: g.weight,
        passed: g.passed,
        total: g.total,
        passed_count: g.passed_count,
        failed_tests: g.cases.filter((c) => c.outcome !== 'passed').map((c) => c.node_id),
        first_failure: (g.cases.find((c) => c.outcome !== 'passed') || {}).message || '',
      })),
      green,
      red,
    },
    model_note: run.note || '',
    baseline_digest: run.baseline_digest,
    drive: run.drive,
    sandbox: run.sandbox,
    generated_at: isoNow(),
  };
}

// ============================ 运行记录 ============================

/**
 * 创建一个新的运行记录。
 * @param {string} taskId
 * @param {string} modelId
 * @param {number} attempt
 * @returns {object|null}
 */
function createRun(taskId, modelId, attempt) {
  const task = TASKS.find((t) => t.id === taskId);
  if (!task) return null;
  if (!models.some((m) => m.id === modelId)) return { bad: 'E_MODEL_NOT_FOUND' };
  const want = Math.max(1, Number(attempt) || 1);
  if (want > task.attempts) return { bad: 'E_BAD_REQUEST' };
  const run = {
    run_id: `${taskId}__${modelId}__${stamp()}`,
    task: taskId,
    model: modelId,
    attempt: want,
    attempts_allowed: task.attempts,
    status: 'ready',
    sandbox: `D:\\new model test\\sandboxes\\${taskId}__${modelId}`,
    drive: 'Q:',
    baseline_digest: '9f2c41ab7de3',
    created_at: isoNow(-42_000),
    updated_at: isoNow(),
    revealed: false,
    calibration: false,
    note: '',
    rounds: [],
    grading: false,
    last_error: null,
    report: null,
    log: [],
    /** 内部计时字段，不进 run_view */
    _preparing: { start: Date.now(), total: 1800, log: PREPARE_LOG_LINES },
    _grading: null,
  };
  runs.set(run.run_id, run);
  return run;
}

/**
 * 把内部 run 转成 GET /api/runs/{id} 的响应体（run_view 的形状）。
 * @param {object} run
 * @returns {object}
 */
function runView(run) {
  const { _preparing, _grading, chat_messages, ...rest } = run;
  const view = { ...rest };
  view.log = [];
  if (run.status === 'ready' || run.status === 'graded' || run.status === 'error') {
    // run_view 只在 grading/graded/error 时回传校验日志
    if (run.status === 'graded' && run.log.length) view.log = run.log;
  }
  return view;
}

const MOCK_CHAT_TOOLS = [
  {
    type: 'function',
    function: {
      name: 'list_files', description: '列出沙箱内文件。',
      parameters: { type: 'object', properties: { path: { type: 'string' }, recursive: { type: 'boolean' } } },
    },
  },
  {
    type: 'function',
    function: {
      name: 'read_file', description: '读取沙箱内文本文件。',
      parameters: { type: 'object', required: ['path'], properties: { path: { type: 'string' }, max_chars: { type: 'integer' } } },
    },
  },
  {
    type: 'function',
    function: {
      name: 'write_file', description: '写入沙箱内文件。',
      parameters: { type: 'object', required: ['path', 'content'], properties: { path: { type: 'string' }, content: { type: 'string' } } },
    },
  },
  {
    type: 'function',
    function: {
      name: 'run_command', description: '在沙箱根目录运行一个受限命令。',
      parameters: {
        type: 'object', required: ['command'],
        properties: {
          command: { anyOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }] },
          timeout_s: { type: 'integer' },
        },
      },
    },
  },
];

function chatMessages(run) {
  if (!Array.isArray(run.chat_messages)) run.chat_messages = [];
  return run.chat_messages;
}

function appendChatMessage(run, message) {
  const item = {
    id: message.id || `mock-msg-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    created_at: message.created_at || isoNow(),
    ...message,
  };
  chatMessages(run).push(item);
  return item;
}

function chatModel(run) {
  const model = models.find((item) => item.id === run.model) || {};
  return {
    id: model.id || run.model,
    model: model.model || run.model,
    protocol: model.protocol || 'openai',
    api_mode: model.api_mode || 'chat_completions',
  };
}

function chatHistoryResponse(run) {
  return {
    run_id: run.run_id,
    messages: chatMessages(run),
    model: chatModel(run),
    tools: MOCK_CHAT_TOOLS,
  };
}

function handleChatSend(run, body) {
  const text = String(body.message || '').trim();
  if (!text) return fail(400, 'E_BAD_REQUEST', '消息不能为空。');
  if (run.status !== 'ready') {
    return fail(409, 'E_RUN_BUSY', '只有沙箱就绪时才能对话；请等待当前操作结束后再试。');
  }
  const model = models.find((item) => item.id === run.model) || {};
  if (model.protocol !== 'openai' || model.api_mode !== 'chat_completions') {
    return fail(
      400,
      'E_CHAT_UNSUPPORTED',
      '当前工作区对话需要 Chat Completions，以便让模型通过受限工具操作沙箱。',
      { detail: `protocol=${model.protocol || 'unknown'}; api_mode=${model.api_mode || 'unknown'}` },
    );
  }
  appendChatMessage(run, { role: 'user', content: text });
  const toolId = `mock-tool-${Date.now()}`;
  const toolCall = {
    id: toolId,
    type: 'function',
    function: { name: 'list_files', arguments: JSON.stringify({ path: '', recursive: false }) },
  };
  appendChatMessage(run, {
    role: 'assistant',
    content: '',
    tool_calls: [toolCall],
    reasoning_content: '【模拟数据】先查看沙箱文件，再确定需要检查的模块。',
  });
  appendChatMessage(run, {
    role: 'tool',
    name: 'list_files',
    tool_call_id: toolId,
    content: JSON.stringify({ path: '.', entries: ['backend/', 'frontend/', 'tests/'], truncated: false }),
  });
  const final = appendChatMessage(run, {
    role: 'assistant',
    content: `已收到：${text}\n我先检查了当前沙箱的顶层目录，可以继续在这里修改文件。`,
  });
  return ok({ message: final, ...chatHistoryResponse(run) });
}

// ============================ 路由分发 ============================

/**
 * 假 fetch 的路由分发本体（transport 包在外面负责落盘）。
 * @param {string} url
 * @param {object} [init]
 * @returns {Promise<object>}
 */
async function dispatch(url, init = {}) {
  const method = (init.method || 'GET').toUpperCase();
  const route = routeOf(url);
  const body = await readBody(init);
  const q = queryOf(url);

  // 模拟网络往返，让加载态、骨架屏真的能被看见
  await sleep(method === 'GET' ? 80 : 130);

  if (String(url).indexOf('/api/') === -1) {
    return fail(404, 'E_NOT_FOUND', `mock 未实现：${route}`);
  }

  // ---- 健康检查 ----
  if (route === 'health' && method === 'GET') {
    return ok({ ...HEALTH });
  }

  // ---- 任务库 ----
  if (route === 'tasks' && method === 'GET') {
    const tier = q.get('tier');
    const tag = q.get('tag');
    let list = TASKS;
    if (tier) list = list.filter((t) => t.tier === tier);
    if (tag) list = list.filter((t) => (t.tags || []).includes(tag));
    return ok({
      tasks: list.map((t) => ({ ...t, history: history[t.id] || { runs: 0, models: [], best_score: 0, last_at: '' } })),
      count: list.length,
    });
  }
  if (route.indexOf('tasks/') === 0 && route.endsWith('/leaderboard') && method === 'GET') {
    const id = decodeURIComponent(route.slice('tasks/'.length, -'/leaderboard'.length));
    const task = TASKS.find((t) => t.id === id);
    if (!task) return fail(404, 'E_TASK_NOT_FOUND', `没有这道题：${id}`);
    // 数据源是成绩台账：同一模型取分数最高的那条（口径与 server.py task_leaderboard 一致）
    const byModel = new Map();
    ledger.filter((e) => e.task === id).forEach((entry) => {
      const list = byModel.get(entry.model) || [];
      list.push(entry);
      byModel.set(entry.model, list);
    });
    const entries = [];
    byModel.forEach((items, model) => {
      const best = items.slice().sort((a, b) => (b.score - a.score)
        || (a.rounds - b.rounds)
        || String(a.ended_at).localeCompare(String(b.ended_at)))[0];
      entries.push({
        entry_id: best.entry_id,
        model,
        attempts: items.length,
        score: best.score,
        rounds: best.rounds,
        duration_s: best.model_work_seconds,
        model_work_seconds: best.model_work_seconds,
        wall_seconds: best.wall_seconds,
        completed_at: best.graded_at,
        ended_at: best.ended_at,
      });
    });
    entries.sort((a, b) => b.score - a.score
      || a.rounds - b.rounds
      || (a.duration_s === null ? Infinity : a.duration_s) - (b.duration_s === null ? Infinity : b.duration_s)
      || String(a.completed_at).localeCompare(String(b.completed_at))
      || String(a.model).localeCompare(String(b.model)));
    entries.forEach((entry, index) => { entry.rank = index + 1; });
    return ok({ task: task.id, title: task.title, entries });
  }
  if (route.indexOf('tasks/') === 0 && method === 'GET') {
    const id = decodeURIComponent(route.slice('tasks/'.length));
    const task = TASKS.find((t) => t.id === id);
    if (!task) return fail(404, 'E_TASK_NOT_FOUND', `没有这道题：${id}`);
    const levels = PROMPTS[id] || PROMPTS['T2-04'];
    const requestedRunId = q.get('run_id');
    const taskRuns = Array.from(runs.values()).filter((r) => r.task === id);
    const latest = requestedRunId
      ? runs.get(requestedRunId)
      : taskRuns.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] || null;
    if (requestedRunId && (!latest || latest.task !== id)) {
      return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${requestedRunId}`);
    }
    const unlocked = latest ? Number(latest.attempt) || 1 : 0;
    const maxLevel = Math.max(task.attempts, 3);
    const prompts = [];
    for (let lv = 1; lv <= maxLevel; lv += 1) {
      if (levels[lv] && (unlocked ? lv <= Math.max(unlocked, 1) : lv <= task.attempts)) {
        prompts.push({ level: lv, text: levels[lv] });
      }
    }
    return ok({
      id: task.id,
      title: task.title,
      tier: task.tier,
      attempts: task.attempts,
      summary: task.summary,
      symptom: task.symptom,
      tags: task.tags,
      allowed_paths: task.allowed_paths,
      forbidden_paths: task.forbidden_paths,
      budget: { timeout_s: 240, diff_line_cap: 800, retry: 1 },
      calibration: task.calibrated,
      prompts,
      unlocked_prompts: unlocked,
      wiring_note: WIRING_NOTE,
      run: latest
        ? {
          run_id: latest.run_id,
          model: latest.model,
          attempt: latest.attempt,
          status: latest.status,
          sandbox: latest.sandbox,
          drive: latest.drive,
          revealed: latest.revealed,
        }
        : null,
    });
  }

  // ---- 运行记录 ----
  if (route === 'runs' && method === 'GET') {
    const items = Array.from(runs.values())
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
      .map((r) => ({
        run_id: r.run_id, task: r.task, model: r.model, attempt: r.attempt, status: r.status,
        drive: r.drive, created_at: r.created_at, score: r.report ? r.report.score : null,
        revealed: r.revealed,
      }));
    return ok({ runs: items, count: items.length });
  }
  if (route === 'runs' && method === 'POST') {
    const made = createRun(body.task, body.model, body.attempt || 1);
    if (!made) return fail(404, 'E_TASK_NOT_FOUND', `没有这道题：${body.task}`);
    if (made.bad === 'E_MODEL_NOT_FOUND') {
      return fail(404, 'E_MODEL_NOT_FOUND', `没有这个模型档案：${body.model}`);
    }
    if (made.bad === 'E_BAD_REQUEST') {
      return fail(400, 'E_BAD_REQUEST', `轮次不存在，最多 ${TASKS.find((t) => t.id === body.task).attempts} 次机会。`);
    }
    // 真实后端 POST /api/runs 是**同步阻塞**的：返回时沙箱已经铺好、status 已是 ready。
    // 所以这里不模拟 preparing 推进，避免前端出现一段假进度。
    // （会异步推进的只有 /grade，那才是前端需要轮询的阶段。）
    return ok(runView(made));
  }
  if (route.indexOf('runs/') === 0 && route.endsWith('/chat') && method === 'GET') {
    const id = runIdOf(route, '/chat');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    return ok(chatHistoryResponse(run));
  }
  if (route.indexOf('runs/') === 0 && route.endsWith('/chat') && method === 'POST') {
    const id = runIdOf(route, '/chat');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    return handleChatSend(run, body);
  }
  if (route.indexOf('runs/') === 0 && method === 'GET') {
    const id = runIdOf(route, '');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);

    // 准备阶段按时间推进
    if (run.status === 'preparing') {
      const p = run._preparing;
      const elapsed = Date.now() - p.start;
      if (elapsed >= p.total) {
        run.status = 'ready';
        run._preparing = null;
        run.log = [];
      }
    }
    // 校验阶段按时间推进
    if (run.status === 'grading' && run._grading) {
      const g = run._grading;
      const elapsed = Date.now() - g.start;
      run.log = progressLines(GRADE_LOG_LINES, elapsed, g.total, 0);
      if (elapsed >= g.total) {
        run.status = 'graded';
        run._grading = null;
        run.log = GRADE_LOG_LINES.slice();
        const groups = run.task === 'T4-11'
          ? kingGradeGroups(run.attempt)
          : GRADE_GROUPS.map((x) => ({ ...x, cases: x.cases.map((c) => ({ ...c })) }));
        run.report = buildReport(run, groups, {
          comparison: {
            has_previous: true, previous_attempt: 1, previous_score: 20,
            turned_green: ['coverage_exit', 'sync_pool_exit'], stayed_red: ['coherence'], regressed: [],
          },
        });
        run.rounds = [
          {
            attempt: run.attempt, score: run.report.score, passed: run.report.passed,
            invalidated: run.report.invalidated, graded_at: run.report.graded_at, report: 'round-1.json',
          },
        ];
      }
    }
    return ok(runView(run));
  }
  if (route.endsWith('/grade') && method === 'POST') {
    const id = runIdOf(route, '/grade');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    if (run.status === 'grading') {
      return fail(409, 'E_RUN_BUSY', '这一轮正在校验中，请等当前校验结束。重复点击不会重复跑。');
    }
    if (!run.sandbox) return fail(409, 'E_SANDBOX_MISSING', '沙箱还没准备好，无法校验。请先点「准备沙箱」。');
    run.status = 'grading';
    run.grading = true;
    run.log = [];
    run._grading = { start: Date.now(), total: 5000 };
    return ok({ run_id: id, status: 'grading' });
  }
  if (route.endsWith('/promote') && method === 'POST') {
    const id = runIdOf(route, '/promote');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    if (run.revealed) {
      return fail(400, 'E_BAD_REQUEST', '这一轮已经揭晓过参考解，不能再进入下一轮。请点「清空改动」换个模型重来。');
    }
    if (Number(run.attempt) >= Number(run.attempts_allowed)) {
      return fail(400, 'E_BAD_REQUEST', `已经用完 ${run.attempts_allowed} 次机会。`);
    }
    run.attempt = Number(run.attempt) + 1;
    run.status = 'ready';
    run.report = null;
    run.rounds = [];
    return ok({ run_id: id, attempt: run.attempt, can_promote: run.attempt < run.attempts_allowed });
  }
  if (route.endsWith('/reveal') && method === 'POST') {
    const id = runIdOf(route, '/reveal');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    run.revealed = true;
    return ok({
      run_id: id,
      patch: REFERENCE_PATCH,
      notice: '该轮已标记为「已揭晓」，按规则不计入通过率统计。',
    });
  }
  if (route.endsWith('/note') && method === 'POST') {
    const id = runIdOf(route, '/note');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    run.note = String(body.note || '').slice(0, 4000);
    return ok({ run_id: id, note: run.note });
  }
  if (route.endsWith('/finish') && method === 'POST') {
    const id = runIdOf(route, '/finish');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', '运行记录目录不存在，可能已经被删除过了。');
    if (run.status === 'grading') {
      return fail(409, 'E_RUN_BUSY', '这一轮正在校验中，等校验结束后再结束本轮。');
    }
    // 与 server.py record_run_result 同一套判据：只收作数轮，揭晓过的一律不收
    const counted = (run.rounds || []).filter((r) => !r.voided && !r.invalidated);
    const best = counted.slice().sort((a, b) => Number(b.score) - Number(a.score))[0];
    let entry = null;
    if (!run.revealed && best && !ledger.some((e) => e.source_run_id === id)) {
      entry = {
        entry_id: `res-${String(ledger.length + 1).padStart(6, '0')}`,
        task: run.task,
        model: run.model,
        model_raw: run.model,
        source_run_id: id,
        origin: 'run',
        rounds: counted.length,
        best_round: Number(best.attempt) || 1,
        score: Number(best.score) || 0,
        passed: counted.some((r) => r.passed),
        pass1: counted.some((r) => Number(r.attempt) === 1 && r.passed),
        model_work_seconds: null,
        wall_seconds: null,
        graded_at: best.graded_at || '',
        ended_at: isoNow(),
      };
      ledger.push(entry);
    }
    // 真删：记录、对话、沙箱一起消失，只剩台账里的那条成绩
    runs.delete(id);
    persistRuns();
    return ok({
      run_id: id,
      finished: true,
      ledgered: Boolean(entry),
      entry,
      purged: [`D:\\new model test\\runs\\${id}`],
      notice: entry
        ? '本轮成绩已记入台账，记分板与排行榜按最高分那条展示；运行记录、对话与沙箱已彻底删除，下次再跑是全新一轮。'
        : '这一轮没有可计入台账的成绩（未校验 / 已作废 / 已揭晓参考解），记录、对话与沙箱已彻底删除，不留成绩。',
    });
  }
  if (route.endsWith('/diff') && method === 'POST') {
    const id = runIdOf(route, '/diff');
    const run = runs.get(id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${id}`);
    return ok({ diff: SAMPLE_DIFF });
  }

  // ---- 沙箱 ----
  if (route === 'sandbox/reset' && method === 'POST') {
    const run = runs.get(body.run_id);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', `没有这条运行记录：${body.run_id}`);
    if (!run.sandbox) {
      return fail(409, 'E_SANDBOX_MISSING', '沙箱已经不在了，无法清空改动。请点「重建沙箱」。');
    }
    run.status = 'ready';
    run.report = null;
    run.log = [];
    return ok({ seconds: 0.8, cleaned: run.report ? 6 : 6, run_id: run.run_id, sandbox: run.sandbox, drive: run.drive });
  }
  if (route === 'sandbox/rebuild' && method === 'POST') {
    const run = runs.get(body.run_id)
      || Array.from(runs.values()).find((r) => r.task === body.task);
    if (!run) return fail(404, 'E_RUN_NOT_FOUND', '这道题还没有任何运行记录，请先点「准备沙箱」。');
    run.status = 'ready';
    run.report = null;
    run.rounds = [];
    run.log = [];
    run.sandbox = `D:\\new model test\\sandboxes\\${run.task}__${run.model}`;
    run.drive = 'Q:';
    run.baseline_digest = '9f2c41ab7de3';
    return ok({ run_id: run.run_id, sandbox: run.sandbox, drive: run.drive });
  }

  // ---- 记分板 ----
  if (route === 'scoreboard' && method === 'GET') {
    if (q.get('format') === 'csv') {
      const header = '任务,档位,模型,结束次数,pass@1/次数,最高分,均分';
      const lines = SCOREBOARD_MATRIX.flatMap((row) =>
        Object.entries(row.cells).map(([model, c]) => [
          row.task, row.tier, model, c.attempts, `${c.pass1}/${c.attempts}`, c.best_score, c.avg_score,
        ].join(',')),
      );
      return ok([header, ...lines].join('\n'));
    }
    const attempts = SCOREBOARD_MATRIX.flatMap((row) => Object.values(row.cells))
      .reduce((sum, c) => sum + c.attempts, 0);
    const pass1 = SCOREBOARD_MATRIX.flatMap((row) => Object.values(row.cells))
      .reduce((sum, c) => sum + c.pass1, 0);
    return ok({
      generated_at: isoNow(),
      tasks: SCOREBOARD_MATRIX.map((r) => r.task),
      models: models.map((m) => m.id),
      matrix: SCOREBOARD_MATRIX,
      totals: { attempts, pass1, pass_rate: attempts ? Math.round((pass1 / attempts) * 1000) / 1000 : 0 },
      note: '数据源是成绩台账（runs/_results/ledger.json）：只有点过「结束本轮」的尝试才在这里，'
        + '每次结束各留一条，单元格展示最高分那条；作废轮、判无效轮与已揭晓参考解的尝试永不进台账。',
    });
  }

  // ---- 模型档案 ----
  if (route === 'models' && method === 'GET') {
    return ok({ models: models.map((m) => ({ ...m })) });
  }
  if (route === 'models' && method === 'POST') {
    if (!body.id) return fail(400, 'E_BAD_REQUEST', '缺少 id 参数。');
    if (models.some((m) => m.id === body.id)) return fail(409, 'E_BAD_REQUEST', '档案编号已存在。');
    const protocol = body.protocol || 'openai';
    const apiMode = normalizeApiMode(protocol, body.api_mode);
    if (!validApiMode(protocol, apiMode)) return fail(400, 'E_MODEL_INVALID', 'OpenAI 兼容档案的接口形态无效。');
    const item = {
      id: body.id,
      protocol,
      api_mode: apiMode,
      base_url: body.base_url || '',
      model: body.model || body.id,
      key_masked: '',
      note: body.note || '',
    };
    models.push(item);
    return ok(item);
  }
  if (route === 'models' && method === 'PATCH') {
    const idx = models.findIndex((m) => m.id === body.id);
    if (idx === -1) return fail(404, 'E_NOT_FOUND', `没有这个档案：${body.id}`);
    const protocol = body.protocol || models[idx].protocol;
    const apiMode = normalizeApiMode(protocol, body.api_mode || models[idx].api_mode);
    if (!validApiMode(protocol, apiMode)) return fail(400, 'E_MODEL_INVALID', 'OpenAI 兼容档案的接口形态无效。');
    models[idx] = {
      ...models[idx],
      protocol,
      api_mode: apiMode,
      base_url: body.base_url === undefined ? models[idx].base_url : body.base_url,
      model: body.model || models[idx].model,
      note: body.note === undefined ? models[idx].note : body.note,
    };
    return ok(models[idx]);
  }
  if (route === 'models' && method === 'DELETE') {
    const id = q.get('id') || body.id;
    const idx = models.findIndex((m) => m.id === id);
    if (idx === -1) return fail(404, 'E_NOT_FOUND', `没有这个档案：${id}`);
    const [removed] = models.splice(idx, 1);
    return ok({ id: removed.id, removed: true });
  }

  // ---- 校准 ----
  if (route === 'calibration' && method === 'POST') {
    return ok({ queued: Number(body.trials || 5), task: body.task, model: body.model });
  }
  if (route === 'calibration' && method === 'GET') {
    return ok({ task: q.get('task') || '', model: q.get('model') || '', running: [], queued: [] });
  }
  if (route === 'calibration/cancel' && method === 'POST') {
    return ok({ cancelled: 0 });
  }

  // ---- 自检 ----
  if (route === 'selfcheck' && method === 'POST') {
    return ok({ ...SELFCHECK });
  }

  return fail(404, 'E_NOT_FOUND', `mock 未实现该接口：${method} ${route}`);
}

/**
 * 假 fetch：分发一次请求，再把内存态写回 sessionStorage（见 runs 的注释）。
 * api.js 用 `mod.transport` 具名导入顶替传输层，default 导出只是兼容旧写法。
 * @param {string} url
 * @param {object} [init]
 * @returns {Promise<object>}
 */
export async function transport(url, init = {}) {
  const res = await dispatch(url, init);
  persistRuns();
  return res;
}

export default transport;
