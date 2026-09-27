"""Storage stays shared by the server and plugin tools outside the project cwd."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from ankisense import paths
from ankisense.llm import Settings


@pytest.fixture
def moved_project(tmp_path, monkeypatch):
    root = tmp_path / "移动后的项目 with spaces"
    outside = tmp_path / "another terminal"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(root / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    monkeypatch.chdir(outside)
    return root, outside, create_app


@pytest.mark.parametrize("source", ["default", "environment", "dotenv", "argument", "absolute"])
def test_same_data_after_changing_working_directory(moved_project, monkeypatch, source):
    root, outside, create_app = moved_project
    monkeypatch.delenv("ANKISENSE_DATA_DIR")
    argument = None
    expected = root / "data"
    if source in {"environment", "dotenv"}:
        expected = root / "shared data"
        (root / ".env").write_text('ANKISENSE_DATA_DIR="./shared data"\n', encoding="utf-8")
        if source == "environment":
            monkeypatch.setenv("ANKISENSE_DATA_DIR", "./environment data")
            expected = root / "environment data"
    elif source in {"argument", "absolute"}:
        monkeypatch.setenv("ANKISENSE_DATA_DIR", "./ignored")
        argument = Path("./argument data") if source == "argument" else outside / "external data"
        expected = root / argument if source == "argument" else argument
    app = create_app(data_dir=argument, settings=Settings(mode="demo"))
    assert app.state.store.path == expected / "ankisense.sqlite3"
    with TestClient(app) as client:
        assert client.post("/api/demo").status_code == 200
        cards = client.get("/api/state").json()["cards"]
        assert cards
    monkeypatch.chdir(root)
    restarted = create_app(data_dir=argument, settings=Settings(mode="demo"))
    with TestClient(restarted) as client:
        assert client.get("/api/state").json()["cards"] == cards
    assert not (outside / "data").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_plugin_cli_and_server_share_relative_storage(moved_project, monkeypatch, capsys, explicit):
    from tools import plugin_connection

    root, outside, create_app = moved_project
    monkeypatch.setenv("ANKISENSE_DATA_DIR", "./environment data")
    argument = Path("./cli data") if explicit else None
    arguments = ["plugin_connection.py", "init"]
    if explicit:
        arguments.extend(["--data-dir", str(argument)])
    monkeypatch.setattr(sys, "argv", arguments)
    plugin_connection.main()
    initialized = json.loads(capsys.readouterr().out)
    app = create_app(data_dir=argument, settings=Settings(mode="demo"))
    assert initialized["connection_file"] == str(app.state.plugin.connection.path)
    assert app.state.plugin.connection.public()["server_id"] == initialized["server_id"]
    assert not (outside / "environment data").exists()
    assert not (outside / "cli data").exists()


def test_copied_source_runs_from_another_directory(tmp_path):
    """Exercise actual __file__ locations, .env loading and CLI subprocesses."""
    root = tmp_path / "迁移后的 AnkiSense"
    root.mkdir()
    shutil.copytree(paths.ROOT / "ankisense", root / "ankisense", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "tools").mkdir()
    shutil.copyfile(paths.ROOT / "tools/plugin_connection.py", root / "tools/plugin_connection.py")
    (root / ".env").write_text("AI_MODE=demo\nANKISENSE_DATA_DIR=./shared-data\n", encoding="utf-8")
    outside = tmp_path / "终端目录"
    outside.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in {"ANKISENSE_DATA_DIR", "PYTHONPATH"}}
    env.update(AI_MODE="demo", AI_API_KEY="", AI_MODEL="", AI_BASE_URL="", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "tools/plugin_connection.py"), "init"],
        cwd=outside, env=env, capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
    )
    initialized = json.loads(result.stdout)
    assert Path(initialized["connection_file"]) == root / "shared-data/plugin-connection.json"
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from ankisense.app import app
with TestClient(app) as client:
    assert client.get('/').status_code == 200
    assert client.post('/api/demo').status_code == 200
    assert client.get('/api/state').json()['cards']
print(json.dumps({'storage': str(app.state.store.path), 'connection': str(app.state.plugin.connection.path)}))
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, str(root)], cwd=outside, env=env,
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=30,
    )
    loaded = json.loads(result.stdout)
    assert Path(loaded["storage"]) == root / "shared-data/ankisense.sqlite3"
    assert loaded["connection"] == initialized["connection_file"]
    assert list(outside.iterdir()) == []
