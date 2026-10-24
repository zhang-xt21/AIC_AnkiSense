"""Full application generation -> prospective picture -> review -> APKG, offline."""
import copy
import io
import json
import time
import zipfile

import pytest

from ankisense.approval import valid_machine_approval
from ankisense.llm import Settings
from ankisense.schemas import CardDraft, PlannedDraftBatch
from test_card_auto_review import bind_imported_pdf, good_review
from test_figure_recommendations import context, save
from test_image_auto_review import GOOD


@pytest.fixture
def integrated(context, monkeypatch):
    client, original, adapter, storage = context
    bind_imported_pdf(client, original, storage)
    app = client.app
    app.state.store.put("cards", {**original, "status": "draft"})
    settings = Settings(mode="cloud", base_url="https://model.invalid", model="offline-fixture", api_key="test-only")
    app.state.settings = app.state.card_auto_review.settings = app.state.image_auto_review.settings = settings
    extract = adapter.extract_regions

    def layout(*args):
        result = extract(*args)
        for page in result["pages"]:
            page["regions"] = [region for region in page["regions"] if region["kind"] == "picture"]
            page["text_regions"] = []
        return result

    adapter.extract_regions = layout
    generated, reviewed, visual = [], [], []
    options = {"need": "required"}

    async def generate(blocks, count, **kwargs):
        generated.append(kwargs)
        card = {key: original[key] for key in CardDraft.model_fields}
        return PlannedDraftBatch(cards=[{**card, "image_intent": {
            "need": options["need"], "reason": "栈的操作图示", "block_ids": [card["evidence"][0]["block_id"]]}}])

    async def review(card, blocks):
        reviewed.append(card.model_dump())
        return good_review(card.model_dump())

    def vision(*args):
        visual.append(True)
        return {"result": copy.deepcopy(GOOD)}

    monkeypatch.setattr(app.state.ai, "generate_drafts", generate)
    monkeypatch.setattr(app.state.ai, "review_card", review)
    monkeypatch.setattr("ankisense.image_auto_review.capability", lambda _: {"available": True})
    monkeypatch.setattr("ankisense.image_auto_review.run_enrichment", vision)
    return client, original, adapter, generated, reviewed, visual, options


def run_job(fixture, *, enabled=True):
    client, original, *_ = fixture
    plan = client.post(f"/api/documents/{original['document_id']}/generation-plan", json={"count": 1}).json()
    response = client.post("/api/generation-jobs", json={"document_id": original["document_id"], "count": 1,
        "source_fingerprint": plan["source_fingerprint"], "request_id": "image-flow-fixture", "auto_images": enabled})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 10
    card = None
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        cards = [item for item in state["cards"] if item.get("generation_job_id") == response.json()["id"]]
        if cards:
            card = cards[0]
            if ((card.get("auto_illustration") or {}).get("state") in {"attached", "skipped"}
                    and card.get("review_state") in {"auto_pass", "needs_review"}):
                return card
        time.sleep(.02)
    pytest.fail(f"Local automatic image flow did not finish: {card}")


@pytest.mark.parametrize("need,side", [("helpful", "answer"), ("required", "question")])
def test_generated_card_gets_verified_picture_and_exports_with_media(integrated, need, side):
    client, original, adapter, generated, reviewed, visual, options = integrated
    options["need"] = need
    card = run_job(integrated)
    assert card["status"] == "approved" and card["version"] == 2
    assert valid_machine_approval(client.app.state.store, card)
    assert card["images"][0]["side"] == side
    assert len(generated) == len(visual) == 1 and adapter.calls == [[2]]
    final_review = next(item for item in reviewed if item.get("verified_images"))
    assert final_review["verified_images"] == [{"side": side, "page": 2, "description": GOOD["description"]}]
    if need == "required":
        assert len(reviewed) == 1, "Required pictures cannot be text-approved before attachment"
    media_id = card["images"][0]["media_id"]
    checked = client.get(f"/api/cards/{card['id']}/images/{media_id}/auto-check").json()
    assert checked["cached"] and checked["status"] == "auto_pass" and len(visual) == 1
    package = client.get("/api/export/apkg")
    assert package.status_code == 200, package.text
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        manifest = json.loads(archive.read("media"))
        image_entry = next(key for key, name in manifest.items() if media_id in name)
        assert archive.read(image_entry).startswith(b"\x89PNG")
    assert card["id"] in client.get("/api/export").text


@pytest.mark.parametrize("need,status", [("helpful", "approved"), ("required", "draft")])
def test_disabled_matching_keeps_text_cards_and_blocks_missing_required_images(integrated, need, status):
    client, _original, adapter, _generated, reviewed, visual, options = integrated
    options["need"] = need
    card = run_job(integrated, enabled=False)
    assert card["status"] == status and card["version"] == 1
    assert card["auto_illustration"]["state"] == "skipped" and not card.get("images")
    assert not adapter.calls and not visual
    if need == "required":
        assert not reviewed
        assert save(client, card, status="approved").status_code == 409
        assert card["id"] not in client.get("/api/export").text
