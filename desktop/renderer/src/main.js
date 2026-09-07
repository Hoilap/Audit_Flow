import { workflowTasks } from './config.js'
import { findWorkflowTaskByName, state, taskRunKey } from './state.js'
import { $, $$ } from './dom.js'
import { renderEvidencePanel, renderShell, renderWorkflowWorkspace, setAgentStatus, showPage } from './ui.js'
import { cancelRunningStep, commitAll, createProject, deleteProject, loadProjects, loadProcedures, loadTaskDefinitions, loadProgramReadmes, loadDashboardStats, previewFile, refreshFiles, refreshLog, refreshTokens, runAllSteps, runNextStep, runStep, selectProject, setupEventSource, toggleCustomMode, updateCustomCustomerName, updateCustomTaskName, updateDetectMethod, updateMatchMethod, updateProject, uploadFile, syncLlmConfig, updateLlmModel, loadSettingsProviders, saveLlmProviders, revealProviderKey, addLlmProvider, deleteLlmProvider, bindDataImportZone, validateDataDirs } from './actions.js'
import { openReviewEditor } from './reviewEditor.js'
import { renderProjectsTable, showProjectFormModal } from './ui.js'

function bindEvents() {
  document.addEventListener('click', async (event) => {
    const navButton = event.target.closest('[data-page]')
    if (navButton) {
      showPage(navButton.dataset.page)
      // 切换到 Dashboard 页面时刷新统计
      if (navButton.dataset.page === 'dashboard') {
        await loadDashboardStats()
      }
      // 切换到项目管理页面时刷新表格
      if (navButton.dataset.page === 'projects') {
        await loadProjects()
        refreshProjectTable()
      }
      // 切换到数据源页面时重新扫描目录规范
      if (navButton.dataset.page === 'data') {
        validateDataDirs()
      }
      // 切换到 Agent 工作流页面时刷新项目选择器
      if (navButton.dataset.page === 'agent') {
        await loadProjects()
        await loadTaskDefinitions()
        await renderWorkflowWorkspace()
      }
      // 切换到审计程序页面时加载 README 文档
      if (navButton.dataset.page === 'programs') {
        await loadProgramReadmes()
      }
      // 切换到 Agent 对话页面时加载会话列表、文件树和输出路径选择器
      if (navButton.dataset.page === 'agent-loop') {
        const { loadAgentConversations, renderAgentFileTree, initAgentOutputSelector, bindAgentOutputSelectorEvents } = await import('./agentActions.js')
        await loadAgentConversations()
        await renderAgentFileTree()
        await initAgentOutputSelector()
        bindAgentOutputSelectorEvents()
      }
      // 切换到设置页面时加载 LLM provider 配置
      if (navButton.dataset.page === 'settings') {
        await loadSettingsProviders()
      }
    }

    const stepCard = event.target.closest('[data-step-index]')
    if (stepCard) {
      state.activeStepIndex = Number(stepCard.dataset.stepIndex)
      await renderWorkflowWorkspace()
    }

    // 步骤卡独立运行按钮
    const runStepBtn = event.target.closest('[data-run-step]')
    if (runStepBtn) {
      event.stopPropagation()
      const idx = Number(runStepBtn.dataset.runStep)
      state.activeStepIndex = idx
      runStepBtn.disabled = true
      try { await runStep(idx) } finally { runStepBtn.disabled = false }
    }

    const fileRow = event.target.closest('[data-file]')
    if (fileRow) previewFile(fileRow.dataset.file)

    // 项目文件树中的文件点击预览
    const projectFile = event.target.closest('#project-filetree .tree-file')
    if (projectFile && projectFile.dataset.path) {
      previewFile(projectFile.dataset.path)
    }

    // 消息/README卡片折叠切换（箭头指示由 CSS rotate 处理）
    const collapseHead = event.target.closest('.message-head, .readme-card-head')
    if (collapseHead) {
      const container = collapseHead.closest('.message') || collapseHead.closest('.readme-card')
      if (container) container.classList.toggle('collapsed')
    }

    // 消息区：全部折叠 / 全部展开（仅作用于含 message-body 的块，箭头由 CSS rotate 处理）
    if (event.target.id === 'chat-collapse-all' || event.target.id === 'chat-expand-all') {
      const expand = event.target.id === 'chat-expand-all'
      const chat = $('#chat')
      if (chat) {
        chat.querySelectorAll('.message').forEach((msg) => {
          if (!msg.querySelector('.message-body')) return
          msg.classList.toggle('collapsed', !expand)
        })
      }
    }

    // 右栏区块折叠/展开
    const sectionToggle = event.target.closest('[data-toggle-section]')
    if (sectionToggle) {
      const sectionId = sectionToggle.dataset.toggleSection
      state.collapsedSections[sectionId] = !state.collapsedSections[sectionId]
      renderEvidencePanel()
      // 展开 git-log 时自动刷新内容
      if (sectionId === 'git-log' && !state.collapsedSections[sectionId]) {
        refreshLog()
      }
    }

    // 设置页：保存 LLM provider 配置
    if (event.target.id === 'settings-save-providers') {
      await saveLlmProviders()
    }

    // 设置页：添加 Provider
    if (event.target.id === 'settings-add-provider') {
      addLlmProvider()
    }

    // 设置页：删除 Provider
    const deleteProviderBtn = event.target.closest('.settings-delete-provider')
    if (deleteProviderBtn) {
      await deleteLlmProvider(deleteProviderBtn.dataset.provider)
    }

    // 设置页：眼睛切换 API Key 明文/遮罩
    const eyeToggle = event.target.closest('.settings-eye-toggle')
    if (eyeToggle) {
      const providerName = eyeToggle.dataset.provider
      const revealed = eyeToggle.dataset.revealed === 'true'
      const card = eyeToggle.closest('.settings-provider-card')
      const keyInput = card?.querySelector('.settings-api-key-input')

      if (!revealed) {
        const actualKey = await revealProviderKey(providerName)
        if (keyInput) {
          keyInput.value = actualKey
          keyInput.type = 'text'
        }
        eyeToggle.dataset.revealed = 'true'
        eyeToggle.textContent = '\uD83D\uDD12'
      } else {
        if (keyInput) keyInput.type = 'password'
        eyeToggle.dataset.revealed = 'false'
        eyeToggle.textContent = '\uD83D\uDC41'
      }
    }
  })

  // 项目选择器事件（通过 DOM 事件代理，因为这些元素在 renderWorkflowWorkspace 中动态生成）
  document.addEventListener('change', (event) => {
    // 选择已有项目
    if (event.target.id === 'project-select') {
      selectProject(event.target.value)
    }
    // 自定义模式 checkbox
    if (event.target.id === 'project-custom-check') {
      toggleCustomMode(event.target.checked)
    }
    // 自定义任务名称下拉
    if (event.target.id === 'project-task-select') {
      updateCustomTaskName(event.target.value)
    }
    // Detect 识别方式 radio（同时作为解析器选择）
    if (event.target.name === 'detect-method') {
      updateDetectMethod(event.target.value)
    }
    // Match 步骤解析器 radio
    if (event.target.name === 'match-method') {
      updateMatchMethod(event.target.value)
    }
  })

  // 客户名称实时更新
  document.addEventListener('input', (event) => {
    if (event.target.id === 'project-customer-input') {
      updateCustomCustomerName(event.target.value)
    }
    // 附加需求 textarea 实时保存
    if (event.target.id === 'step-requirement') {
      state.stepRequirements[taskRunKey()] = event.target.value
    }
  })

  $('#theme-toggle').addEventListener('click', () => document.body.classList.toggle('dark'))
  $('#refresh-all').addEventListener('click', refreshAll)
  $('#refresh-files').addEventListener('click', refreshFiles)
  $('#refresh-log').addEventListener('click', refreshLog)
  // 复核文件列表：点击"打开复核"按钮
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.open-review-btn')
    if (btn) {
      const filePath = btn.dataset.file
      if (filePath) openReviewEditor(filePath)
    }
  })
  $('#run-next-step').addEventListener('click', () => withDisabled('#run-next-step', runNextStep))
  $('#run-all-steps').addEventListener('click', () => withDisabled('#run-all-steps', runAllSteps))
  $('#stop-task').addEventListener('click', cancelRunningStep)
  $('#upload-btn').addEventListener('click', () => withDisabled('#upload-btn', uploadFile))
  $('#open-inputs-dir').addEventListener('click', () => openDataDir('inputs'))
  $('#open-outputs-dir').addEventListener('click', () => openDataDir('outputs'))
  $('#commit-all').addEventListener('click', () => withDisabled('#commit-all', commitAll))

  // ────────── Agent 对话面板事件 ──────────
  document.addEventListener('click', async (e) => {
    if (e.target.id === 'agent-send') {
      const { sendAgentMessage } = await import('./agentActions.js')
      await sendAgentMessage()
    }
    if (e.target.id === 'agent-new-conv') {
      const { newAgentConversation } = await import('./agentActions.js')
      await newAgentConversation()
    }
    if (e.target.id === 'agent-delete-conv') {
      const { deleteAgentConversation } = await import('./agentActions.js')
      await deleteAgentConversation()
    }
    if (e.target.id === 'agent-refresh-filetree' || e.target.closest('#agent-refresh-filetree')) {
      const { renderAgentFileTree } = await import('./agentActions.js')
      await renderAgentFileTree()
    }
  })
  document.addEventListener('keydown', async (e) => {
    if (e.target.id === 'agent-prompt' && e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      const { sendAgentMessage } = await import('./agentActions.js')
      await sendAgentMessage()
    }
  })
  document.addEventListener('change', async (e) => {
    if (e.target.id === 'agent-conv-select' && e.target.value) {
      const { loadAgentConversation } = await import('./agentActions.js')
      await loadAgentConversation(e.target.value)
    }
  })

  $('#model-select').addEventListener('change', (event) => {
    updateLlmModel(event.target.value)
  })

  // ────────── 项目管理页面事件（click 代理）──────────
  document.addEventListener('click', async (event) => {
    // 新建项目按钮
    if (event.target.id === 'new-project-btn') {
      await openProjectForm(null)
    }
    // 编辑按钮
    const editBtn = event.target.closest('.edit-project-btn')
    if (editBtn) {
      const id = Number(editBtn.dataset.projectId)
      const project = state.projects.find((p) => p.id === id)
      if (project) await openProjectForm(project)
    }
    // 删除按钮
    const deleteBtn = event.target.closest('.delete-project-btn')
    if (deleteBtn) {
      const id = Number(deleteBtn.dataset.projectId)
      if (confirm('确定要删除该项目吗？')) {
        await deleteProject(id)
        await refreshProjectTable()
      }
    }
  })

  // 搜索
  document.addEventListener('input', (event) => {
    if (event.target.id === 'project-search') refreshProjectTable()
  })
}

