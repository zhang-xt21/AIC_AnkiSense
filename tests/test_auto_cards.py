"""Offline recovery, source integrity and review checks for automatic generation."""
import asyncio
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

from ankisense.auto_card_models import (CardQualityReport, KnowledgeBatch,
                                      KnowledgeCard, KnowledgeCardBatch, KnowledgePoint,
                                      QualityIssue)
from ankisense.llm import AIError, Settings
from ankisense.schemas import CardDraft, Diagnosis, Evidence, RubricPoint, SourceBlock
from ankisense.sources import page_records


class FakeAI:
    def __init__(self):
        self.extract_calls = 0
        self.generation_calls = 0
        self.review_calls = 0
        self.repair_calls = 0
        self.fail_extract = None
        self.fail_review = False
        self.bad_quality = False
        self.entered, self.release = None, None

    async def extract_knowledge(self, blocks, focus_block_ids=None):
        self.extract_calls += 1
        if self.entered:
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 5)
        if self.extract_calls == self.fail_extract:
            raise AIError("离线模拟：本批调用失败。")
        return KnowledgeBatch(points=[KnowledgePoint(knowledge_id=f"k{index}", concept="来源知识点",
                               statement=block.text[:40], conditions=[], evidence=[Evidence(block_id=block.block_id, quote=block.text[:40])])
                               for index, block in enumerate(blocks) if focus_block_ids is None or block.block_id in focus_block_ids])

    async def generate_knowledge_cards(self, blocks, points):
        self.generation_calls += 1
        return KnowledgeCardBatch(cards=[KnowledgeCard(knowledge_id=p.knowledge_id,
            card=CardDraft(question=f"{p.evidence[0].block_id}说明了什么？", answer=p.statement,
                           concept=p.concept, rubric=[RubricPoint(point_id="fact", text=p.statement)], evidence=p.evidence)) for p in points])

    async def review_card(self, card, blocks):
        self.review_calls += 1
        if self.fail_review:
            raise AIError("离线模拟：质检失败。")
        if self.bad_quality:
            return CardQualityReport(verdict="needs_review", supported_point_ids=[],
                      issues=[QualityIssue(code="unsupported_answer", severity="error", reason="答案依据不足。", evidence=card.evidence)])
        return CardQualityReport(verdict="clear", issues=[], supported_point_ids=["fact"])

    async def repair_card(self, card, blocks, report):
        self.repair_calls += 1
        return card

    async def diagnose_answer(self, card, blocks, answer):
        return Diagnosis(verdict="correct", addressed_point_ids=[p.point_id for p in card.rubric],
                         missing_point_ids=[], contradicted_point_ids=[], answer_spans=[answer],
                         source_block_ids=[card.evidence[0].block_id], reason="离线自测固定响应，不代表模型准确率。")

    async def minimal_answer_for_question(self, card, blocks):
        return card.answer  # Explicit offline fixture, not semantic evaluation.


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    app = create_app(tmp_path / "app", Settings(mode="cloud", base_url="https://offline.invalid/v1", model="offline-test", api_key="offline-placeholder"), FakeAI())
    with TestClient(app) as instance:
        yield instance


