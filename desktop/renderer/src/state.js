import { customWorkflowTask, workflowTasks } from './config.js'

export const state = {
  files: [],
  activePage: 'dashboard',
  activeTaskId: workflowTasks[0].id,
  activeStepIndex: 0,
  stepRuns: {},
  status: 'Idle',
  startedAt: null,
  timer: null,
  chatHidden: false,
  sidebarHidden: false,
  /** @type {Array<{id:number,task_name:string,customer_name:string,status:string,created_at:string,responsible_person:string,risk:string}>} */
  projects: [],
  /** 当前在 Agent 页面选中的项目 ID；null 表示自定义模式 */
  activeProjectId: null,
  /** 如果 activeProjectId 为 null，用户手动输入的客户名称 */
  customCustomerName: '',
  /** 如果 activeProjectId 为 null，用户手动选择的任务名称 */
  customTaskName: workflowTasks[0].name,  /** Detect 步骤的识别方式: 'llm' | 'script' */
  detectMethod: 'llm',
  /** 右栏区块折叠状态，key 为 section id，value 为 true 表示已折叠 */
  collapsedSections: { 'git-log': true },
  // ── Agent 对话面板状态 ──
  agentConversations: [],       // [{id, title, created_at, message_count}]
  activeAgentConvId: null,      // 当前对话 ID
  agentMessages: [],            // 当前对话的消息列表
  agentIsRunning: false,        // Agent 是否正在执行
  agentStreamingText: '',       // 流式输出累积的文本
  agentHasStreamed: false,      // 本轮是否已通过 SSE 收到文本（防止 HTTP 兜底重复）
  // ── Agent 输出路径选择器 ──
  agentCustomerName: '',        // Agent 页面当前选中的客户名
  agentTaskName: '',            // Agent 页面当前选中的任务名
  agentProjectOptions: [],      // 从 DB 加载的项目列表（供下拉使用）
  agentTaskDefinitions: [],     // 从 task_definitions 表加载的任务类型（name ↔ dir_name）
  // ── 设置页 LLM 配置 ──
  llmProviders: [],             // [{name, model, base_url, api_key_masked, api_key_set, api_key_env, api_key_source}]
  llmConfigDefault: '',         // 默认 provider 名称
  llmConfigPath: '',            // YAML 配置文件路径
}

export function activeTask() {
  return workflowTasks.find((task) => task.id === state.activeTaskId) || customWorkflowTask
}

export function activeStep() {
  return activeTask().steps[state.activeStepIndex]
}

export function taskRunKey(taskId = state.activeTaskId, stepId = activeStep()?.id) {
  return `${taskId}:${stepId}`
}

export function markStep(taskId, stepId, status, result = null) {
  state.stepRuns[taskRunKey(taskId, stepId)] = { status, result, ranAt: new Date().toISOString() }
}

export function stepStatus(taskId, stepId) {
  return state.stepRuns[taskRunKey(taskId, stepId)]?.status || 'idle'
}

/**
 * 根据任务名称（task_name）查找匹配的 workflowTask。
 * 如果找不到，返回 null 表示自定义工作流。
 */
export function findWorkflowTaskByName(taskName) {
  return workflowTasks.find((t) => t.name === taskName) || null
}

/**
 * 获取当前项目/客户+任务对应的输出根目录。
 * 格式: outputs/{customerName}/{taskDirName}
 * @returns {string|null}
 */
export function getProjectBasePath() {
  const wfTask = findWorkflowTaskByName(state.customTaskName)
  if (!wfTask || !state.customCustomerName) return null
  return `outputs/${state.customCustomerName}/${wfTask.dirName}`
}

/**
 * 将步骤/复核的相对路径解析为完整项目路径。
 * @param {string} relativePath - 如 'matches/matches.csv'
 * @returns {string|null} 如 'outputs/桂平金山/bank_ledger_match/matches/matches.csv'
 */
export function resolveProjectPath(relativePath) {
  const base = getProjectBasePath()
  if (!base) return null
  return `${base}/${relativePath}`
}
