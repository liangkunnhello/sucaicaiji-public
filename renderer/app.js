const DEFAULT_EXPORT_DIR = 'D:\\跑量素材\\外部跑量素材';
const APPGROWING_MEDIA = [
  '微信', '微博', 'QQ', '百度贴吧', '小红书',
  '今日头条', '手机百度', '腾讯新闻', '网易新闻', '知乎',
  '汽车之家', '抖音短视频', '抖音火山版', '西瓜视频', '快手',
  '爱奇艺', '腾讯视频', 'Bilibili', '好看视频', '芒果 TV',
  '搜狐视频', 'UC 浏览器', '百度浏览器', '网易邮箱大师', 'QQ 浏览器',
  '腾讯手机管家', '番茄小说', '小米联盟媒体', 'QQ 音乐',
  '穿山甲联盟', '优量汇联盟', '百青藤联盟', '淘宝 Tanx 媒体',
  '京东直投媒体', '易通天下媒体', '快手联盟', '字节小程序媒体',
  '百青藤小程序媒体', 'QQ 小程序媒体', 'OPPO联盟媒体',
  'OPPO 浏览器', '小米浏览器', 'vivo 浏览器',
  '小米应用商店', '小米音乐', '小米视频', '小米安全中心',
  '小米下载管理', '小米天气', '小米个性主题', '小米画报',
  'OPPO 书城', 'OPPO 阅读'
];
const APPGROWING_TRAFFIC = [
  '腾讯广告', '巨量广告/千川', '百度营销', '阿里超级汇川',
  '微博粉丝通', '快手磁力引擎', '奇炬', '优酷睿视',
  '趣头条', 'bilibili 三连推广', '知乎', '小红书聚光',
  '网易易效', '360点睛', '新浪扶翼', '凤凰凤羽',
  '搜狗星耀', 'WiFi 万能钥匙', '汽车之家', '美柚女人通',
  '搜狐汇算', '一点资讯', '芒果 TV', '悦头条',
  'OPPO', 'vivo', '小米', 'UnityAds',
  'Vungle', 'Applovin', 'Adcolony', '4399游戏盒',
  '淘宝 Tanx/Unidesk', '京东直投', '易通天下'
];
const APPGROWING_SORT_OPTIONS = {
  '广告': [
    '按视频播放数降序',
    '按广告活跃指数降序',
    '按今日曝光数降序',
    '按视频点赞数降序',
    '按投放天数降序',
    '按投放时间降序',
    '按投放时间升序',
    '按投放天数升序'
  ],
  '素材': [
    '按关联广告创意数降序',
    '按素材质量指数降序',
    '按搜索相关性排序',
    '按投放时间降序',
    '按投放天数降序'
  ]
};
const appGrowingSortState = {
  '广告': '按视频播放数降序',
  '素材': '按关联广告创意数降序'
};
let currentExportDir = DEFAULT_EXPORT_DIR;
let currentMode = 'fast';
let toastTimer = null;
let apiPromptDefault = '';
let apiPromptIsCustom = false;
let downloadRunning = false;
let downloadStartedAt = 0;
let downloadElapsedTimer = null;
let classifyRunning = false;
let classifyStopRequested = false;
let classifyElapsedTimer = null;
let classifySeenProgress = new Set();
let classifyProgress = {
  status: 'idle',
  current: 0,
  total: 0,
  auto: 0,
  review: 0,
  phase: '等待任务',
  message: '选择素材目录后启动整理任务。',
  startedAt: 0
};

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initPlatformTabs();
  initAppGrowingFilters();
  initAppGrowingView();
  initClassifyMode();
  initDownloadMonitor();
  initInternalDateControl();
  initLogPanelResize();
  initLogListeners();
  initPromptEditor();
  resetClassifyProgress();
  loadRuntimeStatus();
  loadApiConfig();
  
  document.getElementById('openSaveDir').addEventListener('click', async () => {
    const result = await window.electronAPI.openFolder(currentExportDir);
    if (result && !result.success) {
      showToast('无法打开导出目录：' + result.error, 'error');
    }
  });

  document.getElementById('changeExportDir').addEventListener('click', changeExportDir);

  initAutomation();
});

function initAppGrowingFilters() {
  renderFilterOptions('appgrowing-media', APPGROWING_MEDIA);
  renderFilterOptions('appgrowing-traffic', APPGROWING_TRAFFIC);

  document.querySelectorAll('.filter-toggle').forEach(toggle => {
    toggle.addEventListener('click', () => {
      const block = toggle.closest('.filter-block');
      const collapsed = block.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
    });
  });

  document.querySelectorAll('[data-filter-search]').forEach(search => {
    search.addEventListener('input', () => {
      const group = document.getElementById(search.dataset.filterSearch);
      const query = search.value.trim().toLocaleLowerCase('zh-CN');

      group.querySelectorAll('.filter-option').forEach(option => {
        option.hidden = Boolean(query) &&
          !option.dataset.searchText.includes(query);
      });
    });
  });

  document.querySelectorAll('[data-filter-action]').forEach(button => {
    button.addEventListener('click', () => {
      const group = document.getElementById(button.dataset.filterGroup);
      const checked = button.dataset.filterAction === 'select';

      group.querySelectorAll('.filter-option:not([hidden]) input').forEach(input => {
        input.checked = checked;
      });
      updateAppGrowingFilterCounts();
    });
  });

  document.getElementById('appgrowing-media').addEventListener('change', updateAppGrowingFilterCounts);
  document.getElementById('appgrowing-traffic').addEventListener('change', updateAppGrowingFilterCounts);
  updateAppGrowingFilterCounts();
}

function initAppGrowingView() {
  const viewInputs = document.querySelectorAll('input[name="appgrowing-view"]');
  const sortSelect = document.getElementById('appgrowing-sort');
  let activeView = document.querySelector('input[name="appgrowing-view"]:checked')?.value || '广告';

  const renderSortOptions = (view) => {
    const options = APPGROWING_SORT_OPTIONS[view] || APPGROWING_SORT_OPTIONS['广告'];
    const selected = options.includes(appGrowingSortState[view])
      ? appGrowingSortState[view]
      : options[0];
    const fragment = document.createDocumentFragment();

    options.forEach(value => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      option.selected = value === selected;
      fragment.append(option);
    });

    sortSelect.replaceChildren(fragment);
    document.getElementById('appgrowing-sort-label').textContent = `${view}排序`;
  };

  sortSelect.addEventListener('change', () => {
    appGrowingSortState[activeView] = sortSelect.value;
  });

  viewInputs.forEach(input => {
    input.addEventListener('change', () => {
      if (!input.checked) return;
      appGrowingSortState[activeView] = sortSelect.value;
      activeView = input.value;
      renderSortOptions(activeView);
    });
  });

  renderSortOptions(activeView);
}

function renderFilterOptions(containerId, options) {
  const container = document.getElementById(containerId);
  const fragment = document.createDocumentFragment();

  options.forEach(option => {
    const label = document.createElement('label');
    label.className = 'filter-option';
    label.dataset.searchText = option.toLocaleLowerCase('zh-CN');

    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = option;

    const text = document.createElement('span');
    text.textContent = option;

    label.append(input, text);
    fragment.append(label);
  });

  container.replaceChildren(fragment);
}

function updateAppGrowingFilterCounts() {
  const mediaSelected = document.querySelectorAll('#appgrowing-media input:checked').length;
  const trafficSelected = document.querySelectorAll('#appgrowing-traffic input:checked').length;

  document.getElementById('appgrowing-media-count').textContent =
    `${mediaSelected} / ${APPGROWING_MEDIA.length} 已选`;
  document.getElementById('appgrowing-traffic-count').textContent =
    `${trafficSelected} / ${APPGROWING_TRAFFIC.length} 已选`;
}

function activateTab(tabName) {
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  tabContents.forEach(c => c.classList.remove('active'));
  const target = document.getElementById(`${tabName}-tab`);
  if (target) target.classList.add('active');

  if (tabName === 'classify') {
    loadRuntimeStatus();
  }
  if (tabName === 'automation') {
    refreshPlans();
    refreshQueueState();
    refreshSchedulerState();
  }
  if (tabName === 'history') {
    refreshRuns();
  }
}

/** 从顶部的 API 状态直接跳到「素材治理」的 API 配置面板并高亮。 */
function jumpToApiConfig() {
  activateTab('classify');
  const panel = document.querySelector('.api-panel');
  if (!panel) return;
  // 等标签页切换完成再滚动，否则拿到的是隐藏元素的位置
  requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
    panel.classList.remove('api-panel-flash');
    // 强制重排，保证连续点击也能重新播放高亮
    void panel.offsetWidth;
    panel.classList.add('api-panel-flash');
    const endpoint = document.getElementById('api-endpoint');
    if (endpoint) {
      setTimeout(() => endpoint.focus({ preventScroll: true }), 320);
    }
  });
}

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });
}

function initPlatformTabs() {
  const platformBtns = document.querySelectorAll('.platform-btn');
  const platformPanels = document.querySelectorAll('.platform-panel');
  
  platformBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const platform = btn.dataset.platform;
      
      platformBtns.forEach(b => b.classList.remove('active'));
      platformPanels.forEach(p => p.classList.remove('active'));
      
      btn.classList.add('active');
      document.getElementById(`${platform}-panel`).classList.add('active');
    });
  });
}

function initClassifyMode() {
  const modeCards = document.querySelectorAll('.mode-card');
  const fastOptions = document.getElementById('fast-options');
  const directionOptions = document.getElementById('direction-options');
  const pureApiOptions = document.getElementById('pure-api-options');
  
  modeCards.forEach(card => {
    card.addEventListener('click', () => {
      const mode = card.dataset.mode;
      currentMode = mode;
      
      modeCards.forEach(c => c.classList.remove('active'));
      card.classList.add('active');
      
      fastOptions.style.display = mode === 'fast' ? 'block' : 'none';
      directionOptions.style.display = mode === 'direction' ? 'block' : 'none';
      pureApiOptions.style.display = mode === 'pure-api' ? 'block' : 'none';
    });
  });
}

