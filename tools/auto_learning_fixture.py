"""Isolated end-to-end fixture: real queue/approval, deterministic local models."""

import asyncio
import runpy
from pathlib import Path

import uvicorn

fixture = runpy.run_path(str(Path(__file__).with_name("workbench_fixture.py")))
from ankisense.app import create_app
from ankisense.llm import AIError, AIService, CardReviewResult, Settings
from ankisense.schemas import CardDraft, DraftBatch

store = fixture["app"].state.store
original = store.get("cards", "fixture-pdf-stack")
for source in store.all("source_pages"):
    if source["document_id"] == original["document_id"]:
        store.put("source_pages", {**source, "status": "pending", "reviewed_at": None})


class OfflineAI(AIService):
    async def generate_drafts(self, blocks, count, **kwargs):
        return DraftBatch(cards=[CardDraft(
            question=f"{label}: How does a stack remove elements?",
            answer="The last inserted element is removed first (LIFO).",
            concept="Stack / LIFO", kind="basic",
            rubric=[{"point_id": "fixture_lifo", "text": "Explain last in, first out."}],
            evidence=[{"block_id": blocks[0].block_id, "quote": blocks[0].text}],
        ) for label in ["Pass", "Needs evidence", "Offline failure"][:count]])

    async def review_card(self, card, blocks):
        await asyncio.sleep(4)
        if "Offline failure" in card.question:
            raise AIError("离线测试模拟模型超时，请稍后重试。")
        uncertain = "Needs evidence" in card.question
        quote = {"block_id": card.evidence[0].block_id, "exact_quote": card.evidence[0].quote}
        return CardReviewResult(
            minimal_answer=card.answer, minimal_answer_citations=[quote],
            decision="uncertain" if uncertain else "pass", question_answerable=not uncertain,
            answer_relevant=True, answer_supported=True, unsupported_claims=[],
            question_coverage="complete", missing_question_parts=[],
            answer_citations=[quote],
            rubric_checks=[{"point_id": point.point_id, "verdict": "supported", "scope": "required", "scope_reason": "回答所问。", **quote} for point in card.rubric],
            issues=["离线测试：题目条件需要补充。"] if uncertain else [],
        )


settings = Settings(mode="cloud", api_key="offline-fixture-key", base_url="https://offline.invalid", model="offline-fixture")
app = create_app(data_dir=fixture["DATA"], settings=settings, ai_service=OfflineAI(settings))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8976, log_level="warning")
