"""Fresh-clone setup contracts, with no downloads, installations or user profiles."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from ankisense.plugin_api import PluginConnection
from tools.build import build_anki_addon as builder
from tools.setup import setup_anki

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "team project"
    source = root / "addons" / builder.PACKAGE
    source.mkdir(parents=True)
    for name in builder.FILES:
        shutil.copyfile(builder.SOURCE / name, source / name)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "SOURCE", source)
    monkeypatch.setattr(setup_anki, "ROOT", root)
    monkeypatch.setattr(setup_anki, "resolve_data_dir", lambda: root / "data")
    return root


def test_setup_creates_local_connection_without_exposing_credentials_or_rewriting_source(project):
    before = {name: (builder.SOURCE / name).read_bytes() for name in builder.FILES}
    result = setup_anki.prepare()
    connection = PluginConnection(project / "data").read()
    assert connection is not None and connection["enabled"]
    assert connection["token"] not in json.dumps(result)
    assert result["collections_opened"] == 0
    assert result["runtime_configured"] is False
    target = project / "origin" / "AnkiData" / "addons21" / builder.PACKAGE
    assert json.loads((target / "config.json").read_text(encoding="utf-8"))["connection_file"] == str((project / "data" / "plugin-connection.json").resolve())
    assert before == {name: (builder.SOURCE / name).read_bytes() for name in builder.FILES}


def test_repeat_setup_preserves_learning_files_and_disabled_connection(project):
    connection = PluginConnection(project / "data")
    connection.configure(enabled=False, base_url="http://127.0.0.1:9001")
    credentials = connection.path.read_bytes()
    profile = project / "origin" / "AnkiData" / "learner"
    profile.mkdir(parents=True)
    collection = profile / "collection.anki2"
    collection.write_bytes(b"sentinel collection: must not be opened or changed")
    result = setup_anki.prepare()
    target = project / "origin" / "AnkiData" / "addons21" / builder.PACKAGE
    user_files = target / "user_files"
    user_files.mkdir()
    (user_files / "notes.txt").write_text("keep personal plugin files", encoding="utf-8")
    setup_anki.prepare()
    assert result["connection_enabled"] is False
    assert connection.path.read_bytes() == credentials
    assert collection.read_bytes() == b"sentinel collection: must not be opened or changed"
    assert (user_files / "notes.txt").read_text(encoding="utf-8") == "keep personal plugin files"


def test_invalid_connection_and_missing_runtime_fail_without_resetting_data(project):
    with pytest.raises(ValueError, match="anki.exe"):
        setup_anki.prepare(project / "missing Anki")
    assert not (project / "data").exists()
    (project / "data").mkdir()
    connection = project / "data" / "plugin-connection.json"
    connection.write_text("invalid-but-preserved", encoding="utf-8")
    with pytest.raises(ValueError, match="Existing plugin connection"):
        setup_anki.prepare()
    assert connection.read_text(encoding="utf-8") == "invalid-but-preserved"
    assert not (project / "origin" / "AnkiData").exists()


@pytest.mark.parametrize("external", [False, True])
def test_runtime_config_uses_relative_path_when_possible_and_survives_repeat(project, tmp_path, external):
    runtime = (tmp_path / "existing official Anki") if external else (project / "origin" / "Anki")
    runtime.mkdir(parents=True)
    (runtime / "anki.exe").write_bytes(b"placeholder: never executed")
    setup_anki.prepare(runtime)
    config_file = project / "origin" / "anki-runtime.json"
    content = config_file.read_bytes()
    config = json.loads(content)
    assert config["anki_root"] == (str(runtime.resolve()) if external else "Anki")
    assert setup_anki.prepare()["runtime_configured"]
    assert config_file.read_bytes() == content
    assert (runtime / "anki.exe").read_bytes() == b"placeholder: never executed"


@pytest.fixture
def powershell(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows PowerShell setup helper")
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        pytest.skip("PowerShell is not available")

    def run(body):
        script = tmp_path / "setup-case.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n. $env:TEST_SETUP_HELPER\n" + body, encoding="utf-8-sig")
        env = {**os.environ, "TEST_SETUP_HELPER": str(ROOT / "tools" / "setup" / "setup_helpers.ps1"), "TEST_SETUP_TEMP": str(tmp_path), "TEST_SETUP_ROOT": str(ROOT)}
        result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-File", str(script)], env=env, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    return run


def test_env_initialization_is_repeatable_and_preserves_existing_credentials(powershell):
    powershell("""