function initLogListeners() {
  window.electronAPI.onDownloadLog((data) => {
    appendLog('download-log', data.text);
  });

  window.electronAPI.onDownloadProgress((data) => {
    updateDownloadProgress(data);
  });
  
  window.electronAPI.onClassifyLog((data) => {
    const text = typeof data === 'string' ? data : data.data;
    appendLog('classify-log', text);
    String(text || '').split(/\r?\n/).forEach(parseClassifyProgressLine);
  });
}

function setClassifyControls(running) {
  classifyRunning = running;
  document.querySelector('.classify-start').disabled = running;
  document.querySelector('.classify-stop').disabled = !running;
}

function renderClassifyProgress() {
  const panel = document.getElementById('classify-progress-panel');
  if (!panel) return;

  const statusLabels = {
    idle: '待命',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    stopped: '已停止'
  };
  const total = Number(classifyProgress.total) || 0;
  const current = Number(classifyProgress.current) || 0;
  const percent = classifyProgress.status === 'completed'
    ? 100
    : (total > 0 ? Math.min(100, Math.round(current / total * 100)) : 0);

  panel.dataset.status = classifyProgress.status;
  panel.dataset.indeterminate = String(classifyProgress.status === 'running' && total === 0);
  document.getElementById('classify-progress-status').textContent =
    statusLabels[classifyProgress.status] || classifyProgress.status;
  document.getElementById('classify-progress-percent').textContent = `${percent}%`;
  document.getElementById('classify-progress-phase').textContent = classifyProgress.phase;
  document.getElementById('classify-progress-message').textContent = classifyProgress.message;
  document.getElementById('classify-progress-bar').style.width = `${percent}%`;
  document.getElementById('classify-progress-current').textContent = current;
  document.getElementById('classify-progress-total').textContent = total || '—';
  document.getElementById('classify-progress-auto').textContent = classifyProgress.auto;
  document.getElementById('classify-progress-review').textContent = classifyProgress.review;
  document.getElementById('classify-log-status').textContent =
    classifyProgress.status === 'idle'
      ? '等待整理任务'
      : `${classifyProgress.phase} · ${statusLabels[classifyProgress.status]}`;
}

function resetClassifyProgress() {
  clearInterval(classifyElapsedTimer);
  classifyElapsedTimer = null;
  classifySeenProgress = new Set();
  classifyProgress = {
    status: 'idle',
    current: 0,
    total: 0,
    auto: 0,
    review: 0,
    phase: '等待任务',
    message: '选择素材目录后启动整理任务。',
    startedAt: 0
  };
  setClassifyControls(false);
  const elapsed = document.getElementById('classify-progress-elapsed');
  if (elapsed) elapsed.textContent = '00:00';
  renderClassifyProgress();
}

function beginClassifyProgress() {
  resetClassifyProgress();
  classifyStopRequested = false;
  classifyProgress.status = 'running';
  classifyProgress.phase = '启动引擎';
  classifyProgress.message = (currentMode === 'pure-api' || currentMode === 'fast')
    ? '正在启动 AI 关键词提取与本地规则分类服务…'
    : '正在加载画面方向分析服务…';
  classifyProgress.startedAt = Date.now();
  setClassifyControls(true);
  renderClassifyProgress();
  classifyElapsedTimer = setInterval(() => {
    document.getElementById('classify-progress-elapsed').textContent =
      formatElapsed(Date.now() - classifyProgress.startedAt);
  }, 1000);
}

function finishClassifyProgress(status, message) {
  classifyProgress.status = status;
  classifyProgress.phase = status === 'completed' ? '整理完成' :
    (status === 'stopped' ? '任务已停止' : '执行失败');
  classifyProgress.message = message;
  if (status === 'completed' && classifyProgress.total > 0) {
    classifyProgress.current = classifyProgress.total;
  }
  clearInterval(classifyElapsedTimer);
  classifyElapsedTimer = null;
  if (classifyProgress.startedAt) {
    document.getElementById('classify-progress-elapsed').textContent =
      formatElapsed(Date.now() - classifyProgress.startedAt);
  }
  setClassifyControls(false);
  renderClassifyProgress();
}

function parseProtocolValues(parts) {
  return Object.fromEntries(parts.map(item => {
    const index = item.indexOf('=');
    return index < 0 ? [item, ''] : [item.slice(0, index), item.slice(index + 1)];
  }));
}

function parseClassifyProgressLine(rawLine) {
  const line = String(rawLine || '').trim();
  if (!line || classifyProgress.status === 'idle') return;
  let changed = false;

  if (line.startsWith('FOUND|')) {
    classifyProgress.total = Number(line.split('|')[1]) || 0;
    classifyProgress.phase = '准备分类';
    classifyProgress.message = `已发现 ${classifyProgress.total} 张待处理图片`;
    changed = true;
  } else if (line.startsWith('RESUME|')) {
    const parts = line.split('|');
    const values = parseProtocolValues(parts.slice(2));
    classifyProgress.current = Number(parts[1]) || 0;
    classifyProgress.auto = Number(values.AUTO) || 0;
    classifyProgress.review = Number(values.REVIEW) || 0;
    classifyProgress.phase = '恢复任务';
    classifyProgress.message = `从第 ${classifyProgress.current} 张图片继续`;
    changed = true;
  } else if (line.startsWith('PROGRESS|')) {
    const parts = line.split('|');
    const current = Number(parts[1]) || 0;
    const status = parts[3] || 'REVIEW';
    const direction = parts[5] || '待确认';
    const filename = parts.slice(6).join('|') || '正在处理图片';
    const progressKey = `classify-${current}`;
    classifyProgress.current = current;
    classifyProgress.total = Number(parts[2]) || classifyProgress.total;
    if (!classifySeenProgress.has(progressKey)) {
      classifySeenProgress.add(progressKey);
      if (status === 'AUTO') classifyProgress.auto += 1;
      else classifyProgress.review += 1;
    }
    classifyProgress.phase = '智能判定';
    classifyProgress.message = `${filename} → ${status === 'AUTO' ? direction : '待确认'}`;
    changed = true;
  } else if (line.includes('API_PROGRESS|')) {
    const parts = line.slice(line.indexOf('API_PROGRESS|')).split('|');
    classifyProgress.phase = 'API 分析';
    classifyProgress.message = parts[3]
      ? `正在分析 ${parts[3]} · ${parts[1]}/${parts[2]}`
      : `智能分析进度 ${parts[1]}/${parts[2]}`;
    changed = true;
  } else if (line.startsWith('SUMMARY|')) {
    const values = parseProtocolValues(line.split('|').slice(1));
    classifyProgress.auto = Number(values.AUTO) || 0;
    classifyProgress.review = Number(values.REVIEW) || 0;
    classifyProgress.total = Number(values.TOTAL) || classifyProgress.total;
    classifyProgress.current = classifyProgress.total;
    classifyProgress.phase = '分类汇总';
    classifyProgress.message = `自动分类 ${classifyProgress.auto} 张，待确认 ${classifyProgress.review} 张`;
    changed = true;
  } else if (line.startsWith('POST_FOUND|')) {
    classifyProgress.current = 0;
    classifyProgress.total = Number(line.split('|')[1]) || classifyProgress.total;
    classifyProgress.phase = '文件归档';
    classifyProgress.message = '分类完成，正在执行移动或备份操作…';
    changed = true;
  } else if (line.startsWith('POST_PROGRESS|')) {
    const parts = line.split('|');
    classifyProgress.current = Number(parts[1]) || 0;
    classifyProgress.total = Number(parts[2]) || classifyProgress.total;
    classifyProgress.phase = '文件归档';
    classifyProgress.message = `${parts[4] || '文件'} · ${parts[3] || '处理中'}`;
    changed = true;
  } else if (line.startsWith('POST_SUMMARY|')) {
    const values = parseProtocolValues(line.split('|').slice(1));
    classifyProgress.total = Number(values.TOTAL) || classifyProgress.total;
    classifyProgress.current = classifyProgress.total;
    classifyProgress.phase = '归档完成';
    classifyProgress.message = `文件操作完成，共处理 ${classifyProgress.total} 张`;
    changed = true;
  } else if (line.startsWith('STAGE|')) {
    classifyProgress.phase = '引擎准备';
    classifyProgress.message = line.split('|').slice(3).join(' · ') || '分类引擎已就绪';
    changed = true;
  }

  if (changed) renderClassifyProgress();
}

async function loadRuntimeStatus() {
  const statusDiv = document.getElementById('runtime-status');
  statusDiv.innerHTML = '正在检测运行环境...';
  
  try {
    const status = await window.electronAPI.getRuntimeStatus();
    
    const pythonStatus = status.python ? '<span class="status-badge status-ok">✓ 可用</span>' : '<span class="status-badge status-error">✗ 不可用</span>';
    const apiOk = Boolean(status.api);
    // API 状态做成可点击入口：直接跳到「素材治理」的配置面板，
    // 免得还要自己去右侧栏里找。
    const apiStatus = `
      <button type="button" class="status-badge status-jump ${apiOk ? 'status-ok' : 'status-error'}"
        onclick="jumpToApiConfig()" title="点击前往「素材治理」页修改 API 配置">
        ${apiOk ? '✓ 已配置' : '✗ 未配置'} · 点此设置
      </button>`;

    statusDiv.innerHTML = `
      <strong>Python:</strong> ${pythonStatus} (${status.pythonMode || '未知'}) &nbsp;&nbsp;
      <strong>API:</strong> ${apiStatus}
    `;

    // 顶栏入口同步提示：未配置时亮红点，省得用户不知道要来这里填
    const entry = document.getElementById('openApiConfig');
    const dot = document.getElementById('apiEntryDot');
    if (entry) entry.classList.toggle('is-warning', !apiOk);
    if (dot) dot.hidden = apiOk;
  } catch (err) {
    statusDiv.innerHTML = '<span class="status-badge status-error">✗ 检测失败：' + err.message + '</span>';
  }
}

async function loadApiConfig() {
  try {
    const config = await window.electronAPI.getConfig();
    if (config) {
      document.getElementById('api-endpoint').value = config.api_endpoint || '';
      document.getElementById('model-id').value = config.model_id || '';
      document.getElementById('api-key').value = config.api_key || '';
      applyExportDir(config.export_dir || DEFAULT_EXPORT_DIR, true);
    }
  } catch (err) {
    console.error('加载配置失败:', err);
  }
}

