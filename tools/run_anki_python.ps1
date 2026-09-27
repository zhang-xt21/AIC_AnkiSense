param(
    [Parameter(Mandatory=$true)][string]$Script,
    [string]$AnkiRoot = (Join-Path $PSScriptRoot '..\origin\Anki'),
    [string]$WorkDirectory = (Join-Path $PSScriptRoot '..'),
    [string]$DataDirectory = '',
    [string]$ArgumentsJson = '[]',
    [switch]$AllowLoopback,
    [switch]$EmbeddedChild
)
$ErrorActionPreference = 'Stop'
$AnkiRoot = (Resolve-Path -LiteralPath $AnkiRoot).Path
$Script = (Resolve-Path -LiteralPath $Script).Path
$WorkDirectory = (Resolve-Path -LiteralPath $WorkDirectory).Path
$scriptArgs = @(ConvertFrom-Json -InputObject $ArgumentsJson)
if ($ArgumentsJson -notmatch '^\s*\[' -or @($scriptArgs | Where-Object { $_ -isnot [string] }).Count -gt 0) {
    throw 'ArgumentsJson must be a JSON array of strings.'
}
if (-not $DataDirectory) {
    $DataDirectory = Join-Path $WorkDirectory ('测试记录\2026-09-27-Anki插件\runtime-' + [Guid]::NewGuid().ToString('N'))
}
$DataDirectory = [System.IO.Path]::GetFullPath($DataDirectory)
foreach ($required in @('python313.dll', 'python313.zip', 'app_packages')) {
    if (-not (Test-Path -LiteralPath (Join-Path $AnkiRoot $required))) { throw ('Missing Anki runtime component: ' + $required) }
}
if (-not $EmbeddedChild) {
    # A dedicated console child isolates CPython/Qt lifetime from the caller.
    # This uses the calling shell with argument arrays; no shell-built command.
    $shellPath = (Get-Process -Id $PID).Path
    $childArguments = @('-NoLogo', '-NoProfile', '-File', $PSCommandPath, '-Script', $Script, '-AnkiRoot', $AnkiRoot,
        '-WorkDirectory', $WorkDirectory, '-DataDirectory', $DataDirectory, '-ArgumentsJson', $ArgumentsJson, '-EmbeddedChild')
    if ($AllowLoopback) { $childArguments += '-AllowLoopback' }
    & $shellPath @childArguments
    exit $LASTEXITCODE
}

$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$optionsJson = ConvertTo-Json -Depth 4 -Compress -InputObject @{
    script = $Script; anki_root = $AnkiRoot; work_directory = $WorkDirectory
    data_directory = $DataDirectory; arguments = @($scriptArgs); allow_loopback = [bool]$AllowLoopback
}
Set-Location -LiteralPath $WorkDirectory

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AnkiIsolatedPythonHost {
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool SetDllDirectory(string path);
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr LoadLibrary(string path);
    [DllImport("python313.dll", CallingConvention=CallingConvention.Cdecl, CharSet=CharSet.Unicode)]
    public static extern void Py_SetPath(string path);
    [DllImport("python313.dll", CallingConvention=CallingConvention.Cdecl)]
    public static extern void Py_InitializeEx(int installSignals);
    [DllImport("python313.dll", CallingConvention=CallingConvention.Cdecl)]
    public static extern int PyRun_SimpleString(byte[] code);
    [DllImport("python313.dll", CallingConvention=CallingConvention.Cdecl)]
    public static extern int Py_FinalizeEx();
}
'@
if (-not [AnkiIsolatedPythonHost]::SetDllDirectory($AnkiRoot)) { throw 'Could not configure the Anki DLL directory.' }
if ([AnkiIsolatedPythonHost]::LoadLibrary((Join-Path $AnkiRoot 'python313.dll')) -eq [IntPtr]::Zero) {
    throw 'Could not load the official Anki embedded Python.'
}
[AnkiIsolatedPythonHost]::Py_SetPath(($AnkiRoot, (Join-Path $AnkiRoot 'python313.zip'), (Join-Path $AnkiRoot 'app_packages'), $PSScriptRoot) -join ';')
[AnkiIsolatedPythonHost]::Py_InitializeEx(0)
$optionsBase64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($optionsJson))
$code = "import sys; sys.dont_write_bytecode = True; import json, base64, anki_test_bootstrap; anki_test_bootstrap.run(json.loads(base64.b64decode('$optionsBase64')))"
$codeBytes = [System.Text.Encoding]::UTF8.GetBytes($code + [char]0)
$runCode = [AnkiIsolatedPythonHost]::PyRun_SimpleString($codeBytes)
$finalizeCode = [AnkiIsolatedPythonHost]::Py_FinalizeEx()
if ($runCode -ne 0 -or $finalizeCode -ne 0) { exit 1 }
exit 0
