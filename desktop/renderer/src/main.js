import { workflowTasks } from './config.js'
import { findWorkflowTaskByName, state } from './state.js'
import { $, $$ } from './dom.js'
import { renderShell, renderWorkflowWorkspace, setAgentStatus, showPage } from './ui.js'
import { commitAll, createProject, deleteProject, loadProjects, previewFile, refreshFiles, refreshLog, runAllSteps, runNextStep, runStep, selectProject, sendPrompt, toggleCustomMode, updateCustomCustomerName, updateCustomTaskName, updateDetectMethod, updateProject, uploadFile, syncLlmConfig, startTokenPolling, stopTokenPolling, updateLlmModel } from './actions.js'
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
        renderWorkflowWorkspace()
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

    // 消息/配置面板折叠切换
    const collapseHead = event.target.closest('.message-head, .llm-code-head')
    if (collapseHead) {
      const container = collapseHead.closest('.message') || collapseHead.closest('.llm-code-panel')
      if (container) container.classList.toggle('collapsed')
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
    // Detect 识别方式 radio
    if (event.target.name === 'detect-method') {
      updateDetectMethod(event.target.value)
    }
  })

  // 客户名称实时更新
  document.addEventListener('input', (event) => {
    if (event.target.id === 'project-customer-input') {
      updateCustomCustomerName(event.target.value)
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

  // 底部 LLM 输入区折叠
  $('#toggle-chat').addEventListener('click', () => {
    const composer = document.querySelector('.composer')
    if (!composer) return
    state.chatHidden = !state.chatHidden
    composer.style.display = state.chatHidden ? 'none' : ''
    $('#main').style.paddingBottom = state.chatHidden ? '20px' : ''
    $('#toggle-chat').textContent = state.chatHidden ? '▲' : '▼'
  })

  // 页面初始化时恢复 composer 状态
  if (state.chatHidden) {
    const composer = document.querySelector('.composer')
    if (composer) composer.style.display = 'none'
    const main = $('#main')
    if (main) main.style.paddingBottom = '20px'
    $('#toggle-chat').textContent = '▲'
  }

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
  $('#send').addEventListener('click', () => withDisabled('#send', sendPrompt))
  $('#upload-btn').addEventListener('click', () => withDisabled('#upload-btn', uploadFile))
  $('#commit-all').addEventListener('click', () => withDisabled('#commit-all', commitAll))

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
  showPage(state.activePage)
  setAgentStatus('Idle', 0)
  $$('.page').forEach((page) => page.classList.toggle('active', page.id === `page-${state.activePage}`))
  // 加载项目数据
  await loadProjects()
  if (state.projects.length > 0) {
    state.activeProjectId = state.projects[0].id
    const p = state.projects[0]
    state.customCustomerName = p.customer_name
    state.customTaskName = p.task_name
    const wfTask = findWorkflowTaskByName(p.task_name)
    if (wfTask) state.activeTaskId = wfTask.id
    renderWorkflowWorkspace()
  }
  
  // 初始化 LLM 配置和 token 轮询
  await syncLlmConfig()
  startTokenPolling(2000)
  refreshAll()
}

init()
