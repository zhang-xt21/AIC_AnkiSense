"""Source-calibration policy, authority, conflicts and continuation, all offline."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ankisense.auto_card_models import CardQualityReport, KnowledgeBatch, KnowledgeCardBatch
from ankisense.llm import AIError, AIService, Settings
from ankisense.schemas import SourceBlock
from ankisense.source_calibration_models import PageTranscription
from ankisense.source_policy import machine_verified
from ankisense.sources import page_records, require_current_reviewed

BEFORE = "栈遵循后进先山，最后入栈的元素最先出栈。"
AFTER = "栈遵循后进先出，最后入栈的元素最先出栈。"


class CalibrationAI:
    def __init__(self):
        self.reads = []
        self.reference_inputs = []
        self.entered = self.release = None
        self.fail = False

    async def transcribe_source_page(self, image, reference_text=None):
        self.reference_inputs.append(reference_text)
        if self.entered:
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 5)
        if self.fail:
            raise AIError("离线图片输入失败。")
        return self.reads.pop(0) if self.reads else PageTranscription(text=AFTER)

    async def extract_knowledge(self, blocks, focus_block_ids=None):
        return KnowledgeBatch.model_validate({"points": [{"knowledge_id": "order", "concept": "栈", "statement": AFTER,
            "conditions": [], "evidence": [{"block_id": blocks[0].block_id, "quote": blocks[0].text}]}]})

    async def generate_knowledge_cards(self, blocks, points):
        return KnowledgeCardBatch.model_validate({"cards": [{"knowledge_id": "order", "card": {
            "question": "栈的顺序是什么？", "answer": "后进先出。", "concept": "栈", "rubric": [{"point_id": "order", "text": "后进先出"}],
            "evidence": [p.model_dump() for p in points[0].evidence]}}]})

    async def review_card(self, card, blocks):
        return CardQualityReport(verdict="clear", issues=[], supported_point_ids=["order"])


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    from ankisense import source_calibration
    ai = CalibrationAI()
    app = create_app(tmp_path / "app", Settings(mode="cloud", base_url="https://offline.test/v1", model="offline-vision", api_key="fixture-key"), ai)
    doc_id = "pdf-" + "1" * 24
    app.state.store.put("documents", {"id": doc_id, "title": "自编扫描样本", "filename": "fixture.pdf", "is_demo": False, "file_url": f"/api/documents/{doc_id}/file"})
    app.state.store.put_many(page_records(doc_id, {"page": 1, "text": BEFORE, "raw_text": BEFORE,
        "status": "pending", "method": "ocr", "ocr_lines": [{"text": BEFORE, "start": 0, "end": len(BEFORE), "bbox": [.1,.1,.9,.3], "score": .7}]}))
    uploads = tmp_path / "app" / "uploads"
    uploads.mkdir()
    (uploads / f"{doc_id}.pdf").write_bytes(b"%PDF-fictional-original")
    monkeypatch.setattr(source_calibration, "render_page_png", lambda *args: b"fictitious-png")
    monkeypatch.setattr(source_calibration, "recognize_region", lambda *args: {"text": AFTER})
    with TestClient(app) as client:
        yield client, ai, doc_id


def path(doc):
    return f"/api/documents/{doc}/pages/1/calibration"


def current(client, doc):
    return client.app.state.store.get("source_pages", f"{doc}-p1")


def test_fixed_policy_cases():
    from tools.evaluate_source_calibration import policy_cases
    result = policy_cases()
    assert result["real_model_accuracy"] is None
    assert len(result["results"]) == 10
    assert all(row["passed"] for row in result["results"]), result


@pytest.mark.parametrize("before,after", [("容量是三十个元素。", "容量是八十个元素。"),
    ("容量为128字节。", "容量为128位。"), ("电流为20毫安。", "电流为20微安。"),
    ("更新表达式为a+b。", "更新表达式为a-b。")])
def test_critical_symbols_units_and_chinese_numbers_never_auto_change(before, after):
    from ankisense.source_calibration import assess
    report = assess({"page":1,"text":before,"raw_text":before,"status":"pending"},
                    PageTranscription(text=after), PageTranscription(text=after))
    assert not report["protected_content_unchanged"]
    assert report["issues"]


def test_correction_has_new_source_revision_proof_diff_and_old_text(context):
    client, ai, doc = context
    old = current(client, doc)
    response = client.post(path(doc), json={"revision": 1})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "auto_applied" and report["changes"][0]["local_supported"]
    page = current(client, doc)
    assert page["text"] == AFTER and page["raw_text"] == BEFORE
    assert page["status"] == "auto_verified" and page["reviewed_at"] is None
    assert page["revision"] == 2 and machine_verified(page)
    assert ai.reference_inputs == [BEFORE, None]
    assert client.app.state.store.get("source_revisions", f"{doc}-p1-r1")["text"] == BEFORE
    assert client.app.state.store.get("blocks", old["block_ids"][0])["text"] == BEFORE
    assert client.app.state.store.get("source_calibrations", report["id"]) == report
    require_current_reviewed(client.app.state.store, [SourceBlock.model_validate(client.app.state.store.get("blocks", page["block_ids"][0]))])
    with pytest.raises(HTTPException):
        require_current_reviewed(client.app.state.store, [SourceBlock.model_validate(client.app.state.store.get("blocks", old["block_ids"][0]))])
    assert client.get(path(doc)).json()["id"] == report["id"]
    assert client.post(path(doc), json={"revision": 2}).json()["status"] == "already_verified"
    assert len(ai.reference_inputs) == 2


def test_disagreement_is_saved_without_overwriting_and_retries_use_cache(context):
    client, ai, doc = context
    ai.reads = [PageTranscription(text=AFTER), PageTranscription(text=BEFORE)]
    report = client.post(path(doc), json={"revision": 1}).json()
    assert report["status"] == "needs_confirmation" and report["issues"]
    assert current(client, doc)["text"] == BEFORE and current(client, doc)["revision"] == 1
    assert client.post(path(doc), json={"revision": 1}).json()["id"] == report["id"]
    assert len(ai.reference_inputs) == 2
    # Loading and saving a candidate is an explicit human confirmation, not a machine label.
    response = client.put(f"/api/documents/{doc}/pages/1", json={"revision": 1, "text": report["proposed_text"]})
    assert response.status_code == 200 and response.json()["status"] == "reviewed"
    assert response.json()["verification"] is None
    assert client.get(path(doc)).json()["status"] == "not_checked"


def test_local_disagreement_refuses_automatic_adoption(context, monkeypatch):
    client, ai, doc = context
    monkeypatch.setattr("ankisense.source_calibration.recognize_region", lambda *args: {"text": BEFORE})
    report = client.post(path(doc), json={"revision": 1}).json()
    assert report["status"] == "needs_confirmation"
    assert not report["local_changes_supported"]
    assert current(client, doc)["status"] == "pending"


def test_local_ocr_failure_keeps_visual_candidate_for_confirmation(context, monkeypatch):
    client, ai, doc = context
    def unavailable(*args):
        raise ValueError("Local OCR unavailable")
    monkeypatch.setattr("ankisense.source_calibration.recognize_region", unavailable)
    response = client.post(path(doc), json={"revision":1})
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "needs_confirmation" and report["proposed_text"] == AFTER
    assert "本地局部复识别" in report["issues"][0]
    assert current(client, doc)["text"] == BEFORE
    assert not machine_verified(current(client, doc))


def test_image_failure_cannot_publish_candidate_or_machine_status(context):
    client, ai, doc = context
    ai.fail = True
    assert client.post(path(doc), json={"revision": 1}).status_code == 422
    assert current(client, doc)["text"] == BEFORE
    assert not client.app.state.store.all("source_calibrations")


def test_revision_conflict_and_model_settings_lock(context):
    client, ai, doc = context
    ai.entered, ai.release = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, path(doc), json={"revision": 1})
        try:
            assert ai.entered.wait(3)
            assert client.put("/api/settings/ai", json={"mode":"demo", "base_url":"https://offline.test/v1", "model":"offline-vision", "timeout_seconds":45}).status_code == 409
            assert client.put(f"/api/documents/{doc}/pages/1", json={"revision":1,"text":BEFORE+"人工修订。"}).status_code == 200
        finally:
            ai.release.set()
        assert future.result(timeout=6).status_code == 409
    assert current(client, doc)["text"].endswith("人工修订。")
    assert not client.app.state.store.all("source_calibrations")


def test_auto_task_calibrates_then_generates_without_manual_source_confirmation(context):
    client, ai, doc = context
    response = client.post(f"/api/documents/{doc}/auto-card-jobs", json={"source_mode":"visual","image_mode":"off"})
    assert response.status_code == 200
    job = response.json()
    for _ in range(200):
        job = client.get(f"/api/auto-card-jobs/{job['id']}").json()
        if job["status"] not in {"pending","running"}:
            break
        time.sleep(.01)
    assert job["status"] == "completed", job
    page = current(client, doc)
    assert machine_verified(page) and page["reviewed_at"] is None
    cards = client.get("/api/state").json()["cards"]
    assert len(cards) == 1 and cards[0]["status"] == "draft"
    assert cards[0]["evidence"][0]["block_id"].endswith("-r2")
    # Human card approval still gates export; machine sources remain distinguishable.
    from io import BytesIO
    from PIL import Image
    from ankisense.media import normalize_image, save_media
    output = BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    media = save_media(client.app.state.store, client.app.state.auto_cards.storage, normalize_image(output.getvalue()),
                       source={"document_id": doc, "page": 1, "revision": 2})
    from ankisense.schemas import CardDraft
    body = {key: cards[0][key] for key in CardDraft.model_fields}
    response = client.put(f"/api/cards/{cards[0]['id']}", json={**body,"expected_version":1,"status":"approved","images":[{"media_id":media["id"],"side":"answer"}]})
    assert response.status_code == 200, response.text
    catalog = client.app.state.plugin.sync.catalog("http://127.0.0.1:8765")
    assert len(catalog["cards"]) == 1 and catalog["excluded"] == []
    assert client.get("/api/export").status_code == 200


def test_manual_mode_never_sends_images(context):
    client, ai, doc = context
    job = client.post(f"/api/documents/{doc}/auto-card-jobs", json={"image_mode":"off"}).json()
    for _ in range(100):
        job = client.get(f"/api/auto-card-jobs/{job['id']}").json()
        if job["status"] not in {"pending","running"}:
            break
        time.sleep(.01)
    assert job["status"] == "awaiting_confirmation" and ai.reference_inputs == []


def test_human_confirmation_of_identical_machine_text_keeps_evidence_identity(context):
    client, ai, doc = context
    report = client.post(path(doc), json={"revision":1}).json()
    original = current(client, doc)
    signature = client.app.state.auto_cards._capture(doc)[-1]
    snapshot = client.app.state.store.get("source_revisions", f"{doc}-p1-r2")
    result = client.put(f"/api/documents/{doc}/pages/1", json={"revision":2,"text":AFTER})
    assert result.status_code == 200
    current_page = result.json()
    assert current_page["status"] == "reviewed" and current_page["reviewed_at"]
    assert current_page["revision"] == original["revision"] == 2
    assert current_page["block_ids"] == original["block_ids"]
    assert client.app.state.auto_cards._capture(doc)[-1] == signature
    assert client.app.state.store.get("source_revisions", f"{doc}-p1-r2") == snapshot
    history = client.get(f"/api/documents/{doc}/pages/1/revisions").json()
    assert len(history["confirmations"]) == 1
    assert history["confirmations"][0]["calibration_id"] == report["id"]


@pytest.mark.parametrize("change", ["text", "revision", "proof", "status"])
def test_machine_authority_cannot_survive_tampering_or_new_revision(context, change):
    client, ai, doc = context
    client.post(path(doc), json={"revision":1})
    page = current(client, doc)
    if change == "text": page["text"] += "额外内容。"
    elif change == "revision": page["revision"] += 1
    elif change == "proof": page["verification"]["visual_reads_agree"] = False
    else: page["status"] = "pending"
    assert not machine_verified(page)


def test_visual_adapter_sends_original_image_and_no_reference_on_blind_read(monkeypatch):
    seen = []
    real_client = httpx.AsyncClient
    def handler(request):
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "fixture-secret" not in request.content.decode()
        seen.append(json.loads(content[0]["text"]))
        return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"text":AFTER,"uncertainties":[]})}}]})
    monkeypatch.setattr("ankisense.llm.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler),**kwargs))
    service = AIService(Settings(mode="cloud",base_url="https://offline.test/v1",model="offline",api_key="fixture-secret"))
    async def run():
        await service.transcribe_source_page(b"png",reference_text=BEFORE)
        await service.transcribe_source_page(b"png")
    asyncio.run(run())
    assert seen[0]["reference_ocr_text"] == BEFORE
    assert "reference_ocr_text" not in seen[1]
