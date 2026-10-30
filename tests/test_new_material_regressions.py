"""Offline reproductions of the new-material failures; not model accuracy scores."""
import asyncio
import copy

import pytest

from ankisense.auto_card_models import KnowledgeBatch, KnowledgePoint, CardQualityReport, QualityIssue
from ankisense.llm import AIService, AIError, Settings
from ankisense.schemas import CardDraft, SourceBlock
from ankisense.learning_policy import unasked_expression_requirement
from test_card_auto_review import good_review
from test_automatic_import import client, lecture_pdf, wait_import


def lesson():
    block = SourceBlock(block_id="b", document_id="doc", page=1,
                        text="线性表是有穷序列，长度n≥0，可表示为(e0,…,en-1)。")
    card = CardDraft(question="什么是线性表？长度n满足什么条件？", answer=block.text,
                     concept="线性表", rubric=[{"point_id":"p1","text":"有穷序列"},
                     {"point_id":"p2","text":"n≥0并写出(e0,…,en-1)"}],
                     evidence=[{"block_id":"b","quote":block.text}])
    service = AIService(Settings(mode="cloud", base_url="https://offline.invalid/v1",
                                 model="offline-fixture", api_key="offline-placeholder"))
    return service, card, [block]


def test_visual_review_retries_unwrapped_response_with_binding_and_preserves_valid_neighbor(monkeypatch):
    service, card, blocks = lesson()
    review = good_review(card.model_dump())
    calls = []
    async def request(instruction, data, schema, **kwargs):
        calls.append(copy.deepcopy(data))
        if len(calls) == 1:
            return {"reviews":[{"card_index":0,"review":review}, review]}
        assert data["retry_feedback"][0]["card_index"] == 1
        changed = {**review, "decision":"revise", "issues":["不能覆盖之前有效的回执。"]}
        return {"reviews":[{"card_index":0,"review":changed}, {"card_index":1,"review":review}]}
    monkeypatch.setattr(service, "_request", request)
    result = asyncio.run(service.review_cards_visual_semantic([card,card],blocks,[b"image"],[1]))
    assert len(calls) == 2 and all(r.verdict == "clear" for r in result)
    assert calls[0]["cards"] == calls[1]["cards"]


@pytest.mark.parametrize("response", ["unwrapped", "duplicate", "unknown"])
def test_invalid_review_binding_is_never_repaired_by_guessing_indices(monkeypatch, response):
    service, card, blocks = lesson()
    review = good_review(card.model_dump())
    calls=[]
    async def request(*args, **kwargs):
        calls.append(True)
        entries = {"unwrapped":[review], "duplicate":[{"card_index":0,"review":review}]*2,
                   "unknown":[{"card_index":4,"review":review}]}[response]
        return {"reviews":entries}
    monkeypatch.setattr(service,"_request",request)
    result=asyncio.run(service.review_cards_visual_semantic([card],blocks,[b"image"],[1]))
    assert len(calls)==2
    assert result[0].verdict=="needs_review" and result[0].issues[0].code=="review_protocol_error"


def test_conflicting_diagnosis_receives_specific_feedback_and_only_one_retry(monkeypatch):
    service, card, blocks=lesson()
    answer="线性表可以有无穷多个元素。"
    invalid={"verdict":"incorrect","addressed_point_ids":[],"missing_point_ids":["p1"],
             "contradicted_point_ids":["p1"],"answer_spans":[answer],"source_block_ids":["b"],"reason":"与有穷相矛盾。"}
    calls=[]
    async def request(instruction,data,schema):
        calls.append(copy.deepcopy(data))
        if len(calls)==1:
            return invalid
        assert "同时" in data["retry_feedback"]["validation_errors"][0]["error"]
        assert data["student_answer"]==answer and data["card"]==card.model_dump()
        return {**invalid,"missing_point_ids":[]}
    monkeypatch.setattr(service,"_request",request)
    result=asyncio.run(service.diagnose_answer(card,blocks,answer))
    assert result.verdict=="incorrect" and len(calls)==2


