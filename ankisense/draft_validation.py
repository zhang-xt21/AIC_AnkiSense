"""Validate each planned candidate without weakening source-quote checks."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from pydantic import ValidationError

from .evidence import validate_card
from .schemas import PlannedCardDraft, SourceBlock
from .source_quotes import align_model_quotes


@dataclass
class DraftSelection:
    cards: list[PlannedCardDraft]
    rejected_cards: list[dict] = field(default_factory=list)
    card_positions: list[int] = field(default_factory=list)


@dataclass
class KnowledgeSelection:
    cards: list = field(default_factory=list)
    rejected_cards: list[dict] = field(default_factory=list)


@dataclass
class KnowledgePointSelection:
    points: list = field(default_factory=list)
    rejected_points: list[dict] = field(default_factory=list)
    filtered_points: list[dict] = field(default_factory=list)


def validation_reason(error):
    if isinstance(error, ValidationError):
        errors = error.errors(include_input=False, include_url=False)
        return ("字段格式错误：" + "；".join(
            ".".join(str(part) for part in item["loc"]) + "：" + item["msg"] for item in errors[:4]))[:1500]
    return str(error)[:1500]


def select_knowledge_points(raw_points, blocks, focus_ids, expected_ids=None):
    from .auto_card_models import KnowledgePoint
    from .card_quality import validate_knowledge_points
    from .learning_policy import non_learning_reason
    if not isinstance(raw_points, list) or len(raw_points) > 6:
        raise ValueError("知识点列表格式或数量无效。")
    expected = set(expected_ids) if expected_ids is not None else None
    result = KnowledgePointSelection()
    seen = set()
    identifiers = [raw.get("knowledge_id") for raw in raw_points if isinstance(raw, dict)
                   and isinstance(raw.get("knowledge_id"), str)]
    reserved = set(identifiers)
    def recovery_id(index):
        value = f"repair-{index}"
        while value in reserved:
            value = "r-" + value
        reserved.add(value)
        return value
    for index, raw in enumerate(raw_points, 1):
        identifier = raw.get("knowledge_id") if isinstance(raw, dict) else None
        if expected is not None and (not isinstance(identifier, str) or identifier not in expected):
            continue
        target = identifier if isinstance(identifier, str) and 0 < len(identifier) <= 80 else recovery_id(index)
        try:
            if not isinstance(identifier, str) or identifiers.count(identifier) > 1:
                target = recovery_id(index) if expected is None else target
                raise ValueError("知识点编号缺失、无效或重复。")
            seen.add(identifier)
            point = KnowledgePoint.model_validate(align_model_quotes(raw, blocks))
            validate_knowledge_points([point], blocks)
            if not {item.block_id for item in point.evidence}.intersection(focus_ids):
                raise ValueError("知识点必须引用本批原文。")
        except (ValidationError, ValueError, TypeError) as error:
            preview = json.dumps(raw, ensure_ascii=False, indent=2)
            result.rejected_points.append({"index": index, "knowledge_id": target,
                "reason": validation_reason(error), "draft_preview": preview[:12000],
                "preview_truncated": len(preview) > 12000})
        else:
            reason = non_learning_reason(point.concept, point.statement)
            if reason:
                result.filtered_points.append({"knowledge_id": identifier, "reason": reason,
                                               "point": point.model_dump()})
            else:
                result.points.append(point)
    if expected is not None:
        for identifier in sorted(expected - seen):
            if not any(item["knowledge_id"] == identifier for item in result.rejected_points):
                result.rejected_points.append({"knowledge_id": identifier, "reason": "模型未返回指定知识点，或返回了其他编号。",
                                               "draft_preview": "{}", "preview_truncated": False})
    return result


def select_knowledge_drafts(raw_cards, blocks, points) -> KnowledgeSelection:
    """Isolate malformed candidates while preserving their knowledge identity."""
    from .auto_card_models import KnowledgeCard
    if not isinstance(raw_cards, list) or len(raw_cards) > 6:
        raise ValueError("模型返回的知识卡片列表格式或数量无效。")
    expected = {point.knowledge_id for point in points}
    seen = set()
    result = KnowledgeSelection()
    for index, raw in enumerate(raw_cards, 1):
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        identifier = data.get("knowledge_id") if isinstance(data, dict) else None
        try:
            if identifier not in expected or identifier in seen:
                raise ValueError("候选引用了未知或重复的知识点编号。")
            seen.add(identifier)
            item = KnowledgeCard.model_validate(align_model_quotes(data, blocks))
            validate_card(item.card, blocks)
        except (ValidationError, ValueError, TypeError) as error:
            rejected = rejected_draft(data.get("card", data) if isinstance(data, dict) else data,
                                      index, validation_reason(error))
            rejected["knowledge_id"] = identifier if isinstance(identifier, str) and identifier in expected else None
            result.rejected_cards.append(rejected)
        else:
            result.cards.append(item)
    for identifier in sorted(expected - seen):
        result.rejected_cards.append({**rejected_draft({}, len(raw_cards) + 1, "模型未返回该知识点的候选卡片。"),
                                      "knowledge_id": identifier})
    return result


def rejected_draft(raw, index: int, reason: str) -> dict:
    """Keep bounded, inert text for inspection; never save a bad learning card."""
    data = raw.model_dump() if hasattr(raw, "model_dump") else raw
    question = data.get("question") if isinstance(data, dict) else None
    preview = json.dumps(data, ensure_ascii=False, indent=2)
    return {"index": index, "question": question[:2000] if isinstance(question, str) else "未提供有效题目",
            "reason": reason, "draft_preview": preview[:12000], "preview_truncated": len(preview) > 12000}


def select_planned_drafts(raw_cards, blocks: list[SourceBlock], count: int, *, positions=None) -> DraftSelection:
    if not isinstance(raw_cards, list) or len(raw_cards) > count or len(raw_cards) > 6:
        raise ValueError("模型返回的卡片列表格式或数量不符合本批计划。")
    result = DraftSelection(cards=[])
    for offset, raw in enumerate(raw_cards):
        index = positions[offset] if positions else offset + 1
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        try:
            draft = PlannedCardDraft.model_validate(align_model_quotes(data, blocks))
            validate_card(draft, blocks)
        except ValidationError:
            # Pydantic errors can contain whole inputs. Keep only a fixed reason.
            result.rejected_cards.append(rejected_draft(data, index, "卡片字段格式、评分要点或配图用途不符合要求，请核对候选内容。"))
        except ValueError as error:
            result.rejected_cards.append(rejected_draft(data, index, str(error)))
        else:
            result.cards.append(draft)
            result.card_positions.append(index)
    return result
