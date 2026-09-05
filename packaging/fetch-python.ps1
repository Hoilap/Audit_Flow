<#
.SYNOPSIS
    下载并解压独立的 Python 运行时（python-build-standalone）到 desktop/runtime/python。
.DESCRIPTION
    只需在准备发布时运行一次；runtime/python 会被 electron-builder 打进安装包。
    运行时版本应与开发环境保持一致（当前项目使用 Python 3.12.x）。
.PARAMETER Force
    强制重新下载解压（即使目录已存在）。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging/fetch-python.ps1
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot 'desktop\runtime\python'
$cacheDir = Join-Path $projectRoot 'desktop\runtime\.downloads'

# python-build-standalone 版本（astral-sh 维护，uv 同款发行版）
# 如需更换版本，修改 $Tag 和 $PyVersion 后加 -Force 重跑
$PyVersion = '3.12.4'
$Tag = '20240713'
$FileName = "cpython-$PyVersion+$Tag-x86_64-pc-windows-msvc-install_only.tar.gz"
$Url = "https://github.com/astral-sh/python-build-standalone/releases/download/$Tag/$FileName"

if ((Test-Path $runtimeDir) -and (Test-Path (Join-Path $runtimeDir 'python.exe')) -and -not $Force) {
    Write-Host "[fetch-python] runtime 已存在：$runtimeDir（跳过，加 -Force 重新下载）"
    exit 0
}

New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
$archive = Join-Path $cacheDir $FileName

if (-not (Test-Path $archive)) {
    Write-Host "[fetch-python] 下载 $Url"
    # GitHub 直连较慢时可手动下载后放入 $cacheDir，脚本会自动跳过下载
    Invoke-WebRequest -Uri $Url -OutFile $archive
} else {
    Write-Host "[fetch-python] 使用缓存：$archive"
}

Write-Host "[fetch-python] 解压到临时目录..."
$tmp = Join-Path $cacheDir ('extract_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
tar -xzf $archive -C $tmp
if ($LASTEXITCODE -ne 0) { throw "tar 解压失败（需要 Windows 10 1803+ 自带的 bsdtar）" }

# install_only 包解压后即 python/ 目录本身
$extracted = Get-ChildItem $tmp | Where-Object Name -eq 'python' | Select-Object -First 1
if (-not $extracted) { throw "解压结果中未找到 python/ 目录" }

if (Test-Path $runtimeDir) { Remove-Item $runtimeDir -Recurse -Force }
Move-Item $extracted.FullName $runtimeDir
Remove-Item $tmp -Recurse -Force

# 裁剪体积：删除测试套件与静态库（运行时不需要）
Get-ChildItem $runtimeDir -Directory | Where-Object Name -in @('test', 'idlelib', 'lib2to3') -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

& (Join-Path $runtimeDir 'python.exe') --version
Write-Host "[fetch-python] 完成：$runtimeDir"
