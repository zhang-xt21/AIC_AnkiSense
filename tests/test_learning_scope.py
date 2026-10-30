import asyncio
import copy

import pytest
from pydantic import ValidationError

from ankisense.learning_policy import multiple_target_reason
from ankisense.llm import AIService, CardReviewResult, Settings, validate_card_review
from ankisense.schemas import SourceBlock
from test_study_quality_feedback import draft
from test_card_auto_review import good_review, ready, post
from test_figure_recommendations import context


@pytest.mark.parametrize("question", [
    r"在数组 \(a\) 中二分查找 \(x\)：初始 \(i\)、\(j\) 如何设置？循环条件是什么？当x>a[mid]和x<a[mid]时分别如何更新？循环结束后怎样判断没找到？",
    "二分查找的初始化如何设置？终止条件是什么？失败怎样判断？",
    "信念具有哪些特性？高层次信念与低层次信念、信仰之间是什么关系？",
])
def test_known_overloads_cannot_pass_even_with_correct_answers(question):
    assert multiple_target_reason(draft(question))


@pytest.mark.parametrize("question", [
    "二分查找中，初始左右边界如何设置？",
    "二分查找中，循环条件是什么？为什么？",
    "给定二分查找的初始化、循环条件和更新规则，为什么要求数组有序？",
    "信念具有哪些特性？",
    "什么是信念？",
    "栈在哪端插入和删除？遵循什么顺序？",
])
def test_related_questions_and_provided_context_are_not_counted_as_overload(question):
    assert multiple_target_reason(draft(question)) is None


def test_old_review_without_scope_is_not_a_current_machine_pass():
    value = good_review(draft("什么是信念？").model_dump())
    value.pop("learning_scope")
    with pytest.raises(ValidationError):
        CardReviewResult.model_validate(value)


def test_scope_must_bind_question_and_cannot_pass_with_multiple_targets():
    card = draft("甲是什么？乙是什么？")
    blocks = [SourceBlock(block_id="b1", document_id="doc", page=1, text="原文结论。")]
    value = good_review(card.model_dump())
    value["learning_scope"] = {"verdict": "multiple", "targets": [
        {"question_span": "甲是什么？", "goal": "甲的定义"}, {"question_span": "乙是什么？", "goal": "乙的定义"}],
        "reason": "两个独立定义，应拆开。"}
    with pytest.raises(ValueError):
        validate_card_review(CardReviewResult.model_validate(value), card, blocks)
    value.update(decision="revise", issues=["两个独立目标。"])
    validate_card_review(CardReviewResult.model_validate(value), card, blocks)
    value["learning_scope"]["targets"][0]["question_span"] = "不存在的题干"
    with pytest.raises(ValueError):
        validate_card_review(CardReviewResult.model_validate(value), card, blocks)


def test_saved_overload_is_blocked_before_paid_review(ready):
    client, card, service, calls = ready
    card["question"] = "二分查找初始化如何设置？循环条件是什么？区间如何更新？怎样判断没找到？"
    service.store.put("cards", card)
    assert post(client, card).json()["status"] == "attention"
    assert calls == []


def test_multiple_scope_becomes_repairable_multiple_concepts(monkeypatch):
    card = draft("甲是什么？乙是什么？")
    blocks = [SourceBlock(block_id="b1", document_id="doc", page=1, text="原文结论。")]
    value = good_review(card.model_dump())
    value.update(decision="revise", issues=["两个目标应分开。"])
    value["learning_scope"] = {"verdict": "multiple", "targets": [
        {"question_span": "甲是什么？", "goal": "甲"}, {"question_span": "乙是什么？", "goal": "乙"}],
        "reason": "两个独立目标应分别复习。"}
    ai = AIService(Settings(mode="cloud"))
    async def request(*args, **kwargs):return {"reviews": [{"card_index": 0, "review": copy.deepcopy(value)}]}
    monkeypatch.setattr(ai, "_request", request)
    result = asyncio.run(ai.review_cards_visual_semantic([card], blocks, [b"offline"], [1]))
    assert result[0].verdict == "needs_review"
    assert result[0].issues[0].code == "multiple_concepts"
