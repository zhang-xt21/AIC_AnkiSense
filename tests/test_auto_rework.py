"""Offline workflow contracts, not semantic accuracy measurements."""
import asyncio
import copy
import pytest
from fastapi.testclient import TestClient

from ankisense.auto_card_models import KnowledgeBatch, KnowledgeCardBatch, KnowledgeCard, CardQualityReport, QualityIssue
from ankisense.llm import Settings, AIError
from ankisense.schemas import CardDraft
from test_automatic_import import VisualAI, lecture_pdf, wait_import
from test_card_auto_review import good_review


class SplitAI(VisualAI):
    def __init__(self):
        super().__init__()
        self.split_calls = 0
        self.bad_children = False
        self.bad_split = False
        self.fail_other = False

    async def extract_knowledge_visual(self, blocks, focus_block_ids, images, page_numbers):
        batch = await super().extract_knowledge_visual(blocks, focus_block_ids, images, page_numbers)
        if self.fail_other:
            batch.points.append(batch.points[0].model_copy(update={"knowledge_id": "other", "concept": "永不通过"}))
        return batch

    async def generate_knowledge_cards(self, blocks, points):
        self.generation_calls += 1
        return KnowledgeCardBatch(cards=[KnowledgeCard(knowledge_id=p.knowledge_id, card=CardDraft(
            question=f"{p.concept}说明什么？", answer=p.statement, concept=p.concept,
            rubric=[{"point_id": "fact", "text": p.statement}], evidence=p.evidence)) for p in points])

    async def review_card(self, card, blocks):
        self.review_calls += 1
        if card.concept == "永不通过":
            return CardQualityReport(verdict="needs_review", supported_point_ids=[], issues=[
                QualityIssue(code="unsupported_answer", severity="warning", reason="固定失败用例。")])
        if card.concept == "来源知识点" or self.bad_children:
            return CardQualityReport(verdict="needs_review", supported_point_ids=[], issues=[
                QualityIssue(code="multiple_concepts", severity="warning", reason="两个独立目标需要分别制卡。")])
        return CardQualityReport(verdict="clear", supported_point_ids=["fact"], issues=[])

    async def split_overloaded_knowledge(self, point, card, blocks, report, images, page_numbers):
        self.split_calls += 1
        if self.bad_split:
            raise AIError("离线夹具：本轮拆分失败。", code="validation")
        assert images and page_numbers
        return KnowledgeBatch(points=[point.model_copy(update={"knowledge_id": f"part-{i}",
            "concept": concept, "statement": text}) for i, (concept, text) in enumerate([
                ("目标甲", point.statement[:12]), ("目标乙", point.statement[12:])])])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    ai = SplitAI()
    app = create_app(tmp_path / "app", Settings(mode="cloud", base_url="https://offline.invalid/v1", model="fixture", api_key="fixture"), ai)
    with TestClient(app) as client:
        yield client, ai


def run(client, count=None):
    response = client.post("/api/material-imports", files={"file": ("old.pdf", lecture_pdf(1), "application/pdf")},
                           data={} if count is None else {"count": str(count)})
    assert response.status_code == 200, response.text
    return wait_import(client, response.json())


@pytest.mark.parametrize("count", [None, 2])
def test_overload_becomes_two_reviewed_cards_with_both_goals_preserved(setup, count):
    client, ai = setup
    result = run(client, count)
    assert result["status"] == "completed", result
    cards = client.get("/api/state").json()["cards"]
    assert {c["concept"] for c in cards} == {"目标甲", "目标乙"}
    assert all(c["status"] == "approved" for c in cards)
    assert ai.split_calls == 1 and ai.repair_calls == 0
    assert len(result["job"]["split_history"]) == 1
    history = result["job"]["split_history"][0]
    assert history["card"]["concept"] == "来源知识点" and len(history["children"]) == 2
    calls = ai.generation_calls
    for _ in range(3): client.get("/api/material-imports/" + result["id"])
    assert ai.generation_calls == calls, "Reading results must never spend model calls."


def test_quantity_cap_defers_second_goal_instead_of_losing_it(setup):
    client, ai = setup
    result = run(client, 1)
    assert result["status"] == "partial", result
    assert len(result["job"]["card_ids"]) == 1
    assert result["job"]["batch_summary"]["pending_points"] == 1
    assert any("拆分目标" in reason for d in result["job"]["remaining_details"] for reason in d["reasons"])
    saved = client.app.state.store.get("auto_card_jobs", result["job_id"])
    assert sum(len(b.get("deferred_knowledge", [])) for b in saved["batches"]) == 1
    assert ai.split_calls == 1


