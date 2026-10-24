"""Offline OCR routing and source-review integrity checks.

The OCR engine is replaced with deterministic page results here. Real printed
Chinese recognition is assessed separately against rendered lecture pages.
"""

import asyncio
import importlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ankisense.llm import Settings
from ankisense.schemas import CardUpdate, Diagnosis, DraftBatch


NATIVE_TEXT = "Stacks use LIFO. The last element pushed is the first one popped."
OCR_TEXT = "栈遵循后进先出，最后入栈的元素最先出栈。入栈和出栈均在栈顶进行。"
CORRECTED_TEXT = "栈遵循后进先出，最后入栈的元素最先出栈。入栈和出栈均在同一端的栈顶进行。"


def pdf_bytes(page_texts):
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text is None:
            continue
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                })
            })
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def recognition(page, text=OCR_TEXT, status="pending"):
    return {"page": page, "text": text, "raw_text": text, "method": "ocr",
            "engine": "offline-ocr-double", "engine_version": "test-1", "status": status}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    module = importlib.import_module("ankisense.app")
    application = module.create_app(data_dir=tmp_path / "ocr-app", settings=Settings(mode="demo"))
    with TestClient(application) as instance:
        yield instance


def state(client):
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    return response.json()


def page_for(client, document, page=1):
    return next(item for item in state(client)["source_pages"]
                if item["document_id"] == document["id"] and item["page"] == page)


