const { app, BrowserWindow, dialog, ipcMain } = require('electron')
const path = require('path')
const fs = require('fs')
const { spawn, execSync } = require('child_process')

let backend = null
let backendStartedAt = 0
let mainWindow = null
let installProgress = { state: 'idle', progress: 0, message: '' }

function broadcast(channel, payload) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (win && !win.isDestroyed()) win.webContents.send(channel, payload)
  }
}

// 渲染进程加载晚于主进程时，通过 invoke 主动查询当前安装状态，避免漏掉进度事件
ipcMain.handle('get-install-status', () => installProgress)

// ── 运行模式与路径解析 ──────────────────────────────────────────
// 开发模式：项目根 = desktop/ 的上级目录，Python 用 .venv
// 打包模式：resources/app 为后端源码，resources/runtime/python 为内置运行时，
//           用户数据（inputs/outputs/db/config/.env/日志）在 app.getPath('userData')

const IS_PACKAGED = app.isPackaged

const DEV_ROOT = path.join(__dirname, '..')
const RESOURCES_DIR = IS_PACKAGED ? process.resourcesPath : DEV_ROOT
const BACKEND_SRC_DIR = path.join(RESOURCES_DIR, IS_PACKAGED ? 'app' : '.')
const RUNTIME_PYTHON_DIR = path.join(RESOURCES_DIR, 'runtime', 'python')
const WHEELS_DIR = path.join(RESOURCES_DIR, 'wheels')
const DATA_DIR = process.env.AUDIT_WORKFLOW_DATA_DIR || (IS_PACKAGED
  ? app.getPath('userData') // %APPDATA%\audit-workflow-desktop（可写，升级不丢失）
  : DEV_ROOT)

// ── bootstrap 日志：打包后 GUI 无控制台，安装/启动失败必须落盘才可排查 ──
function bootstrapLog(msg) {
  console.log(msg)
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true })
    fs.appendFileSync(path.join(DATA_DIR, 'bootstrap.log'),
      `[${new Date().toISOString()}] ${msg}\n`)
  } catch (e) { /* 日志失败不影响主流程 */ }
}

// ── 清理占用指定端口的进程（Windows 专用）──────────────────────

// 杀掉所有命令行含 desktop.api 的 python 进程（含 uvicorn reload 的孤儿 worker）。
// 背景：dev 模式 reload=True 时 worker 继承父进程 socket；父进程死后 netstat 把
// 端口归属到已死 PID，按端口 taskkill 打不中真正存活的 worker，必须按命令行兜底。
function killOrphanBackends() {
  if (process.platform !== 'win32') return
  try {
    execSync(
      'powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \\"Name=\'python.exe\'\\" | Where-Object { $_.CommandLine -match \'desktop\\.api\' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
      { stdio: 'ignore' }
    )
    console.log('[main] killed orphan desktop.api backends')
  } catch (e) {
    // 无匹配进程时忽略
  }
}

function killProcessOnPort(port) {
  if (process.platform !== 'win32') return
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
        execSync(`taskkill /F /T /PID ${pid}`, { stdio: 'ignore' })
        console.log(`[main] killed process ${pid} on port ${port}`)
      } catch (e) {
        // 进程可能已退出，忽略
      }
    }
  } catch (e) {
    // netstat 无输出说明端口未被占用，无需处理
  }
  // 兜底：netstat 归属到已死 PID 时，按命令行清掉残留的 desktop.api worker
  killOrphanBackends()
}

// ── Python 解释器定位 ────────────────────────────────────────────

