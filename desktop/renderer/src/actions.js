import { api } from './api.js'
import { llmCodePath, workflowTasks } from './config.js'
import { activeTask, findWorkflowTaskByName, getProjectBasePath, resolveProjectPath, markStep, state, stepStatus } from './state.js'
import { $ } from './dom.js'
import { addMessage, renderFileError, renderFileTree, renderFiles, renderLlmCode, renderStepFiles, renderTimeline, renderWorkflowWorkspace, setAgentStatus, startTimer, stopTimer } from './ui.js'
import { openCsvPreview } from './previewModal.js'
import { openReviewEditor } from './reviewEditor.js'

export async function refreshFiles() {
  try {
    const root = getProjectBasePath() || 'outputs'
    const data = await api.listFiles(root)
    renderFiles(data.files || [], root)
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
  const wfTask = findWorkflowTaskByName(state.customTaskName)
  const reviewFiles = wfTask ? wfTask.reviewFiles || [] : []
  if (reviewFiles.length > 0) {
    const resolved = resolveProjectPath(reviewFiles[0])
    if (resolved) await openReviewEditor(resolved)
  } else {
    alert('当前工作流没有配置复核文件。')
  }
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
    // 步骤完成后，如果该工作流配置了复核文件，自动弹出第一个复核文件编辑器
    const wfTask = findWorkflowTaskByName(state.customTaskName)
    const reviewFiles = wfTask ? wfTask.reviewFiles || [] : []
    if (reviewFiles.length > 0) {
      const resolved = resolveProjectPath(reviewFiles[0])
      if (resolved) openReviewEditor(resolved)
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
    // 刷新文件树
    await renderFileTree()
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

// ────────── 项目管理 CRUD ──────────

export async function loadProjects() {
  try {
    const data = await api.listProjects()
    state.projects = data.projects || []
  } catch (error) {
    state.projects = []
  }
}

export async function createProject(payload) {
  const data = await api.createProject(payload)
  await loadProjects()
  return data.project
}

export async function updateProject(id, payload) {
  const data = await api.updateProject(id, payload)
  await loadProjects()
  return data.project
}

export async function deleteProject(id) {
  await api.deleteProject(id)
  await loadProjects()
}

// ────────── 项目选择器交互 ──────────

/**
 * 当下拉选择已有项目时调用。
 */
export function selectProject(projectId) {
  const id = projectId ? Number(projectId) : null
  state.activeProjectId = id
  if (id) {
    const project = state.projects.find((p) => p.id === id)
    if (project) {
      state.customCustomerName = project.customer_name
      state.customTaskName = project.task_name
      const wfTask = findWorkflowTaskByName(project.task_name)
      if (wfTask) {
        state.activeTaskId = wfTask.id
        $('#prompt').value = wfTask.prompt || ''
      } else {
        // 自定义任务名：使用第一个 workflow 模板
        state.activeTaskId = workflowTasks[0].id
      }
    }
  } else {
    // 未选项目，回退到自定义模式
    state.activeProjectId = null
  }
  state.activeStepIndex = 0
  renderWorkflowWorkspace()
}

/**
 * 切换自定义模式 checkbox
 */
export function toggleCustomMode(checked) {
  state.activeProjectId = checked ? null : (state.projects[0]?.id || null)
  if (!checked && state.projects.length > 0 && !state.activeProjectId) {
    state.activeProjectId = state.projects[0].id
  }
  renderWorkflowWorkspace()
}

/**
 * 自定义模式下更新客户名称
 */
export function updateCustomCustomerName(name) {
  state.customCustomerName = name
}

/**
 * 自定义模式下更新任务名称
 */
export function updateCustomTaskName(taskName) {
  state.customTaskName = taskName
  const wfTask = findWorkflowTaskByName(taskName)
  if (wfTask) {
    state.activeTaskId = wfTask.id
    $('#prompt').value = wfTask.prompt || ''
  }
  state.activeStepIndex = 0
  renderWorkflowWorkspace()
}