function applyExportDir(directory, syncClassifyDirs = false) {
  const previousExportDir = currentExportDir;
  currentExportDir = directory || DEFAULT_EXPORT_DIR;

  const display = document.getElementById('exportDirDisplay');
  display.textContent = currentExportDir;
  display.title = currentExportDir;

  if (syncClassifyDirs) {
    ['classify-dir-fast', 'classify-dir-direction', 'classify-dir-pure-api'].forEach(id => {
      const input = document.getElementById(id);
      if (input && (input.value === previousExportDir || input.value === DEFAULT_EXPORT_DIR)) {
        input.value = '';
      }
    });
  }
}

async function changeExportDir() {
  try {
    const folder = await window.electronAPI.selectFolder(currentExportDir);
    if (!folder) return;

    const result = await window.electronAPI.saveConfig({ export_dir: folder });
    if (!result.success) {
      showToast('保存失败：' + result.error, 'error');
      return;
    }

    applyExportDir(result.config?.export_dir || folder, true);

    const resolved = result.config?.export_dir || folder;
    const pinned = Number(result.pinnedPlans) || 0;

    if (result.exportDirChanged && pinned > 0) {
      const follow = window.confirm(
        `默认导出位置已更新为：\n${resolved}\n\n`
        + `另有 ${pinned} 条采集计划固定使用各自的下载目录，不会跟着变。\n\n`
        + `是否把这 ${pinned} 条计划一并改成跟随当前导出目录？`
      );
      if (follow) {
        const sync = await window.electronAPI.plansFollowGlobalRoot();
        if (sync && sync.success) {
          showToast(`已让 ${sync.changed} 条计划跟随当前导出目录`);
          refreshPlans();
        } else {
          showToast('计划同步失败：' + ((sync && sync.error) || '未知错误'), 'error');
        }
        return;
      }
      showToast(`默认导出位置已更新；${pinned} 条计划仍使用各自的固定目录`);
      return;
    }

    showToast('默认导出位置已更新');
  } catch (err) {
    showToast('保存失败：' + err.message, 'error');
  }
}

function showToast(message, type = 'success') {
  const toast = document.getElementById('appToast');
  toast.textContent = message;
  toast.className = `app-toast visible ${type}`;

  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.remove('visible');
  }, 2600);
}

function initPromptEditor() {
  const textarea = document.getElementById('apiPromptText');
  textarea.addEventListener('input', () => {
    updatePromptCharCount();
    const isDefault = textarea.value.trim() === apiPromptDefault.trim();
    setPromptModeBadge(isDefault ? '内置规则 · 待保存' : '自定义规则 · 待保存', true);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !document.getElementById('apiPromptModal').hidden) {
      closeApiPromptEditor();
    }
  });
}

function setPromptModeBadge(text, pending = false) {
  const badge = document.getElementById('apiPromptModeBadge');
  badge.textContent = text;
  badge.classList.toggle('pending', pending);
}

function updatePromptCharCount() {
  const length = document.getElementById('apiPromptText').value.length;
  document.getElementById('apiPromptCharCount').textContent = `${length.toLocaleString('zh-CN')} 字符`;
}

async function openApiPromptEditor() {
  const modal = document.getElementById('apiPromptModal');
  const textarea = document.getElementById('apiPromptText');
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  textarea.value = '';
  textarea.disabled = true;
  setPromptModeBadge('正在读取');
  updatePromptCharCount();

  try {
    const result = await window.electronAPI.getApiClassifyPrompt();
    if (!result?.success) {
      throw new Error(result?.error || '读取提示词失败');
    }
    apiPromptDefault = result.defaultPrompt || '';
    apiPromptIsCustom = Boolean(result.isCustom);
    textarea.value = result.prompt || apiPromptDefault;
    textarea.disabled = false;
    setPromptModeBadge(apiPromptIsCustom ? '自定义规则 · 已生效' : '内置规则 · 已生效');
    updatePromptCharCount();
    textarea.focus();
    if (result.ignoredLegacyPrompt) {
      showToast('旧版“模型直判分类”提示词已停用，当前使用关键词提取规则');
    }
  } catch (error) {
    closeApiPromptEditor();
    showToast(`提示词读取失败：${error.message}`, 'error');
  }
}

function closeApiPromptEditor() {
  const modal = document.getElementById('apiPromptModal');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('modal-open');
}

function resetApiPromptEditor() {
  if (!apiPromptDefault) {
    showToast('内置提示词尚未读取完成', 'error');
    return;
  }
  const textarea = document.getElementById('apiPromptText');
  textarea.value = apiPromptDefault;
  setPromptModeBadge('内置规则 · 待保存', true);
  updatePromptCharCount();
  textarea.focus();
}

async function saveApiPromptEditor() {
  const textarea = document.getElementById('apiPromptText');
  const saveButton = document.getElementById('apiPromptSaveButton');
  const prompt = textarea.value.trim();
  const requiredFields = [
    'visible_text',
    'brand_keywords',
    'visual_keywords',
    'layout_keywords',
    'action_keywords',
    'scene_keywords',
    'confidence'
  ];
  const missingFields = requiredFields
    .filter(field => !prompt.includes(field));

  if (prompt.length < 80) {
    showToast('提示词过短，请保留完整的分类规则和返回格式', 'error');
    return;
  }
  if (missingFields.length) {
    showToast(`关键词协议缺少字段：${missingFields.join('、')}`, 'error');
    return;
  }
  if (/"product"\s*:|\"direction\"\s*:/.test(prompt)) {
    showToast('关键词模式禁止让 AI 返回 product 或 direction', 'error');
    return;
  }

  const useDefault = prompt === apiPromptDefault.trim();
  saveButton.disabled = true;
  saveButton.textContent = '正在保存...';
  try {
    const result = await window.electronAPI.saveConfig({
      api_classify_prompt: useDefault ? '' : prompt
    });
    if (!result?.success) {
      throw new Error(result?.error || '保存失败');
    }
    apiPromptIsCustom = !useDefault;
    setPromptModeBadge(useDefault ? '内置规则 · 已生效' : '自定义规则 · 已生效');
    showToast(useDefault ? '已恢复内置提示词规则' : '自定义提示词已保存并生效');
    closeApiPromptEditor();
  } catch (error) {
    showToast(`提示词保存失败：${error.message}`, 'error');
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = '保存并生效';
  }
}

async function saveApiConfig() {
  const config = {
    api_endpoint: document.getElementById('api-endpoint').value.trim(),
    model_id: document.getElementById('model-id').value.trim(),
    api_key: document.getElementById('api-key').value.trim()
  };
  
  try {
    const result = await window.electronAPI.saveConfig(config);
    if (result.success) {
      alert('API 配置已保存');
      loadRuntimeStatus();
    } else {
      alert('保存失败：' + result.error);
    }
  } catch (err) {
    alert('保存失败：' + err.message);
  }
}

const DOWNLOAD_LOG_HEIGHT_KEY = 'downloadLogHeight';
const DOWNLOAD_LOG_MIN_HEIGHT = 96;
const DOWNLOAD_LOG_MAX_HEIGHT = 720;
const DOWNLOAD_LOG_STEP = 60;
const DOWNLOAD_LOG_DEFAULT_HEIGHT = 220;

/**
 * 日志框是否贴在底部。
 * 用户往上翻看历史时不应被新日志强制拽回底部，所以用 dataset.pinned 记住意图：
 * 未设置视为跟随（初始状态），用户滚离底部后置为 'false'，回到底部再恢复。
 */
function isLogPinnedToBottom(logBox) {
  if (logBox.dataset.pinned === 'false') return false;
  return logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight <= 24;
}

function appendLog(logId, text) {
  const logBox = document.getElementById(logId);
  const pinned = isLogPinnedToBottom(logBox);
  logBox.textContent += text;
  // 只有本来就在底部才跟随；正在翻阅历史就停在原地
  if (pinned) logBox.scrollTop = logBox.scrollHeight;
}

function clearLog(logId) {
  const logBox = document.getElementById(logId);
  logBox.textContent = '';
  delete logBox.dataset.pinned;
}

// ---------- 日志区高度：按钮缩放 + 下边沿拖拽 ----------

function applyLogPanelHeight(height) {
  const clamped = Math.max(
    DOWNLOAD_LOG_MIN_HEIGHT,
    Math.min(DOWNLOAD_LOG_MAX_HEIGHT, Math.round(height))
  );
  document.documentElement.style.setProperty('--download-log-height', `${clamped}px`);
  try {
    localStorage.setItem(DOWNLOAD_LOG_HEIGHT_KEY, String(clamped));
  } catch (err) {
    /* 隐私模式下写不了就算了，不影响使用 */
  }
  return clamped;
}

function currentLogPanelHeight() {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue('--download-log-height')
    .trim();
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : DOWNLOAD_LOG_DEFAULT_HEIGHT;
}

function resizeLogPanel(direction) {
  applyLogPanelHeight(currentLogPanelHeight() + direction * DOWNLOAD_LOG_STEP);
}

function resetLogPanelSize() {
  applyLogPanelHeight(DOWNLOAD_LOG_DEFAULT_HEIGHT);
}

function initLogPanelResize() {
  let stored = null;
  try {
    stored = localStorage.getItem(DOWNLOAD_LOG_HEIGHT_KEY);
  } catch (err) {
    stored = null;
  }
  applyLogPanelHeight(stored ? Number(stored) : DOWNLOAD_LOG_DEFAULT_HEIGHT);

  const container = document.querySelector('.download-log-container');
  if (!container) return;

  // 记住用户是否主动离开了底部：离开就暂停自动跟随，回到底部再恢复
  const logBox = document.getElementById('download-log');
  if (logBox) {
    logBox.addEventListener('scroll', () => {
      const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight <= 24;
      logBox.dataset.pinned = atBottom ? 'true' : 'false';
    });

    // 在日志区上 Ctrl + 滚轮也能缩放（顺手加的快捷方式）
    logBox.addEventListener('wheel', (event) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      resizeLogPanel(event.deltaY < 0 ? 1 : -1);
    }, { passive: false });
  }

  // 手柄是 ::after 伪元素，命中它时 e.target 就是容器本身
  container.addEventListener('pointerdown', (event) => {
    if (event.target !== container) return;
    if (event.button !== 0) return;

    const shell = container.closest('.download-shell');
    if (!shell) return;

    event.preventDefault();
    const startY = event.clientY;
    const startHeight = container.getBoundingClientRect().height;
    container.setPointerCapture(event.pointerId);
    container.classList.add('is-resizing');

    const onMove = (moveEvent) => {
      // 向上拖动 = 日志区变高
      applyLogPanelHeight(startHeight - (moveEvent.clientY - startY));
    };
    const onUp = () => {
      container.removeEventListener('pointermove', onMove);
      container.removeEventListener('pointerup', onUp);
      container.removeEventListener('pointercancel', onUp);
      container.classList.remove('is-resizing');
    };

    container.addEventListener('pointermove', onMove);
    container.addEventListener('pointerup', onUp);
    container.addEventListener('pointercancel', onUp);
  });
}