def import_scanned(client, monkeypatch):
    from ankisense import ingest

    monkeypatch.setattr(ingest, "recognize_page", lambda content, page: recognition(page))
    response = client.post("/api/documents", files={
        "file": ("scanned.pdf", pdf_bytes([None]), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def save_page(client, document, text, revision=None, page=1):
    revision = revision if revision is not None else page_for(client, document, page)["revision"]
    return client.put(f"/api/documents/{document['id']}/pages/{page}",
                      json={"revision": revision, "text": text})


def stub_generate(client, monkeypatch):
    calls = []

    async def generate(blocks, count):
        calls.append(blocks)
        return DraftBatch.model_validate({"cards": [{
            "question": "栈遵循什么顺序？", "answer": "后进先出", "concept": "栈",
            "kind": "basic", "rubric": [{"point_id": "lifo", "text": "说明后进先出"}],
            "evidence": [{"block_id": blocks[0].block_id, "quote": "后进先出"}],
        }]})

    monkeypatch.setattr(client.app.state.ai, "generate_drafts", generate)
    return calls


def generate_card(client, document):
    response = client.post("/api/card-drafts", json={
        "block_ids": page_for(client, document)["block_ids"], "count": 1})
    assert response.status_code == 200, response.text
    return response.json()["cards"][0]


def update_card(client, card, **changes):
    body = {name: card[name] for name in CardUpdate.model_fields if name in card}
    body.update(changes)
    return client.put(f"/api/cards/{card['id']}", json=body)


def test_mixed_pdf_ocr_routes_each_selected_physical_page(monkeypatch):
    from ankisense import ingest

    seen = []

    def recognize(content, page):
        seen.append(page)
        return recognition(page)

    monkeypatch.setattr(ingest, "recognize_page", recognize)
    result = ingest.parse_pdf_document(pdf_bytes([NATIVE_TEXT, None, NATIVE_TEXT]), 2, 3)
    assert seen == [2], "A mixed PDF must OCR its scanned page even when another page has text"
    assert [page["page"] for page in result["pages"]] == [2, 3]
    assert result["pages"][0]["text"] == OCR_TEXT
    assert result["pages"][0]["method"] == "ocr"
    assert result["pages"][0]["status"] == "pending"
    assert NATIVE_TEXT in result["pages"][1]["text"]
    assert result["pages"][1]["status"] == "pending"
    assert result["total"] == 3


def test_ocr_failure_does_not_silently_drop_page_from_mixed_pdf(monkeypatch):
    from ankisense import ingest

    def fail(content, page):
        raise RuntimeError("offline OCR unavailable")

    monkeypatch.setattr(ingest, "recognize_page", fail)
    result = ingest.parse_pdf_document(pdf_bytes([NATIVE_TEXT, None]), 1, 2)
    assert len(result["pages"]) == 2
    assert result["pages"][0]["status"] == "pending"
    failed = result["pages"][1]
    assert failed["page"] == 2
    assert failed["status"] == "failed"
    assert not failed["text"]
    assert result["warning"]


def test_explicit_modes_override_automatic_page_routing(monkeypatch):
    from ankisense import ingest

    seen = []

    def recognize(content, page):
        seen.append(page)
        return recognition(page)

    monkeypatch.setattr(ingest, "recognize_page", recognize)
    content = pdf_bytes([NATIVE_TEXT, None])
    text_only = ingest.parse_pdf_document(content, 1, 2, "text")
    assert seen == []
    assert text_only["pages"][1]["status"] == "empty"
    forced = ingest.parse_pdf_document(content, 1, 2, "ocr")
    assert seen == [1, 2]
    assert all(page["method"] == "ocr" for page in forced["pages"])
    assert forced["id"] != text_only["id"]
    with pytest.raises(ValueError):
        ingest.parse_pdf_document(content, 1, 2, "unsupported")


def test_pending_source_drafts_need_card_approval_but_not_page_confirmation(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    page = page_for(client, document)
    assert page["status"] == "pending"
    assert page["raw_text"] == OCR_TEXT
    assert page["revision"] == 1
    calls = stub_generate(client, monkeypatch)
    response = client.post("/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1})
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    candidate = response.json()["cards"][0]
    assert candidate["status"] == "draft"
    assert page_for(client, document) == page, "Drafting must not pretend to confirm the source"
    assert client.post("/api/attempts", json={"card_id": candidate["id"], "answer": "后进先出",
                                              "request_id": "pending-source-001"}).status_code == 409
    assert candidate["id"] not in client.get("/api/export").text
    approved = update_card(client, candidate, status="approved")
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["method"] == "human"
    assert candidate["id"] in client.get("/api/export").text
    assert page_for(client, document) == page


@pytest.mark.parametrize("status", ["failed", "empty"])
def test_failed_or_empty_page_cannot_generate_even_with_old_residual_text(client, monkeypatch, status):
    document = import_scanned(client, monkeypatch)
    page = page_for(client, document)
    client.app.state.store.put("source_pages", {**page, "status": status})
    calls = stub_generate(client, monkeypatch)
    response = client.post("/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1})
    assert response.status_code == 409, response.text
    assert calls == []
    assert state(client)["cards"] == []


@pytest.mark.parametrize("edge_whitespace", ["", " \n"])
def test_unchanged_confirmation_keeps_draft_citations_images_and_source_revision(client, monkeypatch, edge_whitespace):
    from ankisense import ingest

    original_text = edge_whitespace + OCR_TEXT + edge_whitespace
    monkeypatch.setattr(ingest, "recognize_page", lambda content, page: recognition(page, original_text))
    response = client.post("/api/documents", files={
        "file": ("confirm.pdf", pdf_bytes([None]), "application/pdf")})
    assert response.status_code == 200, response.text
    document = response.json()
    original = page_for(client, document)
    store = client.app.state.store
    blocks_before = {key: store.get("blocks", key) for key in original["block_ids"]}
    stub_generate(client, monkeypatch)
    draft = generate_card(client, document)
    media_response = client.post("/api/card-media/from-page", json={
        "document_id": document["id"], "page": 1, "revision": original["revision"]})
    assert media_response.status_code == 200, media_response.text
    media = media_response.json()
    attached = update_card(client, draft, images=[{"media_id": media["id"], "side": "answer"}])
    assert attached.status_code == 200, attached.text
    draft = attached.json()

    confirmed_response = save_page(client, document, original_text)
    assert confirmed_response.status_code == 200, confirmed_response.text
    confirmed = confirmed_response.json()
    assert confirmed["revision"] == original["revision"]
    assert confirmed["block_ids"] == original["block_ids"]
    assert confirmed["text"] == original_text
    assert confirmed["raw_text"] == original_text
    assert confirmed["status"] == "reviewed"
    assert confirmed["reviewed_at"]
    assert confirmed["action"] == "proofread"
    assert store.get("cards", draft["id"]) == draft
    assert store.get("media", media["id"]) == media
    assert {key: store.get("blocks", key) for key in original["block_ids"]} == blocks_before
    history = client.get(f"/api/documents/{document['id']}/pages/1/revisions").json()
    # Confirmation is a separate event, not a rewrite of the extraction snapshot.
    assert len(history["revisions"]) == 1
    snapshot = history["revisions"][0]
    assert snapshot["text"] == original_text
    assert snapshot["revision"] == original["revision"]
    assert snapshot["block_ids"] == original["block_ids"]
    assert snapshot["status"] == original["status"]
    assert history["confirmations"][0]["revision"] == original["revision"]
    assert history["confirmations"][0]["method"] == "human"

    approved_response = update_card(client, draft, status="approved")
    assert approved_response.status_code == 200, approved_response.text
    approved = approved_response.json()
    before = state(client)
    assert save_page(client, document, original_text.strip()).json() == confirmed
    assert state(client) == before
    assert store.get("cards", approved["id"]) == approved


def test_proofreading_preserves_recognition_and_creates_immutable_blocks(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    original_page = page_for(client, document)
    original_blocks = {item["block_id"]: item for item in state(client)["blocks"]}
    response = save_page(client, document, CORRECTED_TEXT)
    assert response.status_code == 200, response.text
    current = page_for(client, document)
    assert current["status"] == "reviewed"
    assert current["text"] == CORRECTED_TEXT
    assert current["raw_text"] == OCR_TEXT
    assert current["engine"] == original_page["engine"]
    assert current["engine_version"] == original_page["engine_version"]
    assert current["revision"] > original_page["revision"]
    assert set(current["block_ids"]).isdisjoint(original_page["block_ids"])
    blocks = {item["block_id"]: item for item in state(client)["blocks"]}
    assert all(blocks[key] == value for key, value in original_blocks.items())
    assert "".join(blocks[key]["text"] for key in current["block_ids"]) == CORRECTED_TEXT
    calls = stub_generate(client, monkeypatch)
    card = generate_card(client, document)
    assert len(calls) == 1
    assert [block.text for block in calls[0]] == [CORRECTED_TEXT]
    assert card["status"] == "draft"
    assert {item["block_id"] for item in card["evidence"]}.issubset(current["block_ids"])


def test_simultaneous_proofreading_saves_cannot_overwrite_each_other(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    revision = page_for(client, document)["revision"]
    barrier = Barrier(2)

    def save(text):
        barrier.wait(timeout=10)
        return save_page(client, document, text, revision=revision)

    alternatives = [CORRECTED_TEXT, CORRECTED_TEXT + "空栈应先判空。"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, alternatives))
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = alternatives[next(index for index, result in enumerate(results) if result.status_code == 200)]
    assert page_for(client, document)["text"] == winner
    previous = state(client)
    assert save_page(client, document, "过期窗口的校订", revision=revision).status_code == 409
    assert state(client) == previous


def test_unchanged_confirmation_rechecks_revision_inside_transaction(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, OCR_TEXT).status_code == 200
    revision = page_for(client, document)["revision"]
    store = client.app.state.store
    original_mutate = store.mutate
    entered, release = Event(), Event()

    def pause_first_write(build, check=None):
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=10)
        return original_mutate(build, check)

    monkeypatch.setattr(store, "mutate", pause_first_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        repeated = pool.submit(save_page, client, document, OCR_TEXT, revision)
        try:
            assert entered.wait(timeout=10)
            changed = save_page(client, document, CORRECTED_TEXT, revision)
            assert changed.status_code == 200, changed.text
        finally:
            release.set()
        response = repeated.result(timeout=10)
    assert response.status_code == 409, response.text
    assert page_for(client, document)["text"] == CORRECTED_TEXT


def test_clearing_a_page_does_not_leave_active_citable_text(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    old_page = page_for(client, document)
    response = save_page(client, document, " \n\t ")
    assert response.status_code == 200, response.text
    page = page_for(client, document)
    assert page["status"] == "empty"
    assert not page["text"]
    assert page["block_ids"] == []
    assert page["raw_text"] == OCR_TEXT
    stub_generate(client, monkeypatch)
    assert client.post("/api/card-drafts", json={
        "block_ids": old_page["block_ids"], "count": 1}).status_code == 409


def test_first_confirmation_of_empty_ocr_page_is_recorded_then_idempotent(client, monkeypatch):
    from ankisense import ingest

    monkeypatch.setattr(ingest, "recognize_page",
                        lambda content, page: recognition(page, "", "empty"))
    imported = client.post("/api/documents", files={
        "file": ("empty-scan.pdf", pdf_bytes([None]), "application/pdf")})
    assert imported.status_code == 200, imported.text
    document = imported.json()
    original = page_for(client, document)
    assert original["status"] == "empty"
    assert not original.get("reviewed_at")
    confirmed = save_page(client, document, "")
    assert confirmed.status_code == 200, confirmed.text
    page = page_for(client, document)
    assert page["revision"] == original["revision"]
    assert page["reviewed_at"]
    assert page["status"] == "empty"
    assert page["block_ids"] == []
    history_url = f"/api/documents/{document['id']}/pages/1/revisions"
    history_before = client.get(history_url).json()
    assert len(history_before["revisions"]) == 1
    assert not history_before["revisions"][0].get("reviewed_at")
    assert history_before["confirmations"][0]["revision"] == page["revision"]
    assert history_before["confirmations"][0]["method"] == "human"
    assert save_page(client, document, "").status_code == 200
    assert page_for(client, document) == page
    assert client.get(history_url).json() == history_before

    calls = stub_generate(client, monkeypatch)
    assert client.post("/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1}).status_code == 422
    assert calls == []
    assert state(client)["cards"] == []


@pytest.mark.parametrize("method", ["text", "ocr"])
def test_import_and_proofreading_share_single_page_length_limit(monkeypatch, method):
    from ankisense import ingest

    oversized = "a" * 50001
    monkeypatch.setattr(ingest, "recognize_page",
                        lambda content, page: recognition(page, oversized))
    content = pdf_bytes([oversized if method == "text" else None])
    with pytest.raises(ValueError, match="单页|5 万|50000"):
        ingest.parse_pdf_document(content, 1, 1, method)


def test_oversized_proofreading_edit_is_rejected_without_mutation(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    before = state(client)
    response = save_page(client, document, "栈" * 50001)
    assert response.status_code == 422, response.text
    assert state(client) == before


@pytest.mark.parametrize("initially_reviewed", [False, True])
def test_source_edit_during_generation_discards_stale_ai_result(client, monkeypatch, initially_reviewed):
    document = import_scanned(client, monkeypatch)
    if initially_reviewed:
        assert save_page(client, document, OCR_TEXT).status_code == 200
    page = page_for(client, document)
    entered, release = Event(), Event()

    async def delayed_generate(blocks, count):
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return DraftBatch.model_validate({"cards": [{
            "question": "栈遵循什么顺序？", "answer": "后进先出", "concept": "栈",
            "kind": "basic", "rubric": [{"point_id": "lifo", "text": "后进先出"}],
            "evidence": [{"block_id": blocks[0].block_id, "quote": "后进先出"}],
        }]})

    monkeypatch.setattr(client.app.state.ai, "generate_drafts", delayed_generate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1})
        try:
            assert entered.wait(timeout=10)
            saved = save_page(client, document, CORRECTED_TEXT)
            assert saved.status_code == 200, saved.text
        finally:
            release.set()
        response = future.result(timeout=10)
    assert response.status_code == 409, response.text
    assert state(client)["cards"] == []


def test_unchanged_confirmation_during_generation_keeps_result_current(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    page = page_for(client, document)
    stub_generate(client, monkeypatch)
    generate = client.app.state.ai.generate_drafts
    entered, release = Event(), Event()

    async def delayed_generate(blocks, count):
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await generate(blocks, count)

    monkeypatch.setattr(client.app.state.ai, "generate_drafts", delayed_generate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/api/card-drafts", json={"block_ids": page["block_ids"], "count": 1})
        try:
            assert entered.wait(timeout=10)
            confirmed = save_page(client, document, OCR_TEXT)
            assert confirmed.status_code == 200, confirmed.text
        finally:
            release.set()
        response = future.result(timeout=10)
    assert response.status_code == 200, response.text
    assert page_for(client, document)["revision"] == page["revision"]
    card = response.json()["cards"][0]
    assert card["status"] == "draft"
    assert update_card(client, card, status="approved").status_code == 200


def test_draft_update_cannot_erase_concurrent_source_outdated_marker(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, OCR_TEXT).status_code == 200
    stub_generate(client, monkeypatch)
    card = generate_card(client, document)
    entered, release = Event(), Event()
    validate = module.validate_card

    def pause_after_validation(candidate, blocks):
        validate(candidate, blocks)
        entered.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(module, "validate_card", pause_after_validation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(update_card, client, card, question="过期窗口想保存的题目", status="draft")
        try:
            assert entered.wait(timeout=10)
            saved = save_page(client, document, CORRECTED_TEXT)
            assert saved.status_code == 200, saved.text
        finally:
            release.set()
        response = future.result(timeout=10)
    assert response.status_code == 409, response.text
    current = client.app.state.store.get("cards", card["id"])
    assert current["source_outdated"] is True
    assert current["status"] == "draft"
    assert current["question"] == card["question"]
    assert current["version"] == card["version"] + 1


def test_changed_source_invalidates_card_but_preserves_historical_attempt(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, OCR_TEXT).status_code == 200
    previous_page = page_for(client, document)
    stub_generate(client, monkeypatch)
    card = generate_card(client, document)
    approved = update_card(client, card, status="approved")
    assert approved.status_code == 200, approved.text
    card = approved.json()

    async def diagnose(card, blocks, answer):
        return Diagnosis.model_validate({"verdict": "correct", "missing_point_ids": [],
            "contradicted_point_ids": [], "answer_spans": [answer],
            "source_block_ids": [blocks[0].block_id], "reason": "回答与讲义的后进先出规则一致。"})

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", diagnose)
    payload = {"card_id": card["id"], "answer": "后进先出", "request_id": "source-snapshot-001"}
    attempted = client.post("/api/attempts", json=payload)
    assert attempted.status_code == 200, attempted.text
    historical = attempted.json()
    assert historical["status"] == "completed"
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200

    current = client.app.state.store.get("cards", card["id"])
    assert current["status"] == "draft"
    assert current["source_outdated"] is True
    assert client.get(f"/api/attempts/{historical['id']}").json() == historical
    assert client.post("/api/attempts", json=payload).json() == historical
    assert client.post("/api/attempts", json={**payload, "request_id": "source-snapshot-002"}).status_code == 409
    assert update_card(client, current, status="approved").status_code == 409
    assert client.post("/api/card-drafts", json={
        "block_ids": previous_page["block_ids"], "count": 1}).status_code == 409
    assert card["id"] not in client.get("/api/export").text

    # Rebinding the citation to reviewed current text explicitly restores eligibility.
    block_id = page_for(client, document)["block_ids"][0]
    restored = update_card(client, current, status="approved",
                           evidence=[{"block_id": block_id, "quote": "后进先出"}])
    assert restored.status_code == 200, restored.text
    assert not restored.json().get("source_outdated", False)
    assert client.get(f"/api/attempts/{historical['id']}").json() == historical


def test_failed_rerecognition_is_visible_and_blocks_are_not_reused(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200
    original = page_for(client, document)
    old_blocks = {block_id: client.app.state.store.get("blocks", block_id)
                  for block_id in original["block_ids"]}
    calls = []

    def failed(content, page):
        calls.append(page)
        return {**recognition(page, "", "failed"), "error": "OCR 暂时不可用，请重试或手工校订。"}

    monkeypatch.setattr(module, "recognize_page", failed)
    response = client.post(f"/api/documents/{document['id']}/pages/1/ocr",
                           json={"revision": original["revision"]})
    assert response.status_code == 200, response.text
    page = page_for(client, document)
    assert calls == [1]
    assert page["status"] == "failed"
    assert page["revision"] > original["revision"]
    assert page["error"]
    assert page["block_ids"] == []
    assert all(client.app.state.store.get("blocks", key) == value for key, value in old_blocks.items())
    stub_generate(client, monkeypatch)
    assert client.post("/api/card-drafts", json={
        "block_ids": original["block_ids"], "count": 1}).status_code == 409


def test_successful_ocr_retry_clears_stale_error_and_model_metadata(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    endpoint = f"/api/documents/{document['id']}/pages/1/ocr"
    failure = {**recognition(1, "", "failed"), "error": "第一次识别失败，请重试。",
               "models": [{"name": "previous-model"}], "pipeline_version": "previous-pipeline"}
    monkeypatch.setattr(module, "recognize_page", lambda content, page: failure.copy())
    response = client.post(endpoint, json={"revision": page_for(client, document)["revision"]})
    assert response.status_code == 200, response.text
    failed = page_for(client, document)
    assert failed["status"] == "failed"
    assert failed["error"] == failure["error"]
    assert failed["models"] == failure["models"]

    # The next recognizer returns no error/model detail. Missing optional
    # metadata must not be filled from the preceding failed recognition run.
    success = {**recognition(1), "engine": "replacement-engine", "engine_version": "test-2"}
    monkeypatch.setattr(module, "recognize_page", lambda content, page: success.copy())
    response = client.post(endpoint, json={"revision": failed["revision"]})
    assert response.status_code == 200, response.text
    retried = page_for(client, document)
    assert retried["status"] == "pending"
    assert retried["error"] is None
    assert retried["models"] == []
    assert retried["pipeline_version"] is None
    assert retried["engine"] == "replacement-engine"
    assert retried["engine_version"] == "test-2"
    assert retried["raw_text"] == OCR_TEXT
    assert not retried.get("reviewed_at")
    calls = stub_generate(client, monkeypatch)
    assert client.post("/api/card-drafts", json={"block_ids": retried["block_ids"], "count": 1}).status_code == 200
    assert len(calls) == 1

    assert save_page(client, document, CORRECTED_TEXT).status_code == 200
    confirmed = page_for(client, document)
    assert confirmed["status"] == "reviewed"
    assert confirmed["reviewed_at"]
    assert confirmed["error"] is None
    assert confirmed["raw_text"] == OCR_TEXT
    generate_card(client, document)
    assert len(calls) == 2
    history = client.get(f"/api/documents/{document['id']}/pages/1/revisions").json()["revisions"]
    historical_failure = next(item for item in history if item["revision"] == failed["revision"])
    assert historical_failure["status"] == "failed"
    assert historical_failure["error"] == failure["error"]
    assert historical_failure["models"] == failure["models"]
    assert historical_failure["pipeline_version"] == "previous-pipeline"


def test_outdated_rerecognition_request_never_runs_engine(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    original = page_for(client, document)
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200

    def must_not_run(*args):
        pytest.fail("An outdated page must be rejected before expensive OCR")

    monkeypatch.setattr(module, "recognize_page", must_not_run)
    before = state(client)
    response = client.post(f"/api/documents/{document['id']}/pages/1/ocr",
                           json={"revision": original["revision"]})
    assert response.status_code == 409, response.text
    assert state(client) == before


def test_rerecognition_advances_revision_even_when_text_is_identical(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, OCR_TEXT).status_code == 200
    original = page_for(client, document)
    stub_generate(client, monkeypatch)
    card = generate_card(client, document)
    monkeypatch.setattr(module, "recognize_page", lambda content, page: recognition(page))
    response = client.post(f"/api/documents/{document['id']}/pages/1/ocr",
                           json={"revision": original["revision"]})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["revision"] == original["revision"] + 1
    assert current["text"] == original["text"]
    assert current["status"] == "pending"
    assert not current.get("reviewed_at")
    assert set(current["block_ids"]).isdisjoint(original["block_ids"])
    assert client.app.state.store.get("cards", card["id"])["source_outdated"] is True


def test_rerecognition_has_its_own_raw_result_and_retains_earlier_revisions(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    original = page_for(client, document)
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200
    corrected = page_for(client, document)
    new_raw = OCR_TEXT + "这是另一次识别的文本。"
    monkeypatch.setattr(module, "recognize_page", lambda content, page: recognition(page, new_raw))
    response = client.post(f"/api/documents/{document['id']}/pages/1/ocr",
                           json={"revision": corrected["revision"]})
    assert response.status_code == 200, response.text
    current = page_for(client, document)
    assert current["status"] == "pending"
    assert current["text"] == current["raw_text"] == new_raw
    history = client.get(f"/api/documents/{document['id']}/pages/1/revisions")
    assert history.status_code == 200, history.text
    revisions = {item["revision"]: item for item in history.json()["revisions"]}
    assert revisions[original["revision"]]["text"] == OCR_TEXT
    assert revisions[corrected["revision"]]["text"] == CORRECTED_TEXT
    assert revisions[corrected["revision"]]["raw_text"] == OCR_TEXT
    assert revisions[current["revision"]]["raw_text"] == new_raw
    stub_generate(client, monkeypatch)
    assert client.post("/api/card-drafts", json={"block_ids": current["block_ids"], "count": 1}).status_code == 200


def test_original_image_only_exposes_imported_pdf_pages(client):
    content = pdf_bytes([NATIVE_TEXT, NATIVE_TEXT])
    imported = client.post("/api/documents", files={"file": ("two-pages.pdf", content, "application/pdf")},
                           data={"start_page": "2", "end_page": "2"})
    assert imported.status_code == 200, imported.text
    document = imported.json()
    page = page_for(client, document, 2)
    image = client.get(page["image_url"])
    assert image.status_code == 200, image.text[:200] if image.status_code != 200 else ""
    assert image.headers["content-type"].startswith("image/png")
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert client.get(f"/api/documents/{document['id']}/pages/1/image").status_code == 404
    assert client.get(f"/api/documents/{document['id']}/pages/3/image").status_code == 404
    text_doc = client.post("/api/documents/text", json={"title": "文本", "text": OCR_TEXT}).json()
    assert client.get(f"/api/documents/{text_doc['id']}/pages/1/image").status_code == 404


def test_source_review_survives_restart_and_does_not_mutate_on_reimport(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200
    before = state(client)
    same = import_scanned(client, monkeypatch)
    assert same["id"] == document["id"]
    assert state(client) == before
    restarted = module.create_app(data_dir=client.app.state.store.path.parent, settings=Settings(mode="demo"))
    with TestClient(restarted) as next_client:
        current = state(next_client)
    assert current["source_pages"] == before["source_pages"]
    assert current["blocks"] == before["blocks"]


def test_reimport_restores_missing_pdf_without_overwriting_proofreading(client, monkeypatch):
    document = import_scanned(client, monkeypatch)
    assert save_page(client, document, CORRECTED_TEXT).status_code == 200
    before = state(client)
    original_download = client.get(document["file_url"])
    assert original_download.status_code == 200
    # This is the per-test tmp_path database's upload directory, never user data.
    upload = client.app.state.store.path.parent / "uploads" / f"{document['id']}.pdf"
    assert upload.is_file()
    upload.unlink()
    assert client.get(document["file_url"]).status_code == 404
    repeated = import_scanned(client, monkeypatch)
    assert repeated["id"] == document["id"]
    assert state(client) == before
    restored = client.get(document["file_url"])
    assert restored.status_code == 200, restored.text
    assert restored.content == original_download.content
    assert page_for(client, document)["status"] == "reviewed"
