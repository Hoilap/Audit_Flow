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
}

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
