"""Strict, source-grounded contracts for the automatic card pipeline."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schemas import CardDraft, Diagnosis, Evidence, StrictModel


class KnowledgePoint(StrictModel):
    knowledge_id: str = Field(min_length=1, max_length=80)
    concept: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=4000)
    conditions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=12)
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class KnowledgeBatch(StrictModel):
    # Some source windows contain only headings or insufficient evidence.
    points: list[KnowledgePoint] = Field(max_length=6)

    @model_validator(mode="after")
    def unique_knowledge_ids(self):
        ids = [point.knowledge_id for point in self.points]
        if len(ids) != len(set(ids)):
            raise ValueError("知识点编号必须唯一。")
        return self


class KnowledgeCard(StrictModel):
    knowledge_id: str = Field(min_length=1, max_length=80)
    card: CardDraft


class KnowledgeCardBatch(StrictModel):
    cards: list[KnowledgeCard] = Field(max_length=6)

    @model_validator(mode="after")
    def unique_knowledge_ids(self):
        ids = [item.knowledge_id for item in self.cards]
        if len(ids) != len(set(ids)):
            raise ValueError("同一知识点只能生成一张卡片。")
        return self


QualityIssueCode = Literal[
    "unsupported_answer",
    "unsupported_rubric",
    "missing_condition",
    "ambiguous_question",
    "multiple_concepts",
    "duplicate_card",
    "source_conflict",
    "insufficient_evidence",
    "irrelevant_content",
    "reference_answer_mismatch",
    "review_protocol_error",
    "minimal_answer_mismatch",
]


class QualityIssue(StrictModel):
    code: QualityIssueCode
    severity: Literal["warning", "error"]
    reason: str = Field(min_length=1, max_length=1500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    suggestion: str = Field(default="", max_length=2000)


class CardQualityReport(StrictModel):
    verdict: Literal["clear", "needs_review"]
    issues: list[QualityIssue] = Field(max_length=32)
    supported_point_ids: list[str] = Field(max_length=12)
    # New visual reviews retain the independently validated whole-card checks.
    # Older quality receipts remain readable without claiming this extra check.
    review_checks: dict | None = None
    reference_answer_check: Diagnosis | None = None
    minimal_answer_check: dict | None = None

    @model_validator(mode="after")
    def consistent_verdict(self):
        if len(self.supported_point_ids) != len(set(self.supported_point_ids)):
            raise ValueError("质量检查包含重复评分要点编号。")
        if self.verdict == "clear" and self.issues:
            raise ValueError("无疑点结论不能包含质量问题。")
        if self.verdict == "needs_review" and not self.issues:
            raise ValueError("待审核结论必须说明质量疑点。")
        return self


class IndexedCardQuality(StrictModel):
    card_index: int = Field(ge=0, le=5, strict=True)
    report: CardQualityReport


class CardQualityBatch(StrictModel):
    reviews: list[IndexedCardQuality] = Field(min_length=1, max_length=6)
