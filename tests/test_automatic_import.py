"""Unattended import, visual checking, partial failures and source invalidation."""
from io import BytesIO
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from ankisense.llm import Settings, AIError
from ankisense.ingest import automatic_page_skip
from ankisense.automatic_policy import automatic_evidence_valid
from ankisense.presentations import slide_text
from ankisense.schemas import SourceBlock
from ankisense.sources import save_page_revision
from test_auto_cards import FakeAI


def lecture_pdf(pages=2):
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=720, height=540)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 18 Tf 40 460 Td (Stack {index + 1}: The last element pushed is the first popped.) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class VisualAI(FakeAI):
    async def extract_knowledge_visual(self, blocks, focus_block_ids, images, page_numbers):
        assert all(image.startswith(b"\x89PNG") for image in images)
        assert page_numbers == sorted({b.page for b in blocks})
        return await self.extract_knowledge(blocks, focus_block_ids)

    async def review_card_visual(self, card, blocks, images, page_numbers):
        assert images and page_numbers
        return await self.review_card(card, blocks)

    async def review_cards_visual(self, cards, blocks, images, page_numbers):
        return [await self.review_card_visual(c, blocks, images, page_numbers) for c in cards]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    app = create_app(tmp_path / "app", Settings(mode="cloud", base_url="https://offline.invalid/v1", model="offline-visual", api_key="test-placeholder"), VisualAI())
    with TestClient(app) as instance:
        yield instance


def upload(client, content=None, name="lecture.pdf"):
    result = client.post("/api/material-imports", files={"file": (name, content or lecture_pdf(), "application/octet-stream")})
    assert result.status_code == 200, result.text
    return result.json()


def wait_import(client, record):
    for _ in range(1500):
        current = client.get(f"/api/material-imports/{record['id']}").json()
        if current["status"] not in {"pending", "running"}:
            return current
        time.sleep(.01)
    raise AssertionError(current)


