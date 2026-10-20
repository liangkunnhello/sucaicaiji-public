/**
 * 无头自检：把 main.js 的纯逻辑函数在 vm 沙箱里跑起来，
 * 校验调度判定、星期归一化、计划参数拼装等核心逻辑。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appDir = process.argv[2] || __dirname;
// 测试用临时目录放在项目内，不落 %TEMP%。
// 这台机器上有安全代理拦截文件操作：%TEMP% 删一个文件要 51s，项目内只要 6s（实测）。
const TEST_TMP_ROOT = path.join(appDir, '.test-tmp');
fs.mkdirSync(TEST_TMP_ROOT, { recursive: true });
const testTmp = (prefix) => fs.mkdtempSync(path.join(TEST_TMP_ROOT, prefix));
if (!appDir) {
  console.error('用法: node selftest.js <工具目录>');
  process.exit(2);
}

const electronStub = {
  app: {
    requestSingleInstanceLock: () => false,
    quit() {},
    on() {},
    whenReady: () => ({ then() {} })
  },
  BrowserWindow: function BrowserWindow() {},
  ipcMain: { handle() {} },
  dialog: {},
  shell: {}
};

let src = fs.readFileSync(path.join(appDir, 'main.js'), 'utf8');
src += `
globalThis.__T = {
  isDue, normalizeWeekdays, currentMinuteKey, normalizePlan, planDownloadParams,
  defaultPlans, getDownloadLimit, parseDownloadProgressLine, platformLabel,
  formatBatchDate, parseLocalDate, formatIsoDate, dayBefore, resolveRunDates,
  dayAfter, normalizeFilterDate, batchDateForFilter,
  buildDownloadLayout, downloadEnv, listMediaPaths, countMedia, countImages
};
`;

const sandbox = {
  require: (name) => (name === 'electron' ? electronStub : require(name)),
  module: { exports: {} },
  exports: {},
  __dirname: appDir,
  __filename: path.join(appDir, 'main.js'),
  console,
  process,
  Buffer,
  setTimeout,
  setInterval,
  clearInterval,
  clearTimeout,
  setImmediate
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: 'main.js' });

const T = sandbox.__T;
let pass = 0;
let fail = 0;

function check(label, actual, expected) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) {
    pass += 1;
    console.log(`  ✓ ${label}`);
  } else {
    fail += 1;
    console.log(`  ✗ ${label}\n      期望 ${e}\n      实际 ${a}`);
  }
}

console.log('\n[1] normalizeWeekdays —— 星期归一化（0=周一 … 6=周日）');
check('空数组 -> 补齐为每天', T.normalizeWeekdays([]), [0, 1, 2, 3, 4, 5, 6]);
check('[0] 保持不变', T.normalizeWeekdays([0]), [0]);
check('[6] 周日', T.normalizeWeekdays([6]), [6]);
check('去重排序', T.normalizeWeekdays([3, 1, 1, 5]), [1, 3, 5]);
check('剔除越界值', T.normalizeWeekdays([0, 9, -1, 6]), [0, 6]);
check('字符串数字也接受', T.normalizeWeekdays(['2', '4']), [2, 4]);
check('非数组 -> 每天', T.normalizeWeekdays('x'), [0, 1, 2, 3, 4, 5, 6]);

console.log('\n[2] isDue —— 定时命中判定');
const plan = (over = {}) => ({
  enabled: true,
  schedule_time: '06:30',
  weekdays: [0, 1, 2, 3, 4, 5, 6],
  ...over
});
// 2026-09-21 是周一
const mon0630 = new Date(2026, 8, 21, 6, 30, 0);
const mon0631 = new Date(2026, 8, 21, 6, 31, 0);
const mon0700 = new Date(2026, 8, 21, 7, 0, 0);
// 2026-09-20 是周日
const sun0630 = new Date(2026, 8, 20, 6, 30, 0);

check('周一 06:30 命中', T.isDue(plan(), mon0630), true);
check('分钟不符不命中', T.isDue(plan(), mon0631), false);
check('小时不符不命中', T.isDue(plan(), mon0700), false);
check('周日不在 [0] 内', T.isDue(plan({ weekdays: [0] }), sun0630), false);
check('周日在 [6] 内', T.isDue(plan({ weekdays: [6] }), sun0630), true);
check('空星期视为每天', T.isDue(plan({ weekdays: [] }), sun0630), true);
check('已停用不触发', T.isDue(plan({ enabled: false }), mon0630), false);
check('时间格式非法不触发', T.isDue(plan({ schedule_time: '25:99' }), mon0630), false);
check('单位数小时 6:30 也命中', T.isDue(plan({ schedule_time: '6:30' }), mon0630), true);

console.log('\n[3] currentMinuteKey —— 防重复触发的分钟键');
check('格式 yyyy-MM-dd HH:mm', T.currentMinuteKey(mon0630), '2026-09-21 06:30');
check('个位数补零', T.currentMinuteKey(new Date(2026, 0, 5, 7, 5, 0)), '2026-01-05 07:05');

console.log('\n[4] planDownloadParams —— 各平台 CLI 参数');
const paramsAdmuse = T.planDownloadParams({
  platform: 'admuse', max_downloads: 300, industries: ['insurance'],
  keyword: '快手', time_range: '7d', sort: '跑量得分降序', score: 40
});
check('admuse 含 --industries', paramsAdmuse.includes('--industries'), true);
check('admuse 含 --max-downloads 300', paramsAdmuse.includes('--max-downloads') && paramsAdmuse.includes('300'), true);
check('admuse 含 --keyword 快手', paramsAdmuse[paramsAdmuse.indexOf('--keyword') + 1], '快手');

const paramsApp = T.planDownloadParams({
  platform: 'appgrowing', max_downloads: 500, keyword: '', media: ['微信'], traffic: [],
  result_view: '广告', material_type: '全部素材类型', material_ratio: '全部素材比例', sort: ''
});
check('appgrowing 用 --max', paramsApp[paramsApp.indexOf('--max') + 1], '500');
check('appgrowing 过滤空 keyword', paramsApp.includes('--keyword'), false);
check('appgrowing 带 --media 微信', paramsApp[paramsApp.indexOf('--media') + 1], '微信');
check('appgrowing 过滤空 traffic', paramsApp.includes('--traffic'), false);

const paramsInternal = T.planDownloadParams({ platform: 'internal', max_downloads: 500 });
check('internal 不产生 CLI 参数（走环境变量）', paramsInternal, []);

console.log('\n[5] getDownloadLimit —— 从参数反推总数');
check('--max-downloads 500', T.getDownloadLimit(['--max-downloads', '500']), 500);
check('--max 200', T.getDownloadLimit(['--max', '200']), 200);
check('无该参数 -> 0', T.getDownloadLimit(['--keyword', 'x']), 0);

console.log('\n[6] parseDownloadProgressLine —— 下载进度解析');
const tracker = { stage: 'connecting', current: 0, total: 0, failed: 0, message: '', actualTotalKnown: false };
T.parseDownloadProgressLine(tracker, '共采集 128 个素材');
check('识别"共采集 N 个素材"', [tracker.total, tracker.stage], [128, 'collecting']);
T.parseDownloadProgressLine(tracker, '下载 12/128');
check('识别"下载 a/b"', [tracker.current, tracker.total, tracker.stage], [12, 128, 'downloading']);
T.parseDownloadProgressLine(tracker, '处理完成！成功: 10, 跳过: 1, 失败: 2');
check('识别完成汇总', [tracker.current, tracker.failed], [13, 2]);

// 登录态要能在界面上看见：以前凭据找不到是静默的，用户只看到进度卡住
const loginTracker = { stage: 'connecting', current: 0, total: 0, failed: 0, message: '', actualTotalKnown: false };
T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|CHECKING|妙思|最多等待 90 秒');
check('登录校验中会提示', loginTracker.message.includes('妙思') && loginTracker.message.includes('校验登录态'), true);
T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|REQUIRED|妙思|等待账号管家恢复登录态或手机验证');
check('掉线等待会提示', loginTracker.message.includes('未登录'), true);
T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|AUTO|妙思|尝试 QQ 账号密码登录（凭据来自本地凭据文件）');
check('自动登录会提示', loginTracker.message.includes('尝试 QQ 账号密码登录'), true);
T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|NO_CREDENTIAL|妙思|未找到 QQ 账号密码，无法自动补登录');
check('缺凭据必须明确报出来',
  loginTracker.message.includes('未找到 QQ 账号密码'), true);
T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|READY|妙思|https://admuse.qq.com/#/idea');
check('登录恢复正常会提示', loginTracker.message.includes('登录态正常'), true);
check('未知登录态不覆盖原有提示',
  T.parseDownloadProgressLine(loginTracker, 'LOGIN_STATUS|WHATEVER|妙思|x'), false);

console.log('\n[7] normalizePlan —— 表单输入归一化');
const p1 = T.normalizePlan({ name: '  测试 ', platform: 'admuse', schedule_time: '07:15', weekdays: [0, 2], max_downloads: 999999 });
check('名称去空格', p1.name, '测试');
check('时间保留', p1.schedule_time, '07:15');
check('星期保留', p1.weekdays, [0, 2]);
check('max_downloads 被夹到 100000', p1.max_downloads, 100000);
check('自动生成 id', typeof p1.id === 'string' && p1.id.startsWith('PLAN-'), true);

const p2 = T.normalizePlan({ platform: '不存在的平台', schedule_time: '乱填', classify_min_confidence: 'abc' });
check('非法平台回退 admuse', p2.platform, 'admuse');
check('非法时间回退 06:30', p2.schedule_time, '06:30');
check('非法置信度回退 0.85', p2.classify_min_confidence, 0.85);
check('默认启用一条龙', p2.classify_enabled, true);

const p3 = T.normalizePlan({ platform: 'reyun', classify_min_confidence: 5, classify_parallel: 99 });
check('置信度上限夹到 1', p3.classify_min_confidence, 1);
check('并发上限夹到 10', p3.classify_parallel, 10);

const existing = T.normalizePlan({ platform: 'admuse' });
const merged = T.normalizePlan({ name: '改名了' }, existing);
check('编辑时保留原 id', merged.id, existing.id);
check('编辑时保留 created_at', merged.created_at, existing.created_at);

console.log('\n[8] defaultPlans —— 默认四条平台计划');
const defaults = T.defaultPlans();
check('数量为 4', defaults.length, 4);
check('时间依次 06:30/06:40/06:50/07:00', defaults.map((x) => x.schedule_time), ['06:30', '06:40', '06:50', '07:00']);
check('平台依次', defaults.map((x) => x.platform), ['admuse', 'appgrowing', 'reyun', 'internal']);
check('默认全部启用', defaults.every((x) => x.enabled), true);
check('播种计划跟随全局导出目录', defaults.every((x) => x.use_global_root === true), true);

console.log('\n[9] 复跑日期一致性 —— 内部系统不能隔天取错日期');
check('formatBatchDate(2026-09-23)', T.formatBatchDate(new Date(2026, 8, 23)), '20260923');
check('dayBefore 跨月', T.formatIsoDate(T.dayBefore(new Date(2026, 9, 1))), '2026-09-30');
check('parseLocalDate 非法输入', T.parseLocalDate('乱码'), null);

// 新记录：已存日期字段，原样沿用
check('新记录沿用原日期', T.resolveRunDates(
  { started_at: '2026-09-24 07:00:08', batch_date: '20260923', filter_date: '2026-09-22' },
  'internal'),
{ batchDate: '20260923', filterDate: '2026-09-22' });
// 老记录：没有日期字段时按开始时间倒推
check('老记录内部系统倒推昨天', T.resolveRunDates(
  { started_at: '2026-09-23 07:00:08' }, 'internal'),
{ batchDate: '20260923', filterDate: '2026-09-22' });
check('老记录其他平台无查询日期', T.resolveRunDates(
  { started_at: '2026-09-23 06:30:08' }, 'admuse'),
{ batchDate: '20260923', filterDate: '' });

// 环境变量透传：复跑必须把原查询日期交给子进程
delete process.env.MATERIAL_DOWNLOADER_FILTER_DATE;
const envLayout = T.buildDownloadLayout(appDir, 'internal', '20260923');
check('普通运行不设 FILTER_DATE（保持原行为）',
  T.downloadEnv(envLayout, appDir, {}).MATERIAL_DOWNLOADER_FILTER_DATE, undefined);
check('复跑透传原查询日期',
  T.downloadEnv(envLayout, appDir, { filter_date: '2026-09-22' }).MATERIAL_DOWNLOADER_FILTER_DATE,
  '2026-09-22');
check('复跑批次目录沿用原日期',
  path.basename(T.downloadEnv(envLayout, appDir, {}).MATERIAL_DOWNLOADER_BATCH_DIR),
  '20260923');

// 内部系统「指定日期下载」：查询日期与产出目录（目录 = 数据日期 + 1 天）
check('指定日期合法', T.normalizeFilterDate('2026-10-12'), '2026-10-12');
check('不存在的日期被拒', T.normalizeFilterDate('2026-02-31'), '');
check('乱填被拒', T.normalizeFilterDate('昨天'), '');
check('dayAfter 跨月', T.formatIsoDate(T.dayAfter(new Date(2026, 8, 30))), '2026-10-01');

const fakeNow = new Date(2026, 9, 19); // 2026-10-19
check('数据日期 10-12 -> 目录 20261013', T.batchDateForFilter('2026-10-12', fakeNow), '20261013');
check('跨月：数据日期 09-30 -> 目录 20261001', T.batchDateForFilter('2026-09-30', fakeNow), '20261001');
check('选到今天时目录回落到今天', T.batchDateForFilter('2026-10-19', fakeNow), '20261019');
check('非法日期回落到今天', T.batchDateForFilter('2026-02-31', fakeNow), '20261019');

const pDate = T.normalizePlan({ platform: 'internal', filter_date: '2026-10-12' });
check('计划保留数据日期', pDate.filter_date, '2026-10-12');
check('计划非法数据日期清空', T.normalizePlan({ platform: 'internal', filter_date: '乱填' }).filter_date, '');
check('默认计划不带数据日期', T.normalizePlan({ platform: 'internal' }).filter_date, '');

// 同一天同一平台只用一个日期目录：多次采集（不同方向）并进来，不再 -2/-3 顺延
check('首批用纯日期目录', T.buildDownloadLayout(appDir, 'admuse', '20260923').batchName, '20260923');
check('再次调用仍是同一个目录',
  T.buildDownloadLayout(appDir, 'admuse', '20260923').batchDir,
  T.buildDownloadLayout(appDir, 'admuse', '20260923').batchDir);
check('不会生成 -2 后缀',
  T.buildDownloadLayout(appDir, 'admuse', '20260923').batchName.includes('-'), false);
check('不同日期仍是不同目录',
  T.buildDownloadLayout(appDir, 'admuse', '20260924').batchName, '20260924');
check('不同平台互不干扰',
  T.buildDownloadLayout(appDir, 'admuse', '20260923').batchDir
    !== T.buildDownloadLayout(appDir, 'reyun', '20260923').batchDir, true);

// 内部系统在日期下再分「图片 / 视频」；其他平台不分
const internalImage = T.buildDownloadLayout(appDir, 'internal', '20260923', '图片');
const internalVideo = T.buildDownloadLayout(appDir, 'internal', '20260923', '视频');
check('内部系统-图片 落在 日期/图片 下',
  path.basename(internalImage.rawDir) === '原始素材'
    && path.basename(path.dirname(internalImage.rawDir)) === '图片', true);
check('内部系统-视频 落在 日期/视频 下',
  path.basename(path.dirname(internalVideo.rawDir)) === '视频', true);
check('图片与视频共用一个日期目录',
  internalImage.dateDir === internalVideo.dateDir, true);
check('图片与视频的产出根不同',
  internalImage.batchDir !== internalVideo.batchDir, true);
check('其他平台不加图片视频层',
  path.basename(path.dirname(T.buildDownloadLayout(appDir, 'admuse', '20260923', '视频').rawDir)),
  '20260923');
check('mediaType 已记录', internalVideo.mediaType, '视频');
check('非内部平台不带 mediaType',
  T.buildDownloadLayout(appDir, 'admuse', '20260923', '视频').mediaType, '');

// 日期目录已存在时**不能**顺延成 -2/-3 —— 这正是「四个平台还是日期-序号」的根因
const seqDir = testTmp('selftest-seq-');
try {
  fs.mkdirSync(path.join(seqDir, '妙思', '20260923'), { recursive: true });
  const again = T.buildDownloadLayout(seqDir, 'admuse', '20260923');
  check('日期目录已存在仍用纯日期', again.batchName, '20260923');
  check('已存在时也不加序号', again.batchDir.endsWith('20260923'), true);

  fs.mkdirSync(path.join(seqDir, '妙思', '20260923-2'), { recursive: true });
  const third = T.buildDownloadLayout(seqDir, 'admuse', '20260923');
  check('已有 -2 也不继续顺延', third.batchName, '20260923');

  // 内部系统同理，且图片/视频各自一层仍落在同一日期下
  fs.mkdirSync(path.join(seqDir, '内部系统', '20260923', '图片'), { recursive: true });
  const internalAgain = T.buildDownloadLayout(seqDir, 'internal', '20260923', '图片');
  check('内部系统已存在仍用纯日期', internalAgain.batchName, '20260923');
  check('内部系统仍落在 日期/图片',
    internalAgain.batchDir.endsWith(path.join('20260923', '图片')), true);
} finally {
  fs.rmSync(seqDir, { recursive: true, force: true });
}

// listMediaPaths：合并目录后靠前后快照差集算「本次新增」；视频也要算进去
const snapDir = testTmp('selftest-snap-');
try {
  fs.mkdirSync(path.join(snapDir, '子目录'), { recursive: true });
  fs.writeFileSync(path.join(snapDir, 'a.jpg'), 'x');
  fs.writeFileSync(path.join(snapDir, 'note.txt'), 'x');
  const before = T.listMediaPaths(snapDir);
  check('只统计媒体文件、忽略其他文件', [...before], ['a.jpg']);
  fs.writeFileSync(path.join(snapDir, 'b.png'), 'x');
  fs.writeFileSync(path.join(snapDir, '子目录', 'c.webp'), 'x');
  fs.writeFileSync(path.join(snapDir, 'v.mp4'), 'x');
  const after = T.listMediaPaths(snapDir);
  check('递归统计子目录', after.has('子目录/c.webp'), true);
  check('视频也算媒体文件', after.has('v.mp4'), true);
  check('差集即本次新增',
    [...after].filter((n) => !before.has(n)).sort(), ['b.png', 'v.mp4', '子目录/c.webp']);
  check('目录不存在时返回空集', T.listMediaPaths(path.join(snapDir, '不存在')).size, 0);
  check('countMedia 含视频', T.countMedia(snapDir), 4);
  check('countImages 不含视频', T.countImages(snapDir), 3);
} finally {
  fs.rmSync(snapDir, { recursive: true, force: true });
}

// ---------------------------------------------------------------------------
// [10] 双车道队列：采集车道串行、采集与分类并行。
// 这段要动 setTimeout，且 tickScheduler 会写 plans 文件，
// 所以另起一个 __dirname 指向临时目录的沙箱，绝不碰真实 user_data。
// ---------------------------------------------------------------------------
function makeSandbox(baseDir, extraGlobals) {
  let source = fs.readFileSync(path.join(appDir, 'main.js'), 'utf8');
  source += `\nglobalThis.__T = { ${extraGlobals} };\n`;
  const ctx = {
    require: (name) => (name === 'electron' ? electronStub : require(name)),
    module: { exports: {} },
    exports: {},
    __dirname: baseDir,
    __filename: path.join(baseDir, 'main.js'),
    console, process, Buffer,
    setTimeout, setInterval, clearInterval, clearTimeout, setImmediate
  };
  vm.createContext(ctx);
  vm.runInContext(source, ctx, { filename: 'main.js' });
  return ctx;
}

async function runQueueChecks() {
  const os = require('os');
  const tmpAppDir = testTmp('selftest-queue-');
  try {
    const queueSandbox = makeSandbox(
      tmpAppDir,
      'lanes, enqueueJob, laneOf, queueTotals, tickScheduler, isDue, '
      + 'loadPlans, savePlans, normalizePlan'
    );
    const R = queueSandbox.__T;

    console.log('\n[10] 双车道队列 —— 采集串行、采集与分类并行');
    check('classify 任务走分类车道', R.laneOf({ kind: 'classify' }), 'classify');
    check('plan-classify 走分类车道', R.laneOf({ kind: 'plan-classify' }), 'classify');
    check('显式 lane=classify 也认', R.laneOf({ kind: 'x', lane: 'classify' }), 'classify');
    check('plan 任务走采集车道', R.laneOf({ kind: 'plan' }), 'collect');
    check('未声明车道默认采集', R.laneOf({ kind: 'whatever' }), 'collect');

    // 用假的 runJob 记录时序，不真的起子进程。
    // runJob 是 vm 上下文里的顶层函数声明，直接覆盖即可生效。
    const events = [];
    const HOLD = 90;
    queueSandbox.runJob = async (job) => {
      const lane = R.laneOf(job);
      const name = (job.plan && job.plan.name) || job.kind;
      events.push({ phase: 'start', lane, name, t: Date.now() });
      await new Promise((r) => setTimeout(r, Number(job.holdMs) || HOLD));
      events.push({ phase: 'end', lane, name, t: Date.now() });
      return { success: true };
    };

    const wins = (lane, name) => {
      const out = [];
      let open = null;
      for (const e of events) {
        if (e.lane !== lane || e.name !== name) continue;
        if (e.phase === 'start') open = { start: e };
        else if (open) { out.push({ start: open.start, end: e }); open = null; }
      }
      return out;
    };
    const overlap = (a, b) => a.start.t < b.end.t && b.start.t < a.end.t;

    events.length = 0;
    const a = R.enqueueJob({ kind: 'plan', plan: { id: 'A', name: '采集A' } });
    const b = R.enqueueJob({ kind: 'plan', plan: { id: 'B', name: '采集B' } });
    const longC = R.enqueueJob({ kind: 'classify', plan: { id: 'C', name: '长分类' }, holdMs: 300 });
    await Promise.all([a, b, longC]);

    const aW = wins('collect', '采集A')[0];
    const bW = wins('collect', '采集B')[0];
    const cW = wins('classify', '长分类')[0];
    check('两个采集任务都执行了', [Boolean(aW), Boolean(bW)], [true, true]);
    check('同车道采集任务不重叠（串行）', overlap(aW, bW), false);
    check('分类与采集时间重叠（并行）', overlap(cW, aW), true);
    check('采集未被长分类拖住，先跑完', aW.end.t < cW.end.t && bW.end.t < cW.end.t, true);

    events.length = 0;
    const p1 = R.enqueueJob({ kind: 'plan', plan: { id: 'X', name: '采集X' }, holdMs: 120 });
    const p2 = R.enqueueJob({ kind: 'plan', plan: { id: 'Y', name: '采集Y' } });
    const p3 = R.enqueueJob({ kind: 'classify', plan: { id: 'Z', name: '分类Z' } });
    await new Promise((r) => setTimeout(r, 30));
    check('两条车道各有一个在跑', R.queueTotals().active, 2);
    check('采集车道还有 1 个在等', R.queueTotals().pending, 1);
    await Promise.all([p1, p2, p3]);
    check('跑完后队列清空', R.queueTotals(), { active: 0, pending: 0 });

    // 同一时间的多条计划：一次 tick 全部入队，且串行执行
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const hhmm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    const weekday = (now.getDay() + 6) % 7;
    const sameTime = ['甲', '乙', '丙'].map((name, i) => {
      const plan = R.normalizePlan({
        name: `同时间${name}`,
        platform: 'admuse',
        schedule_time: hhmm,
        weekdays: [weekday],
        download_root: 'D:\\跑量素材'
      }, null, undefined);
      plan.id = `SAME-${i}`;
      return plan;
    });
    check('三条计划都判定为到点', sameTime.map((p) => R.isDue(p, now)), [true, true, true]);

    R.savePlans(sameTime);
    events.length = 0;
    R.tickScheduler();
    const afterTick = R.queueTotals();
    check('一次 tick 把三条同时间计划全部入队', afterTick.active + afterTick.pending, 3);
    R.tickScheduler();
    const afterSecond = R.queueTotals();
    check('同一分钟重复 tick 不重复入队',
      afterSecond.active + afterSecond.pending, afterTick.active + afterTick.pending);

    await new Promise((r) => setTimeout(r, HOLD * 3 + 200));
    const w = ['甲', '乙', '丙'].map((n) => wins('collect', `同时间${n}`)[0]);
    check('三条同时间计划都执行了', w.every(Boolean), true);
    check('三者两两不重叠（排队执行）',
      overlap(w[0], w[1]) || overlap(w[1], w[2]) || overlap(w[0], w[2]), false);
    check('队列最终清空', R.queueTotals(), { active: 0, pending: 0 });
  } finally {
    fs.rmSync(tmpAppDir, { recursive: true, force: true });
  }
}

runQueueChecks().then(() => {
  console.log(`\n${'='.repeat(52)}`);
  console.log(`通过 ${pass} 项，失败 ${fail} 项`);
  console.log('='.repeat(52));
  process.exit(fail === 0 ? 0 : 1);
}).catch((err) => {
  console.error('队列自检异常:', err);
  process.exit(1);
});

