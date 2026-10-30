"""Both saved approval policies must grant the same offline client access."""
import copy
import hashlib
import io
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from PIL import Image

from ankisense.approval import MACHINE_APPROVAL_POLICY, card_binding_hash, valid_machine_approval
from ankisense.automatic_policy import POLICY, block_hash, content_hash
from ankisense.llm import Settings
from ankisense.image_auto_review import POLICY_VERSION as IMAGE_POLICY_VERSION
from ankisense.figure_vision import PROMPT_VERSION as IMAGE_PROMPT_VERSION
from ankisense.media import normalize_image, save_media
from ankisense.plugin_api import PREFIX
from ankisense.prompts import CARD_REVIEW_PROMPT_VERSION
from ankisense.schemas import Diagnosis, SourceBlock
from ankisense.source_policy import SOURCE_POLICY_VERSION, text_hash
from ankisense.sources import page_records, save_page_revision


EXPORTS = ("/api/export", "/api/export/media", "/api/export/apkg")
TEXT = "A stack removes the last inserted item first, called LIFO."


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app

    app = create_app(data_dir=tmp_path / "data", settings=Settings(mode="demo"))

    async def diagnose(card, blocks, answer):
        return Diagnosis(verdict="correct", addressed_point_ids=[card.rubric[0].point_id],
                         missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[blocks[0].block_id], reason="Offline fixture supports this answer.")

    monkeypatch.setattr(app.state.ai, "diagnose_answer", diagnose)
    app.state.plugin.connection.configure()
    token = app.state.plugin.connection.read()["token"]
    headers = {"Authorization": "Bearer " + token, "X-AnkiSense-Client": "integrated-profile-001"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, app.state.store, headers


def install(context, method, source_status="pending", *, picture=False, required=False):
    client, store, _headers = context
    document = {"id": "pdf-integrated", "filename": "lecture.pdf", "title": "Lecture", "is_demo": False,
                "page_count": 1, "file_url": "/api/documents/pdf-integrated/file"}
    store.put("documents", document)
    records = page_records(document["id"], {"page": 1, "text": TEXT, "status": source_status,
                                           "reviewed_at": "fixture" if source_status == "reviewed" else None})
    page = records[0][1]
    if source_status == "auto_verified":
        page["verification"] = {"policy_version": SOURCE_POLICY_VERSION,
                                "method": "two_visual_reads_and_local_changes", "revision": 1,
                                "text_sha256": text_hash(TEXT), "page_image_sha256": "a" * 64,
                                "visual_reads_agree": True, "protected_content_unchanged": True,
                                "local_changes_supported": True}
        records[1] = ("source_revisions", {**page, "id": page["id"] + "-r1"})
    store.put_many(records)
    block = store.get("blocks", page["block_ids"][0])
    blocks = [SourceBlock.model_validate(block)]
    card = {"id": "integrated-card", "document_id": document["id"], "question": "Which item leaves a stack first?",
            "answer": "The last inserted item leaves first.", "concept": "Stack", "kind": "basic",
            "rubric": [{"point_id": "lifo", "text": "The last inserted item leaves first."}],
            "evidence": [{"block_id": block["block_id"], "quote": "last inserted item first"}],
            "status": "approved", "version": 1, "images": [], "is_demo": False}
    reference = Diagnosis(verdict="correct", addressed_point_ids=["lifo"], missing_point_ids=[],
                          contradicted_point_ids=[], answer_spans=[card["answer"]],
                          source_block_ids=[block["block_id"]], reason="Offline reference-answer receipt.").model_dump()
    media = None
    if picture:
        png = io.BytesIO()
        Image.new("RGB", (40, 30), "green").save(png, "PNG")
        media = save_media(store, store.path.parent, normalize_image(png.getvalue()),
                           source={"origin": "automatic_source_page" if method == "automatic" else "figure_recommendation",
                                   "document_id": document["id"], "page": 1, "revision": 1})
        card["images"] = [{"media_id": media["id"], "side": "question" if required else "answer", "caption": "Lecture"}]
    if required:
        card["image_intent"] = {"need": "required", "reason": "Read the original diagram.",
                                "block_ids": [block["block_id"]]}
    if method == "human":
        card["approval"] = {"method": "human", "card_version": 1, "approved_at": "fixture"}
    elif method == "automatic":
        card["approval_method"] = "automatic"
        card["auto_quality"] = {"verdict": "clear", "issues": [], "scope": "text_and_original_page", "card_version": 1,
                                "reference_answer_check": reference,
                                "minimal_answer_check": {"answer": card["answer"], "diagnosis": reference}}
        card["automatic_evidence"] = {"policy": POLICY, "visual_check": True, "card_hash": content_hash(card),
                                      "block_hash": block_hash(blocks), "page_images": {"1": "a" * 64}}
    elif method == "machine":
        review_id = "cardreview-fixture"
        card["approval"] = {"method": "machine", "card_version": 1, "review_id": review_id}
        bindings = []
        if media:
            image_id = "imgreview-fixture"
            store.put("image_auto_reviews", {"id": image_id, "review_id": image_id, "card_id": card["id"],
                                             "card_version": 1, "policy_version": IMAGE_POLICY_VERSION,
                                             "prompt_version": IMAGE_PROMPT_VERSION,
                                             "media_id": media["id"], "fingerprint": "image-fingerprint",
                                             "status": "auto_pass", "retryable": False, "source_revision": 1,
                                             "bbox": None, "side": card["images"][0]["side"]})
            bindings = [{"attachment": card["images"][0], "media_id": media["id"], "current_pass": True,
                         "review_id": image_id, "image_fingerprint": "image-fingerprint", "media_source": media["source"]}]
        store.put("card_auto_reviews", {"id": review_id, "review_id": review_id, "card_id": card["id"], "card_version": 1,
                                       "policy_version": MACHINE_APPROVAL_POLICY, "prompt_version": CARD_REVIEW_PROMPT_VERSION,
                                       "status": "auto_pass", "retryable": False, "card_binding_hash": card_binding_hash(card),
                                       "reference_answer_check": reference,
                                       "minimal_answer_check": {"answer": card["answer"], "diagnosis": reference},
                                       "source_blocks": [block], "source_pages": {"1": {key: page[key] for key in ("revision", "block_ids", "text")}},
                                       "image_bindings": bindings})
    store.put("cards", card)
    return card, page, media


def assert_access(context, card, allowed):
    client, store, headers = context
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == (200 if allowed else 409), (endpoint, response.text)
    catalog = client.get(PREFIX + "/cards", headers=headers).json()
    assert [item["id"] for item in catalog["cards"]] == ([card["id"]] if allowed else [])
    assert bool(catalog["excluded"]) is not allowed
    response = client.post("/api/attempts", json={"card_id": card["id"], "answer": "The last inserted item.",
                                                "request_id": uuid4().hex})
    assert response.status_code == (200 if allowed else 409), response.text
    if allowed:
        assert response.json()["status"] == "completed"
    else:
        assert store.all("attempts") == []


@pytest.mark.parametrize("method", ["automatic", "machine"])
def test_missing_reference_answer_receipt_blocks_every_learning_consumer(integrated, method):
    _client, store, _headers = integrated
    card, _page, _media = install(integrated, method, "reviewed")
    if method == "automatic":
        card["auto_quality"].pop("reference_answer_check")
        store.put("cards", card)
    else:
        record = store.get("card_auto_reviews", card["approval"]["review_id"])
        record.pop("reference_answer_check")
        store.put("card_auto_reviews", record)
    assert_access(integrated, card, False)


@pytest.mark.parametrize("method", ["legacy", "human", "automatic", "machine"])
@pytest.mark.parametrize("source_status", ["pending", "reviewed", "auto_verified"])
def test_original_and_new_approval_source_matrix(integrated, method, source_status):
    card, _page, _media = install(integrated, method, source_status)
    assert_access(integrated, card, method != "legacy" or source_status != "pending")
    if method == "automatic":
        client, store, _headers = integrated
        projected = client.get(f"/api/cards/{card['id']}/auto-review").json()
        assert projected["status"] == "auto_pass" and projected["current"] is True
        assert projected["approval_method"] == "automatic" and projected["review_id"] is None
        assert projected["approval"] is None and store.all("card_auto_reviews") == []


@pytest.mark.parametrize("method", ["automatic", "machine", "human"])
@pytest.mark.parametrize("source_status", ["pending", "auto_verified"])
def test_current_required_picture_is_accepted_by_every_client(integrated, method, source_status):
    card, _page, _media = install(integrated, method, source_status, picture=True, required=True)
    assert_access(integrated, card, True)


@pytest.mark.parametrize("method", ["automatic", "machine"])
@pytest.mark.parametrize("damage", ["content", "version", "source", "image_revision", "proof", "missing_front_image"])
def test_stale_automatic_proofs_never_fall_back_to_legacy(integrated, method, damage):
    _client, store, _headers = integrated
    card, page, media = install(integrated, method, "reviewed", picture=True, required=True)
    if damage == "content":
        card["answer"] += " A changed answer."
    elif damage == "version":
        card["version"] += 1
    elif damage == "source":
        block = store.get("blocks", card["evidence"][0]["block_id"])
        store.put("blocks", {**block, "text": block["text"] + " Changed text."})
    elif damage == "image_revision":
        store.put("media", {**media, "source": {**media["source"], "revision": 0}})
    elif damage == "missing_front_image":
        card["images"][0]["side"] = "answer"
    elif method == "automatic":
        card["automatic_evidence"]["card_hash"] = "0" * 64
    else:
        record = store.get("card_auto_reviews", card["approval"]["review_id"])
        store.put("card_auto_reviews", {**record, "policy_version": "obsolete"})
    store.put("cards", card)
    assert_access(integrated, card, False)


@pytest.mark.parametrize("method", ["automatic", "machine"])
@pytest.mark.parametrize("source_status", ["pending", "auto_verified"])
def test_confirming_unchanged_source_preserves_both_proof_formats(integrated, method, source_status):
    client, store, _headers = integrated
    card, page, _media = install(integrated, method, source_status, picture=True)
    snapshot = copy.deepcopy(store.get("source_revisions", page["id"] + "-r1"))
    proof = copy.deepcopy(card.get("approval") or card["automatic_evidence"])
    response = client.put(f"/api/documents/{card['document_id']}/pages/1", json={"revision": 1, "text": TEXT})
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1 and response.json()["block_ids"] == page["block_ids"]
    assert store.get("source_revisions", page["id"] + "-r1") == snapshot
    assert len(store.all("source_confirmations")) == 1
    current = store.get("cards", card["id"])
    assert (current.get("approval") or current["automatic_evidence"]) == proof
    assert_access(integrated, current, True)


@pytest.mark.parametrize("method", ["automatic", "machine"])
def test_revising_source_clears_both_old_approval_fields(integrated, method):
    _client, store, _headers = integrated
    card, page, _media = install(integrated, method)
    # Extra historical metadata must never revive an approval after an edit.
    card["approval_method"] = "automatic"
    card.setdefault("automatic_evidence", {"policy": "historical"})
    store.put("cards", card)
    result = save_page_revision(store, card["document_id"], 1, 1,
                                {"text": TEXT + " A corrected paragraph.", "status": "reviewed"}, "proofread")
    assert result["revision"] == 2
    changed = store.get("cards", card["id"])
    assert changed["status"] == "draft" and changed["source_outdated"] is True
    assert not {"approval", "approval_method", "automatic_evidence"} & changed.keys()
    assert store.get("source_revisions", page["id"] + "-r1")["text"] == TEXT


@pytest.mark.parametrize("automatic_import", [False, True])
def test_proofreading_large_automatic_import_uses_its_original_text_limit(integrated, automatic_import):
    _client, store, _headers = integrated
    card, _page, _media = install(integrated, "human")
    document = store.get("documents", card["document_id"])
    store.put("documents", {**document, "automatic_import": automatic_import})
    for number in range(2, 5):
        store.put("source_pages", {"id": f"{document['id']}-p{number}", "document_id": document["id"],
                                   "page": number, "revision": 1, "text": "x" * 40000, "status": "pending", "block_ids": []})
    if not automatic_import:
        with pytest.raises(HTTPException) as error:
            save_page_revision(store, document["id"], 1, 1, {"text": TEXT + " corrected", "status": "reviewed"}, "proofread")
        assert error.value.status_code == 422
    else:
        changed = save_page_revision(store, document["id"], 1, 1,
                                     {"text": TEXT + " corrected", "status": "reviewed"}, "proofread")
        assert changed["revision"] == 2


def test_page_review_and_matching_accept_pdf_above_previous_10mib_limit(integrated):
    client, store, _headers = integrated
    card, _page, _media = install(integrated, "human")
    payload = b"%PDF-1.4\n" + b" " * (11 * 1024 * 1024)
    directory = store.path.parent / "uploads"
    directory.mkdir()
    path = directory / f"{card['document_id']}.pdf"
    path.write_bytes(payload)
    document = store.get("documents", card["document_id"])
    store.put("documents", {**document, "pdf_sha256": hashlib.sha256(payload).hexdigest()})
    captured = client.app.state.card_auto_review._capture(card["id"])
    assert captured["source_reasons"] == []
    proofread = client.app.state.source_proofread._capture(card["document_id"], 1, 1)
    assert proofread["pdf"] == payload
    first = client.app.state.automatic_illustrations._snapshot(card)
    path.write_bytes(payload[:-1] + b"x")
    assert client.app.state.automatic_illustrations._snapshot(card) != first


def test_original_page_picture_can_receive_independent_current_image_review(integrated):
    from ankisense.ocr import render_page_png
    from test_pdf_to_anki_flow import lecture_pdf

    client, store, _headers = integrated
    card, _page, media = install(integrated, "automatic", "auto_verified", picture=True)
    pdf = lecture_pdf()
    directory = store.path.parent / "uploads"
    directory.mkdir()
    (directory / f"{card['document_id']}.pdf").write_bytes(pdf)
    document = store.get("documents", card["document_id"])
    store.put("documents", {**document, "pdf_sha256": hashlib.sha256(pdf).hexdigest()})
    png = render_page_png(pdf, 1)
    media_file = store.path.parent / "media" / f"{media['id']}.png"
    media_file.write_bytes(normalize_image(png)[0])
    captured = client.app.state.image_auto_review._capture(card["id"], media["id"])
    assert captured["reasons"] == []
    assert captured["bbox"] is None and captured["page_image"] == png
    other = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(other, "PNG")
    media_file.write_bytes(normalize_image(other.getvalue())[0])
    assert client.app.state.image_auto_review._capture(card["id"], media["id"])["reasons"]


@pytest.mark.parametrize("method", ["legacy", "human", "automatic", "machine"])
def test_invalid_machine_source_proof_is_rejected_by_every_client(integrated, method):
    _client, store, _headers = integrated
    card, page, _media = install(integrated, method, "auto_verified")
    page["verification"]["text_sha256"] = "0" * 64
    store.put("source_pages", page)
    assert_access(integrated, card, False)


@pytest.mark.parametrize("field,value", [("card_version", 0), ("policy_version", "obsolete"), ("prompt_version", "obsolete")])
def test_independent_image_receipt_keeps_its_own_version_and_policy(integrated, field, value):
    _client, store, _headers = integrated
    card, _page, _media = install(integrated, "machine", "reviewed", picture=True)
    assert valid_machine_approval(store, card)
    record = store.get("image_auto_reviews", "imgreview-fixture")
    store.put("image_auto_reviews", {**record, field: value})
    assert_access(integrated, card, False)
