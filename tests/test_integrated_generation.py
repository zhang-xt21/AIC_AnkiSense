"""Offline regression coverage for the integrated visual generation workflow."""
import asyncio
import copy
import hashlib
from types import SimpleNamespace

import pytest

from ankisense.llm import AIError, AIService, Settings
from ankisense.auto_card_models import KnowledgePoint
from ankisense.auto_cards import automatic_plan, batch_blocks
from ankisense.schemas import CardDraft, SourceBlock
from test_automatic_import import client, lecture_pdf, upload, wait_import
from test_card_auto_review import good_review


def candidate(block, label="valid"):
    return {"question": f"What does {label} explain?", "answer": block.text,
            "concept": "source", "rubric": [{"point_id": "fact", "text": block.text}],
            "evidence": [{"block_id": block.block_id, "quote": block.text}]}


def test_real_adapter_isolates_invalid_knowledge_candidate(monkeypatch):
    block = SourceBlock(block_id="doc-p1-b1-r1", document_id="doc", page=1, text="Stacks follow last in, first out.")
    points = [KnowledgePoint(knowledge_id=f"k{i}", concept="stack", statement=block.text,
                             evidence=[{"block_id": block.block_id, "quote": block.text}]) for i in range(2)]
    bad = candidate(block, "invalid")
    bad["evidence"][0]["quote"] = "This quotation does not exist."
    ai = AIService(Settings(mode="cloud"))
    async def request(*args, **kwargs):
        return {"cards": [{"knowledge_id": "k0", "card": candidate(block)}, {"knowledge_id": "k1", "card": bad}]}
    monkeypatch.setattr(ai, "_request", request)
    result = asyncio.run(ai.generate_knowledge_cards_partial([block], points))
    assert [item.knowledge_id for item in result.cards] == ["k0"]
    assert [item["knowledge_id"] for item in result.rejected_cards] == ["k1"]
    assert "校验" in result.rejected_cards[0]["reason"]


def test_semantic_visual_batch_keeps_images_and_isolates_bad_review(monkeypatch):
    block = SourceBlock(block_id="doc-p1-b1-r1", document_id="doc", page=1, text="Stacks follow last in, first out.")
    card = CardDraft.model_validate(candidate(block))
    good = good_review(card.model_dump())
    bad = copy.deepcopy(good)
    bad["rubric_checks"][0].update(scope="background", scope_reason="The question did not ask for this.")
    calls = []
    async def request(instruction, data, schema, images=None):
        calls.append((instruction, data, images))
        return {"reviews": [{"card_index": 0, "review": good}, {"card_index": 1, "review": bad}]}
    ai = AIService(Settings(mode="cloud"))
    monkeypatch.setattr(ai, "_request", request)
    reports = asyncio.run(ai.review_cards_visual_semantic([card, card], [block], [b"original-page"], [1]))
    assert [report.verdict for report in reports] == ["clear", "needs_review"]
    assert reports[0].review_checks["question_coverage"] == "complete"
    assert calls[0][2] == [b"original-page"] and calls[0][1]["page_numbers"] == [1]


def test_failed_import_retries_saved_bytes_and_concurrent_clicks_are_idempotent(client, monkeypatch):
    import ankisense.automatic_import as module
    original = module.parse_pdf_document
    calls = []
    def parse(content, **options):
        calls.append(content)
        if len(calls) == 1:
            raise ValueError("Offline parser failure")
        return original(content, **options)
    monkeypatch.setattr(module, "parse_pdf_document", parse)
    content = lecture_pdf()
    failed = wait_import(client, upload(client, content))
    assert failed["status"] == "failed" and failed["retryable"]
    assert upload(client, content)["id"] == failed["id"] and len(calls) == 1
    service = client.app.state.automatic_imports
    async def retry_twice():
        return await asyncio.gather(service.retry(failed["id"]), service.retry(failed["id"]))
    results = client.portal.call(retry_twice)
    assert results[0]["id"] == results[1]["id"] == failed["id"]
    done = wait_import(client, failed)
    assert done["status"] == "completed" and len(calls) == 2
    assert done["attempt"] == 2
    document = client.app.state.store.get("documents", done["document_id"])
    assert document["pdf_sha256"] == hashlib.sha256(content).hexdigest()
    assert len(client.app.state.store.all("documents")) == 1


