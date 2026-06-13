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
  workflow: (endpoint) => request(endpoint, { method: 'POST' }),
  writeFile: (payload) => request('/files/write', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  generateAndRun: (form) => request('/llm/generate_and_run', { method: 'POST', body: form }),

  // ---------- Project CRUD ----------
  listProjects: () => request('/projects/list'),
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
