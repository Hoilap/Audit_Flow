const apiBase = 'http://127.0.0.1:8001'

const state = {
  files: [],
  status: 'Idle',
  startedAt: null,
  timer: null,
  timeline: [
    { key: 'understand', label: '理解任务', state: 'idle' },
    { key: 'profile', label: '分析数据结构', state: 'idle' },
    { key: 'plan', label: '制定审计程序', state: 'idle' },
    { key: 'code', label: '生成 Python 代码', state: 'idle' },
    { key: 'run', label: '执行代码', state: 'idle' },
    { key: 'validate', label: '校验结果', state: 'idle' },
    { key: 'export', label: '输出工作底稿', state: 'idle' },
  ],
}

const $ = (selector) => document.querySelector(selector)
const $$ = (selector) => Array.from(document.querySelectorAll(selector))

function formatSeconds(total) {
  const minutes = String(Math.floor(total / 60)).padStart(2, '0')
  const seconds = String(total % 60).padStart(2, '0')
  return `${minutes}:${seconds}`
}

function setAgentStatus(status, progress = 0) {
  state.status = status
  const normalized = status.toLowerCase()
  $('#agent-status').textContent = `Agent ${status}`
  $('#agent-dot').className = `status-dot ${normalized}`
  $('#agent-progress').style.width = `${progress}%`
  $('#running-agents').textContent = ['Running', 'Validating', 'Retrying'].includes(status) ? '1' : '0'
}

function startTimer() {
  state.startedAt = Date.now()
  clearInterval(state.timer)
  state.timer = setInterval(() => {
    $('#elapsed').textContent = formatSeconds(Math.floor((Date.now() - state.startedAt) / 1000))
  }, 500)
}

function stopTimer() {
  clearInterval(state.timer)
  state.timer = null
}

function setTimeline(activeKey, failed = false) {
  let beforeActive = true
  state.timeline = state.timeline.map((item) => {
    if (item.key === activeKey) {
      beforeActive = false
      return { ...item, state: failed ? 'failed' : 'active' }
    }
    if (beforeActive) return { ...item, state: 'done' }
    return { ...item, state: failed ? item.state : 'idle' }
  })
  renderTimeline()
}

function completeTimeline() {
  state.timeline = state.timeline.map((item) => ({ ...item, state: 'done' }))
  renderTimeline()
}

function resetTimeline() {
  state.timeline = state.timeline.map((item) => ({ ...item, state: 'idle' }))
  renderTimeline()
}

function renderTimeline() {
  $('#timeline').innerHTML = state.timeline.map((item) => {
    const mark = item.state === 'done' ? '✓' : item.state === 'failed' ? '!' : item.state === 'active' ? '•' : ''
    return `<div class="timeline-item ${item.state}"><span class="timeline-mark">${mark}</span><span>${item.label}</span></div>`
  }).join('')
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function addMessage({ role = 'Agent', title = '', body = '', code = '', result = null, failed = false }) {
  const block = document.createElement('div')
  block.className = 'message'
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  block.innerHTML = `
    <div class="message-head"><span>${role}${title ? ` · ${title}` : ''}</span><span>${failed ? 'Failed' : time}</span></div>
    ${body ? `<p>${escapeHtml(body)}</p>` : ''}
    ${code ? `<pre class="code-block">${escapeHtml(code)}</pre>` : ''}
    ${result ? `<pre class="log-block">${escapeHtml(JSON.stringify(result, null, 2))}</pre>` : ''}
  `
  $('#chat').appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })
}

async function request(path, options = {}) {
  const res = await fetch(`${apiBase}${path}`, options)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
  return data
}

async function refreshFiles() {
  const list = $('#file-list')
  try {
    const data = await request('/files/list?root=outputs')
    state.files = data.files || []
    $('#file-count').textContent = state.files.length
    if (!state.files.length) {
      list.innerHTML = '<div class="subtle">暂无输出文件</div>'
      return
    }
    list.innerHTML = state.files.slice(0, 60).map((file) => `
      <div class="file-row" data-file="${escapeHtml(file)}">
        <div class="file-name">${escapeHtml(file)}</div>
        <div class="file-meta"><span>Agent 输出</span><span>预览</span></div>
      </div>
    `).join('')
    $$('.file-row').forEach((row) => row.addEventListener('click', () => previewFile(row.dataset.file)))
  } catch (error) {
    list.innerHTML = `<div class="subtle">本地后端启动中或不可用：${escapeHtml(error.message)}</div>`
  }
}

async function previewFile(file) {
  try {
    const data = await request(`/files/read?path=${encodeURIComponent(file)}`)
    const content = (data.content || '').slice(0, 4000)
    $('#prompt').value = content.slice(0, 1200)
    $('#workpaper-preview').textContent = content || '文件为空'
    addMessage({ role: 'Evidence', title: '文件预览', body: `已载入 ${file}，可在工作底稿区查看内容。` })
    showPage('workpapers')
  } catch (error) {
    addMessage({ role: 'Evidence', title: '读取失败', body: error.message, failed: true })
  }
}

