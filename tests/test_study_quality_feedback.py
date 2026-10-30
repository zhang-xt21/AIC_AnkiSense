"""Human feedback gates: local fixtures, not a human or model accuracy score."""
import asyncio
import copy

import pytest

from ankisense.learning_policy import non_learning_reason, question_context_reason
from ankisense.llm import AIService, Settings
from ankisense.reference_check import minimal_answer_passes
from ankisense.schemas import CardDraft, SourceBlock
from test_card_auto_review import good_review, post, ready
from test_figure_recommendations import context


def draft(question):
    return CardDraft(question=question, answer="原文结论。", concept="复习知识",
                     rubric=[{"point_id": "p1", "text": "原文结论"}],
                     evidence=[{"block_id": "b1", "quote": "原文结论。"}])


@pytest.mark.parametrize("question", [
    "逻辑函数化简题目作答前应先检查什么？",
    "公式法化简在过程书写方面有什么要求？",
])
def test_exam_procedure_is_not_the_default_learning_target(question):
    assert non_learning_reason("公式法化简", question)


@pytest.mark.parametrize("question", [
    "二分查找为什么要求待查区间有序？",
    "欧几里得算法的递推公式是什么？",
    "逻辑函数化简为什么可以使用反演定理？",
    "如何判断整型表达式在C语言中是否为真？",
])
def test_algorithm_principles_and_conditions_remain_valid_targets(question):
    assert non_learning_reason("算法与逻辑", question) is None
    assert question_context_reason(draft(question)) is None


@pytest.mark.parametrize("question", [
    "字符数组为什么需要结束标志？讲义中哪种初始化写法不安全？",
    "for语句中exp1、exp2、exp3各有什么作用？",
])
def test_observed_missing_context_is_explicitly_flagged(question):
    assert question_context_reason(draft(question))


@pytest.mark.parametrize("question", [
    "在for(exp1; exp2; exp3)中，三个表达式分别起什么作用？",
    "给定以下代码：char c[]={'a','b'};，它能否直接作为C字符串使用？",
    r"设$V=\{(x,y,0):x,y\in\mathbb{R}\}$，$u=(0,0,1)$，$u+V$是什么平面？",
    r"判断整数$m$是否为素数时，试除因子可检查到哪个上限？",
])
def test_supplied_context_and_latex_are_not_local_quality_errors(question):
    assert question_context_reason(draft(question)) is None


@pytest.mark.parametrize("question", [
    "公式法化简在过程书写方面有什么要求？",
    "for语句中exp1、exp2、exp3各有什么作用？",
])
def test_saved_review_blocks_known_style_failures_without_paid_requests(ready, question):
    client, card, service, calls = ready
    card["question"] = question
    service.store.put("cards", card)
    result = post(client, card)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "attention"
    assert calls == []
    assert service.store.get("cards", card["id"])["status"] == "draft"


def test_visual_model_pass_cannot_bypass_explicit_missing_context(monkeypatch):
    card = draft("for语句中exp1、exp2、exp3各有什么作用？")
    block = SourceBlock(block_id="b1", document_id="doc", page=1, text="原文结论。")
    service = AIService(Settings(mode="cloud", base_url="https://offline.invalid/v1",
                                 model="offline-fixture", api_key="fixture"))
    async def request(*args, **kwargs):
        return {"reviews": [{"card_index": 0, "review": copy.deepcopy(good_review(card.model_dump()))}]}
    monkeypatch.setattr(service, "_request", request)
    reports = asyncio.run(service.review_cards_visual_semantic([card], [block], [b"local-fixture"], [1]))
    assert reports[0].verdict == "needs_review"
    assert reports[0].issues[0].code == "ambiguous_question"


def test_consistency_gate_does_not_accept_contextless_old_card():
    card = draft("for语句中exp1、exp2、exp3各有什么作用？")
    block = SourceBlock(block_id="b1", document_id="doc", page=1, text="原文结论。")
    value = {"answer": "原文结论。", "diagnosis": {
        "verdict": "correct", "addressed_point_ids": ["p1"], "missing_point_ids": [],
        "contradicted_point_ids": [], "answer_spans": ["原文结论。"],
        "source_block_ids": ["b1"], "reason": "本地固定响应。"}}
    assert not minimal_answer_passes(value, card, [block])
