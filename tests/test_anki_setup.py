"""Onboarding only touches the local add-on, never an Anki collection."""
from io import BytesIO
import json
from pathlib import Path
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from ankisense.anki_setup import ADDON_FILES, ADDON_VERSION
from ankisense.llm import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    monkeypatch.setattr("ankisense.anki_setup.anki_running", lambda: False)
    from ankisense.app import create_app
    with TestClient(create_app(tmp_path / "app", Settings(mode="demo")), base_url="http://127.0.0.1:18923") as c:
        yield c


def enable(client):
    response = client.post("/api/anki/setup/enable", json={})
    assert response.status_code == 200, response.text
    connection = client.app.state.plugin.connection
    value = connection.read()
    assert value["base_url"] == "http://127.0.0.1:18923"
    assert value["token"] not in response.text
    return value, {"Authorization": "Bearer " + value["token"], "X-AnkiSense-Client": "anki-setup-fixture"}


def test_setup_never_claims_connected_until_authenticated_plugin_receipt(client):
    first = client.get("/api/anki/setup").json()
    assert not first["connection"]["enabled"] and not first["connected"]
    value, headers = enable(client)
    enable(client)
    assert client.app.state.plugin.connection.read() == value, "Opening/re-enabling must not rotate local credentials"
    event = {"kind": "heartbeat", "plugin_version": ADDON_VERSION}
    assert client.post("/api/plugin/v1/events", json=event).status_code == 401
    assert not client.get("/api/anki/setup").json()["connected"]
    assert client.post("/api/plugin/v1/events", headers=headers, json=event).status_code == 200
    status = client.get("/api/anki/setup").json()
    assert status["connected"] and status["plugin_version"] == ADDON_VERSION
    assert status["last_sync"] is None
    assert value["token"] not in json.dumps(status)
    record = client.app.state.store.get("anki_clients", headers["X-AnkiSense-Client"])
    client.app.state.store.put("anki_clients", {**record, "last_seen": time.time() - 100})
    assert not client.get("/api/anki/setup").json()["connected"]


def test_sync_counts_survive_heartbeat_but_old_pairing_is_not_current(client):
    _, headers = enable(client)
    body = {"kind": "sync", "plugin_version": ADDON_VERSION, "added": 2, "updated": 1, "unchanged": 4, "conflicts": 1}
    assert client.post("/api/plugin/v1/events", headers=headers, json=body).status_code == 200
    assert client.post("/api/plugin/v1/events", headers=headers, json={"kind": "heartbeat", "plugin_version": ADDON_VERSION}).status_code == 200
    assert client.get("/api/anki/setup").json()["last_sync"]["added"] == 2
    client.app.state.plugin.connection.configure(rotate=True)
    status = client.get("/api/anki/setup").json()
    assert not status["connected"] and status["last_sync"] is None
    assert client.post("/api/plugin/v1/events", headers=headers, json=body).status_code == 401


def test_install_updates_only_our_addon_backs_up_and_is_idempotent(client, tmp_path):
    value, _ = enable(client)
    base = tmp_path / "AnkiData"
    base.mkdir()
    (base / "prefs21.db").write_bytes(b"prefs fixture")
    profile = base / "User 1"
    profile.mkdir()
    collection = profile / "collection.anki2"
    collection.write_bytes(b"personal collection must not change")
    other = base / "addons21" / "unrelated"
    other.mkdir(parents=True)
    (other / "config.json").write_text("private other add-on", encoding="utf-8")
    response = client.post("/api/anki/setup/install", json={"anki_base": str(base)})
    assert response.status_code == 200, response.text
    assert response.json()["changed"]
    target = base / "addons21" / "ankisense_sync"
    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert config["connection_file"] == str(client.app.state.plugin.connection.path)
    assert config["setup_id"] == value["server_id"]
    assert value["token"] not in (target / "config.json").read_text(encoding="utf-8")
    assert set(ADDON_FILES) <= {file.name for file in target.iterdir()}
    assert not client.post("/api/anki/setup/install", json={"anki_base": str(base)}).json()["changed"]
    (target / "ui.py").write_text("old version", encoding="utf-8")
    assert client.post("/api/anki/setup/install", json={"anki_base": str(base)}).json()["changed"]
    backups = list(client.app.state.anki_setup.storage.glob("anki/addon-backups/*.zip"))
    assert len(backups) == 1
    with zipfile.ZipFile(backups[0]) as archive:
        assert archive.read("ui.py") == b"old version"
    assert collection.read_bytes() == b"personal collection must not change"
    assert (other / "config.json").read_text() == "private other add-on"
    assert not client.get("/api/anki/setup").json()["connected"], "Installation alone is not a handshake"


def test_running_anki_invalid_path_and_cross_origin_do_not_install(client, tmp_path, monkeypatch):
    enable(client)
    base = tmp_path / "AnkiData"; base.mkdir()
    assert client.post("/api/anki/setup/install", json={"anki_base": str(base)}).status_code == 422
    (base / "prefs21.db").touch()
    monkeypatch.setattr("ankisense.anki_setup.anki_running", lambda: True)
    response = client.post("/api/anki/setup/install", json={"anki_base": str(base)})
    assert response.status_code == 409 and not (base / "addons21").exists()
    assert client.post("/api/anki/setup/enable", json={}, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.post("/api/anki/setup/enable", data={}).status_code == 415
    assert client.post("/api/anki/setup/enable", json={"token": "unwanted"}).status_code == 422


def test_download_contains_plugin_and_no_local_pairing_secret(client):
    value, _ = enable(client)
    response = client.get("/api/anki/setup/addon")
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == set(ADDON_FILES)
        assert json.loads(archive.read("config.json")) == {"connection_file": ""}
        assert json.loads(archive.read("manifest.json"))["human_version"] == ADDON_VERSION
        assert all(value["token"].encode() not in archive.read(name) for name in archive.namelist())
