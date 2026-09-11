<#
.SYNOPSIS
    一键打包：校验前置条件 -> electron-builder 产出 NSIS 安装包到 dist/。
.DESCRIPTION
    前置步骤（fetch-python / fetch-wheels / npm install）只需在首次或依赖
    变更后手动执行；本脚本会自动校验它们已完成。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging/build.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$desktopDir = Join-Path $projectRoot 'desktop'

# 国内网络下使用镜像加速 Electron 与 electron-builder 二进制下载
$env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'

# ── 1. 前置校验 ──────────────────────────────────────────────
$runtimePy = Join-Path $desktopDir 'runtime\python\python.exe'
if (-not (Test-Path $runtimePy)) {
    throw "内置 Python 运行时不存在。请先运行: packaging/fetch-python.ps1"
}
$wheelsDir = Join-Path $desktopDir 'wheels'
if (-not (Test-Path $wheelsDir) -or (Get-ChildItem $wheelsDir -Filter *.whl).Count -eq 0) {
    throw "离线 wheels 不存在。请先运行: packaging/fetch-wheels.ps1"
}
if (-not (Test-Path (Join-Path $desktopDir 'node_modules\electron'))) {
    throw "node_modules 未安装。请先在 desktop/ 下运行: npm install"
}

Write-Host "[build] 前置校验通过"
Write-Host "  runtime: $runtimePy"
Write-Host "  wheels : $((Get-ChildItem $wheelsDir -Filter *.whl).Count) 个"

# ── 2. electron-builder ──────────────────────────────────────
Push-Location $desktopDir
try {
    Write-Host "[build] 运行 electron-builder（nsis / x64）..."
    npx electron-builder --win nsis --x64
    if ($LASTEXITCODE -ne 0) { throw "electron-builder 失败" }
} finally {
    Pop-Location
}

# ── 3. 安全校验：安装包资源中不得出现任何 .env 文件 ──────────
$unpackedResources = Join-Path $projectRoot 'dist\win-unpacked\resources'
$envFiles = @()
if (Test-Path $unpackedResources) {
    $envFiles = @(Get-ChildItem $unpackedResources -Recurse -Force -File |
        Where-Object { $_.Name -eq '.env' -or $_.Name -like '.env.*' })
}
$asarEnvEntries = @()
$asarPath = Join-Path $unpackedResources 'app.asar'
$asarCli = Join-Path $desktopDir 'node_modules\.bin\asar.cmd'
if ((Test-Path $asarPath) -and (Test-Path $asarCli)) {
    $asarEntries = @(& $asarCli list $asarPath)
    if ($LASTEXITCODE -ne 0) { throw "无法检查 app.asar 内容" }
    $asarEnvEntries = @($asarEntries | Where-Object {
        (Split-Path $_ -Leaf) -eq '.env' -or (Split-Path $_ -Leaf) -like '.env.*'
    })
}
if ($envFiles.Count -gt 0 -or $asarEnvEntries.Count -gt 0) {
    $paths = @($envFiles | ForEach-Object { $_.FullName }) + @($asarEnvEntries | ForEach-Object { "app.asar:$_" })
    $pathText = $paths -join [Environment]::NewLine
    throw "安全校验失败：打包产物中发现 .env 文件：`n$pathText"
}
Write-Host "[build] 安全校验通过：产物中不含 .env / .env.*"

# ── 4. 产物报告 ──────────────────────────────────────────────
$distDir = Join-Path $projectRoot 'dist'
Get-ChildItem $distDir -File |
    Where-Object { $_.Extension -in '.exe', '.blockmap' } |
    ForEach-Object {
        $size = [math]::Round($_.Length / 1MB, 1)
        Write-Host ("[build] 产物: {0}  ({1}MB)" -f $_.Name, $size)
    }
Write-Host "[build] 完成。安装包位于 $distDir"
