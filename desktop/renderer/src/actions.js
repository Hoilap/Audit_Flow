import { api } from './api.js'
import { llmCodePath, workflowTasks } from './config.js'
import { activeTask, findWorkflowTaskByName, getProjectBasePath, resolveProjectPath, markStep, state, stepStatus } from './state.js'
import { $ } from './dom.js'
import { addMessage, renderFileError, renderFileTree, renderFiles, renderLlmCode, renderStepFiles, renderTimeline, renderWorkflowWorkspace, setAgentStatus, startTimer, stopTimer, renderDetectResult, renderConfigConfirm, renderCheckResult } from './ui.js'
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

  const customerName = state.customCustomerName || ''
  const taskName = state.customTaskName || 'bank_ledger_match'
  const taskDirName = (findWorkflowTaskByName(taskName) || {}).dirName || 'bank_ledger_match'

  try {
    if (!step.endpoint) {
      const message = '该步骤需要先通过自然语言在 LLM 代码区生成并执行处理脚本。'
      markStep(task.id, step.id, 'completed', { mode: 'llm-code', message })
      addMessage({ title: step.title, body: message })
      setAgentStatus('Completed', 100)
      return { ok: true, skippedToLlm: true }
    }

    let result

    // ── Step 1: Detect ──
    if (step.id === 'detect') {
      if (!customerName) {
        throw new Error('请先在项目选择器中输入客户名称。')
      }
      const useLlm = state.detectMethod === 'llm'
      result = await api.workflowDetect(customerName, taskDirName, useLlm)

      let bodyText = `扫描完成：找到 ${result.files_count} 个文件。`
      if (result.llm_error) {
        bodyText += `\n⚠️ LLM 调用失败，已回退到本地关键词识别。错误原因：${result.llm_error.message}`
      } else if (result.llm_used) {
        bodyText += `\n🤖 LLM 识别已启用。`
      } else {
        bodyText += `\n📜 使用脚本（关键词）识别。`
      }

      markStep(task.id, step.id, 'completed', result)
      addMessage({
        title: step.title,
        body: bodyText,
        result,
      })
      // Detect 完成后自动加载配置并展示
      if (result.ok && result.task_config) {
        await renderDetectResult(result)
      }
    }
    // ── Step 2: Confirm ──
    else if (step.id === 'confirm') {
      // 获取当前配置内容
      const configRes = await api.workflowGetConfig(customerName, taskDirName)
      if (!configRes.ok) {
        throw new Error(configRes.error || '尚未生成配置，请先执行 Detect 步骤。')
      }
      // 展示配置确认 UI
      renderConfigConfirm(configRes.content, customerName, taskDirName)
      markStep(task.id, step.id, 'completed', { mode: 'config_confirm', configRes })
      addMessage({ title: step.title, body: '已加载 task.yml 配置，请在右侧面板确认或修改。' })
      result = { ok: true, configShown: true }
    }
    // ── Step 4: Check ──
    else if (step.id === 'check') {
      result = await api.workflowCheck(customerName, taskDirName)
      markStep(task.id, step.id, result.ok ? 'completed' : 'failed', result)
      if (result.ok && result.all_ok) {
        addMessage({ title: step.title, body: '✅ 数据完备性检查通过！银行流水与序时账月度流入流出一致。', result })
      } else if (result.ok && !result.all_ok) {
        addMessage({
          title: step.title,
          body: `⚠️ 发现 ${result.summary.mismatch_count} 个月份/账户数据不一致，请检查。`,
          result,
          failed: true,
        })
        renderCheckResult(result)
      } else {
        addMessage({ title: step.title, body: result.error || '检查失败', failed: true })
      }
    }
    // ── 其他步骤（clean, match, fill）── 使用通用 workflow + customer 参数
    else {
      const form = new FormData()
      form.append('customer_name', customerName)
      form.append('task_name', taskDirName)
      result = await api.workflow(step.endpoint, form)
      markStep(task.id, step.id, 'completed', result)
      addMessage({ title: step.title, body: '步骤执行完成，右侧已更新生成文件。', result })
    }

    setAgentStatus('Completed', 100)
    await refreshFiles()
    // 自动弹出复核文件
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

/**
 * 更新 Detect 步骤的识别方式：'llm' | 'script'
 */
export function updateDetectMethod(method) {
  state.detectMethod = method
  renderWorkflowWorkspace()
}

/**
 * 保存用户确认/修改后的 task.yml 配置
 */
export async function saveConfig(customerName, taskName, content) {
  try {
    setAgentStatus('Saving', 50)
    const result = await api.workflowSaveConfig(customerName, taskName, content)
    addMessage({ title: '配置已保存', body: `task.yml 已保存到 ${result.path}` })
    setAgentStatus('Completed', 100)
    return result
  } catch (error) {
    addMessage({ title: '保存配置失败', body: error.message, failed: true })
    setAgentStatus('Failed', 100)
    throw error
  }
}
