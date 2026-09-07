/**
 * agentActions.js — Agent 对话面板的前端逻辑。
 *
 * 职责：消息发送、SSE agent_step 事件处理、对话 UI 渲染、会话管理。
 */

import { api } from './api.js'
import { state } from './state.js'
import { $, escapeHtml } from './dom.js'
import { logger } from './logger.js'

// ── 发送消息 ──

export async function sendAgentMessage() {
  const promptEl = $('#agent-prompt')
  const message = promptEl?.value?.trim()
  if (!message || state.agentIsRunning) return

  state.agentIsRunning = true
  promptEl.value = ''
  setSendButtonDisabled(true)

  // 立即渲染用户气泡
  addAgentMessageToUI({
    role: 'user',
    type: 'text',
    content: message,
    timestamp: new Date().toISOString(),
  })

  setAgentLoopStatus('Agent 思考中...')

  try {
    const result = await api.agentChat(
      message,
      state.activeAgentConvId || '',
      state.agentCustomerName || '',
      state.agentTaskName || ''
    )

    if (result.conversation_id) {
      state.activeAgentConvId = result.conversation_id
    }

    // SSE 已经推送了大部分内容，这里作为兜底
    if (result.ok && result.response) {
      // 只有 SSE 没推到任何文本时才补一条（agentHasStreamed 在整个轮次中保持 true）
      if (!state.agentHasStreamed) {
        addAgentMessageToUI({
          role: 'assistant',
          type: 'text',
          content: result.response,
          timestamp: new Date().toISOString(),
          metadata: { usage: result.usage },
        })
      }
    } else if (!result.ok) {
      addAgentMessageToUI({
        role: 'assistant',
        type: 'error',
        content: result.error || '未知错误',
        timestamp: new Date().toISOString(),
      })
    }

    // 刷新会话列表
    await loadAgentConversations()
  } catch (error) {
    logger.error('agentChat', error.message)
    addAgentMessageToUI({
      role: 'assistant',
      type: 'error',
      content: `请求失败: ${error.message}`,
      timestamp: new Date().toISOString(),
    })
  } finally {
    state.agentIsRunning = false
    state.agentStreamingText = ''
    state.agentHasStreamed = false
    setSendButtonDisabled(false)
    setAgentLoopStatus('')
    finalizeStreamingMessage()
  }
}

// ── SSE 事件处理 ──

export function handleAgentStepEvent(data) {
  const { step_type, conversation_id } = data

  // 只处理当前活跃会话的事件
  if (conversation_id && state.activeAgentConvId &&
      conversation_id !== state.activeAgentConvId) return

  switch (step_type) {
    case 'thinking':
      addAgentMessageToUI({
        role: 'assistant',
        type: 'thinking',
        content: data.content || '正在分析...',
        timestamp: new Date().toISOString(),
      })
      break

    case 'tool_call':
      addAgentMessageToUI({
        role: 'assistant',
        type: 'tool_call',
        content: `调用工具: ${data.tool_name}`,
        timestamp: new Date().toISOString(),
        metadata: {
          tool_name: data.tool_name,
          arguments: data.arguments,
          code_preview: data.code_preview,
        },
      })
      setAgentLoopStatus(`执行 ${data.tool_name}...`)
      break

    case 'tool_result': {
      const icon = data.success ? '✓' : '✗'
      addAgentMessageToUI({
        role: 'system',
        type: 'tool_result',
        content: data.result_preview || '',
        timestamp: new Date().toISOString(),
        metadata: {
          tool_name: data.tool_name,
          success: data.success,
          stdout_preview: data.stdout_preview,
          stderr_preview: data.stderr_preview,
        },
      })
      // 用图标更新状态
      setAgentLoopStatus(`${data.tool_name} ${icon}`)
      break
    }

    case 'text_delta':
      state.agentStreamingText += (data.delta || '')
      state.agentHasStreamed = true
      updateStreamingMessage(state.agentStreamingText)
      break

    case 'finalize_stream':
      // 终结当前流式文本气泡，后续 text_delta 会创建新气泡
      finalizeStreamingMessage()
      state.agentStreamingText = ''
      break

    case 'code_output':
      addAgentMessageToUI({
        role: 'system',
        type: 'code_output',
        content: '',
        timestamp: new Date().toISOString(),
        metadata: {
          returncode: data.returncode,
          stdout: data.stdout,
          stderr: data.stderr,
        },
      })
      break

    case 'error':
      addAgentMessageToUI({
        role: 'assistant',
        type: 'error',
        content: data.content || '未知错误',
        timestamp: new Date().toISOString(),
        metadata: { error_type: data.error_type },
      })
      break

    case 'done':
      finalizeStreamingMessage()
      state.agentStreamingText = ''
      setAgentLoopStatus('完成')
      break
  }
}

