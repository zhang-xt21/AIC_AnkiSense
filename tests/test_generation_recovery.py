"""Local quote recovery and candidate-scoped retries; no real provider calls."""
import asyncio
import json

import pytest

from ankisense.auto_card_models import KnowledgePoint
from ankisense.draft_validation import (KnowledgeSelection, rejected_draft,
                                       select_knowledge_drafts, select_knowledge_points)
from ankisense.evidence import validate_card
from ankisense.llm import AIError, AIService, Settings
from ankisense.schemas import CardDraft, SourceBlock
from ankisense.source_quotes import align_model_quotes, source_quote
from ankisense.job_progress import summarize_batches
from test_automatic_import import client, upload, wait_import


def block(text):
    return SourceBlock(block_id="doc-p1-b1-r1", document_id="doc", page=1, text=text)


def point(source, identifier="good", quote=None):
    return {"knowledge_id": identifier, "concept": "来源事实", "statement": source.text,
            "conditions": [], "evidence": [{"block_id": source.block_id, "quote": quote or source.text}]}


@pytest.mark.parametrize("source,quote", [
    ("社会主义革命任务（3个方面的表现）：", "社会主义革命任务(3个方面的表现)："),
    ("中国共\n产党团结带领全国人民继续奋斗。", "中国共产党团结带领全国人民继续奋斗。"),
    ("其次，新文化运动掀起的思想解放的潮流。", "其次，新\n文化运动掀起的思想解放的潮流。"),
    ("the last\n element is first", "the last element is first"),
])
def test_layout_alignment_restores_actual_source_without_changing_inputs(source, quote):
    b = block(source)
    raw = {"question": "说明了什么？", "answer": "来源事实。", "concept": "事实",
           "rubric": [{"point_id": "fact", "text": "来源事实。"}], "evidence": [{"block_id": b.block_id, "quote": quote}]}
    aligned = align_model_quotes(raw, [b])
    assert raw["evidence"][0]["quote"] == quote
    assert aligned["evidence"][0]["quote"] == source
    validate_card(CardDraft.model_validate(aligned), [b])


@pytest.mark.parametrize("source,quote", [
    ("应当先判空，再出栈。", "应当判空，再出栈。"),
    ("空栈不能出栈。", "空栈能出栈。"),
    ("共 1949 个。", "共 1948 个。"),
    ("a b", "ab"),
    ("x + +y", "x ++y"),
    ("值为 1.2。", "值为 1,2。"),
    ("中\n国。另一个：中国。", "中 国。"),
])
def test_alignment_does_not_guess_changed_facts_or_ambiguous_spans(source, quote):
    assert source_quote(quote, source) == quote
    assert quote not in source


def test_one_bad_knowledge_point_keeps_its_valid_neighbor_and_specific_reason():
    b = block("栈遵循后进先出。")
    selected = select_knowledge_points([point(b), point(b, "bad", "栈遵循先进先出。")], [b], [b.block_id])
    assert [p.knowledge_id for p in selected.points] == ["good"]
    assert len(selected.rejected_points) == 1
    assert selected.rejected_points[0]["knowledge_id"] == "bad"
    assert "连续原文" in selected.rejected_points[0]["reason"]


def test_repair_id_cannot_collide_with_a_valid_neighbor():
    b = block("栈遵循后进先出。")
    bad = point(b)
    bad["knowledge_id"] = None
    selected = select_knowledge_points([bad, point(b, "repair-1")], [b], [b.block_id])
    assert selected.points[0].knowledge_id == "repair-1"
    assert selected.rejected_points[0]["knowledge_id"] != "repair-1"


def test_batch_summary_uses_live_checkpoints_instead_of_historical_errors():
    job = {"progress": {"completed": 3, "total": 4},
           "batches": [{"batch_id": "fixed", "status": "completed"},
                       {"batch_id": "bad", "status": "failed", "pages": [4, 5, 6]},
                       {"batch_id": "partial", "status": "partial", "pages": [7],
                        "knowledge": [point(block("事实。"))], "completed_knowledge_ids": []}],
           "issues": [{"code": "batch_failed", "batch_id": "fixed", "reason": "historical"},
                      {"code": "batch_failed", "batch_id": "bad", "reason": "current"}]}
    summary, details = summarize_batches(job)
    assert summary == {"total": 4, "successful": 1, "failed": 1, "partial": 1, "running": 0,
                       "pending": 1, "attempted": 3, "remaining": 3, "pending_points": 1}
    assert len(details) == 2 and details[0]["reasons"] == ["current"]


def test_stopped_request_and_unstarted_plan_are_pending_not_running():
    job = {"status": "cancelled", "progress": {"completed": 0, "total": 2},
           "batches": [{"status": "running", "block_ids": ["one"], "pages": [1]}],
           "plan": {"batches": [{"block_ids": ["one"], "pages": [1]}, {"block_ids": ["two"], "pages": [2]}]}}
    summary, details = summarize_batches(job)
    assert summary["running"] == 0 and summary["pending"] == 2
    assert len(details) == 2 and {x["status"] for x in details} == {"pending"}


def test_repair_does_not_accept_other_knowledge_ids_or_drop_missing_target():
    b = block("栈遵循后进先出。")
    selected = select_knowledge_points([point(b, "already-done")], [b], [b.block_id], ["failed-one"])
    assert not selected.points
    assert [x["knowledge_id"] for x in selected.rejected_points] == ["failed-one"]