function initDownloadMonitor() {
  document.getElementById('stopDownloadBtn').addEventListener('click', stopDownload);
  updateDownloadProgress({
    status: 'idle',
    stage: 'connecting',
    current: 0,
    total: 0,
    failed: 0,
    message: '选择左侧来源并启动任务，进度会自动更新。'
  });
}

function setDownloadControls(running) {
  downloadRunning = running;
  document.querySelectorAll('[data-download-action]').forEach(button => {
    button.disabled = running;
  });
  document.getElementById('stopDownloadBtn').disabled = !running;
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const mm = String(minutes).padStart(2, '0');
  const ss = String(seconds).padStart(2, '0');
  return hours > 0 ? `${String(hours).padStart(2, '0')}:${mm}:${ss}` : `${mm}:${ss}`;
}

function startDownloadClock(startedAt) {
  downloadStartedAt = startedAt || Date.now();
  clearInterval(downloadElapsedTimer);

  const renderElapsed = () => {
    document.getElementById('progress-elapsed').textContent =
      formatElapsed(Date.now() - downloadStartedAt);
  };

  renderElapsed();
  downloadElapsedTimer = setInterval(renderElapsed, 1000);
}

function stopDownloadClock() {
  clearInterval(downloadElapsedTimer);
  downloadElapsedTimer = null;
  if (downloadStartedAt) {
    document.getElementById('progress-elapsed').textContent =
      formatElapsed(Date.now() - downloadStartedAt);
  }
}

function updateDownloadProgress(data) {
  const status = data.status || 'idle';
  const stage = data.stage || 'connecting';
  const current = Number(data.current) || 0;
  const total = Number(data.total) || 0;
  const failed = Number(data.failed) || 0;
  const isRunning = status === 'running';
  const stageOrder = ['connecting', 'collecting', 'downloading', 'completed'];
  const stageIndex = Math.max(0, stageOrder.indexOf(stage));
  const statusLabels = {
    idle: '待命',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    stopped: '已停止'
  };
  const stageLabels = {
    connecting: '正在连接素材来源',
    collecting: '正在筛选与采集素材',
    downloading: '正在保存素材文件',
    completed: '任务完成'
  };

  setDownloadControls(isRunning);
  const consolePanel = document.getElementById('download-progress-console');
  const orbit = document.getElementById('download-progress-orbit');
  consolePanel.dataset.status = status;
  document.querySelector('#download-status strong').textContent = statusLabels[status] || status;
  document.getElementById('progress-platform').textContent =
    data.platform ? `${getPlatformName(data.platform)}下载任务` : '等待下载任务';
  document.getElementById('progress-stage').textContent = stageLabels[stage] || '任务处理中';
  document.getElementById('progress-message').textContent =
    data.message || '任务状态已更新';
  document.getElementById('progress-current').textContent = current;
  document.getElementById('progress-total').textContent = total || '—';
  document.getElementById('progress-failed').textContent = failed;

  let percent = 0;
  const hasDeterminateProgress = stage === 'downloading' && total > 0;
  if (status === 'completed') {
    percent = 100;
  } else if (hasDeterminateProgress) {
    percent = Math.min(100, Math.round((current / total) * 100));
  }

  orbit.style.setProperty('--progress', percent);
  orbit.classList.toggle('indeterminate', isRunning && !hasDeterminateProgress);
  document.getElementById('download-progress-value').textContent =
    isRunning && !hasDeterminateProgress ? '···' : `${percent}%`;
  document.getElementById('download-progress-caption').textContent =
    status === 'completed' ? '全部完成' :
      (hasDeterminateProgress ? '下载进度' : (isRunning ? '处理中' : '尚未开始'));

  document.querySelectorAll('#download-stage-rail .stage-node').forEach((node, index) => {
    node.classList.toggle('done', status === 'completed' || index < stageIndex);
    node.classList.toggle('current', status !== 'completed' && index === stageIndex);
  });

  document.getElementById('download-log-status').textContent =
    status === 'idle' ? '等待任务输出' :
      `${getPlatformName(data.platform || '')} · ${statusLabels[status] || status}`;

  if (isRunning && (!downloadElapsedTimer || data.startedAt !== downloadStartedAt)) {
    startDownloadClock(data.startedAt);
  } else if (!isRunning) {
    stopDownloadClock();
  }
}

/* ---------------- 内部系统：数据日期（下载哪一天的表格） ---------------- */

function toDateInputValue(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 校验 YYYY-MM-DD 且真实存在（2026-02-31 这类不算）。 */
function isValidDateInput(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || '').trim());
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day;
}

/** 内部系统默认取「昨天」，与脚本不带日期时的行为一致。 */
function internalDateDefault() {
  const date = new Date();
  date.setDate(date.getDate() - 1);
  return toDateInputValue(date);
}

/**
 * 与主进程 batchDateForFilter 同一口径：产出目录 = 数据日期 + 1 天
 * （内部系统目录名是运行日，装的是前一天的数据），未来日期回落到今天。
 */
function batchDateFromFilter(value) {
  if (!isValidDateInput(value)) return '';
  const parts = String(value).split('-').map(Number);
  const batch = new Date(parts[0], parts[1] - 1, parts[2] + 1);
  const today = toDateInputValue(new Date()).replace(/-/g, '');
  const batchText = toDateInputValue(batch).replace(/-/g, '');
  return batchText > today ? today : batchText;
}

function initInternalDateControl() {
  const input = document.getElementById('internal-date');
  if (!input) return;

  input.value = internalDateDefault();
  input.max = toDateInputValue(new Date());
  input.addEventListener('input', syncInternalDateHint);

  document.querySelectorAll('.date-quick').forEach((button) => {
    button.addEventListener('click', () => {
      const days = Number(button.dataset.days) || 1;
      const date = new Date();
      date.setDate(date.getDate() - days);
      input.value = toDateInputValue(date);
      syncInternalDateHint();
    });
  });

  syncInternalDateHint();
}

function syncInternalDateHint() {
  const input = document.getElementById('internal-date');
  const hint = document.getElementById('internal-date-hint');
  if (!input || !hint) return;
  const value = String(input.value || '');

  document.querySelectorAll('.date-quick').forEach((button) => {
    const days = Number(button.dataset.days) || 1;
    const date = new Date();
    date.setDate(date.getDate() - days);
    button.classList.toggle('is-active', value === toDateInputValue(date));
  });

  if (!isValidDateInput(value)) {
    hint.textContent = '未选择日期：按内部系统默认口径取昨天。';
    return;
  }
  hint.textContent = `将查询 ${value} 的表格，产出目录 ${batchDateFromFilter(value)}`
    + '（内部系统口径：目录 = 数据日期 + 1 天）。';
}

async function startDownload(platform) {
  if (downloadRunning) {
    showToast('已有下载任务正在运行', 'error');
    return;
  }

  // 内部系统：先校验「数据日期」，非法时不要清空日志/启动任务
  const options = {};
  if (platform === 'internal') {
    const dateInput = document.getElementById('internal-date');
    const filterDate = dateInput ? String(dateInput.value || '').trim() : '';
    if (filterDate) {
      if (!isValidDateInput(filterDate)) {
        showToast('数据日期格式不正确，请重新选择', 'error');
        return;
      }
      if (filterDate > toDateInputValue(new Date())) {
        showToast('数据日期不能晚于今天', 'error');
        return;
      }
      options.filterDate = filterDate;
    }
  }

  clearLog('download-log');
  appendLog('download-log', `开始 ${getPlatformName(platform)} 下载...\n`);
  appendLog('download-log', `导出位置: ${currentExportDir}\n`);
  if (options.filterDate) {
    appendLog('download-log',
      `数据日期: ${options.filterDate}（产出目录 ${batchDateFromFilter(options.filterDate)}）\n`);
  }
  appendLog('download-log', '\n');

  const params = collectParams(platform);
  const startedAt = Date.now();
  updateDownloadProgress({
    platform,
    status: 'running',
    stage: 'connecting',
    current: 0,
    total: getRequestedMax(platform),
    failed: 0,
    message: '正在启动下载进程并连接素材来源…',
    startedAt
  });
  
  try {
    const result = await window.electronAPI.runDownload(platform, params, currentExportDir, options);
    if (result.success) {
      appendLog('download-log', '\n✓ 下载完成\n');
      if (result.paths?.rawDir) {
        // 把整理页的目录输入框都定位到刚下载的批次
        ['classify-dir-fast', 'classify-dir-direction', 'classify-dir-pure-api'].forEach((id) => {
          const input = document.getElementById(id);
          if (input) input.value = result.paths.rawDir;
        });
        appendLog('download-log', `整理工坊已定位到: ${result.paths.rawDir}\n`);
      }
    } else if (result.stopped) {
      appendLog('download-log', '\n■ 下载任务已停止\n');
    } else if (result.busy) {
      updateDownloadProgress({
        platform,
        status: 'failed',
        stage: 'connecting',
        current: 0,
        total: 0,
        failed: 0,
        message: result.output
      });
      appendLog('download-log', `\n✗ ${result.output}\n`);
    } else {
      appendLog('download-log', '\n✗ 下载出错\n');
    }
  } catch (err) {
    updateDownloadProgress({
      platform,
      status: 'failed',
      stage: 'connecting',
      current: 0,
      total: 0,
      failed: 0,
      message: err.message
    });
    appendLog('download-log', `\n✗ 错误: ${err.message}\n`);
  }
}