function resolvePython() {
  // 1) 打包模式：优先使用内置运行时 resources/runtime/python
  if (IS_PACKAGED) {
    const bundledPy = process.platform === 'win32'
      ? path.join(RUNTIME_PYTHON_DIR, 'python.exe')
      : path.join(RUNTIME_PYTHON_DIR, 'bin', 'python3')
    if (fs.existsSync(bundledPy)) return bundledPy
    console.error('[main] 内置 Python 运行时缺失，回退系统 python：', bundledPy)
    return process.platform === 'win32' ? 'python' : 'python3'
  }
  // 2) 开发模式：优先 .venv
  const venvPy = process.platform === 'win32'
    ? path.join(DEV_ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(DEV_ROOT, '.venv', 'bin', 'python3')
  return fs.existsSync(venvPy) ? venvPy : (process.platform === 'win32' ? 'python' : 'python3')
}

// ── 依赖 bootstrap（打包模式，仅首次运行执行）───────────────────

function requirementsHash() {
  const reqPath = path.join(BACKEND_SRC_DIR, 'requirements.txt')
  const content = fs.existsSync(reqPath) ? fs.readFileSync(reqPath, 'utf-8') : ''
  // 简单内容指纹，requirements 变化后会重新安装
  let h = 0
  for (let i = 0; i < content.length; i++) {
    h = ((h << 5) - h + content.charCodeAt(i)) | 0
  }
  return String(h)
}

function installDependencies(py, onState) {
  return new Promise((resolve) => {
    const marker = path.join(RUNTIME_PYTHON_DIR, '.deps-installed')
    const hash = requirementsHash()
    try {
      if (fs.existsSync(marker) && fs.readFileSync(marker, 'utf-8').trim() === hash) {
        console.log('[bootstrap] 依赖已就绪，跳过安装')
        resolve(true)
        return
      }
    } catch (e) { /* 读取失败则重新安装 */ }

    bootstrapLog('[bootstrap] 首次运行：正在安装 Python 依赖（离线 wheels），可能需要几分钟...')
    const reqPath = path.join(BACKEND_SRC_DIR, 'requirements.txt')
    const args = ['-m', 'pip', 'install', '--no-index', '--find-links', WHEELS_DIR, '-r', reqPath, '--no-warn-script-location']
    // 异步 spawn：避免阻塞主线程导致窗口假死，装依赖期间界面保持响应并显示进度
    const child = spawn(py, args, { windowsHide: true })
    if (onState) onState('installing')
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => { stdout += String(d) })
    child.stderr.on('data', (d) => { stderr += String(d) })
    child.on('error', (err) => {
      bootstrapLog('[bootstrap] 依赖安装失败：' + err.message)
      resolve(false)
    })
    child.on('close', (code) => {
      if (code !== 0) {
        bootstrapLog('[bootstrap] 依赖安装失败：exit code ' + code)
        bootstrapLog('[bootstrap] pip stdout:\n' + stdout)
        bootstrapLog('[bootstrap] pip stderr:\n' + stderr)
        resolve(false)
        return
      }
      try { fs.writeFileSync(marker, hash) } catch (e) { /* 标记写入失败不影响运行 */ }
      bootstrapLog('[bootstrap] 依赖安装完成')
      if (onState) onState('done')
      resolve(true)
    })
  })
}

// ── 数据目录初始化（打包模式：把随包的配置模板种到数据目录）─────

function seedDataDir() {
  if (!IS_PACKAGED) return
  fs.mkdirSync(DATA_DIR, { recursive: true })

  // 0) inputs/、outputs/ 根目录：工作流的输入/输出区（公司/任务 两级结构）
  for (const base of ['inputs', 'outputs']) {
    fs.mkdirSync(path.join(DATA_DIR, base), { recursive: true })
  }

  // 1) config/：逐文件补齐（用户改过的配置不会被覆盖）
  const shippedConfigDir = path.join(BACKEND_SRC_DIR, 'config')
  const dataConfigDir = path.join(DATA_DIR, 'config')
  if (fs.existsSync(shippedConfigDir)) {
    fs.mkdirSync(dataConfigDir, { recursive: true })
    for (const f of fs.readdirSync(shippedConfigDir)) {
      const src = path.join(shippedConfigDir, f)
      const dst = path.join(dataConfigDir, f)
      if (!fs.existsSync(dst)) {
        fs.copyFileSync(src, dst)
        console.log(`[bootstrap] seeded config: ${f}`)
      }
    }
  }

  // 2) .env：从 .env.example 种子（存在则不覆盖）
  const envExample = path.join(BACKEND_SRC_DIR, '.env.example')
  const envTarget = path.join(DATA_DIR, '.env')
  if (fs.existsSync(envExample) && !fs.existsSync(envTarget)) {
    fs.copyFileSync(envExample, envTarget)
    console.log('[bootstrap] seeded .env from .env.example')
  }
}

// ── 后端启动 ─────────────────────────────────────────────────────

