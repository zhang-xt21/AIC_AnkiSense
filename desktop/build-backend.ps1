$ErrorActionPreference = "Stop"
$taskProject = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskProject ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw "Create the project .venv and install requirements.lock.txt and desktop/requirements-build.txt first."
}
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot "build/pyinstaller-cache"
Push-Location -LiteralPath $PSScriptRoot
try {
    & $taskPython -m PyInstaller ankisense-backend.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "Backend packaging failed ($LASTEXITCODE)." }
}
finally { Pop-Location }
