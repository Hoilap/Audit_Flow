import { navItems, workflowTasks, evidencePanelSections } from './config.js'
import { activeStep, activeTask, getProjectBasePath, resolveProjectPath, state, stepStatus, findWorkflowTaskByName, taskRunKey } from './state.js'
import { $, $$, escapeHtml, formatSeconds, isCsv } from './dom.js'
import { parseCsv } from './csv.js'
import { api } from './api.js'

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
    agentLoopPage(),
    workpapersPage(),
    reportsPage(),
    settingsPage(),
  ].join('')
  // 右侧证据面板仅在 Agent 页面显示
  updateEvidencePanelVisibility()
  renderEvidencePanel()
  renderWorkflowWorkspace()
}

/**
 * 按 config.evidencePanelSections 顺序动态渲染右栏区块。
 * 调整 config 中数组顺序即可自定义右栏布局。
 */
export function renderEvidencePanel() {
  const body = $('#evidence-body')
  if (!body) return

  body.innerHTML = evidencePanelSections
    .filter(([, , visible]) => visible !== false)
    .map(([id, title, , hasRefresh, collapsible]) => {
    const collapsed = collapsible && state.collapsedSections[id]
    const collapseClass = collapsed ? ' collapsed' : ''
    let headerActions = ''
    if (hasRefresh && id === 'git-log') {
      headerActions += `<button id="refresh-log" class="section-toggle" title="刷新历史">↻</button>`
    }
    if (collapsible) {
      headerActions += `<button class="section-toggle" data-toggle-section="${id}" title="${collapsed ? '展开' : '折叠'}">${collapsed ? '▶' : '▼'}</button>`
    }
    let inner = ''
    if (id === 'timeline') {
      inner = `<div class="timeline" id="timeline"></div>`
    } else if (id === 'step-files') {
      inner = `<div id="step-file-list" class="grid file-scroll-list"></div>`
    } else if (id === 'all-files') {
      inner = `<div id="file-list" class="grid file-scroll-list"></div>`
    } else if (id === 'project-file-tree') {
      inner = `<div id="project-filetree" class="project-filetree"><div class="subtle" style="padding:16px;text-align:center;">请先选择项目</div></div>`
    } else if (id === 'git-log') {
      inner = `<div id="git-log" class="timeline"></div>`
    } else if (id === 'review-editor') {
      inner = `<div id="review-file-list" class="grid file-scroll-list"></div>`
    } else if (id === 'config-editor') {
      inner = `<div id="config-editor-content"></div>`
    }
    return `<section class="panel-section${collapseClass}">
      <h3 class="panel-section-header">${escapeHtml(title)}<span class="panel-section-actions">${headerActions}</span></h3>
      <div class="panel-section-body">${inner}</div>
    </section>`
  }).join('')

  // 异步填充需要加载数据的区块
  if ($('#project-filetree')) renderProjectFileTree()
}

export function showPage(page) {
  state.activePage = page
  $$('.page').forEach((el) => el.classList.toggle('active', el.id === `page-${page}`))
  $$('#nav button').forEach((button) => button.classList.toggle('active', button.dataset.page === page))
  updateEvidencePanelVisibility()
  if (page === 'data') {
    setTimeout(() => renderFileTree(), 0)
    // 绑定项目选择下拉 → 自动更新上传目标目录
    setTimeout(() => {
      const sel = $('#data-project-select')
      const dest = $('#upload-dest')
      if (sel && dest) {
        sel.addEventListener('change', () => {
          if (sel.value) {
            dest.value = `inputs/${sel.value}/`
          } else {
            dest.value = 'inputs/'
          }
        })
      }
    }, 50)
  }
}

/**
 * 右侧证据面板仅在 Agent 工作流页面显示。
 */
function updateEvidencePanelVisibility() {
  const evidence = $('#evidence')
  const layout = $('#layout')
  if (!evidence || !layout) return
  const visible = state.activePage === 'agent'
  evidence.style.display = visible ? '' : 'none'
  layout.classList.toggle('evidence-hidden', !visible)
}

export function setAgentStatus(status, progress = 0, label = null) {
  state.status = status
  const normalized = status.toLowerCase()
  $('#agent-status').textContent = `Agent ${label || status}`
  $('#agent-dot').className = `status-dot ${normalized}`
  $('#agent-progress').style.width = `${progress}%`
  const isRunning = ['Running', 'Validating', 'Retrying'].includes(status)
  const running = isRunning ? '1' : '0'
  const runningEl = $('#running-agents')
  if (runningEl) runningEl.textContent = running
  const stopBtn = $('#stop-task')
  if (stopBtn) stopBtn.style.display = isRunning ? 'inline-block' : 'none'
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
  if (promptEl && !promptEl.value.trim()) {
    // 自定义任务填充引导 prompt；预定义任务不预制
    if (task.id === '__custom__') {
      promptEl.value = '请描述您的审计任务需求，并说明输入数据路径（如 inputs/{客户名}/ 下的文件）和期望的输出结果路径（如 outputs/{客户名}/ 下的文件）。'
    }
  }

  // ------ 项目选择器（替代原 task-list） ------
  const taskList = $('#workflow-task-list')
  if (taskList) {
    taskList.innerHTML = renderProjectSelector()
  }

  // ------ Detect 识别方式选择器 ------
  const detectMethod = $('#detect-method-bar')
  if (detectMethod) {
    detectMethod.innerHTML = renderDetectMethodSelector()
  }

  // ------ 步骤列表 ------
  const steps = $('#workflow-step-list')
  if (steps) {
    steps.innerHTML = task.steps.map((step, index) => {
      const status = stepStatus(task.id, step.id)
      const active = index === state.activeStepIndex ? 'active' : ''
      const statusClass = status === 'completed' ? 'done' : status === 'failed' ? 'failed' : ''
      const icon = status === 'completed' ? '✓' : status === 'failed' ? '!' : status === 'running' ? '⏳' : '▶'
      return `
        <div class="step-card ${active} ${statusClass}" data-step-index="${index}">
          <div class="step-title">
            <strong>${escapeHtml(step.label)}</strong>
            <span class="step-actions-inline">
              <span class="badge">${statusLabel(status)}</span>
              <button class="run-step-btn" data-run-step="${index}" title="运行此步骤">${icon}</button>
            </span>
          </div>
          <div class="subtle">${escapeHtml(step.title)}</div>
          <div class="step-files">${step.outputs.slice(0, 2).map((file) => `<span>${escapeHtml(shortName(file))}</span>`).join('')}</div>
        </div>
      `
    }).join('')
  }

  const title = $('#active-task-title')
  if (title) {
    title.textContent = task.id === '__custom__'
      ? (state.customTaskName || '自定义任务')
      : task.name
  }
  const desc = $('#active-task-desc')
  if (desc) {
    const wfTask = findWorkflowTaskByName(state.customTaskName)
    if (wfTask) {
      desc.textContent = wfTask.description
    } else {
      desc.textContent = task.description
    }
  }
  renderReviewFiles()
  renderProjectFileTree()

  // ------ 附加需求 textarea ------
  const reqBar = $('#step-requirement-bar')
  const reqEl = $('#step-requirement')
  if (reqBar && reqEl) {
    const step = activeStep()
    const llmSteps = ['detect', 'clean-bank', 'clean-ledger', 'clean-settlement', 'clean-outbound', 'fill', 'match']
    const isLlm = step && llmSteps.includes(step.id)
    reqBar.style.display = isLlm ? '' : 'none'
    if (isLlm) {
      const key = taskRunKey()
      reqEl.value = state.stepRequirements[key] || ''
    }
  }
}

/**
 * 渲染项目选择器 UI：下拉菜单选择已有项目 或 手动输入客户名+任务名。
 */
function renderProjectSelector() {
  const projects = state.projects || []
  const options = projects.map((p) => {
    const selected = p.id === state.activeProjectId ? 'selected' : ''
    return `<option value="${p.id}" ${selected}>${escapeHtml(p.customer_name)} — ${escapeHtml(p.task_name)} [${escapeHtml(p.status)}]</option>`
  }).join('')

  const customChecked = state.activeProjectId === null ? 'checked' : ''
  const customerVal = escapeHtml(state.customCustomerName)
  const taskOptions = workflowTasks.map((t) => {
    const sel = t.name === state.customTaskName ? 'selected' : ''
    return `<option value="${escapeHtml(t.name)}" ${sel}>${escapeHtml(t.name)}</option>`
  }).join('')
  // 如果当前自定义任务名不在预定义列表中，动态添加选项
  const isCustomName = state.customTaskName && !workflowTasks.some((t) => t.name === state.customTaskName)
  const customTaskOption = isCustomName
    ? `<option value="${escapeHtml(state.customTaskName)}" selected>${escapeHtml(state.customTaskName)} (自定义)</option>`
    : ''

  return `
    <div class="project-bar-inner">
      <span class="project-bar-label">项目</span>
      <select id="project-select" class="project-bar-select">
        <option value="">-- 选择已有项目 --</option>
        ${options}
      </select>
      <label class="project-bar-custom">
        <input type="checkbox" id="project-custom-check" ${customChecked} /> 自定义
      </label>
      <span id="project-custom-fields" class="project-bar-fields" style="display:${state.activeProjectId === null ? 'inline-flex' : 'none'};">
        <input id="project-customer-input" class="project-bar-input" placeholder="客户名称" value="${customerVal}" />
        <select id="project-task-select" class="project-bar-select">${customTaskOption}${taskOptions}</select>
      </span>
    </div>
  `
}