def test_model_receives_only_failed_knowledge_and_validation_feedback():
    b = block("新\n文化运动推动思想解放。")
    ai = AIService(Settings(mode="cloud", base_url="https://offline.invalid", model="fixture", api_key="fictional"))
    calls = []
    async def request(instructions, payload, schema, **kwargs):
        calls.append((instructions, payload))
        return {"points": [point(b, "failed-one", "新文化运动推动思想解放。") ]}
    ai._request = request
    rejected = [{"knowledge_id": "failed-one", "reason": "引用不是连续原文。", "draft_preview": "{}"}]
    selected = asyncio.run(ai.repair_knowledge_points_visual([b], [b.block_id], [b"fixture-image"], [1], rejected))
    assert len(calls) == 1 and calls[0][1]["retry_feedback"] == rejected
    assert "只返回这些指定" in calls[0][0]
    assert selected.points[0].evidence[0].quote == b.text
    assert not selected.rejected_points


def test_partial_extraction_is_saved_and_retry_only_generates_repaired_point(client):
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    repair_calls = []
    async def partial(blocks, ids, images, pages):
        result = await original(blocks, ids, images, pages)
        raw = [p.model_dump() for p in result.points]
        raw[1]["evidence"][0]["quote"] = "Invented evidence"
        return select_knowledge_points(raw, blocks, ids)
    async def repair(blocks, ids, images, pages, feedback):
        repair_calls.append(feedback)
        identifier = feedback[0]["knowledge_id"]
        raw = point(blocks[1], identifier, blocks[1].text[:40])
        return select_knowledge_points([raw], blocks, ids, [identifier])
    ai.extract_knowledge_visual_partial, ai.repair_knowledge_points_visual = partial, repair
    result = wait_import(client, upload(client))
    assert result["status"] == "partial"
    assert len(result["job"]["card_ids"]) == 1
    assert result["job"]["batch_summary"]["partial"] == 1
    assert "连续原文" in result["job"]["remaining_details"][0]["reasons"][0]
    old_ids = set(result["job"]["card_ids"])
    assert client.post(f"/api/material-imports/{result['id']}/retry").status_code == 200
    finished = wait_import(client, result)
    assert finished["status"] == "completed", finished
    assert len(finished["job"]["card_ids"]) == 2 and old_ids <= set(finished["job"]["card_ids"])
    assert len(repair_calls) == 1 and len(repair_calls[0]) == 1
    assert repair_calls[0][0]["knowledge_id"] == "k1"
    assert ai.generation_calls == 2
    assert finished["job"]["batch_summary"]["successful"] == 1
    assert finished["job"]["remaining_details"] == []


def test_old_rejected_layout_only_candidate_is_reused_before_any_generation(client):
    ai = client.app.state.ai
    original = ai.generate_knowledge_cards
    async def legacy_rejection(blocks, points):
        batch = await original(blocks, points)
        raw = batch.cards[1].card.model_dump()
        raw["evidence"][0]["quote"] = raw["evidence"][0]["quote"].replace(" ", "\n")
        bad = {**rejected_draft(raw, 2, "Old literal comparison failed"), "knowledge_id": points[1].knowledge_id}
        return KnowledgeSelection(cards=[batch.cards[0]], rejected_cards=[bad])
    ai.generate_knowledge_cards_partial = legacy_rejection
    result = wait_import(client, upload(client))
    assert result["status"] == "partial" and len(result["job"]["card_ids"]) == 1
    assert ai.generation_calls == 1
    assert client.post(f"/api/material-imports/{result['id']}/retry").status_code == 200
    finished = wait_import(client, result)
    assert finished["status"] == "completed" and len(finished["job"]["card_ids"]) == 2
    assert ai.generation_calls == 1, "A locally recoverable draft needs review, not another generation request"
    assert len(client.get("/api/state").json()["cards"]) == 2


def test_bad_candidate_retry_sends_reason_instead_of_repeating_original_generation(client):
    ai = client.app.state.ai
    original = ai.generate_knowledge_cards
    feedback_calls = []
    async def partial(blocks, points):
        batch = await original(blocks, points)
        raw = [item.model_dump() for item in batch.cards]
        raw[1]["card"]["evidence"][0]["quote"] = "Invented fact"
        return select_knowledge_drafts(raw, blocks, points)
    async def repair(blocks, points, feedback):
        feedback_calls.append(([p.knowledge_id for p in points], feedback))
        return await original(blocks, points)
    ai.generate_knowledge_cards_partial, ai.repair_knowledge_cards_partial = partial, repair
    result = wait_import(client, upload(client))
    assert result["status"] == "partial"
    assert client.post(f"/api/material-imports/{result['id']}/retry").status_code == 200
    finished = wait_import(client, result)
    assert finished["status"] == "completed"
    assert feedback_calls[0][0] == ["k1"]
    assert "连续原文" in feedback_calls[0][1][0]["reason"]


def test_quality_failure_reuses_checkpointed_valid_drafts(client):
    ai = client.app.state.ai
    review = ai.review_cards_visual
    failing = True
    async def interrupted(*args):
        if failing:
            raise AIError("Fixture invalid review", code="validation")
        return await review(*args)
    ai.review_cards_visual = interrupted
    result = wait_import(client, upload(client))
    assert result["status"] == "failed" and ai.generation_calls == 1
    failing = False
    assert client.post(f"/api/material-imports/{result['id']}/retry").status_code == 200
    finished = wait_import(client, result)
    assert finished["status"] == "completed" and len(finished["job"]["card_ids"]) == 2
    assert ai.generation_calls == 1
