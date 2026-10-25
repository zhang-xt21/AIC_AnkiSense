"""Changing a model stops the old generation before resuming its checkpoints."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import HTTPException
import pytest

from test_automatic_import import client, VisualAI, lecture_pdf, upload, wait_import


def body(**changes):
    return {"mode": "cloud", "base_url": "https://offline.invalid/v1", "model": "new-model",
            "timeout_seconds": 45, "api_key": "", "resume_active_jobs": True, **changes}


def pending(client, monkeypatch, *, second_batch=False, cleanup_gate=None):
    entered, stopped = threading.Event(), threading.Event()
    old = client.app.state.ai
    original = old.extract_knowledge_visual
    calls = []
    async def block(blocks, focus, images, pages):
        calls.append(pages)
        if not second_batch or len(calls) == 2:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                if cleanup_gate:
                    await asyncio.to_thread(cleanup_gate.wait, 5)
                raise
        return await original(blocks, focus, images, pages)
    old.extract_knowledge_visual = block
    new = VisualAI()
    new_calls = []
    next_extract = new.extract_knowledge_visual
    async def resumed(blocks, focus, images, pages):
        assert stopped.is_set(), "New model ran before old request stopped"
        new_calls.append(pages)
        return await next_extract(blocks, focus, images, pages)
    new.extract_knowledge_visual = resumed
    monkeypatch.setattr("ankisense.model_settings.AIService", lambda settings: new)
    record = upload(client, lecture_pdf(6 if second_batch else 2))
    assert entered.wait(5)
    current = client.get(f"/api/material-imports/{record['id']}").json()
    return record, current["job"], stopped, calls, new_calls


def test_switch_keeps_successful_cards_and_retries_only_unfinished_batch(client, monkeypatch):
    record, job, stopped, old_calls, new_calls = pending(client, monkeypatch, second_batch=True)
    assert len(job["card_ids"]) == 3
    before = {identifier: copy.deepcopy(client.app.state.store.get("cards", identifier)) for identifier in job["card_ids"]}
    result = client.put("/api/settings/ai", json=body())
    assert result.status_code == 200, result.text
    assert result.json()["task_switch"] == {"resumed": 1, "not_resumed": 0, "preparing": 0}
    done = wait_import(client, record)
    assert done["status"] == "completed", done
    assert stopped.is_set() and old_calls == [[1, 2, 3], [4, 5, 6]]
    assert new_calls == [[4, 5, 6]]
    assert done["job"]["id"] == job["id"] and len(done["job"]["card_ids"]) == 6
    for identifier, card in before.items():
        assert client.app.state.store.get("cards", identifier) == card
    assert client.app.state.auto_cards._job(job["id"])["config"]["model"] == "new-model"
    assert client.get("/api/settings/ai").json()["connection"]["status"] == "unverified"


@pytest.mark.parametrize("changes,expected", [
    ({"resume_active_jobs": False}, 409),
    ({"resume_active_jobs": "true"}, 422),
    ({"mode": "demo"}, 422),
    ({"base_url": "https://other.invalid/v1"}, 422),
])
def test_invalid_or_unconfirmed_switch_does_not_stop_the_old_task(client, monkeypatch, changes, expected):
    record, job, stopped, _, _ = pending(client, monkeypatch)
    previous = client.app.state.settings
    response = client.put("/api/settings/ai", json=body(**changes))
    assert response.status_code == expected, response.text
    assert not stopped.is_set() and client.app.state.settings == previous
    assert client.app.state.auto_cards.get(job["id"])["status"] == "running"
    client.post(f"/api/material-imports/{record['id']}/cancel")


def test_existing_cancelled_completed_and_failed_jobs_do_not_restart(client, monkeypatch):
    record = wait_import(client, upload(client))
    store = client.app.state.store
    for state in ("cancelled", "failed", "partial"):
        store.put("auto_card_jobs", {**copy.deepcopy(store.get("auto_card_jobs", record["job"]["id"])), "id": "old-" + state, "status": state})
    def unexpected(*args, **kwargs):
        pytest.fail("Historical jobs must not be restarted")
    monkeypatch.setattr(client.app.state.auto_cards, "retry", unexpected)
    result = client.put("/api/settings/ai", json=body())
    assert result.status_code == 200
    assert result.json()["task_switch"]["resumed"] == 0
    assert store.get("auto_card_jobs", record["job"]["id"])["status"] == "completed"
    assert all(store.get("auto_card_jobs", "old-" + state)["status"] == state for state in ("cancelled", "failed", "partial"))


def test_unchanged_settings_do_not_cancel_or_reissue_current_request(client, monkeypatch):
    record, _, stopped, old_calls, new_calls = pending(client, monkeypatch)
    response = client.put("/api/settings/ai", json=body(model="offline-visual"))
    assert response.status_code == 200, response.text
    assert response.json()["task_switch"]["unchanged"] == 1
    assert not stopped.is_set() and len(old_calls) == 1 and new_calls == []
    client.post(f"/api/material-imports/{record['id']}/cancel")


def test_save_failure_preserves_old_config_and_leaves_cards_paused(client, monkeypatch):
    record, job, stopped, _, new_calls = pending(client, monkeypatch, second_batch=True)
    previous = client.app.state.settings
    def fail(*args):
        raise HTTPException(500, "fixture save error")
    monkeypatch.setattr("ankisense.model_settings.ModelSettingsFile.save", fail)
    response = client.put("/api/settings/ai", json=body())
    assert response.status_code == 500 and "已暂停" in response.json()["detail"]
    assert stopped.is_set() and client.app.state.settings == previous
    assert not client.app.state.model_settings_switching and new_calls == []
    after = client.app.state.auto_cards.get(job["id"])
    assert after["status"] == "cancelled" and after["card_ids"] == job["card_ids"]


def test_resume_failure_reports_saved_config_without_claiming_resumed(client, monkeypatch):
    _, job, stopped, _, _ = pending(client, monkeypatch)
    async def unavailable(*args, **kwargs):
        raise HTTPException(409, "fixture changed sources")
    monkeypatch.setattr(client.app.state.auto_cards, "_retry", unavailable)
    response = client.put("/api/settings/ai", json=body())
    assert response.status_code == 200
    assert response.json()["task_switch"] == {"resumed": 0, "not_resumed": 1, "preparing": 0}
    assert client.app.state.settings.model == "new-model" and stopped.is_set()
    assert client.app.state.auto_cards.get(job["id"])["status"] == "cancelled"


def test_switch_blocks_new_mutations_until_old_request_finishes(client, monkeypatch):
    release = threading.Event()
    record, _, stopped, _, _ = pending(client, monkeypatch, cleanup_gate=release)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.put, "/api/settings/ai", json=body())
        try:
            assert stopped.wait(5)
            assert client.app.state.model_settings_switching
            assert client.post("/api/card-drafts", json={}).status_code == 409
            assert client.put("/api/settings/ai", json=body()).status_code == 409
            assert client.get("/api/settings/ai").json()["model"] == "offline-visual"
        finally:
            release.set()
        assert future.result(timeout=10).status_code == 200
    assert wait_import(client, record)["status"] == "completed"


def test_local_import_uses_new_config_when_its_preparation_finishes(client, monkeypatch):
    from ankisense.automatic_import import parse_pdf_document
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return parse_pdf_document(*args, **kwargs)
    monkeypatch.setattr("ankisense.automatic_import.parse_pdf_document", slow)
    monkeypatch.setattr("ankisense.model_settings.AIService", lambda settings: VisualAI())
    record = upload(client)
    try:
        assert entered.wait(5)
        response = client.put("/api/settings/ai", json=body())
        assert response.status_code == 200, response.text
        assert response.json()["task_switch"] == {"resumed": 0, "not_resumed": 0, "preparing": 1}
    finally:
        release.set()
    done = wait_import(client, record)
    assert done["status"] == "completed", done
    assert client.app.state.auto_cards._job(done["job"]["id"])["config"]["model"] == "new-model"