async function withDisabled(selector, action) {
  const button = $(selector)
  button.disabled = true
  try {
    await action()
  } finally {
    button.disabled = false
  }
}

async function refreshAll() {
  await Promise.allSettled([refreshFiles(), refreshLog(), syncLlmConfig(), refreshTokens()])
}

async function refreshProjectTable() {
  const search = ($('#project-search')?.value || '').toLowerCase()
  let filtered = state.projects || []
  if (search) {
    filtered = filtered.filter((p) =>
      (p.customer_name || '').toLowerCase().includes(search) ||
      (p.customer_short_name || '').toLowerCase().includes(search) ||
      (p.project_code || '').toLowerCase().includes(search) ||
      (p.project_name || '').toLowerCase().includes(search) ||
      (p.prepared_by || '').toLowerCase().includes(search) ||
      (p.reviewed_by || '').toLowerCase().includes(search)
    )
  }
  renderProjectsTable(filtered)
}

async function openProjectForm(project) {
  await showProjectFormModal(project, async (payload) => {
    if (project) {
      await updateProject(project.id, payload)
    } else {
      await createProject(payload)
    }
    await refreshProjectTable()
    // 同步回 workflow 页面状态
    await loadProjects()
  }, async (id) => {
    await deleteProject(id)
    await refreshProjectTable()
    if (state.activeProjectId === id) {
      state.activeProjectId = null
      await renderWorkflowWorkspace()
    }
  })
}

