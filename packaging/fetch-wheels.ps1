<#
.SYNOPSIS
    离线收集全部 Python 依赖的 wheel 包到 desktop/wheels。
.DESCRIPTION
    目标机器可能无法联网，因此把 requirements.txt 解析出的所有 wheel
    （按 Windows x64 + Python 3.12 平台）提前下载好，随安装包分发。
    首次运行时由 main.js 的 bootstrap 用 pip --no-index 离线安装。
    注意：必须在 Windows x64 环境下运行本脚本，否则下载到的
    二进制 wheel 平台不匹配。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging/fetch-wheels.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wheelsDir = Join-Path $projectRoot 'desktop\wheels'
$reqFile = Join-Path $projectRoot 'requirements.txt'

# 优先使用项目 .venv 的 Python（保证 pip 可用且版本正确）
$venvPy = Join-Path $projectRoot '.venv\Scripts\python.exe'
$py = if (Test-Path $venvPy) { $venvPy } else { 'python' }
Write-Host "[fetch-wheels] using Python: $py"
& $py --version

New-Item -ItemType Directory -Force -Path $wheelsDir | Out-Null

Write-Host "[fetch-wheels] 下载 wheels 到 $wheelsDir（含依赖闭包）..."
& $py -m pip download `
    -r $reqFile `
    -d $wheelsDir `
    -i https://pypi.org/simple `
    --only-binary=:all: `
    --platform win_amd64 `
    --python-version 3.12 `
    --implementation cp `
    --abi cp312
if ($LASTEXITCODE -ne 0) { throw "pip download 失败" }

$count = (Get-ChildItem $wheelsDir -Filter *.whl).Count
$size = [math]::Round((Get-ChildItem $wheelsDir | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "[fetch-wheels] 完成：$count 个 wheel，共 ${size}MB"
