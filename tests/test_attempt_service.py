"""Offline contract tests for the shared web/Anki answer service."""

import asyncio

import pytest
from fastapi import HTTPException

from ankisense.attempts import ANALYSIS_FAILURE, AttemptService
from ankisense.db import Store
from ankisense.demo import seed_records
from ankisense.llm import AIError, AIService, Settings
from ankisense.prompts import DIAGNOSE_PROMPT_VERSION
from ankisense.schemas import Diagnosis


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "attempts.sqlite3")
    store.put_many(seed_records())
    card = next(card for card in store.all("cards") if card["question"] == "栈的出入顺序规则是什么？")
    card["status"] = "approved"
    store.put("cards", card)
    ai = AIService(Settings())
    service = AttemptService(store, Settings(), lambda: ai)
    return store, card, service


def test_blank_answer_is_saved_without_calling_ai(setup):
    store, card, service = setup

    def forbidden():
        pytest.fail("A blank answer must never invoke the AI adapter")

    service.ai_getter = forbidden
    attempt = asyncio.run(service.submit(card["id"], " \n\t", "blank-answer"))
    assert attempt["status"] == "unanswered"
    assert attempt["answer"] == " \n\t"
    assert attempt["diagnosis"] is None
    assert store.get("attempts", attempt["id"]) == attempt


def test_unsupported_partial_is_failed_not_shown_as_knowledge_feedback(setup):
    store, card, service = setup

    class UnsupportedAI:
        async def diagnose_answer(self, *args):
            return Diagnosis(verdict="incomplete", missing_point_ids=["stack_lifo"],
                             contradicted_point_ids=[], answer_spans=["你好"],
                             source_block_ids=["demo-block-1"], reason="没有矛盾所以不完整。")

    service.ai_getter = lambda: UnsupportedAI()
    attempt = asyncio.run(service.submit(card["id"], "你好", "unsupported-partial"))
    assert attempt["status"] == "failed"
    assert attempt["answer"] == "你好"
    assert attempt["diagnosis"] is None
    assert attempt["remediation"] is None
    assert attempt["prompt_version"] == DIAGNOSE_PROMPT_VERSION
    assert store.get("attempts", attempt["id"]) == attempt


def test_old_completed_diagnosis_is_read_and_replayed_without_rewriting(setup):
    store, card, service = setup
    original = asyncio.run(service.submit(card["id"], "你好", "legacy-partial"))
    assert original["diagnosis"]["verdict"] == "uncertain"
    legacy = {**original, "prompt_version": "v1", "diagnosis": {
        "verdict": "incomplete", "missing_point_ids": ["stack_lifo"], "contradicted_point_ids": [],
        "answer_spans": ["你好"], "source_block_ids": ["demo-block-1"], "reason": "过去的分析说明。"}}
    store.put("attempts", legacy)
    service.ai_getter = lambda: pytest.fail("Viewing or replaying history must not reanalyze it")
    assert service.get(legacy["id"]) == legacy
    assert asyncio.run(service.submit(card["id"], "你好", "legacy-partial")) == legacy
    assert store.get("attempts", legacy["id"]) == legacy


def test_plugin_requires_exact_version_and_replays_original_after_edit(setup):
    store, card, service = setup

    async def run():
        with pytest.raises(HTTPException) as absent:
            await service.submit(card["id"], "后进先出", "missing-version", origin="anki", client_id="client-one")
        assert absent.value.status_code == 422
        with pytest.raises(HTTPException) as stale:
            await service.submit(card["id"], "后进先出", "stale-version", expected_version=999,
                                 origin="anki", client_id="client-one")
        assert stale.value.status_code == 409
        assert store.all("attempts") == []
        args = dict(card_id=card["id"], answer="后进先出", request_id="plugin-request",
                    expected_version=card["version"], origin="anki", client_id="client-one")
        original = await service.submit(**args)
        assert original["origin"] == "anki"
        assert original["client_id"] == "client-one"
        assert original["diagnosis"]["verdict"] == "correct"
        store.put("cards", {**card, "version": card["version"] + 1, "status": "draft", "source_outdated": True})
        assert await service.submit(**args) == original
        assert service.get(original["id"], "anki", "client-one") == original
        for changed in ({"answer": "先进先出"}, {"expected_version": card["version"] + 1},
                        {"origin": "web"}, {"client_id": "client-two"}, {"card_id": "another-card"}):
            with pytest.raises(HTTPException) as collision:
                await service.submit(**{**args, **changed})
            assert collision.value.status_code == 409
        for scope in ({"expected_origin": "web"}, {"client_id": "client-two"}):
            with pytest.raises(HTTPException) as hidden:
                service.get(original["id"], **scope)
            assert hidden.value.status_code == 404
        assert len(store.all("attempts")) == 1

    asyncio.run(run())


