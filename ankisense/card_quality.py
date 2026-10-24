"""Deterministic integrity checks; these cannot prove semantic correctness."""

import re
from collections.abc import Mapping, Sequence

from .auto_card_models import CardQualityReport, KnowledgePoint, QualityIssue
from .evidence import validate_card
from .schemas import CardDraft, Evidence, SourceBlock


def source_map(blocks: Sequence[SourceBlock]) -> dict[str, SourceBlock]:
    if not blocks:
        raise ValueError("需要提供讲义片段。")
    mapping = {block.block_id: block for block in blocks}
    if len(mapping) != len(blocks):
        raise ValueError("讲义片段编号重复。")
    return mapping


def validate_evidence(evidence: Sequence[Evidence], blocks: Sequence[SourceBlock]) -> None:
    mapping = source_map(blocks)
    for item in evidence:
        block = mapping.get(item.block_id)
        if block is None:
            raise ValueError("引用了本次材料中不存在的讲义片段。")
        if not item.quote.strip() or item.quote not in block.text:
            raise ValueError("引用必须是对应讲义片段中的连续原文。")


def validate_knowledge_points(points: Sequence[KnowledgePoint], blocks: Sequence[SourceBlock]) -> None:
    source_map(blocks)
    ids = [point.knowledge_id for point in points]
    if len(points) > 6 or len(ids) != len(set(ids)):
        raise ValueError("知识点数量不能超过六个，且编号必须唯一。")
    for point in points:
        if not point.evidence:
            raise ValueError("每个知识点必须有原文依据。")
        validate_evidence(point.evidence, blocks)


def validate_quality_report(
    report: CardQualityReport, card: CardDraft, blocks: Sequence[SourceBlock]
) -> None:
    validate_card(card, list(blocks))
    source_map(blocks)
    supported = set(report.supported_point_ids)
    point_ids = {point.point_id for point in card.rubric}
    if len(supported) != len(report.supported_point_ids) or not supported.issubset(point_ids):
        raise ValueError("质量检查引用了未知或重复的评分要点编号。")
    for issue in report.issues:
        validate_evidence(issue.evidence, blocks)
    if report.review_checks is not None:
        from .llm import CardReviewResult, validate_card_review
        review = CardReviewResult.model_validate(report.review_checks)
        validate_card_review(review, card, list(blocks))
        if report.verdict == "clear" and review.decision != "pass":
            raise ValueError("原页通过结论必须同时满足整卡题目范围与逐项证据校验。")
    if report.verdict == "clear":
        if report.issues or supported != point_ids:
            raise ValueError("无疑点结论必须确认所有评分要点得到原文支持。")
    elif report.verdict != "needs_review" or not report.issues:
        raise ValueError("待审核结论必须说明质量疑点。")


def _same_text(text: str) -> str:
    # Whitespace differences are safe to normalize. Punctuation, case, signs,
    # numbers and formulas remain intact to avoid treating distinct facts alike.
    return re.sub(r"\s+", " ", text).strip()


def rule_issues(
    card: CardDraft,
    blocks: Sequence[SourceBlock],
    existing_cards: Sequence[CardDraft | Mapping],
) -> list[QualityIssue]:
    """Flag an exact repeated question/answer without guessing semantic overlap."""
    validate_card(card, list(blocks))
    key = (_same_text(card.question), _same_text(card.answer), card.kind)
    for existing in existing_cards:
        if isinstance(existing, CardDraft):
            question, answer, kind = existing.question, existing.answer, existing.kind
        elif isinstance(existing, Mapping):
            question, answer, kind = existing.get("question"), existing.get("answer"), existing.get("kind", "basic")
        else:
            raise ValueError("已有卡片格式不正确。")
        if not isinstance(question, str) or not isinstance(answer, str):
            raise ValueError("已有卡片缺少题目或参考答案。")
        if key == (_same_text(question), _same_text(answer), kind):
            return [QualityIssue(
                code="duplicate_card", severity="warning",
                reason="已有卡片包含相同的问题与参考答案。",
                evidence=card.evidence,
                suggestion="检查重复卡片，保留一张或修改为不同的知识目标。",
            )]
    return []
