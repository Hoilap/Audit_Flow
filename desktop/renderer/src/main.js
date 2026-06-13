import { workflowTasks } from './config.js'
import { state } from './state.js'
import { $, $$ } from './dom.js'
import { renderShell, renderWorkflowWorkspace, setAgentStatus, showPage } from './ui.js'
import { commitAll, previewFile, refreshFiles, refreshLog, runAllSteps, runNextStep, runStep, sendPrompt, uploadFile } from './actions.js'
import { openReviewEditor } from './reviewEditor.js'

function bindEvents() {
  document.addEventListener('click', async (event) => {
    const navButton = event.target.closest('[data-page]')
    if (navButton) showPage(navButton.dataset.page)

    const taskButton = event.target.closest('[data-task-id]')
    if (taskButton) {
      state.activeTaskId = taskButton.dataset.taskId
      state.activeStepIndex = 0
      $('#prompt').value = workflowTasks.find((task) => task.id === state.activeTaskId)?.prompt || ''
      renderWorkflowWorkspace()
    }

    const stepCard = event.target.closest('[data-step-index]')
    if (stepCard) {
      state.activeStepIndex = Number(stepCard.dataset.stepIndex)
      renderWorkflowWorkspace()
    }

    const fileRow = event.target.closest('[data-file]')
    if (fileRow) previewFile(fileRow.dataset.file)
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
  $('#open-review-editor').addEventListener('click', () => openReviewEditor())
  $('#run-next-step').addEventListener('click', () => withDisabled('#run-next-step', runNextStep))
  $('#run-all-steps').addEventListener('click', () => withDisabled('#run-all-steps', runAllSteps))
  $('#send').addEventListener('click', () => withDisabled('#send', sendPrompt))
  $('#upload-btn').addEventListener('click', () => withDisabled('#upload-btn', uploadFile))
  $('#commit-all').addEventListener('click', () => withDisabled('#commit-all', commitAll))

  $('#model-select').addEventListener('change', (event) => {
    $('#status-model').textContent = event.target.value
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

function init() {
  renderShell()
  bindEvents()
  showPage(state.activePage)
  setAgentStatus('Idle', 0)
  $$('.page').forEach((page) => page.classList.toggle('active', page.id === `page-${state.activePage}`))
  refreshAll()
}

init()
