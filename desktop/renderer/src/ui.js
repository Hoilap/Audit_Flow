import { llmCodePath, navItems, workflowTasks, evidencePanelSections } from './config.js'
import { activeTask, state, stepStatus } from './state.js'
import { $, $$, escapeHtml, formatSeconds, isCsv } from './dom.js'
import { parseCsv } from './csv.js'

export function renderShell() {
  $('#nav').innerHTML = navItems.map(([id, icon, label]) => `
    <button class="${state.activePage === id ? 'active' : ''}" data-page="${id}"><span class="nav-icon">${icon}</span>${label}</button>
  `).join('')
  $('#main').innerHTML = [
    dashboardPage(),
    projectsPage(),
    dataPage(),
    agentPage(),
    programsPage(),
    workpapersPage(),
    reportsPage(),
    settingsPage(),
  ].join('')
  renderEvidencePanel()
  renderWorkflowWorkspace()
  renderTimeline()
  renderStepFiles()
}

/**
 * 按 config.evidencePanelSections 顺序动态渲染右栏区块。
 * 调整 config 中数组顺序即可自定义右栏布局。
 */
export function renderEvidencePanel() {
  const body = $('#evidence-body')
  if (!body) return

  body.innerHTML = evidencePanelSections.map(([id, title, hasRefresh]) => {
    let inner = ''
    if (id === 'timeline') {
      inner = `<div class="timeline" id="timeline"></div>`
    } else if (id === 'step-files') {
      inner = `<div id="step-file-list" class="grid file-scroll-list"></div>`
    } else if (id === 'all-files') {
      inner = `<div id="file-list" class="grid file-scroll-list"></div>`
    } else if (id === 'git-log') {
      inner = `<div id="git-log" class="timeline"></div>
        ${hasRefresh ? `<button id="refresh-log">刷新历史</button>` : ''}`
    } else if (id === 'review-editor') {
      inner = `<button id="open-review-editor" class="primary" style="width:100%;">打开人工复核编辑器</button>`
    }
    return `<section class="panel-section">
      <h3>${escapeHtml(title)}</h3>
      ${inner}
    </section>`
  }).join('')

  // 刷新按钮事件已在 main.js 中通过 id 绑定，此处仅渲染结构
}

export function showPage(page) {
  state.activePage = page
  $$('.page').forEach((el) => el.classList.toggle('active', el.id === `page-${page}`))
  $$('#nav button').forEach((button) => button.classList.toggle('active', button.dataset.page === page))
}

export function setAgentStatus(status, progress = 0) {
  state.status = status
  const normalized = status.toLowerCase()
  $('#agent-status').textContent = `Agent ${status}`
  $('#agent-dot').className = `status-dot ${normalized}`
  $('#agent-progress').style.width = `${progress}%`
  const running = ['Running', 'Validating', 'Retrying'].includes(status) ? '1' : '0'
  const runningEl = $('#running-agents')
  if (runningEl) runningEl.textContent = running
}

export function startTimer() {
  state.startedAt = Date.now()
  clearInterval(state.timer)
  state.timer = setInterval(() => {
    $('#elapsed').textContent = formatSeconds(Math.floor((Date.now() - state.startedAt) / 1000))
  }, 500)
}

export function stopTimer() {
  clearInterval(state.timer)
  state.timer = null
}

export function renderTimeline(activeStepId = null, failed = false) {
  const task = activeTask()
  $('#timeline').innerHTML = task.steps.map((step, index) => {
    const savedStatus = stepStatus(task.id, step.id)
    const stateClass = failed && step.id === activeStepId
      ? 'failed'
      : step.id === activeStepId
        ? 'active'
        : savedStatus === 'completed'
          ? 'done'
          : savedStatus === 'failed'
            ? 'failed'
            : ''
    const mark = stateClass === 'done' ? '✓' : stateClass === 'failed' ? '!' : stateClass === 'active' ? '•' : index + 1
    return `<div class="timeline-item ${stateClass}"><span class="timeline-mark">${mark}</span><span>${escapeHtml(step.label)} · ${escapeHtml(step.title)}</span></div>`
  }).join('')
}

