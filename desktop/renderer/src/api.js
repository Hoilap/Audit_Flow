export const apiBase = 'http://127.0.0.1:8000'

export async function request(path, options = {}) {
  const res = await fetch(`${apiBase}${path}`, options)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  return data
}

export const api = {
  listFiles: (root = 'outputs') => request(`/files/list?root=${encodeURIComponent(root)}`),
  readFile: (path) => request(`/files/read?path=${encodeURIComponent(path)}`),
  uploadFile: (form) => request('/files/upload', { method: 'POST', body: form }),
  deleteFile: (path) => request(`/files/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),
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
  /** Step 1: 扫描文件 + LLM 识别 */
  workflowDetect: (customerName, taskName, useLlm = true) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    form.append('use_llm', useLlm ? 'true' : 'false')
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
  /** Step 3: 清洗数据 */
  workflowClean: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/clean', { method: 'POST', body: form })
  },
  /** Step 4: 核查数据完备性 */
  workflowCheck: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/check', { method: 'POST', body: form })
  },
  /** Step 5: 执行匹配 */
  workflowMatch: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/match', { method: 'POST', body: form })
  },
  /** Step 6: 填入底稿（脚本方式） */
  workflowFill: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/fill', { method: 'POST', body: form })
  },
  /** Step 6: 填入底稿（LLM 方式，自适应模板布局） */
  workflowFillLlm: (customerName, taskName) => {
    const form = new FormData()
    form.append('customer_name', customerName)
    form.append('task_name', taskName)
    return request('/workflow/bank_ledger_match/fill_llm', { method: 'POST', body: form })
  },

  // ---------- Project CRUD ----------
  listProjects: () => request('/projects/list'),
  listProjectDirs: () => request('/projects/dirs'),
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
}