/**
 * 渲染统一的解析器选择器。
 * 完全由 config.task_definitions.yml 驱动：
 *   - allow_llm=true  → 显示 "🤖 LLM 智能解析"（非 detect 步骤追加 "🔄 LLM 重新生成"）
 *   - allow_script=true 且 allow_scripts_list 有值 → 显示列表中的脚本解析器
 *   - YAML 中无对应任务定义 → 显示错误提示
 */
function renderDetectMethodSelector() {
  const task = activeTask()
  const step = task.steps[state.activeStepIndex]
  if (!step) return ''

  // 查找当前任务在 task_definitions.yml 中的定义
  const def = state.taskDefinitions?.find(
    (d) => d.name === state.customTaskName || d.dir_name === state.customTaskName
  )

  // ── YAML 中找不到任务定义 → 报错 ──
  if (!def) {
    return `
      <div class="project-bar-inner" style="margin-top:6px;">
        <span class="project-bar-label">解析器</span>
        <span style="color:var(--danger,#e55);">⚠ 未在 task_definitions.yml 中找到「${escapeHtml(state.customTaskName)}」的定义</span>
      </div>
    `
  }

  // 查找当前步骤在定义中的配置
  const stepDef = def?.steps?.find((s) => s.id === step.id)
  const isDetectStep = step?.id === 'detect' || step?.id === 'osm-detect'

  let options = []

  // ── LLM 选项 ──
  if (stepDef?.allow_llm) {
    options.push({ value: 'llm', label: '🤖 LLM 智能解析' })
    if (!isDetectStep) {
      options.push({ value: 'llm_regenerate', label: '🔄 LLM 重新生成' })
    }
  }

  // ── 脚本解析器选项（来自 allow_scripts_list） ──
  if (stepDef?.allow_script && Array.isArray(stepDef.allow_scripts_list) && stepDef.allow_scripts_list.length > 0) {
    for (const entry of stepDef.allow_scripts_list) {
      // 兼容旧格式（纯字符串）和新格式（{id, label} 对象）
      const id = typeof entry === 'string' ? entry : entry.id
      const label = typeof entry === 'string' ? entry : (entry.label || entry.id)
      options.push({ value: id, label: `📜 ${label}` })
    }
  }

  // ── 无可选项 → 提示 ──
  if (options.length === 0) {
    return `
      <div class="project-bar-inner" style="margin-top:6px;">
        <span class="project-bar-label">解析器</span>
        <span class="subtle">此步骤无可用的解析器</span>
      </div>
    `
  }

  const method = state.detectMethod

  // 如果当前选中的值不在可用选项中，自动回退到第一个
  const validValues = options.map(o => o.value)
  const selectedMethod = validValues.includes(method) ? method : validValues[0]
  if (selectedMethod !== method) {
    state.detectMethod = selectedMethod
  }

  const radios = options.map(o => {
    const checked = selectedMethod === o.value ? 'checked' : ''
    return `<label class="project-bar-custom" style="margin-right:12px;">
      <input type="radio" name="detect-method" value="${o.value}" ${checked} /> ${escapeHtml(o.label)}
    </label>`
  }).join('')

  return `
    <div class="project-bar-inner" style="margin-top:6px;">
      <span class="project-bar-label">解析器</span>
      ${radios}
    </div>
  `
}

export function renderFiles(files, root = 'outputs') {
  state.files = files.map(normalizePath)
  const count = $('#file-count')
  if (count) count.textContent = files.length
  const list = $('#file-list')
  if (!list) return
  if (!files.length) {
    list.innerHTML = '<div class="subtle">暂无输出文件</div>'
    return
  }
  list.innerHTML = state.files.slice(0, 80).map((file) => fileRow(file, `${root}`)).join('')
}

export function renderFileError(message) {
  $('#file-list').innerHTML = `<div class="subtle">本地后端启动中或不可用：${escapeHtml(message)}</div>`
}

