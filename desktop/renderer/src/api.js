export const apiBase = 'http://127.0.0.1:8001'

let currentController = null

export function cancelRequest() {
  if (currentController) {
    currentController.abort()
    currentController = null
  }
}

export async function request(path, options = {}) {
  // If caller didn't provide a signal, create an AbortController
  if (!options.signal) {
    currentController = new AbortController()
    options.signal = currentController.signal
  }
  try {
    const res = await fetch(`${apiBase}${path}`, options)
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
    return data
  } finally {
    currentController = null
  }
}

export const api = {
  listFiles: (root = 'outputs') => request(`/files/list?root=${encodeURIComponent(root)}`),
  readFile: (path) => request(`/files/read?path=${encodeURIComponent(path)}`),
  fileMtime: (path) => request(`/files/mtime?path=${encodeURIComponent(path)}`),
  uploadFile: (form) => request('/files/upload', { method: 'POST', body: form }),
  copyFromPath: (sourcePath, dest) => {
    const form = new FormData()
    form.append('source', sourcePath)
    form.append('dest', dest)
    return request('/files/copy-from-path', { method: 'POST', body: form })
  },
  deleteFile: (path) => request(`/files/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
  /** 校验 inputs/ 目录结构是否符合「客户/任务英文目录名」规范并与数据库一致 */
  validateInputs: () => request('/files/validate-inputs'),
  /** 在系统文件资源管理器中打开数据根目录内的路径（如 inputs/ 或 outputs/） */
  openInExplorer: (path) => {
    const form = new FormData()
    form.append('path', path)
    return request('/files/open-in-explorer', { method: 'POST', body: form })
  },
  gitLog: () => request('/git/log'),
  gitCommit: (message) => request(`/git/commit?message=${encodeURIComponent(message)}`, { method: 'POST' }),
  workflow: (endpoint, body = null) => {
    if (body instanceof FormData) {
      return request(endpoint, { method: 'POST', body })
    }
    return request(endpoint, { method: 'POST' })
  },
  writeFile: (payload) => request('/files/write', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  generateAndRun: (form) => request('/llm/generate_and_run', { method: 'POST', body: form }),

  // ---------- 新工作流 API ----------
  /** Step 1: 扫描文件 + 识别（LLM 或脚本） */
  workflowDetect: (customerName, taskName, parser = 'llm', requirement = '') => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    form.append('parser', parser)
    if (requirement) form.append('requirement', requirement)
    return request('/workflow/detect', { method: 'POST', body: form })
  },
  /** 获取 task.yml 配置内容 */
  workflowGetConfig: (customerName, taskName) =>
    request(`/workflow/config?customer_name=${encodeURIComponent(customerName)}&task_name=${encodeURIComponent(taskName)}`),
  /** Step 2: 保存 task.yml */
  workflowSaveConfig: (customerName, taskName, content) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    form.append('content', content)
    return request('/workflow/config/save', { method: 'POST', body: form })
  },
  /** Step 3a: 清洗银行流水 */
  workflowCleanBank: (customerName, taskName, parser, requirement = '') => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    if (parser) form.append('parser', parser)
    if (requirement) form.append('requirement', requirement)
    return request('/workflow/bank_ledger_match/clean_bank', { method: 'POST', body: form })
  },
  /** Step 3b: 清洗序时账 */
  workflowCleanLedger: (customerName, taskName, parser, requirement = '') => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    if (parser) form.append('parser', parser)
    if (requirement) form.append('requirement', requirement)
    return request('/workflow/bank_ledger_match/clean_ledger', { method: 'POST', body: form })
  },
  /** Step 4: 核查数据完备性 */
  workflowCheck: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/check', { method: 'POST', body: form })
  },
  /** Step 5: 执行匹配 */
  workflowMatch: (customerName, taskName, parser, requirement = '') => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    if (parser) form.append('parser', parser)
    if (requirement) form.append('requirement', requirement)
    return request('/workflow/bank_ledger_match/match', { method: 'POST', body: form })
  },
  /** Step 8: 填入底稿（parser: 'llm' | 'script'） */
  workflowFill: (customerName, taskName, parser = '', requirement = '') => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    if (parser) form.append('parser', parser)
    if (requirement) form.append('requirement', requirement)
    return request('/workflow/bank_ledger_match/fill', { method: 'POST', body: form })
  },

  // ---------- Project CRUD ----------
  dashboardStats: () => request('/dashboard/stats'),
  listProjects: () => request('/projects/list'),
  listProcedures: () => request('/projects/procedures'),
  listProjectDirs: () => request('/projects/dirs'),
  listTaskDefinitions: () => request('/task-definitions'),
  createProject: (payload) => request('/projects/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  updateProject: (id, payload) => request(`/projects/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  deleteProject: (id) => request(`/projects/${id}`, { method: 'DELETE' }),

  // ---------- LLM 配置 ----------
  getLlmConfig: () => request('/llm/config'),
  updateLlmConfig: (defaultProvider, enabled) => {
    const form = new FormData()
    if (defaultProvider) form.append('default_provider', defaultProvider)
    if (enabled !== undefined) form.append('enabled', enabled)
    return request('/llm/config', { method: 'POST', body: form })
  },
  getLlmTokens: () => request('/llm/tokens'),
  getLlmProviderKey: (name) => request(`/llm/config/provider/${encodeURIComponent(name)}/key`),
  updateLlmProviders: (providers) => request('/llm/config/providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ providers }),
  }),
  createLlmProvider: (data) => request('/llm/config/providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }),
  deleteLlmProvider: (name) => request(`/llm/config/providers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  }),

  // ---------- Workflow README ----------
  /** 获取所有任务子目录下的 readme.md 内容 */
  getReadmes: () => request('/workflow/readmes'),

  // ---------- 日志 ----------
  /** 获取后端日志（最近 lines 行） */
  getLogs: (lines = 100) => request(`/logs?lines=${lines}`),

  // ---------- Agent 对话 ----------
  agentChat: (message, conversationId = '', customerName = '', taskName = '') => request('/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      conversation_id: conversationId,
      customer_name: customerName,
      task_name: taskName,
    }),
  }),
  agentListConversations: () => request('/agent/conversations'),
  agentGetConversation: (id) => request(`/agent/conversations/${id}`),
  agentNewConversation: () => request('/agent/conversations/new', { method: 'POST' }),
  agentDeleteConversation: (id) => request(`/agent/conversations/${id}`, { method: 'DELETE' }),
}