def test_upload_alone_generates_ready_cards_with_original_pages(client):
    result = wait_import(client, upload(client))
    assert result["status"] == "completed", result
    assert len(result["job"]["card_ids"]) == 2
    data = client.get("/api/state").json()
    assert all(p["status"] == "pending" and not p.get("reviewed_at") for p in data["source_pages"])
    for card in data["cards"]:
        assert card["status"] == "approved" and card["approval_method"] == "automatic"
        assert card["auto_quality"]["scope"] == "text_and_original_page"
        assert card["images"] and card["images"][0]["side"] == "answer"
        blocks = [SourceBlock.model_validate(b) for b in data["blocks"] if b["block_id"] in {e["block_id"] for e in card["evidence"]}]
        assert automatic_evidence_valid(card, blocks)
        assert client.app.state.attempts._current_sources(card)
        assert client.app.state.plugin.sync._validated(card, client.app.state.store)
    assert len([line for line in client.get("/api/export").text.splitlines() if line.startswith("autcard-")]) == 2
    assert client.get("/api/export/apkg", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert upload(client)["id"] == result["id"]
    assert len(client.get("/api/state").json()["cards"]) == 2


def test_full_lecture_over_legacy_30_page_limit(client):
    result = wait_import(client, upload(client, lecture_pdf(31)))
    assert result["status"] == "completed", result
    assert result["job"]["progress"] == {"completed": 11, "total": 11}
    assert len(result["job"]["card_ids"]) == 31


def test_automatic_source_changes_invalidate_usable_cards(client):
    result = wait_import(client, upload(client))
    store = client.app.state.store
    card_id = result["job"]["card_ids"][0]
    card = store.get("cards", card_id)
    altered = {**card, "answer": "Invented answer"}
    blocks = client.app.state.attempts._sources(card)
    assert not automatic_evidence_valid(altered, blocks)
    save_page_revision(store, card["document_id"], blocks[0].page, 1, {"text": "Changed source", "status": "pending"}, "test")
    updated = store.get("cards", card_id)
    assert updated["status"] == "draft" and updated["source_outdated"]
    assert not automatic_evidence_valid(updated, blocks)
    assert card_id not in client.get("/api/export").text


def test_failed_batch_retries_then_continues_without_manual_confirmation(client):
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    calls = 0
    async def fail_first_batch(*args):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise AIError("Temporary fixture failure")
        return await original(*args)
    ai.extract_knowledge_visual = fail_first_batch
    result = wait_import(client, upload(client, lecture_pdf(7)))
    assert result["status"] == "partial" and result["retryable"], result
    assert result["job"]["progress"]["completed"] == 3
    assert len(result["job"]["card_ids"]) == 4
    assert any(i["code"] == "batch_failed" and i["pages"] == [1, 2, 3] for i in result["job"]["issues"])


def test_unresolved_card_is_visible_without_false_automatic_approval(client):
    client.app.state.ai.bad_quality = True
    result = wait_import(client, upload(client))
    assert result["status"] == "completed"
    cards = client.get("/api/state").json()["cards"]
    assert len(cards) == 2
    assert all(c["status"] == "draft" and c["auto_quality"]["issues"] and not c.get("automatic_evidence") for c in cards)
    assert client.get("/api/export").text.count("autcard-") == 0


def test_wrong_format_and_missing_model_are_actionable(client):
    assert client.post("/api/material-imports", files={"file": ("x.pdf", b"invalid")}).status_code == 422
    assert client.post("/api/material-imports", files={"file": ("x.exe", b"x")}).status_code == 422
    client.app.state.settings = Settings(mode="demo")
    assert client.post("/api/material-imports", files={"file": ("lecture.pdf", lecture_pdf())}).status_code == 422


def test_pptx_upload_uses_native_text_and_starts_pipeline(client, monkeypatch):
    monkeypatch.setattr("ankisense.automatic_import.convert_to_pdf", lambda *args: lecture_pdf())
    monkeypatch.setattr("ankisense.automatic_import.slide_text", lambda *args: ["Native stack fact: last in, first out.", "Native queue fact: first in, first out."])
    result = wait_import(client, upload(client, b"PK-test-fixture", "lecture.pptx"))
    assert result["status"] == "completed", result
    data = client.get("/api/state").json()
    assert len(data["cards"]) == 2
    assert all(p["engine"] == "pptx-native" for p in data["source_pages"])
    assert data["documents"][0]["filename"] == "lecture.pptx"


def test_finished_checkpoints_wait_for_explicit_resume_on_service_restart(client):
    result = wait_import(client, upload(client))
    store = client.app.state.store
    job = store.get("auto_card_jobs", result["job_id"])
    saved_ids = job["card_ids"][:]
    calls = client.app.state.ai.generation_calls
    job.update(status="pending", stage="quality")
    store.put("auto_card_jobs", job)
    async def recover():
        client.app.state.auto_cards.recover()
    client.portal.call(recover)
    result = wait_import(client, result)
    assert result["status"] == "interrupted" and result["retryable"]
    assert client.app.state.ai.generation_calls == calls
    assert client.post(f"/api/material-imports/{result['id']}/retry").status_code == 200
    result = wait_import(client, result)
    assert result["status"] == "completed"
    assert result["job"]["card_ids"] == saved_ids
    assert len(client.get("/api/state").json()["cards"]) == 2
    assert client.app.state.ai.generation_calls == calls


def test_pptx_native_text_follows_slide_relationship_order(tmp_path):
    path = tmp_path / "lecture.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="r2"/><p:sldId id="257" r:id="r1"/></p:sldIdLst></p:presentation>')
        archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>')
        for i in [1, 2]:
            archive.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>Slide {i}</a:t></a:r><a:r><a:t> fact</a:t></a:r></a:p></p:sld>')
    assert slide_text(path) == ["Slide 2 fact", "Slide 1 fact"]


def test_outline_skipping_does_not_skip_facts_about_directories():
    assert automatic_page_skip("• 本章主要教学目标\n掌握课程概念和代码的对应关系。")
    assert automatic_page_skip("程序的机器级表示\n分以下五个部分介绍\n机器指令\n过程调用")
    assert automatic_page_skip("AssemblyLanguage.mp4")
    assert not automatic_page_skip("文件目录\n目录存放文件的名称和位置，支持按名称查找文件。")
