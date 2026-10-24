"""Picture-dependent cards cannot escape review before their source image exists."""

import copy
import io
import json
import zipfile

import pytest
from fastapi import HTTPException

from ankisense.approval import card_binding_hash, require_required_image, valid_machine_approval
from ankisense.plugin_sync import PluginSync
from ankisense.schemas import Diagnosis
from test_card_auto_review import bind_imported_pdf, good_review, post, ready
from test_figure_recommendations import context
from test_image_auto_review import GOOD, saved


BASE = "http://127.0.0.1:8765"
EXPORTS = ("/api/export", "/api/export/media", "/api/export/apkg")


def intent(card, need="required"):
    return {"need": need, "reason": "题目要求根据讲义图示判断。",
            "block_ids": [card["evidence"][0]["block_id"]]}


@pytest.fixture
def required_picture(saved, monkeypatch):
    client, card, media, job, image_calls, image_service, storage = saved
    bind_imported_pdf(client, card, storage)
    card["image_intent"] = intent(card)
    card["images"][0]["side"] = "question"
    image_service.store.put("cards", card)
    service = client.app.state.card_auto_review
    service.settings = image_service.settings
    text_calls = []

    async def review(_card, _blocks):
        text_calls.append(True)
        return copy.deepcopy(good_review(card))

    async def diagnose(draft, blocks, answer):
        return Diagnosis(verdict="correct", addressed_point_ids=[point.point_id for point in draft.rubric],
                         missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[blocks[0].block_id], reason="离线测试回答与引用一致。")

    monkeypatch.setattr(client.app.state.ai, "review_card", review)
    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", diagnose)
    return client, card, media, service, image_calls, text_calls


def test_image_intent_binding_keeps_legacy_and_explicit_none_receipts_compatible(ready):
    client, card, service, calls = ready
    assert card_binding_hash(card) == card_binding_hash({**card, "image_intent": intent(card, "none")})
    assert card_binding_hash(card) != card_binding_hash({**card, "image_intent": intent(card, "helpful")})
    required = {**card, "image_intent": intent(card)}
    assert card_binding_hash(required) != card_binding_hash({**required, "image_intent": intent(card, "helpful")})
    assert post(client, card).json()["status"] == "auto_pass"
    approved = service.store.get("cards", card["id"])
    approved["image_intent"] = intent(card, "none")
    assert valid_machine_approval(service.store, approved)
    approved["image_intent"] = intent(card, "helpful")
    assert not valid_machine_approval(service.store, approved)
    assert calls == [True]


def test_missing_required_image_blocks_review_before_any_model_request(ready):
    client, card, service, calls = ready
    card["image_intent"] = intent(card)
    service.store.put("cards", card)
    capture = service._capture(card["id"])
    assert any("题目侧" in reason for reason in capture["source_reasons"])
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review"
    assert calls == []
    assert service.store.get("cards", card["id"])["status"] == "draft"


@pytest.mark.parametrize("mutation", ["answer_side", "outside_page", "old_revision", "no_source",
                                     "missing_media", "empty_intent", "outside_intent"])
def test_required_picture_must_match_front_side_and_current_intent_source(required_picture, mutation):
    client, card, media, service, image_calls, text_calls = required_picture
    if mutation == "answer_side":
        card["images"][0]["side"] = "answer"
    elif mutation == "outside_page":
        media["source"]["page"] = 1
    elif mutation == "old_revision":
        media["source"]["revision"] = 99
    elif mutation == "no_source":
        media.pop("source")
    elif mutation == "missing_media":
        card["images"][0]["media_id"] = "img-" + "f" * 32
    elif mutation == "empty_intent":
        card["image_intent"]["block_ids"] = []
    else:
        card["image_intent"]["block_ids"] = [card["document_id"] + "-p1-b1-r1"]
    service.store.put("media", media)
    service.store.put("cards", card)
    with pytest.raises(HTTPException) as blocked:
        require_required_image(service.store, card)
    assert blocked.value.status_code == 409
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review"
    assert image_calls == [] and text_calls == []


@pytest.mark.parametrize("method", ["human", "legacy"])
def test_required_image_gate_also_applies_to_human_and_legacy_approved_cards(ready, method):
    client, card, service, calls = ready
    card["image_intent"] = intent(card)
    card["status"] = "approved"
    if method == "human":
        card["approval"] = {"method": "human", "card_version": card["version"]}
    else:
        card.pop("approval", None)
    service.store.put("cards", card)
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 409, (endpoint, response.text)
    sync = PluginSync(service.store, service.storage)
    assert sync.catalog(BASE)["cards"] == []
    assert sync.catalog(BASE)["excluded"][0]["id"] == card["id"]
    response = client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出",
                                                "request_id": "required-missing-image"})
    assert response.status_code == 409, response.text
    assert calls == []


def test_valid_required_picture_runs_visual_then_text_review_and_exports_front_image(required_picture):
    client, card, media, service, image_calls, text_calls = required_picture
    page = service.store.get("source_pages", card["document_id"] + "-p2")
    page["status"] = "pending"
    service.store.put("source_pages", page)
    require_required_image(service.store, card)
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "auto_pass", result.json()
    assert len(image_calls) == 1 and text_calls == [True]
    approved = service.store.get("cards", card["id"])
    assert valid_machine_approval(service.store, approved)
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 200, (endpoint, response.text)
    package = client.get("/api/export/apkg")
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert any(media["id"] in name for name in json.loads(archive.read("media")).values())
    sync = PluginSync(service.store, service.storage)
    payload = sync.card(card["id"], BASE)
    assert payload["images"][0]["side"] == "question"
    assert "<img" in payload["fields"]["Question"] and media["id"] in payload["fields"]["Question"]
    response = client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出",
                                                "request_id": "required-valid-image"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    changed = {**approved, "images": [{**approved["images"][0], "side": "answer"}]}
    assert not valid_machine_approval(service.store, changed)


def test_attaching_required_image_does_not_bypass_independent_visual_review(required_picture, monkeypatch):
    client, card, _media, _service, _image_calls, text_calls = required_picture
    def unrelated(*_args):
        return {"result": {**GOOD, "relation": "unrelated", "reasons": ["图片与题目无关。"]}, "diagnostics": {}}
    monkeypatch.setattr("ankisense.image_auto_review.run_enrichment", unrelated)
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "needs_review"
    assert text_calls == []
    assert client.app.state.store.get("cards", card["id"])["status"] == "draft"
