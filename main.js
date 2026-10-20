/**
 * 素材采集分类工具 · Electron 主进程
 *
 * 独立工具，不依赖任何外部系统。功能：
 *   1. 下载中心   —— 妙思 / 有米云 / 热云 / 内部系统 四个渠道抓取素材
 *   2. 素材治理   —— API 极速分类 / 画面方向拆解 / API 精细分类
 *   3. 自动化计划 —— 定时自动「下载 + 分类」一条龙，可配执行时间与星期
 *   4. 运行历史   —— 每次运行的记录与完整日志
 *
 * 任务走「采集 / 分类」两条独立车道：各自内部串行，两条之间并行。
 * 采集依赖账号管家的浏览器登录态，必须串行；分类是纯 API 调用，可以同时跑。
 */

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');

// ============================ 路径与常量 ============================

const APP_DIR = __dirname;
const USER_DATA_DIR = path.join(APP_DIR, 'userdata');
const CLASSIFY_ENGINE_DIR = path.join(APP_DIR, 'classify-engine');
const LOG_DIR = path.join(USER_DATA_DIR, 'logs');

const CONFIG_PATH = path.join(USER_DATA_DIR, 'classify_config.json');
const PLANS_PATH = path.join(USER_DATA_DIR, 'capture_plans.json');
const RUNS_PATH = path.join(USER_DATA_DIR, 'run_history.json');

const PURE_API_SCRIPT = path.join(CLASSIFY_ENGINE_DIR, 'scripts', 'classify_pure_api.py');
const DIRECTION_SCRIPT = path.join(CLASSIFY_ENGINE_DIR, 'scripts', 'classify_by_direction.py');
const BUNDLED_PYTHON = path.join(CLASSIFY_ENGINE_DIR, 'runtime', 'python', 'python.exe');

const DEFAULT_EXPORT_DIR = 'D:\\素材库\\外部素材';

const PLATFORM_LABELS = {
  admuse: '妙思',
  appgrowing: '有米云',
  reyun: '热云',
  internal: '内部系统'
};

// 平台 -> 下载脚本名（多数平台是 {platform}_download.py，内部系统例外）
const DOWNLOAD_SCRIPTS = {
  internal: 'internal_export.py'
};

// 必须与 classify_pure_api.py 的 SUPPORTED_PLATFORMS 一致
const CLASSIFY_PLATFORMS = ['admuse', 'appgrowing', 'reyun', 'internal'];

const MAX_RUN_HISTORY = 200;      // 运行历史保留条数
const SCHEDULER_TICK_MS = 30000;  // 调度器扫描间隔：30 秒
const SCHEDULER_START_DELAY_MS = 5000;
const PROCESS_TIMEOUT_MINUTES = 120;

const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

fs.mkdirSync(USER_DATA_DIR, { recursive: true });
fs.mkdirSync(LOG_DIR, { recursive: true });

let mainWindow;
let schedulerTimer = null;
let schedulerStartedAt = null;

// ============================ 通用小工具 ============================

function nowIso() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function makeId(prefix) {
  const stamp = nowIso().replace(/[-: ]/g, '');
  return `${prefix}-${stamp}-${Math.random().toString(36).slice(2, 7)}`;
}

function platformLabel(platform) {
  return PLATFORM_LABELS[platform] || platform;
}

