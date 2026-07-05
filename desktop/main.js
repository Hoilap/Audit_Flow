const { app, BrowserWindow } = require('electron')
const path = require('path')
const { spawn, execSync } = require('child_process')

let backend = null

/**
 * 清理占用指定端口的进程（Windows 专用）。
 * 通过 netstat 查找 PID，再用 taskkill 终止。
 */
function killProcessOnPort(port) {
  try {
    const stdout = execSync(`netstat -ano | findstr :${port}`, { encoding: 'utf-8' })
    const lines = stdout.trim().split('\n')
    const pids = new Set()
    for (const line of lines) {
      const parts = line.trim().split(/\s+/)
      const pid = parts[parts.length - 1]
      if (pid && pid !== '0') pids.add(pid)
    }
    for (const pid of pids) {
      try {
        execSync(`taskkill /F /PID ${pid}`, { stdio: 'ignore' })
        console.log(`[main] killed process ${pid} on port ${port}`)
      } catch (e) {
        // 进程可能已退出，忽略
      }
    }
  } catch (e) {
    // netstat 无输出说明端口未被占用，无需处理
  }
}

function startBackend() {
  // 启动前先清理端口占用，避免 WinError 10013
  killProcessOnPort(8001)
  const py = process.platform === 'win32' ? 'python' : 'python3'
  backend = spawn(py, ['-m', 'desktop.api'], { cwd: path.join(__dirname, '..') })
  backend.stdout.on('data', (data) => console.log(`[api] ${data}`))
  backend.stderr.on('data', (data) => console.error(`[api-err] ${data}`))
}

function createWindow () {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: true,
      contextIsolation: false,
    }
  })

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'))

  // Prevent accidental navigation from file drops or link clicks
  win.webContents.on('will-navigate', (e) => e.preventDefault())
}

app.whenReady().then(() => {
  startBackend()
  createWindow()
  app.on('activate', function () {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', function () {
  if (backend) backend.kill()
  if (process.platform !== 'darwin') app.quit()
})