export function renderStepFiles() {
  const task = activeTask()
  const seen = new Set()
  // 将所有步骤的 outputs 解析为完整路径，只保留 state.files 中实际存在的
  const existingSet = new Set(state.files.map(normalizePath))
  const files = task.steps.flatMap((step) =>
    step.outputs
      .map((rel) => resolveProjectPath(rel))
      .filter((full) => full && existingSet.has(full))
      .map((file) => ({ file, step }))
  ).filter(({ file, step }) => {
    const key = `${step.id}:${file}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  const list = $('#step-file-list')
  if (!list) return
  if (files.length === 0) {
    list.innerHTML = '<div class="subtle">暂无生成文件，执行步骤后出现</div>'
    return
  }
  list.innerHTML = files.map(({ file, step }) => fileRow(file, step.label)).join('')
}

/**
 * 渲染复核文件列表（根据当前选中工作流的 reviewFiles 配置）。
 * 每个文件可点击打开人工复核编辑器。
 */
function renderReviewFiles() {
  const wfTask = findWorkflowTaskByName(state.customTaskName)
  const reviewFiles = wfTask ? wfTask.reviewFiles || [] : []
  const list = $('#review-file-list')
  if (!list) return
  if (reviewFiles.length === 0) {
    list.innerHTML = '<div class="subtle">此工作流暂无人工复核文件</div>'
    return
  }
  const existingSet = new Set(state.files.map(normalizePath))
  const existing = reviewFiles
    .map((rel) => resolveProjectPath(rel))
    .filter((full) => full && existingSet.has(full))

  if (existing.length === 0) {
    list.innerHTML = '<div class="subtle">暂无人工复核文件，执行步骤后生成</div>'
    return
  }
  list.innerHTML = existing.map((file) => {
    const name = shortName(file)
    return `<div class="file-row review-file-item" data-file="${escapeHtml(file)}" title="${escapeHtml(file)}">
      <span>📝 ${escapeHtml(name)}</span>
      <button class="open-review-btn" data-file="${escapeHtml(file)}">打开复核</button>
    </div>`
  }).join('')
}

/**
 * 渲染项目文件树：展示当前客户名/任务名下的 inputs 和 outputs 目录。
 * 支持文件夹折叠、文件预览、复制路径、拖拽导入。
 */
let _fileTreePending = null
export async function renderProjectFileTree() {
  if (_fileTreePending) return _fileTreePending
  _fileTreePending = _renderProjectFileTreeImpl()
  try { return await _fileTreePending } finally { _fileTreePending = null }
}
async function _renderProjectFileTreeImpl() {
  const container = $('#project-filetree')
  if (!container) return

  const customer = state.customCustomerName
  const wfTask = findWorkflowTaskByName(state.customTaskName)
  const taskDir = wfTask ? wfTask.dirName : (state.customTaskName || '')

  if (!customer || !taskDir) {
    container.innerHTML = '<div class="subtle" style="padding:16px;text-align:center;">请先选择项目</div>'
    return
  }

  container.innerHTML = '<div class="subtle" style="padding:16px;text-align:center;">加载中...</div>'

  const inputsRoot = `inputs/${customer}/${taskDir}`
  const outputsRoot = `outputs/${customer}/${taskDir}`

  try {
    const [inputsRes, outputsRes] = await Promise.all([
      api.listFiles(inputsRoot).catch(() => ({ files: [] })),
      api.listFiles(outputsRoot).catch(() => ({ files: [] })),
    ])

    const inputsFiles = (inputsRes.files || []).filter(f => !f.endsWith('/') && !f.endsWith('.pyc'))
    const outputsFiles = (outputsRes.files || []).filter(f => !f.endsWith('/') && !f.endsWith('.pyc'))

    let html = ''
    if (inputsFiles.length > 0) {
      html += `<div class="tree-root"><span class="tree-icon">📂</span><strong>inputs</strong></div>`
      html += renderProjectTreeNodes(inputsFiles, inputsRoot)
    } else {
      html += `<div class="tree-root"><span class="tree-icon">📂</span><strong>inputs</strong> <span class="subtle">(空)</span></div>`
    }
    if (outputsFiles.length > 0) {
      html += `<div class="tree-root" style="margin-top:8px;"><span class="tree-icon">📂</span><strong>outputs</strong></div>`
      html += renderProjectTreeNodes(outputsFiles, outputsRoot)
    } else {
      html += `<div class="tree-root" style="margin-top:8px;"><span class="tree-icon">📂</span><strong>outputs</strong> <span class="subtle">(空)</span></div>`
    }

    container.innerHTML = html
    bindProjectFileTreeEvents(container)
    bindProjectDragAndDrop(container, inputsRoot)

    // 默认折叠所有文件夹
    container.querySelectorAll('.tree-folder').forEach(folder => {
      let sibling = folder.nextElementSibling
      while (sibling && !sibling.classList.contains('tree-root')) {
        const sibIndent = parseInt(sibling.style.paddingLeft) || 0
        const folderIndent = parseInt(folder.style.paddingLeft) || 0
        if (sibIndent > folderIndent) {
          sibling.style.display = 'none'
          sibling = sibling.nextElementSibling
        } else {
          break
        }
      }
    })
  } catch (err) {
    container.innerHTML = `<div class="subtle" style="padding:16px;text-align:center;">加载失败: ${escapeHtml(err.message)}</div>`
  }
}

/**
 * 递归构建项目文件树的 HTML 节点。
 */
function renderProjectTreeNodes(files, basePath) {
  const tree = {}
  for (const file of files) {
    const rel = file.startsWith(basePath + '/') ? file.slice(basePath.length + 1)
              : file.startsWith(basePath) ? file.slice(basePath.length) : file
    const parts = rel.replace(/\\/g, '/').split('/').filter(Boolean)
    let cursor = tree
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i]
      if (i === parts.length - 1) {
        if (!cursor._files) cursor._files = []
        cursor._files.push({ name: seg, relPath: rel })
      } else {
        if (!cursor[seg]) cursor[seg] = {}
        cursor = cursor[seg]
      }
    }
  }

  function renderNode(node, name, depth) {
    const hasFiles = node._files && node._files.length > 0
    const indent = depth * 16
    let html = ''
    if (name) {
      html += `<div class="tree-folder" style="padding-left:${indent}px;" data-expand="false">
        <span class="tree-icon">📁</span><span class="tree-name">${escapeHtml(name)}</span></div>`
    }
    for (const key of Object.keys(node).sort()) {
      if (key === '_files') continue
      html += renderNode(node[key], key, name ? depth + 1 : depth)
    }
    if (hasFiles) {
      for (const f of node._files) {
        const fullPath = basePath + '/' + f.relPath
        html += `<div class="tree-file" style="padding-left:${(name ? depth + 1 : depth) * 16}px;" data-path="${escapeHtml(fullPath)}">
          <span class="tree-icon">📄</span><span class="tree-name">${escapeHtml(f.name)}</span>
          <button class="tree-copy-btn" data-copy-path="${escapeHtml(fullPath)}" title="复制路径">&#128203;</button>
        </div>`
      }
    }
    return html
  }
  return renderNode(tree, null, 0)
}

/**
 * 绑定项目文件树的事件：文件夹折叠、复制路径。
 * 文件点击预览由 main.js 事件代理处理。
 */
function bindProjectFileTreeEvents(container) {
  // 文件夹折叠/展开
  container.querySelectorAll('.tree-folder').forEach(folder => {
    folder.style.cursor = 'pointer'
    folder.addEventListener('click', () => {
      const expanded = folder.dataset.expand === 'true'
      folder.dataset.expand = expanded ? 'false' : 'true'
      const icon = folder.querySelector('.tree-icon')
      if (icon) icon.textContent = expanded ? '📁' : '📂'
      let sibling = folder.nextElementSibling
      while (sibling && !sibling.classList.contains('tree-root')) {
        if (sibling.style.paddingLeft && parseInt(sibling.style.paddingLeft) > parseInt(folder.style.paddingLeft || '0')) {
          sibling.style.display = expanded ? 'none' : ''
        } else {
          break
        }
        sibling = sibling.nextElementSibling
      }
    })
  })

  // 复制路径
  container.querySelectorAll('.tree-copy-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation()
      const path = btn.dataset.copyPath
      try {
        await navigator.clipboard.writeText(path)
        btn.textContent = '✓'
        setTimeout(() => { btn.innerHTML = '&#128203;' }, 1500)
      } catch {
        btn.textContent = '✗'
        setTimeout(() => { btn.innerHTML = '&#128203;' }, 1500)
      }
    })
  })
}

/**
 * 绑定项目文件树的拖拽导入：从系统文件管理器拖入文件到 inputs/{customer}/{task}/
 */
function bindProjectDragAndDrop(container, inputsRoot) {
  if (container.dataset.dndBound) return
  container.dataset.dndBound = 'true'

  container.addEventListener('dragover', (e) => {
    e.preventDefault()
    e.stopPropagation()
    if (!e.dataTransfer.types.includes('Files')) return
    container.classList.add('drag-over')
    if (!container.querySelector('.drop-hint')) {
      const hint = document.createElement('div')
      hint.className = 'drop-hint'
      hint.textContent = `释放文件以导入 ${inputsRoot}/`
      container.appendChild(hint)
    }
  })

  container.addEventListener('dragleave', (e) => {
    if (container.contains(e.relatedTarget)) return
    container.classList.remove('drag-over')
    const hint = container.querySelector('.drop-hint')
    if (hint) hint.remove()
  })

  container.addEventListener('drop', async (e) => {
    e.preventDefault()
    e.stopPropagation()
    container.classList.remove('drag-over')
    const hint = container.querySelector('.drop-hint')
    if (hint) hint.remove()

    if (!e.dataTransfer.types.includes('Files')) return
    const files = Array.from(e.dataTransfer.files)
    if (files.length === 0) return

    const filePaths = files.map(f => f.path).filter(Boolean)
    if (filePaths.length === 0) {
      // Electron 中 File 对象有 path 属性
      return
    }

    const dest = inputsRoot + '/'
    let uploaded = 0
    let failed = 0
    for (const srcPath of filePaths) {
      try {
        await api.copyFromPath(srcPath, dest)
        uploaded++
      } catch (err) {
        failed++
      }
    }

    // 刷新文件树
    await renderProjectFileTree()
  })
}

/**
 * 渲染项目管理表格。
 * @param {Array} projects - 项目列表（已过滤）
 */
export function renderProjectsTable(projects) {
  const tbody = $('#project-table-body')
  if (!tbody) return
  if (!projects || projects.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="subtle">暂无项目数据</td></tr>'
    return
  }
  tbody.innerHTML = projects.map((p) => `
    <tr>
      <td>${escapeHtml(p.task_name)}</td>
      <td>${escapeHtml(p.customer_name)}</td>
      <td><span class="badge">${escapeHtml(p.status)}</span></td>
      <td>${escapeHtml(p.created_at)}</td>
      <td>${escapeHtml(p.responsible_person)}</td>
      <td><span class="badge ${riskClass(p.risk)}">${escapeHtml(p.risk)}</span></td>
      <td>
        <button class="edit-project-btn" data-project-id="${p.id}" title="编辑">✎</button>
        <button class="delete-project-btn" data-project-id="${p.id}" title="删除">✕</button>
      </td>
    </tr>
  `).join('')
}

/**
 * 弹出新建/编辑项目的 Modal 表单。
 * @param {object|null} project - 编辑时传入已有项目，新建时传 null
 * @param {function} onSave - 保存回调 (formData) => Promise
 * @param {function} onDelete - 删除回调 (projectId) => Promise（仅编辑模式可用）
 */
export async function showProjectFormModal(project, onSave, onDelete) {
  const { createModal } = await import('./modal.js')
  const isEdit = !!project

  const statusOptions = ['Planning', 'Running', 'Reviewing', 'Completed'].map((s) => {
    const sel = project && project.status === s ? 'selected' : ''
    return `<option value="${s}" ${sel}>${s}</option>`
  }).join('')
  const riskOptions = ['Low', 'Medium', 'High', 'Critical'].map((r) => {
    const sel = project && project.risk === r ? 'selected' : ''
    return `<option value="${r}" ${sel}>${r}</option>`
  }).join('')
  const taskOptions = workflowTasks.map((t) => {
    const sel = project && project.task_name === t.name ? 'selected' : ''
    return `<option value="${escapeHtml(t.name)}" ${sel}>${escapeHtml(t.name)}</option>`
  }).join('')
  // 如果 project.task_name 不在预定义列表中，添加自定义选项
  const isCustomTask = project && !workflowTasks.some((t) => t.name === project.task_name)
  const customTaskOption = isCustomTask ? `<option value="${escapeHtml(project.task_name)}" selected>${escapeHtml(project.task_name)} (自定义)</option>` : ''

  const modal = createModal({ title: isEdit ? '编辑项目' : '新建项目', width: '520px', height: 'auto' })
  modal.setBody(`
    <form id="project-form" class="project-form">
      <label>任务名称
        <select id="form-task-name" class="search" style="width:100%;">${taskOptions}${customTaskOption}<option value="__custom__">自定义输入...</option></select>
        <input id="form-task-name-custom" class="search" style="width:100%;margin-top:4px;display:none;" placeholder="输入自定义任务名称" value="${isCustomTask ? escapeHtml(project.task_name) : ''}" />
      </label>
      <label>客户名称 <input id="form-customer" class="search" value="${escapeHtml(project?.customer_name || '')}" /></label>
      <label>项目状态 <select id="form-status">${statusOptions}</select></label>
      <label>创建时间 <input id="form-created" class="search" type="date" value="${project?.created_at || ''}" /></label>
      <label>负责人 <input id="form-person" class="search" value="${escapeHtml(project?.responsible_person || '')}" /></label>
      <label>风险等级 <select id="form-risk">${riskOptions}</select></label>
      <div class="form-actions">
        ${isEdit ? '<button type="button" id="form-delete-btn" class="danger">删除项目</button>' : ''}
        <button type="button" id="form-cancel-btn">取消</button>
        <button type="submit" class="primary">保存</button>
      </div>
    </form>
  `)

  // 任务名称下拉 → 自定义输入联动
  modal.getBodyEl().querySelector('#form-task-name').addEventListener('change', (e) => {
    const customInput = modal.getBodyEl().querySelector('#form-task-name-custom')
    if (e.target.value === '__custom__') {
      customInput.style.display = ''
      customInput.focus()
    } else {
      customInput.style.display = 'none'
    }
  })

  modal.getBodyEl().querySelector('#form-cancel-btn').addEventListener('click', () => modal.close())
  if (isEdit) {
    modal.getBodyEl().querySelector('#form-delete-btn').addEventListener('click', async () => {
      if (confirm(`确定要删除项目「${project.customer_name} — ${project.task_name}」吗？`)) {
        await onDelete(project.id)
        modal.close()
      }
    })
  }

  modal.getBodyEl().querySelector('#project-form').addEventListener('submit', async (e) => {
    e.preventDefault()
    const getVal = (id) => modal.getBodyEl().querySelector(id).value
    const taskSelect = getVal('#form-task-name')
    const customTask = modal.getBodyEl().querySelector('#form-task-name-custom').value.trim()
    const taskName = taskSelect === '__custom__' ? customTask : taskSelect

    const payload = {
      task_name: taskName,
      customer_name: getVal('#form-customer'),
      status: getVal('#form-status'),
      created_at: getVal('#form-created'),
      responsible_person: getVal('#form-person'),
      risk: getVal('#form-risk'),
    }
    await onSave(payload)
    modal.close()
  })

  modal.open()
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

  // 从 result 中提取 token 使用信息
  let tokenInfo = ''
  if (result && result.usage) {
    const u = result.usage
    const total = u.total_tokens || 0
    const prompt = u.prompt_tokens || 0
    const completion = u.completion_tokens || 0
    if (total > 0) {
      tokenInfo = ` | 📊 Tokens: ${total} (提示:${prompt} 完成:${completion})`
    }
  }

  block.innerHTML = `
    <div class="message-head"><span>${escapeHtml(role)}${title ? ` · ${escapeHtml(title)}` : ''}</span><span>${failed ? 'Failed' : time}${tokenInfo} <button class="message-delete-btn" title="删除此消息">✕</button><span class="message-collapse-btn">▼</span></span></div>
    <div class="message-body">
      ${body ? `<p>${escapeHtml(body)}</p>` : ''}
      ${result ? `<pre class="log-block">${escapeHtml(JSON.stringify(result, null, 2))}</pre>` : ''}
    </div>
  `

  // 删除按钮事件：阻止冒泡以避免触发折叠，点击后淡出并移除
  const deleteBtn = block.querySelector('.message-delete-btn')
  deleteBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    block.classList.add('message-deleting')
    block.addEventListener('animationend', () => block.remove())
  })

  chat.appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })
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
  return `<section class="page" id="page-projects"><div class="page-header"><div><h1>项目管理</h1><p class="subtle">按状态、负责人和风险等级管理审计项目</p></div><button id="new-project-btn" class="primary">新建项目</button></div><div class="card"><div class="toolbar" style="margin-bottom:12px;"><input id="project-search" class="search" placeholder="搜索项目、客户或负责人"><select id="project-filter-status"><option value="">全部状态</option><option>Planning</option><option>Running</option><option>Reviewing</option><option>Completed</option></select><select id="project-filter-risk"><option value="">全部风险</option><option>Low</option><option>Medium</option><option>High</option><option>Critical</option></select></div><div id="project-table-container"><table class="table"><thead><tr><th>任务名称</th><th>客户名称</th><th>项目状态</th><th>创建时间</th><th>负责人</th><th>风险等级</th><th>操作</th></tr></thead><tbody id="project-table-body"><tr><td colspan="7" class="subtle">加载中...</td></tr></tbody></table></div></div></section>`
}

function dataPage() {
  return `<section class="page" id="page-data">
    <div class="page-header"><div><h1>数据源</h1><p class="subtle">上传文件、浏览文件树，点击文件查看自动数据画像。按「客户名称/任务名称」组织文件。</p></div></div>
    <div class="upload-bar" style="margin-bottom:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <select id="data-project-select" class="project-bar-select" style="min-width:200px;">
        <option value="">-- 选择项目以定位目录 --</option>
      </select>
      <input type="file" id="upload-file" style="flex:1;min-width:160px;">
      <input type="text" id="upload-dest" value="inputs/" style="width:280px;height:32px;padding:0 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);">
      <button id="upload-btn" class="primary">上传</button>
    </div>
    <div class="grid two-col" style="align-items:start;">
      <div class="card" style="padding:10px;">
        <h2 style="margin-bottom:10px;">📁 文件树</h2>
        <div id="file-tree" class="file-tree"><div class="subtle">加载中...</div></div>
      </div>
      <div class="card" id="data-profile-card">
        <h2>📊 自动数据画像</h2>
        <div id="data-profile-content" style="margin-top:10px;">
          <p class="subtle">请点击左侧文件查看其数据画像</p>
        </div>
      </div>
    </div>
  </section>`
}

/**
 * 递归渲染文件树节点（inputs / outputs 下所有文件）
 */
function renderTreeNodes(files, basePath) {
  const tree = {}
  for (const file of files) {
    const rel = file.startsWith(basePath + '/') ? file.slice(basePath.length + 1) : file.startsWith(basePath) ? file.slice(basePath.length) : file
    const parts = rel.replace(/\\/g, '/').split('/').filter(Boolean)
    let cursor = tree
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i]
      if (i === parts.length - 1) {
        if (!cursor._files) cursor._files = []
        cursor._files.push({ name: seg, relPath: rel })
      } else {
        if (!cursor[seg]) cursor[seg] = {}
        cursor = cursor[seg]
      }
    }
  }

  function renderNode(node, name, depth) {
    const hasFiles = node._files && node._files.length > 0
    const hasDirs = Object.keys(node).filter((k) => k !== '_files').length > 0
    const indent = depth * 16
    let html = ''
    if (name) {
      html += `<div class="tree-folder" style="padding-left:${indent}px;" data-expand="false">
        <span class="tree-icon">📁</span><span class="tree-name">${escapeHtml(name)}</span></div>`
    }
    // subdirs
    for (const key of Object.keys(node).sort()) {
      if (key === '_files') continue
      html += renderNode(node[key], key, name ? depth + 1 : depth)
    }
    // files
    if (hasFiles) {
      for (const f of node._files) {
        html += `<div class="tree-file" style="padding-left:${(name ? depth + 1 : depth) * 16}px;" data-path="${escapeHtml(basePath + '/' + f.relPath)}">
          <span class="tree-icon">📄</span><span class="tree-name">${escapeHtml(f.name)}</span>
          <button class="tree-delete-btn" data-delete="${escapeHtml(basePath + '/' + f.relPath)}" title="删除文件">🗑</button>
        </div>`
      }
    }
    return html
  }
  return renderNode(tree, null, 0)
}

/**
 * 构建并渲染完整文件树（inputs + outputs）
 */
export async function renderFileTree() {
  const container = $('#file-tree')
  if (!container) return
  container.innerHTML = '<div class="subtle">加载中...</div>'

  // 同时加载项目列表，填充项目下拉选择器
  let projects = []
  try {
    const projData = await api.listProjects()
    projects = projData.projects || []
  } catch (e) { /* ignore */ }
  const projSelect = $('#data-project-select')
  if (projSelect) {
    const currentVal = projSelect.value
    projSelect.innerHTML = '<option value="">-- 选择项目以定位目录 --</option>' +
      projects.map((p) => {
        const val = `${p.customer_name}/${p.task_name}`
        const sel = val === currentVal ? 'selected' : ''
        return `<option value="${escapeHtml(val)}" ${sel}>${escapeHtml(p.customer_name)} — ${escapeHtml(p.task_name)}</option>`
      }).join('')
  }

  try {
    const [inputsRes, outputsRes] = await Promise.all([
      api.listFiles('inputs').catch(() => ({ files: [] })),
      api.listFiles('outputs').catch(() => ({ files: [] }))
    ])
    const inputsFiles = (inputsRes.files || []).filter((f) => !f.endsWith('/'))
    const outputsFiles = (outputsRes.files || []).filter((f) => !f.endsWith('/'))

    let html = ''
    if (inputsFiles.length > 0) {
      html += `<div class="tree-root"><span class="tree-icon">📁</span><strong>inputs</strong></div>`
      html += renderTreeNodes(inputsFiles, 'inputs')
    }
    if (outputsFiles.length > 0) {
      html += `<div class="tree-root"><span class="tree-icon">📁</span><strong>outputs</strong></div>`
      html += renderTreeNodes(outputsFiles, 'outputs')
    }
    if (!html) html = '<div class="subtle">暂无文件</div>'
    container.innerHTML = html

    // 点击文件时触发数据画像
    container.querySelectorAll('.tree-file').forEach((el) => {
      el.addEventListener('click', (e) => {
        // 如果点击的是删除按钮则不触发画像
        if (e.target.closest('.tree-delete-btn')) return
        const path = el.dataset.path
        renderDataProfile(path)
        // 高亮选中
        container.querySelectorAll('.tree-file.active').forEach((e) => e.classList.remove('active'))
        el.classList.add('active')
      })
    })
    // 删除按钮
    container.querySelectorAll('.tree-delete-btn').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation()
        const path = btn.dataset.delete
        if (!confirm(`确定删除文件？\n${path}`)) return
        try {
          await api.deleteFile(path)
          await renderFileTree()
          // 如果当前画像面板正在显示此文件则清空
          const content = $('#data-profile-content')
          if (content) content.innerHTML = '<p class="subtle">请点击左侧文件查看其数据画像</p>'
        } catch (err) {
          alert(`删除失败: ${err.message}`)
        }
      })
    })
    // 折叠/展开文件夹
    container.querySelectorAll('.tree-folder').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation()
        const expanded = el.dataset.expand === 'true'
        el.dataset.expand = expanded ? 'false' : 'true'
        el.querySelector('.tree-icon').textContent = expanded ? '📁' : '📂'
        // toggle siblings until next same-depth folder
        let next = el.nextElementSibling
        while (next) {
          const nextIndent = parseInt(next.style.paddingLeft) || 0
          const myIndent = parseInt(el.style.paddingLeft) || 0
          if (nextIndent <= myIndent && !next.classList.contains('tree-file') && !next.classList.contains('tree-folder')) break
          if (next.classList.contains('tree-folder') && nextIndent <= myIndent) break
          next.style.display = expanded ? 'none' : ''
          next = next.nextElementSibling
        }
      })
    })
    // 默认折叠所有文件夹
    container.querySelectorAll('.tree-folder').forEach((folder) => {
      let next = folder.nextElementSibling
      while (next) {
        const nextIndent = parseInt(next.style.paddingLeft) || 0
        const folderIndent = parseInt(folder.style.paddingLeft) || 0
        if (nextIndent > folderIndent) {
          next.style.display = 'none'
          next = next.nextElementSibling
        } else {
          break
        }
      }
    })
  } catch (err) {
    container.innerHTML = `<div class="subtle">加载失败: ${escapeHtml(err.message)}</div>`
  }
}

/**
 * 读取并渲染单个文件的数据画像
 */
export async function renderDataProfile(filePath) {
  const card = $('#data-profile-card')
  const content = $('#data-profile-content')
  if (!card || !content) return
  content.innerHTML = '<div class="subtle">加载中...</div>'
  try {
    const resp = await api.readFile(filePath)
    const raw = resp.content || ''
    if (!raw.trim()) {
      content.innerHTML = '<p class="subtle">文件为空</p>'
      return
    }
    const rows = parseCsv(raw)
    if (rows.length === 0) {
      content.innerHTML = '<p class="subtle">无法解析文件内容</p>'
      return
    }
    const headers = rows[0]
    const dataRows = rows.slice(1).filter((r) => r.some((c) => c.trim() !== ''))

    // 推断列类型
    const colTypes = headers.map((h, i) => {
      const samples = dataRows.slice(0, 100).map((r) => (r[i] || '').trim()).filter(Boolean)
      if (samples.length === 0) return { name: h, type: '空列', icon: '⬜' }
      const numCount = samples.filter((v) => !isNaN(parseFloat(v)) && isFinite(v) && v !== '').length
      const dateCount = samples.filter((v) => /^\d{4}[-/]\d{1,2}[-/]\d{1,2}/.test(v) || /^\d{1,2}[-/]\d{1,2}[-/]\d{4}/.test(v)).length
      if (numCount >= samples.length * 0.8) return { name: h, type: '数值', icon: '🔢' }
      if (dateCount >= samples.length * 0.8) return { name: h, type: '日期', icon: '📅' }
      return { name: h, type: '文本', icon: '📝' }
    })

    // 金额列识别
    const amountCols = colTypes.filter((c) => c.type === '数值' && /金额|发生额|余额|amount|借方|贷方/i.test(c.name))

    // 日期列识别
    const dateCols = colTypes.filter((c) => c.type === '日期' || /日期|date|时间|time/i.test(c.name))

    // 样本数据(前5行)
    const sampleRows = dataRows.slice(0, 5)

    content.innerHTML = `
      <div class="profile-stats">
        <div class="profile-stat"><span class="label">文件路径</span><strong>${escapeHtml(filePath)}</strong></div>
        <div class="profile-stat"><span class="label">行数</span><strong>${dataRows.length.toLocaleString()}</strong></div>
        <div class="profile-stat"><span class="label">列数</span><strong>${headers.length}</strong></div>
      </div>
      <div style="margin-top:12px;">
        <div class="label" style="margin-bottom:6px;">已识别金额字段</div>
        <strong>${amountCols.length ? amountCols.map((c) => c.name).join('、') : '—'}</strong>
      </div>
      <div style="margin-top:10px;">
        <div class="label" style="margin-bottom:6px;">已识别日期字段</div>
        <strong>${dateCols.length ? dateCols.map((c) => c.name).join('、') : '—'}</strong>
      </div>
      <div style="margin-top:14px;">
        <div class="label" style="margin-bottom:6px;">列信息 (${headers.length} 列)</div>
        <div class="profile-cols">${colTypes.map((c) => `<div class="profile-col-item"><span>${c.icon}</span> <strong>${escapeHtml(c.name)}</strong> <span class="badge">${c.type}</span></div>`).join('')}</div>
      </div>
      <div style="margin-top:14px;">
        <div class="label" style="margin-bottom:6px;">数据预览 (前5行)</div>
        <div class="profile-table-wrap">
          <table class="table" style="font-size:12px;"><thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join('')}</tr></thead>
            <tbody>${sampleRows.map((r) => `<tr>${headers.map((_, i) => `<td>${escapeHtml(r[i] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table>
        </div>
      </div>`
  } catch (err) {
    content.innerHTML = `<div class="subtle">读取失败: ${escapeHtml(err.message)}</div>`
  }
}

function agentPage() {
  return `<section class="page" id="page-agent"><div class="page-header"><div><h1>Agent 工作流</h1><p id="active-task-desc" class="subtle"></p></div><div class="toolbar"><button id="run-next-step">执行下一步</button><button id="run-all-steps" class="primary">执行全部</button></div></div><div id="workflow-task-list" class="project-bar"></div><div id="detect-method-bar" class="project-bar"></div><div class="agent-grid" style="margin-top:12px;"><div><div class="card"><div class="page-header" style="margin-bottom:12px;"><div><h2 id="active-task-title"></h2><p class="subtle">每个步骤可独立执行，也可按顺序全部执行。</p></div></div><div id="workflow-step-list" class="step-list"></div></div><div class="conversation" id="chat"><div class="message"><div class="message-head"><span>Agent</span><span>Ready</span></div><p>请先选择项目，每个步骤可点击 ▶ 独立运行。</p></div></div><div class="step-requirement-bar" id="step-requirement-bar" style="display:none;"><label class="step-requirement-label">附加需求</label><textarea id="step-requirement" rows="2" placeholder="可选：对此步骤的额外要求，会传递给 LLM…"></textarea></div></div></div></section>`
}

function agentLoopPage() {
  return `<section class="page" id="page-agent-loop">
    <div class="page-header">
      <div>
        <h1>Agent 对话</h1>
        <p class="subtle">与审计 Agent 多轮对话，自动扫描文件、读取数据、执行代码</p>
      </div>
      <div class="toolbar">
        <button id="agent-new-conv" class="ghost">新建对话</button>
        <select id="agent-conv-select" class="search" style="min-width:200px;">
          <option value="">-- 选择历史对话 --</option>
        </select>
        <button id="agent-delete-conv" class="ghost" title="删除当前对话" style="display:none;">&#128465;</button>
      </div>
    </div>
    <div class="agent-loop-container">
      <div class="agent-loop-chat">
        <div class="agent-loop-messages" id="agent-messages">
          <div class="agent-welcome">
            <h2>审计 Agent 助手</h2>
            <p class="subtle">我可以帮您扫描项目文件、分析数据、编写和执行处理代码。试试输入："帮我看看 inputs 目录下有哪些文件"</p>
          </div>
        </div>
        <div class="agent-loop-input">
          <textarea id="agent-prompt" placeholder="输入您的指令，例如：分析 inputs/ 下的银行流水文件，统计大额交易..." rows="3"></textarea>
          <div class="agent-loop-actions">
            <span id="agent-loop-status" class="subtle"></span>
            <button id="agent-send" class="primary">发送</button>
          </div>
        </div>
      </div>
      <div class="agent-loop-filetree" id="agent-filetree-panel">
        <div class="agent-filetree-header">
          <strong>项目文件</strong>
          <button id="agent-refresh-filetree" class="ghost" title="刷新文件树" style="padding:2px 8px;">&#8635;</button>
        </div>
        <div class="agent-filetree-body" id="agent-filetree">
          <div class="subtle" style="padding:16px;text-align:center;">加载中...</div>
        </div>
        <div class="agent-filetree-dropdowns" id="agent-output-selector">
          <div class="agent-dropdown-row">
            <label class="agent-dropdown-label">客户</label>
            <input id="agent-customer-input" class="agent-dropdown-input"
                   list="agent-customer-list" placeholder="选择或输入客户名称"
                   autocomplete="off" />
            <datalist id="agent-customer-list"></datalist>
          </div>
          <div class="agent-dropdown-row">
            <label class="agent-dropdown-label">任务</label>
            <input id="agent-task-input" class="agent-dropdown-input"
                   list="agent-task-list" placeholder="选择或输入任务名称"
                   autocomplete="off" />
            <datalist id="agent-task-list"></datalist>
          </div>
          <div class="agent-dropdown-path" id="agent-output-path">
            <span class="subtle">outputs/ — 请先选择客户和任务</span>
          </div>
        </div>
      </div>
    </div>
  </section>`
}

function programsPage() {
  return `<section class="page" id="page-programs"><div class="page-header"><div><h1>审计程序</h1><p class="subtle">可解释、可复核的程序模板</p></div></div><div id="programs-list" class="programs-list"><p class="subtle" style="padding:40px;text-align:center;">加载中...</p></div></section>`
}

/**
 * 根据 api 返回的 readmes 数组渲染审计程序卡片。
 * 每个 readme 匹配 config.workflowTasks 中的任务名和风险等级。
 * 没有 readme 的任务显示占位卡片。
 * Mermaid 图表会通过动态加载 mermaid.js 渲染为交互式 SVG。
 */
export function renderProgramsList(readmes) {
  const container = $('#programs-list')
  if (!container) return

  // 建立 dir_name → readme 映射
  const readmeMap = {}
  for (const r of (readmes || [])) {
    readmeMap[r.dir_name] = r
  }

  const cards = workflowTasks.map((task) => {
    const readme = readmeMap[task.dirName]
    const title = task.name
    const riskBadge = task.risk === 'High'
      ? '<span class="badge badge-high">高风险</span>'
      : task.risk === 'Medium'
        ? '<span class="badge badge-medium">中风险</span>'
        : '<span class="badge badge-low">低风险</span>'

    if (readme) {
      const html = markdownToHtml(readme.content)
      return `<div class="card readme-card">
        <div class="readme-card-head">
          <span class="readme-toggle-arrow">▼</span>
          <h2>${escapeHtml(title)}</h2>
          ${riskBadge}
          <span class="subtle" style="font-size:11px;margin-left:8px;">来源: audit_workflow/${escapeHtml(task.dirName)}/readme.md</span>
        </div>
        <div class="readme-content">${html}</div>
      </div>`
    } else {
      return `<div class="card readme-card readme-card-empty">
        <div class="readme-card-head">
          <span class="readme-toggle-arrow">▼</span>
          <h2>${escapeHtml(title)}</h2>
          ${riskBadge}
        </div>
        <div class="readme-content">
          <p class="subtle">暂无程序文档。请在 audit_workflow/${escapeHtml(task.dirName)}/ 下创建 readme.md。</p>
          <p class="subtle">${escapeHtml(task.description)}</p>
        </div>
      </div>`
    }
  })

  container.innerHTML = cards.join('')

  // ── Mermaid 图表渲染 ──
  // 将 markdown 中的 <pre class="language-mermaid"><code>...</code></pre> 转换为
  // <div class="mermaid">...</div>，然后调用 mermaid.run() 渲染为 SVG。
  const mermaidBlocks = container.querySelectorAll('pre.language-mermaid')
  if (mermaidBlocks.length > 0) {
    for (const pre of mermaidBlocks) {
      const code = pre.querySelector('code')
      const mermaidCode = code ? code.textContent : pre.textContent
      const div = document.createElement('div')
      div.className = 'mermaid'
      div.textContent = mermaidCode
      pre.replaceWith(div)
    }
    renderMermaidDiagrams(container)
  }
}

/**
 * 动态加载 mermaid.js 并渲染页面中的 .mermaid 元素。
 */
let _mermaidLoading = null
let _mermaidReady = false

function renderMermaidDiagrams(container) {
  if (_mermaidReady) {
    // 已初始化，直接调用 run
    if (window.mermaid) {
      window.mermaid.run({ querySelector: '.mermaid' }).catch(() => {})
    }
    return
  }

  if (_mermaidLoading) {
    // 正在加载中，等待完成后重试
    _mermaidLoading.then(() => {
      _mermaidReady = true
      if (window.mermaid) window.mermaid.run({ querySelector: '.mermaid' }).catch(() => {})
    })
    return
  }

  _mermaidLoading = new Promise((resolve) => {
    const script = document.createElement('script')
    script.src = 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js'
    script.onload = () => {
      window.mermaid.initialize({ startOnLoad: false, theme: 'default' })
      window.mermaid.run({ querySelector: '.mermaid' }).catch(() => {})
      resolve()
    }
    script.onerror = () => {
      console.warn('Failed to load mermaid.js, diagrams will not render')
      resolve()
    }
    document.head.appendChild(script)
  })
}

// ============================================================
// 简易 Markdown → HTML 转换器
// ============================================================

function markdownToHtml(md) {
  if (!md) return ''

  // 先提取并保护代码块和 mermaid 块
  const blocks = []
  // 保存围栏代码块
  let text = md.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = blocks.length
    const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : ''
    blocks.push(`<pre${langClass}><code>${escapeHtml(code.trimEnd())}</code></pre>`)
    return `\x00BLOCK${idx}\x00`
  })

  // 处理标题
  text = text.replace(/^#### (.+)$/gm, '<h4>$1</h4>')
  text = text.replace(/^### (.+)$/gm, '<h3>$1</h3>')
  text = text.replace(/^## (.+)$/gm, '<h2>$1</h2>')
  text = text.replace(/^# (.+)$/gm, '<h1>$1</h1>')

  // 处理粗体和斜体
  text = text.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/\*(.+?)\*/g, '<em>$1</em>')

  // 处理行内代码
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>')

  // 处理链接
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>')

  // 按双换行分割段落
  const paragraphs = text.split(/\n\n+/)
  const result = []

  for (const para of paragraphs) {
    const trimmed = para.trim()
    if (!trimmed) continue

    // 已经是 HTML 块元素（h1-h4, pre）则直接保留
    if (/^<(h[1-4]|pre|ul|ol|table|div)/.test(trimmed)) {
      result.push(trimmed)
      continue
    }

    // 处理无序列表
    if (/^[-*+]\s/.test(trimmed)) {
      const items = trimmed.split(/\n/).filter((l) => /^[-*+]\s/.test(l.trim()))
      const lis = items.map((l) => `<li>${l.replace(/^[-*+]\s+/, '')}</li>`).join('')
      result.push(`<ul>${lis}</ul>`)
      continue
    }

    // 处理有序列表
    if (/^\d+[.)]\s/.test(trimmed)) {
      const items = trimmed.split(/\n/).filter((l) => /^\d+[.)]\s/.test(l.trim()))
      const lis = items.map((l) => `<li>${l.replace(/^\d+[.)]\s+/, '')}</li>`).join('')
      result.push(`<ol>${lis}</ol>`)
      continue
    }

    // 普通段落
    result.push(`<p>${trimmed.replace(/\n/g, '<br>')}</p>`)
  }

  text = result.join('\n')

  // 恢复代码块
  text = text.replace(/\x00BLOCK(\d+)\x00/g, (_, idx) => blocks[Number(idx)])

  return text
}

function workpapersPage() {
  return `<section class="page" id="page-workpapers"><div class="page-header"><div><h1>工作底稿</h1><p class="subtle">自动生成审计目标、程序、结果、结论与附件清单</p></div></div><div class="card"><div class="tabs"><button class="active">审计目标</button><button>审计程序</button><button>测试结果</button><button>审计结论</button></div><h2>资金流水专项核查工作底稿</h2><p class="subtle" style="margin-top:10px;">本底稿基于银行流水、总账明细和 Agent 执行证据链生成。</p><pre id="workpaper-preview" style="margin-top:12px;">等待选择右侧生成文件预览。</pre></div></section>`
}

function reportsPage() {
  return `<section class="page" id="page-reports"><div class="page-header"><div><h1>分析报告</h1><p class="subtle">审计报告、管理建议书、内控评价和风险分析</p></div></div><div class="report-list"><div class="card"><h2>风险分析报告</h2><p class="subtle" style="margin-top:8px;">汇总异常交易、控制缺陷和高风险事项。</p></div><div class="card"><h2>管理建议书</h2><p class="subtle" style="margin-top:8px;">将审计发现转化为管理层可执行建议。</p></div></div></section>`
}

function settingsPage() {
  return `<section class="page" id="page-settings">
    <div class="page-header">
      <div>
        <h1>设置</h1>
        <p class="subtle">LLM 模型配置与偏好</p>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <h2>LLM 模型配置</h2>
        <div style="display:flex;align-items:center;gap:12px;">
          <span id="settings-config-path" class="subtle" style="font-size:11px;"></span>
          <button id="settings-save-providers" class="primary" style="padding:4px 16px;">保存配置</button>
        </div>
      </div>
      <div id="settings-provider-status" class="subtle" style="margin-bottom:8px;"></div>
      <div id="settings-provider-list">
        <p class="subtle">加载中...</p>
      </div>
    </div>
  </section>`
}

export function renderSettingsProviders() {
  const container = $('#settings-provider-list')
  const statusEl = $('#settings-provider-status')
  const pathEl = $('#settings-config-path')
  if (!container) return

  if (pathEl) pathEl.textContent = state.llmConfigPath

  if (!state.llmProviders.length) {
    container.innerHTML = '<p class="subtle">未找到 LLM 配置。</p>'
    return
  }

  const sourceLabels = {
    'direct': '直接配置',
    'env': '环境变量',
    'literal': '字面量 (api_key_env)',
    'fallback': 'OPENAI_API_KEY 回退',
    'none': '未设置',
  }

  container.innerHTML = state.llmProviders.map((p) => {
    const isDefault = p.name === state.llmConfigDefault
    const defaultBadge = isDefault ? '<span class="badge low" style="margin-left:8px;">默认</span>' : ''
    const envHint = p.api_key_env ? ` (${escapeHtml(p.api_key_env)})` : ''
    const sourceText = (sourceLabels[p.api_key_source] || p.api_key_source) + envHint

    return `<div class="settings-provider-card" data-provider-name="${escapeHtml(p.name)}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <strong>${escapeHtml(p.name)}${defaultBadge}</strong>
          <button class="settings-delete-provider" data-provider="${escapeHtml(p.name)}" title="删除此 Provider">删除</button>
        </div>
        <span class="subtle" style="font-size:11px;">密钥来源: ${escapeHtml(sourceText)}</span>
      </div>
      <div class="grid" style="grid-template-columns:1.2fr 1fr 0.8fr;gap:10px;">
        <label style="font-size:12px;">API Key
          <div style="display:flex;gap:4px;">
            <input class="search settings-api-key-input"
                   data-provider="${escapeHtml(p.name)}"
                   data-original-masked="${escapeHtml(p.api_key_masked || '')}"
                   type="password"
                   value="${escapeHtml(p.api_key_masked || '')}"
                   placeholder="输入 API Key"
                   style="flex:1;font-family:monospace;letter-spacing:1px;" />
            <button class="settings-eye-toggle"
                    data-provider="${escapeHtml(p.name)}"
                    data-revealed="false"
                    title="显示/隐藏">&#128065;</button>
          </div>
        </label>
        <label style="font-size:12px;">Base URL
          <input class="search settings-base-url-input"
                 data-provider="${escapeHtml(p.name)}"
                 value="${escapeHtml(p.base_url || '')}"
                 placeholder="https://api.example.com/v1" />
        </label>
        <label style="font-size:12px;">Model
          <input class="search settings-model-input"
                 data-provider="${escapeHtml(p.name)}"
                 value="${escapeHtml(p.model || '')}"
                 placeholder="model-name" />
        </label>
      </div>
    </div>`
  }).join('') + `
    <div style="margin-top:12px;text-align:center;">
      <button id="settings-add-provider" class="ghost" style="padding:8px 24px;border-style:dashed;">+ 添加 Provider</button>
    </div>`
}

// ============================================================
// 新工作流 UI 函数：Detect → Confirm → Check
// ============================================================

/**
 * Step 1 - Detect 结果渲染：在聊天区域展示 LLM 识别结果
 */
export function renderDetectResult(result) {
  if (!result || !result.task_config) return

  const chat = $('#chat')
  if (!chat) return

  const identifications = result.identifications || []
  const bankItems = identifications.filter((i) => i.type === 'bank_statement')
  const ledgerItems = identifications.filter((i) => i.type === 'ledger')

  const idTable = (items, title) => {
    if (items.length === 0) return `<div class="subtle">未识别到${title}</div>`
    return `<div style="margin-top:6px;">
      <div class="label" style="margin-bottom:4px;">${escapeHtml(title)} (${items.length} 个)</div>
      <table class="table" style="font-size:12px;">
        <thead><tr><th>ID</th><th>文件名</th><th>银行/来源</th><th>银行账号</th><th>工作表</th><th>时间段</th><th>解析器</th><th>置信度</th></tr></thead>
        <tbody>${items.map((i) => {
          const missingFields = []
          if (!i.bank_name) missingFields.push('银行名称')
          if (!i.account_no) missingFields.push('银行账号')
          const hasIssue = missingFields.length > 0
          const rowStyle = hasIssue ? 'background:#fff3cd;' : ''
          const bankCell = i.bank_name
            ? escapeHtml(i.bank_name)
            : '<span style="color:#d9534f;font-weight:bold;">⚠ 未识别</span>'
          const acctCell = i.account_no
            ? escapeHtml(i.account_no)
            : '<span style="color:#d9534f;font-weight:bold;">⚠ 未识别</span>'
          const sheetCell = i.sheet ? escapeHtml(i.sheet) : '<span class="subtle">-</span>'
          const warnTitle = hasIssue ? ` title="缺失字段: ${missingFields.join(', ')}"` : ''
          return `<tr style="${rowStyle}"${warnTitle}>
          <td><strong>${escapeHtml(i.id)}</strong></td>
          <td>${escapeHtml(i.name || '')}</td>
          <td>${bankCell}</td>
          <td>${acctCell}</td>
          <td>${sheetCell}</td>
          <td>${escapeHtml(i.date_from || '')} ~ ${escapeHtml(i.date_to || '')}</td>
          <td><span class="badge">${escapeHtml(i.parser || '')}</span></td>
          <td>${((i.confidence || 0) * 100).toFixed(0)}%</td>
        </tr>`
        }).join('')}</tbody>
      </table>
    </div>`
  }

  // 构建警告横幅 HTML
  const warningsHtml = (result.warnings && result.warnings.length > 0)
    ? `<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:10px 14px;margin:8px 0;font-size:13px;">
        <strong style="color:#856404;">⚠️ 检测警告 (${result.warnings.length})</strong>
        <ul style="margin:6px 0 0 0;padding-left:20px;color:#856404;">
          ${result.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}
        </ul>
      </div>`
    : ''

  const block = document.createElement('div')
  block.className = 'message'
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })

  // 从 result 中提取 token 信息
  let tokenInfo = ''
  if (result.usage) {
    const u = result.usage
    const total = u.total_tokens || 0
    if (total > 0) {
      tokenInfo = ` | 📊 Tokens: ${total}`
    }
  }

  let llmStatusHtml = ''
  if (result.llm_used && result.llm_error) {
    llmStatusHtml = `<p style="color:#f0ad4e;">⚠️ LLM 调用失败，已回退到本地关键词识别。</p>
      <details style="margin:8px 0;font-size:12px;">
        <summary style="cursor:pointer;color:var(--accent);">查看 LLM 错误详情</summary>
        <pre style="background:var(--bg);padding:8px;border-radius:4px;margin-top:4px;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;font-size:11px;color:#d9534f;">${escapeHtml(result.llm_error.message || '')}
${escapeHtml(result.llm_error.traceback || '')}</pre>
      </details>`
  } else if (result.llm_used) {
    llmStatusHtml = `<p>LLM 识别: <strong style="color:#5cb85c;">✅ 已启用</strong></p>`
  } else {
    llmStatusHtml = `<p>识别方式: <strong>📜 脚本（关键词）</strong></p>`
  }

  block.innerHTML = `
    <div class="message-head"><span>Agent · 文件识别结果</span><span>${escapeHtml(time)}${tokenInfo} <span class="message-collapse-btn">▼</span></span></div>
    <div class="message-body">
    <p>扫描到 <strong>${result.files_count}</strong> 个文件。</p>
    ${llmStatusHtml}
    ${warningsHtml}
    ${idTable(bankItems, '银行流水')}
    ${idTable(ledgerItems, '序时账')}
    <p style="margin-top:8px;" class="subtle">task.yml 已自动生成 → 进入下一步「确认配置」进行审核。</p>
    </div>
  `
  chat.appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })
}

/**
 * 清洗出库表 - 渲染每个工作表的任务状态表
 * @param {object} result - API 响应，包含 sheet_tasks 数组
 * @param {function} onRetry - 重试回调：(file, sheet, sheetType, colSig) => result
 */
export function renderSheetTasks(result, onRetry) {
  const tasks = result && result.sheet_tasks
  if (!tasks || tasks.length === 0) return

  const chat = $('#chat')
  if (!chat) return

  const taskList = tasks.map(t => ({ ...t }))
  let accumulatedUsage = result.usage ? { ...result.usage } : { total_tokens: 0, prompt_tokens: 0, completion_tokens: 0 }

  const statusBadge = (status) => {
    const map = {
      llm_success: { label: 'LLM 成功', cls: 'badge low' },
      llm_fallback: { label: '回退硬编码', cls: 'badge medium' },
      failed: { label: '失败', cls: 'badge high' },
      skipped: { label: '跳过', cls: 'badge' },
    }
    const info = map[status] || { label: status, cls: 'badge' }
    return `<span class="${info.cls}">${escapeHtml(info.label)}</span>`
  }

  const typeLabels = { sellout: '出库', refund: '仅退款', return: '货损', transfer: '退回保税仓' }

  const renderRows = () => taskList.map((t, idx) => {
    const script = t.script_name ? `<code style="font-size:11px;">${escapeHtml(t.script_name)}</code>` : '<span class="subtle">硬编码</span>'
    const canRetry = t.status === 'llm_fallback' || t.status === 'failed'
    const retryBtn = canRetry
      ? `<button class="sheet-retry-btn" data-idx="${idx}" data-file="${escapeHtml(t.file)}" data-sheet="${escapeHtml(t.sheet)}" data-type="${escapeHtml(t.type)}" data-colsig="${escapeHtml(t.column_signature || '')}" style="font-size:11px;padding:2px 8px;border-radius:4px;border:1px solid var(--warning);color:var(--warning);background:transparent;cursor:pointer;">重试</button>`
      : ''
    const errorHtml = t.error ? `<details style="font-size:11px;margin-top:2px;"><summary style="cursor:pointer;color:var(--danger);">错误详情</summary><pre style="white-space:pre-wrap;word-break:break-all;font-size:10px;max-height:100px;overflow-y:auto;margin-top:2px;">${escapeHtml(t.error)}</pre></details>` : ''
    return `<tr><td style="font-size:12px;">${escapeHtml(t.file)}</td><td style="font-size:12px;"><strong>${escapeHtml(t.sheet)}</strong></td><td style="font-size:12px;">${escapeHtml(typeLabels[t.type] || t.type)}</td><td>${statusBadge(t.status)}${errorHtml}</td><td>${script}</td><td style="text-align:right;font-size:12px;">${t.rows > 0 ? t.rows.toLocaleString() : '-'}</td><td>${retryBtn}</td></tr>`
  }).join('')

  const renderSummary = () => {
    const s = taskList.filter(t => t.status === 'llm_success').length
    const f = taskList.filter(t => t.status === 'llm_fallback').length
    const d = taskList.filter(t => t.status === 'failed').length
    const totalRows = taskList.reduce((sum, t) => sum + (t.rows || 0), 0)
    let token = ''
    if (accumulatedUsage.total_tokens > 0) {
      token = `<span style="margin-left:8px;color:var(--text-soft);font-size:12px;">| Token 累计：${accumulatedUsage.total_tokens}（输入 ${accumulatedUsage.prompt_tokens || 0}，输出 ${accumulatedUsage.completion_tokens || 0}）</span>`
    }
    return `共 <strong>${taskList.length}</strong> 个工作表：LLM 成功 <strong style="color:var(--success);">${s}</strong>${f > 0 ? `，回退 <strong style="color:var(--warning);">${f}</strong>` : ''}${d > 0 ? `，失败 <strong style="color:var(--danger);">${d}</strong>` : ''}，合计 <strong>${totalRows.toLocaleString()}</strong> 条记录。${token}`
  }

  const block = document.createElement('div')
  block.className = 'message'
  block.id = 'sheet-tasks-panel'
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  block.innerHTML = `
    <div class="message-head"><span>Agent · 工作表清洗详情</span><span>${time} <span class="message-collapse-btn">▼</span></span></div>
    <div class="message-body">
      <p id="sheet-tasks-summary" style="margin-bottom:8px;">${renderSummary()}</p>
      <div style="overflow-x:auto;"><table class="table" style="font-size:12px;"><thead><tr><th>文件</th><th>工作表</th><th>类型</th><th>状态</th><th>脚本</th><th style="text-align:right;">行数</th><th>操作</th></tr></thead><tbody>${renderRows()}</tbody></table></div>
    </div>`
  chat.appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })

  if (onRetry) {
    block.querySelectorAll('.sheet-retry-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const idx = parseInt(btn.dataset.idx, 10)
        btn.disabled = true
        btn.textContent = '重试中...'
        btn.style.borderColor = 'var(--text-muted)'
        btn.style.color = 'var(--text-muted)'
        try {
          const res = await onRetry(btn.dataset.file, btn.dataset.sheet, btn.dataset.type, btn.dataset.colsig)
          if (res && res.status === 'llm_success') {
            btn.textContent = '成功'
            btn.style.borderColor = 'var(--success)'
            btn.style.color = 'var(--success)'
            taskList[idx].status = 'llm_success'
            taskList[idx].script_name = res.script_name || null
            taskList[idx].rows = res.rows || 0
            taskList[idx].error = null
            if (res.total_usage) { accumulatedUsage = { ...res.total_usage } }
            else if (res.usage) { accumulatedUsage.total_tokens += res.usage.total_tokens || 0; accumulatedUsage.prompt_tokens += res.usage.prompt_tokens || 0; accumulatedUsage.completion_tokens += res.usage.completion_tokens || 0 }
            const tr = btn.closest('tr')
            if (tr) { const c = tr.querySelectorAll('td'); c[3].innerHTML = statusBadge('llm_success'); c[4].innerHTML = `<code style="font-size:11px;">${escapeHtml(res.script_name || '')}</code>`; c[5].textContent = res.rows > 0 ? res.rows.toLocaleString() : '-'; c[6].innerHTML = '' }
            const s = block.querySelector('#sheet-tasks-summary')
            if (s) s.innerHTML = renderSummary()
          } else {
            btn.textContent = '重试'; btn.disabled = false; btn.style.borderColor = 'var(--warning)'; btn.style.color = 'var(--warning)'
            const msg = res && res.error ? res.error : '未知错误'
            if (res && res.error) taskList[idx].error = res.error
            const tr = btn.closest('tr')
            if (tr) { const c = tr.querySelectorAll('td'); if (!c[3].querySelector('details')) c[3].insertAdjacentHTML('beforeend', `<details style="font-size:11px;margin-top:2px;"><summary style="cursor:pointer;color:var(--danger);">错误详情</summary><pre style="white-space:pre-wrap;word-break:break-all;font-size:10px;max-height:100px;overflow-y:auto;margin-top:2px;">${escapeHtml(msg)}</pre></details>`) }
          }
        } catch (err) { btn.textContent = '重试'; btn.disabled = false; btn.style.borderColor = 'var(--danger)'; btn.style.color = 'var(--danger)' }
      })
    })
  }
}

/**
 * Step 2 - Config Confirm: 展示可编辑的 YAML 配置面板
 */
export function renderConfigConfirm(yamlContent, customerName, taskName) {
  const container = $('#config-editor-content')
  if (!container) return

  // 如果配置区在折叠面板内，自动展开
  const section = container.closest('.panel-section.collapsed')
  if (section) section.classList.remove('collapsed')

  container.innerHTML = `
    <div style="margin-bottom:8px;display:flex;gap:8px;align-items:center;">
      <strong style="font-size:13px;">📝 outputs/${escapeHtml(customerName)}/${escapeHtml(taskName)}/task.yml</strong>
    </div>
    <div style="margin-bottom:8px;display:flex;gap:8px;">
      <button id="save-config-btn" class="primary" style="padding:4px 12px;font-size:13px;">💾 保存配置</button>
      <button id="reload-config-btn" style="padding:4px 12px;font-size:13px;">⟳ 重新加载</button>
      <span id="config-save-status" class="subtle" style="line-height:28px;"></span>
    </div>
    <textarea id="config-yml-editor" style="
      width:100%;height:380px;font-family:'Consolas','Courier New',monospace;
      font-size:13px;line-height:1.5;padding:10px;border:1px solid var(--border);
      border-radius:6px;background:var(--bg);color:var(--text);
      resize:vertical;white-space:pre;tab-size:2;
    " spellcheck="false">${escapeHtml(yamlContent)}</textarea>
  `

  // 绑定保存按钮
  document.getElementById('save-config-btn')?.addEventListener('click', async () => {
    const editor = document.getElementById('config-yml-editor')
    const statusEl = document.getElementById('config-save-status')
    if (!editor || !statusEl) return
    const content = editor.value
    statusEl.textContent = '保存中...'
    statusEl.className = 'subtle'
    try {
      // 动态导入 actions 中的 saveConfig
      const { saveConfig } = await import('./actions.js')
      await saveConfig(customerName, taskName, content)
      statusEl.textContent = '✓ 已保存'
      statusEl.className = 'badge'
    } catch (err) {
      statusEl.textContent = '✗ 保存失败: ' + err.message
      statusEl.className = 'badge high'
    }
  })

  // 绑定重新加载按钮
  document.getElementById('reload-config-btn')?.addEventListener('click', async () => {
    const statusEl = document.getElementById('config-save-status')
    if (statusEl) {
      statusEl.textContent = '重新加载中...'
      statusEl.className = 'subtle'
    }
    try {
      const { api } = await import('./api.js')
      const res = await api.workflowGetConfig(customerName, taskName)
      if (res.ok) {
        const editor = document.getElementById('config-yml-editor')
        if (editor) editor.value = res.content
        if (statusEl) { statusEl.textContent = '✓ 已重新加载'; statusEl.className = 'badge' }
      } else {
        if (statusEl) { statusEl.textContent = '✗ ' + (res.error || '加载失败'); statusEl.className = 'badge high' }
      }
    } catch (err) {
      if (statusEl) { statusEl.textContent = '✗ ' + err.message; statusEl.className = 'badge high' }
    }
  })
}

/**
 * Step 4 - Check Result: 展示月度流量核查报告
 */
export function renderCheckResult(result) {
  if (!result) return

  const chat = $('#chat')
  if (!chat) return

  const summary = result.summary || {}
  const mismatches = result.mismatches || []
  const allRows = result.rows || []

  let mismatchTable = ''
  if (mismatches.length > 0) {
    mismatchTable = `<div style="margin-top:8px;">
      <div class="label" style="margin-bottom:4px;color:#e74c3c;">⚠️ 差异明细 (${mismatches.length} 项)</div>
      <div style="max-height:300px;overflow-y:auto;">
        <table class="table" style="font-size:12px;">
          <thead><tr><th>账号</th><th>月份</th><th>方向</th><th>银行笔数</th><th>银行金额</th><th>账务笔数</th><th>账务金额</th><th>差异</th><th>状态</th></tr></thead>
          <tbody>${mismatches.map((r) => `<tr class="${r.status === 'mismatch' ? 'row-warn' : ''}">
            <td>${escapeHtml(r.account_no || '')}</td>
            <td>${escapeHtml(r.month || '')}</td>
            <td>${escapeHtml(r.flow || '')}</td>
            <td>${escapeHtml(r.bank_count || '')}</td>
            <td>${escapeHtml(r.bank_total || '')}</td>
            <td>${escapeHtml(r.ledger_count || '')}</td>
            <td>${escapeHtml(r.ledger_total || '')}</td>
            <td><strong style="color:#e74c3c;">${escapeHtml(r.amount_diff || '')}</strong></td>
            <td><span class="badge high">${escapeHtml(r.status || 'mismatch')}</span></td>
          </tr>`).join('')}</tbody>
        </table>
      </div>
    </div>`
  }

  const block = document.createElement('div')
  block.className = 'message'
  const time = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  block.innerHTML = `
    <div class="message-head"><span>Agent · 数据完备性检查</span><span>${escapeHtml(time)} <span class="message-collapse-btn">▼</span></span></div>
    <div class="message-body">
    <div class="grid metrics" style="margin-bottom:8px;">
      <div class="card" style="text-align:center;"><div class="card-title">总计</div><div class="card-value">${summary.total_rows || 0}</div></div>
      <div class="card" style="text-align:center;"><div class="card-title">✓ 一致</div><div class="card-value" style="color:#27ae60;">${summary.ok_count || 0}</div></div>
      <div class="card" style="text-align:center;"><div class="card-title">✗ 不一致</div><div class="card-value" style="color:#e74c3c;">${summary.mismatch_count || 0}</div></div>
    </div>
    ${result.all_ok
      ? '<p style="color:#27ae60;font-weight:bold;">✅ 所有月份/账户的银行流水与序时账流入流出一致，数据完备！</p>'
      : '<p style="color:#e74c3c;font-weight:bold;">⚠️ 存在不一致项，请检查原始数据是否有遗漏或错误。</p>'}
    ${mismatchTable}
    </div>
  `
  chat.appendChild(block)
  block.scrollIntoView({ behavior: 'smooth', block: 'end' })
}