// ── UI 渲染 ──

function addAgentMessageToUI(msg) {
  const container = $('#agent-messages')
  if (!container) return

  // 移除欢迎消息
  const welcome = container.querySelector('.agent-welcome')
  if (welcome) welcome.remove()

  const el = document.createElement('div')
  el.className = `agent-msg agent-msg-${msg.type}`

  const time = new Date(msg.timestamp).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })

  let headerLeft = ''
  let body = ''

  switch (msg.type) {
    case 'text':
      headerLeft = msg.role === 'user' ? '你' : 'Agent'
      body = `<div class="agent-msg-text">${escapeHtml(msg.content)}</div>`
      break

    case 'thinking':
      headerLeft = '思考'
      body = `<div class="agent-msg-thinking">${escapeHtml(msg.content)}</div>`
      break

    case 'tool_call':
      headerLeft = msg.metadata?.tool_name || 'tool'
      body = `<details class="agent-msg-tool"><summary>参数</summary><pre>${escapeHtml(JSON.stringify(msg.metadata?.arguments || {}, null, 2))}</pre>${msg.metadata?.code_preview ? `<pre class="code-block">${escapeHtml(msg.metadata.code_preview)}</pre>` : ''}</details>`
      break

    case 'tool_result': {
      const ok = msg.metadata?.success
      headerLeft = `${ok ? '✓' : '✗'} ${msg.metadata?.tool_name || 'tool'} 结果`
      body = `<div class="agent-msg-result ${ok ? 'success' : 'failure'}">
        ${escapeHtml(msg.content)}
        ${msg.metadata?.stdout_preview ? `<pre>${escapeHtml(msg.metadata.stdout_preview)}</pre>` : ''}
        ${msg.metadata?.stderr_preview ? `<pre class="error-text">${escapeHtml(msg.metadata.stderr_preview)}</pre>` : ''}
      </div>`
      break
    }

    case 'code_output': {
      const rc = msg.metadata?.returncode ?? -1
      headerLeft = rc === 0 ? '执行成功' : `执行失败 (code ${rc})`
      body = `<div class="agent-msg-code">
        ${msg.metadata?.stdout ? `<div class="label">stdout</div><pre>${escapeHtml(msg.metadata.stdout)}</pre>` : ''}
        ${msg.metadata?.stderr ? `<div class="label">stderr</div><pre class="error-text">${escapeHtml(msg.metadata.stderr)}</pre>` : ''}
      </div>`
      break
    }

    case 'error':
      headerLeft = '错误'
      body = `<div class="agent-msg-error">${escapeHtml(msg.content)}</div>`
      break

    default:
      headerLeft = msg.role || 'system'
      body = `<div>${escapeHtml(msg.content)}</div>`
  }

  el.innerHTML = `<div class="agent-msg-head"><span>${headerLeft}</span><span class="agent-msg-time">${time}</span></div><div class="agent-msg-body">${body}</div>`

  container.appendChild(el)
  el.scrollIntoView({ behavior: 'smooth', block: 'end' })
}

function updateStreamingMessage(accumulatedText) {
  const container = $('#agent-messages')
  if (!container) return

  // 查找或创建流式消息元素
  let streamingEl = container.querySelector('[data-streaming="true"]')
  if (!streamingEl) {
    addAgentMessageToUI({
      role: 'assistant',
      type: 'text',
      content: accumulatedText,
      timestamp: new Date().toISOString(),
    })
    // 标记为流式
    const lastMsg = container.lastElementChild
    if (lastMsg) lastMsg.dataset.streaming = 'true'
    return
  }

  const textDiv = streamingEl.querySelector('.agent-msg-text')
  if (textDiv) {
    textDiv.textContent = accumulatedText
  }
  streamingEl.scrollIntoView({ behavior: 'smooth', block: 'end' })
}

function finalizeStreamingMessage() {
  const container = $('#agent-messages')
  if (!container) return
  const el = container.querySelector('[data-streaming="true"]')
  if (el) el.removeAttribute('data-streaming')
}

