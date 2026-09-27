param(
    [string]$Python = "python",
    [switch]$SkipModels,
    [string]$IndexUrl = "https://pypi.org/simple"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $projectRoot ".venv-docling"
$workerPython = Join-Path $environmentRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $workerPython)) {
    & $Python -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the isolated Docling environment." }
}
$lockFile = Join-Path $projectRoot "requirements-docling.lock.txt"
$requirementsFile = if (Test-Path -LiteralPath $lockFile) { $lockFile } else { Join-Path $projectRoot "requirements-docling.txt" }
$networkOptions = @("--index-url", $IndexUrl, "--timeout", "120", "--retries", "5")
# Python 3.12 ships an older pip; upgrade first so interrupted large downloads
# can be resumed. Pin the installer used for the verified environment.
& $workerPython -m pip install @networkOptions --upgrade "pip==26.2.1"
if ($LASTEXITCODE -ne 0) { throw "Could not prepare pip with resumable-download support." }
& $workerPython -m pip install @networkOptions --resume-retries 10 -r $requirementsFile
if ($LASTEXITCODE -ne 0) { throw "Docling dependency installation failed." }
& $workerPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Docling dependencies are inconsistent." }
if (-not $SkipModels) {
    & $workerPython (Join-Path $PSScriptRoot "docling_worker.py") download-models
    if ($LASTEXITCODE -ne 0) { throw "Docling model download failed; existing web data is unchanged." }
}
Write-Host "Docling is installed separately from the web environment. Runtime inference is offline."