async function stopDownload() {
  if (!downloadRunning) return;

  const button = document.getElementById('stopDownloadBtn');
  button.disabled = true;
  document.getElementById('progress-message').textContent = '正在停止下载进程…';

  try {
    const result = await window.electronAPI.stopDownload();
    if (!result.success) {
      button.disabled = false;
      showToast(result.error || '停止失败', 'error');
    }
  } catch (err) {
    button.disabled = false;
    showToast('停止失败：' + err.message, 'error');
  }
}

function getRequestedMax(platform) {
  const input = document.getElementById(`${platform}-max`);
  return input ? Number.parseInt(input.value, 10) || 0 : 0;
}

function collectParams(platform) {
  const params = [];
  
  switch(platform) {
    case 'admuse':
      params.push('--time-range', document.getElementById('admuse-time').value);
      params.push('--industries', document.getElementById('admuse-industry').value);
      params.push('--max-downloads', document.getElementById('admuse-max').value);
      break;
      
    case 'appgrowing':
      const keyword = document.getElementById('appgrowing-keyword').value;
      if (keyword) params.push('--keyword', keyword);

      const resultView = document.querySelector('input[name="appgrowing-view"]:checked')?.value || '广告';
      params.push('--view', resultView);
      
      const media = Array.from(document.querySelectorAll('#appgrowing-media input:checked'))
        .map(cb => cb.value);
      if (media.length > 0) params.push('--media', media.join(','));

      const traffic = Array.from(document.querySelectorAll('#appgrowing-traffic input:checked'))
        .map(cb => cb.value);
      if (traffic.length > 0) params.push('--traffic', traffic.join(','));
      
      params.push('--type', document.getElementById('appgrowing-type').value);
      params.push('--ratio', document.getElementById('appgrowing-ratio').value);
      params.push('--sort', document.getElementById('appgrowing-sort').value);
      params.push('--max', document.getElementById('appgrowing-max').value);
      break;
      
    case 'reyun':
      const reyunKeyword = document.getElementById('reyun-keyword').value;
      if (reyunKeyword) params.push('--keyword', reyunKeyword);
      
      const type = document.getElementById('reyun-type').value;
      if (type) params.push('--type', type);
      
      params.push('--sort', document.getElementById('reyun-sort').value);
      params.push('--max', document.getElementById('reyun-max').value);
      break;
      
    case 'internal':
      break;
  }
  
  return params;
}

// ============================ 分类：平台识别与输出目录 ============================

// 平台决定用哪套方向规则与最终目录结构，必须显式传给主进程；
// 不传的话主进程会退回 internal，外部平台（妙思/有米云/热云）就会套错规则。
const PLATFORM_LABELS = {
  internal: '内部系统',
  admuse: '妙思',
  appgrowing: '有米云',
  reyun: '热云'
};

// 路径关键词 → 平台。同一个路径里可能出现多个（如「有米云素材\妙思\20260921-2」），
// 取**最靠后**命中的那个，因为它最贴近实际投放渠道。
const PLATFORM_PATH_HINTS = [
  ['妙思', 'admuse'],
  ['admuse', 'admuse'],
  ['有米云', 'appgrowing'],
  ['appgrowing', 'appgrowing'],
  ['热云', 'reyun'],
  ['reyun', 'reyun'],
  ['内部系统', 'internal'],
  ['internal', 'internal']
];

function detectPlatformFromPath(folderPath) {
  const parts = String(folderPath || '').split(/[\\/]+/).filter(Boolean);
  let found = '';
  for (const part of parts) {
    const lower = part.toLowerCase();
    for (const [keyword, key] of PLATFORM_PATH_HINTS) {
      if (lower.includes(keyword.toLowerCase())) found = key;
    }
  }
  return found;
}

/** 把下拉里的 auto 解析成真实平台 key。 */
function resolvePlatform(mode) {
  const select = document.getElementById(`platform-${mode}`);
  const chosen = select ? select.value : 'auto';
  if (chosen && chosen !== 'auto') return chosen;
  const folder = (document.getElementById(`classify-dir-${mode}`) || {}).value || '';
  return detectPlatformFromPath(folder) || 'internal';
}

function updatePlatformHint(mode) {
  const hint = document.getElementById(`platform-hint-${mode}`);
  if (!hint) return;
  const select = document.getElementById(`platform-${mode}`);
  const folder = (document.getElementById(`classify-dir-${mode}`) || {}).value || '';
  if (select && select.value !== 'auto') {
    hint.textContent = `将按「${PLATFORM_LABELS[select.value]}」规则分类并输出到对应目录结构。`;
    return;
  }
  const detected = detectPlatformFromPath(folder);
  hint.textContent = detected
    ? `已按路径识别为「${PLATFORM_LABELS[detected]}」；识别不对可手动改上面的下拉。`
    : '未从路径识别出平台，将按「内部系统」处理；请确认上面的分类平台。';
}

/** 目录框被手动输入/粘贴时重新识别平台。 */
function onClassifyDirInput(mode) {
  updatePlatformHint(mode);
}

async function selectOutputDir(mode) {
  const folder = await window.electronAPI.selectFolder();
  if (folder) {
    const input = document.getElementById(`output-dir-${mode}`);
    if (input) input.value = folder;
  }
}

function selectOutputDirFast() {
  return selectOutputDir('fast');
}

function selectOutputDirPureApi() {
  return selectOutputDir('pure-api');
}

async function selectClassifyDirDirection() {
  const folder = await window.electronAPI.selectFolder();
  if (folder) {
    document.getElementById('classify-dir-direction').value = folder;
  }
}

async function selectClassifyDirFast() {
  const folder = await window.electronAPI.selectFolder();
  if (folder) {
    document.getElementById('classify-dir-fast').value = folder;
    updatePlatformHint('fast');
  }
}

async function selectClassifyDirPureApi() {
  const folder = await window.electronAPI.selectFolder();
  if (folder) {
    document.getElementById('classify-dir-pure-api').value = folder;
    updatePlatformHint('pure-api');
  }
}

async function startClassify() {
  if (classifyRunning) return;
  clearLog('classify-log');
  appendLog('classify-log', '开始 AI 分类...\n\n');
  
  try {
    let result;
    
    if (currentMode === 'fast') {
      const folder = document.getElementById('classify-dir-fast').value.trim();
      const output = document.getElementById('output-dir-fast').value.trim();
      const platform = resolvePlatform('fast');
      const parallel = parseInt(document.getElementById('parallel-count-fast').value);
      const minConfidence = parseFloat(document.getElementById('min-confidence-fast').value);
      const applyCopy = document.getElementById('apply-copy-fast').checked;
      const fileAction = document.querySelector('input[name="file-action-fast"]:checked').value;

      if (!folder) {
        alert('请填写素材目录（可直接粘贴路径）');
        return;
      }

      beginClassifyProgress();
      appendLog('classify-log',
        `素材目录: ${folder}\n分类平台: ${PLATFORM_LABELS[platform]}（${platform}）\n` +
        `输出目录: ${output || '自动（素材目录旁的「分类结果」）'}\n\n`);
      result = await window.electronAPI.runPureApi({
        folder,
        output,
        platform,
        parallel,
        minConfidence,
        applyCopy,
        fileAction
      });
    } else if (currentMode === 'direction') {
      const folder = document.getElementById('classify-dir-direction').value;
      const parallel = parseInt(document.getElementById('parallel-count-direction').value);
      const minConfidence = parseFloat(document.getElementById('min-confidence').value);
      const applyCopy = document.getElementById('apply-copy-direction').checked;
      const fileAction = document.querySelector('input[name="file-action-direction"]:checked').value;
      
      if (!folder) {
        alert('请选择图片目录');
        return;
      }

      beginClassifyProgress();
      result = await window.electronAPI.runDirection({
        folder,
        parallel,
        minConfidence,
        applyCopy,
        fileAction
      });
    } else if (currentMode === 'pure-api') {
      const folder = document.getElementById('classify-dir-pure-api').value.trim();
      const output = document.getElementById('output-dir-pure-api').value.trim();
      const platform = resolvePlatform('pure-api');
      const parallel = parseInt(document.getElementById('parallel-count-pure-api').value);
      const minConfidence = parseFloat(document.getElementById('min-confidence-pure-api').value);
      const applyCopy = document.getElementById('apply-copy-pure-api').checked;
      const fileAction = document.querySelector('input[name="file-action-pure-api"]:checked').value;

      if (!folder) {
        alert('请填写素材目录（可直接粘贴路径）');
        return;
      }

      beginClassifyProgress();
      appendLog('classify-log',
        `素材目录: ${folder}\n分类平台: ${PLATFORM_LABELS[platform]}（${platform}）\n` +
        `输出目录: ${output || '自动（素材目录旁的「分类结果」）'}\n\n`);
      result = await window.electronAPI.runPureApi({
        folder,
        output,
        platform,
        parallel,
        minConfidence,
        applyCopy,
        fileAction
      });
    }
    
    if (result.success) {
      appendLog('classify-log', '\n\n✓ 分类完成！\n');
      if (result.output) {
        appendLog('classify-log', `输出目录: ${result.output}\n`);
      }
      if (result.auditExcel) {
        appendLog('classify-log', `调试审计 Excel: ${result.auditExcel}\n`);
      }
      finishClassifyProgress('completed', `结果已保存到 ${result.output || '分类结果目录'}`);
    } else if (classifyStopRequested) {
      finishClassifyProgress('stopped', '任务已由用户停止');
    } else {
      appendLog('classify-log', '\n\n✗ 分类失败：' + (result.error || '未知错误') + '\n');
      finishClassifyProgress('failed', result.error || '分类任务执行失败');
    }
  } catch (err) {
    appendLog('classify-log', `\n\n✗ 错误: ${err.message}\n`);
    finishClassifyProgress('failed', err.message);
  }
}

async function stopClassify() {
  if (!classifyRunning) return;
  classifyStopRequested = true;
  classifyProgress.phase = '正在停止';
  classifyProgress.message = '正在安全停止分类进程…';
  renderClassifyProgress();
  try {
    const result = await window.electronAPI.stopClassify();
    if (result.success) {
      appendLog('classify-log', '\n⚠️ 已停止分类任务\n');
      finishClassifyProgress('stopped', '任务已停止，已完成的文件不受影响');
    }
  } catch (err) {
    appendLog('classify-log', `\n✗ 停止失败: ${err.message}\n`);
    classifyStopRequested = false;
    classifyProgress.phase = '停止失败';
    classifyProgress.message = err.message;
    renderClassifyProgress();
  }
}