def test_partial_batch_retry_does_not_repeat_successful_cards(client):
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    calls = []
    async def extract(blocks, focus, images, numbers):
        calls.append(numbers)
        if len(calls) <= 2:
            raise AIError("Offline transient failure")
        return await original(blocks, focus, images, numbers)
    ai.extract_knowledge_visual = extract
    first = wait_import(client, upload(client, lecture_pdf(7)))
    assert first["status"] == "partial" and first["retryable"]
    saved = set(first["job"]["card_ids"])
    assert len(saved) == 4
    assert client.post(f"/api/material-imports/{first['id']}/retry").status_code == 200
    done = wait_import(client, first)
    assert done["status"] == "completed" and not done["retryable"]
    assert len(done["job"]["card_ids"]) == 7
    assert saved <= set(done["job"]["card_ids"])
    assert calls[-1] == [1, 2, 3] and calls.count([4, 5, 6]) == 1 and calls.count([7]) == 1
    assert len(client.app.state.store.all("cards")) == 7


def test_one_bad_candidate_keeps_valid_siblings_and_retry_only_regenerates_missing_point(client):
    ai = client.app.state.ai
    original = ai.generate_knowledge_cards
    generated = []
    async def generate(blocks, points):
        generated.append([point.knowledge_id for point in points])
        result = await original(blocks, points)
        if len(generated) == 1:
            raw = [item.model_dump() for item in result.cards]
            raw[-1]["card"]["evidence"][0]["quote"] = "No such original quotation."
            return SimpleNamespace(cards=raw)
        return result
    ai.generate_knowledge_cards = generate
    first = wait_import(client, upload(client, lecture_pdf(4)))
    assert first["status"] == "partial" and len(first["job"]["card_ids"]) == 3
    assert first["job"]["rejected_count"] == 1
    assert client.post(f"/api/material-imports/{first['id']}/retry").status_code == 200
    done = wait_import(client, first)
    assert done["status"] == "completed" and len(done["job"]["card_ids"]) == 4
    assert generated[-1] == ["k2"]
    assert done["job"]["rejected_count"] == 0


def test_optional_count_and_image_preference_use_same_import_flow(client):
    result = client.post("/api/material-imports", files={"file": ("target.pdf", lecture_pdf(7))},
                         data={"count": "2", "auto_images": "false"})
    assert result.status_code == 200, result.text
    done = wait_import(client, result.json())
    assert done["status"] == "completed" and len(done["job"]["card_ids"]) == 2
    assert done["plan"]["target_count"] == 2 and done["plan"]["partial_scope"]
    assert all(not client.app.state.store.get("cards", identifier)["images"] for identifier in done["job"]["card_ids"])


def test_unreachable_count_explains_shortfall_without_suggesting_billable_retry(client):
    result = client.post("/api/material-imports", files={"file": ("target.pdf", lecture_pdf(1))}, data={"count": "600"})
    assert result.status_code == 200
    done = wait_import(client, result.json())
    assert done["status"] == "partial" and not done["retryable"]
    assert "少于目标" in done["message"]
    assert client.post(f"/api/material-imports/{done['id']}/retry").status_code == 409


def test_large_local_plan_retains_original_limits_and_contiguous_windows():
    blocks = [SourceBlock(block_id=f"b{page}", document_id="large", page=page, text=str(page) + "x" * 2200)
              for page in range(1, 501)]
    plan = automatic_plan(blocks, None, 600)
    assert plan["max_count"] == 600 and plan["coverage_ratio"] == 1
    assert len(plan["selected_pages"]) == 500
    long = SourceBlock(block_id="long", document_id="large", page=1, text="x" * 25000)
    windows = [block for batch in batch_blocks([long]) for block in batch]
    assert "".join(block.text for block in windows) == long.text
    assert all(block.block_id == "long" and len(block.text) <= 9000 for block in windows)
