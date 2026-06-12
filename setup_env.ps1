<#
Windows PowerShell 环境初始化脚本
用法（在项目根目录运行）:
.\setup_env.ps1
#>
Set-StrictMode -Version Latest
Write-Host "Creating Python virtual environment .venv..."
python -m venv .venv
Write-Host "Activating virtual environment and upgrading pip..."
. .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
Write-Host "Installing Python requirements..."
pip install -r requirements.txt

if (Test-Path .\desktop\package.json) {
    Write-Host "Installing Node dependencies for desktop..."
    Push-Location .\desktop
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        npm install
    } else {
        Write-Host "npm not found. Please install Node.js and npm to proceed with desktop frontend."
    }
    Pop-Location
}

Write-Host "Done. To activate venv later: .\.venv\Scripts\Activate.ps1"