function formatBatchDate(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}${month}${day}`;
}

/** 解析 YYYY-MM-DD / YYYY-MM-DD HH:mm(:ss) 为本地时间 Date；失败返回 null。 */
function parseLocalDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatIsoDate(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 内部系统的口径是「昨天」，即运行日的前一天。 */
function dayBefore(date) {
  const copy = new Date(date.getTime());
  copy.setDate(copy.getDate() - 1);
  return copy;
}

function dayAfter(date) {
  const copy = new Date(date.getTime());
  copy.setDate(copy.getDate() + 1);
  return copy;
}

/** 校验 YYYY-MM-DD（含真实存在性，2026-02-31 这类会被拒绝）；非法返回空串。 */
function normalizeFilterDate(value) {
  const text = String(value || '').trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return '';
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  if (Number.isNaN(date.getTime())) return '';
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return '';
  }
  return text;
}

/**
 * 由「数据日期」反推批次（目录）日期。
 *
 * 内部系统一天一个目录，且**目录名 = 运行日 = 数据日期 + 1 天**：
 * 10-12 的数据落在 20261013 里（日常定时任务在 13 号早上导出 12 号的数据）。
 * 所以按日期补采时也沿用这个口径，补 10-12 就并进 20261013，不会散落在今天的新目录里。
 * 选到今天/未来时回落到今天，避免生成尚未到来的目录。
 */
function batchDateForFilter(filterDate, now = new Date()) {
  const dataDate = parseLocalDate(normalizeFilterDate(filterDate));
  if (!dataDate) return formatBatchDate(now);
  const batch = formatBatchDate(dayAfter(dataDate));
  const todayBatch = formatBatchDate(now);
  return batch > todayBatch ? todayBatch : batch;
}

/**
 * 推算出某次运行实际使用的日期上下文，用于复跑时保持日期一致。
 * 优先用记录里存下来的值；老记录没有这些字段时按开始时间倒推。
 */
function resolveRunDates(run, platform) {
  const runDate = parseLocalDate(run.started_at) || new Date();
  const batchDate = /^\d{8}$/.test(String(run.batch_date || ''))
    ? String(run.batch_date)
    : formatBatchDate(runDate);
  let filterDate = /^\d{4}-\d{2}-\d{2}$/.test(String(run.filter_date || ''))
    ? String(run.filter_date)
    : '';
  if (!filterDate && platform === 'internal') {
    filterDate = formatIsoDate(dayBefore(runDate));
  }
  return { batchDate, filterDate };
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    return fallback;
  }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2), 'utf8');
}

function normalizeExportDir(value) {
  const candidate = String(value || '').trim();
  return candidate && path.isAbsolute(candidate)
    ? path.normalize(candidate)
    : DEFAULT_EXPORT_DIR;
}

/**
 * 解析计划真正使用的下载根目录。
 * 标记了 use_global_root 的计划跟随「下载中心」的导出目录实时取值，
 * 这样改一处导出目录就能同时作用于手动下载和定时计划。
 */
function resolvePlanRoot(plan) {
  if (plan && plan.use_global_root === true) {
    return normalizeExportDir(readConfig().export_dir);
  }
  return normalizeExportDir(plan && plan.download_root);
}

function pythonCommand() {
  return fs.existsSync(BUNDLED_PYTHON) ? BUNDLED_PYTHON : 'python';
}

function send(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send(channel, payload);
  }
}

// ============================ 批次目录布局 ============================

/**
 * 同一天、同一平台只用一个日期目录。
 *
 * 同一平台一天会按不同方向采集多次（如 保险 / APP），这些采集**并进同一个日期目录**，
 * 产品在「分类结果」下自然分成多个子目录；不再生成 20260928-2、-3 这类后缀目录。
 * 合并是安全的：下载脚本会跳过已存在的文件（按文件名），分类审计是追加式且带断点续跑。
 *
 * 例外：**内部系统**会在日期下再分「图片 / 视频」两层（mediaType）。
 * 图片走「下载 + 分类」，视频只下载不分类，两者产物结构不同，混在一个目录里没法用。
 * 其他平台保持「日期/原始素材」不变。
 */
function buildDownloadLayout(exportRoot, platform, batchDate = formatBatchDate(), mediaType = '') {
  const platformDir = path.join(exportRoot, platformLabel(platform));
  const batchName = batchDate;
  const dateDir = path.join(platformDir, batchName);
  const mediaDir = (platform === 'internal' && mediaType)
    ? path.join(dateDir, mediaType)
    : dateDir;
  return {
    batchDate,
    batchName,
    // batchDir 始终是「本次运行的产出根」，下游按 batchDir/分类结果 取件仍然成立
    batchDir: mediaDir,
    dateDir,
    mediaDir,
    mediaType: (platform === 'internal' && mediaType) ? mediaType : '',
    rawDir: path.join(mediaDir, '原始素材'),
    reportDir: path.join(mediaDir, '报表'),
    classificationDir: path.join(mediaDir, '分类结果')
  };
}

function resolveClassificationOutput(source, requestedOutput) {
  if (requestedOutput) return path.resolve(String(requestedOutput));
  const sourcePath = path.resolve(String(source));
  return path.basename(sourcePath) === '原始素材'
    ? path.join(path.dirname(sourcePath), '分类结果')
    : path.join(sourcePath, '分类结果');
}

// 图片扩展名：分类只处理这些
const IMAGE_EXT_PATTERN = /\.(jpe?g|png|webp|bmp|gif)$/i;
// 媒体扩展名：图片 + 视频。内部系统视频模式产出的是视频，用图片口径统计会算成 0。
const MEDIA_EXT_PATTERN = /\.(jpe?g|png|webp|bmp|gif|mp4|mov|avi|mkv|webm|m4v)$/i;

function countImages(dir) {
  return countByPattern(dir, IMAGE_EXT_PATTERN);
}

/** 统计图片 + 视频，用于「本次新增 / 当天累计」这类不区分类型的计数。 */
function countMedia(dir) {
  return countByPattern(dir, MEDIA_EXT_PATTERN);
}

function countByPattern(dir, pattern) {
  let total = 0;
  const walk = (current) => {
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (pattern.test(entry.name)) total += 1;
    }
  };
  if (fs.existsSync(dir)) walk(dir);
  return total;
}

/**
 * 列出目录下所有图片/视频的相对路径集合。
 * 因为同一天的多次采集会并进同一个目录，需要靠前后快照的差集
 * 才能算出「本次真正新增了多少个」，否则会把当天累计当成本次下载量。
 */
function listMediaPaths(dir) {
  const names = new Set();
  const walk = (current, prefix) => {
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(path.join(current, entry.name), relative);
      else if (MEDIA_EXT_PATTERN.test(entry.name)) names.add(relative);
    }
  };
  if (fs.existsSync(dir)) walk(dir, '');
  return names;
}

// ============================ 下载进度解析 ============================

const DOWNLOAD_STAGE_ORDER = {
  connecting: 0,
  collecting: 1,
  downloading: 2,
  completed: 3
};

function getDownloadLimit(params = []) {
  for (const flag of ['--max', '--max-downloads']) {
    const index = params.indexOf(flag);
    if (index >= 0) {
      const value = Number.parseInt(params[index + 1], 10);
      if (Number.isFinite(value) && value > 0) return value;
    }
  }
  return 0;
}

function advanceDownloadStage(tracker, stage) {
  if ((DOWNLOAD_STAGE_ORDER[stage] ?? -1) >= (DOWNLOAD_STAGE_ORDER[tracker.stage] ?? -1)) {
    tracker.stage = stage;
  }
}

function parseDownloadProgressLine(tracker, rawLine) {
  const line = String(rawLine || '').trim();
  if (!line) return false;

  let changed = false;
  // 登录态用友好文案展示；下面统一回填原始行时要让位，否则会被 LOGIN_STATUS|... 原文覆盖
  let friendlyMessage = '';

  // 登录态：妙思掉线时会不会自动补登录、有没有凭据，必须让人在界面上看得见。
  // 以前凭据找不到是静默返回的，用户只看到进度卡住，不知道卡在哪。
  if (line.startsWith('LOGIN_STATUS|')) {
    const parts = line.split('|');
    const state = parts[1] || '';
    const platformName = parts[2] || '';
    const detail = parts.slice(3).join('|');
    const messages = {
      CHECKING: `${platformName}：校验登录态…`,
      READY: `${platformName}：登录态正常`,
      REQUIRED: `${platformName}：未登录，等待恢复登录态或手机验证`,
      NO_CREDENTIAL: `${platformName}：未找到 QQ 账号密码，无法自动补登录`,
      AUTO: `${platformName}：${detail || '尝试自动登录…'}`,
      AUTO_WAIT: `${platformName}：未找到登录按钮，等待人工登录`
    };
    const message = messages[state];
    if (message) {
      friendlyMessage = message;
      changed = true;
    }
  }

  let match = line.match(/下载\s+(\d+)\s*\/\s*(\d+)/i)
    || line.match(/\[(\d+)\s*\/\s*(\d+)\]/);

  if (match) {
    advanceDownloadStage(tracker, 'downloading');
    tracker.current = Number.parseInt(match[1], 10);
    tracker.total = Number.parseInt(match[2], 10);
    tracker.actualTotalKnown = true;
    changed = true;
  }

  match = line.match(/共采集\s*(\d+)\s*个素材/i)
    || line.match(/共发现\s*(\d+)\s*条数据/i);
  if (match) {
    advanceDownloadStage(tracker, 'collecting');
    tracker.total = Number.parseInt(match[1], 10);
    tracker.current = 0;
    tracker.actualTotalKnown = true;
    changed = true;
  }

  match = line.match(/Downloaded:\s*(\d+),\s*Failed:\s*(\d+)/i);
  if (match) {
    tracker.current = Number.parseInt(match[1], 10);
    tracker.failed = Number.parseInt(match[2], 10);
    tracker.total = tracker.current + tracker.failed;
    tracker.actualTotalKnown = true;
    changed = true;
  }

  match = line.match(/处理完成！成功:\s*(\d+),\s*跳过:\s*(\d+),\s*失败:\s*(\d+)/);
  if (match) {
    const success = Number.parseInt(match[1], 10);
    const skipped = Number.parseInt(match[2], 10);
    tracker.failed = Number.parseInt(match[3], 10);
    tracker.current = success + skipped + tracker.failed;
    tracker.total = tracker.current;
    tracker.actualTotalKnown = true;
    changed = true;
  }

  if (/开始采集|筛选已应用|开始解析Excel|正在全选|点击查询|连接 Chrome|连接浏览器/.test(line)) {
    advanceDownloadStage(tracker, 'collecting');
    changed = true;
  }

  if (/开始下载|下载成功|已保存|\[OK\].*score=/.test(line)) {
    advanceDownloadStage(tracker, 'downloading');
    changed = true;
  }

  if (changed) {
    tracker.message = friendlyMessage || line.slice(0, 140);
  }
  return changed;
}

// ============================ 任务队列（采集 / 分类 双车道） ============================
//
// 采集与分类互不冲突，所以拆成两条**独立并行**的车道：
//   collect  采集下载 —— 共用账号管家的浏览器登录态，必须串行，否则会互相抢标签页
//   classify 分类整理 —— 纯视觉 API 调用，不碰浏览器，可以和采集同时进行
//
// 每条车道**内部仍然串行**（同一时刻只跑一个子进程）：
// 分类不并行是因为脚本自身已开 10 路并发，两条分类叠加会把网关压垮
// （实测并发 16 反而比 10 更慢）。

const LANES = ['collect', 'classify'];

/**
 * 任务归属哪条车道。
 * 分类类任务（kind 为 classify / plan-classify）一律走分类车道，
 * 也允许用 lane 显式指定——两条都认，避免调用方漏写 lane 时掉进采集车道。
 */
function laneOf(job) {
  if (!job) return 'collect';
  if (job.lane === 'classify') return 'classify';
  if (job.kind === 'classify' || job.kind === 'plan-classify') return 'classify';
  return 'collect';
}

function createLane() {
  return { pending: [], active: null, child: null, pumping: false };
}

const lanes = { collect: createLane(), classify: createLane() };

function describeJob(job) {
  return {
    kind: job.kind,
    lane: laneOf(job),
    planId: job.plan ? job.plan.id : '',
    planName: job.plan ? job.plan.name : '',
    platform: job.platform || '',
    platformLabel: job.platform ? platformLabel(job.platform) : '',
    trigger: job.trigger || 'manual'
  };
}

function showQueueState() {
  const snapshot = {};
  for (const lane of LANES) {
    const state = lanes[lane];
    snapshot[lane] = {
      active: state.active ? describeJob(state.active) : null,
      pending: state.pending.map(describeJob)
    };
  }
  send('queue-state', snapshot);
}

function enqueueJob(job) {
  const lane = laneOf(job);
  return new Promise((resolve, reject) => {
    lanes[lane].pending.push({ ...job, lane, resolve, reject });
    showQueueState();
    pumpLane(lane);
  });
}

/** 推进某条车道：空则取下一条；已在跑就等它结束后自己再调用一次。 */
function pumpLane(lane) {
  const state = lanes[lane];
  if (state.active || state.pending.length === 0 || state.pumping) return;
  state.pumping = true;
  setImmediate(async () => {
    state.pumping = false;
    if (state.active || state.pending.length === 0) return;
    const job = state.pending.shift();
    state.active = job;
    showQueueState();
    try {
      job.resolve(await runJob(job));
    } catch (error) {
      job.reject(error);
    } finally {
      state.active = null;
      state.child = null;
      showQueueState();
      pumpLane(lane);
    }
  });
}

function runJob(job) {
  if (job.kind === 'plan-classify') return runPlanClassifyJob(job);
  if (job.kind === 'classify') return runClassifyJob(job);
  if (job.kind === 'plan') return runPlanJob(job);
  return runCaptureJob(job);
}

/**
 * 中止正在运行的子进程。
 * 不传 lane 时中止全部车道（关窗退出用），传了就只停那一条。
 */
function abortActiveChild(lane) {
  const targets = lane ? [lanes[lane]] : LANES.map((name) => lanes[name]);
  let aborted = false;
  for (const state of targets) {
    const child = state.child;
    if (child && !child.killed) {
      child.__stopRequested = true;
      try { child.kill(); } catch (_) { /* 进程可能已自行退出 */ }
      aborted = true;
    }
  }
  return aborted;
}

/** 把某条车道里第一个符合条件的任务撤下来（还没开始跑的情况）。 */
function dropQueuedJob(predicate, result, lane) {
  const targets = lane ? [lanes[lane]] : LANES.map((name) => lanes[name]);
  for (const state of targets) {
    const index = state.pending.findIndex(predicate);
    if (index < 0) continue;
    const [removed] = state.pending.splice(index, 1);
    removed.resolve(result);
    showQueueState();
    return true;
  }
  return false;
}

/** 队列总览：还有多少任务在跑或排队，用于界面提示。 */
function queueTotals() {
  return LANES.reduce((acc, lane) => {
    acc.active += lanes[lane].active ? 1 : 0;
    acc.pending += lanes[lane].pending.length;
    return acc;
  }, { active: 0, pending: 0 });
}

// ============================ 子进程执行器 ============================

/**
 * 启动子进程，输出按行回调，带超时与停止支持。
 * onLine(line) 每行调用一次。
 */
function spawnChild({ command, args, cwd, env, onLine, lane = 'collect' }) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd, env, windowsHide: true });
    // 记在所属车道上，停止按钮才能只停对的那一条
    lanes[laneOf({ lane })].child = child;
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');

    let buffer = '';
    const consume = (text, flush = false) => {
      buffer += text;
      const lines = buffer.split(/\r?\n/);
      buffer = flush ? '' : (lines.pop() || '');
      for (const line of lines) onLine(line);
      if (flush && buffer) {
        onLine(buffer);
        buffer = '';
      }
    };

    child.stdout.on('data', (data) => consume(data));
    child.stderr.on('data', (data) => consume(data));

    const timeout = setTimeout(() => {
      child.__timedOut = true;
      try { child.kill(); } catch (_) { /* 已退出 */ }
    }, PROCESS_TIMEOUT_MINUTES * 60 * 1000);

    child.on('error', (error) => {
      clearTimeout(timeout);
      resolve({ failedToStart: true, error: error.message });
    });

    child.on('close', (code) => {
      clearTimeout(timeout);
      consume('', true);
      resolve({
        code,
        stopped: Boolean(child.__stopRequested),
        timedOut: Boolean(child.__timedOut)
      });
    });
  });
}

function downloadEnv(layout, exportRoot, plan = {}) {
  const background = plan.background_mode === false ? 'false' : 'true';
  const env = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    MATERIAL_DOWNLOADER_EXPORT_DIR: exportRoot,
    MATERIAL_DOWNLOADER_BATCH_DATE: layout.batchDate,
    MATERIAL_DOWNLOADER_BATCH_DIR: layout.batchDir,
    MATERIAL_DOWNLOADER_RAW_DIR: layout.rawDir,
    MATERIAL_DOWNLOADER_REPORT_DIR: layout.reportDir,
    MATERIAL_DOWNLOADER_CLASSIFICATION_DIR: layout.classificationDir,
    MATERIAL_DOWNLOADER_BACKGROUND_MODE: background,
    MATERIAL_DOWNLOADER_HEADLESS: background
  };
  if (plan.filter_date) env.MATERIAL_DOWNLOADER_FILTER_DATE = String(plan.filter_date);
  if (plan.internal_material_type) {
    env.MATERIAL_DOWNLOADER_INTERNAL_MATERIAL_TYPE = String(plan.internal_material_type);
  }
  if (plan.internal_keyword) {
    env.MATERIAL_DOWNLOADER_INTERNAL_KEYWORD = String(plan.internal_keyword);
  }
  if (plan.internal_top_n !== undefined && plan.internal_top_n !== null && plan.internal_top_n !== '') {
    env.MATERIAL_DOWNLOADER_INTERNAL_TOP_N = String(plan.internal_top_n);
  }
  return env;
}

// ============================ 步骤一：下载 ============================

async function executeDownload({ platform, params, exportDir, plan = {}, logPath, batchDate, lane = 'collect' }) {
  const scriptName = DOWNLOAD_SCRIPTS[platform] || `${platform}_download.py`;
  const scriptPath = path.join(APP_DIR, 'scripts', scriptName);
  const targetExportDir = normalizeExportDir(exportDir);
  // batchDate 由调用方给定：复跑时沿用原运行日期，普通运行就是当天
  // 内部系统按素材类型在日期下再分一层（图片 / 视频）
  const mediaType = platform === 'internal'
    ? String(plan.internal_material_type || '')
    : '';
  const layout = buildDownloadLayout(
    targetExportDir, platform, batchDate || formatBatchDate(), mediaType
  );

  if (!fs.existsSync(scriptPath)) {
    return { success: false, error: `下载脚本不存在：${scriptPath}` };
  }

  // 视频只下载不分类，不建分类结果目录（免得留个空壳）
  const dirsToCreate = [layout.rawDir, layout.reportDir];
  if (layout.mediaType !== '视频') dirsToCreate.push(layout.classificationDir);
  for (const directory of dirsToCreate) {
    fs.mkdirSync(directory, { recursive: true });
  }

  // 下载前的快照：合并到同一天目录后，靠它区分「本次新增」与「当天累计」
  // 用 media 口径（图片+视频）：内部系统视频模式产出的是视频
  const beforeMedia = listMediaPaths(layout.rawDir);

  const tracker = {
    platform,
    status: 'running',
    stage: 'connecting',
    current: 0,
    total: getDownloadLimit(params),
    failed: 0,
    message: '正在启动下载进程并连接素材来源…',
    startedAt: Date.now(),
    actualTotalKnown: false,
    message$ok: true
  };

  const emit = (patch = {}) => {
    Object.assign(tracker, patch);
    send('download-progress', {
      platform: tracker.platform,
      status: tracker.status,
      stage: tracker.stage,
      current: tracker.current,
      total: tracker.total,
      failed: tracker.failed,
      message: tracker.message,
      startedAt: tracker.startedAt
    });
  };

  const header = `[批次] ${layout.batchName}\n` +
    (plan.filter_date ? `[日期] 数据日期 ${plan.filter_date}（内部系统按该日期查询表格）\n` : '') +
    `[目录] 原始素材：${layout.rawDir}\n` +
    `[目录] 报表：${layout.reportDir}\n` +
    `[目录] 分类结果：${layout.classificationDir}\n`;
  appendRunLog(logPath, header);
  send('download-log', { platform, text: header });
  emit();

  const outcome = await spawnChild({
    command: pythonCommand(),
    args: [scriptPath, ...(Array.isArray(params) ? params : [])],
    cwd: path.join(APP_DIR, 'scripts'),
    env: downloadEnv(layout, targetExportDir, plan),
    lane,
    onLine: (line) => {
      appendRunLog(logPath, line);
      send('download-log', { platform, text: line + '\n' });
      if (parseDownloadProgressLine(tracker, line)) emit();
    }
  });

  if (outcome.failedToStart) {
    emit({ status: 'failed', message: `下载进程启动失败：${outcome.error}` });
    return { success: false, error: outcome.error, layout };
  }
  if (outcome.stopped) {
    emit({ status: 'stopped', message: '任务已由用户停止' });
    return { success: false, stopped: true, layout };
  }
  if (outcome.timedOut) {
    emit({ status: 'failed', message: `下载超时（超过 ${PROCESS_TIMEOUT_MINUTES} 分钟）` });
    return { success: false, error: '下载超时', layout };
  }
  if (outcome.code !== 0) {
    emit({ status: 'failed', message: `下载进程异常退出（代码 ${outcome.code}）` });
    return { success: false, error: `下载进程异常退出（代码 ${outcome.code}）`, layout };
  }

  if (!tracker.actualTotalKnown) {
    tracker.total = tracker.current + tracker.failed;
  }
  // found = 当天该目录的累计个数（图片+视频；图片会拿去分类，视频只下载）
  // added = 本次采集真正新增的个数（同一天的多次采集会并进同一目录）
  const found = countMedia(layout.rawDir);
  const afterMedia = listMediaPaths(layout.rawDir);
  const added = beforeMedia
    ? [...afterMedia].filter((name) => !beforeMedia.has(name)).length
    : found;
  emit({
    status: 'completed',
    stage: 'completed',
    message: `下载任务已完成，本次新增 ${added} 张（当天累计 ${found} 张）`
  });

  return {
    success: true,
    layout,
    found,
    added,
    downloaded: tracker.current,
    failed: tracker.failed
  };
}

// ============================ 步骤二：分类 ============================

async function executeClassify({ source, output, platform, parallel = 5, minConfidence = 0.85,
                                 fileAction = 'copy', refreshApi = false, logPath,
                                 lane = 'classify' }) {
  if (!fs.existsSync(source)) {
    return { success: false, error: `图片目录不存在：${source}` };
  }
  if (!fs.existsSync(PURE_API_SCRIPT)) {
    return { success: false, error: '纯API分类脚本缺失' };
  }

  const cfg = readConfig();
  if (!cfg.api_endpoint || !cfg.model_id || !cfg.api_key) {
    return { success: false, error: '分类依赖模型 API，请先在“素材治理”页配置 API' };
  }

  const resolvedOutput = resolveClassificationOutput(source, output);
  fs.mkdirSync(resolvedOutput, { recursive: true });

  // 重跑已分类过的批次时，place_file() 遇到同名文件会生成 xxx_1.jpg，
  // 而 --action copy 又不会移除源文件，于是产出整套重复文件。
  // 这里只提示、不动用户文件，由用户决定改名/清空还是换个输出目录。
  const existingImages = countImages(resolvedOutput);
  if (existingImages > 0) {
    const warning =
      `⚠ 输出目录已有 ${existingImages} 张图：${resolvedOutput}\n` +
      '   重跑会为同名文件生成 _1 / _2 副本。建议先把旧结果改名或清空，' +
      '或在界面上指定一个空的「结果输出目录」。\n';
    appendRunLog(logPath, warning);
    send('classify-log', { type: 'stderr', data: warning });
  }

  const args = [
    PURE_API_SCRIPT,
    '--source', source,
    '--output', resolvedOutput,
    '--config', CONFIG_PATH,
    '--platform', CLASSIFY_PLATFORMS.includes(platform) ? platform : 'internal',
    '--parallel', String(Math.max(1, Math.min(10, Number(parallel) || 5))),
    '--min-confidence', String(Math.max(0, Math.min(1, Number(minConfidence)))),
    '--action', fileAction === 'move' ? 'move' : 'copy'
  ];
  if (refreshApi) args.push('--refresh-api');

  const outcome = await spawnChild({
    command: pythonCommand(),
    args,
    cwd: CLASSIFY_ENGINE_DIR,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
    lane,
    onLine: (line) => {
      appendRunLog(logPath, line);
      send('classify-log', { type: 'stdout', data: line + '\n' });
    }
  });

  if (outcome.failedToStart) {
    return { success: false, error: outcome.error, output: resolvedOutput };
  }
  if (outcome.stopped) {
    return { success: false, stopped: true, output: resolvedOutput };
  }
  if (outcome.timedOut) {
    return { success: false, error: '分类超时', output: resolvedOutput };
  }

  const auditExcel = path.join(resolvedOutput, '_API分类调试审计.xlsx');
  return {
    success: outcome.code === 0,
    code: outcome.code,
    output: resolvedOutput,
    auditExcel: fs.existsSync(auditExcel) ? auditExcel : '',
    error: outcome.code === 0 ? '' : `分类进程异常退出（代码 ${outcome.code}）`
  };
}

// ============================ 手动任务 ============================

/** 手动下载（渲染层 runDownload），返回结构与改造前一致。 */
async function runCaptureJob(job) {
  const logPath = job.logPath || path.join(LOG_DIR, `${makeId('RUN')}.log`);
  // 采集车道是串行的：前面还有任务在跑/在排，就明确告诉用户已排队
  if (lanes.collect.active || lanes.collect.pending.length > 1) {
    const text = '已加入采集队列，等待前一个任务完成…\n';
    appendRunLog(logPath, text);
    send('download-log', { platform: job.platform, text });
  }

  const result = await executeDownload({ ...job, logPath });
  return {
    success: result.success,
    stopped: Boolean(result.stopped),
    output: result.error || '',
    error: result.error || '',
    paths: result.layout || null,
    found: result.found || 0
  };
}

async function runClassifyJob(job) {
  if (job.script) return runDirectionJob(job);
  return executeClassify({
    source: job.source,
    output: job.output,
    platform: job.platform,
    parallel: job.parallel,
    minConfidence: job.minConfidence,
    fileAction: job.fileAction,
    logPath: job.logPath
  });
}

async function runDirectionJob(job) {
  if (!fs.existsSync(job.source)) {
    return { success: false, error: `图片目录不存在：${job.source}` };
  }
  if (!fs.existsSync(DIRECTION_SCRIPT)) {
    return { success: false, error: '纯方向分类脚本缺失' };
  }

  const cfg = readConfig();
  if (!cfg.api_endpoint || !cfg.model_id || !cfg.api_key) {
    return { success: false, error: '方向拆解依赖模型 API，请先配置' };
  }

  fs.mkdirSync(job.output, { recursive: true });

  const args = [
    DIRECTION_SCRIPT,
    '--source', job.source,
    '--output', job.output,
    '--config', CONFIG_PATH,
    '--parallel', String(job.parallel),
    '--min-confidence', String(job.minConfidence),
    '--action', job.fileAction
  ];
  if (job.dryRun) args.push('--dry-run');

  const outcome = await spawnChild({
    command: pythonCommand(),
    args,
    cwd: CLASSIFY_ENGINE_DIR,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
    lane: 'classify',
    onLine: (line) => {
      appendRunLog(job.logPath, line);
      send('classify-log', { type: 'stdout', data: line + '\n' });
    }
  });

  if (outcome.failedToStart) return { success: false, error: outcome.error, output: job.output };
  if (outcome.stopped) return { success: false, stopped: true, output: job.output };
  if (outcome.timedOut) return { success: false, error: '方向拆解超时', output: job.output };
  return {
    success: outcome.code === 0,
    code: outcome.code,
    output: job.output,
    error: outcome.code === 0 ? '' : `方向拆解进程异常退出（代码 ${outcome.code}）`
  };
}

// ============================ 计划任务：下载 + 分类一条龙 ============================

async function runPlanJob(job) {
  const plan = job.plan;
  const runId = makeId('RUN');
  const logPath = path.join(LOG_DIR, `${runId}.log`);
  const startedAt = Date.now();
  const platform = plan.platform;

  // 本次运行实际使用的日期上下文。复跑时 plan.batch_date / plan.filter_date 已被指定，
  // 于是沿用原日期；普通运行则取当天，并记下内部系统真正会查询的那一天。
  // 内部系统固定了「数据日期」时，目录按数据日期 + 1 走（与日常定时任务的口径一致）。
  const runDate = new Date(startedAt);
  const planFilterDate = platform === 'internal' ? normalizeFilterDate(plan.filter_date) : '';
  const batchDate = /^\d{8}$/.test(String(plan.batch_date || ''))
    ? String(plan.batch_date)
    : (planFilterDate ? batchDateForFilter(planFilterDate, runDate) : formatBatchDate(runDate));
  const filterDate = planFilterDate
    ? planFilterDate
    : (platform === 'internal' ? formatIsoDate(dayBefore(runDate)) : '');

  const record = {
    id: runId,
    plan_id: plan.id,
    plan_name: plan.name,
    platform,
    platform_label: platformLabel(platform),
    trigger: job.trigger || 'schedule',
    status: 'running',
    started_at: nowIso(),
    finished_at: '',
    duration_ms: 0,
    batch_date: batchDate,
    filter_date: filterDate,
    batch_dir: '',
    raw_dir: '',
    classification_dir: '',
    found: 0,
    downloaded: 0,
    failed: 0,
    classified: false,
    classify_output: '',
    classify_error: '',
    message: '任务已启动',
    log: logPath
  };
  upsertRun(record);
  send('run-started', record);
  const triggerLabel = { schedule: '定时触发', manual: '手动触发', rerun: '按原日期重跑' }[record.trigger]
    || '手动触发';
  send('download-log', {
    platform,
    text: `\n${'='.repeat(52)}\n[计划] 「${plan.name}」开始（${triggerLabel}）`
      + `\n日期：批次 ${batchDate}${filterDate ? ` · 查询 ${filterDate}` : ''}\n${'='.repeat(52)}\n`
  });

  try {
    const downloadResult = await executeDownload({
      platform,
      params: job.params || [],
      exportDir: resolvePlanRoot(plan),
      plan,
      logPath,
      batchDate
    });

    if (downloadResult.layout) {
      record.batch_dir = downloadResult.layout.batchDir;
      record.raw_dir = downloadResult.layout.rawDir;
      record.classification_dir = downloadResult.layout.classificationDir;
    }

    if (downloadResult.stopped) {
      record.status = 'stopped';
      record.message = '下载被用户停止';
      return finishRun(record, plan, startedAt);
    }

    if (!downloadResult.success) {
      record.status = 'failed';
      record.message = downloadResult.error || '下载失败';
      appendRunLog(logPath, `[错误] ${record.message}`);
      return finishRun(record, plan, startedAt);
    }

    record.found = downloadResult.found || 0;
    record.added = downloadResult.added || 0;
    record.downloaded = downloadResult.downloaded || 0;
    record.failed = downloadResult.failed || 0;

    // 内部系统的视频模式：只下载不分类。
    // 视频不进视觉关键词分类流程（提示词、审计、目录层级都是为图片设计的），
    // 产物落在「日期/视频/原始素材」，结构上也没有分类结果目录。
    const isVideoRun = platform === 'internal'
      && String(plan.internal_material_type || '') === '视频';
    if (isVideoRun) {
      record.status = 'success';
      record.message = `下载完成：本次新增 ${record.added} 个（当天累计 ${record.found} 个；`
        + `视频模式只下载不分类）`;
      appendRunLog(logPath, `[完成] ${record.message}`);
      return finishRun(record, plan, startedAt);
    }

    // 一个素材都没拿到就不再白跑一次分类
    if (record.found === 0) {
      record.status = 'warning';
      record.message = '下载完成，但原始素材目录没有素材，已跳过分类';
      appendRunLog(logPath, `[警告] ${record.message}`);
      return finishRun(record, plan, startedAt);
    }

    if (plan.classify_enabled === false) {
      record.status = 'success';
      record.message = `下载完成：本次新增 ${record.added} 个（当天累计 ${record.found} 个，已按计划跳过分类）`;
      appendRunLog(logPath, `[完成] ${record.message}`);
      return finishRun(record, plan, startedAt);
    }

    // 分类阶段拆到「分类」车道单独执行，采集车道立刻释放去接下一条计划。
    // 两条车道并行，所以下载不必等分类跑完，同一时间的多个计划能连续开跑。
    appendRunLog(
      logPath,
      `\n[一条龙] 下载完成（本次新增 ${record.added} 个，当天累计 ${record.found} 个），`
      + `分类已排入分类车道（与采集并行，不阻塞后续计划）`
    );
    send('download-log', {
      platform,
      text: `\n[一条龙] 本次新增 ${record.added} 张素材，分类任务排入分类车道…\n`
    });

    enqueueJob({
      kind: 'plan-classify',
      lane: 'classify',
      plan,
      record,
      platform,
      source: downloadResult.layout.rawDir,
      output: downloadResult.layout.classificationDir,
      logPath,
      startedAt
    }).catch(() => { /* 失败已写入运行历史 */ });

    return { success: true, deferredClassify: true };
  } catch (error) {
    record.status = 'failed';
    record.message = error.message || String(error);
    appendRunLog(logPath, `[异常] ${record.message}`);
    return finishRun(record, plan, startedAt);
  }
}

/**
 * 计划任务的「分类」阶段，跑在分类车道上。
 * 与下载拆开后，采集车道不用等分类；运行记录会一直保持 running 到这一步收尾。
 */
async function runPlanClassifyJob(job) {
  const { plan, record, platform, source, output, logPath, startedAt } = job;

  appendRunLog(logPath, `\n[一条龙] 开始分类，源目录：${source}`);
  send('classify-log', {
    type: 'stdout',
    data: `\n[一条龙] 「${plan.name}」开始分类当天累计 ${record.found} 张素材…\n`
  });

  let classifyResult;
  try {
    classifyResult = await executeClassify({
      source,
      output,
      platform,
      parallel: plan.classify_parallel,
      minConfidence: plan.classify_min_confidence,
      fileAction: plan.classify_action,
      refreshApi: plan.classify_refresh_api,
      logPath,
      lane: 'classify'
    });
  } catch (error) {
    classifyResult = { success: false, output, error: error.message || String(error) };
  }

  record.classified = Boolean(classifyResult.success);
  record.classify_output = classifyResult.output || '';

  if (classifyResult.success) {
    record.status = 'success';
    record.message = `采集分类完成：本次新增 ${record.added || 0} 张，当天累计 ${record.found} 张，`
      + `结果在 ${classifyResult.output}`;
    appendRunLog(logPath, `[完成] ${record.message}`);
  } else if (classifyResult.stopped) {
    record.status = 'stopped';
    record.message = '分类被用户停止（素材已下载完成）';
    appendRunLog(logPath, `[停止] ${record.message}`);
  } else {
    record.status = 'warning';
    record.classify_error = classifyResult.error || '分类失败';
    record.message = `素材已下载（当天累计 ${record.found} 张），但分类失败：${record.classify_error}`;
    appendRunLog(logPath, `[警告] ${record.message}`);
  }
  return finishRun(record, plan, startedAt);
}

function finishRun(record, plan, startedAt) {
  record.finished_at = nowIso();
  record.duration_ms = Date.now() - startedAt;
  upsertRun(record);

  const plans = loadPlans();
  const target = plans.find((item) => item.id === plan.id);
  if (target) {
    target.last_run_at = record.finished_at;
    target.last_status = record.status;
    target.last_message = record.message;
    target.last_found = record.found;
    target.last_run_id = record.id;
    target.updated_at = nowIso();
    savePlans(plans);
    send('plans-changed', plans);
  }

  send('run-finished', record);
  return record;
}

function appendRunLog(logPath, line) {
  if (!logPath) return;
  try {
    fs.appendFileSync(logPath, `[${nowIso()}] ${line}\n`, 'utf8');
  } catch (_) {
    /* 日志写失败不影响任务本身 */
  }
}

// ============================ 运行历史 ============================

function loadRuns() {
  const data = readJson(RUNS_PATH, []);
  return Array.isArray(data) ? data : [];
}

function saveRuns(runs) {
  writeJson(RUNS_PATH, runs.slice(0, MAX_RUN_HISTORY));
}

function upsertRun(record) {
  const runs = loadRuns();
  const index = runs.findIndex((item) => item.id === record.id);
  if (index >= 0) runs[index] = record;
  else runs.unshift(record);
  saveRuns(runs);
}

// ============================ 采集计划 ============================

function defaultPlans() {
  const now = nowIso();
  const exportDir = readConfig().export_dir;
  const seeds = [
    ['妙思每日素材采集', 'admuse', '06:30'],
    ['有米云每日素材采集', 'appgrowing', '06:40'],
    ['热云每日素材采集', 'reyun', '06:50'],
    ['内部系统每日素材采集', 'internal', '07:00']
  ];
  return seeds.map(([name, platform, scheduleTime]) => normalizePlan({
    name,
    platform,
    schedule_time: scheduleTime,
    weekdays: [0, 1, 2, 3, 4, 5, 6],
    download_root: exportDir,
    // 播种的计划默认跟随「下载中心」的导出目录，避免写死后与全局设置脱节
    use_global_root: true
  }, null, now));
}

function loadPlans() {
  const data = readJson(PLANS_PATH, null);
  if (Array.isArray(data)) return data;
  // 首次运行播种四条默认计划；用户删空后不会再塞回来
  const seeded = defaultPlans();
  savePlans(seeded);
  return seeded;
}

function savePlans(plans) {
  writeJson(PLANS_PATH, plans);
}

function normalizeWeekdays(value) {
  if (!Array.isArray(value)) return [0, 1, 2, 3, 4, 5, 6];
  const cleaned = value
    .map((item) => Number.parseInt(item, 10))
    .filter((item) => Number.isInteger(item) && item >= 0 && item <= 6);
  // 空 = 每天。这里显式展开成 7 天，避免界面上出现"一天都没选"却每天照跑的矛盾状态。
  if (cleaned.length === 0) return [0, 1, 2, 3, 4, 5, 6];
  return Array.from(new Set(cleaned)).sort((a, b) => a - b);
}

function normalizePlan(input, existing, createdAtOverride) {
  const base = existing || {
    id: makeId('PLAN'),
    created_at: createdAtOverride || nowIso(),
    last_run_at: '',
    last_status: '',
    last_message: '',
    last_found: 0,
    last_run_id: '',
    last_run_minute: ''
  };
  const plan = { ...base };
  const source = input || {};
  const platform = CLASSIFY_PLATFORMS.includes(source.platform) ? source.platform : 'admuse';

  plan.name = String(source.name || '').trim() || `${platformLabel(platform)}采集计划`;
  plan.platform = platform;
  plan.platform_label = platformLabel(platform);
  plan.enabled = source.enabled !== false;
  plan.schedule_time = /^([01]?\d|2[0-3]):[0-5]\d$/.test(String(source.schedule_time || ''))
    ? String(source.schedule_time)
    : '06:30';
  plan.weekdays = normalizeWeekdays(source.weekdays);
  // 跟随全局导出目录的计划：download_root 只留作展示快照，
  // 真正落盘的路径在每次执行时由 resolvePlanRoot 实时解析，
  // 因此改「下载中心」的导出目录即可同时作用于所有跟随的计划。
  plan.use_global_root = source.use_global_root === true;
  plan.download_root = plan.use_global_root
    ? normalizeExportDir(readConfig().export_dir)
    : normalizeExportDir(source.download_root);

  plan.keyword = String(source.keyword || '');
  plan.time_range = String(source.time_range || '7d');
  plan.industries = Array.isArray(source.industries) && source.industries.length
    ? source.industries.map(String)
    : ['insurance'];
  plan.sort = String(source.sort || '');
  plan.score = Number.parseInt(source.score, 10) || 40;
  plan.media = Array.isArray(source.media) ? source.media.map(String) : [];
  plan.traffic = Array.isArray(source.traffic) ? source.traffic.map(String) : [];
  plan.result_view = source.result_view === '素材' ? '素材' : '广告';
  plan.material_type = String(source.material_type || '全部素材类型');
  plan.material_ratio = String(source.material_ratio || '全部素材比例');
  plan.creative_type = String(source.creative_type || '图片');
  plan.max_downloads = Math.max(1, Math.min(100000, Number.parseInt(source.max_downloads, 10) || 500));
  plan.internal_material_type = source.internal_material_type === '视频' ? '视频' : '图片';
  plan.internal_keyword = String(source.internal_keyword || '');
  plan.internal_top_n = Math.max(1, Number.parseInt(source.internal_top_n, 10) || 50);
  // 内部系统专用：固定下载某一天的表格（补采/回溯用）。留空 = 跟随运行日自动取昨天。
  plan.filter_date = normalizeFilterDate(source.filter_date);
  plan.background_mode = source.background_mode !== false;

  plan.classify_enabled = source.classify_enabled !== false;
  // 默认并发 10：实测（2026-09-23）该网关下 5→10 吞吐提升 1.55x，
  // 而 16 反而更慢（单张延迟 48s→72s），所以 10 是拐点，不要再往上加。
  plan.classify_parallel = Math.max(1, Math.min(10, Number.parseInt(source.classify_parallel, 10) || 10));
  const confidence = Number.parseFloat(source.classify_min_confidence);
  plan.classify_min_confidence = Number.isFinite(confidence)
    ? Math.max(0, Math.min(1, confidence))
    : 0.85;
  plan.classify_action = source.classify_action === 'move' ? 'move' : 'copy';
  plan.classify_refresh_api = source.classify_refresh_api === true;

  plan.updated_at = nowIso();
  return plan;
}

/** 计划 -> 下载脚本 CLI 参数，语义对齐上游 CapturePlanService.downloadCommand。 */
function planDownloadParams(plan) {
  const params = [];
  const max = plan.max_downloads;
  switch (plan.platform) {
    case 'admuse':
      params.push('--industries', ...(plan.industries.length ? plan.industries : ['insurance']));
      if (plan.keyword) params.push('--keyword', plan.keyword);
      params.push('--time-range', plan.time_range);
      params.push('--sort', plan.sort || '跑量得分降序');
      params.push('--score', String(plan.score));
      params.push('--max-downloads', String(max));
      break;
    case 'appgrowing':
      params.push('--max', String(max));
      if (plan.keyword) params.push('--keyword', plan.keyword);
      if (plan.media.length) params.push('--media', plan.media.join(','));
      if (plan.traffic.length) params.push('--traffic', plan.traffic.join(','));
      params.push('--view', plan.result_view);
      params.push('--type', plan.material_type);
      params.push('--ratio', plan.material_ratio);
      if (plan.sort) params.push('--sort', plan.sort);
      break;
    case 'reyun':
      params.push('--max', String(max));
      if (plan.keyword) params.push('--keyword', plan.keyword);
      if (plan.creative_type) params.push('--type', plan.creative_type);
      if (plan.sort) params.push('--sort', plan.sort);
      break;
    case 'internal':
      // 内部系统参数走环境变量（见 downloadEnv），CLI 保持兼容
      break;
  }
  return params;
}

// ============================ 调度器 ============================

/** 对齐 Java 语义：HH:mm 精确命中 + 星期匹配（0=周一 … 6=周日，空=每天）。 */
function isDue(plan, now) {
  if (!plan.enabled) return false;
  const match = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(String(plan.schedule_time || ''));
  if (!match) return false;
  if (now.getHours() !== Number(match[1]) || now.getMinutes() !== Number(match[2])) return false;

  const weekday = (now.getDay() + 6) % 7; // JS 的 0=周日 -> 统一成 0=周一
  const weekdays = normalizeWeekdays(plan.weekdays);
  return weekdays.length === 0 || weekdays.includes(weekday);
}

function currentMinuteKey(now) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function tickScheduler() {
  const now = new Date();
  const minuteKey = currentMinuteKey(now);
  const plans = loadPlans();
  let changed = false;

  for (const plan of plans) {
    if (!isDue(plan, now)) continue;
    // 同一分钟只触发一次，避免 30 秒轮询重复排任务
    if (plan.last_run_minute === minuteKey) continue;

    plan.last_run_minute = minuteKey;
    plan.last_run_at = nowIso();
    plan.last_status = 'queued';
    plan.last_message = '已按计划排入队列';
    changed = true;

    enqueueJob({
      kind: 'plan',
      plan,
      platform: plan.platform,
      params: planDownloadParams(plan),
      trigger: 'schedule'
    }).catch(() => { /* 失败已写入运行历史 */ });

    send('download-log', {
      platform: plan.platform,
      text: `\n[计划] 「${plan.name}」到达执行时间 ${plan.schedule_time}，已排入队列\n`
    });
  }

  if (changed) {
    savePlans(plans);
    send('plans-changed', plans);
  }
  send('scheduler-tick', { at: nowIso(), ...queueTotals() });
}

function startScheduler() {
  if (schedulerTimer) return;
  schedulerStartedAt = nowIso();
  schedulerTimer = setInterval(tickScheduler, SCHEDULER_TICK_MS);
  setTimeout(() => {
    tickScheduler();
    send('scheduler-ready', { startedAt: schedulerStartedAt, intervalMs: SCHEDULER_TICK_MS });
  }, SCHEDULER_START_DELAY_MS);
}

function stopScheduler() {
  if (schedulerTimer) {
    clearInterval(schedulerTimer);
    schedulerTimer = null;
  }
}

// ============================ 配置 ============================

function readConfig() {
  const defaults = {
    api_endpoint: '',
    model_id: '',
    api_key: '',
    api_classify_prompt: '',
    export_dir: DEFAULT_EXPORT_DIR
  };
  const stored = readJson(CONFIG_PATH, {});
  return {
    ...defaults,
    ...stored,
    export_dir: normalizeExportDir(stored.export_dir)
  };
}

/** 采集依赖「账号管家」提供的浏览器登录态（CDP 9223），这里只做存在性探测。 */
function detectAccountsManager() {
  const local = process.env.LOCALAPPDATA || '';
  const candidates = [
    'C:\\Program Files\\onion\\onion.exe',
    'C:\\Program Files (x86)\\onion\\onion.exe',
    local ? path.join(local, 'onion', 'onion.exe') : '',
    local ? path.join(local, 'Programs', 'onion', 'onion.exe') : ''
  ];
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate)) return true;
    } catch (_) { /* 忽略非法路径 */ }
  }
  return false;
}

function runtimeStatus() {
  const python = pythonCommand();
  let pythonOk = false;
  try {
    const probe = spawnSync(python, ['-c', "print('PY=1')"], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 15000
    });
    pythonOk = probe.status === 0;
  } catch (_) { /* 探测失败即视为不可用 */ }

  const cfg = readConfig();
  return {
    python: pythonOk,
    api: Boolean(cfg.api_endpoint && cfg.model_id && cfg.api_key),
    pythonMode: fs.existsSync(BUNDLED_PYTHON) ? '内置 Python' : '系统 Python',
    accountsManager: detectAccountsManager()
  };
}

// ============================ 窗口与生命周期 ============================

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'renderer', 'preload.js')
    },
    frame: true,
    backgroundColor: '#eef2f6'
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  if (process.argv.includes('--dev')) {
    mainWindow.webContents.openDevTools();
  }
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(() => {
    createWindow();
    startScheduler();
  });
}

app.on('window-all-closed', () => {
  abortActiveChild();
  stopScheduler();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

// ============================ IPC：下载 / 分类 ============================

ipcMain.handle('run-download', async (_event, { platform, params, exportDir, options }) => {
  if (!PLATFORM_LABELS[platform]) {
    return { success: false, error: `不支持的采集平台：${platform}` };
  }
  try {
    const job = {
      kind: 'manual',
      platform,
      params,
      exportDir: normalizeExportDir(exportDir || readConfig().export_dir),
      trigger: 'manual'
    };
    // 内部系统：可以指定「下载哪一天的表格」。日期同时决定查询日期与产出目录
    // （目录 = 数据日期 + 1 天，和日常定时任务的口径一致），其他平台忽略该参数。
    // 手动导出固定走「图片」分支，与定时计划的产出结构对齐（日期/图片/原始素材），
    // 否则按日期补采的素材会落在日期根目录，和定时任务的目录对不上。
    const filterDate = platform === 'internal'
      ? normalizeFilterDate(options && options.filterDate)
      : '';
    if (platform === 'internal') {
      job.plan = { internal_material_type: '图片' };
      if (filterDate) {
        job.plan.filter_date = filterDate;
        job.batchDate = batchDateForFilter(filterDate);
      }
    }
    return await enqueueJob(job);
  } catch (error) {
    return { success: false, error: error.message, output: error.message };
  }
});

ipcMain.handle('stop-download', async () => {
  // 只停采集车道，分类车道不受影响（两者本来就并行）
  if (lanes.collect.active) {
    if (abortActiveChild('collect')) return { success: true };
  }
  if (dropQueuedJob(() => true, { success: false, stopped: true, output: '', error: '' }, 'collect')) {
    return { success: true };
  }
  return { success: false, error: '当前没有运行中的下载任务' };
});

ipcMain.handle('stop-classify', async () => {
  // 只停分类车道，正在下载的任务继续跑
  if (lanes.classify.active) {
    if (abortActiveChild('classify')) return { success: true };
  }
  if (dropQueuedJob(() => true, { success: false, stopped: true, output: '' }, 'classify')) {
    return { success: true };
  }
  return { success: false };
});

ipcMain.handle('run-direction', async (_event, options) => {
  const source = path.resolve(String(options.folder || ''));
  const output = resolveClassificationOutput(source, options.output);
  return enqueueJob({
    kind: 'classify',
    lane: 'classify',
    script: DIRECTION_SCRIPT,
    source,
    output,
    parallel: Math.max(1, Math.min(10, Number(options.parallel || 5))),
    minConfidence: Math.max(0, Math.min(1, Number(options.minConfidence || 0.85))),
    fileAction: ['copy', 'move', 'backup-move'].includes(options.fileAction)
      ? options.fileAction
      : 'copy',
    dryRun: !options.applyCopy
  }).catch((error) => ({ success: false, error: error.message, output }));
});

ipcMain.handle('run-pure-api', async (_event, options) => {
  const source = path.resolve(String(options.folder || ''));
  const output = resolveClassificationOutput(source, options.output);

  if (!fs.existsSync(source)) {
    return { success: false, error: `图片目录不存在：${source}` };
  }
  const cfg = readConfig();
  if (!cfg.api_endpoint || !cfg.model_id || !cfg.api_key) {
    return { success: false, error: '纯API分类依赖模型 API，请先配置' };
  }
  if (!options.applyCopy) {
    return { success: false, error: '纯API模式不支持预览，请勾选“执行文件操作”' };
  }

  return enqueueJob({
    kind: 'classify',
    lane: 'classify',
    source,
    output,
    platform: CLASSIFY_PLATFORMS.includes(options.platform) ? options.platform : 'internal',
    parallel: Math.max(1, Math.min(10, Number(options.parallel || 5))),
    minConfidence: Math.max(0, Math.min(1, Number(options.minConfidence || 0.85))),
    fileAction: ['copy', 'move'].includes(options.fileAction) ? options.fileAction : 'copy'
  }).catch((error) => ({ success: false, error: error.message, output }));
});

// ============================ IPC：窗口 / 文件 ============================

ipcMain.handle('open-folder', async (_event, folderPath) => {
  try {
    const target = normalizeExportDir(folderPath);
    fs.mkdirSync(target, { recursive: true });
    const error = await shell.openPath(target);
    return error ? { success: false, error } : { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle('select-folder', async (_event, defaultPath) => {
  const result = await dialog.showOpenDialog(mainWindow, {
    defaultPath: defaultPath && path.isAbsolute(String(defaultPath))
      ? path.normalize(String(defaultPath))
      : undefined,
    properties: ['openDirectory']
  });
  return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
});

ipcMain.handle('open-path', async (_event, target) => {
  if (!target) return { success: false, error: '路径为空' };
  const error = await shell.openPath(target);
  return error ? { success: false, error } : { success: true };
});

// ============================ IPC：配置 ============================

ipcMain.handle('get-runtime-status', async () => runtimeStatus());

ipcMain.handle('get-config', async () => readConfig());

ipcMain.handle('get-api-classify-prompt', async () => {
  try {
    const cfg = readConfig();
    const probe = spawnSync(pythonCommand(), [PURE_API_SCRIPT, '--print-prompt'], {
      cwd: CLASSIFY_ENGINE_DIR,
      encoding: 'utf8',
      windowsHide: true,
      timeout: 15000,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }
    });
    if (probe.status !== 0 || !String(probe.stdout || '').trim()) {
      return { success: false, error: String(probe.stderr || '无法读取内置提示词').trim() };
    }
    const defaultPrompt = String(probe.stdout).trim();
    const storedPrompt = String(cfg.api_classify_prompt || '').trim();
    const keywordFields = [
      'visible_text', 'brand_keywords', 'visual_keywords',
      'layout_keywords', 'action_keywords', 'scene_keywords', 'confidence'
    ];
    const customPrompt = keywordFields.every((field) => storedPrompt.includes(field))
      ? storedPrompt
      : '';
    return {
      success: true,
      prompt: customPrompt || defaultPrompt,
      defaultPrompt,
      isCustom: Boolean(customPrompt),
      ignoredLegacyPrompt: Boolean(storedPrompt && !customPrompt)
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle('save-config', async (_event, config) => {
  try {
    const current = readConfig();
    const next = { ...current };

    if (Object.prototype.hasOwnProperty.call(config, 'api_endpoint')) {
      next.api_endpoint = String(config.api_endpoint || '').trim();
    }
    if (Object.prototype.hasOwnProperty.call(config, 'model_id')) {
      next.model_id = String(config.model_id || '').trim();
    }
    if (Object.prototype.hasOwnProperty.call(config, 'api_key')) {
      next.api_key = String(config.api_key || '').trim();
    }
    if (Object.prototype.hasOwnProperty.call(config, 'api_classify_prompt')) {
      const prompt = String(config.api_classify_prompt || '').replace(/\r\n/g, '\n').trim();
      if (prompt.length > 50000) {
        throw new Error('提示词过长，请控制在 50000 字符以内');
      }
      next.api_classify_prompt = prompt;
    }
    let exportDirChanged = false;
    if (Object.prototype.hasOwnProperty.call(config, 'export_dir')) {
      const resolved = normalizeExportDir(config.export_dir);
      exportDirChanged = resolved !== next.export_dir;
      next.export_dir = resolved;
      fs.mkdirSync(next.export_dir, { recursive: true });
    }

    writeJson(CONFIG_PATH, next);

    const plans = loadPlans();

    // 跟随全局目录的计划立刻跟着走，并把快照同步成新路径，界面上不会显示过期目录
    let syncedPlans = 0;
    if (exportDirChanged) {
      plans.forEach((plan) => {
        if (plan.use_global_root === true) {
          plan.download_root = next.export_dir;
          syncedPlans += 1;
        }
      });
      if (syncedPlans) {
        savePlans(plans);
        send('plans-changed', plans);
      }
    }

    // 仍固定在各自目录上的计划数，交给界面决定要不要提示一键切换
    const pinnedPlans = plans.filter((plan) => plan.use_global_root !== true).length;
    return { success: true, config: next, syncedPlans, pinnedPlans, exportDirChanged };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

// 把指定计划（默认全部）切换为「跟随下载中心的导出目录」
ipcMain.handle('plans-follow-global-root', async (_event, planIds) => {
  try {
    const ids = Array.isArray(planIds) && planIds.length ? new Set(planIds.map(String)) : null;
    const plans = loadPlans();
    const exportDir = normalizeExportDir(readConfig().export_dir);
    let changed = 0;
    plans.forEach((plan) => {
      if (ids && !ids.has(String(plan.id))) return;
      if (plan.use_global_root === true && plan.download_root === exportDir) return;
      plan.use_global_root = true;
      plan.download_root = exportDir;
      changed += 1;
    });
    if (changed) {
      savePlans(plans);
      send('plans-changed', plans);
    }
    return { success: true, changed, exportDir };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

// ============================ IPC：计划 / 历史 ============================

ipcMain.handle('plans-list', async () => loadPlans());

ipcMain.handle('plans-save', async (_event, input) => {
  try {
    const plans = loadPlans();
    const index = input && input.id ? plans.findIndex((item) => item.id === input.id) : -1;
    if (index >= 0) {
      plans[index] = normalizePlan(input, plans[index]);
      savePlans(plans);
      send('plans-changed', plans);
      return { success: true, plan: plans[index] };
    }
    const created = normalizePlan(input, null);
    plans.push(created);
    savePlans(plans);
    send('plans-changed', plans);
    return { success: true, plan: created };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle('plans-delete', async (_event, planId) => {
  const plans = loadPlans().filter((item) => item.id !== planId);
  savePlans(plans);
  send('plans-changed', plans);
  return { success: true, plans };
});

ipcMain.handle('plans-toggle', async (_event, { planId, enabled }) => {
  const plans = loadPlans();
  const target = plans.find((item) => item.id === planId);
  if (!target) return { success: false, error: '计划不存在' };
  target.enabled = enabled !== false;
  target.updated_at = nowIso();
  savePlans(plans);
  send('plans-changed', plans);
  return { success: true, plan: target };
});

ipcMain.handle('plans-run-now', async (_event, planId) => {
  const plans = loadPlans();
  const plan = plans.find((item) => item.id === planId);
  if (!plan) return { success: false, error: '计划不存在' };

  // 立即执行属于人工干预，占住本分钟避免调度器再触发一次
  plan.last_run_minute = currentMinuteKey(new Date());
  plan.last_run_at = nowIso();
  plan.last_status = 'queued';
  plan.last_message = '已手动排入队列';
  plan.updated_at = nowIso();
  savePlans(plans);
  send('plans-changed', plans);

  enqueueJob({
    kind: 'plan',
    plan,
    platform: plan.platform,
    params: planDownloadParams(plan),
    trigger: 'manual'
  }).catch(() => { /* 失败已写入运行历史 */ });

  return { success: true, ...queueTotals() };
});

ipcMain.handle('runs-list', async () => {
  return loadRuns().map((run) => ({ ...run, log: undefined }));
});

ipcMain.handle('run-detail', async (_event, runId) => {
  const run = loadRuns().find((item) => item.id === runId);
  if (!run) return null;
  let logContent;
  try {
    logContent = fs.readFileSync(run.log, 'utf8');
  } catch (_) {
    logContent = '（日志文件不存在或已被清理）';
  }
  return { ...run, logContent: logContent.slice(-120000) };
});

ipcMain.handle('runs-clear', async () => {
  saveRuns([]);
  return { success: true };
});

/**
 * 按原日期重跑某次运行。
 *
 * 关键点是**日期保持一致**：内部系统按「昨天」查询，隔天重跑会取到错误的日期
 * （曾出现「期望 09-22、表格实际 09-21」导致整批数据采错），
 * 所以这里把原运行的批次日期与查询日期原样带回去。
 *
 * 用当前计划的最新参数（关键词/筛选条件等）执行，只有日期沿用原值；
 * 输出落在同一个日期目录下的新批次（如 20260923-2），与首次尝试归在一起。
 */
ipcMain.handle('runs-rerun', async (_event, runId) => {
  try {
    const run = loadRuns().find((item) => item.id === runId);
    if (!run) return { success: false, error: '找不到该运行记录' };

    const plan = loadPlans().find((item) => item.id === run.plan_id);
    if (!plan) {
      return { success: false, error: '原计划已被删除，无法按原参数重跑' };
    }

    const { batchDate, filterDate } = resolveRunDates(run, plan.platform);

    // 只在这里临时附加日期，不写回计划文件
    const replayPlan = { ...plan, batch_date: batchDate };
    if (filterDate) replayPlan.filter_date = filterDate;

    enqueueJob({
      kind: 'plan',
      plan: replayPlan,
      platform: plan.platform,
      params: planDownloadParams(plan),
      trigger: 'rerun'
    });

    return {
      success: true,
      planName: plan.name,
      batchDate,
      filterDate,
      // 相对时间范围（近7天等）无法回溯，只能按当前时间重新查询
      relativeRange: Boolean(plan.time_range)
    };
  } catch (error) {
    return { success: false, error: error.message };
  }
});

ipcMain.handle('queue-state-get', async () => {
  const snapshot = {};
  for (const lane of LANES) {
    const state = lanes[lane];
    snapshot[lane] = {
      active: state.active ? describeJob(state.active) : null,
      pending: state.pending.map(describeJob)
    };
  }
  return snapshot;
});

ipcMain.handle('scheduler-status', async () => ({
  running: Boolean(schedulerTimer),
  startedAt: schedulerStartedAt,
  intervalMs: SCHEDULER_TICK_MS,
  weekdays: WEEKDAY_LABELS,
  now: nowIso()
}));

ipcMain.handle('platform-labels', async () => PLATFORM_LABELS);