def test_legacy_web_attempt_without_scope_metadata_can_be_replayed(setup):
    store, card, service = setup
    original = asyncio.run(service.submit(card["id"], "后进先出", "legacy-request"))
    original.pop("origin")
    original.pop("expected_version")
    store.put("attempts", original)
    assert asyncio.run(service.submit(card["id"], "后进先出", "legacy-request")) == original


def test_concurrent_submissions_share_one_analysis(setup):
    store, card, service = setup

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        delegate = AIService(Settings())

        class WaitingAI:
            calls = 0

            async def diagnose_answer(self, *args):
                self.calls += 1
                entered.set()
                await release.wait()
                return await delegate.diagnose_answer(*args)

        ai = WaitingAI()
        service.ai_getter = lambda: ai
        submissions = [asyncio.create_task(service.submit(card["id"], "后进先出", "concurrent-request"))
                       for _ in range(8)]
        await asyncio.wait_for(entered.wait(), 2)
        assert len(store.all("attempts")) == 1
        assert store.all("attempts")[0]["status"] == "pending"
        release.set()
        results = await asyncio.gather(*submissions)
        assert all(result == results[0] for result in results)
        assert results[0]["status"] == "completed"
        assert ai.calls == 1
        assert not service._locks
        assert not service._tasks

    asyncio.run(run())


def test_second_service_reuses_pending_attempt_without_starting_ai(setup):
    store, card, service = setup

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        delegate = AIService(Settings())

        class WaitingAI:
            async def diagnose_answer(self, *args):
                entered.set()
                await release.wait()
                return await delegate.diagnose_answer(*args)

        service.ai_getter = WaitingAI
        first = asyncio.create_task(service.submit(card["id"], "后进先出", "shared-request"))
        await asyncio.wait_for(entered.wait(), 2)
        second_service = AttemptService(Store(store.path), Settings(), lambda: pytest.fail("Duplicate analysis"))
        pending = await second_service.submit(card["id"], "后进先出", "shared-request")
        assert pending["status"] == "pending"
        assert len(store.all("attempts")) == 1
        release.set()
        completed = await first
        assert pending["id"] == completed["id"]
        assert await second_service.submit(card["id"], "后进先出", "shared-request") == completed

    asyncio.run(run())