async function startBackend() {
  // 启动前先清理端口占用，避免 WinError 10013
  killProcessOnPort(8001)

  seedDataDir()
  const py = resolvePython()
  console.log(`[main] using Python: ${py}`)
  console.log(`[main] backend src: ${BACKEND_SRC_DIR}`)
  console.log(`[main] data dir:    ${DATA_DIR}`)

  // 依赖安装失败必须终止：否则后端起不来，前端永远卡在加载页，
  // 且 Ctrl+R 只刷新页面、不会重启后端，用户无法自愈
  if (IS_PACKAGED) {
    const ok = await installDependencies(py, (state) => {
      installProgress = { state, progress: state === 'done' ? 100 : 0, message: state === 'installing' ? '正在安装 Python 依赖...' : '' }
      broadcast('dependency-install-progress', installProgress)
    })
    if (!ok) {
      installProgress = { state: 'failed', progress: 0, message: '依赖安装失败' }
      broadcast('dependency-install-progress', installProgress)
      const msg = 'Python 依赖安装失败，应用无法启动。\n\n' +
        `详细原因请查看日志：\n${path.join(DATA_DIR, 'bootstrap.log')}\n\n` +
        '修复后请完全退出应用（而非刷新页面）再重新打开。'
      bootstrapLog('[main] ' + msg)
      dialog.showErrorBox('AuditWorkflow 启动失败', msg)
      app.quit()
      return
    }
  }

  const env = Object.assign({}, process.env, {
    PYTHONPATH: BACKEND_SRC_DIR,          // 让 python -m desktop.api 从资源目录导入
    AUDIT_WORKFLOW_DATA_DIR: DATA_DIR,    // 后端把 inputs/outputs/db/config 都放这里
    AUDIT_DEV: IS_PACKAGED ? '' : '1',
    PYTHONUNBUFFERED: '1',
    // 防止用户机器上的 PYTHONHOME/PYTHONPATH 污染内置运行时
    PYTHONHOME: '',
  })
  delete env.PYTHONHOME

  backend = spawn(py, ['-m', 'desktop.api'], { cwd: DATA_DIR, env, windowsHide: true })
  backendStartedAt = Date.now()

  // 后端 stdout/stderr 写入 backend.log（含 HTTP 500 的 Python traceback），
  // 否则打包后的 GUI 应用没有控制台，报错全部丢失
  const logStream = fs.createWriteStream(path.join(DATA_DIR, 'backend.log'), { flags: 'a' })
  const logLine = (tag, data) => {
    const text = String(data)
    logStream.write(`[${new Date().toISOString()}] [${tag}] ${text}`)
    console.log(`[${tag}] ${text}`)
  }
  backend.stdout.on('data', (data) => logLine('api', data))
  backend.stderr.on('data', (data) => logLine('api-err', data))
  backend.on('exit', (code) => {
    logLine('api', `exited with code ${code}\n`)
    // 启动后 30 秒内就退出 = 启动失败（如缺依赖/端口占用）。
    // 前端只会一直转圈，刷新也没用，必须弹窗告知用户并指路日志。
    if (IS_PACKAGED && code !== 0 && Date.now() - backendStartedAt < 30000) {
      dialog.showErrorBox('AuditWorkflow 后端启动失败',
        `后端进程已退出（code ${code}），界面将无法响应，刷新页面无法解决。\n\n` +
        `详细原因请查看日志：\n${path.join(DATA_DIR, 'backend.log')}\n\n` +
        '修复后请完全退出应用再重新打开。')
    }
  })
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

  mainWindow = win
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'))

  // Prevent accidental navigation from file drops or link clicks
  win.webContents.on('will-navigate', (e) => e.preventDefault())
}

// ── 单实例锁：防止应用被重复打开 ─────────────────────────────────
// 第二个实例获取锁失败直接退出；已运行的实例收到 second-instance 事件，
// 唤起并聚焦已有窗口（后端 8001 端口也只由首个实例占用）。
const gotSingleInstanceLock = app.requestSingleInstanceLock()

if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    const win = BrowserWindow.getAllWindows()[0]
    if (win) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  app.whenReady().then(() => {
    // 先建窗口再启动后端：首次运行安装依赖可能耗时几分钟，
    // 若先阻塞安装再建窗口，用户看不到任何界面会误以为卡死而强杀，
    // 导致依赖装到一半、后端永远起不来（刷新页面无法恢复）
    createWindow()
    startBackend()
    app.on('activate', function () {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  app.on('window-all-closed', function () {
    if (backend) {
      // Windows 上 backend.kill() 只终止直接子进程；dev 模式 uvicorn reload 的
      // worker 孙子进程会存活并继续持有 8001，必须用 taskkill /T 杀整棵进程树
      if (process.platform === 'win32') {
        try {
          execSync(`taskkill /F /T /PID ${backend.pid}`, { stdio: 'ignore' })
        } catch (e) { /* 进程可能已退出 */ }
      } else {
        backend.kill()
      }
    }
    if (process.platform !== 'darwin') app.quit()
  })
}
