import { workflowTasks } from './config.js'
import { findWorkflowTaskByName, state, taskRunKey } from './state.js'
import { $, $$ } from './dom.js'
import { renderEvidencePanel, renderShell, renderWorkflowWorkspace, setAgentStatus, showPage } from './ui.js'
import { cancelRunningStep, commitAll, createProject, deleteProject, loadProjects, loadTaskDefinitions, loadProgramReadmes, previewFile, refreshFiles, refreshLog, refreshTokens, runAllSteps, runNextStep, runStep, selectProject, setupEventSource, toggleCustomMode, updateCustomCustomerName, updateCustomTaskName, updateDetectMethod, updateProject, uploadFile, syncLlmConfig, updateLlmModel, loadSettingsProviders, saveLlmProviders, revealProviderKey, addLlmProvider, deleteLlmProvider } from './actions.js'
import { openReviewEditor } from './reviewEditor.js'
import { renderProjectsTable, showProjectFormModal } from './ui.js'

function bindEvents() {
  document.addEventListener('click', async (event) => {
    const navButton = event.target.closest('[data-page]')
    if (navButton) {
      showPage(navButton.dataset.page)
      // 切换到项目管理页面时刷新表格
      if (navButton.dataset.page === 'projects') {
        await loadProjects()
        refreshProjectTable()
      }
      // 切换到 Agent 工作流页面时刷新项目选择器
      if (navButton.dataset.page === 'agent') {
        await loadProjects()
        await loadTaskDefinitions()
        renderWorkflowWorkspace()
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
      renderWorkflowWorkspace()
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

    // 消息/README卡片折叠切换
    const collapseHead = event.target.closest('.message-head, .readme-card-head')
    if (collapseHead) {
      const container = collapseHead.closest('.message') || collapseHead.closest('.readme-card')
      if (container) container.classList.toggle('collapsed')
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

  // 左栏折叠
  $('#toggle-sidebar').addEventListener('click', () => {
    const sidebar = $('#sidebar')
    const layout = $('#layout')
    sidebar.classList.toggle('collapsed')
    layout.classList.toggle('sidebar-hidden')
    $('#toggle-sidebar').textContent = sidebar.classList.contains('collapsed') ? '▶' : '◀'
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

  // 搜索/过滤
  document.addEventListener('input', (event) => {
    if (event.target.id === 'project-search') refreshProjectTable()
  })
  document.addEventListener('change', (event) => {
    if (event.target.id === 'project-filter-status' || event.target.id === 'project-filter-risk') refreshProjectTable()
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
  await Promise.allSettled([refreshFiles(), refreshLog()])
}

async function refreshProjectTable() {
  const search = ($('#project-search')?.value || '').toLowerCase()
  const statusFilter = $('#project-filter-status')?.value || ''
  const riskFilter = $('#project-filter-risk')?.value || ''
  let filtered = state.projects || []
  if (search) {
    filtered = filtered.filter((p) =>
      p.task_name.toLowerCase().includes(search) ||
      p.customer_name.toLowerCase().includes(search) ||
      p.responsible_person.toLowerCase().includes(search)
    )
  }
  if (statusFilter) filtered = filtered.filter((p) => p.status === statusFilter)
  if (riskFilter) filtered = filtered.filter((p) => p.risk === riskFilter)
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
      renderWorkflowWorkspace()
    }
  })
}

async function init() {
  renderShell()
  bindEvents()
  setupEventSource()   // 建立 SSE 连接，实时接收后端事件
  showPage(state.activePage)
  setAgentStatus('Idle', 0)
  $$('.page').forEach((page) => page.classList.toggle('active', page.id === `page-${state.activePage}`))
  // 加载项目数据
  await loadProjects()
  await loadTaskDefinitions()
  if (state.projects.length > 0) {
    state.activeProjectId = state.projects[0].id
    const p = state.projects[0]
    state.customCustomerName = p.customer_name
    state.customTaskName = p.task_name
    const wfTask = findWorkflowTaskByName(p.task_name)
    if (wfTask) {
      state.activeTaskId = wfTask.id
    } else {
      state.activeTaskId = '__custom__'
    }
    renderWorkflowWorkspace()
  }
  
  // 初始化 LLM 配置和 token 显示
  await syncLlmConfig()
  refreshTokens()
  refreshAll()
}

init()
