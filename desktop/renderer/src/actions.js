import { api, apiBase, cancelRequest } from './api.js'
import { activeTask, findWorkflowTaskByName, getProjectBasePath, resolveProjectPath, markStep, state, stepStatus } from './state.js'
import { $ } from './dom.js'
import { addMessage, renderFileError, renderFileTree, renderFiles, renderWorkflowWorkspace, setAgentStatus, startTimer, stopTimer, renderDetectResult, renderConfigConfirm, renderCheckResult, renderProgramsList, renderSheetTasks, renderSettingsProviders } from './ui.js'
import { openCsvPreview } from './previewModal.js'
import { openReviewEditor } from './reviewEditor.js'
import { createModal } from './modal.js'
import { logger } from './logger.js'

export async function refreshFiles() {
  try {
    const root = getProjectBasePath() || 'outputs'
    const data = await api.listFiles(root)
    renderFiles(data.files || [], root)
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

export function cancelRunningStep() {
  logger.info('cancelRunningStep', '用户请求停止当前任务')
  cancelRequest()
  stopTimer()
  setAgentStatus('Cancelled', 0)
  addMessage({ title: '任务已停止', body: '用户手动停止了当前任务。' })
}

export async function runStep(stepIndex = state.activeStepIndex) {
  const task = activeTask()
  const step = task.steps[stepIndex]
  state.activeStepIndex = stepIndex
  logger.info('runStep', `${step.title} (id=${step.id})`)
  markStep(task.id, step.id, 'running')
  renderWorkflowWorkspace()
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
    // ── Step 3a: Clean Bank ──
    else if (step.id === 'clean-bank') {
      const parser = state.detectMethod === 'llm' ? 'llm_bank' : state.detectMethod
      result = await api.workflowCleanBank(customerName, taskDirName, parser)
      markStep(task.id, step.id, 'completed', result)
      addMessage({
        title: step.title,
        body: `使用解析器「${parser}」清洗银行流水完成。`,
        result,
      })
    }
    // ── Step 3b: Clean Ledger ──
    else if (step.id === 'clean-ledger') {
      const parser = state.detectMethod === 'llm' ? 'llm_ledger' : state.detectMethod
      result = await api.workflowCleanLedger(customerName, taskDirName, parser)
      markStep(task.id, step.id, 'completed', result)
      addMessage({
        title: step.title,
        body: `使用解析器「${parser}」清洗序时账完成。`,
        result,
      })
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
    // ── Step 6: Fill ──
    else if (step.id === 'fill') {
      const useLlm = state.detectMethod === 'llm'
      result = useLlm
        ? await api.workflowFillLlm(customerName, taskDirName)
        : await api.workflowFill(customerName, taskDirName)
      const methodLabel = useLlm ? '🤖 LLM 自适应填表' : '📜 脚本填表'
      markStep(task.id, step.id, 'completed', result)
      addMessage({ title: step.title, body: `${methodLabel} 完成，右侧已更新生成文件。`, result })
    }
    // ── OSM Step 1: Detect (出库结算扫描) ──
    else if (step.id === 'osm-detect') {
      if (!customerName) {
        throw new Error('请先在项目选择器中输入客户名称。')
      }
      const form = new FormData()
      form.append('customer_name', customerName)
      form.append('task_name', taskDirName)
      result = await api.workflow(step.endpoint, form)
      markStep(task.id, step.id, 'completed', result)
      const sCount = (result.settlement_files || []).length
      const oCount = (result.outbound_files || []).length
      addMessage({
        title: step.title,
        body: `扫描完成：找到 ${sCount} 个结算流水文件，${oCount} 个出库表文件。`,
        result,
      })
    }
    // ── OSM Step 2: Clean Settlement ──
    else if (step.id === 'clean-settlement') {
      const form = new FormData()
      form.append('customer_name', customerName)
      form.append('task_name', taskDirName)
      const parser = state.detectMethod === 'llm' ? 'llm_settlement' : state.detectMethod
      form.append('parser', parser)
      result = await api.workflow(step.endpoint, form)
      markStep(task.id, step.id, 'completed', result)
      const mode = result.cleaning_mode === 'llm' ? 'LLM 生成脚本' : '硬编码规则'
      let body = `结算流水清洗完成（${mode}），已生成合并结算 CSV 和月度汇总。`
      if (result.usage && result.usage.total_tokens > 0) {
        body += `\nToken 用量：${result.usage.total_tokens}（输入 ${result.usage.prompt_tokens || 0}，输出 ${result.usage.completion_tokens || 0}）`
      }
      if (result.script_dir) {
        body += `\n脚本目录：${result.script_dir}`
      }
      if (result.fallback_reason) {
        body += `\n⚠ 回退原因：${result.fallback_reason}`
      }
      if (result.file_fallbacks && result.file_fallbacks.length > 0) {
        const files = result.file_fallbacks.map(f => `${f.file}: ${f.reason}`).join('\n')
        body += `\n以下文件回退到硬编码：\n${files}`
      }
      addMessage({
        title: step.title,
        body,
        result,
      })
    }
    // ── OSM Step 3: Clean Outbound ──
    else if (step.id === 'clean-outbound') {
      const form = new FormData()
      form.append('customer_name', customerName)
      form.append('task_name', taskDirName)
      const parser = state.detectMethod === 'llm' ? 'llm_outbound' : state.detectMethod
      form.append('parser', parser)
      result = await api.workflow(step.endpoint, form)
      markStep(task.id, step.id, 'completed', result)
      const paths = result.paths || {}
      const pathCount = Object.values(paths).filter(Boolean).length
      const mode = result.cleaning_mode === 'llm' ? 'LLM 生成脚本' : '硬编码规则'
      let body = `出库表清洗完成（${mode}），生成 ${pathCount} 个标准化 CSV 文件。`
      if (result.usage && result.usage.total_tokens > 0) {
        body += `\nToken 用量：${result.usage.total_tokens}（输入 ${result.usage.prompt_tokens || 0}，输出 ${result.usage.completion_tokens || 0}）`
      }
      if (result.script_dir) {
        body += `\n脚本目录：${result.script_dir}`
      }
      if (result.fallback_reason) {
        body += `\n⚠ 回退原因：${result.fallback_reason}`
      }
      if (result.file_fallbacks && result.file_fallbacks.length > 0) {
        const sheets = result.file_fallbacks.map(f => `${f.file}/${f.sheet}: ${f.reason}`).join('\n')
        body += `\n以下工作表回退到硬编码：\n${sheets}`
      }
      addMessage({
        title: step.title,
        body,
        result,
      })
      // 渲染每个工作表的任务状态表（含重试按钮）
      if (result.sheet_tasks && result.sheet_tasks.length > 0) {
        renderSheetTasks(result, async (file, sheet, sheetType, colSig) => {
          const retryForm = new FormData()
          retryForm.append('customer_name', customerName)
          retryForm.append('task_name', taskDirName)
          retryForm.append('file', file)
          retryForm.append('sheet', sheet)
          retryForm.append('sheet_type', sheetType)
          retryForm.append('column_signature', colSig)
          retryForm.append('force_regenerate', 'true')
          retryForm.append('parser', state.detectMethod === 'llm' ? 'llm_outbound' : state.detectMethod)
          const retryResult = await api.workflow(
            '/workflow/outbound_settlement_match/clean_outbound_sheet',
            retryForm,
          )
          // 显示重试 token 用量
          if (retryResult.usage && retryResult.usage.total_tokens > 0) {
            addMessage({
              title: `重试: ${sheet}`,
              body: `Token 用量：${retryResult.usage.total_tokens}（输入 ${retryResult.usage.prompt_tokens || 0}，输出 ${retryResult.usage.completion_tokens || 0}）`,
            })
          }
          return retryResult
        })
      }
    }
    // ── 其他步骤（match, verify）── 使用通用 workflow + customer 参数
    else {
      const form = new FormData()
      form.append('customer_name', customerName)
      form.append('task_name', taskDirName)
      const parser = state.detectMethod === 'llm' ? 'llm' : state.detectMethod
      if (parser) form.append('parser', parser)
      result = await api.workflow(step.endpoint, form)
      markStep(task.id, step.id, 'completed', result)
      // Match 步骤特殊消息
      if (step.id === 'match' && result.summary) {
        const s = result.summary
        addMessage({
          title: step.title,
          body: `匹配完成：匹配 ${s.matched || 0} 条，未匹配出库 ${s.unmatched_outbound || 0} 条，未匹配结算 ${s.unmatched_settlement || 0} 条。`,
          result,
        })
      } else {
        addMessage({ title: step.title, body: '步骤执行完成，右侧已更新生成文件。', result })
      }
    }

    setAgentStatus('Completed', 100)
    await refreshFiles()
    await refreshTokens()
    // 自动弹出复核文件 —— 仅在 match 步骤后
    if (step.id === 'match') {
      const wfTask = findWorkflowTaskByName(state.customTaskName)
      const reviewFiles = wfTask ? wfTask.reviewFiles || [] : []
      if (reviewFiles.length > 0) {
        const resolved = resolveProjectPath(reviewFiles[0])
        if (resolved) openReviewEditor(resolved)
      }
    }
    return result
  } catch (error) {
    if (error.name === 'AbortError') {
      logger.info('runStep', `${step.title} 已被用户停止`)
      markStep(task.id, step.id, 'idle')
      return
    }
    logger.error('runStep', `${step.title} 失败: ${error.message}`)
    markStep(task.id, step.id, 'failed', { error: error.message })
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

export async function uploadFile() {
  const input = $('#upload-file')
  if (!input.files?.length) return
  logger.info('uploadFile', `上传文件: ${input.files[0].name}`)
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
        // 预定义任务不预制 prompt，让用户自由输入
        if ($('#prompt')) $('#prompt').value = ''
      } else {
        // 自定义任务名：使用通用自定义工作流模板，并提示用户包含路径信息
        state.activeTaskId = '__custom__'
        if ($('#prompt')) {
          $('#prompt').value = '请描述您的审计任务需求，并说明输入数据路径（如 inputs/{客户名}/ 下的文件）和期望的输出结果路径（如 outputs/{客户名}/ 下的文件）。'
        }
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
    // 预定义任务不预制 prompt
    if ($('#prompt')) $('#prompt').value = ''
  } else {
    state.activeTaskId = '__custom__'
    // 自定义任务提示用户包含路径信息
    if ($('#prompt')) {
      $('#prompt').value = '请描述您的审计任务需求，并说明输入数据路径（如 inputs/{客户名}/ 下的文件）和期望的输出结果路径（如 outputs/{客户名}/ 下的文件）。'
    }
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

// ────────── LLM 配置与 Token 计数 ──────────

/**
 * 从后端读取 LLM 配置并更新前端模型下拉菜单。
 * 后端返回 providers 列表，前端为每个 provider 生成一个 <option>，
 * 并将当前 default provider 设为选中项。
 */
export async function syncLlmConfig() {
  try {
    const result = await api.getLlmConfig()
    if (result.ok && result.config) {
      const modelSelect = $('#model-select')
      if (modelSelect) {
        const providers = result.config.providers || []
        const defaultName = result.config.default || ''

        if (providers.length > 0) {
          modelSelect.innerHTML = providers.map(p => {
            const selected = p.name === defaultName ? ' selected' : ''
            const label = `${p.model} (${p.name})`
            return `<option value="${p.name}"${selected}>${label}</option>`
          }).join('')
        } else {
          modelSelect.innerHTML = '<option value="">未配置 Provider</option>'
        }

        const currentProvider = providers.find(p => p.name === defaultName)
        updateStatusModel(currentProvider ? currentProvider.model : 'unknown')
      }
    }
  } catch (error) {
    console.error('Failed to sync LLM config:', error)
  }
}

/**
 * 更新顶部状态栏中显示的模型名称
 */
function updateStatusModel(modelName) {
  const el = $('#status-model')
  if (el) el.textContent = modelName || 'unknown'
}

/**
 * 单次请求后端获取 token 使用情况并更新显示。
 * 在每次 LLM 调用完成后调用，替代定时轮询。
 */
export async function refreshTokens() {
  try {
    const result = await api.getLlmTokens()
    if (result.ok) {
      const { total_tokens } = result
      const el = $('#tokens')
      if (el) el.textContent = `${total_tokens.toLocaleString()} tokens`
    }
  } catch (error) {
    // 忽略错误
  }
}

/**
 * 建立 SSE (Server-Sent Events) 连接，监听后端推送的事件。
 * 后端每次 LLM 调用完成后会自动推送 token_updated 事件，
 * 前端实时更新顶部 token 用量显示，无需轮询。
 * EventSource 断开后会自动重连。
 */
export function setupEventSource() {
  const es = new EventSource(`${apiBase}/events`)

  es.addEventListener('token_updated', (e) => {
    try {
      const data = JSON.parse(e.data)
      const el = $('#tokens')
      if (el) el.textContent = `${(data.total_tokens || 0).toLocaleString()} tokens`
    } catch (err) {
      // 忽略解析错误
    }
  })

  es.addEventListener('retry', (e) => {
    try {
      const data = JSON.parse(e.data)
      const reasonMap = {
        syntax: '语法错误',
        runtime: '运行错误',
        missing_clean: '缺少 clean() 函数',
      }
      const reasonLabel = reasonMap[data.reason] || data.reason
      const attempt = data.attempt || 0
      const maxRetries = data.max_retries || 3
      setAgentStatus('Running', 60, `重试中 (${attempt}/${maxRetries})…`)
      addMessage({
        title: '自动重试',
        body: `代码存在${reasonLabel}，正在重试 (${attempt}/${maxRetries})…\n${data.error || ''}`,
      })
    } catch (err) {
      // 忽略解析错误
    }
  })

  // 银行流水/序时账解析器生成进度
  es.addEventListener('blm_clean', (e) => {
    try {
      const data = JSON.parse(e.data)
      const isLedger = data.type === 'ledger'
      const parserLabel = isLedger ? '序时账解析器' : '银行解析器'
      if (data.status === 'generating') {
        setAgentStatus('Running', 50, `正在生成 ${data.item_id} 解析代码…`)
      } else if (data.status === 'generated') {
        addMessage({ title: `${parserLabel}已生成`, body: data.message || data.item_id })
      } else if (data.status === 'cache_hit') {
        addMessage({ title: `${parserLabel}（缓存命中）`, body: data.message || data.item_id })
      }
    } catch (err) {
      // 忽略解析错误
    }
  })

  // Agent 对话面板事件
  es.addEventListener('agent_step', (e) => {
    try {
      const data = JSON.parse(e.data)
      import('./agentActions.js').then(({ handleAgentStepEvent }) => {
        handleAgentStepEvent(data)
      })
    } catch (err) {
      // 忽略解析错误
    }
  })

  es.onerror = () => {
    // EventSource 内置自动重连（默认 3 秒间隔），无需手动处理
  }

  return es
}

/**
 * 保存用户在模型选择下拉菜单中的选择到后端
 */
export async function updateLlmModel(providerName) {
  try {
    await api.updateLlmConfig(providerName, undefined)
    // 更新状态栏显示：找到当前选中 option 的文本，提取其中的 model 名
    const modelSelect = $('#model-select')
    const selectedOption = modelSelect ? modelSelect.options[modelSelect.selectedIndex] : null
    const modelLabel = selectedOption ? selectedOption.text.split(' (')[0] : providerName
    updateStatusModel(modelLabel)
    addMessage({ title: 'LLM 模型已更新', body: `当前 Provider：${providerName}` })
  } catch (error) {
    addMessage({ title: '模型更新失败', body: error.message, failed: true })
  }
}

/**
 * 加载所有任务子目录下的 readme.md 内容并渲染到审计程序页面。
 */
export async function loadProgramReadmes() {
  try {
    const data = await api.getReadmes()
    renderProgramsList(data.readmes || [])
  } catch (error) {
    console.error('Failed to load READMEs:', error)
    renderProgramsList([])
  }
}

// ────────── 设置页 LLM Provider 配置 ──────────

/**
 * 从后端加载 LLM provider 配置并渲染设置页面。
 */
export async function loadSettingsProviders() {
  try {
    const result = await api.getLlmConfig()
    if (result.ok && result.config) {
      state.llmProviders = result.config.providers || []
      state.llmConfigDefault = result.config.default || ''
      state.llmConfigPath = result.path || ''
      renderSettingsProviders()
    }
  } catch (error) {
    const container = $('#settings-provider-list')
    if (container) container.innerHTML = `<p class="subtle">加载失败: ${error.message}</p>`
  }
}

/**
 * 从后端获取指定 provider 的明文 API Key。
 */
export async function revealProviderKey(providerName) {
  try {
    const result = await api.getLlmProviderKey(providerName)
    if (result.ok) return result.api_key || ''
    return ''
  } catch {
    return ''
  }
}

/**
 * 收集所有 provider 表单数据并保存到后端。
 */
export async function saveLlmProviders() {
  const statusEl = $('#settings-provider-status')
  if (statusEl) {
    statusEl.textContent = '保存中...'
    statusEl.className = 'subtle'
  }

  const cards = document.querySelectorAll('.settings-provider-card')
  const providers = []

  cards.forEach((card) => {
    const name = card.dataset.providerName
    const keyInput = card.querySelector('.settings-api-key-input')
    const urlInput = card.querySelector('.settings-base-url-input')
    const modelInput = card.querySelector('.settings-model-input')

    const maskedOriginal = keyInput?.dataset.originalMasked || ''
    const currentKey = keyInput?.value || ''

    providers.push({
      name,
      // 仅当用户修改了值时才发送 api_key（与原始遮罩值对比）
      api_key: currentKey !== maskedOriginal ? currentKey : '',
      base_url: urlInput?.value || '',
      model: modelInput?.value || '',
    })
  })

  try {
    const result = await api.updateLlmProviders(providers)
    if (result.ok) {
      if (statusEl) {
        statusEl.textContent = '✓ 配置已保存'
        statusEl.style.color = '#27ae60'
      }
      // 重新加载以获取最新遮罩值
      await loadSettingsProviders()
      // 同步侧栏模型下拉菜单
      await syncLlmConfig()
    } else {
      if (statusEl) {
        statusEl.textContent = '✗ 保存失败: ' + (result.error || '')
        statusEl.style.color = '#e74c3c'
      }
    }
  } catch (error) {
    if (statusEl) {
      statusEl.textContent = `✗ 保存失败: ${error.message}`
      statusEl.style.color = '#e74c3c'
    }
  }
}

/**
 * 弹出表单模态框，填写新 provider 信息后保存。
 */
export function addLlmProvider() {
  const modal = createModal({ title: '添加 LLM Provider', width: '480px', height: 'auto' })
  modal.setBody(`
    <div style="display:flex;flex-direction:column;gap:12px;padding:8px 0;">
      <label style="font-size:13px;">名称（唯一标识）
        <input class="search" id="new-provider-name" placeholder="例如 deepseek" style="width:100%;" />
      </label>
      <label style="font-size:13px;">Base URL
        <input class="search" id="new-provider-url" placeholder="https://api.example.com/v1" style="width:100%;" />
      </label>
      <label style="font-size:13px;">Model
        <input class="search" id="new-provider-model" placeholder="model-name" style="width:100%;" />
      </label>
      <label style="font-size:13px;">API Key（可选）
        <input class="search" id="new-provider-key" type="password" placeholder="留空则稍后配置" style="width:100%;font-family:monospace;" />
      </label>
      <label style="font-size:13px;">API Key 环境变量名（可选）
        <input class="search" id="new-provider-key-env" placeholder="例如 MY_API_KEY" style="width:100%;" />
      </label>
      <div id="new-provider-error" style="color:#e74c3c;font-size:12px;"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px;">
        <button id="new-provider-cancel" class="ghost" style="padding:6px 16px;">取消</button>
        <button id="new-provider-submit" class="primary" style="padding:6px 16px;">添加</button>
      </div>
    </div>
  `)
  modal.open()

  const submit = async () => {
    const name = modal.getBodyEl().querySelector('#new-provider-name').value.trim()
    const baseUrl = modal.getBodyEl().querySelector('#new-provider-url').value.trim()
    const model = modal.getBodyEl().querySelector('#new-provider-model').value.trim()
    const apiKey = modal.getBodyEl().querySelector('#new-provider-key').value.trim()
    const apiKeyEnv = modal.getBodyEl().querySelector('#new-provider-key-env').value.trim()
    const errorEl = modal.getBodyEl().querySelector('#new-provider-error')

    if (!name) {
      errorEl.textContent = '名称不能为空'
      return
    }

    try {
      const result = await api.createLlmProvider({
        name, base_url: baseUrl, model, api_key: apiKey, api_key_env: apiKeyEnv,
      })
      if (result.ok) {
        modal.close()
        await loadSettingsProviders()
        await syncLlmConfig()
      } else {
        errorEl.textContent = result.error || '添加失败'
      }
    } catch (err) {
      errorEl.textContent = err.detail || err.message || '添加失败'
    }
  }

  modal.getBodyEl().querySelector('#new-provider-submit').addEventListener('click', submit)
  modal.getBodyEl().querySelector('#new-provider-cancel').addEventListener('click', () => modal.close())
  modal.getBodyEl().querySelector('#new-provider-name').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit()
  })
}

/**
 * 确认并删除指定 provider。
 */
export async function deleteLlmProvider(name) {
  if (!confirm(`确定要删除 Provider "${name}" 吗？`)) return

  const statusEl = $('#settings-provider-status')
  try {
    const result = await api.deleteLlmProvider(name)
    if (result.ok) {
      if (statusEl) {
        statusEl.textContent = `✓ 已删除 ${name}`
        statusEl.style.color = '#27ae60'
      }
      await loadSettingsProviders()
      await syncLlmConfig()
    } else {
      if (statusEl) {
        statusEl.textContent = '✗ ' + (result.error || '删除失败')
        statusEl.style.color = '#e74c3c'
      }
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = `✗ 删除失败: ${err.detail || err.message}`
      statusEl.style.color = '#e74c3c'
    }
  }
}