def import_text(client, size=50):
    text = ("栈遵循后进先出，最后入栈的元素最先出栈。" * (size // 20 + 1))[:size]
    response = client.post("/api/documents/text", json={"title": "离线制卡样本", "text": text})
    assert response.status_code == 200
    return response.json()


def start(client, doc, **options):
    response = client.post(f"/api/documents/{doc['id']}/auto-card-jobs", json={"image_mode": "off", **options})
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(client, job, statuses=None):
    expected = statuses or {"completed", "failed", "outdated", "awaiting_confirmation", "cancelled"}
    for _ in range(400):
        current = client.get(f"/api/auto-card-jobs/{job['id']}").json()
        if current["status"] in expected:
            return current
        time.sleep(.01)
    raise AssertionError(current)


def cards(client):
    return client.get("/api/state").json()["cards"]


def test_document_batches_quality_and_duplicate_start(client):
    doc = import_text(client, 18050)
    job = wait_job(client, start(client, doc))
    assert job["status"] == "completed", job
    assert job["progress"] == {"completed": 3, "total": 3}
    saved = cards(client)
    assert len(saved) == 7
    assert {c["status"] for c in saved} == {"draft"}
    assert all(c["auto_quality"]["verdict"] == "clear" for c in saved)
    assert all(c["auto_job_id"] == job["id"] for c in saved)
    assert start(client, doc)["id"] == job["id"]
    assert len(cards(client)) == 7
    assert client.app.state.ai.extract_calls == 3
    assert client.get(f"/api/documents/{doc['id']}/auto-card-jobs").json()["jobs"][0]["id"] == job["id"]
    assert client.get("/api/export").text.count("autcard-") == 0


def test_partial_failure_retries_only_uncommitted_batches(client):
    doc = import_text(client, 18050)
    ai = client.app.state.ai
    ai.fail_extract = 2
    job = wait_job(client, start(client, doc))
    assert job["status"] == "failed"
    old_ids = {c["id"] for c in cards(client)}
    assert len(old_ids) == 3
    response = client.post(f"/api/auto-card-jobs/{job['id']}/retry", json={})
    assert response.status_code == 200
    job = wait_job(client, job)
    assert job["status"] == "completed", job
    assert len(cards(client)) == 7
    assert old_ids.issubset({c["id"] for c in cards(client)})
    assert ai.generation_calls == 3


def test_semantic_issues_are_retained_after_one_repair(client):
    doc = import_text(client)
    ai = client.app.state.ai
    ai.bad_quality = True
    job = wait_job(client, start(client, doc))
    assert job["status"] == "completed"
    card = cards(client)[0]
    assert card["status"] == "draft"
    assert card["auto_quality"]["verdict"] == "needs_review"
    assert card["auto_quality"]["repair_attempts"] == 1
    assert card["auto_quality"]["repair_history"]["quality"]["issues"]
    assert ai.repair_calls == 1 and ai.review_calls == 2
    assert job["issues"][0]["card_id"] == card["id"]


def test_quality_failure_does_not_save_unchecked_cards(client):
    client.app.state.ai.fail_review = True
    job = wait_job(client, start(client, import_text(client)))
    assert job["status"] == "failed"
    assert not cards(client)


def test_card_limit_reports_unprocessed_material(client):
    job = wait_job(client, start(client, import_text(client, 18050), max_cards=2))
    assert job["status"] == "completed"
    assert len(cards(client)) == 2
    assert any(i["code"] == "card_limit" for i in job["issues"])
    assert job["progress"]["completed"] < job["progress"]["total"]


def test_confirmation_then_continue_preserves_previous_batches(client):
    doc = import_text(client)
    store = client.app.state.store
    text = "队列遵循先进先出，最先入队的元素最先出队。"
    store.put_many(page_records(doc["id"], {"page": 1, "text": text, "raw_text": text, "status": "pending", "method": "ocr"}))
    job = wait_job(client, start(client, doc))
    assert job["status"] == "awaiting_confirmation"
    assert job["calibration"][0]["status"] == "needs_confirmation"
    assert job["issues"] and not cards(client)
    assert client.app.state.ai.extract_calls == 0
    response = client.put(f"/api/documents/{doc['id']}/pages/1", json={"revision": 1, "text": text})
    assert response.status_code == 200
    assert client.post(f"/api/auto-card-jobs/{job['id']}/retry", json={}).status_code == 200
    job = wait_job(client, job)
    assert job["status"] == "completed", job
    assert len(cards(client)) == 1
    assert cards(client)[0]["evidence"][0]["block_id"].endswith("-r1")
    assert client.post(f"/api/auto-card-jobs/{job['id']}/retry", json={}).status_code == 409
    assert len(cards(client)) == 1


def test_source_change_during_model_call_discards_current_batch(client):
    doc = import_text(client)
    store = client.app.state.store
    text = "栈遵循后进先出，最后入栈的元素最先出栈。"
    store.put_many(page_records(doc["id"], {"page": 1, "text": text, "status": "reviewed", "reviewed_at": "now"}))
    ai = client.app.state.ai
    ai.entered, ai.release = Event(), Event()
    job = start(client, doc)
    try:
        assert ai.entered.wait(3)
        assert client.put(f"/api/documents/{doc['id']}/pages/1", json={"revision": 1, "text": text + "出栈前要判空。"}).status_code == 200
    finally:
        ai.release.set()
    job = wait_job(client, job)
    assert job["status"] == "outdated", job
    assert not cards(client)


def test_running_background_job_locks_settings_and_cancellation(client):
    doc = import_text(client)
    ai = client.app.state.ai
    ai.entered, ai.release = Event(), Event()
    job = start(client, doc)
    try:
        assert ai.entered.wait(3)
        response = client.put("/api/settings/ai", json={"mode": "demo", "base_url": "https://offline.invalid/v1", "model": "offline-test", "timeout_seconds": 45})
        assert response.status_code == 409
        assert start(client, doc)["id"] == job["id"]
        assert client.post(f"/api/auto-card-jobs/{job['id']}/cancel", json={}).json()["status"] == "cancelled"
    finally:
        ai.release.set()
    assert wait_job(client, job)["status"] == "cancelled"
    assert not cards(client)


def test_edit_invalidates_quality_approval_only_preserves_binding(client):
    wait_job(client, start(client, import_text(client)))
    card = cards(client)[0]
    body = {key: card[key] for key in CardDraft.model_fields}
    response = client.put(f"/api/cards/{card['id']}", json={**body, "status": "approved", "expected_version": 1})
    assert response.status_code == 200
    assert response.json()["auto_quality"]["verdict"] == "clear"
    assert response.json()["auto_quality"]["card_version"] == 2
    response = client.put(f"/api/cards/{card['id']}", json={**body, "question": "补充解释栈的顺序？", "status": "draft", "expected_version": 2})
    assert response.status_code == 200
    assert response.json()["auto_quality"]["verdict"] == "stale"
    checked = client.post(f"/api/cards/{card['id']}/auto-quality-check", json={"expected_version": 3})
    assert checked.status_code == 200, checked.text
    assert checked.json()["auto_quality"]["verdict"] == "clear"
    assert checked.json()["auto_quality"]["card_version"] == 3
    assert checked.json()["status"] == "draft" and checked.json()["version"] == 3
    assert client.post(f"/api/cards/{card['id']}/auto-quality-check", json={"expected_version": 2}).status_code == 409


def test_recheck_result_cannot_overwrite_concurrent_edit(client):
    wait_job(client, start(client, import_text(client)))
    card = cards(client)[0]
    entered, release = Event(), Event()
    ai = client.app.state.ai
    original_review = ai.review_card

    async def slow_review(*args):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return await original_review(*args)

    ai.review_card = slow_review
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, f"/api/cards/{card['id']}/auto-quality-check", json={"expected_version": 1})
        try:
            assert entered.wait(3)
            body = {key: card[key] for key in CardDraft.model_fields}
            assert client.put(f"/api/cards/{card['id']}", json={**body, "question": "修改后的问题？", "expected_version": 1}).status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 409
    assert cards(client)[0]["auto_quality"]["verdict"] == "stale"


def test_exact_duplicates_are_not_saved_twice(client):
    doc = import_text(client, 18050)
    ai = client.app.state.ai
    original_generate = ai.generate_knowledge_cards

    async def same_question(*args):
        batch = await original_generate(*args)
        for item in batch.cards:
            item.card.question = "栈遵循什么顺序？"
            item.card.answer = "后进先出。"
        return batch

    ai.generate_knowledge_cards = same_question
    job = wait_job(client, start(client, doc))
    assert job["status"] == "completed"
    assert len(cards(client)) == 1 and job["skipped_count"] == 6
    assert job["knowledge_count"] == 7
    assert any(i["code"] == "duplicate_card" for i in job["issues"])


@pytest.mark.parametrize("mode,classification,expected", [("recommend", "diagram", "recommended"), ("checked", "diagram", "recommended"), ("checked", "text", "recommended")])
def test_image_preparation_never_checks_or_attaches_without_selection(client, mode, classification, expected):
    store = client.app.state.store
    text = "栈遵循后进先出，最后入栈的元素最先出栈。"
    doc = {"id": "pdf-" + "a" * 24, "title": "自编图文样本", "is_demo": False}
    store.put("documents", doc)
    store.put_many(page_records(doc["id"], {"page": 1, "text": text, "status": "reviewed", "reviewed_at": "now"}))

    class Images:
        checks = 0
        def get(self, job_id):
            return {"id": "figjob-" + "b" * 32, "status": "completed", "candidates": [{"id": "fig-" + "c" * 32, "kind": "figure", "quality_flags": [], "page": 1}]}
        async def start(self, *args):
            return self.get(None)
        async def check(self, *args):
            self.checks += 1
            return {"id": "check-test", "classification": {"label": classification}, "relation": "supported", "completeness": "complete", "legibility": "clear"}
        async def adopt(self, *args):
            return {"id": "img-" + "d" * 32}

    images = Images()
    client.app.state.auto_cards.recommendations = images
    client.app.state.auto_cards.image_checks = images
    job = wait_job(client, start(client, doc, image_mode=mode))
    assert job["status"] == "completed", job
    card = cards(client)[0]
    assert card["image_result"]["status"] == expected
    assert images.checks == 0
    assert card["status"] == "draft"
    if expected == "attached":
        assert card["version"] == card["auto_quality"]["card_version"] == 2
        assert card["images"][0]["side"] == "answer"
    else:
        assert not card.get("images")


def test_recovery_does_not_erase_completed_results(client):
    job = wait_job(client, start(client, import_text(client)))
    record = client.app.state.store.get("auto_card_jobs", job["id"])
    record["status"] = "running"
    client.app.state.store.put("auto_card_jobs", record)
    client.app.state.auto_cards.recover()
    recovered = client.get(f"/api/auto-card-jobs/{job['id']}").json()
    assert recovered["status"] == "interrupted" and recovered["retryable"]
    assert len(cards(client)) == 1


def test_batch_limits_and_no_source_rewrite():
    from ankisense.auto_cards import batch_blocks
    blocks = [SourceBlock(block_id=str(i), document_id="test", page=i + 1, text="原" * 3000) for i in range(7)]
    result = batch_blocks(blocks)
    assert [len(b) for b in result] == [3, 3, 1]
    assert [b.block_id for batch in result for b in batch] == [str(i) for i in range(7)]
    assert all(sum(len(b.text) for b in batch) <= 16000 for batch in result)
    from ankisense.auto_cards import context_blocks
    assert [b.block_id for b in context_blocks(blocks, result[1])] == ["2", "3", "4", "5", "6"]
    assert sum(len(b.text) for b in context_blocks(blocks, result[1])) <= 16000


def test_mode_validation_unknown_ids_and_invalid_options(client):
    assert client.post("/api/documents/missing/auto-card-jobs", json={}).status_code == 404
    assert client.get("/api/auto-card-jobs/missing").status_code == 404
    doc = import_text(client)
    assert client.post(f"/api/documents/{doc['id']}/auto-card-jobs", json={"max_cards": True}).status_code == 422
    assert client.post(f"/api/documents/{doc['id']}/auto-card-jobs", json={"max_cards": 61}).status_code == 422
    client.app.state.settings = Settings(mode="demo")
    assert client.post(f"/api/documents/{doc['id']}/auto-card-jobs", json={}).status_code == 422