def test_persistent_diagnosis_conflict_is_rejected_without_loosening_constraints(monkeypatch):
    service,card,blocks=lesson()
    calls=[]
    async def request(*args,**kwargs):
        calls.append(True)
        return {"verdict":"incorrect","addressed_point_ids":[],"missing_point_ids":["p1"],
                "contradicted_point_ids":["p1"],"answer_spans":["无穷"],"source_block_ids":["b"],"reason":"冲突"}
    monkeypatch.setattr(service,"_request",request)
    with pytest.raises(AIError) as error:
        asyncio.run(service.diagnose_answer(card,blocks,"无穷"))
    assert error.value.code=="validation" and len(calls)==2


def test_minimal_probe_excludes_reference_and_rubric_and_requires_real_evidence(monkeypatch):
    service,card,blocks=lesson()
    async def request(instruction,data,schema):
        assert set(data)=={"question","evidence","source_blocks"}
        assert "card" not in data and "rubric" not in data and "answer" not in data
        return {"answer":"有穷序列，长度为非负整数。", "citations":[{"block_id":"b","exact_quote":"线性表是有穷序列，长度n≥0"}]}
    monkeypatch.setattr(service,"_request",request)
    assert "非负" in asyncio.run(service.minimal_answer_for_question(card,blocks))
    async def fabricated(*args,**kwargs):
        return {"answer":"任意答案", "citations":[{"block_id":"b","exact_quote":"并不存在的原文"}]}
    monkeypatch.setattr(service,"_request",fabricated)
    with pytest.raises(AIError,match="引用"):
        asyncio.run(service.minimal_answer_for_question(card,blocks))


@pytest.mark.parametrize("question,point,blocked", [
    ("什么是线性表？长度n需满足什么条件？", "指出n≥0并给出表示 (e0,e1,…,en-1)", True),
    ("请写出线性表的符号表示及长度条件。", "给出表示 (e0,e1,…,en-1)，n≥0", False),
    ("条件概率如何定义？", "给出P(AB)/P(B)", False),
    ("请写出分布函数的表达式。", "写出符号表示F(x)=P(X≤x)", False),
    ("请给出分布函数公式。", "写出数学表示F(x)=P(X≤x)", False),
    ("线性表是什么？", "指出元素的有穷序列", False),
])
def test_explicit_unasked_notation_is_blocked_without_rejecting_requested_formulas(question,point,blocked):
    _,card,_=lesson()
    card.question=question
    card.rubric[1].text=point
    assert bool(unasked_expression_requirement(card))==blocked


def test_model_self_agreement_cannot_bypass_explicit_notation_scope_conflict(monkeypatch):
    from ankisense.reference_check import minimal_answer_passes
    service,card,blocks=lesson()
    card.rubric[1].text="指出n≥0并给出表示 (e0,e1,…,en-1)"
    review=good_review(card.model_dump())
    async def request(*args,**kwargs):
        return {"reviews":[{"card_index":0,"review":review}]}
    monkeypatch.setattr(service,"_request",request)
    result=asyncio.run(service.review_cards_visual_semantic([card],blocks,[b'image'],[1]))[0]
    assert result.verdict=="needs_review" and result.issues[0].code=="minimal_answer_mismatch"
    receipt={"answer":card.answer,"diagnosis":{"verdict":"correct","addressed_point_ids":["p1","p2"],
        "missing_point_ids":[],"contradicted_point_ids":[],"answer_spans":[card.answer],"source_block_ids":["b"],"reason":"模型自测称正确。"}}
    assert not minimal_answer_passes(receipt,card,blocks)


