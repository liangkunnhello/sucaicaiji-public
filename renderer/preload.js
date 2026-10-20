const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // ---------- 下载 ----------
  runDownload: (platform, params, exportDir, options) => ipcRenderer.invoke('run-download', { platform, params, exportDir, options }),
  stopDownload: () => ipcRenderer.invoke('stop-download'),
  openFolder: (folderPath) => ipcRenderer.invoke('open-folder', folderPath),

  onDownloadLog: (callback) => ipcRenderer.on('download-log', (event, data) => callback(data)),
  onDownloadProgress: (callback) => ipcRenderer.on('download-progress', (event, data) => callback(data)),
  onClassifyLog: (callback) => ipcRenderer.on('classify-log', (event, data) => callback(data)),

  // ---------- 分类 ----------
  runDirection: (options) => ipcRenderer.invoke('run-direction', options),
  runPureApi: (options) => ipcRenderer.invoke('run-pure-api', options),
  stopClassify: () => ipcRenderer.invoke('stop-classify'),

  // ---------- 配置 / 环境 ----------
  getRuntimeStatus: () => ipcRenderer.invoke('get-runtime-status'),
  getConfig: () => ipcRenderer.invoke('get-config'),
  getApiClassifyPrompt: () => ipcRenderer.invoke('get-api-classify-prompt'),
  saveConfig: (config) => ipcRenderer.invoke('save-config', config),

  // ---------- 文件 ----------
  selectFolder: (defaultPath) => ipcRenderer.invoke('select-folder', defaultPath),
  openPath: (p) => ipcRenderer.invoke('open-path', p),

  // ---------- 自动化计划 ----------
  listPlans: () => ipcRenderer.invoke('plans-list'),
  savePlan: (plan) => ipcRenderer.invoke('plans-save', plan),
  deletePlan: (planId) => ipcRenderer.invoke('plans-delete', planId),
  togglePlan: (planId, enabled) => ipcRenderer.invoke('plans-toggle', { planId, enabled }),
  runPlanNow: (planId) => ipcRenderer.invoke('plans-run-now', planId),
  plansFollowGlobalRoot: (planIds) => ipcRenderer.invoke('plans-follow-global-root', planIds),
  onPlansChanged: (callback) => ipcRenderer.on('plans-changed', (event, data) => callback(data)),

  // ---------- 运行历史 ----------
  listRuns: () => ipcRenderer.invoke('runs-list'),
  runDetail: (runId) => ipcRenderer.invoke('run-detail', runId),
  rerunRun: (runId) => ipcRenderer.invoke('runs-rerun', runId),
  clearRuns: () => ipcRenderer.invoke('runs-clear'),
  onRunStarted: (callback) => ipcRenderer.on('run-started', (event, data) => callback(data)),
  onRunFinished: (callback) => ipcRenderer.on('run-finished', (event, data) => callback(data)),

  // ---------- 队列 / 调度器状态 ----------
  getQueueState: () => ipcRenderer.invoke('queue-state-get'),
  getSchedulerStatus: () => ipcRenderer.invoke('scheduler-status'),
  onQueueState: (callback) => ipcRenderer.on('queue-state', (event, data) => callback(data)),
  onSchedulerTick: (callback) => ipcRenderer.on('scheduler-tick', (event, data) => callback(data))
});