// 首次运行装依赖时显示全屏遮罩，避免窗口看似卡死。
// 主进程广播 dependency-install-progress 事件；渲染进程加载晚于主进程时，
// 通过 getInstallStatus 主动查询，防止漏掉已开始的安装状态。
function setupInstallOverlay() {
  const overlay = $('#install-overlay')
  if (!overlay || !window.electronAPI) return
  const show = () => { overlay.style.display = 'flex' }
  const hide = () => { overlay.style.display = 'none' }
  window.electronAPI.onDependencyProgress((data) => {
    if (data && data.state === 'installing') show()
    else hide()
  })
  window.electronAPI.getInstallStatus()
    .then((status) => { if (status && status.state === 'installing') show() })
    .catch(() => {})
}

// ────────── 左右栏拖拽调宽 ──────────
// 列宽由 CSS 变量 --sidebar-w / --evidence-w 控制（把手 5px）。
// 拖拽时指针捕获并禁用网格过渡，松手写入 localStorage；
// 双击把手恢复默认宽度。
const COLUMN_CONFIG = {
  left:  { min: 0, max: 420, cssVar: '--sidebar-w',  storageKey: 'ui.sidebarWidth',  defaultW: 260 },
  right: { min: 0, max: 640, cssVar: '--evidence-w', storageKey: 'ui.evidenceWidth', defaultW: 380 },
}

