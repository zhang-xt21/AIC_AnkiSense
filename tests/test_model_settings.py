"""Local-only model settings, credential boundaries and in-flight consistency."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from ankisense.llm import AIError, Settings
from ankisense.model_settings import ModelSettingsFile
from ankisense.schemas import CardDraft, CardUpdate, DraftBatch


SECRET = "TEST-PRIVATE-KEY-DO-NOT-ECHO"


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")

    def forbid_network(*args, **kwargs):
        pytest.fail("Model configuration tests must not contact a model provider")

    monkeypatch.setattr(httpx.AsyncClient, "post", forbid_network)
    from ankisense.app import create_app
    clients = []

    def make(name="settings-app", settings=None):
        app = create_app(data_dir=tmp_path / name, settings=settings or Settings())
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def config(**changes):
    return {"mode": "cloud", "base_url": "https://model.test/v1", "model": "offline-test-model",
            "timeout_seconds": 45, "api_key": SECRET, **changes}


def update(client, **changes):
    return client.put("/api/settings/ai", json=config(**changes))


def assert_secret_absent(response):
    assert SECRET not in response.text


def test_safe_default_and_persisted_settings_override_environment(factory, tmp_path, monkeypatch):
    client = factory()
    response = client.get("/api/settings/ai")
    assert response.json() == {"mode": "demo", "base_url": "", "model": "", "timeout_seconds": 45.0,
                               "api_key_configured": False, "configured": True}
    saved = update(client, base_url="https://model.test/v1/")
    assert saved.status_code == 200
    assert saved.json()["base_url"] == "https://model.test/v1"
    assert saved.json()["configured"] is True
    assert "api_key" not in saved.json()
    assert_secret_absent(saved)
    assert saved.headers["cache-control"] == "no-store"
    assert client.get("/health").json()["mode"] == "cloud"
    assert client.get("/api/state").json()["model"] == "offline-test-model"
    for owner in (client.app.state, client.app.state.ai, client.app.state.attempts,
                  client.app.state.plugin, client.app.state.image_checks):
        assert owner.settings.api_key == SECRET
    configuration = ModelSettingsFile(tmp_path / "settings-app")
    assert configuration.load(Settings()).api_key == SECRET
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "invalid-env-must-not-be-read")
    second = factory(settings=Settings(mode="demo"))
    assert second.get("/api/settings/ai").json() == saved.json()
    isolated = factory(name="other-settings-app")
    assert isolated.get("/api/settings/ai").json()["api_key_configured"] is False
    assert not list((tmp_path / "settings-app").glob(".model-settings-*.tmp"))


def test_blank_key_retains_clear_removes_and_remains_cleared_after_restart(factory):
    client = factory()
    assert update(client).status_code == 200
    assert update(client, api_key="", model="second-model").status_code == 200
    assert client.app.state.settings.api_key == SECRET
    assert update(client, api_key="", clear_api_key=True).json()["configured"] is False
    assert client.app.state.settings.api_key == ""
    restarted = factory()
    assert restarted.get("/api/settings/ai").json()["api_key_configured"] is False


def test_no_stale_key_is_retained_when_changing_provider(factory):
    client = factory()
    assert update(client).status_code == 200
    rejected = update(client, base_url="https://other-model.test/v1", api_key="")
    assert rejected.status_code == 422
    assert_secret_absent(rejected)
    assert client.app.state.settings.base_url == "https://model.test/v1"
    assert update(client, base_url="https://model.test/v2", api_key="").status_code == 200
    assert update(client, base_url="https://other-model.test", api_key="", clear_api_key=True).status_code == 200


@pytest.mark.parametrize("changes", [
    {"mode": "bad"}, {"mode": []}, {"model": "name\n"}, {"model": SECRET * 30},
    {"timeout_seconds": 0}, {"timeout_seconds": 181}, {"timeout_seconds": True}, {"timeout_seconds": 10 ** 400},
    {"timeout_seconds": "45"}, {"api_key": {"key": SECRET}},
    {"clear_api_key": True}, {"clear_api_key": "true"}, {"extra": SECRET},
    {"base_url": "http://remote-model.test"}, {"base_url": "https://model.test/?key=" + SECRET},
    {"base_url": "https://user:" + SECRET + "@model.test"},
    {"base_url": "https://model.test/#" + SECRET},
    {"base_url": "https://model.test:not-a-port"}, {"base_url": "https://[broken"},
    {"base_url": "https://model.test/v1/chat/completions"},
])
def test_invalid_config_never_echoes_credentials(factory, changes):
    client = factory()
    response = update(client, **changes)
    assert response.status_code == 422
    assert_secret_absent(response)
    assert client.app.state.settings.mode == "demo"


@pytest.mark.parametrize("base_url", ["http://localhost:8080/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"])
def test_local_compatible_endpoints_allowed(factory, base_url):
    assert update(factory(), base_url=base_url).status_code == 200


def test_cloud_config_can_be_saved_incomplete_without_network_call(factory):
    response = update(factory(), base_url="", model="", api_key="")
    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_origin_json_size_and_malformed_payload_boundaries(factory):
    client = factory()
    blocked = client.put("/api/settings/ai", json=config(), headers={"Origin": "https://other.test"})
    assert blocked.status_code == 403
    assert client.put("/api/settings/ai", data={"api_key": SECRET}).status_code == 415
    malformed = client.put("/api/settings/ai", content='{"api_key":"' + SECRET,
                           headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422
    assert_secret_absent(malformed)
    assert client.put("/api/settings/ai", content="x" * 32769,
                      headers={"Content-Type": "application/json"}).status_code == 413
    assert not client.app.state.store.path.with_name("model-settings.json").exists()


def test_pending_attempt_and_image_check_prevent_settings_change(factory):
    client = factory()
    store = client.app.state.store
    store.put("attempts", {"id": "pending-fixture", "status": "pending"})
    assert update(client).status_code == 409
    store.put("attempts", {"id": "pending-fixture", "status": "completed"})
    client.app.state.image_checks.running["fake-check"] = object()
    assert update(client).status_code == 409
    client.app.state.image_checks.running.clear()
    client.app.state.image_auto_review.running["fake-review"] = object()
    assert update(client).status_code == 409
    client.app.state.image_auto_review.running.clear()
    assert update(client).status_code == 200


def test_generation_holds_settings_until_request_finishes(factory):
    client = factory()
    client.post("/api/demo")
    state = client.get("/api/state").json()
    card = state["cards"][0]
    entered, release = threading.Event(), threading.Event()

    class SlowAI:
        async def generate_drafts(self, blocks, count):
            entered.set()
            await asyncio.to_thread(release.wait, 5)
            return DraftBatch(cards=[CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields})])

    client.app.state.ai = SlowAI()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, "/api/card-drafts",
                                 json={"block_ids": [card["evidence"][0]["block_id"]], "count": 1})
        try:
            assert entered.wait(5)
            blocked = update(client)
            assert blocked.status_code == 409
            assert client.app.state.settings.mode == "demo"
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200
    assert update(client).status_code == 200


def test_save_failure_keeps_runtime_and_existing_file(factory, monkeypatch):
    client = factory()
    assert update(client).status_code == 200
    original = client.app.state.settings
    path = client.app.state.store.path.with_name("model-settings.json")
    contents = path.read_bytes()

    def fail_replace(*args):
        raise OSError(SECRET)

    monkeypatch.setattr(type(path), "replace", fail_replace)
    response = update(client, model="new-model")
    assert response.status_code == 500
    assert_secret_absent(response)
    assert client.app.state.settings == original
    assert path.read_bytes() == contents


def test_plugin_status_reads_current_settings(factory):
    client = factory()
    connection = client.app.state.plugin.connection
    connection.configure()
    value = connection.read()
    headers = {"Authorization": "Bearer " + value["token"], "X-AnkiSense-Client": "settings-test-profile"}
    assert update(client, timeout_seconds=120).status_code == 200
    response = client.get("/api/plugin/v1/status", headers=headers)
    assert response.status_code == 200
    assert response.json()["ai"]["model"] == "offline-test-model"
    assert response.json()["limits"]["recommended_timeout_seconds"] == 150
    assert_secret_absent(response)
    assert update(client, mode="demo", api_key="").status_code == 200
    assert client.get("/api/plugin/v1/status", headers=headers).json()["ai"]["requires_cloud_confirmation"] is False


def test_corrupt_local_config_is_safely_reported(tmp_path):
    file = ModelSettingsFile(tmp_path)
    file.path.write_text(SECRET, encoding="utf-8")
    with pytest.raises(AIError) as error:
        file.load(Settings())
    assert SECRET not in str(error.value)


def test_stale_workbench_version_cannot_overwrite_card(factory):
    client = factory()
    client.post("/api/demo")
    card = client.get("/api/state").json()["cards"][0]
    body = {key: card[key] for key in CardUpdate.model_fields if key in card}
    first = client.put("/api/cards/" + card["id"], json={**body, "expected_version": card["version"], "status": "approved"})
    assert first.status_code == 200
    assert "expected_version" not in first.json()
    stale = client.put("/api/cards/" + card["id"], json={**body, "expected_version": card["version"]})
    assert stale.status_code == 409
    assert client.app.state.store.get("cards", card["id"])["status"] == "approved"
    # Existing clients without an expected version retain their original contract.
    assert client.put("/api/cards/" + card["id"], json=body).status_code == 200