function getPlatformName(platform) {
  const names = {
    'admuse': '妙思',
    'appgrowing': '有米云',
    'reyun': '热云',
    'internal': '内部系统'
  };
  return names[platform] || platform;
}

/* ==================================================================
 * 自动化计划 + 运行历史
 * ================================================================== */

const WEEKDAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const PLAN_PLATFORMS = ['admuse', 'appgrowing', 'reyun', 'internal'];

const RUN_STATUS_LABELS = {
  running: '运行中',
  queued: '排队中',
  success: '成功',
  failed: '失败',
  warning: '部分完成',
  stopped: '已停止'
};

const RUN_TRIGGER_LABELS = {
  schedule: '定时',
  manual: '手动',
  rerun: '按原日期重跑'
};

let planCache = [];
let currentRun = null;
let runCache = [];

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDuration(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor((Number(milliseconds) || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hours > 0
    ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
    : `${pad(minutes)}:${pad(seconds)}`;
}

function statusPill(status) {
  const key = RUN_STATUS_LABELS[status] ? status : 'stopped';
  return `<span class="auto-status" data-status="${key}">${RUN_STATUS_LABELS[key]}</span>`;
}

function initAutomation() {
  renderPlanPlatformOptions();
  renderWeekdayOptions();

  const api = window.electronAPI;
  if (!api) return;

  if (api.onPlansChanged) api.onPlansChanged((plans) => { planCache = plans || []; renderPlans(); });
  if (api.onQueueState) api.onQueueState(renderQueueState);
  if (api.onRunStarted) api.onRunStarted(() => { refreshRuns(); refreshQueueState(); });
  if (api.onRunFinished) api.onRunFinished(() => { refreshRuns(); refreshPlans(); });
  if (api.onSchedulerTick) api.onSchedulerTick(() => { refreshQueueState(); });

  // 计划编辑表单：实时刷新底部摘要
  ['plan-name', 'plan-time', 'plan-platform', 'plan-max', 'plan-internal-date'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', updatePlanSummary);
  });
  document.getElementById('plan-platform').addEventListener('change', (event) => {
    showPlatformParams(event.target.value);
    updatePlanSummary();
  });
  const followGlobalToggle = document.getElementById('plan-follow-global');
  if (followGlobalToggle) {
    followGlobalToggle.addEventListener('change', () => {
      applyFollowGlobalState();
      updatePlanSummary();
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!document.getElementById('planModal').hidden) closePlanEditor();
    if (!document.getElementById('runLogModal').hidden) closeRunLog();
  });

  refreshPlans();
  refreshQueueState();
  refreshSchedulerState();
  refreshRuns();
}

/* ---------------- 采集计划：列表 ---------------- */

async function refreshPlans() {
  if (!window.electronAPI) return;
  try {
    planCache = (await window.electronAPI.listPlans()) || [];
    renderPlans();
  } catch (err) {
    console.error('读取计划失败', err);
  }
}

function nextRunText(plan) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(plan.schedule_time || '');
  if (!match) return '时间未设置';
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  const weekdays = Array.isArray(plan.weekdays) && plan.weekdays.length ? plan.weekdays : [0, 1, 2, 3, 4, 5, 6];

  const now = new Date();
  for (let offset = 0; offset < 8; offset += 1) {
    const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset, hour, minute, 0, 0);
    if (candidate <= now) continue;
    const weekday = (candidate.getDay() + 6) % 7;
    if (!weekdays.includes(weekday)) continue;
    const isToday = offset === 0;
    const isTomorrow = offset === 1;
    const dayText = isToday ? '今天' : (isTomorrow ? '明天' : WEEKDAY_NAMES[weekday]);
    return `${dayText} ${plan.schedule_time}`;
  }
  return `${plan.schedule_time}（本周不再触发）`;
}

function weekdayText(plan) {
  const weekdays = Array.isArray(plan.weekdays) ? plan.weekdays : [];
  if (weekdays.length === 0 || weekdays.length === 7) return '每天';
  return weekdays.map((d) => WEEKDAY_NAMES[d]).join(' ');
}

function planRootText(plan) {
  const following = plan.use_global_root === true;
  const dir = plan.download_root || currentExportDirValue();
  return following ? `${dir}（跟随导出目录）` : `${dir}（固定）`;
}

function renderPlans() {
  const list = document.getElementById('autoPlanList');
  const empty = document.getElementById('autoPlanEmpty');
  if (!list) return;

  if (!planCache.length) {
    list.innerHTML = '';
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  list.innerHTML = planCache.map((plan) => {
    const lastStatus = plan.last_status && RUN_STATUS_LABELS[plan.last_status]
      ? statusPill(plan.last_status)
      : '<span class="auto-status" data-status="stopped">未运行</span>';
    const chain = plan.classify_enabled === false
      ? '下载后不分类'
      : `下载后自动分类（并发 ${plan.classify_parallel} · 阈值 ${plan.classify_min_confidence}）`;

    return `
      <article class="auto-plan-card" data-enabled="${plan.enabled !== false}">
        <div class="auto-plan-head">
          <div class="auto-plan-title">
            <strong>${escapeHtml(plan.name)}</strong>
            <span class="auto-badge">${escapeHtml(getPlatformName(plan.platform))}</span>
          </div>
          <div class="auto-plan-schedule">
            <em>${escapeHtml(weekdayText(plan))}</em>
            <span>${escapeHtml(plan.schedule_time)}</span>
          </div>
        </div>

        <div class="auto-plan-meta">
          <span>下次执行：<b>${plan.enabled !== false ? escapeHtml(nextRunText(plan)) : '已停用'}</b></span>
          <span>上次运行：<b>${plan.last_run_at ? escapeHtml(plan.last_run_at) : '从未运行'}</b></span>
          <span>下载目录：<b>${escapeHtml(planRootText(plan))}</b></span>
          ${plan.platform === 'internal' && plan.filter_date
            ? `<span>数据日期：<b>${escapeHtml(plan.filter_date)}（固定，产出目录 ${escapeHtml(batchDateFromFilter(plan.filter_date))}）</b></span>`
            : ''}
          <span>上次结果：${lastStatus}</span>
          <span>一条龙：<b>${escapeHtml(chain)}</b></span>
          ${plan.last_message ? `<span>说明：${escapeHtml(plan.last_message)}</span>` : ''}
        </div>

        <div class="auto-plan-actions">
          <button class="auto-ghost auto-run" type="button" onclick="runPlanNow('${plan.id}')">立即执行</button>
          <button class="auto-ghost" type="button" onclick="openPlanEditor('${plan.id}')">编辑</button>
          <button class="auto-ghost" type="button" onclick="togglePlan('${plan.id}', ${plan.enabled === false})">
            ${plan.enabled === false ? '启用' : '停用'}
          </button>
          <button class="auto-ghost auto-danger" type="button" onclick="deletePlan('${plan.id}')">删除</button>
        </div>
      </article>
    `;
  }).join('');
}

async function runPlanNow(planId) {
  const result = await window.electronAPI.runPlanNow(planId);
  if (result && result.success) {
    showToast('已排入执行队列，可在“下载中心”看进度');
    refreshPlans();
    refreshQueueState();
  } else {
    showToast((result && result.error) || '执行失败', 'error');
  }
}

async function togglePlan(planId, enabled) {
  const result = await window.electronAPI.togglePlan(planId, enabled);
  if (result && result.success) {
    showToast(enabled ? '计划已启用' : '计划已停用');
    refreshPlans();
  } else {
    showToast((result && result.error) || '操作失败', 'error');
  }
}

async function deletePlan(planId) {
  const plan = planCache.find((item) => item.id === planId);
  if (!window.confirm(`确定删除计划「${plan ? plan.name : planId}」吗？此操作不可撤销。`)) return;
  const result = await window.electronAPI.deletePlan(planId);
  if (result && result.success) {
    showToast('计划已删除');
    refreshPlans();
  } else {
    showToast((result && result.error) || '删除失败', 'error');
  }
}

/* ---------------- 采集计划：编辑弹窗 ---------------- */

function renderPlanPlatformOptions() {
  const select = document.getElementById('plan-platform');
  if (!select) return;
  select.innerHTML = PLAN_PLATFORMS
    .map((key) => `<option value="${key}">${getPlatformName(key)}</option>`)
    .join('');
}

function renderWeekdayOptions() {
  const row = document.getElementById('plan-weekdays');
  if (!row) return;
  row.innerHTML = WEEKDAY_NAMES.map((label, index) => `
    <label><input type="checkbox" value="${index}" checked><span>${label}</span></label>
  `).join('');
  row.querySelectorAll('input').forEach((input) => {
    input.addEventListener('change', updatePlanSummary);
  });
}

function showPlatformParams(platform) {
  document.querySelectorAll('.plan-platform-block').forEach((block) => {
    block.classList.toggle('active', block.dataset.platform === platform);
  });
  const nameEl = document.getElementById('plan-platform-name');
  if (nameEl) nameEl.textContent = getPlatformName(platform);
}

/** 下拉里没有该值时补一个选项，避免保存过的值显示为空。 */
function setSelectValue(selectId, value) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const target = String(value === undefined || value === null ? '' : value);
  if (target && !Array.from(select.options).some((option) => option.value === target)) {
    const option = document.createElement('option');
    option.value = target;
    option.textContent = target;
    select.appendChild(option);
  }
  select.value = target;
}

function setChecked(id, checked) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(checked);
}

function currentExportDirValue() {
  const display = document.getElementById('exportDirDisplay');
  return (display && display.textContent)
    || currentExportDir
    || (typeof DEFAULT_EXPORT_DIR !== 'undefined' ? DEFAULT_EXPORT_DIR : '');
}

/** 勾选「跟随导出目录」后目录输入框只做展示，实际路径由主进程实时解析。 */
function applyFollowGlobalState() {
  const checkbox = document.getElementById('plan-follow-global');
  const input = document.getElementById('plan-root');
  const hint = document.getElementById('plan-root-hint');
  if (!checkbox || !input) return;

  const following = checkbox.checked;
  input.disabled = following;
  if (following) input.value = currentExportDirValue();
  if (hint) {
    hint.textContent = following
      ? '实际落盘到「下载中心」当前导出目录，改一处即对所有跟随的计划生效'
      : '该计划固定使用上面填写的目录，不随导出目录变化';
    hint.classList.toggle('is-follow', following);
  }
}

