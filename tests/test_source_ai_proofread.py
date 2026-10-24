"""Offline visual proofreading contract; provider calls are replaced by fakes."""

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from ankisense.app import create_app
from ankisense.llm import AIService, Settings, SourceProofreadResult


OCR_TEXT = "栈遵循后进先出。"
SUGGESTED = "栈遵循后进先出，入栈和出栈都在栈顶进行。"


def blank_pdf(width=612):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeVision:
    def __init__(self, settings=None):
        self.settings = settings or Settings(mode="cloud", base_url="https://vision.example/v1",
                                             model="test-vision", api_key="private-test-key")
        self.calls = []

    async def proofread_page(self, png, text, page):
        self.calls.append((png, text, page))
        return SourceProofreadResult(suggested_text=SUGGESTED,
                                     issues=["第 1 页公式和图示仍须对照原图确认。"], uncertain=True)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "isolated"))
    from ankisense import ingest
    monkeypatch.setattr(ingest, "recognize_page", lambda _content, page: {
        "page": page, "text": OCR_TEXT, "raw_text": OCR_TEXT,
        "method": "ocr", "engine": "test-local-ocr", "status": "pending",
    })
    fake = FakeVision()
    application = create_app(data_dir=tmp_path / "data", settings=fake.settings, ai_service=fake)
    with TestClient(application) as client:
        response = client.post("/api/documents", files={"file": ("scan.pdf", blank_pdf(), "application/pdf")})
        assert response.status_code == 200, response.text
        document = response.json()
        page = next(item for item in client.get("/api/state").json()["source_pages"]
                    if item["document_id"] == document["id"])
        yield client, fake, document, page


def url(document):
    return f"/api/documents/{document['id']}/pages/1/ai-proofread"


def test_proposal_is_cached_without_confirming_source(setup):
    client, fake, document, source = setup
    first = client.post(url(document), json={"revision": source["revision"]})
    assert first.status_code == 200, first.text
    proposal = first.json()
    assert set(proposal) == {"id", "document_id", "page", "revision", "suggested_text",
                             "issues", "uncertain", "model", "created_at", "cached"}
    assert proposal["suggested_text"] == SUGGESTED
    assert proposal["issues"] and proposal["uncertain"] is True
    assert proposal["cached"] is False
    assert len(fake.calls) == 1
    assert fake.calls[0][0].startswith(b"\x89PNG\r\n\x1a\n")
    assert fake.calls[0][1:] == (OCR_TEXT, 1)
    state = client.get("/api/state").json()
    assert state["source_proposals"] == [proposal]
    assert next(item for item in state["source_pages"] if item["id"] == source["id"]) == source
    assert source["status"] == "pending"
    again = client.post(url(document), json={"revision": source["revision"]})
    assert again.status_code == 200
    assert again.json() == {**proposal, "cached": True}
    assert len(fake.calls) == 1


