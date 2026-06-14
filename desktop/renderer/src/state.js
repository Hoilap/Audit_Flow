import { workflowTasks } from './config.js'

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
  detectMethod: 'llm',}

export function activeTask() {
  return workflowTasks.find((task) => task.id === state.activeTaskId) || workflowTasks[0]
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
