Set-StrictMode -Version Latest

function Invoke-SetupCommand {
    param([string]$Executable, [string[]]$Arguments, [string]$Failure)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Failure (exit $LASTEXITCODE)" }
}

function Get-SetupPython {
    param([string]$Requested)
    if ($Requested) {
        $candidate = $Requested
    } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $candidate = & py.exe -3.12 -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 was not found. Install Python 3.12, or pass -Python with its python.exe path.' }
    } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        $candidate = (Get-Command python.exe).Source
    } else {
        throw 'Python 3.12 was not found. Install Python 3.12, or pass -Python with its python.exe path.'
    }
    $version = & $candidate -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
    if ($LASTEXITCODE -ne 0 -or "$version".Trim() -ne '3.12') {
        throw 'This project uses Python 3.12. Pass -Python with a Python 3.12 executable; existing environments will not be replaced.'
    }
    return $candidate
}

function Initialize-SetupEnvironment {
    param([string]$ProjectRoot)
    $destination = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $destination) {
        Write-Host 'Keeping existing .env unchanged.'
        return
    }
    # CreateNew also prevents overwriting a file created between the check and copy.
    $content = [System.IO.File]::ReadAllBytes((Join-Path $ProjectRoot '.env.example'))
    $stream = [System.IO.File]::Open($destination, [System.IO.FileMode]::CreateNew)
    try { $stream.Write($content, 0, $content.Length) } finally { $stream.Dispose() }
    Write-Host 'Created .env from the demo-mode example; no model API call is made.'
}

function Get-SetupSha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-VerifiedAnkiInstaller {
    param([string]$Directory)
    $name = 'anki-26.09.3-win-x64.msi'
    $url = 'https://github.com/ankitects/anki/releases/download/26.09.3/anki-26.09.3-win-x64.msi'
    $expected = '8a45823d1c31017cebb7d3af4493d69b6ae3c185345fcb2390ede4af01e0a950'
    $ancestor = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Directory))
    while ($null -ne $ancestor) {
        if ($ancestor.Exists -and ($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw 'The installer download directory must not contain directory links or junctions.'
        }
        $ancestor = $ancestor.Parent
    }
    [System.IO.Directory]::CreateDirectory($Directory) | Out-Null
    $destination = Join-Path $Directory $name
    if (Test-Path -LiteralPath $destination) {
        if ((Get-Item -LiteralPath $destination).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw 'The installer cache file must not be a symbolic link.'
        }
        if ((Get-SetupSha256 $destination) -ne $expected) {
            throw 'The cached Anki installer failed SHA256 verification. Move it aside and rerun; it has not been executed or replaced.'
        }
        return $destination
    }
    $temporary = Join-Path $Directory ($name + '.' + [Guid]::NewGuid().ToString('N') + '.part')
    try {
        Write-Host 'Downloading the official Anki 26.09.3 Windows x64 installer (about 201 MB)...'
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $temporary -UseBasicParsing
        if ((Get-SetupSha256 $temporary) -ne $expected) {
            throw 'Downloaded Anki installer failed SHA256 verification. Nothing was installed.'
        }
        # File.Move does not overwrite a concurrently created destination.
        [System.IO.File]::Move($temporary, $destination)
        return $destination
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