def test_adoption_uses_existing_proofread_save_and_invalidates_old_proposal(setup):
    client, fake, document, source = setup
    proposal = client.post(url(document), json={"revision": source["revision"]}).json()
    saved = client.put(f"/api/documents/{document['id']}/pages/1",
                       json={"revision": source["revision"], "text": proposal["suggested_text"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "reviewed"
    assert saved.json()["revision"] == source["revision"] + 1
    assert saved.json()["raw_text"] == OCR_TEXT
    stale = client.post(url(document), json={"revision": source["revision"]})
    assert stale.status_code == 409
    fresh = client.post(url(document), json={"revision": saved.json()["revision"]})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["id"] != proposal["id"]
    assert len(fake.calls) == 2


def test_thinking_policy_change_does_not_reuse_provider_default_suggestion(setup, monkeypatch):
    client, fake, document, source = setup
    fake.settings = Settings(mode="cloud", base_url="https://api.deepseek.com", model="deepseek-flash", api_key="test-only")
    # Simulate a persisted result from the former provider-default policy.
    with monkeypatch.context() as previous_policy:
        previous_policy.setattr("ankisense.source_proofread.generation_options", lambda *_args: {})
        previous = client.post(url(document), json={"revision": source["revision"]}).json()
    current = client.post(url(document), json={"revision": source["revision"]}).json()
    assert current["id"] != previous["id"] and current["cached"] is False
    assert len(fake.calls) == 2
    assert client.post(url(document), json={"revision": source["revision"]}).json()["cached"] is True
    assert len(fake.calls) == 2


def test_legacy_proof_v1_pdf_without_stored_hash_can_still_be_checked(setup):
    client, fake, document, source = setup
    original = client.app.state.store.get("documents", document["id"])
    original.pop("pdf_sha256")
    client.app.state.store.put("documents", original)
    assert "pdf_sha256" not in client.app.state.store.get("documents", document["id"])
    proposal = client.post(url(document), json={"revision": source["revision"]})
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["suggested_text"] == SUGGESTED
    assert len(fake.calls) == 1
    # The old row is not silently rewritten during a read-only AI suggestion.
    assert "pdf_sha256" not in client.app.state.store.get("documents", document["id"])


def test_legacy_pdf_replaced_on_disk_is_rejected_before_model_call(setup):
    client, fake, document, source = setup
    original = client.app.state.store.get("documents", document["id"])
    original.pop("pdf_sha256")
    client.app.state.store.put("documents", original)
    upload = client.app.state.store.path.parent / "uploads" / f"{document['id']}.pdf"
    upload.write_bytes(blank_pdf(width=613))
    response = client.post(url(document), json={"revision": source["revision"]})
    assert response.status_code == 409
    assert fake.calls == []
    assert client.get("/api/state").json()["source_proposals"] == []


def test_model_failure_and_invalid_output_leave_page_untouched(setup, monkeypatch):
    client, fake, document, source = setup

    async def invalid(*_args):
        return {"suggested_text": "假内容", "issues": [], "uncertain": "no"}

    monkeypatch.setattr(fake, "proofread_page", invalid)
    failed = client.post(url(document), json={"revision": source["revision"]})
    assert failed.status_code == 502
    assert "假内容" not in failed.text
    state = client.get("/api/state").json()
    assert state["source_proposals"] == []
    assert state["source_pages"][0] == source

    async def unexpected(*_args):
        raise RuntimeError("private-test-key: provider supplied raw diagnostics")

    monkeypatch.setattr(fake, "proofread_page", unexpected)
    error = client.post(url(document), json={"revision": source["revision"]})
    assert error.status_code == 502
    assert "private-test-key" not in error.text
    assert client.get("/api/state").json()["source_proposals"] == []


def test_demo_mode_and_missing_pdf_are_explicit(setup):
    client, fake, document, source = setup
    fake.settings = Settings(mode="demo")
    unavailable = client.post(url(document), json={"revision": source["revision"]})
    assert unavailable.status_code == 503
    assert not fake.calls
    fake.settings = Settings(mode="cloud", base_url="https://vision.example/v1",
                             model="test-vision", api_key="private-test-key")
    assert client.post("/api/documents/missing/pages/1/ai-proofread", json={"revision": 1}).status_code == 404
    assert client.post(f"/api/documents/{document['id']}/pages/2/ai-proofread", json={"revision": 1}).status_code == 404


def test_stale_page_during_model_call_does_not_store_proposal(setup, monkeypatch):
    client, fake, document, source = setup
    entered, release = Event(), Event()

    async def delayed(*_args):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return SourceProofreadResult(suggested_text=SUGGESTED, issues=[], uncertain=False)

    monkeypatch.setattr(fake, "proofread_page", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, url(document), json={"revision": source["revision"]})
        try:
            assert entered.wait(10)
            busy = client.put("/api/settings/ai", json={"mode": "demo", "base_url": "", "model": "",
                                                           "timeout_seconds": 45, "clear_api_key": True})
            assert busy.status_code == 409
            changed = client.put(f"/api/documents/{document['id']}/pages/1",
                                 json={"revision": source["revision"], "text": "人工校订的新原文"})
            assert changed.status_code == 200, changed.text
        finally:
            release.set()
        result = pending.result(timeout=10)
    assert result.status_code == 409, result.text
    assert client.get("/api/state").json()["source_proposals"] == []


def test_transport_uses_one_page_image_and_rejects_unsupported_vision(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert payload["messages"][1]["content"][0]["text"] == json.dumps(
            {"page": 1, "local_extracted_text": OCR_TEXT}, ensure_ascii=False)
        image = payload["messages"][1]["content"][1]
        assert image["type"] == "image_url"
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
        assert "private-test-key" not in request.content.decode()
        return httpx.Response(400, json={"error": {"message": "secret provider details"}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr("ankisense.llm.httpx.AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    service = AIService(Settings(mode="cloud", base_url="https://vision.example/v1",
                                 model="test-vision", api_key="private-test-key"))
    with pytest.raises(Exception, match="支持视觉输入") as error:
        asyncio.run(service.proofread_page(b"\x89PNG\r\n\x1a\nsmall-fake", OCR_TEXT, 1))
    assert "secret provider details" not in str(error.value)
    assert len(seen) == 1
