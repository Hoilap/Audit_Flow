import { api } from './api.js'
import { llmCodePath } from './config.js'
import { activeTask, markStep, state, stepStatus } from './state.js'
import { $ } from './dom.js'
import { addMessage, renderFileError, renderFiles, renderLlmCode, renderStepFiles, renderTimeline, renderWorkflowWorkspace, setAgentStatus, startTimer, stopTimer } from './ui.js'
import { openCsvPreview } from './previewModal.js'
import { openReviewEditor } from './reviewEditor.js'

export async function refreshFiles() {
  try {
    const data = await api.listFiles('outputs')
    renderFiles(data.files || [])
    renderStepFiles()
  } catch (error) {
    renderFileError(error.message)
  }
}

export async function previewFile(path) {
  await openCsvPreview(path)
}

export async function refreshLog() {
  try {
    const data = await api.gitLog()
    const commits = data.commits || data.log || []
    $('#git-log').innerHTML = commits.slice(0, 8).map((item) => `
      <div class="timeline-item done"><span class="timeline-mark">✓</span><span>${item.hexsha || item.hash || ''} · ${item.message || ''}</span></div>
    `).join('') || '<div class="subtle">暂无 Git 历史</div>'
  } catch (error) {
    $('#git-log').innerHTML = `<div class="subtle">Git 历史不可用：${error.message}</div>`
  }
}

export async function refreshApprove() {
  await openReviewEditor()
}

export async function runStep(stepIndex = state.activeStepIndex) {
  const task = activeTask()
  const step = task.steps[stepIndex]
  state.activeStepIndex = stepIndex
  markStep(task.id, step.id, 'running')
  renderWorkflowWorkspace()
  renderTimeline(step.id)
  setAgentStatus('Running', 28)
  startTimer()

  try {
    if (!step.endpoint) {
      const message = '该步骤需要先通过自然语言在 LLM 代码区生成并执行处理脚本。'
      markStep(task.id, step.id, 'completed', { mode: 'llm-code', message })
      addMessage({ title: step.title, body: message })
      setAgentStatus('Completed', 100)
      return { ok: true, skippedToLlm: true }
    }
    const result = await api.workflow(step.endpoint)
    markStep(task.id, step.id, 'completed', result)
    addMessage({ title: step.title, body: '步骤执行完成，右侧已更新生成文件。', result })
    setAgentStatus('Completed', 100)
    await refreshFiles()
    // match 步骤完成后自动弹出人工复核编辑器
    if (step.id === 'match') {
      openReviewEditor()
    }
    return result
  } catch (error) {
    markStep(task.id, step.id, 'failed', { error: error.message })
    renderTimeline(step.id, true)
    addMessage({ title: step.title, body: error.message, failed: true })
    setAgentStatus('Failed', 100)
    throw error
  } finally {
    stopTimer()
    renderWorkflowWorkspace()
  }
}

export async function runNextStep() {
  const task = activeTask()
  const nextIndex = task.steps.findIndex((step) => stepStatus(task.id, step.id) !== 'completed')
  if (nextIndex === -1) {
    addMessage({ body: '当前任务所有步骤都已完成。' })
    return
  }
  await runStep(nextIndex)
}

export async function runAllSteps() {
  const task = activeTask()
  for (let index = 0; index < task.steps.length; index += 1) {
    if (stepStatus(task.id, task.steps[index].id) === 'completed') continue
    await runStep(index)
  }
}

export async function sendPrompt() {
  const prompt = $('#prompt').value.trim()
  if (!prompt) return
  setAgentStatus('Running', 20)
  startTimer()
  addMessage({ role: 'User', body: prompt })

  const form = new FormData()
  form.append('prompt', prompt)
  form.append('target', llmCodePath)
  form.append('run', $('#run-after-generate').checked ? 'true' : 'false')
  form.append('timeout', $('#composer-timeout').value || '5')

  try {
    const result = await api.generateAndRun(form)
    const code = await api.readFile(llmCodePath).catch(() => ({ content: result.code || '' }))
    renderLlmCode(llmCodePath, code.content || result.code || '', result)
    addMessage({ title: 'LLM 代码生成', body: '代码已写入独立目录，可继续执行下一步。', result })
    setAgentStatus('Completed', 100)
    await refreshFiles()
  } catch (error) {
    renderLlmCode(llmCodePath, '', { error: error.message })
    addMessage({ title: 'LLM 代码生成失败', body: error.message, failed: true })
    setAgentStatus('Failed', 100)
  } finally {
    stopTimer()
  }
}

export async function uploadFile() {
  const input = $('#upload-file')
  if (!input.files?.length) return
  const form = new FormData()
  form.append('file', input.files[0])
  form.append('dest', $('#upload-dest').value || 'inputs/')
  try {
    const result = await api.uploadFile(form)
    addMessage({ title: '文件上传', body: `已上传到 ${result.path || '输入目录'}` })
  } catch (error) {
    addMessage({ title: '文件上传失败', body: error.message, failed: true })
  }
}

export async function commitAll() {
  try {
    const result = await api.gitCommit('AuditFlow evidence update')
    addMessage({ title: 'Git 提交', body: result.message || '已提交当前证据链。', result })
    await refreshLog()
  } catch (error) {
    addMessage({ title: 'Git 提交失败', body: error.message, failed: true })
  }
}