function openPlanEditor(planId) {
  const plan = planId ? planCache.find((item) => item.id === planId) : null;
  const modal = document.getElementById('planModal');

  document.getElementById('planEditorTitle').textContent = plan ? `编辑计划 · ${plan.name}` : '新建采集计划';
  document.getElementById('plan-id').value = plan ? plan.id : '';
  document.getElementById('plan-name').value = plan ? plan.name : '';
  document.getElementById('plan-time').value = plan ? plan.schedule_time : '06:30';
  // 新建的计划默认跟随「下载中心」的导出目录，避免又写死成一份与全局设置脱节的路径
  document.getElementById('plan-follow-global').checked = plan ? plan.use_global_root === true : true;
  document.getElementById('plan-root').value = plan
    ? (plan.download_root || '')
    : currentExportDirValue();
  document.getElementById('plan-max').value = plan ? plan.max_downloads : 500;
  applyFollowGlobalState();

  setSelectValue('plan-platform', plan ? plan.platform : 'admuse');
  showPlatformParams(plan ? plan.platform : 'admuse');

  setChecked('plan-enabled', plan ? plan.enabled !== false : true);
  setChecked('plan-background', plan ? plan.background_mode !== false : true);

  // 平台参数
  const keyword = plan ? plan.keyword : '';
  document.getElementById('plan-admuse-keyword').value = keyword;
  document.getElementById('plan-appgrowing-keyword').value = keyword;
  document.getElementById('plan-reyun-keyword').value = keyword;

  setSelectValue('plan-time-range', plan ? plan.time_range : '7d');
  setSelectValue('plan-industries', plan && plan.industries && plan.industries.length
    ? plan.industries[0] : 'insurance');
  setSelectValue('plan-admuse-sort', plan && plan.sort ? plan.sort : '跑量得分降序');
  document.getElementById('plan-score').value = plan ? plan.score : 40;

  setSelectValue('plan-result-view', plan ? plan.result_view : '广告');
  setSelectValue('plan-material-type', plan ? plan.material_type : '全部素材类型');
  setSelectValue('plan-material-ratio', plan ? plan.material_ratio : '全部素材比例');
  document.getElementById('plan-appgrowing-sort').value =
    plan && plan.sort && plan.platform === 'appgrowing' ? plan.sort : '';
  document.getElementById('plan-media').value = plan && plan.media ? plan.media.join(',') : '';
  document.getElementById('plan-traffic').value = plan && plan.traffic ? plan.traffic.join(',') : '';

  setSelectValue('plan-creative-type', plan ? plan.creative_type : '图片');
  setSelectValue('plan-reyun-sort', plan && plan.sort && plan.platform === 'reyun'
    ? plan.sort : '曝光预估');

  setSelectValue('plan-internal-type', plan ? plan.internal_material_type : '图片');
  const internalDate = document.getElementById('plan-internal-date');
  if (internalDate) {
    // 留空 = 跟随运行日自动取昨天；填了 = 每次执行都下载这一天（补采用）
    internalDate.value = plan && plan.filter_date ? plan.filter_date : '';
    internalDate.max = toDateInputValue(new Date());
  }
  document.getElementById('plan-internal-keyword').value = plan ? plan.internal_keyword : '';
  document.getElementById('plan-internal-top').value = plan ? plan.internal_top_n : 50;

  // 一条龙
  setChecked('plan-classify', plan ? plan.classify_enabled !== false : true);
  document.getElementById('plan-classify-parallel').value = plan ? plan.classify_parallel : 5;
  document.getElementById('plan-classify-confidence').value = plan ? plan.classify_min_confidence : 0.85;
  setSelectValue('plan-classify-action', plan ? plan.classify_action : 'copy');
  setChecked('plan-classify-refresh', plan ? plan.classify_refresh_api === true : false);

  // 星期
  const weekdays = plan && Array.isArray(plan.weekdays) ? plan.weekdays : [0, 1, 2, 3, 4, 5, 6];
  document.querySelectorAll('#plan-weekdays input').forEach((input) => {
    input.checked = weekdays.includes(Number(input.value));
  });

  updatePlanSummary();
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
}

function closePlanEditor() {
  const modal = document.getElementById('planModal');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
}

function updatePlanSummary() {
  const el = document.getElementById('planEditorSummary');
  if (!el) return;
  const time = document.getElementById('plan-time').value || '06:30';
  const weekdays = Array.from(document.querySelectorAll('#plan-weekdays input:checked'))
    .map((input) => Number(input.value));
  const dayText = weekdays.length === 0 || weekdays.length === 7
    ? '每天'
    : weekdays.map((d) => WEEKDAY_NAMES[d]).join(' ');
  const platform = document.getElementById('plan-platform').value;
  const internalDateEl = document.getElementById('plan-internal-date');
  const dataDate = platform === 'internal' && internalDateEl ? internalDateEl.value : '';
  const dateText = dataDate ? `（固定数据日期 ${dataDate}）` : '';
  const chain = document.getElementById('plan-classify').checked ? '，下载后自动分类' : '';
  el.textContent = `${dayText} ${time} 执行${dateText}${chain}`;
}

async function pickPlanRoot() {
  const checkbox = document.getElementById('plan-follow-global');
  if (checkbox && checkbox.checked) {
    showToast('当前计划跟随「下载中心」的导出目录，请先取消勾选再单独指定', 'error');
    return;
  }
  const folder = await window.electronAPI.selectFolder(document.getElementById('plan-root').value);
  if (folder) document.getElementById('plan-root').value = folder;
}

