param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Please create .venv and install requirements.lock.txt first. See README.md.'
}
& $projectPython -m uvicorn ankisense.app:app --host 127.0.0.1 --port $Port
