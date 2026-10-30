param(
    [string]$Python = (Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'),
    [string]$AnkiRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'setup_helpers.ps1')
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if ($env:OS -ne 'Windows_NT') { throw 'This Anki setup helper targets Windows x64.' }
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
    throw 'The pinned Anki package is for Windows x64. Other architectures require their official package.'
}
$Python = Get-SetupPython -Requested $Python
$runtimeFile = Join-Path $projectRoot 'origin\anki-runtime.json'
if (-not $AnkiRoot -and (Test-Path -LiteralPath $runtimeFile)) {
    $runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($runtime.schema_version -ne 1 -or -not ($runtime.anki_root -is [string]) -or -not $runtime.anki_root) {
        throw 'Invalid local Anki runtime configuration. Specify -AnkiRoot to select its program directory.'
    }
    $AnkiRoot = $runtime.anki_root
    if (-not [System.IO.Path]::IsPathRooted($AnkiRoot)) {
        $AnkiRoot = Join-Path (Join-Path $projectRoot 'origin') $AnkiRoot
    }
}
if ($AnkiRoot -and -not [System.IO.Path]::IsPathRooted($AnkiRoot)) {
    $AnkiRoot = Join-Path $projectRoot $AnkiRoot
}
if (-not $AnkiRoot -and (Test-Path -LiteralPath (Join-Path $projectRoot 'origin\Anki\anki.exe'))) {
    $AnkiRoot = Join-Path $projectRoot 'origin\Anki'
}
if ($AnkiRoot -and -not (Test-Path -LiteralPath (Join-Path $AnkiRoot 'anki.exe') -PathType Leaf)) {
    throw '-AnkiRoot must contain an installed official Anki anki.exe. No program files were changed.'
}
if (Get-Process -Name anki -ErrorAction SilentlyContinue) {
    throw 'Close Anki normally before preparing or updating the project plugin, then run this command again.'
}
$installer = $null
if (-not $AnkiRoot) {
    $installer = Get-VerifiedAnkiInstaller -Directory (Join-Path $projectRoot 'origin\downloads')
}
$arguments = @((Join-Path $PSScriptRoot 'setup_anki.py'))
if ($AnkiRoot) { $arguments += @('--runtime', $AnkiRoot) }
Invoke-SetupCommand $Python $arguments 'Could not prepare the isolated Anki profile and plugin'
if ($installer) {
    Write-Host 'Anki is NOT installed yet. The verified official installer is ready at:'
    Write-Host $installer
    Write-Host 'Run that MSI manually when ready (the official installer can update an existing Anki installation).'
    Write-Host 'Then connect its program directory using:'
    Write-Host '.\tools\setup\setup_anki.ps1 -AnkiRoot "$env:LOCALAPPDATA\Programs\Anki"'
    Write-Host 'Alternatively select origin\Anki in the official installer, then rerun .\setup.ps1 -IncludeAnki.'
} else {
    Write-Host 'Anki program connected. Start this project profile with .\origin\启动Anki.ps1'
}
Write-Host 'Only this project profile in origin\AnkiData receives the plugin. Personal Anki profiles are not changed.'