def test_protocol_failure_keeps_original_candidate_without_content_repair(client):
    ai=client.app.state.ai
    async def protocol(cards,*args):
        return [CardQualityReport(verdict="needs_review",supported_point_ids=[],
                issues=[QualityIssue(code="review_protocol_error",severity="warning",reason="结构重试失败")]) for c in cards]
    ai.review_cards_visual_semantic=protocol
    record=client.post("/api/material-imports",files={"file":("lecture.pdf",lecture_pdf(1))}).json()
    result=wait_import(client,record)
    cards=client.get("/api/state").json()["cards"]
    assert not cards
    assert ai.repair_calls==0
    pending=result["job"]["rejected_cards"][0]
    assert pending["quality"]["issues"][0]["code"]=="review_protocol_error"
    assert pending["repair_attempts"]==0 and pending["draft_preview"]


def test_independent_minimal_answer_failure_prevents_automatic_export(client):
    from ankisense.schemas import Diagnosis
    ai=client.app.state.ai
    original=ai.diagnose_answer
    async def minimal(card,blocks):
        return "独立最短充分回答。"
    async def diagnose(card,blocks,answer):
        if answer != "独立最短充分回答。":
            return await original(card,blocks,answer)
        return Diagnosis(verdict="incomplete",addressed_point_ids=["fact"],missing_point_ids=["fact"],
                         contradicted_point_ids=[],answer_spans=[answer],source_block_ids=[card.evidence[0].block_id],
                         reason="评分点还要求题目未问的符号。")
    ai.minimal_answer_for_question=minimal
    ai.diagnose_answer=diagnose
    record=client.post("/api/material-imports",files={"file":("lecture.pdf",lecture_pdf(1))}).json()
    result=wait_import(client,record)
    cards=client.get("/api/state").json()["cards"]
    assert not cards
    quality=result["job"]["rejected_cards"][0]["quality"]
    assert quality["issues"][0]["code"]=="minimal_answer_mismatch"
    assert quality["reference_answer_check"]["verdict"]=="correct"
    assert quality["minimal_answer_check"]["diagnosis"]["verdict"]=="incomplete"
    assert "autcard-" not in client.get("/api/export").text


def test_unused_page_budget_backfills_known_points_and_resume_does_not_repeat(client):
    ai=client.app.state.ai
    pages=[]
    async def extract(blocks,ids,images,numbers,**kwargs):
        page=numbers[0]
        pages.append(page)
        block=blocks[0]
        count={1:1,2:1,3:0,4:4}[page]
        return KnowledgeBatch(points=[KnowledgePoint(knowledge_id=f"p{page}_{i}",concept=f"fact{page}_{i}",
            statement=block.text[i:i+20],evidence=[{"block_id":block.block_id,"quote":block.text[i:i+20]}])
            for i in range(count)])
    ai.extract_knowledge_visual_partial=extract
    # Distinguish multiple facts on one page in this mechanical fixture.
    original=ai.generate_knowledge_cards
    async def generate(blocks,points):
        batch=await original(blocks,points)
        for item in batch.cards:
            item.card.question=f"{item.knowledge_id}是什么？"
        return batch
    ai.generate_knowledge_cards=generate
    record=client.post("/api/material-imports",data={"count":"8"},files={"file":("lecture.pdf",lecture_pdf(4))}).json()
    result=wait_import(client,record)
    job=result["job"]
    assert len(job["card_ids"])==6 and pages==[1,2,3,4]
    batches=client.app.state.store.get("auto_card_jobs",result["job_id"])["batches"]
    assert sum(b.get("backfilled_count",0) for b in batches)==2
    assert job["coverage"]["selected_knowledge_count"]==6
    assert job["progress"]=={"completed":4,"total":4}
    store=client.app.state.store
    saved=store.get("auto_card_jobs",result["job_id"])
    store.put("auto_card_jobs",{**saved,"status":"interrupted","retryable":True})
    client.post(f"/api/material-imports/{result['id']}/retry").raise_for_status()
    resumed=wait_import(client,result)
    assert resumed["job"]["card_ids"]==job["card_ids"] and pages==[1,2,3,4]
