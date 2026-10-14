"""Plugin protocol boundaries, cloud opt-in and shared durable practice."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ankisense.llm import AIError, AIService, Settings
from ankisense.plugin_api import PREFIX, PluginConnection, local_base_url
from ankisense.schemas import CardUpdate


class OfflineAI:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.demo = AIService(Settings(mode="demo"))

    async def diagnose_answer(self, card, blocks, answer):
        self.calls += 1
        if self.fail:
            raise AIError("模拟服务暂时不可用")
        return await self.demo.diagnose_answer(card, blocks, answer)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    instances = []

    def make(cloud=False, enable=True):
        storage = tmp_path / f"instance-{len(instances)}"
        settings = Settings(mode="cloud", base_url="https://never-call.invalid", model="offline-fixture", api_key="PRIVATE-MODEL-KEY") if cloud else Settings(mode="demo")
        ai = OfflineAI()
        app = create_app(data_dir=storage, settings=settings, ai_service=ai)
        connection = app.state.plugin.connection
        if enable:
            connection.configure()
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        client.__enter__()
        instances.append(client)
        assert client.post("/api/demo").status_code == 200
        value = connection.read()
        headers = {"Authorization": "Bearer " + (value["token"] if value else "x" * 43), "X-AnkiSense-Client": "profile-test-001"}
        return client, headers, ai

    yield make
    for client in instances:
        client.__exit__(None, None, None)


def approved(client):
    card = next(item for item in client.app.state.store.all("cards") if item["question"] == "栈的出入顺序规则是什么？")
    body = {key: card[key] for key in CardUpdate.model_fields if key in card}
    response = client.put(f"/api/cards/{card['id']}", json={**body, "status": "approved"})
    assert response.status_code == 200
    return response.json()


def payload(card, **changes):
    return {"card_id": card["id"], "card_version": card["version"], "answer": "后进先出", "request_id": "answer-request-0001", **changes}


def test_plugin_is_disabled_until_connection_is_explicitly_initialized(make_client):
    client, headers, _ = make_client(enable=False)
    connection = client.app.state.plugin.connection
    assert not connection.path.exists()
    response = client.get(PREFIX + "/status", headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "plugin_disabled"
    assert not connection.path.exists()


def test_status_requires_local_credential_and_profile_but_never_exposes_secrets(make_client):
    client, headers, _ = make_client(cloud=True)
    assert client.get(PREFIX + "/status").status_code == 401
    assert client.get(PREFIX + "/status", headers={**headers, "Authorization": "Bearer " + "z" * 43}).status_code == 401
    assert client.get(PREFIX + "/status", headers={"Authorization": headers["Authorization"]}).status_code == 400
    response = client.get(PREFIX + "/status", headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["protocol_version"] == 1
    assert result["capabilities"]["schedule_write"] is False
    assert result["ai"]["requires_cloud_confirmation"] is True
    assert result["ai"]["images_in_answer_analysis"] is False
    assert "PRIVATE-MODEL-KEY" not in response.text
    assert headers["Authorization"].split(" ")[1] not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_rotation_disable_and_reopen_keep_server_identity(make_client):
    client, headers, _ = make_client()
    connection = client.app.state.plugin.connection
    server_id = connection.public()["server_id"]
    connection.configure(rotate=True)
    assert client.get(PREFIX + "/status", headers=headers).status_code == 401
    fresh = {**headers, "Authorization": "Bearer " + connection.read()["token"]}
    assert client.get(PREFIX + "/status", headers=fresh).json()["server_id"] == server_id
    assert PluginConnection(connection.path.parent).read() == connection.read()
    connection.configure(enabled=False)
    assert client.get(PREFIX + "/cards", headers=fresh).status_code == 503
    connection.configure()
    assert connection.public()["server_id"] == server_id
    assert client.get(PREFIX + "/cards", headers=fresh).status_code == 200


def test_manifest_detail_and_media_are_consistent_and_guarded(make_client):
    client, headers, _ = make_client()
    assert client.get(PREFIX + "/cards", headers=headers).json()["cards"] == []
    card = approved(client)
    output = io.BytesIO()
    Image.new("RGB", (40, 20), "green").save(output, "PNG")
    media = client.post("/api/card-media", files={"file": ("x.png", output.getvalue(), "image/png")}).json()
    body = {key: card[key] for key in CardUpdate.model_fields if key in card}
    card = client.put(f"/api/cards/{card['id']}", json={**body, "images": [{"media_id": media["id"], "side": "question", "caption": "栈图"}]}).json()
    catalog = client.get(PREFIX + "/cards", headers=headers).json()
    assert catalog["complete"] is True and len(catalog["cards"]) == 1
    summary = catalog["cards"][0]
    response = client.get(PREFIX + f"/cards/{card['id']}", params={"expected_hash": summary["content_hash"]}, headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["model"]["id"] == 1786831001 and detail["deck"]["id"] == 1786831002
    assert detail["guid"] == summary["guid"]
    assert "<img" in detail["fields"]["Question"] and "<img" not in detail["fields"]["Answer"]
    asset = detail["media"][0]
    image = client.get(PREFIX + f"/media/{media['id']}", params={"sha256": asset["sha256"]}, headers=headers)
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert client.get(PREFIX + f"/media/{media['id']}", headers=headers).status_code == 422
    assert client.get(PREFIX + f"/media/{media['id']}", params={"sha256": "0" * 64}, headers=headers).status_code == 409
    assert client.get(PREFIX + f"/cards/{card['id']}", params={"expected_hash": "0" * 64}, headers=headers).status_code == 409
    stored = client.app.state.store.get("cards", card["id"])
    client.app.state.store.put("cards", {**stored, "status": "draft"})
    assert client.get(PREFIX + f"/cards/{card['id']}", headers=headers).status_code == 404
    assert client.get(PREFIX + f"/media/{media['id']}", params={"sha256": asset["sha256"]}, headers=headers).status_code == 404


@pytest.mark.parametrize("change", ["version", "unapproved", "outdated"])
def test_changed_card_is_a_definite_unaccepted_submission(make_client, change):
    client, headers, ai = make_client()
    card = approved(client)
    if change == "version":
        body = {key: card[key] for key in CardUpdate.model_fields if key in card}
        changed = client.put(f"/api/cards/{card['id']}", json={**body, "question": card["question"] + " 请解释。"})
        assert changed.status_code == 200, changed.text
    else:
        client.app.state.store.put("cards", {**card, **({"status": "draft"} if change == "unapproved"
                                                  else {"source_outdated": True})})

    response = client.post(PREFIX + "/attempts", json=payload(card), headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "card_changed"
    assert client.app.state.store.all("attempts") == []
    assert ai.calls == 0


def test_plugin_practice_is_idempotent_versioned_and_profile_scoped(make_client):
    client, headers, ai = make_client()
    card = approved(client)
    body = payload(card)
    first = client.post(PREFIX + "/attempts", json=body, headers=headers)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "completed" and result["origin"] == "anki"
    assert result["card_snapshot"]["version"] == card["version"]
    assert client.post(PREFIX + "/attempts", json=body, headers=headers).json()["id"] == result["id"]
    assert ai.calls == 1
    other = {**headers, "X-AnkiSense-Client": "profile-test-002"}
    assert client.get(PREFIX + f"/attempts/{result['id']}", headers=other).status_code == 404
    assert client.post(PREFIX + f"/attempts/{result['id']}/retry", json={"expected_retry_count": 0}, headers=other).status_code == 404
    separate = client.post(PREFIX + "/attempts", json=body, headers=other)
    assert separate.status_code == 200 and separate.json()["id"] != result["id"]
    assert ai.calls == 2
    assert client.post(PREFIX + "/attempts", json={**body, "answer": "先进先出"}, headers=headers).status_code == 409
    client.app.state.store.put("cards", {**card, "version": card["version"] + 1})
    assert client.post(PREFIX + "/attempts", json=body, headers=headers).json()["id"] == result["id"]
    stale = client.post(PREFIX + "/attempts", json={**body, "request_id": "answer-request-0002"}, headers=headers)
    assert stale.status_code == 409 and ai.calls == 2


def test_cloud_calls_require_opt_in_but_empty_answers_and_cached_replay_do_not(make_client):
    client, headers, ai = make_client(cloud=True)
    card = approved(client)
    response = client.post(PREFIX + "/attempts", json=payload(card), headers=headers)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "cloud_confirmation_required"
    assert ai.calls == 0 and not client.app.state.store.all("attempts")
    blank = client.post(PREFIX + "/attempts", json=payload(card, answer=" \n ", request_id="blank-answer-0001"), headers=headers)
    assert blank.json()["status"] == "unanswered" and ai.calls == 0
    response = client.post(PREFIX + "/attempts", json=payload(card, allow_cloud=True), headers=headers)
    assert response.json()["status"] == "completed" and ai.calls == 1
    assert client.post(PREFIX + "/attempts", json=payload(card), headers=headers).json()["id"] == response.json()["id"]
    assert ai.calls == 1


def test_failed_plugin_analysis_retains_answer_and_retry_has_explicit_cost_gate(make_client):
    client, headers, ai = make_client(cloud=True)
    card = approved(client)
    ai.fail = True
    result = client.post(PREFIX + "/attempts", json=payload(card, allow_cloud=True), headers=headers).json()
    assert result["status"] == "failed" and result["answer"] == "后进先出"
    route = PREFIX + f"/attempts/{result['id']}/retry"
    assert client.post(route, json={"expected_retry_count": 0}, headers=headers).status_code == 409
    assert ai.calls == 1
    for retry_count in [1, 2]:
        response = client.post(route, json={"allow_cloud": True, "expected_retry_count": retry_count - 1}, headers=headers)
        assert response.status_code == 200 and response.json()["retry_count"] == retry_count
    assert client.post(route, json={"allow_cloud": True, "expected_retry_count": 2}, headers=headers).status_code == 409
    assert ai.calls == 3


def test_plugin_validation_does_not_echo_input_and_cross_origin_cannot_post(make_client):
    client, headers, ai = make_client()
    card = approved(client)
    response = client.post(PREFIX + "/attempts", json=payload(card, card_version="PRIVATE-ANSWER-STRING"), headers=headers)
    assert response.status_code == 422 and "PRIVATE-ANSWER-STRING" not in response.text
    assert response.json()["detail"]["code"] == "invalid_request"
    assert client.post(PREFIX + "/attempts", json=payload(card), headers={**headers, "Origin": "https://unrelated.invalid"}).status_code == 403
    assert ai.calls == 0


def test_openapi_documents_plugin_auth_profile_and_version_contract(make_client):
    client, _, _ = make_client()
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for path, methods in schema["paths"].items():
        if not path.startswith(PREFIX):
            continue
        for operation in methods.values():
            assert operation["security"] == [{"HTTPBearer": []}]
            assert any(parameter["name"] == "X-AnkiSense-Client" and parameter["in"] == "header" for parameter in operation["parameters"])
    fields = schema["components"]["schemas"]["PluginAttempt"]
    assert "card_version" in fields["required"]
    assert fields["properties"]["allow_cloud"]["default"] is False


@pytest.mark.parametrize("url", ["https://example.org", "http://127.0.0.1@evil.test", "http://localhost/a", "http://localhost:0", "http://localhost:", "\0http://localhost", "http://localhost?token=a", "http://localhost#x", "ftp://localhost", "http://localhost:99999"])
def test_connection_url_never_targets_a_remote_or_ambiguous_endpoint(url):
    with pytest.raises(ValueError):
        local_base_url(url)


def test_configured_local_url_is_canonical_and_accepted_by_sync(make_client):
    client, headers, _ = make_client()
    client.app.state.plugin.connection.configure(base_url="HTTP://LOCALHOST:08765/")
    assert client.app.state.plugin.connection.read()["base_url"] == "http://localhost:8765"
    assert client.get(PREFIX + "/cards", headers=headers).status_code == 200


def test_malformed_connection_file_is_not_overwritten(tmp_path):
    connection = PluginConnection(tmp_path)
    connection.path.write_text("damaged config", encoding="utf-8")
    with pytest.raises(ValueError):
        connection.configure()
    assert connection.path.read_text() == "damaged config"
