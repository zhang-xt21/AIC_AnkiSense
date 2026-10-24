"""Background drafting lifecycle, using local plans and model doubles only."""
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ankisense.db import Store
from ankisense.generation_jobs import GenerationJobService, GenerationRequest
from ankisense.llm import AIError, AIService, Settings
from ankisense.schemas import CardDraft, SourceBlock
from ankisense.sources import page_records, save_page_revision


def draft(block, label):
    return CardDraft(question=f"这段讲义说明什么？{label}", answer=block.text[:80], concept="概念",
                     rubric=[{"point_id": "point", "text": block.text[:80]}],
                     evidence=[{"block_id": block.block_id, "quote": block.text[:80]}])


class FakeAI:
    def __init__(self):
        self.calls = []
        self.handler = None

    async def generate_drafts(self, blocks, count, **options):
        self.calls.append((blocks, count, options))
        if self.handler:
            return await self.handler(blocks, count, len(self.calls))
        return SimpleNamespace(cards=[draft(blocks[0], f"{len(self.calls)}-{index}") for index in range(count)])


def local_plan(store, document_id, block_ids=None, count=None):
    """Predictable batching lets these tests focus on task races and persistence."""
    pages = [item for item in store.all("source_pages") if item["document_id"] == document_id]
    if any(page["status"] not in {"pending", "reviewed"} for page in pages):
        raise HTTPException(409, "来源失败")
    current_ids = {identifier for page in pages for identifier in page["block_ids"]}
    chosen = set(block_ids) if block_ids else current_ids
    if not chosen <= current_ids:
        raise HTTPException(409, "来源过期")
    blocks = [item for item in store.all("blocks") if item["block_id"] in chosen]
    fingerprint = hashlib.sha256(json.dumps({"blocks": blocks, "revisions": [page["revision"] for page in pages]},
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    count = count or 3
    batches = [{"index": index + 1, "count": min(6, count - offset), "blocks": blocks,
                "pages": [1], "chars": sum(len(block["text"]) for block in blocks)}
               for index, offset in enumerate(range(0, count, 6))]
    return {"document_id": document_id, "scope": "selected" if block_ids else "all",
            "source_fingerprint": fingerprint, "recommended_count": 3, "target_count": count,
            "warnings": [], "batches": batches}


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = Store(tmp_path / "jobs.sqlite3")
    def add_document(identifier="doc"):
        store.put("documents", {"id": identifier, "is_demo": False, "filename": "本地测试讲义.pdf"})
        store.put_many(page_records(identifier, {"page": 1, "text": "栈在同一端插入和删除元素，遵循后进先出。", "status": "pending"}))
    add_document()
    monkeypatch.setattr("ankisense.generation_jobs.build_plan", local_plan)
    monkeypatch.setattr("ankisense.generation_jobs.public_plan", lambda plan: {key: value for key, value in plan.items() if key != "batches"})
    settings = [Settings(mode="cloud", base_url="https://model.invalid", model="fake", api_key="test-only")]
    ai, reviewed = FakeAI(), []
    async def review(card):
        reviewed.append(card["id"])
        return card
    service = GenerationJobService(store, lambda: settings[0], lambda: ai, review)
    @asynccontextmanager
    async def lifespan(app):
        await service.recover()
        yield
        await service.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.include_router(service.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, ai=ai, service=service,
                              reviewed=reviewed, settings=settings, add_document=add_document)


def request(context, *, document_id="doc", count=8, request_id="request-one", **changes):
    plan = context.client.post(f"/api/documents/{document_id}/generation-plan", json={"count": count}).json()
    return {"document_id": document_id, "count": count, "request_id": request_id,
            "source_fingerprint": plan["source_fingerprint"], **changes}


def start(context, **values):
    response = context.client.post("/api/generation-jobs", json=request(context, **values))
    assert response.status_code == 200, response.text
    return response.json()


def finish(context, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = context.client.get(f"/api/generation-jobs/{job['id']}").json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.01)
    raise AssertionError("local model double did not finish")


async def gate_wait(gate):
    while not gate.is_set():
        await asyncio.sleep(.005)


def test_start_returns_without_waiting_and_commits_batches_before_review(context):
    gate, entered = Event(), Event()
    async def held(blocks, count, number):
        entered.set()
        await gate_wait(gate)
        return SimpleNamespace(cards=[draft(blocks[0], f"{number}-{index}") for index in range(count)])
    context.ai.handler = held
    try:
        job = start(context)
        assert job["status"] == "queued" and job["generated_count"] == 0
        assert entered.wait(1)
        assert context.store.all("cards") == []
    finally:
        gate.set()
    result = finish(context, job)
    assert result["status"] == "completed"
    assert result["generated_count"] == 8 and result["completed_batches"] == 2
    assert result["card_ids"] == context.reviewed
    assert len(context.ai.calls) == 2
    assert context.ai.calls[0][2] == {"allow_fewer": True, "avoid_questions": []}
    assert len(context.ai.calls[1][2]["avoid_questions"]) == 6
    assert all(card["status"] == "draft" for card in context.store.all("cards"))


def test_idempotency_replays_finished_task_without_new_calls(context):
    body = request(context)
    first = context.client.post("/api/generation-jobs", json=body).json()
    done = finish(context, first)
    again = context.client.post("/api/generation-jobs", json=body)
    assert again.status_code == 200 and again.json() == done
    assert len(context.ai.calls) == 2 and len(context.store.all("cards")) == 8
    conflict = context.client.post("/api/generation-jobs", json={**body, "count": 9})
    assert conflict.status_code == 409


def test_image_preference_is_persisted_and_part_of_request_identity(context):
    body = request(context, count=1, auto_images=False)
    first = context.client.post("/api/generation-jobs", json=body).json()
    done = finish(context, first)
    assert done["auto_images"] is False
    assert context.client.post("/api/generation-jobs", json=body).json() == done
    changed = context.client.post("/api/generation-jobs", json={**body, "auto_images": True})
    assert changed.status_code == 409 and len(context.ai.calls) == 1


@pytest.mark.parametrize("value", ["false", 1, None])
def test_image_preference_requires_a_boolean(context, value):
    response = context.client.post("/api/generation-jobs", json=request(context, auto_images=value))
    assert response.status_code == 422 and not context.ai.calls


def test_image_intent_is_saved_with_its_cited_sources(context):
    async def illustrated(blocks, count, number):
        item = draft(blocks[0], "图示")
        return SimpleNamespace(cards=[{**item.model_dump(), "image_intent": {
            "need": "helpful", "reason": "辅助理解栈的结构", "block_ids": [blocks[0].block_id]}}])
    context.ai.handler = illustrated
    result = finish(context, start(context, count=1))
    assert result["status"] == "completed" and result["auto_images"] is True
    card = context.store.get("cards", result["card_ids"][0])
    assert card["image_intent"]["need"] == "helpful" and context.reviewed == [card["id"]]


def test_replay_active_task_and_per_document_global_limits(context):
    gate = Event()
    async def held(blocks, count, number):
        await gate_wait(gate)
        return SimpleNamespace(cards=[])
    context.ai.handler = held
    try:
        body = request(context)
        job = context.client.post("/api/generation-jobs", json=body).json()
        replay = context.client.post("/api/generation-jobs", json=body)
        assert replay.status_code == 200 and replay.json()["id"] == job["id"]
        duplicate = context.client.post("/api/generation-jobs", json={**body, "request_id": "another-id"})
        assert duplicate.status_code == 409
        context.add_document("second")
        start(context, document_id="second", request_id="second-request")
        context.add_document("third")
        full = context.client.post("/api/generation-jobs", json=request(context, document_id="third", request_id="third-request"))
        assert full.status_code == 429
    finally:
        gate.set()


@pytest.mark.parametrize("settings", [Settings(), Settings(mode="cloud", model="missing-key", base_url="https://model.invalid")])
def test_unconfigured_model_can_plan_but_cannot_start(context, settings):
    context.settings[0] = settings
    assert context.client.post("/api/documents/doc/generation-plan", json={}).status_code == 200
    result = context.client.post("/api/generation-jobs", json=request(context))
    assert result.status_code == 422 and not context.ai.calls


def test_demo_does_not_call_cloud(context):
    context.store.put("documents", {"id": "doc", "is_demo": True})
    result = context.client.post("/api/generation-jobs", json=request(context))
    assert result.status_code == 422 and not context.ai.calls


def test_changed_source_between_plan_and_start_is_rejected(context):
    body = request(context)
    save_page_revision(context.store, "doc", 1, 1, {"text": "新版本讲义内容。", "status": "reviewed"}, "proofread")
    result = context.client.post("/api/generation-jobs", json=body)
    assert result.status_code == 409 and context.store.all("generation_jobs") == [] and not context.ai.calls


def test_source_changed_during_model_call_discards_whole_batch(context):
    async def changed(blocks, count, number):
        save_page_revision(context.store, "doc", 1, 1, {"text": "内容已修改。", "status": "reviewed"}, "proofread")
        return SimpleNamespace(cards=[draft(blocks[0], "旧来源")])
    context.ai.handler = changed
    result = finish(context, start(context))
    assert result["status"] == "failed" and "更新" in result["error"]
    assert context.store.all("cards") == [] and len(context.ai.calls) == 1


def test_source_change_after_saved_batch_stops_next_and_keeps_prior_cards(context):
    async def review(card):
        if context.store.get("source_pages", "doc-p1")["revision"] == 1:
            save_page_revision(context.store, "doc", 1, 1, {"text": "内容已修改。", "status": "reviewed"}, "proofread")
    context.service.review_saved_card = review
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 6
    assert len(context.ai.calls) == 1
    assert all(card["source_outdated"] for card in context.store.all("cards"))


def test_unchanged_human_confirmation_keeps_task_current(context):
    async def review(card):
        page = context.store.get("source_pages", "doc-p1")
        save_page_revision(context.store, "doc", 1, 1, {"text": page["text"], "status": "reviewed"}, "proofread")
    context.service.review_saved_card = review
    result = finish(context, start(context))
    assert result["status"] == "completed" and len(context.ai.calls) == 2


def test_later_model_failure_preserves_prior_batch_without_retry(context):
    async def fail_second(blocks, count, number):
        if number == 2:
            raise AIError("模型请求超时")
        return SimpleNamespace(cards=[draft(blocks[0], str(index)) for index in range(count)])
    context.ai.handler = fail_second
    result = finish(context, start(context, count=15))
    assert result["status"] == "partial" and result["generated_count"] == 6
    assert result["completed_batches"] == 1 and "超时" in result["error"]
    assert len(context.ai.calls) == 2


def test_duplicate_and_fewer_cards_are_partial_without_filling(context):
    async def fewer(blocks, count, number):
        return SimpleNamespace(cards=[draft(blocks[0], "相同"), draft(blocks[0], f"独立{number}")])
    context.ai.handler = fewer
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 3
    assert any("重复" in warning for warning in result["warnings"])
    assert any("实际保存" in warning for warning in result["warnings"])
    assert len(context.ai.calls) == 2


def test_empty_batch_continues_remaining_source(context):
    async def empty(blocks, count, number):
        return SimpleNamespace(cards=[] if number == 1 else [draft(blocks[0], "第二批")])
    context.ai.handler = empty
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 1
    assert result["completed_batches"] == 2 and len(context.ai.calls) == 2


def test_stop_allows_current_request_to_finish_but_no_later_batches(context):
    gate, entered = Event(), Event()
    async def held(blocks, count, number):
        entered.set()
        await gate_wait(gate)
        return SimpleNamespace(cards=[draft(blocks[0], str(index)) for index in range(count)])
    context.ai.handler = held
    try:
        job = start(context, count=15)
        assert entered.wait(1)
        stopped = context.client.post(f"/api/generation-jobs/{job['id']}/stop").json()
        assert stopped["stop_requested"] is True
    finally:
        gate.set()
    result = finish(context, job)
    assert result["status"] == "stopped" and result["generated_count"] == 6
    assert len(context.ai.calls) == 1 and len(context.reviewed) == 6
    assert context.client.post(f"/api/generation-jobs/{job['id']}/stop").json() == result


def test_shutdown_finishes_current_batch_without_starting_more_model_calls(context):
    gate, entered = Event(), Event()
    async def held(blocks, count, number):
        entered.set()
        await gate_wait(gate)
        return SimpleNamespace(cards=[draft(blocks[0], str(index)) for index in range(count)])
    context.ai.handler = held
    try:
        job = start(context, count=15)
        assert entered.wait(1)
        shutdown = context.client.portal.start_task_soon(context.service.shutdown)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not context.service.closing:
            time.sleep(.005)
        assert context.service.closing and not shutdown.done()
    finally:
        gate.set()
    shutdown.result(timeout=2)
    result = finish(context, job)
    assert result["status"] == "stopped" and result["generated_count"] == 6
    assert len(context.ai.calls) == 1


def test_cancel_before_worker_starts_does_not_leave_a_permanently_active_job(context):
    body = GenerationRequest.model_validate(request(context))
    async def cancel_immediately():
        job = await context.service.start(body)
        task = context.service.running[job["id"]]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return job
    job = context.client.portal.call(cancel_immediately)
    result = finish(context, job)
    assert result["status"] == "interrupted" and result["generated_count"] == 0
    assert not context.ai.calls and not context.service.running
    # The same request remains idempotent; a new explicit request can proceed.
    assert context.client.post("/api/generation-jobs", json=body.model_dump()).json() == result
    assert finish(context, start(context, request_id="new-after-cancel"))["status"] == "completed"


def test_invalid_quote_isolated_from_valid_cards_and_later_batches(context):
    async def invalid(blocks, count, number):
        bad = draft(blocks[0], "无依据")
        bad.evidence[0].quote = "原文没有这句话"
        if number == 1:
            return SimpleNamespace(cards=[draft(blocks[0], f"有效{index}") for index in range(count - 1)] + [bad])
        return SimpleNamespace(cards=[draft(blocks[0], f"后续有效{index}") for index in range(count)])
    context.ai.handler = invalid
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 7
    assert result["completed_batches"] == 2 and len(context.ai.calls) == 2
    assert len(context.store.all("cards")) == 7 and result["card_ids"] == context.reviewed
    assert result["rejected_count"] == 1
    rejected = result["rejected_cards"][0]
    assert rejected["batch"] == 1 and rejected["index"] == 6
    assert "连续原文" in rejected["reason"] and "原文没有这句话" in rejected["draft_preview"]
    assert not any(card["question"].endswith("无依据") for card in context.store.all("cards"))


def test_bad_card_schema_is_also_isolated_and_rejections_replay_without_calls(context):
    async def malformed(blocks, count, number):
        valid = draft(blocks[0], "有效")
        return SimpleNamespace(cards=[{"question": "字段缺失的候选"}, valid])
    context.ai.handler = malformed
    body = request(context, count=2)
    result = finish(context, context.client.post("/api/generation-jobs", json=body).json())
    assert result["status"] == "partial" and result["generated_count"] == 1
    assert result["rejected_cards"][0]["question"] == "字段缺失的候选"
    assert context.client.post("/api/generation-jobs", json=body).json() == result
    assert context.client.get(f"/api/generation-jobs/{result['id']}").json() == result
    assert len(context.ai.calls) == 1 and len(context.reviewed) == 1


def test_entirely_invalid_batch_does_not_block_remaining_batches(context):
    async def invalid_first(blocks, count, number):
        if number == 1:
            return SimpleNamespace(cards=[{"question": f"坏候选{index}"} for index in range(count)])
        return SimpleNamespace(cards=[draft(blocks[0], f"有效{index}") for index in range(count)])
    context.ai.handler = invalid_first
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 2
    assert result["rejected_count"] == 6 and result["completed_batches"] == 2
    assert len(context.ai.calls) == 2 and len(context.reviewed) == 2


def test_all_invalid_candidates_fail_with_readable_records_not_learning_cards(context):
    async def invalid(blocks, count, number):
        return SimpleNamespace(cards=[None, {"question": "坏候选", "answer": "长" * 20000}])
    context.ai.handler = invalid
    result = finish(context, start(context, count=2))
    assert result["status"] == "failed" and result["generated_count"] == 0
    assert result["rejected_count"] == 2 and "逐卡校验" in result["error"]
    assert len(result["rejected_cards"][1]["draft_preview"]) == 12000
    assert result["rejected_cards"][1]["preview_truncated"] is True
    assert context.store.all("cards") == [] and not context.reviewed


def test_parser_rejections_reach_saved_job_and_valid_cards_are_queued(context, monkeypatch):
    service, calls = AIService(context.settings[0]), []
    async def response(instruction, payload, schema):
        calls.append(payload)
        block = SourceBlock.model_validate(payload["source_blocks"][0])
        raw = [draft(block, f"{len(calls)}-{i}").model_dump() for i in range(payload["count"])]
        if len(calls) == 1:
            raw[-1]["evidence"][0]["quote"] = "拼接出来而非原文的引文"
        return {"cards": raw}
    monkeypatch.setattr(service, "_request", response)
    context.service.ai_provider = lambda: service
    result = finish(context, start(context))
    assert result["status"] == "partial" and result["generated_count"] == 7
    assert result["rejected_count"] == 1 and result["rejected_cards"][0]["index"] == 6
    assert result["rejected_cards"][0]["batch"] == 1
    assert len(calls) == 2 and result["card_ids"] == context.reviewed
    assert len(calls[1]["avoid_questions"]) == 5


def test_model_overproduction_is_not_silently_saved(context):
    async def excessive(blocks, count, number):
        return SimpleNamespace(cards=[draft(blocks[0], str(index)) for index in range(count + 1)])
    context.ai.handler = excessive
    result = finish(context, start(context, count=1))
    assert result["status"] == "failed" and result["generated_count"] == 0
    assert "超过" in result["error"] and not context.store.all("cards")


def test_review_queue_failure_does_not_lose_saved_cards(context):
    async def broken_review(card):
        raise RuntimeError("local queue failure")
    context.service.review_saved_card = broken_review
    result = finish(context, start(context, count=2))
    assert result["status"] == "completed" and result["generated_count"] == 2
    assert len(result["warnings"]) == 1 and "检查未成功排队" in result["warnings"][0]


def test_recovery_marks_interrupted_without_implicit_model_calls(context):
    body = request(context, count=2)
    done = finish(context, context.client.post("/api/generation-jobs", json=body).json())
    old = context.store.get("generation_jobs", done["id"])
    old["status"] = "running"
    context.store.put("generation_jobs", old)
    context.client.portal.call(context.service.recover)
    interrupted = context.client.get(f"/api/generation-jobs/{done['id']}").json()
    assert interrupted["status"] == "interrupted" and interrupted["generated_count"] == 2
    assert context.client.post("/api/generation-jobs", json=body).json() == interrupted
    assert len(context.ai.calls) == 1


def test_read_only_status_and_state_never_expose_plan_text_or_start_calls(context):
    result = finish(context, start(context, count=1))
    calls = len(context.ai.calls)
    public = context.client.get(f"/api/generation-jobs/{result['id']}").json()
    assert public == context.service.list_jobs()[0]
    assert "block_ids" not in public and "request_signature" not in public
    assert "栈在同一端" not in json.dumps(public, ensure_ascii=False)
    assert len(context.ai.calls) == calls
    assert context.client.get("/api/generation-jobs/missing").status_code == 404


@pytest.mark.parametrize("count", [True, 1.5, "3", 0, 31])
def test_count_validation_is_strict(context, count):
    body = request(context)
    response = context.client.post("/api/generation-jobs", json={**body, "count": count})
    assert response.status_code == 422 and not context.ai.calls


def test_real_planner_windows_keep_original_evidence_and_batch_limits(context, monkeypatch):
    from ankisense.generation_plan import build_plan, public_plan
    monkeypatch.setattr("ankisense.generation_jobs.build_plan", build_plan)
    monkeypatch.setattr("ankisense.generation_jobs.public_plan", public_plan)
    text = "栈遵循后进先出。队列遵循先进先出。" * 1500
    context.store.put("blocks", {"block_id": "long-block", "document_id": "doc", "page": 1, "text": text})
    page = context.store.get("source_pages", "doc-p1")
    context.store.put("source_pages", {**page, "text": text, "block_ids": ["long-block"]})
    preview = context.client.post("/api/documents/doc/generation-plan", json={}).json()
    assert preview["recommended_count"] >= 1 and not context.ai.calls
    assert all("blocks" not in batch for batch in preview["batches"])
    result = finish(context, start(context, count=30))
    assert result["status"] == "completed" and result["generated_count"] == 30
    for blocks, count, _options in context.ai.calls:
        assert 1 <= count <= 6 and 1 <= len(blocks) <= 6
        assert sum(len(block.text) for block in blocks) <= 16000
        assert all(block.block_id == "long-block" and block.text in text for block in blocks)
    assert all(card["evidence"][0]["block_id"] == "long-block" for card in context.store.all("cards"))