export function renderWorkflowWorkspace() {
  const task = activeTask()
  const promptEl = $('#prompt')
  if (promptEl && !promptEl.value.trim()) promptEl.value = task.prompt
  const taskList = $('#workflow-task-list')
  if (taskList) {
    taskList.innerHTML = workflowTasks.map((item) => `
      <button class="task-card ${item.id === state.activeTaskId ? 'active' : ''}" data-task-id="${item.id}">
        <span class="task-name">${escapeHtml(item.name)}</span>
        <span class="task-meta">${escapeHtml(item.description)}</span>
        <span><span class="badge ${riskClass(item.risk)}">${escapeHtml(item.risk)}</span></span>
      </button>
    `).join('')
  }
  const steps = $('#workflow-step-list')
  if (steps) {
    steps.innerHTML = task.steps.map((step, index) => {
      const status = stepStatus(task.id, step.id)
      const active = index === state.activeStepIndex ? 'active' : ''
      const statusClass = status === 'completed' ? 'done' : status === 'failed' ? 'failed' : ''
      return `
        <div class="step-card ${active} ${statusClass}" data-step-index="${index}">
          <div class="step-title"><strong>${escapeHtml(step.label)}</strong><span class="badge">${statusLabel(status)}</span></div>
          <div class="subtle">${escapeHtml(step.title)}</div>
          <div class="step-files">${step.outputs.slice(0, 2).map((file) => `<span>${escapeHtml(shortName(file))}</span>`).join('')}</div>
        </div>
      `
    }).join('')
  }
  const title = $('#active-task-title')
  if (title) title.textContent = task.name
  const desc = $('#active-task-desc')
  if (desc) desc.textContent = task.description
  renderTimeline()
  renderStepFiles()
}

export function renderFiles(files) {
  state.files = files.map(normalizePath)
  const count = $('#file-count')
  if (count) count.textContent = files.length
  const list = $('#file-list')
  if (!list) return
  if (!files.length) {
    list.innerHTML = '<div class="subtle">暂无输出文件</div>'
    return
  }
  list.innerHTML = state.files.slice(0, 80).map((file) => fileRow(file, '所有输出')).join('')
}

export function renderFileError(message) {
  $('#file-list').innerHTML = `<div class="subtle">本地后端启动中或不可用：${escapeHtml(message)}</div>`
}