$sample = Join-Path $env:TEST_SETUP_TEMP '.env.example'
$file = Join-Path $env:TEST_SETUP_TEMP '.env'
[IO.File]::WriteAllText($sample, "AI_MODE=demo`nAI_API_KEY=")
Initialize-SetupEnvironment $env:TEST_SETUP_TEMP
if ([IO.File]::ReadAllText($file) -ne [IO.File]::ReadAllText($sample)) { throw 'Template not copied' }
[IO.File]::WriteAllText($file, 'existing-private-settings')
Initialize-SetupEnvironment $env:TEST_SETUP_TEMP
if ([IO.File]::ReadAllText($file) -ne 'existing-private-settings') { throw 'Existing settings changed' }
""")


def test_bad_anki_download_hash_never_becomes_installer(powershell):
    powershell("""
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing)
    [IO.File]::WriteAllText($OutFile, 'corrupt download')
}
$failed = $false
try { Get-VerifiedAnkiInstaller $env:TEST_SETUP_TEMP | Out-Null } catch { if ($_.Exception.Message -notlike '*SHA256*') { throw }; $failed = $true }
if (-not $failed) { throw 'Bad hash was not rejected' }
if (Get-ChildItem -LiteralPath $env:TEST_SETUP_TEMP -Filter '*.msi') { throw 'Invalid installer adopted' }
if (Get-ChildItem -LiteralPath $env:TEST_SETUP_TEMP -Filter '*.part') { throw 'Partial download left behind' }
""")


def test_bad_cached_installer_is_preserved_and_never_redownloaded(powershell):
    powershell("""
function Invoke-WebRequest { throw 'Network must not be called for an existing bad cache' }
$file = Join-Path $env:TEST_SETUP_TEMP 'anki-26.09.3-win-x64.msi'
[IO.File]::WriteAllText($file, 'keep this invalid cache for inspection')
$failed = $false
try { Get-VerifiedAnkiInstaller $env:TEST_SETUP_TEMP | Out-Null } catch { if ($_.Exception.Message -notlike '*SHA256*') { throw }; $failed = $true }
if (-not $failed) { throw 'Bad cache hash was not rejected' }
if ([IO.File]::ReadAllText($file) -ne 'keep this invalid cache for inspection') { throw 'Bad cache overwritten' }
""")


def test_verified_anki_download_uses_official_pin_and_reuses_cache(powershell):
    powershell("""
$script:downloads = 0
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing)
    if ($Uri -ne 'https://github.com/ankitects/anki/releases/download/26.09.3/anki-26.09.3-win-x64.msi') { throw 'Unexpected source' }
    $script:downloads++
    [IO.File]::WriteAllText($OutFile, 'mock verified installer')
}
function Get-SetupSha256 { param($Path)
    return '8a45823d1c31017cebb7d3af4493d69b6ae3c185345fcb2390ede4af01e0a950'
}
$first = Get-VerifiedAnkiInstaller $env:TEST_SETUP_TEMP
$second = Get-VerifiedAnkiInstaller $env:TEST_SETUP_TEMP
if ($first -ne $second -or $script:downloads -ne 1) { throw 'Verified cache was not reused' }
if (Get-ChildItem -LiteralPath $env:TEST_SETUP_TEMP -Filter '*.part') { throw 'Partial download left behind' }
""")


def test_interrupted_anki_download_cleans_partial_file(powershell):
    powershell("""
function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing)
    [IO.File]::WriteAllText($OutFile, 'partial network response')
    throw 'Mock connection interrupted'
}
$failed = $false
try { Get-VerifiedAnkiInstaller $env:TEST_SETUP_TEMP | Out-Null } catch { $failed = $_.Exception.Message -eq 'Mock connection interrupted' }
if (-not $failed) { throw 'Download error was not propagated' }
if (Get-ChildItem -LiteralPath $env:TEST_SETUP_TEMP -Filter '*.part') { throw 'Partial download left behind' }
""")


def test_setup_scripts_parse_in_windows_powershell(powershell):
    powershell("""
foreach ($relative in @('setup.ps1', 'tools/setup/setup_anki.ps1', 'tools/setup/setup_helpers.ps1')) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $env:TEST_SETUP_ROOT $relative), [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count) { throw ($errors | Out-String) }
}
""")
