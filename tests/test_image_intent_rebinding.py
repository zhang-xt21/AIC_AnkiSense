"""Existing card editors can repair picture requirements after changing sources."""

import pytest

from ankisense.approval import require_required_image
from ankisense.sources import save_page_revision
from test_figure_recommendations import context, save
from test_image_auto_review import saved


def set_intent(client, card, need="required"):
    card["image_intent"] = {"need": need, "reason": "查看引用页中的栈示意图。",
                            "block_ids": [card["evidence"][0]["block_id"]]}
    client.app.state.store.put("cards", card)
    return card


def from_page(client, card, page, revision=1):
    response = client.post("/api/card-media/from-page", json={"document_id": card["document_id"],
                                                             "page": page, "revision": revision})
    assert response.status_code == 200, response.text
    return {"media_id": response.json()["id"], "side": "question", "caption": "当前讲义原图"}


@pytest.mark.parametrize("need", ["required", "helpful"])
def test_revised_source_rebinds_intent_and_detaches_stale_attachment(saved, need):
    client, card, media, _job, image_calls, service, _storage = saved
    set_intent(client, card, need)
    page = save_page_revision(service.store, card["document_id"], 2, 1,
                              {"text": "栈遵循后进先出，插入和删除在栈顶进行。这里补充了校对说明。",
                               "status": "reviewed", "reviewed_at": "now"}, "proofread")
    stale = service.store.get("cards", card["id"])
    response = save(client, stale, status="approved", expected_version=stale["version"],
                    evidence=[{"block_id": page["block_ids"][0], "quote": "栈遵循后进先出"}])
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["image_intent"]["need"] == need
    assert updated["image_intent"]["block_ids"] == page["block_ids"]
    assert updated["version"] == stale["version"] + 1
    assert not updated["images"] and updated["source_outdated"] is False
    assert updated["status"] == "draft" and not updated.get("approval")
    assert updated["auto_illustration"]["state"] == "interrupted"
    assert client.get(media["url"]).status_code == 200, "Detaching must not delete the original file"
    assert image_calls == []
    if need == "required":
        assert save(client, updated, status="approved").status_code == 409
        attachment = from_page(client, updated, 2, page["revision"])
        repaired = save(client, updated, status="approved", images=[attachment])
    else:
        repaired = save(client, updated, status="approved")
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["status"] == "approved"
    require_required_image(service.store, repaired.json())


def test_moving_evidence_to_other_page_rebinds_and_keeps_newly_selected_image(saved):
    client, card, media, _job, image_calls, service, _storage = saved
    set_intent(client, card)
    new_image = from_page(client, card, 1)
    response = save(client, card, status="approved",
                    evidence=[{"block_id": card["document_id"] + "-p1-b1-r1", "quote": "栈遵循后进先出"}],
                    images=[*card["images"], new_image])
    assert response.status_code == 200, response.text
    rebound = response.json()
    assert rebound["image_intent"]["need"] == "required"
    assert rebound["image_intent"]["block_ids"] == [card["document_id"] + "-p1-b1-r1"]
    assert rebound["images"] == [new_image]
    assert rebound["status"] == "draft" and not rebound.get("approval")
    assert client.get(media["url"]).status_code == 200 and not image_calls
    approved = save(client, rebound, status="approved")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert card["id"] in client.get("/api/export").text


def test_same_page_rebinding_does_not_expand_intent_to_other_evidence_pages(context):
    client, card, _adapter, _storage = context
    set_intent(client, card)
    store = client.app.state.store
    page = save_page_revision(store, card["document_id"], 2, 1,
                              {"text": "栈遵循后进先出，插入和删除在栈顶进行。补充内容。",
                               "status": "reviewed", "reviewed_at": "now"}, "proofread")
    stale = store.get("cards", card["id"])
    response = save(client, stale, status="draft", evidence=[
        {"block_id": card["document_id"] + "-p1-b1-r1", "quote": "栈遵循后进先出"},
        {"block_id": page["block_ids"][0], "quote": "栈遵循后进先出"},
    ])
    assert response.status_code == 200, response.text
    assert response.json()["image_intent"]["block_ids"] == page["block_ids"]


def test_editing_only_question_preserves_existing_image_intent_and_attachment(saved):
    client, card, _media, _job, _calls, _service, _storage = saved
    set_intent(client, card)
    response = save(client, card, status="draft", question="请根据图示说明栈的顺序？")
    assert response.status_code == 200, response.text
    assert response.json()["image_intent"] == card["image_intent"]
    assert response.json()["images"] == card["images"]


def test_legacy_card_without_image_intent_keeps_ordinary_editor_behavior(context):
    client, card, _adapter, _storage = context
    response = save(client, card, status="approved",
                    evidence=[{"block_id": card["document_id"] + "-p1-b1-r1", "quote": "栈遵循后进先出"}])
    assert response.status_code == 200, response.text
    assert "image_intent" not in response.json()
    assert response.json()["status"] == "approved"
