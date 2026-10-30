"""Automatic approval is usable across clients, but bound to actual review data."""

import copy

import pytest

from ankisense.approval import card_binding_hash, valid_machine_approval
from ankisense.llm import Settings
from ankisense.plugin_sync import PluginSync
from ankisense.schemas import Diagnosis
from ankisense.sources import save_page_revision
from test_card_auto_review import bind_imported_pdf, good_review, post, ready, minimal_answer_fixture
from test_figure_recommendations import context, save
from test_image_auto_review import saved


BASE = "http://127.0.0.1:8765"
EXPORTS = ("/api/export", "/api/export/media", "/api/export/apkg")


@pytest.fixture(autouse=True)
def local_diagnosis(context, monkeypatch):
    client, _card, _adapter, _storage = context

    async def diagnose(card, blocks, answer):
        return Diagnosis(verdict="correct", addressed_point_ids=[point.point_id for point in card.rubric],
                         missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[blocks[0].block_id], reason="离线测试回答与引用一致。")

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", diagnose)
    monkeypatch.setattr(client.app.state.ai, "minimal_answer_for_question", minimal_answer_fixture)


def pending(store, card):
    identifier = card["document_id"] + "-p2"
    page = store.get("source_pages", identifier)
    store.put("source_pages", {**page, "status": "pending", "reviewed_at": None})
    return store.get("source_pages", identifier)


def assert_usable(client, card):
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 200, (endpoint, response.text)
    sync = PluginSync(client.app.state.store, client.app.state.store.path.parent)
    assert [item["id"] for item in sync.catalog(BASE)["cards"]] == [card["id"]]
    assert sync.card(card["id"], BASE)["version"] == card["version"]
    response = client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出",
                                                "request_id": "approved-machine-practice"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


def test_pending_source_machine_approval_is_usable_without_manual_confirmation(ready):
    client, card, service, calls = ready
    page = pending(service.store, card)
    response = post(client, card)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "auto_pass"
    approved = service.store.get("cards", card["id"])
    assert approved["status"] == "approved" and approved["version"] == card["version"]
    assert approved["approval"]["method"] == "machine"
    assert valid_machine_approval(service.store, approved)
    assert service.store.get("source_pages", page["id"])["status"] == "pending"
    assert_usable(client, approved)
    assert calls == [True]
    confirmed = save_page_revision(service.store, card["document_id"], 2, page["revision"],
                                   {"text": page["text"], "status": "reviewed", "reviewed_at": "now"}, "proofread")
    assert confirmed["revision"] == page["revision"]
    assert valid_machine_approval(service.store, approved)
    assert client.get("/api/export").status_code == 200


@pytest.mark.parametrize("mutation", ["missing_review", "old_policy", "retryable", "not_passed",
                                     "other_card", "answer", "version", "source_block", "source_page",
                                     "attachment"])
def test_forged_or_changed_machine_approval_is_rejected_everywhere(ready, mutation):
    client, card, service, _calls = ready
    pending(service.store, card)
    assert post(client, card).json()["status"] == "auto_pass"
    approved = service.store.get("cards", card["id"])
    record = service.store.get("card_auto_reviews", approved["approval"]["review_id"])
    if mutation == "missing_review":
        approved["approval"]["review_id"] = "cardreview-not-real"
    elif mutation == "old_policy":
        record["policy_version"] = "whole-card-review-v1"
    elif mutation == "retryable":
        record["retryable"] = True
    elif mutation == "not_passed":
        record["status"] = "needs_review"
    elif mutation == "other_card":
        record["card_id"] = "a-different-card"
    elif mutation == "answer":
        approved["answer"] = "先进先出。"
    elif mutation == "version":
        approved["version"] += 1
    elif mutation == "attachment":
        approved["images"] = [{"media_id": "img-" + "b" * 32, "side": "question"}]
    elif mutation == "source_block":
        block = service.store.get("blocks", card["evidence"][0]["block_id"])
        block["text"] += "被外部改写的额外内容。"
        service.store.put("blocks", block)
    else:
        page = service.store.get("source_pages", card["document_id"] + "-p2")
        page["text"] += "被外部改写的额外内容。"
        service.store.put("source_pages", page)
    service.store.put("card_auto_reviews", record)
    service.store.put("cards", approved)
    assert not valid_machine_approval(service.store, approved)
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 409, (endpoint, response.text)
    sync = PluginSync(service.store, service.store.path.parent)
    assert sync.catalog(BASE)["cards"] == []
    assert sync.catalog(BASE)["excluded"][0]["id"] == card["id"]
    assert client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出",
                                              "request_id": "invalid-machine-practice"}).status_code == 409


def test_binding_ignores_approval_metadata_but_includes_attachment_details(ready):
    _client, card, _service, _calls = ready
    assert card_binding_hash(card) == card_binding_hash({**card, "status": "approved", "approval": {},
                                                        "review_state": "ready", "updated_at": "later"})
    images = [{"media_id": "img-" + "a" * 32, "side": "answer", "caption": "original"}]
    first = card_binding_hash({**card, "images": images})
    assert first != card_binding_hash({**card, "images": [{**images[0], "side": "question"}]})
    assert first != card_binding_hash({**card, "images": [{**images[0], "caption": "changed"}]})


def test_machine_approved_image_has_current_source_and_independent_review(saved, monkeypatch):
    client, card, media, _job, _calls, image_service, storage = saved
    bind_imported_pdf(client, card, storage)
    page = pending(image_service.store, card)
    service = client.app.state.card_auto_review
    service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1",
                                model="fixture-reviewer", api_key="test-key")

    async def review(_card, _blocks):
        return copy.deepcopy(good_review(card))

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "auto_pass", result.json()
    approved = service.store.get("cards", card["id"])
    assert valid_machine_approval(service.store, approved)
    assert_usable(client, approved)
    sync = PluginSync(service.store, storage)
    assert sync.media(media["id"])[0]["id"] == media["id"]
    assert service.store.get("source_pages", page["id"])["status"] == "pending"
    image_record = service.store.all("image_auto_reviews")[-1]
    service.store.put("image_auto_reviews", {**image_record, "status": "needs_review"})
    assert not valid_machine_approval(service.store, approved)
    assert client.get("/api/export/apkg").status_code == 409
    assert sync.catalog(BASE)["cards"] == []


def test_human_card_approval_can_use_current_pending_sources(ready):
    client, card, service, _calls = ready
    pending(service.store, card)
    response = save(client, card, status="approved")
    assert response.status_code == 200, response.text
    approved = response.json()
    assert approved["approval"]["method"] == "human"
    assert_usable(client, approved)