def test_same_point_id_on_different_source_batches_does_not_suppress_split(setup):
    client, ai = setup
    response = client.post("/api/material-imports", files={"file": ("four.pdf", lecture_pdf(4), "application/pdf")})
    result = wait_import(client, response.json())
    assert result["status"] == "completed", result
    assert ai.split_calls == 4
    assert len(result["job"]["split_history"]) == 4


def test_bad_children_do_not_split_forever_or_create_failed_learning_cards(setup):
    client, ai = setup
    ai.bad_children = True
    result = run(client, 2)
    assert result["status"] == "failed", result
    assert not client.get("/api/state").json()["cards"]
    assert ai.split_calls == 1 and ai.repair_calls == 0
    assert len(result["job"]["rejected_cards"]) == 2
    assert all(x["repair_attempts"] == 0 and x["quality"]["issues"] for x in result["job"]["rejected_cards"])
    job = client.app.state.store.get("auto_card_jobs", result["job_id"])
    history = [x for b in job["batches"] for x in b.get("rework_history", [])]
    assert len(history) == 2 and all(x["before"] and x["after"] and x["final_quality"]["issues"] for x in history)
    assert client.get("/api/export").text.count("autcard-") == 0


def test_split_goals_continue_after_a_sibling_fails_without_retrying_sibling_forever(setup):
    client, ai = setup
    ai.fail_other = True
    result = run(client, 3)
    assert result["status"] == "partial", result
    assert len(result["job"]["card_ids"]) == 2
    assert {c["concept"] for c in client.get("/api/state").json()["cards"]} == {"目标甲", "目标乙"}
    assert ai.repair_calls == 1 and ai.split_calls == 1
    assert len(result["job"]["rejected_cards"]) == 1


def test_failed_split_keeps_parent_and_explicit_resume_can_try_again(setup):
    client, ai = setup
    ai.bad_split = True
    result = run(client, 2)
    assert result["status"] == "failed" and result["retryable"]
    assert not client.get("/api/state").json()["cards"]
    assert result["job"]["rejected_cards"][0]["question"] == "来源知识点说明什么？"
    assert ai.repair_calls == 0, "Do not shorten a parent after its split failed."
    ai.bad_split = False
    retried = client.post(f"/api/material-imports/{result['id']}/retry")
    assert retried.status_code == 200, retried.text
    final = wait_import(client, result)
    assert final["status"] == "completed", final
    assert len(final["job"]["card_ids"]) == 2
    assert ai.split_calls == 2


@pytest.mark.parametrize("damage", [None, "one", "bad_quote", "duplicate", "fewer_than_identified"])
def test_real_split_adapter_validates_sources_and_keeps_identified_targets(monkeypatch, damage):
    from ankisense.llm import AIService
    from ankisense.schemas import SourceBlock
    from ankisense.auto_card_models import KnowledgePoint
    block = SourceBlock(block_id="b1", document_id="doc", page=1, text="甲为A。乙为B。丙为C。")
    parent = KnowledgePoint(knowledge_id="parent", concept="多个定义", statement=block.text,
                            evidence=[{"block_id": "b1", "quote": block.text}])
    card = CardDraft(question="甲是什么？乙是什么？丙是什么？", answer=block.text, concept=parent.concept,
                     rubric=[{"point_id": "fact", "text": block.text}], evidence=parent.evidence)
    checks = good_review(card.model_dump())
    targets = ["甲是什么？", "乙是什么？", "丙是什么？"] if damage == "fewer_than_identified" else ["甲是什么？", "乙是什么？"]
    checks.update(decision="revise", issues=["多个目标应拆开。"], learning_scope={"verdict": "multiple",
        "targets": [{"question_span": q, "goal": q} for q in targets], "reason": "多个独立定义。"})
    report = CardQualityReport(verdict="needs_review", supported_point_ids=[], review_checks=checks,
                              issues=[QualityIssue(code="multiple_concepts", severity="warning", reason="多个定义。")])
    points = [parent.model_copy(update={"knowledge_id": "a", "concept": "甲", "statement": "甲为A。"}).model_dump(),
              parent.model_copy(update={"knowledge_id": "b", "concept": "乙", "statement": "乙为B。"}).model_dump()]
    if damage == "one": points = points[:1]
    if damage == "bad_quote": points[1]["evidence"] = [{"block_id": "b1", "quote": "无依据句子"}]
    if damage == "duplicate": points[1].update(concept=points[0]["concept"], statement=points[0]["statement"])
    ai = AIService(Settings(mode="cloud"))
    async def request(*args, **kwargs):return {"points": copy.deepcopy(points)}
    monkeypatch.setattr(ai, "_request", request)
    operation = ai.split_overloaded_knowledge(parent, card, [block], report, [b"fixture"], [1])
    if damage:
        with pytest.raises(AIError): asyncio.run(operation)
    else:
        assert len(asyncio.run(operation).points) == 2