async function refreshLog() {
  try {
    const data = await request('/git/log')
    const commits = data.commits || []
    $('#git-log').innerHTML = commits.slice(0, 8).map((commit) => `
      <div class="timeline-item done"><span class="timeline-mark">✓</span><span>${escapeHtml(commit.hexsha.slice(0, 7))} ${escapeHtml(commit.message.trim())}</span></div>
    `).join('') || '<div class="subtle">暂无 Git 历史</div>'
  } catch (error) {
    $('#git-log').innerHTML = `<div class="subtle">${escapeHtml(error.message)}</div>`
  }
}

async function runWorkflow(kind) {
  const labels = { clean: '数据清洗', match: '银行流水匹配', full: '完整审计工作流' }
  try {
    showPage('agent')
    resetTimeline()
    startTimer()
    setAgentStatus('Running', 18)
    setTimeline('profile')
    addMessage({ role: 'Agent', title: labels[kind], body: `开始执行${labels[kind]}，执行过程会进入右侧证据链。` })
    const data = await request(`/workflow/${kind}`, { method: 'POST' })
    setAgentStatus('Validating', 72)
    setTimeline('validate')
    addMessage({ role: 'Python Backend', title: '执行结果', result: data })
    completeTimeline()
    setAgentStatus('Completed', 100)
    stopTimer()
    $('#tokens').textContent = `${Math.floor(8000 + Math.random() * 4200)} tokens`
    await refreshFiles()
  } catch (error) {
    setTimeline('run', true)
    setAgentStatus('Failed', 100)
    stopTimer()
    addMessage({ role: 'Python Backend', title: '执行失败', body: error.message, failed: true })
  }
}

async function sendPrompt() {
  const prompt = $('#prompt').value.trim()
  if (!prompt) return
  const runAfter = $('#run-after-generate').checked
  const timeout = parseInt($('#composer-timeout').value || $('#run-timeout')?.value || '5', 10)
  const form = new FormData()
  form.append('prompt', prompt)
  form.append('target_path', 'outputs/clean/generated_from_llm.py')
  form.append('run_code', runAfter)
  form.append('timeout', timeout)
  try {
    showPage('agent')
    resetTimeline()
    startTimer()
    addMessage({ role: 'User', body: prompt })
    setAgentStatus('Running', 12)
    setTimeline('understand')
    addMessage({ role: 'Agent', title: '任务理解', body: '我会先理解审计目标，再生成 Python 处理代码，并将结果写入本地输出目录。' })
    setTimeline('code')
    setAgentStatus('Running', 48)
    const data = await request('/llm/generate_and_run', { method: 'POST', body: form })
    addMessage({ role: 'Agent', title: '代码生成', body: `已写入 ${data.path}` })
    setTimeline(runAfter ? 'run' : 'validate')
    setAgentStatus(runAfter ? 'Validating' : 'Completed', runAfter ? 78 : 100)
    if (data.run_result) addMessage({ role: 'Local Python', title: '运行日志', result: data.run_result, failed: data.run_result.returncode > 0 })
    completeTimeline()
    setAgentStatus('Completed', 100)
    stopTimer()
    $('#tokens').textContent = `${Math.max(1200, prompt.length * 8)} tokens`
    await refreshFiles()
  } catch (error) {
    setTimeline('run', true)
    setAgentStatus('Failed', 100)
    stopTimer()
    addMessage({ role: 'Agent', title: '执行失败', body: `校验或执行失败：${error.message}。你可以调整指令后重试，失败记录会保留在对话区。`, failed: true })
  }
}

async function llmToClean() {
  $('#prompt').value = $('#prompt').value || '生成用于清洗银行流水和总账明细的 Python 脚本，并输出结构化 CSV。'
  await sendPrompt()
}

async function uploadFile() {
  const fileEl = $('#upload-file')
  if (!fileEl.files.length) {
    addMessage({ role: 'Data Source', title: '等待文件', body: '请选择需要上传的 Excel、CSV、TXT、PDF 或 ZIP 文件。' })
    return
  }
  const form = new FormData()
  form.append('file', fileEl.files[0])
  form.append('dest', $('#upload-dest').value || 'inputs/bank_ledger_match/')
  try {
    setAgentStatus('Running', 24)
    setTimeline('profile')
    const data = await request('/files/upload', { method: 'POST', body: form })
    addMessage({ role: 'Data Source', title: '上传完成', body: `已导入 ${data.path}。金额字段、日期字段和表结构画像可继续由 Agent 分析。` })
    setAgentStatus('Completed', 100)
    await refreshFiles()
  } catch (error) {
    setAgentStatus('Failed', 100)
    addMessage({ role: 'Data Source', title: '上传失败', body: error.message, failed: true })
  }
}

