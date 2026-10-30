param(
    [string]$Python = '',
    [switch]$IncludeBrowserTests,
    [switch]$IncludeAnki,
    [string]$AnkiRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'tools\setup\setup_helpers.ps1')
if ($env:OS -ne 'Windows_NT') { throw 'setup.ps1 targets Windows. See README for other platforms.' }
if ($AnkiRoot -and -not $IncludeAnki) { throw '-AnkiRoot requires -IncludeAnki.' }

Push-Location -LiteralPath $PSScriptRoot
try {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $projectPython) {
        $projectPython = Get-SetupPython -Requested $projectPython
    } else {
        $basePython = Get-SetupPython -Requested $Python
        Invoke-SetupCommand $basePython @('-m', 'venv', (Join-Path $PSScriptRoot '.venv')) 'Could not create the project Python environment'
    }
    Invoke-SetupCommand $projectPython @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $PSScriptRoot 'requirements.lock.txt')) 'Python dependency installation failed; rerun setup after correcting the reported error'
    Invoke-SetupCommand $projectPython @('-m', 'pip', 'check') 'The installed Python dependencies are inconsistent'
    Initialize-SetupEnvironment -ProjectRoot $PSScriptRoot

    if ($IncludeBrowserTests) {
        $node = Get-Command node.exe -ErrorAction Stop
        $npm = Get-Command npm.cmd -ErrorAction Stop
        $major = & $node.Source -p 'parseInt(process.versions.node)'
        if ($LASTEXITCODE -ne 0 -or [int]$major -lt 20) { throw 'Browser tests require Node.js 20 or newer.' }
        Invoke-SetupCommand $npm.Source @('ci') 'Browser test dependency installation failed'
        Invoke-SetupCommand $node.Source @((Join-Path $PSScriptRoot 'node_modules\playwright\cli.js'), 'install', 'chromium') 'Chromium download failed'
    }
    if ($IncludeAnki) {
        & (Join-Path $PSScriptRoot 'tools\setup\setup_anki.ps1') -Python $projectPython -AnkiRoot $AnkiRoot
    }
    Write-Host 'AnkiSense web setup completed. Start the local server with .\start.ps1'
    Write-Host 'Open http://127.0.0.1:8765 after the server starts. Existing local data and model settings are preserved.'
} finally {
    Pop-Location
}