function setAgentLoopStatus(text) {
  const el = $('#agent-loop-status')
  if (el) el.textContent = text
}

function setSendButtonDisabled(disabled) {
  const btn = $('#agent-send')
  if (btn) btn.disabled = disabled
}

// ── 会话管理 ──

export async function loadAgentConversations() {
  try {
    const data = await api.agentListConversations()
    state.agentConversations = data.conversations || []
    renderAgentConvSelect()
  } catch (e) {
    // 静默失败
  }
}

export async function loadAgentConversation(convId) {
  if (!convId) return
  try {
    const data = await api.agentGetConversation(convId)
    state.activeAgentConvId = convId
    state.agentMessages = data.messages || []
    renderAgentMessages(data.messages || [])
  } catch (e) {
    logger.error('loadConv', e.message)
  }
}

export async function newAgentConversation() {
  try {
    const data = await api.agentNewConversation()
    state.activeAgentConvId = data.conversation_id
    state.agentMessages = []
    const container = $('#agent-messages')
    if (container) {
      container.innerHTML = `<div class="agent-welcome">
        <h2>审计 Agent 助手</h2>
        <p class="subtle">新对话已创建。输入您的问题开始对话。</p>
      </div>`
    }
    await loadAgentConversations()
  } catch (e) {
    // 静默失败
  }
}

export async function deleteAgentConversation() {
  if (!state.activeAgentConvId) return
  if (!confirm('确定要删除当前对话吗？此操作不可恢复。')) return
  try {
    await api.agentDeleteConversation(state.activeAgentConvId)
    state.activeAgentConvId = null
    state.agentMessages = []
    const container = $('#agent-messages')
    if (container) {
      container.innerHTML = `<div class="agent-welcome">
        <h2>审计 Agent 助手</h2>
        <p class="subtle">对话已删除。选择其他对话或新建一个。</p>
      </div>`
    }
    await loadAgentConversations()
  } catch (e) {
    logger.error('deleteConv', e.message)
  }
}

function renderAgentConvSelect() {
  const sel = $('#agent-conv-select')
  if (!sel) return
  const convs = state.agentConversations || []
  sel.innerHTML = '<option value="">-- 选择历史对话 --</option>' +
    convs.map(c => {
      const selected = c.id === state.activeAgentConvId ? 'selected' : ''
      const title = c.title || `对话 ${c.id.slice(0, 8)}`
      return `<option value="${c.id}" ${selected}>${escapeHtml(title)} (${c.message_count || 0} 条)</option>`
    }).join('')

  // 切换删除按钮可见性
  const delBtn = $('#agent-delete-conv')
  if (delBtn) delBtn.style.display = state.activeAgentConvId ? '' : 'none'
}

function renderAgentMessages(messages) {
  const container = $('#agent-messages')
  if (!container) return
  container.innerHTML = ''
  if (messages.length === 0) {
    container.innerHTML = `<div class="agent-welcome">
      <h2>审计 Agent 助手</h2>
      <p class="subtle">输入您的问题开始对话。</p>
    </div>`
    return
  }
  messages.forEach(msg => addAgentMessageToUI(msg))
}

// ── 文件树面板 ──

