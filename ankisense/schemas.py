import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceBlock(StrictModel):
    block_id: str
    document_id: str
    page: int = Field(ge=1)
    text: str = Field(min_length=1)


class RubricPoint(StrictModel):
    point_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1000)


class Evidence(StrictModel):
    block_id: str
    quote: str = Field(min_length=1, max_length=4000)


class CardDraft(StrictModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=4000)
    concept: str = Field(min_length=1, max_length=100)
    kind: Literal["basic", "remediation"] = "basic"
    rubric: list[RubricPoint] = Field(min_length=1, max_length=12)
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class DraftBatch(StrictModel):
    cards: list[CardDraft] = Field(min_length=1, max_length=6)


class ImageIntent(StrictModel):
    need: Literal["none", "helpful", "required"] = "none"
    reason: str = Field(default="", max_length=300)
    block_ids: list[str] = Field(default_factory=list, max_length=8)


class PlannedCardDraft(CardDraft):
    # Kept separate from CardDraft so older card editors and exports retain
    # their existing contract. Generation predicts a need, never an image URL.
    image_intent: ImageIntent = Field(default_factory=ImageIntent)

    @model_validator(mode="after")
    def cited_image_sources(self):
        intent = self.image_intent
        cited = {item.block_id for item in self.evidence}
        if (len(set(intent.block_ids)) != len(intent.block_ids)
                or not set(intent.block_ids) <= cited
                or (intent.need != "none" and (not intent.block_ids or not intent.reason))):
            raise ValueError("配图用途必须说明理由，且只能指向本卡明确引用的原文片段。")
        return self


class PlannedDraftBatch(StrictModel):
    cards: list[PlannedCardDraft] = Field(min_length=0, max_length=6)


class VerifiedImageContext(StrictModel):
    side: Literal["question", "answer"]
    page: int = Field(ge=1)
    description: str = Field(max_length=1800)


class CardReviewInput(CardDraft):
    # Only constructed server-side from current, successful visual receipts.
    verified_images: list[VerifiedImageContext] = Field(default_factory=list, max_length=4)


class Diagnosis(StrictModel):
    verdict: Literal["correct", "incomplete", "incorrect", "uncertain"]
    addressed_point_ids: list[str] = Field(default_factory=list, description="回答中有正确相关内容、至少部分涉及的评分要点；不表示要点已完整满足。")
    missing_point_ids: list[str]
    contradicted_point_ids: list[str]
    answer_spans: list[str]
    source_block_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1500)


class GenerateRequest(StrictModel):
    block_ids: list[str] = Field(min_length=1, max_length=6)
    count: int = Field(default=3, ge=1, le=6)


class CardImage(StrictModel):
    media_id: str = Field(pattern=r"^img-[0-9a-f]{32}$")
    side: Literal["question", "answer"] = "answer"
    caption: str = Field(default="", max_length=500)


class CardUpdate(CardDraft):
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    status: Literal["draft", "approved", "rejected"] = "draft"
    # Older clients omit this field. Only an explicit list replaces attachments.
    images: list[CardImage] | None = Field(default=None, max_length=4)


class SourcePageImageRequest(StrictModel):
    document_id: str = Field(min_length=1, max_length=100)
    page: int = Field(ge=1, strict=True)
    revision: int = Field(ge=1, strict=True)


class ImageRecommendationRequest(StrictModel):
    card_version: int = Field(ge=1, strict=True)


class ImageRecommendationAdoptRequest(ImageRecommendationRequest):
    job_id: str = Field(pattern=r"^figjob-[0-9a-f]{32}$")
    candidate_id: str = Field(pattern=r"^fig-[0-9a-f]{32}$")
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)

    @field_validator("bbox")
    @classmethod
    def valid_crop(cls, value):
        if value is not None and (not all(math.isfinite(v) and 0 <= v <= 1 for v in value)
                                  or value[2] - value[0] < .005 or value[3] - value[1] < .005):
            raise ValueError("裁剪范围须在当前页内，且宽高至少为整页的 0.5%。")
        return value


class AttemptRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    card_id: str
    answer: str = Field(max_length=4000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class TextDocumentRequest(StrictModel):
    title: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=20, max_length=50000)


class SourceRevisionRequest(StrictModel):
    revision: int = Field(ge=1)


class SourceCorrectionRequest(SourceRevisionRequest):
    # Preserve entered characters; an empty string explicitly confirms a blank page.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    text: str = Field(max_length=50000)
