"""Run the add-on's real Qt smoke test in an isolated official Anki runtime."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_real_anki_plugin_ui(tmp_path):
    runtime = Path(os.environ.get("ANKISENSE_ANKI_RUNTIME", ROOT / "origin" / "Anki"))
    if not (runtime / "python313.dll").is_file():
        pytest.skip("The optional official Anki runtime is not installed")
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "A PowerShell host is needed for the official embedded runtime"
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(ROOT / "tools" / "run_anki_python.ps1"),
         "-Script", str(ROOT / "tools" / "test_anki_plugin_ui.py"),
         "-AnkiRoot", str(runtime),
         "-DataDirectory", str(tmp_path / "official-anki-ui")],
        cwd=ROOT,
        capture_output=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    report_path = tmp_path / "official-anki-ui" / "ui-smoke-result.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["callback_errors"] == [], output
    assert report["checks"] and all(report["checks"].values()), output
    assert report["passed"] == len(report["checks"]), output