function splitList(value) {
  return String(value || '')
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function collectPlanForm() {
  const platform = document.getElementById('plan-platform').value;
  const keywordMap = {
    admuse: 'plan-admuse-keyword',
    appgrowing: 'plan-appgrowing-keyword',
    reyun: 'plan-reyun-keyword'
  };
  const sortMap = {
    admuse: 'plan-admuse-sort',
    appgrowing: 'plan-appgrowing-sort',
    reyun: 'plan-reyun-sort'
  };

  const plan = {
    id: document.getElementById('plan-id').value || '',
    name: document.getElementById('plan-name').value,
    platform,
    schedule_time: document.getElementById('plan-time').value || '06:30',
    weekdays: Array.from(document.querySelectorAll('#plan-weekdays input:checked'))
      .map((input) => Number(input.value)),
    download_root: document.getElementById('plan-root').value,
    use_global_root: document.getElementById('plan-follow-global').checked,
    enabled: document.getElementById('plan-enabled').checked,
    background_mode: document.getElementById('plan-background').checked,
    max_downloads: Number(document.getElementById('plan-max').value) || 500,

    keyword: keywordMap[platform] ? document.getElementById(keywordMap[platform]).value : '',
    sort: sortMap[platform] ? document.getElementById(sortMap[platform]).value : '',

    time_range: document.getElementById('plan-time-range').value,
    industries: [document.getElementById('plan-industries').value],
    score: Number(document.getElementById('plan-score').value) || 40,

    result_view: document.getElementById('plan-result-view').value,
    material_type: document.getElementById('plan-material-type').value,
    material_ratio: document.getElementById('plan-material-ratio').value,
    media: splitList(document.getElementById('plan-media').value),
    traffic: splitList(document.getElementById('plan-traffic').value),

    creative_type: document.getElementById('plan-creative-type').value,

    internal_material_type: document.getElementById('plan-internal-type').value,
    internal_keyword: document.getElementById('plan-internal-keyword').value,
    internal_top_n: Number(document.getElementById('plan-internal-top').value) || 50,
    // 仅内部系统有意义：固定下载某一天的表格；留空 = 跟随运行日
    filter_date: platform === 'internal'
      ? String(document.getElementById('plan-internal-date').value || '').trim()
      : '',

    classify_enabled: document.getElementById('plan-classify').checked,
    classify_parallel: Number(document.getElementById('plan-classify-parallel').value) || 5,
    classify_min_confidence: Number(document.getElementById('plan-classify-confidence').value),
    classify_action: document.getElementById('plan-classify-action').value,
    classify_refresh_api: document.getElementById('plan-classify-refresh').checked
  };

  if (!plan.id) delete plan.id;
  return plan;
}

async function savePlanEditor() {
  const plan = collectPlanForm();
  if (!plan.name.trim()) {
    showToast('请填写计划名称', 'error');
    return;
  }
  if (!plan.weekdays.length) {
    showToast('请至少选择一个执行日', 'error');
    return;
  }
  if (plan.platform === 'internal' && plan.filter_date) {
    if (!isValidDateInput(plan.filter_date)) {
      showToast('数据日期格式不正确，请重新选择', 'error');
      return;
    }
    if (plan.filter_date > toDateInputValue(new Date())) {
      showToast('数据日期不能晚于今天', 'error');
      return;
    }
  }

  const result = await window.electronAPI.savePlan(plan);
  if (result && result.success) {
    showToast('计划已保存');
    closePlanEditor();
    refreshPlans();
    refreshSchedulerState();
  } else {
    showToast((result && result.error) || '保存失败', 'error');
  }
}

/* ---------------- 队列 / 调度器状态 ---------------- */

const QUEUE_LANE_LABELS = { collect: '采集', classify: '分类' };

function queueJobLabel(job) {
  if (job.kind === 'plan-classify') return `${job.planName || '计划'} · 分类`;
  if (job.kind === 'classify') return '分类任务';
  return job.planName || job.platformLabel || '采集任务';
}

/**
 * 渲染队列状态。采集与分类是两条独立并行的车道，
 * 所以分别展示，让"下载在跑、分类也在跑"这件事看得见。
 */
function renderQueueState(state) {
  const bar = document.getElementById('autoQueueBar');
  const summary = document.getElementById('queueSummary');
  const list = document.getElementById('queueList');
  if (!bar || !summary || !list) return;

  // 兼容只有 active/pending 的旧格式
  const twoLane = state && (state.collect || state.classify);
  const lanesData = twoLane
    ? state
    : {
      collect: { active: (state && state.active) || null, pending: (state && state.pending) || [] },
      classify: { active: null, pending: [] }
    };

  const parts = ['collect', 'classify'].map((lane) => {
    const data = lanesData[lane] || {};
    return {
      lane,
      label: QUEUE_LANE_LABELS[lane],
      active: data.active || null,
      pending: Array.isArray(data.pending) ? data.pending : []
    };
  });

  const totalActive = parts.filter((part) => part.active).length;
  const totalPending = parts.reduce((sum, part) => sum + part.pending.length, 0);
  const idle = totalActive === 0 && totalPending === 0;

  bar.dataset.idle = String(idle);
  // 两条车道同时在跑时给队列条一个状态，方便一眼看出是并行而非卡住
  bar.dataset.parallel = String(totalActive > 1);

  if (idle) {
    summary.textContent = '当前无任务';
    list.innerHTML = '';
    return;
  }

  if (totalActive > 1) {
    summary.textContent = `采集与分类并行执行中（另有 ${totalPending} 个等待）`;
  } else if (totalActive === 1) {
    const running = parts.find((part) => part.active);
    summary.textContent = `${running.label}车道执行中（另有 ${totalPending} 个等待）`;
  } else {
    summary.textContent = `队列中 ${totalPending} 个任务`;
  }

  const chips = [];
  parts.forEach((part) => {
    if (part.active) {
      chips.push(
        `<span class="auto-queue-chip" data-lane="${part.lane}">`
        + `<b>${part.label} · 运行中</b>${escapeHtml(queueJobLabel(part.active))}</span>`
      );
    }
    part.pending.forEach((job) => {
      chips.push(
        `<span class="auto-queue-chip" data-lane="${part.lane}">`
        + `<b>${part.label} · 等待</b>${escapeHtml(queueJobLabel(job))}</span>`
      );
    });
  });
  list.innerHTML = chips.join('');
}

async function refreshQueueState() {
  if (!window.electronAPI) return;
  try {
    renderQueueState(await window.electronAPI.getQueueState());
  } catch (err) {
    console.error('读取队列状态失败', err);
  }
}

async function refreshSchedulerState() {
  if (!window.electronAPI) return;
  const el = document.getElementById('schedulerState');
  if (!el) return;
  try {
    const status = await window.electronAPI.getSchedulerStatus();
    const enabledCount = planCache.filter((plan) => plan.enabled !== false).length;
    const running = Boolean(status && status.running);
    el.dataset.state = running ? 'on' : 'off';
    el.querySelector('span').textContent = running
      ? `调度器运行中 · 每 ${Math.round((status.intervalMs || 30000) / 1000)} 秒检查 · ${enabledCount} 个计划已启用`
      : '调度器未启动';
  } catch (err) {
    el.querySelector('span').textContent = '调度器状态读取失败';
  }
}

/* ---------------- 运行历史 ---------------- */

async function refreshRuns() {
  if (!window.electronAPI) return;
  try {
    const runs = (await window.electronAPI.listRuns()) || [];
    runCache = runs;
    renderRuns(runs);
  } catch (err) {
    console.error('读取运行历史失败', err);
  }
}

function renderRuns(runs) {
  const body = document.getElementById('autoRunBody');
  const empty = document.getElementById('autoRunEmpty');
  const summary = document.getElementById('historySummary');
  if (!body) return;

  const today = new Date().toISOString().slice(0, 10);
  const todayCount = runs.filter((run) => String(run.started_at || '').startsWith(today)).length;
  if (summary) {
    summary.dataset.state = runs.length ? 'on' : 'off';
    summary.querySelector('span').textContent = runs.length
      ? `共 ${runs.length} 条记录 · 今日 ${todayCount} 次`
      : '暂无记录';
  }

  if (!runs.length) {
    body.innerHTML = '';
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  body.innerHTML = runs.map((run) => {
    const classifyText = run.classified
      ? '<span class="auto-status" data-status="success">已完成</span>'
      : (run.classify_error
        ? '<span class="auto-status" data-status="failed">失败</span>'
        : '<span class="auto-status" data-status="stopped">未执行</span>');
    // 同一天的多次采集共用一个日期目录，所以要区分「本次新增」和「当天累计」，
    // 否则第二次采集会显示成整天的总量，看不出这一趟下了多少。
    // 这里只拼数字，不含用户输入，所以整段可以直接放进 HTML（不做转义）。
    const added = Number.isFinite(run.added) ? run.added : null;
    const total = run.found || 0;
    const failed = run.failed || 0;
    const isCumulative = added !== null && added !== total;
    let downloadText = '—';
    if (total > 0 || failed > 0) {
      downloadText = `${isCumulative ? added : total} 张${failed ? `（失败 ${failed}）` : ''}`;
      if (isCumulative) {
        downloadText += `<small class="auto-run-cumulative">当天共 ${total}</small>`;
      }
    }
    const triggerText = RUN_TRIGGER_LABELS[run.trigger] || '手动';
    // 内部系统的查询日期是关键信息：采错日期等于整批数据作废
    const dateHint = run.filter_date
      ? `<small class="auto-run-date">查询 ${escapeHtml(run.filter_date)}</small>`
      : '';
    const canRerun = run.status !== 'running' && Boolean(run.plan_id);

    return `
      <tr>
        <td class="auto-run-time">${escapeHtml(run.started_at || '')}</td>
        <td class="auto-run-plan">
          ${escapeHtml(run.plan_name || '（手动任务）')}
          <small>${escapeHtml(run.platform_label || getPlatformName(run.platform))}</small>
          ${dateHint}
        </td>
        <td>${escapeHtml(triggerText)}</td>
        <td>${statusPill(run.status)}</td>
        <td>${downloadText}</td>
        <td>${classifyText}</td>
        <td class="auto-run-time">${formatDuration(run.duration_ms)}</td>
        <td>
          <div class="auto-run-actions">
            ${canRerun ? `<button type="button" class="auto-run-rerun" onclick="rerunRunById('${run.id}')" title="沿用原运行日期重新执行一次">再次执行</button>` : ''}
            <button type="button" onclick="openRunDetail('${run.id}')">日志</button>
            ${run.batch_dir ? `<button type="button" onclick="openRunFolderByRun('${run.id}', 'batch')">目录</button>` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

/** 再次执行某次运行：沿用原日期，避免隔天重跑取错数据。 */
async function rerunRunById(runId) {
  const run = (runCache || []).find((item) => item.id === runId);
  const label = run ? `${run.plan_name || '手动任务'} · ${run.started_at}` : '该任务';
  const dateHint = run && run.filter_date ? `\n查询日期将保持为 ${run.filter_date}` : '';
  const batchHint = run && run.batch_date
    ? `\n输出并入 ${run.batch_date} 这个日期目录（已存在的素材会自动跳过，不会重复下载）`
    : '';

  if (!window.confirm(`再次执行「${label}」？${dateHint}${batchHint}\n\n将排入队列，可在「下载中心」查看进度。`)) {
    return;
  }

  const result = await window.electronAPI.rerunRun(runId);
  if (result && result.success) {
    showToast(result.filterDate
      ? `已排入队列 · 沿用查询日期 ${result.filterDate}`
      : '已排入队列，可在「下载中心」看进度');
    refreshRuns();
    refreshQueueState();
  } else {
    showToast((result && result.error) || '重跑失败', 'error');
  }
}

async function openRunDetail(runId) {
  const run = await window.electronAPI.runDetail(runId);
  if (!run) {
    showToast('找不到该运行记录', 'error');
    return;
  }
  currentRun = run;

  document.getElementById('runLogTitle').textContent = run.plan_name || '手动任务';
  document.getElementById('runLogMeta').textContent =
    `${run.started_at} · ${run.platform_label || getPlatformName(run.platform)} · ` +
    `下载 ${run.found || 0} 张 · 耗时 ${formatDuration(run.duration_ms)}`;
  const statusEl = document.getElementById('runLogStatus');
  statusEl.textContent = RUN_STATUS_LABELS[run.status] || run.status;
  statusEl.className = run.status === 'failed' || run.status === 'warning' ? 'pending' : '';

  const body = document.getElementById('runLogBody');
  const head = [
    `计划：${run.plan_name || '（手动任务）'}`,
    `平台：${run.platform_label || getPlatformName(run.platform)}`,
    `触发：${RUN_TRIGGER_LABELS[run.trigger] || '手动'}`,
    `状态：${RUN_STATUS_LABELS[run.status] || run.status}`,
    `说明：${run.message || '—'}`,
    // 日期是复跑的关键信息，单独列出来方便核对
    run.filter_date ? `查询日期：${run.filter_date}` : '',
    run.batch_date ? `批次日期：${run.batch_date}` : '',
    run.batch_dir ? `批次目录：${run.batch_dir}` : '',
    run.classification_dir ? `分类结果：${run.classification_dir}` : '',
    '',
    '---------------- 完整日志 ----------------',
    ''
  ].filter(Boolean).join('\n');
  body.textContent = head + (run.logContent || '');

  const modal = document.getElementById('runLogModal');
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
}

function closeRunLog() {
  const modal = document.getElementById('runLogModal');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  currentRun = null;
}

function openCurrentRunFolder(kind) {
  if (!currentRun) return;
  const target = kind === 'classification'
    ? (currentRun.classification_dir || currentRun.raw_dir)
    : (currentRun.batch_dir || currentRun.raw_dir);
  if (!target) {
    showToast('该记录没有关联目录', 'error');
    return;
  }
  window.electronAPI.openPath(target);
}

async function openRunFolderByRun(runId, kind) {
  const run = await window.electronAPI.runDetail(runId);
  if (!run) return;
  const target = kind === 'classification'
    ? (run.classification_dir || run.raw_dir)
    : (run.batch_dir || run.raw_dir);
  if (target) window.electronAPI.openPath(target);
}

function copyRunLog() {
  const body = document.getElementById('runLogBody');
  if (!body) return;
  navigator.clipboard.writeText(body.textContent || '')
    .then(() => showToast('日志已复制到剪贴板'))
    .catch(() => showToast('复制失败', 'error'));
}

async function clearRuns() {
  if (!window.confirm('确定清空全部运行历史吗？日志文件会保留在 userdata/logs 中。')) return;
  await window.electronAPI.clearRuns();
  refreshRuns();
  showToast('运行历史已清空');
}