export function renderStepFiles() {
  const task = activeTask()
  const seen = new Set()
  const files = task.steps.flatMap((step) => step.outputs.map((file) => ({ file: normalizePath(file), step })))
    .filter(({ file, step }) => {
      const key = `${step.id}:${file}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  const list = $('#step-file-list')
  if (!list) return
  list.innerHTML = files.map(({ file, step }) => fileRow(file, step.label)).join('')
}

export function renderCsvPreview(file, content) {
  const rows = parseCsv(content).slice(0, 101)
  const target = $('#csv-preview')
  if (!rows.length) {
    target.innerHTML = `<div class="subtle">${escapeHtml(file)} 为空或无法解析。</div>`
    return
  }
  const headers = rows[0]
  const body = rows.slice(1)
  target.innerHTML = `
    <div class="label" style="margin-bottom:8px;">${escapeHtml(file)} · 预览 ${body.length} 行</div>
    <table class="table">
      <thead><tr>${headers.map((header) => `<th>${escapeHtml(header)}</th>`).join('')}</tr></thead>
      <tbody>${body.map((row) => `<tr>${headers.map((_, index) => `<td>${escapeHtml(row[index] || '')}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>
  `
}

export function renderTextPreview(file, content) {
  $('#csv-preview').innerHTML = `<div class="label" style="margin-bottom:8px;">${escapeHtml(file)}</div><pre>${escapeHtml(content.slice(0, 5000))}</pre>`
}

export function addMessage({ role = 'Agent', title = '', body = '', result = null, failed = false }) {
  const chat = $('#chat')
  if (!chat) return
  const block = document.createElement('div')
  block.className = 'message'
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  block.innerHTML = `
    <div class="message-head"><span>${escapeHtml(role)}${title ? ` · ${escapeHtml(title)}` : ''}</span><span>${failed ? 'Failed' : time}</span></div>
    ${body ? `<p>${escapeHtml(body)}</p>` : ''}
    ${result ? `<pre class="log-block">${escapeHtml(JSON.stringify(result, null, 2))}</pre>` : ''}
  `
  chat.appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })
}

export function renderLlmCode(path, content, result = null) {
  const panel = $('#llm-code-panel')
  if (!panel) return
  panel.innerHTML = `
    <div class="llm-code-head">
      <strong>LLM 生成代码区</strong>
      <span class="llm-code-path">${escapeHtml(path || llmCodePath)}</span>
    </div>
    <pre class="code-block">${escapeHtml(content || '等待生成代码。')}</pre>
    ${result ? `<pre class="log-block">${escapeHtml(JSON.stringify(result, null, 2))}</pre>` : ''}
  `
}

function fileRow(file, source) {
  const preview = isCsv(file) ? 'CSV 预览' : '文本预览'
  return `
    <div class="file-row" data-file="${escapeHtml(file)}">
      <div class="file-name">${escapeHtml(file)}</div>
      <div class="file-meta"><span>${escapeHtml(source)}</span><span>${preview}</span></div>
    </div>
  `
}

function riskClass(risk) {
  return risk.toLowerCase() === 'critical' ? 'critical' : risk.toLowerCase() === 'high' ? 'high' : risk.toLowerCase() === 'medium' ? 'medium' : 'low'
}

function statusLabel(status) {
  if (status === 'completed') return 'Done'
  if (status === 'failed') return 'Failed'
  if (status === 'running') return 'Running'
  return 'Ready'
}

function shortName(file) {
  return file.split('/').slice(-2).join('/')
}

function normalizePath(file) {
  return String(file).replaceAll('\\', '/')
}

function dashboardPage() {
  return `<section class="page active" id="page-dashboard">
    <div class="page-header"><div><h1>Dashboard</h1><p class="subtle">今日审计任务、风险预警与 Agent 执行概览</p></div><span class="badge low">系统正常</span></div>
    <div class="grid metrics">
      <div class="card"><div class="card-title">今日任务数</div><div class="card-value">12</div><p class="subtle">3 个已完成</p></div>
      <div class="card"><div class="card-title">待审核项目</div><div class="card-value">8</div><p class="subtle">含 2 个高风险</p></div>
      <div class="card"><div class="card-title">运行中的 Agent</div><div class="card-value" id="running-agents">0</div><p class="subtle">本地 Python 后端</p></div>
      <div class="card"><div class="card-title">风险预警数量</div><div class="card-value">16</div><p class="subtle">Critical 2 / High 5</p></div>
    </div>
    <div class="grid two-col" style="margin-top:12px;"><div class="card"><h2>最近审计项目</h2><table class="table" style="margin-top:8px;"><thead><tr><th>项目名称</th><th>客户</th><th>状态</th><th>风险</th></tr></thead><tbody><tr><td>资金流水专项核查</td><td>桂平金山</td><td><span class="badge">Reviewing</span></td><td><span class="badge high">High</span></td></tr><tr><td>出库表核对</td><td>A 公司</td><td><span class="badge">Planning</span></td><td><span class="badge medium">Medium</span></td></tr></tbody></table></div><div class="card"><h2>风险分布</h2><div class="grid" style="margin-top:12px;"><div><span class="badge low">Low</span> <span class="subtle">24 项</span></div><div><span class="badge medium">Medium</span> <span class="subtle">11 项</span></div><div><span class="badge high">High</span> <span class="subtle">5 项</span></div><div><span class="badge critical">Critical</span> <span class="subtle">2 项</span></div></div></div></div>
  </section>`
}

function projectsPage() {
  return `<section class="page" id="page-projects"><div class="page-header"><div><h1>项目管理</h1><p class="subtle">按状态、负责人和风险等级管理审计项目</p></div><button class="primary">新建项目</button></div><div class="card"><div class="toolbar" style="margin-bottom:12px;"><input class="search" placeholder="搜索项目、客户或负责人"><select><option>全部状态</option><option>Running</option><option>Reviewing</option></select><select><option>全部风险</option><option>Low</option><option>Medium</option><option>High</option></select></div><table class="table"><thead><tr><th>项目名称</th><th>客户名称</th><th>项目状态</th><th>创建时间</th><th>负责人</th><th>风险等级</th></tr></thead><tbody><tr><td>资金流水专项核查</td><td>桂平金山</td><td><span class="badge">Reviewing</span></td><td>2026-06-08</td><td>审计一组</td><td><span class="badge high">High</span></td></tr><tr><td>出库表核对</td><td>A 公司</td><td><span class="badge">Planning</span></td><td>2026-06-13</td><td>CPA</td><td><span class="badge medium">Medium</span></td></tr></tbody></table></div></section>`
}

function dataPage() {
  return `<section class="page" id="page-data"><div class="page-header"><div><h1>数据源</h1><p class="subtle">上传 Excel、CSV、TXT、PDF 或 ZIP，并查看字段画像</p></div></div><div class="grid two-col"><div class="drop-zone"><h2>导入数据</h2><p class="subtle">文件将保存到本地工作区，Agent 会基于数据结构生成审计程序。</p><div class="upload-row"><input type="file" id="upload-file"><input type="text" id="upload-dest" value="inputs/bank_ledger_match/"><button id="upload-btn" class="primary">上传</button></div></div><div class="card"><h2>自动数据画像</h2><div class="grid" style="margin-top:12px;"><div><span class="label">已识别金额字段</span><br><strong>借方发生额、贷方发生额、银行流水金额</strong></div><div><span class="label">已识别日期字段</span><br><strong>交易日期、记账日期</strong></div><div><span class="label">当前输出文件</span><br><strong id="file-count">0</strong> 个</div></div></div></div></section>`
}

function agentPage() {
  return `<section class="page" id="page-agent"><div class="page-header"><div><h1>Agent 工作流</h1><p id="active-task-desc" class="subtle"></p></div><div class="toolbar"><button id="run-next-step">执行下一步</button><button id="run-all-steps" class="primary">执行全部</button></div></div><div class="agent-grid"><div class="card"><h2>任务选择</h2><div id="workflow-task-list" class="task-list" style="margin-top:12px;"></div></div><div><div class="card"><div class="page-header" style="margin-bottom:12px;"><div><h2 id="active-task-title"></h2><p class="subtle">每个任务有独立执行时间线，可单步执行，也可全部执行。</p></div></div><div id="workflow-step-list" class="step-list"></div></div><div id="llm-code-panel" class="card llm-code-panel"><div class="llm-code-head"><strong>LLM 生成代码区</strong><span class="llm-code-path">${llmCodePath}</span></div><pre class="code-block">等待生成代码。</pre></div><div class="conversation" id="chat"><div class="message"><div class="message-head"><span>Agent</span><span>Ready</span></div><p>请选择任务和步骤。对于没有固定后端接口的任务，可以先用自然语言生成代码，代码会写入独立目录并可选择编译后执行，再继续下一步。</p></div></div></div></div></section>`
}

function programsPage() {
  return `<section class="page" id="page-programs"><div class="page-header"><div><h1>审计程序</h1><p class="subtle">可解释、可复核的程序模板</p></div></div><div class="grid two-col"><div class="card"><h2>序时账银行流水匹配</h2><p class="subtle" style="margin-top:8px;">Clean → Match → Verify → Fill。</p></div><div class="card"><h2>出库表核对</h2><p class="subtle" style="margin-top:8px;">字段识别 → 三表核对 → 差异校验 → 结论生成。</p></div></div></section>`
}

function workpapersPage() {
  return `<section class="page" id="page-workpapers"><div class="page-header"><div><h1>工作底稿</h1><p class="subtle">自动生成审计目标、程序、结果、结论与附件清单</p></div></div><div class="card"><div class="tabs"><button class="active">审计目标</button><button>审计程序</button><button>测试结果</button><button>审计结论</button></div><h2>资金流水专项核查工作底稿</h2><p class="subtle" style="margin-top:10px;">本底稿基于银行流水、总账明细和 Agent 执行证据链生成。</p><pre id="workpaper-preview" style="margin-top:12px;">等待选择右侧生成文件预览。</pre></div></section>`
}

function reportsPage() {
  return `<section class="page" id="page-reports"><div class="page-header"><div><h1>分析报告</h1><p class="subtle">审计报告、管理建议书、内控评价和风险分析</p></div></div><div class="report-list"><div class="card"><h2>风险分析报告</h2><p class="subtle" style="margin-top:8px;">汇总异常交易、控制缺陷和高风险事项。</p></div><div class="card"><h2>管理建议书</h2><p class="subtle" style="margin-top:8px;">将审计发现转化为管理层可执行建议。</p></div></div></section>`
}

function settingsPage() {
  return `<section class="page" id="page-settings"><div class="page-header"><div><h1>设置</h1><p class="subtle">模型、本地后端、导出和界面偏好</p></div></div><div class="card grid"><label>默认导出目录 <input class="search" value="outputs/bank_ledger_match/"></label><label>默认超时秒数 <input id="run-timeout" type="number" value="5" style="height:32px;width:80px;padding:0 8px;"></label></div></section>`
}
