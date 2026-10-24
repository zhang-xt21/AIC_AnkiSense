"""Validate each planned candidate without weakening source-quote checks."""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from pydantic import ValidationError

from .evidence import validate_card
from .schemas import PlannedCardDraft, SourceBlock


@dataclass
class DraftSelection:
    cards: list[PlannedCardDraft]
    rejected_cards: list[dict] = field(default_factory=list)
    card_positions: list[int] = field(default_factory=list)


@dataclass
class KnowledgeSelection:
    cards: list = field(default_factory=list)
    rejected_cards: list[dict] = field(default_factory=list)


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
            item = KnowledgeCard.model_validate(data)
            validate_card(item.card, blocks)
        except (ValidationError, ValueError, TypeError):
            rejected = rejected_draft(data.get("card", data) if isinstance(data, dict) else data,
                                      index, "候选的字段或连续原文引用未通过校验。")
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
            draft = PlannedCardDraft.model_validate(data)
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