export async function renderAgentFileTree() {
  const container = $('#agent-filetree')
  if (!container) return
  container.innerHTML = '<div class="subtle" style="padding:16px;text-align:center;">加载中...</div>'

  try {
    const [inputsRes, outputsRes] = await Promise.all([
      api.listFiles('inputs').catch(() => ({ files: [] })),
      api.listFiles('outputs').catch(() => ({ files: [] })),
    ])
    const inputsFiles = (inputsRes.files || []).filter(f => !f.endsWith('/') && !f.endsWith('.pyc'))
    const outputsFiles = (outputsRes.files || []).filter(f => !f.endsWith('/') && !f.endsWith('.pyc'))

    let html = ''
    if (inputsFiles.length > 0) {
      html += '<div class="tree-root"><span class="tree-icon">📂</span><strong>inputs</strong></div>'
      html += renderAgentTreeNodes(inputsFiles, 'inputs')
    } else {
      html += '<div class="tree-root"><span class="tree-icon">📂</span><strong>inputs</strong> <span class="subtle">(空)</span></div>'
    }
    if (outputsFiles.length > 0) {
      html += '<div class="tree-root" style="margin-top:8px;"><span class="tree-icon">📂</span><strong>outputs</strong></div>'
      html += renderAgentTreeNodes(outputsFiles, 'outputs')
    } else {
      html += '<div class="tree-root" style="margin-top:8px;"><span class="tree-icon">📂</span><strong>outputs</strong> <span class="subtle">(空)</span></div>'
    }
    container.innerHTML = html

    bindAgentFileTreeEvents(container)
    bindDragAndDrop(container)

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

function renderAgentTreeNodes(files, basePath) {
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

function bindAgentFileTreeEvents(container) {
  // 文件夹折叠/展开
  container.querySelectorAll('.tree-folder').forEach(folder => {
    folder.style.cursor = 'pointer'
    folder.addEventListener('click', () => {
      const expanded = folder.dataset.expand === 'true'
      folder.dataset.expand = expanded ? 'false' : 'true'
      const icon = folder.querySelector('.tree-icon')
      if (icon) icon.textContent = expanded ? '📁' : '📂'
      // 切换后续兄弟元素的可见性
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

function bindDragAndDrop(container) {
  // Prevent duplicate listener binding on re-renders
  if (container.dataset.dndBound) return
  container.dataset.dndBound = 'true'

  // ── dragover: 只在拖文件时显示反馈 ──
  container.addEventListener('dragover', (e) => {
    e.preventDefault()
    e.stopPropagation()
    if (!e.dataTransfer.types.includes('Files')) return

    container.classList.add('drag-over')
    if (!container.querySelector('.drop-hint')) {
      const hint = document.createElement('div')
      hint.className = 'drop-hint'
      hint.textContent = '释放文件以导入 inputs/'
      container.appendChild(hint)
    }

    // 高亮光标下方的文件夹
    const folder = e.target.closest?.('.tree-folder')
    container.querySelectorAll('.tree-folder.drag-target')
      .forEach(f => { if (f !== folder) f.classList.remove('drag-target') })
    if (folder) folder.classList.add('drag-target')
  })

  // ── dragleave: 仅在光标真正离开容器时移除反馈 ──
  container.addEventListener('dragleave', (e) => {
    // relatedTarget 是光标移向的元素；如果仍在容器内部则忽略
    if (container.contains(e.relatedTarget)) return
    container.classList.remove('drag-over')
    container.querySelectorAll('.tree-folder.drag-target')
      .forEach(f => f.classList.remove('drag-target'))
    const hint = container.querySelector('.drop-hint')
    if (hint) hint.remove()
  })

  // ── drop: 直接导入到 inputs/，无需弹窗 ──
  container.addEventListener('drop', async (e) => {
    e.preventDefault()
    e.stopPropagation()

    // 清理视觉反馈
    container.classList.remove('drag-over')
    container.querySelectorAll('.tree-folder.drag-target')
      .forEach(f => f.classList.remove('drag-target'))
    const hint = container.querySelector('.drop-hint')
    if (hint) hint.remove()

    if (!e.dataTransfer.types.includes('Files')) return

    const files = Array.from(e.dataTransfer.files)
    if (files.length === 0) return

    // Electron 中 File 对象有 path 属性指向原生文件系统路径
    const filePaths = files.map(f => f.path).filter(Boolean)

    if (filePaths.length === 0) {
      setAgentLoopStatus('无法获取文件路径（请在 Electron 中操作）')
      setTimeout(() => setAgentLoopStatus(''), 3000)
      return
    }

    const dest = 'inputs/'
    setAgentLoopStatus(`正在导入 ${filePaths.length} 个项目...`)

    let uploaded = 0
    let failed = 0
    for (const srcPath of filePaths) {
      try {
        await api.copyFromPath(srcPath, dest)
        uploaded++
      } catch (err) {
        failed++
        logger.error('drop-import', `${srcPath}: ${err.message}`)
      }
    }

    const msg = failed === 0
      ? `已导入 ${uploaded} 个项目到 inputs/`
      : `导入完成: ${uploaded} 成功, ${failed} 失败`
    setAgentLoopStatus(msg)
    setTimeout(() => setAgentLoopStatus(''), 4000)

    await renderAgentFileTree()
  })
}

// ── 输出路径选择器（文件树下方下拉菜单） ──

/**
 * 加载项目数据、填充客户 datalist、恢复已有选择。
 * 在 Agent 页面激活时调用。
 */
export async function initAgentOutputSelector() {
  try {
    const [projData, tdData] = await Promise.all([
      api.listProjects(),
      api.listTaskDefinitions(),
    ])
    state.agentProjectOptions = projData.projects || []
    state.agentTaskDefinitions = tdData.definitions || []
  } catch {
    state.agentProjectOptions = []
    state.agentTaskDefinitions = []
  }
  renderAgentCustomerDatalist()
  const custInput = $('#agent-customer-input')
  const taskInput = $('#agent-task-input')
  if (custInput) custInput.value = state.agentCustomerName
  if (taskInput) taskInput.value = state.agentTaskName
  renderAgentTaskDatalist(state.agentCustomerName)
  updateAgentOutputPath()
}

/** 从 agentProjectOptions 提取去重的客户名，写入 datalist */
function renderAgentCustomerDatalist() {
  const datalist = $('#agent-customer-list')
  if (!datalist) return
  const customers = [...new Set(
    state.agentProjectOptions.map(p => p.customer_short_name).filter(Boolean)
  )]
  datalist.innerHTML = customers.map(c => `<option value="${escapeHtml(c)}">`).join('')
}

/** 按客户名构建任务选项列表：task_definitions 的 dir_name + DB 自定义条目 */
function renderAgentTaskDatalist(customerName) {
  const datalist = $('#agent-task-list')
  if (!datalist) return
  if (!customerName) {
    datalist.innerHTML = ''
    return
  }
  // task_definitions 中的预定义任务（英文 dir_name）
  const defDirNames = new Set(state.agentTaskDefinitions.map(d => d.dir_name))
  const options = [...defDirNames]
  // DB 中该客户下不属于预定义任务的自定义条目
  state.agentProjectOptions
    .filter(p => p.customer_short_name === customerName && p.task_name)
    .forEach(p => {
      // 优先用 dir_name（来自 LEFT JOIN），其次用 task_name
      const tn = p.dir_name || p.task_name
      if (!defDirNames.has(tn) && !options.includes(tn)) {
        options.push(tn)
      }
    })
  datalist.innerHTML = options.map(t => `<option value="${escapeHtml(t)}">`).join('')
}

/** 绑定客户/任务 input 的 input 事件，实现联动和路径更新 */
export function bindAgentOutputSelectorEvents() {
  const custInput = $('#agent-customer-input')
  const taskInput = $('#agent-task-input')

  // 幂等性保护：防止多次导航导致重复绑定
  if (custInput?.dataset.selectorBound) return
  if (custInput) custInput.dataset.selectorBound = 'true'

  if (custInput) {
    custInput.addEventListener('input', () => {
      state.agentCustomerName = custInput.value.trim()
      // 客户名变化 → 刷新任务 datalist（联动）
      renderAgentTaskDatalist(state.agentCustomerName)
      updateAgentOutputPath()
    })
  }

  if (taskInput) {
    taskInput.addEventListener('input', () => {
      state.agentTaskName = taskInput.value.trim()
      updateAgentOutputPath()
    })
  }
}

/** 根据当前选中的客户名/任务名更新路径显示（路径用英文 dir_name） */
function updateAgentOutputPath() {
  const el = $('#agent-output-path')
  if (!el) return
  const c = state.agentCustomerName
  const t = resolveTaskDirName(state.agentTaskName)
  if (c && t) {
    el.textContent = `outputs/${c}/${t}/`
    el.classList.add('has-path')
  } else if (c) {
    el.textContent = `outputs/${c}/ — 请选择任务`
    el.classList.remove('has-path')
  } else {
    el.innerHTML = '<span class="subtle">outputs/ — 请先选择客户和任务</span>'
    el.classList.remove('has-path')
  }
}

/**
 * 将任务名解析为英文目录名（dir_name）。
 * 数据来源：state.agentTaskDefinitions（从 task_definitions API 加载）。
 * 支持：英文 dir_name 直传、中文 name 翻译、自定义名称原样返回。
 */
function resolveTaskDirName(name) {
  if (!name) return ''
  const defs = state.agentTaskDefinitions
  // 已经是英文 dir_name → 直接返回
  if (defs.some(d => d.dir_name === name)) return name
  // 中文 name → 翻译为英文 dir_name
  const match = defs.find(d => d.name === name)
  if (match) return match.dir_name
  // 自定义名称 → 原样返回
  return name
}
