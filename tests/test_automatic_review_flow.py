"""Offline API flow: saving starts review and current results release learning."""
import asyncio
import time
from threading import Event

import pytest

from ankisense.approval import valid_machine_approval
from ankisense.llm import AIError, Settings
from ankisense.schemas import CardDraft, DraftBatch
from ankisense.sources import save_page_revision
from test_card_auto_review import bind_imported_pdf, good_review, correct_reference_diagnosis, minimal_answer_fixture
from test_figure_recommendations import context, save
from test_image_auto_review import saved


def wait_card(client, identifier, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        card = client.app.state.store.get("cards", identifier)
        if predicate(card):
            return card
        time.sleep(.01)
    pytest.fail(f"Background fixture did not finish: {card}")


@pytest.fixture
def flow(context, monkeypatch):
    client, original, _, storage = context
    bind_imported_pdf(client, original, storage)
    app = client.app
    store = app.state.store
    page = store.get("source_pages", original["document_id"] + "-p2")
    page.update(status="pending", reviewed_at=None)
    store.put("source_pages", page)
    # Keep the pre-existing fixture card out of the export catalog.
    store.put("cards", {**original, "status": "draft"})
    settings = Settings(mode="cloud", model="offline-review", base_url="https://model.invalid/v1", api_key="test-only")
    app.state.settings = settings
    app.state.card_auto_review.settings = settings
    gate, entered, calls = Event(), Event(), []

    async def generate(blocks, count, **options):
        return DraftBatch(cards=[CardDraft.model_validate({key: original[key] for key in CardDraft.model_fields})])

    async def review(card, blocks):
        calls.append(card.question)
        entered.set()
        for _ in range(500):
            if gate.is_set():
                break
            await asyncio.sleep(.01)
        else:
            raise AIError("Offline fixture gate timed out")
        return good_review(card.model_dump())

    monkeypatch.setattr(app.state.ai, "generate_drafts", generate)
    monkeypatch.setattr(app.state.ai, "review_card", review)
    monkeypatch.setattr(app.state.ai, "diagnose_answer", correct_reference_diagnosis)
    monkeypatch.setattr(app.state.ai, "minimal_answer_for_question", minimal_answer_fixture)
    try:
        yield client, page, gate, entered, calls
    finally:
        gate.set()


def generate(client, page):
    response = client.post("/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1})
    assert response.status_code == 200, response.text
    return response.json()["cards"][0]


def test_generate_and_save_automatically_review_without_page_confirmation(flow):
    client, page, gate, entered, calls = flow
    card = generate(client, page)
    assert card["status"] == "draft" and card["review_state"] in {"queued", "running"}
    assert entered.wait(3), "Response must return before review completes"
    assert card["id"] not in client.get("/api/export").text
    gate.set()
    approved = wait_card(client, card["id"], lambda item: item["status"] == "approved")
    assert approved["version"] == 1 and valid_machine_approval(client.app.state.store, approved)
    assert client.app.state.store.get("source_pages", page["id"])["status"] == "pending"
    assert approved["id"] in client.get("/api/export").text
    gate.clear()
    entered.clear()
    response = save(client, approved, status="draft", expected_version=approved["version"], question="请解释栈的出入顺序。")
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["version"] == 2 and edited["status"] == "draft" and not edited.get("approval")
    assert entered.wait(3)
    gate.set()
    second = wait_card(client, card["id"], lambda item: item["status"] == "approved")
    assert second["version"] == 2 and second["approval"]["review_id"] != approved["approval"]["review_id"]
    assert len(calls) == 2


def test_document_job_integrates_progress_settings_guard_and_machine_approval(flow, monkeypatch):
    client, page, review_gate, review_entered, calls = flow
    ai = client.app.state.ai
    generate = ai.generate_drafts
    generation_gate, generation_entered = Event(), Event()
    generation_calls = []

    async def held_generate(blocks, count, **options):
        generation_calls.append((count, options))
        generation_entered.set()
        while not generation_gate.is_set():
            await asyncio.sleep(.01)
        return await generate(blocks, count, **options)

    monkeypatch.setattr(ai, "generate_drafts", held_generate)
    plan = client.post(f"/api/documents/{page['document_id']}/generation-plan", json={"count": 1})
    assert plan.status_code == 200, plan.text
    assert plan.json()["scope"] == "all" and not generation_calls and not calls
    body = {"document_id": page["document_id"], "count": 1,
            "source_fingerprint": plan.json()["source_fingerprint"], "request_id": "whole-document-fixture"}
    try:
        response = client.post("/api/generation-jobs", json=body)
        assert response.status_code == 200, response.text
        job_id = response.json()["id"]
        assert generation_entered.wait(3)
        state = client.get("/api/state").json()
        assert any(job["id"] == job_id and job["status"] == "running" for job in state["generation_jobs"])
        settings = client.put("/api/settings/ai", json={"mode": "demo", "base_url": "https://model.invalid/v1",
                                                       "model": "offline-review", "timeout_seconds": 45})
        assert settings.status_code == 409, settings.text
        replay = client.post("/api/generation-jobs", json=body)
        assert replay.status_code == 200 and replay.json()["id"] == job_id
        assert len(generation_calls) == 1
    finally:
        generation_gate.set()
    assert review_entered.wait(3)
    state = client.get("/api/state").json()
    card = next(card for card in state["cards"] if card.get("generation_job_id") == job_id)
    assert card["status"] == "draft" and card["id"] not in client.get("/api/export").text
    review_gate.set()
    approved = wait_card(client, card["id"], lambda item: item["status"] == "approved")
    assert valid_machine_approval(client.app.state.store, approved)
    assert card["id"] in client.get("/api/export").text
    job = client.get(f"/api/generation-jobs/{job_id}").json()
    assert job["status"] == "completed" and job["generated_count"] == job["target_count"] == 1
    assert generation_calls == [(1, {"allow_fewer": True, "avoid_questions": ["栈遵循什么顺序？"]})]
    assert client.app.state.store.get("source_pages", page["id"])["status"] == "pending"


def test_failed_background_review_stays_draft_until_explicit_retry(flow, monkeypatch):
    client, page, gate, _entered, calls = flow
    async def fail(card, blocks):
        calls.append("failure")
        raise AIError("Offline timeout")
    monkeypatch.setattr(client.app.state.ai, "review_card", fail)
    card = generate(client, page)
    failed = wait_card(client, card["id"], lambda item: item.get("review_state") == "needs_review")
    assert failed["status"] == "draft" and not failed.get("approval")
    for _ in range(3):
        assert client.get(f"/api/cards/{card['id']}/auto-review").json()["retryable"]
        assert client.get("/api/state").status_code == 200
    assert calls == ["failure"]
    async def success(card, blocks):
        calls.append("retry")
        return good_review(card.model_dump())
    monkeypatch.setattr(client.app.state.ai, "review_card", success)
    response = client.post(f"/api/cards/{card['id']}/auto-review", json={"expected_version": card["version"]})
    assert response.status_code == 200, response.text
    assert response.json()["card"]["status"] == "approved" and calls == ["failure", "retry"]


def test_source_edit_during_review_cannot_release_old_card_and_can_be_repaired(flow):
    client, page, gate, entered, calls = flow
    card = generate(client, page)
    assert entered.wait(3)
    updated = save_page_revision(client.app.state.store, page["document_id"], page["page"], page["revision"],
                                 {"text": page["text"] + "空栈不能出栈。", "status": "pending"}, "proofread")
    gate.set()
    deadline = time.monotonic() + 5
    while client.app.state.card_auto_review.running and time.monotonic() < deadline:
        time.sleep(.01)
    stale = client.app.state.store.get("cards", card["id"])
    assert stale["status"] == "draft" and stale["source_outdated"] and not stale.get("approval")
    repaired = save(client, stale, status="draft", expected_version=stale["version"],
                    evidence=[{"block_id": updated["block_ids"][0], "quote": card["evidence"][0]["quote"]}])
    assert repaired.status_code == 200, repaired.text
    approved = wait_card(client, card["id"], lambda item: item["status"] == "approved")
    assert not approved["source_outdated"] and valid_machine_approval(client.app.state.store, approved)
    assert len(calls) == 2


def test_queue_failure_returns_saved_cards_instead_of_failing_generation(flow, monkeypatch):
    client, page, _gate, _entered, calls = flow
    async def unavailable(*args):
        raise RuntimeError("offline queue unavailable")
    monkeypatch.setattr(client.app.state.card_auto_review, "queue", unavailable)
    card = generate(client, page)
    assert card["status"] == "draft" and card["review_state"] == "interrupted"
    assert client.get(f"/api/cards/{card['id']}/auto-review").json()["retryable"]
    response = save(client, card, status="draft", expected_version=card["version"], question="请说明出栈顺序。")
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2 and response.json()["review_state"] == "interrupted"
    assert len(client.app.state.store.all("cards")) == 2 and calls == []


def test_manual_approval_rejects_old_picture_after_source_revision(saved):
    client, card, _media, _job, _calls, _service, _storage = saved
    store = client.app.state.store
    page = store.get("source_pages", card["document_id"] + "-p2")
    updated = save_page_revision(store, page["document_id"], page["page"], page["revision"],
                                 {"text": page["text"] + "空栈不能出栈。", "status": "pending"}, "proofread")
    current = store.get("cards", card["id"])
    response = save(client, current, status="approved", expected_version=current["version"],
                    evidence=[{"block_id": updated["block_ids"][0], "quote": card["evidence"][0]["quote"]}])
    assert response.status_code == 409, response.text
    assert store.get("cards", card["id"]) == current
