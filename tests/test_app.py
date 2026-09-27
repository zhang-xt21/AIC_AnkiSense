"""API-level checks for the human review and durable answer workflow."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ankisense.llm import AIError, Settings
from ankisense.schemas import CardUpdate, DraftBatch


@pytest.fixture
def client(tmp_path, monkeypatch):
    # app.py exposes a global ASGI app; isolate its first import as well as
    # each factory-created app so tests never touch the user's real data.
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app

    app = create_app(data_dir=tmp_path / "test-app", settings=Settings(mode="demo"))
    with TestClient(app) as test_client:
        yield test_client


def state(client):
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    return response.json()


def seed(client):
    response = client.post("/api/demo")
    assert response.status_code == 200, response.text
    return state(client)["cards"]


def change_card(client, card, **changes):
    body = {name: card[name] for name in CardUpdate.model_fields if name in card}
    body.update(changes)
    return client.put(f"/api/cards/{card['id']}", json=body)


def approve(client, card):
    response = change_card(client, card, status="approved")
    assert response.status_code == 200, response.text
    return next(item for item in state(client)["cards"] if item["id"] == card["id"])


def submit(client, card, answer="后进先出", request_id="test-request-001"):
    return client.post(
        "/api/attempts",
        json={"card_id": card["id"], "answer": answer, "request_id": request_id},
    )


def export_rows(client):
    response = client.get("/api/export")
    assert response.status_code == 200, response.text
    header = response.text.split("\n", 3)
    assert header[0] == "#separator:Tab"
    assert header[1] == "#html:false"
    assert header[2].startswith("#columns:NoteID\t")
    return list(csv.reader(io.StringIO(header[3]), delimiter="\t"))


def test_review_practice_and_export_only_approved_cards(client):
    cards = seed(client)
    assert len(cards) == 6
    assert {card["status"] for card in cards} == {"draft"}
    assert {card["kind"] for card in cards} == {"basic", "remediation"}
    assert export_rows(client) == []

    stack = next(card for card in cards if card["question"] == "栈的出入顺序规则是什么？")
    approve(client, stack)
    rejected = next(card for card in cards if card["id"] != stack["id"])
    assert change_card(client, rejected, status="rejected").status_code == 200

    response = submit(client, stack)
    assert response.status_code == 200, response.text
    attempt = response.json()
    assert attempt["status"] == "completed"
    assert attempt["diagnosis"]["verdict"] == "correct"
    assert attempt["answer"] == "后进先出"
    assert attempt["diagnosis"]["source_block_ids"]
    rows = export_rows(client)
    assert len(rows) == 1
    assert rows[0][0] == stack["id"]
    assert rows[0][1] == stack["question"]
    assert len(rows[0]) == 8
    assert rows[0][5] == "", "Self-authored text does not have real PDF page numbers"
    assert rejected["id"] not in {row[0] for row in rows}


def test_demo_loading_is_idempotent_and_keeps_review(client):
    cards = seed(client)
    approve(client, cards[0])
    original_ids = {card["id"] for card in cards}
    repeated = seed(client)
    assert {card["id"] for card in repeated} == original_ids
    assert next(card for card in repeated if card["id"] == cards[0]["id"])["status"] == "approved"


def test_repeated_request_reuses_attempt_and_rejects_changed_payload(client, monkeypatch):
    card = approve(client, seed(client)[0])
    calls = []
    original_diagnose = client.app.state.ai.diagnose_answer

    async def counted_diagnose(*args, **kwargs):
        calls.append(True)
        return await original_diagnose(*args, **kwargs)

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", counted_diagnose)
    first = submit(client, card, request_id="same-request-001")
    second = submit(client, card, request_id="same-request-001")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(calls) == 1
    assert len(state(client)["attempts"]) == 1

    conflicting = submit(client, card, answer="先进先出", request_id="same-request-001")
    assert conflicting.status_code == 409
    assert len(calls) == 1
    assert len(state(client)["attempts"]) == 1


@pytest.mark.parametrize("invalid_reference", ["unknown-block", "fabricated-quote", "duplicate-point"])
def test_approval_rejects_invalid_evidence_or_rubric(client, invalid_reference):
    card = seed(client)[0]
    body = {name: card[name] for name in CardUpdate.model_fields if name in card}
    body["status"] = "approved"
    if invalid_reference == "unknown-block":
        body["evidence"] = [{"block_id": "not-in-the-document", "quote": "栈"}]
    elif invalid_reference == "fabricated-quote":
        body["evidence"] = [dict(card["evidence"][0], quote="这句话不存在于任何示例原文中。")]
    else:
        body["rubric"] = [card["rubric"][0], card["rubric"][0]]
    response = client.put(f"/api/cards/{card['id']}", json=body)
    assert response.status_code == (404 if invalid_reference == "unknown-block" else 422), response.text
    assert state(client)["cards"][0]["status"] == "draft"
    assert export_rows(client) == []


def test_model_failure_retains_answer_without_inventing_a_verdict(client, monkeypatch):
    card = approve(client, seed(client)[0])
    observed_before_call = []

    async def fail_diagnosis(*args, **kwargs):
        observed_before_call.extend(client.app.state.store.all("attempts"))
        raise AIError("模型调用超时，答案已保留，请稍后重试。")

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", fail_diagnosis)
    answer = "  这是需要保留的学生原始答案。\n包含换行。  "
    response = submit(client, card, answer=answer)
    assert response.status_code == 200, response.text
    assert len(observed_before_call) == 1
    assert observed_before_call[0]["answer"] == answer
    assert observed_before_call[0]["status"] == "pending"
    attempt = response.json()
    assert attempt["status"] == "failed"
    assert attempt["answer"] == answer
    assert attempt["diagnosis"] is None
    assert attempt["error"]
    stored = state(client)["attempts"]
    assert len(stored) == 1
    assert stored[0]["answer"] == answer
    assert stored[0]["diagnosis"] is None


def test_blank_answer_is_unanswered_and_unapproved_cards_cannot_be_practiced(client, monkeypatch):
    card = seed(client)[0]

    async def must_not_call(*args, **kwargs):
        pytest.fail("Blank or unapproved submissions must not call AI")

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", must_not_call)
    response = submit(client, card, request_id="unapproved-001")
    assert response.status_code in {409, 422}
    assert state(client)["attempts"] == []

    approve(client, card)
    response = submit(client, card, answer=" \n\t", request_id="blank-request-001")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unanswered"
    assert response.json()["diagnosis"] is None
    assert response.json()["answer"] == " \n\t"


def test_historical_attempt_keeps_question_and_evidence_snapshot(client):
    card = approve(client, seed(client)[0])
    response = submit(client, card, request_id="snapshot-request-001")
    assert response.status_code == 200, response.text
    historical = response.json()
    snapshot = historical["card_snapshot"]
    source_snapshot = historical["source_snapshot"]
    assert snapshot["question"] == card["question"]
    assert snapshot["evidence"] == card["evidence"]
    assert {block["block_id"] for block in source_snapshot} == {
        evidence["block_id"] for evidence in card["evidence"]
    }
    assert all(block["text"] for block in source_snapshot)

    edited = change_card(client, card, question="修改后的新版本题目是什么？", status="approved")
    assert edited.status_code == 200, edited.text
    current = next(item for item in state(client)["cards"] if item["id"] == card["id"])
    assert current["question"] != snapshot["question"]
    assert current["version"] > snapshot["version"]
    saved = state(client)["attempts"][0]
    assert saved["card_snapshot"] == snapshot
    assert saved["source_snapshot"] == source_snapshot
    repeated = submit(client, card, request_id="snapshot-request-001")
    assert repeated.json() == historical


def test_text_document_import_preserves_source_and_has_no_implicit_approval(client):
    text = "栈是一种后进先出的线性结构。入栈和出栈都发生在栈顶。空栈不能执行出栈。"
    response = client.post("/api/documents/text", json={"title": "自有栈讲义", "text": text})
    assert response.status_code == 200, response.text
    current = state(client)
    assert len(current["documents"]) == 1
    assert any(text in block["text"] for block in current["blocks"])
    assert current["cards"] == []
    assert current["attempts"] == []


def test_generated_card_stays_draft_and_uses_selected_material(client, monkeypatch):
    text = "栈是一种后进先出的线性结构。入栈和出栈都发生在栈顶。空栈不能执行出栈。"
    imported = client.post("/api/documents/text", json={"title": "栈讲义", "text": text})
    assert imported.status_code == 200
    block = state(client)["blocks"][0]
    calls = []

    async def generate(blocks, count):
        calls.append((blocks, count))
        return DraftBatch.model_validate({"cards": [{
            "question": "栈遵循什么出入顺序？",
            "answer": "后进先出。",
            "concept": "栈",
            "kind": "basic",
            "rubric": [{"point_id": "lifo", "text": "后进先出"}],
            "evidence": [{"block_id": block["block_id"], "quote": "后进先出"}],
        }]})

    monkeypatch.setattr(client.app.state.ai, "generate_drafts", generate)
    response = client.post("/api/card-drafts", json={"block_ids": [block["block_id"]], "count": 1})
    assert response.status_code == 200, response.text
    generated = response.json()["cards"]
    assert len(generated) == 1
    assert generated[0]["status"] == "draft"
    assert generated[0]["document_id"] == imported.json()["id"]
    assert len(calls) == 1
    assert [item.block_id for item in calls[0][0]] == [block["block_id"]]
    assert calls[0][1] == 1
    assert export_rows(client) == []


def test_retry_records_the_current_model_after_configuration_change(client, monkeypatch):
    from ankisense.app import create_app
    from ankisense.llm import AIService

    card = approve(client, seed(client)[0])

    async def fail(*args):
        raise AIError("演示阶段失败")

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", fail)
    failed = submit(client, card).json()
    next_app = create_app(
        data_dir=client.app.state.store.path.parent,
        settings=Settings(mode="cloud", model="test-cloud-model"),
        ai_service=AIService(Settings(mode="demo")),  # Offline response double.
    )
    with TestClient(next_app) as next_client:
        retried = next_client.post(f"/api/attempts/{failed['id']}/retry").json()
    assert retried["status"] == "completed"
    assert retried["is_demo"] is False
    assert retried["model"] == "test-cloud-model"
    assert retried["analysis_history"][0]["is_demo"] is True
    assert retried["card_snapshot"] == failed["card_snapshot"]


def test_recovery_waits_until_application_startup(client):
    from ankisense.app import create_app

    card = approve(client, seed(client)[0])
    saved = submit(client, card).json()
    saved["status"] = "pending"
    client.app.state.store.put("attempts", saved)
    next_app = create_app(data_dir=client.app.state.store.path.parent, settings=Settings())
    assert next_app.state.store.get("attempts", saved["id"])["status"] == "pending"
    with TestClient(next_app) as next_client:
        recovered = next_client.get(f"/api/attempts/{saved['id']}").json()
    assert recovered["status"] == "failed"
    assert recovered["answer"] == saved["answer"]


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


def test_pdf_upload_extracts_selected_physical_page_and_rejects_invalid_range(client):
    content = pdf_bytes(["Stacks use LIFO.", "Queues use FIFO."])
    response = client.post(
        "/api/documents",
        files={"file": ("lecture.pdf", content, "application/pdf")},
        data={"start_page": "2", "end_page": "2"},
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["page_count"] == 2
    current = state(client)
    assert len(current["documents"]) == 1
    assert len(current["blocks"]) == 1
    block = current["blocks"][0]
    assert block["document_id"] == document["id"]
    assert block["page"] == 2
    assert "Queues use FIFO." in block["text"]
    assert "Stacks use LIFO." not in block["text"]
    downloaded = client.get(document["file_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == content

    invalid = client.post(
        "/api/documents",
        files={"file": ("lecture.pdf", content, "application/pdf")},
        data={"start_page": "3", "end_page": "3"},
    )
    assert invalid.status_code == 422
    assert "页码范围" in invalid.json()["detail"]
    assert len(state(client)["documents"]) == 1


def test_blank_pdf_is_retained_for_original_page_review(client, monkeypatch):
    from ankisense import ingest

    monkeypatch.setattr(ingest, "recognize_page", lambda content, page: {
        "page": page, "text": "", "raw_text": "", "method": "ocr",
        "engine": "offline-ocr-double", "engine_version": "test-1", "status": "empty",
    })
    response = client.post(
        "/api/documents",
        files={"file": ("blank.pdf", pdf_bytes([None]), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    current = state(client)
    assert len(current["documents"]) == 1
    assert len(current["source_pages"]) == 1
    assert current["source_pages"][0]["status"] == "empty"
    assert current["source_pages"][0]["block_ids"] == []
    assert current["blocks"] == []
