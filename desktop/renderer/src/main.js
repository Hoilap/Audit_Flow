import { workflowTasks } from './config.js'
import { state } from './state.js'
import { $, $$ } from './dom.js'
import { renderShell, renderWorkflowWorkspace, setAgentStatus, showPage } from './ui.js'
import { commitAll, previewFile, refreshApprove, refreshFiles, refreshLog, runAllSteps, runNextStep, runStep, sendPrompt, uploadFile } from './actions.js'

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

  $('#theme-toggle').addEventListener('click', () => document.body.classList.toggle('dark'))
  $('#refresh-all').addEventListener('click', refreshAll)
  $('#refresh-files').addEventListener('click', refreshFiles)
  $('#refresh-log').addEventListener('click', refreshLog)
  $('#refresh-approve').addEventListener('click', refreshApprove)
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
  await Promise.allSettled([refreshFiles(), refreshLog(), refreshApprove()])
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