async function refreshApprove() {
  const el = $('#approve-table')
  el.innerHTML = '<span class="subtle">加载中...</span>'
  try {
    const data = await request('/files/read?path=outputs/matches/matches.csv')
    const lines = (data.content || '').split('\n').filter(Boolean)
    if (!lines.length) {
      el.innerHTML = '<span class="subtle">暂无 matches.csv</span>'
      return
    }
    const header = lines[0].split(',')
    const rows = lines.slice(1, 8).map((line, index) => {
      const cells = line.split(',')
      return `<tr><td><input type="checkbox" data-row="${index + 1}"></td>${header.map((_, cellIndex) => `<td>${escapeHtml(cells[cellIndex] || '')}</td>`).join('')}</tr>`
    }).join('')
    el.innerHTML = `
      <table class="table">
        <thead><tr><th></th>${header.map((item) => `<th>${escapeHtml(item)}</th>`).join('')}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="toolbar" style="margin-top: 10px;"><button id="approve-selected">通过</button><button id="reject-selected">驳回</button></div>
    `
    $('#approve-selected').addEventListener('click', () => writeApproval(lines, 'approved'))
    $('#reject-selected').addEventListener('click', () => writeApproval(lines, 'rejected'))
  } catch (error) {
    el.innerHTML = `<span class="subtle">${escapeHtml(error.message)}</span>`
  }
}

async function writeApproval(lines, type) {
  const checked = Array.from($('#approve-table').querySelectorAll('input[type=checkbox]:checked'))
  if (!checked.length) return
  const selected = checked.map((checkbox) => lines[parseInt(checkbox.dataset.row, 10)])
  const payload = {
    path: `outputs/approvals/${type}.csv`,
    content: [lines[0], ...selected].join('\n'),
    commit_message: `${type} selected matches`,
  }
  await request('/files/write', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  addMessage({ role: 'Review', title: type === 'approved' ? '复核通过' : '复核驳回', body: `已写入 ${payload.path}` })
  await refreshFiles()
}

async function commitAll() {
  const message = window.prompt('Commit message', 'commit from AuditFlow desktop')
  if (!message) return
  try {
    await request(`/git/commit?message=${encodeURIComponent(message)}`, { method: 'POST' })
    addMessage({ role: 'Evidence', title: '证据链已提交', body: message })
    await refreshLog()
  } catch (error) {
    addMessage({ role: 'Evidence', title: '提交失败', body: error.message, failed: true })
  }
}

function showPage(page) {
  $$('.page').forEach((el) => el.classList.toggle('active', el.id === `page-${page}`))
  $$('#nav button').forEach((button) => button.classList.toggle('active', button.dataset.page === page))
}

function bindEvents() {
  $$('#nav button').forEach((button) => button.addEventListener('click', () => showPage(button.dataset.page)))
  $$('.task-pill').forEach((button) => button.addEventListener('click', () => {
    $('#prompt').value = button.dataset.prompt
    showPage('agent')
  }))
  $('#send').addEventListener('click', sendPrompt)
  $('#llm-to-clean').addEventListener('click', llmToClean)
  $('#run-clean').addEventListener('click', () => runWorkflow('clean'))
  $('#run-match').addEventListener('click', () => runWorkflow('match'))
  $('#run-full').addEventListener('click', () => runWorkflow('full'))
  $('#refresh-files').addEventListener('click', refreshFiles)
  $('#refresh-log').addEventListener('click', refreshLog)
  $('#refresh-approve').addEventListener('click', refreshApprove)
  $('#refresh-all').addEventListener('click', refreshAll)
  $('#upload-btn').addEventListener('click', uploadFile)
  $('#commit-all').addEventListener('click', commitAll)
  $('#model-select').addEventListener('change', (event) => { $('#status-model').textContent = event.target.value })
  $('#theme-toggle').addEventListener('click', () => {
    document.body.classList.toggle('dark')
    $('#theme-toggle').textContent = document.body.classList.contains('dark') ? '浅色' : '深色'
    localStorage.setItem('auditflow-theme', document.body.classList.contains('dark') ? 'dark' : 'light')
  })
  $('#prompt').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      sendPrompt()
    }
  })
}

async function refreshAll() {
  await Promise.allSettled([refreshFiles(), refreshLog(), refreshApprove()])
}

function initializeTheme() {
  const saved = localStorage.getItem('auditflow-theme')
  if (saved === 'dark') {
    document.body.classList.add('dark')
    $('#theme-toggle').textContent = '浅色'
  }
}

function init() {
  initializeTheme()
  bindEvents()
  resetTimeline()
  setAgentStatus('Idle', 0)
  refreshAll()
}

init()