// 窄栏模式：宽度低于阈值时切换为图标模式（文字标签平滑收起，只保留图标等）
const NARROW_THRESHOLD = 160
function applyNarrowState(side, width) {
  const el = side === 'left' ? $('#sidebar') : $('#evidence')
  if (el) el.classList.toggle('narrow', width < NARROW_THRESHOLD)
}

function setupResizeHandles() {
  const layout = $('#layout')
  const root = document.documentElement
  if (!layout || !root) return

  // 启动时恢复持久化宽度（钳制到合法范围，越界值回写修正而不是丢弃；0 宽度合法 = 完全收起）
  for (const side of ['left', 'right']) {
    const conf = COLUMN_CONFIG[side]
    const raw = localStorage.getItem(conf.storageKey)
    if (raw !== null) {
      const saved = Number(raw)
      if (Number.isFinite(saved) && saved >= 0) {
        const clamped = Math.min(conf.max, Math.max(0, saved))
        root.style.setProperty(conf.cssVar, `${clamped}px`)
        applyNarrowState(side, clamped)
        if (clamped !== saved) localStorage.setItem(conf.storageKey, String(Math.round(clamped)))
      }
    }
  }

  for (const side of ['left', 'right']) {
    const handle = $(`#handle-${side}`)
    if (!handle) continue
    const conf = COLUMN_CONFIG[side]
    // 左把手向右拖 = 左栏加宽；右把手向左拖 = 右栏加宽
    const sign = side === 'left' ? 1 : -1

    handle.addEventListener('pointerdown', (e) => {
      e.preventDefault()
      const startX = e.clientX
      const startW = parseFloat(getComputedStyle(root).getPropertyValue(conf.cssVar)) || conf.defaultW
      let newW = startW
      let tip = null
      layout.classList.add('resizing')
      handle.classList.add('active')
      handle.setPointerCapture(e.pointerId)
      document.body.style.cursor = 'col-resize'

      const onMove = (ev) => {
        newW = Math.min(conf.max, Math.max(conf.min, startW + (ev.clientX - startX) * sign))
        root.style.setProperty(conf.cssVar, `${newW}px`)
        applyNarrowState(side, newW)
        if (!tip) {
          tip = document.createElement('div')
          tip.className = 'resize-tip'
          document.body.appendChild(tip)
        }
        tip.textContent = `${Math.round(newW)}px`
        tip.style.left = `${ev.clientX}px`
        tip.style.top = `${ev.clientY + 16}px`
      }
      const onUp = () => {
        layout.classList.remove('resizing')
        handle.classList.remove('active')
        document.body.style.cursor = ''
        if (tip) { tip.remove(); tip = null }
        handle.removeEventListener('pointermove', onMove)
        handle.removeEventListener('pointerup', onUp)
        handle.removeEventListener('pointercancel', onUp)
        localStorage.setItem(conf.storageKey, String(Math.round(newW)))
      }
      handle.addEventListener('pointermove', onMove)
      handle.addEventListener('pointerup', onUp)
      handle.addEventListener('pointercancel', onUp)
    })

    // 双击复位默认宽度
    handle.addEventListener('dblclick', () => {
      root.style.setProperty(conf.cssVar, `${conf.defaultW}px`)
      localStorage.removeItem(conf.storageKey)
      applyNarrowState(side, conf.defaultW)
    })
  }
}

async function init() {
  setupInstallOverlay()
  await renderShell()
  bindEvents()
  setupResizeHandles()   // 左右栏拖拽调宽（把手 + 双击复位 + 持久化）
  bindDataImportZone()   // 数据源页拖拽导入框
  setupEventSource()   // 建立 SSE 连接，实时接收后端事件
  showPage(state.activePage)
  setAgentStatus('Idle', 0)
  $$('.page').forEach((page) => page.classList.toggle('active', page.id === `page-${state.activePage}`))
  // 加载项目数据
  await loadProjects()
  await loadProcedures()
  await loadTaskDefinitions()
  await loadDashboardStats()
  if (state.projects.length > 0) {
    state.activeProjectId = state.projects[0].id
    const p = state.projects[0]
    state.customCustomerName = p.customer_short_name
    state.customTaskName = p.task_name
    const wfTask = findWorkflowTaskByName(p.task_name)
    if (wfTask) {
      state.activeTaskId = wfTask.id
    } else {
      state.activeTaskId = '__custom__'
    }
    await renderWorkflowWorkspace()
  }
  
  // 初始化 LLM 配置和 token 显示
  await syncLlmConfig()
  refreshTokens()
  refreshAll()
}

init()
