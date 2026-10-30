param(
    [string]$AnkiRoot = (Join-Path $PSScriptRoot 'Anki'),
    [string]$DataDirectory = (Join-Path $PSScriptRoot 'AnkiData')
)
$ErrorActionPreference = 'Stop'
# setup_anki stores only this computer's program path, never keys or user data.
if (-not $PSBoundParameters.ContainsKey('AnkiRoot')) {
    $runtimeFile = Join-Path $PSScriptRoot 'anki-runtime.json'
    if (Test-Path -LiteralPath $runtimeFile) {
        $runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($runtime.schema_version -ne 1 -or -not ($runtime.anki_root -is [string]) -or -not $runtime.anki_root) {
            throw 'Invalid local Anki runtime configuration. Rerun tools/setup/setup_anki.ps1 -AnkiRoot with the program directory.'
        }
        $AnkiRoot = $runtime.anki_root
    }
}
# Defaults follow this script when the project is moved. Explicit relative
# parameters are relative to origin/ as well, independent of the shell cwd.
if (-not [System.IO.Path]::IsPathRooted($AnkiRoot)) {
    $AnkiRoot = Join-Path $PSScriptRoot $AnkiRoot
}
if (-not [System.IO.Path]::IsPathRooted($DataDirectory)) {
    $DataDirectory = Join-Path $PSScriptRoot $DataDirectory
}
$ankiExecutable = Join-Path $AnkiRoot 'anki.exe'
if (-not (Test-Path -LiteralPath $ankiExecutable -PathType Leaf)) {
    throw 'Anki is not installed here. Install Anki first or supply -AnkiRoot with its installation directory.'
}
& $ankiExecutable -b ([System.IO.Path]::GetFullPath($DataDirectory)) -l zh_CN
