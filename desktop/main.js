const { app, BrowserWindow } = require('electron')
const path = require('path')
const { spawn } = require('child_process')

let backend = null

function startBackend() {
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
    }
  })

  win.loadFile(path.join(__dirname, 'renderer', 'index.html'))
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
