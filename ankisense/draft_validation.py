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