def test_disconnected_caller_does_not_cancel_saved_analysis(setup):
    store, card, service = setup

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        delegate = AIService(Settings())

        class WaitingAI:
            calls = 0

            async def diagnose_answer(self, *args):
                self.calls += 1
                entered.set()
                await release.wait()
                return await delegate.diagnose_answer(*args)

        ai = WaitingAI()
        service.ai_getter = lambda: ai
        caller = asyncio.create_task(service.submit(card["id"], "后进先出", "disconnect-request"))
        await asyncio.wait_for(entered.wait(), 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert store.all("attempts")[0]["status"] == "pending"
        replay = asyncio.create_task(service.submit(card["id"], "后进先出", "disconnect-request"))
        release.set()
        assert (await replay)["status"] == "completed"
        assert ai.calls == 1

    asyncio.run(run())


def test_retry_uses_original_snapshots_and_current_ai_getter(setup):
    store, card, service = setup

    class BrokenAI:
        async def diagnose_answer(self, *args):
            raise ValueError("provider body contains secret key and submitted answer")

    async def run():
        service.ai_getter = BrokenAI
        failed = await service.submit(card["id"], "后进先出", "failed-request",
                                      expected_version=card["version"], origin="anki", client_id="client-one")
        assert failed["status"] == "failed"
        assert failed["error"] == ANALYSIS_FAILURE
        assert failed["diagnosis"] is None
        for scope in ({"expected_origin": "web"}, {"client_id": "client-two"}):
            with pytest.raises(HTTPException) as hidden:
                await service.retry(failed["id"], **scope)
            assert hidden.value.status_code == 404
        assert service.get(failed["id"])["retry_count"] == 0
        store.put("cards", {**card, "version": card["version"] + 1, "question": "修改后的题目", "status": "draft", "source_outdated": True})
        block = failed["source_snapshot"][0]
        store.put("blocks", {**block, "text": "修改后的原文"})
        ai = AIService(Settings())
        service.ai_getter = lambda: ai
        retried = await service.retry(failed["id"], "anki", "client-one")
        assert retried["status"] == "completed"
        assert retried["diagnosis"]["verdict"] == "correct"
        assert retried["card_snapshot"] == failed["card_snapshot"]
        assert retried["source_snapshot"] == failed["source_snapshot"]
        assert retried["retry_count"] == 1
        assert retried["analysis_history"][0]["error"] == ANALYSIS_FAILURE

    asyncio.run(run())


def test_failed_attempt_allows_only_two_retries(setup):
    _, card, service = setup

    class BrokenAI:
        calls = 0

        async def diagnose_answer(self, *args):
            self.calls += 1
            raise AIError("模型调用超时，请稍后重试。")

    async def run():
        ai = BrokenAI()
        service.ai_getter = lambda: ai
        failed = await service.submit(card["id"], "后进先出", "retry-limit-request")
        for retry_count in (1, 2):
            failed = await service.retry(failed["id"])
            assert failed["status"] == "failed"
            assert failed["retry_count"] == retry_count
        with pytest.raises(HTTPException) as limit:
            await service.retry(failed["id"])
        assert limit.value.status_code == 409
        assert ai.calls == 3
        assert len(failed["analysis_history"]) == 2

    asyncio.run(run())


def test_concurrent_retries_of_same_failure_start_only_one_additional_analysis(setup):
    _, card, service = setup

    class BrokenAI:
        calls = 0

        async def diagnose_answer(self, *args):
            self.calls += 1
            raise AIError("模型调用超时，请稍后重试。")

    async def run():
        ai = BrokenAI()
        service.ai_getter = lambda: ai
        failed = await service.submit(card["id"], "后进先出", "retry-conflict-request")
        results = await asyncio.gather(
            service.retry(failed["id"], expected_retry_count=0),
            service.retry(failed["id"], expected_retry_count=0), return_exceptions=True)
        attempts = [result for result in results if isinstance(result, dict)]
        conflicts = [result for result in results if isinstance(result, HTTPException)]
        assert len(attempts) == len(conflicts) == 1
        assert attempts[0]["status"] == "failed"
        assert attempts[0]["retry_count"] == 1
        assert conflicts[0].status_code == 409
        assert ai.calls == 2
        assert service.get(failed["id"])["retry_count"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("count", [-1, 3, True, 0.0, "0"])
def test_retry_expected_count_is_strict_and_checked_before_analysis(setup, count):
    store, _, service = setup
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(service.retry("unneeded-id", expected_retry_count=count))
    assert invalid.value.status_code == 422
    assert store.all("attempts") == []


@pytest.mark.parametrize("condition", ["draft", "unreviewed", "stale-source", "source-outdated"])
def test_only_approved_cards_with_current_reviewed_sources_are_saved(setup, condition):
    store, card, service = setup
    block = store.get("blocks", card["evidence"][0]["block_id"])
    if condition == "draft":
        store.put("cards", {**card, "status": "draft"})
    else:
        if condition == "source-outdated":
            store.put("cards", {**card, "source_outdated": True})
        store.put("source_pages", {
            "id": f"{block['document_id']}-p{block['page']}",
            "document_id": block["document_id"], "page": block["page"],
            "status": "pending" if condition == "unreviewed" else "reviewed",
            "block_ids": ["different-revision"] if condition == "stale-source" else [block["block_id"]],
        })
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(service.submit(card["id"], "后进先出", "unreviewed-request"))
    assert invalid.value.status_code == 409
    assert store.all("attempts") == []


@pytest.mark.parametrize("condition", ["cross-document", "invalid-quote", "duplicate-rubric"])
def test_invalid_card_sources_are_rejected_before_saving_or_calling_ai(setup, condition):
    store, card, service = setup
    if condition == "cross-document":
        block = store.get("blocks", card["evidence"][0]["block_id"])
        store.put("blocks", {**block, "document_id": "another-document"})
    elif condition == "invalid-quote":
        card["evidence"][0]["quote"] = "fabricated source content"
        store.put("cards", card)
    else:
        card["rubric"].append(dict(card["rubric"][0]))
        store.put("cards", card)
    service.ai_getter = lambda: pytest.fail("Invalid cards must not invoke AI")
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(service.submit(card["id"], "后进先出", "invalid-source-request"))
    assert invalid.value.status_code == 422
    assert "fabricated source content" not in invalid.value.detail
    assert store.all("attempts") == []


@pytest.mark.parametrize("condition", ["source-outdated", "unreviewed", "stale-source", "invalid-quote", "cross-document"])
def test_remediation_skips_invalid_sources_and_uses_valid_followup(setup, condition):
    store, card, service = setup
    candidate = store.get("cards", "demo-stack-practice")
    candidate["status"] = "approved"
    fallback = {**candidate, "id": "valid-stack-followup"}
    original_block = store.get("blocks", card["evidence"][0]["block_id"])
    block = {**original_block, "block_id": "followup-block", "page": 4}
    candidate["evidence"] = [{"block_id": block["block_id"], "quote": block["text"]}]
    if condition == "source-outdated":
        candidate["source_outdated"] = True
    elif condition == "invalid-quote":
        candidate["evidence"][0]["quote"] = "fabricated follow-up source content"
    elif condition == "cross-document":
        block["document_id"] = "another-document"
    store.put("blocks", block)
    store.put("source_pages", {
        "id": f"{block['document_id']}-p4", "document_id": block["document_id"], "page": 4,
        "status": "pending" if condition == "unreviewed" else "reviewed",
        "block_ids": ["new-source-version"] if condition == "stale-source" else [block["block_id"]],
    })
    store.put_many([("cards", candidate), ("cards", fallback)])
    attempt = asyncio.run(service.submit(card["id"], "先进先出", "remediation-request"))
    assert attempt["status"] == "completed"
    assert attempt["diagnosis"]["verdict"] == "incorrect"
    assert attempt["remediation"]["id"] == fallback["id"]


@pytest.mark.parametrize("already_started", [False, True])
def test_shutdown_preserves_pending_answer_as_retryable_failure(setup, already_started):
    store, card, service = setup

    async def run():
        entered = asyncio.Event()

        class WaitingAI:
            async def diagnose_answer(self, *args):
                entered.set()
                await asyncio.Event().wait()

        service.ai_getter = WaitingAI
        caller = asyncio.create_task(service.submit(card["id"], "后进先出", "shutdown-request"))
        if already_started:
            await asyncio.wait_for(entered.wait(), 2)
        else:
            # Let submit persist and schedule analysis, then stop before that
            # newly scheduled analysis task gets an event-loop turn.
            await asyncio.sleep(0)
        await service.shutdown()
        with pytest.raises(asyncio.CancelledError):
            await caller
        saved = store.all("attempts")[0]
        assert saved["status"] == "failed"
        assert saved["answer"] == "后进先出"
        assert saved["diagnosis"] is None
        assert not service._tasks
        with pytest.raises(HTTPException) as closed:
            await service.submit(card["id"], "后进先出", "after-shutdown")
        assert closed.value.status_code == 503

    asyncio.run(run())
