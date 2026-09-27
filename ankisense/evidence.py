"""Deterministic checks for model outputs; these do not prove semantic accuracy."""

from .schemas import CardDraft, Diagnosis, SourceBlock


def _source_map(blocks: list[SourceBlock]) -> dict[str, SourceBlock]:
    mapping = {block.block_id: block for block in blocks}
    if len(mapping) != len(blocks):
        raise ValueError("讲义片段编号重复。")
    return mapping


def validate_card(card: CardDraft, blocks: list[SourceBlock]) -> None:
    """Ensure all rubric identifiers and verbatim source quotations are valid."""
    source_map = _source_map(blocks)
    point_ids = [point.point_id for point in card.rubric]
    if len(set(point_ids)) != len(point_ids):
        raise ValueError("评分要点编号必须唯一。")
    for evidence in card.evidence:
        block = source_map.get(evidence.block_id)
        if block is None:
            raise ValueError("卡片引用了本次材料中不存在的讲义片段。")
        if not evidence.quote.strip() or evidence.quote not in block.text:
            raise ValueError("卡片引用必须是对应讲义片段中的连续原文。")


def validate_diagnosis(
    diagnosis: Diagnosis,
    card: CardDraft,
    blocks: list[SourceBlock],
    answer: str,
) -> None:
    """Reject fabricated evidence, unknown points, and contradictory verdicts."""
    validate_card(card, blocks)
    point_ids = {point.point_id for point in card.rubric}
    cited_ids = {evidence.block_id for evidence in card.evidence}
    for values in (
        diagnosis.addressed_point_ids,
        diagnosis.missing_point_ids,
        diagnosis.contradicted_point_ids,
        diagnosis.source_block_ids,
        diagnosis.answer_spans,
    ):
        if len(values) != len(set(values)):
            raise ValueError("诊断结果包含重复编号或重复引用。")
    missing = set(diagnosis.missing_point_ids)
    contradicted = set(diagnosis.contradicted_point_ids)
    addressed = set(diagnosis.addressed_point_ids)
    if not (missing | contradicted | addressed).issubset(point_ids):
        raise ValueError("诊断引用了不存在的评分要点。")
    if missing & contradicted:
        raise ValueError("同一要点不能同时被标记为缺失和矛盾。")
    if addressed & contradicted:
        raise ValueError("同一要点不能同时被标记为已正确涉及和矛盾。")
    if not set(diagnosis.source_block_ids).issubset(cited_ids):
        raise ValueError("诊断只能引用当前题目已经核验的讲义片段。")
    for span in diagnosis.answer_spans:
        if not span.strip() or span not in answer:
            raise ValueError("诊断中的学生答案引用必须来自本次提交的连续原文。")
    if diagnosis.verdict == "correct":
        if missing or contradicted or not diagnosis.answer_spans:
            raise ValueError("正确结论不能包含错误要点，且需要引用学生答案。")
    elif diagnosis.verdict == "incomplete":
        if not missing or contradicted or not addressed or not diagnosis.answer_spans:
            raise ValueError("不完整结论须有已涉及要点和相关学生原文，也须有缺失要点且无矛盾。")
    elif diagnosis.verdict == "incorrect":
        if not contradicted or not diagnosis.answer_spans:
            raise ValueError("错误结论必须包含矛盾要点及对应的学生原文。")
    elif missing or contradicted or addressed:
        raise ValueError("无法判断时不能断言要点已涉及、缺失或矛盾。")
