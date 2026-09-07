/**
 * Electron preload script.
 *
 * Prevents Electron's default behaviour of navigating to dropped files,
 * which would otherwise kill the app when the user drops a file outside
 * the designated drop zone.
 */

// Safety net: prevent file drops from navigating the webContents.
// The container-level drop handler in agentActions.js processes legitimate
// drops on the filetree panel; this document-level handler catches any
// stray drops that land outside the drop zone.
window.addEventListener('dragover', (e) => e.preventDefault())
window.addEventListener('drop', (e) => e.preventDefault())

// 暴露依赖安装进度给渲染进程（contextIsolation: false 下直接挂到 window）。
// 首次运行装依赖耗时几分钟，渲染进程据此显示"正在安装"遮罩，避免窗口看似卡死。
const { ipcRenderer } = require('electron')
window.electronAPI = {
  onDependencyProgress(callback) {
    ipcRenderer.on('dependency-install-progress', (_event, data) => callback(data))
  },
  getInstallStatus() {
    return ipcRenderer.invoke('get-install-status')
  },
}
