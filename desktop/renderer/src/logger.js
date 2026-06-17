/**
 * 前端操作日志工具
 * - 在控制台输出带时间戳的分级日志
 * - 可选地将日志条目推送到 #chat 面板（通过 addMessage）
 */

const LEVELS = { debug: 0, info: 1, warn: 2, error: 3 }
let currentLevel = 'info'

function _ts() {
  return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function _log(level, tag, ...args) {
  if (LEVELS[level] < LEVELS[currentLevel]) return
  const prefix = `[${_ts()}] [${level.toUpperCase()}] ${tag ? tag + ' |' : ''}`
  const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log
  fn(prefix, ...args)
}

export const logger = {
  debug: (tag, ...args) => _log('debug', tag, ...args),
  info:  (tag, ...args) => _log('info',  tag, ...args),
  warn:  (tag, ...args) => _log('warn',  tag, ...args),
  error: (tag, ...args) => _log('error', tag, ...args),
  setLevel: (level) => { if (LEVELS[level] !== undefined) currentLevel = level },
}
