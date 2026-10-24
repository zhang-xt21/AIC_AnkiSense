"""Resumable status, but deliberately non-resuming, document drafting jobs.

The local plan decides bounded batches. Each batch has one model request; cards
and progress are committed together before their independent review is queued.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .evidence import validate_card
from .draft_validation import rejected_draft, select_planned_drafts
from .generation_plan import build_plan, public_plan
from .llm import AIError
from .schemas import SourceBlock
from .sources import require_current_source
from .outline import coverage_report, duplicate_question


ACTIVE_STATUSES = {"queued", "running"}
PUBLIC_FIELDS = {
    "id", "document_id", "status", "target_count", "generated_count",
    "completed_batches", "total_batches", "card_ids", "error", "warnings",
    "created_at", "updated_at", "finished_at", "stop_requested", "current_batch",
    "source_fingerprint", "request_id", "scope", "auto_images", "rejected_count", "rejected_cards",
}


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)
    count: int | None = Field(default=None, ge=1, le=30, strict=True)


class GenerationRequest(PlanRequest):
    document_id: str = Field(min_length=1, max_length=100)
    count: int = Field(ge=1, le=30, strict=True)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    auto_images: bool = Field(default=True, strict=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _question_key(value):
    return "".join(value.split()).casefold()


class GenerationJobService:
    def __init__(self, store, settings_provider, ai_provider, review_saved_card):
        self.store = store
        self.settings_provider = settings_provider
        self.ai_provider = ai_provider
        self.review_saved_card = review_saved_card
        self.running: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.closing = False
        self.router = APIRouter()
        self.router.add_api_route("/api/documents/{document_id}/generation-plan", self.plan, methods=["POST"])
        self.router.add_api_route("/api/generation-jobs", self.start, methods=["POST"])
        self.router.add_api_route("/api/generation-jobs/{job_id}", self.get, methods=["GET"])
        self.router.add_api_route("/api/generation-jobs/{job_id}/stop", self.stop, methods=["POST"])

    def _public(self, job):
        cards = [card for identifier in job.get("card_ids", []) if (card := self.store.get("cards", identifier))]
        topics = job.get("topics", [])
        stale = any(not self.store.get("blocks", topic["block_id"])
                    or ((page := self.store.get("source_pages", f"{job['document_id']}-p{topic['page']}"))
                        and topic["block_id"] not in page.get("block_ids", [])) for topic in topics)
        return {key: value for key, value in job.items() if key in PUBLIC_FIELDS} | {
            "coverage": coverage_report(topics, cards, stale=stale)}

    def list_jobs(self):
        return [self._public(job) for job in self.store.all("generation_jobs")]

    def _get(self, job_id):
        job = self.store.get("generation_jobs", job_id)
        if job is None:
            raise HTTPException(404, "制卡任务不存在。")
        return job

    def get(self, job_id: str):
        return self._public(self._get(job_id))

    def plan(self, document_id: str, body: PlanRequest):
        return public_plan(build_plan(self.store, document_id, body.block_ids, body.count))

    def _update(self, job_id, **changes):
        def build():
            job = self._get(job_id)
            return [("generation_jobs", {**job, **changes, "updated_at": _now()})]
        return self.store.mutate(build)[0][1]

    async def start(self, body: GenerationRequest):
        signature = _hash({"document_id": body.document_id,
                           "block_ids": sorted(set(body.block_ids)) if body.block_ids else None,
                           "count": body.count, "source_fingerprint": body.source_fingerprint,
                           "auto_images": body.auto_images})
        job_id = "genjob-" + _hash(body.request_id)[:32]
        async with self.lock:
            previous = self.store.get("generation_jobs", job_id)
            if previous:
                if previous.get("request_id") != body.request_id or previous.get("request_signature") != signature:
                    raise HTTPException(409, "同一请求编号不能用于不同的制卡范围或数量。请重新开始。")
                return self._public(previous)
            if self.closing:
                raise HTTPException(503, "服务正在关闭，请稍后重新开始制卡。")
            settings = self.settings_provider()
            if settings.mode != "cloud" or not all((settings.api_key, settings.model, settings.base_url)):
                raise HTTPException(422, "请先在模型设置中配置云端模型，再开始自动制卡。")
            document = self.store.get("documents", body.document_id)
            if not document:
                raise HTTPException(404, "讲义不存在。")
            if document.get("is_demo"):
                raise HTTPException(422, "内置演示讲义不调用云端制卡；请导入自己的讲义。")
            active = [job for job in self.store.all("generation_jobs") if job.get("status") in ACTIVE_STATUSES]
            if any(job["document_id"] == body.document_id for job in active):
                raise HTTPException(409, "这份讲义已有制卡任务，请查看进度或先停止后续批次。")
            if len(active) >= 2:
                raise HTTPException(429, "已有两份讲义正在制卡，请等待其中一份完成。")
            plan = build_plan(self.store, body.document_id, body.block_ids, body.count)
            if plan["source_fingerprint"] != body.source_fingerprint:
                raise HTTPException(409, "讲义内容已更新，请刷新建议数量后重新开始。")
            if not plan["batches"]:
                raise HTTPException(422, "没有可以制卡的原文，请先导入或补充文字。")
            job = {"id": job_id, "request_id": body.request_id, "request_signature": signature,
                   "document_id": body.document_id, "block_ids": body.block_ids,
                   "auto_images": body.auto_images,
                   "source_fingerprint": body.source_fingerprint, "scope": plan["scope"],
                   "status": "queued", "target_count": body.count, "generated_count": 0,
                   "completed_batches": 0, "total_batches": len(plan["batches"]),
                   "card_ids": [], "error": None, "warnings": list(plan.get("warnings", [])),
                   "rejected_count": 0, "rejected_cards": [],
                   "topics": plan.get("topics", []),
                   "created_at": _now(), "updated_at": _now(), "stop_requested": False,
                   "current_batch": 0}
            # Source validation shares the writer transaction with task creation.
            self.store.put_many([("generation_jobs", job)], check=lambda: self._current_plan(job))
            task = asyncio.create_task(self._run(job_id, plan, self.ai_provider()))
            self.running[job_id] = task
            task.add_done_callback(lambda completed: self._done(job_id, completed))
            return self._public(job)

    def _done(self, job_id, task):
        self.running.pop(job_id, None)
        if task.cancelled():
            # A task cancelled before its coroutine starts never enters _run's
            # exception handler. Release the persisted active job in that case.
            job = self._get(job_id)
            if job["status"] in ACTIVE_STATUSES:
                self._update(job_id, status="interrupted", error="制卡任务被中断；已保存的卡片仍保留，不会自动续跑。", finished_at=_now())
        else:
            task.exception()  # Consume even a persistence failure during finalization.

    async def stop(self, job_id: str):
        async with self.lock:
            job = self._get(job_id)
            if job["status"] in ACTIVE_STATUSES:
                job = self._update(job_id, stop_requested=True)
            return self._public(job)

    def _current_plan(self, job):
        try:
            plan = build_plan(self.store, job["document_id"], job.get("block_ids"), job["target_count"])
        except HTTPException as error:
            raise HTTPException(409, "讲义内容或可用页面已更新，本任务已停止后续制卡。") from error
        if plan["source_fingerprint"] != job["source_fingerprint"]:
            raise HTTPException(409, "讲义内容或可用页面已更新，本任务已停止后续制卡。")
        return plan

    def _save_batch(self, job_id, batch_number, drafts, context, requested, *, rejected=None, positions=None):
        def build():
            job = self._get(job_id)
            self._current_plan(job)
            originals = []
            for block in context:
                original = self.store.get("blocks", block.block_id)
                if not original:
                    raise HTTPException(409, "原文片段已更新，本批结果未保存。")
                originals.append(SourceBlock.model_validate(original))
            require_current_source(self.store, originals)
            questions = {_question_key(card["question"]) for card_id in job["card_ids"]
                         if (card := self.store.get("cards", card_id))}
            cards, warnings = [], list(job["warnings"])
            selected = select_planned_drafts(drafts, context, requested, positions=positions)
            failures = list(rejected or []) + selected.rejected_cards
            duplicates = 0
            for position, draft in zip(selected.card_positions, selected.cards):
                try:
                    validate_card(draft, originals)
                except ValueError as error:
                    failures.append(rejected_draft(draft, position, str(error)))
                    continue
                key = _question_key(draft.question)
                if duplicate_question(draft.question, questions):
                    duplicates += 1
                    continue
                questions.add(key)
                cards.append({**draft.model_dump(), "id": str(uuid4()), "document_id": job["document_id"],
                              "status": "draft", "version": 1, "is_demo": False,
                              "created_at": _now(), "generation_job_id": job_id})
            if duplicates:
                warnings.append(f"第 {batch_number} 批跳过 {duplicates} 张问题文字重复的卡片。")
            if failures:
                warnings.append(f"第 {batch_number} 批有 {len(failures)} 张候选未通过逐卡校验，已单独保留原因；其余有效卡片继续保存和检查。")
            if len(cards) < requested:
                warnings.append(f"第 {batch_number} 批计划 {requested} 张，实际保存 {len(cards)} 张；没有为凑数量自动重试。")
            updated = {**job, "generated_count": job["generated_count"] + len(cards),
                       "card_ids": job["card_ids"] + [card["id"] for card in cards],
                       "rejected_count": job.get("rejected_count", 0) + len(failures),
                       "rejected_cards": job.get("rejected_cards", []) + [{**item, "batch": batch_number} for item in sorted(failures, key=lambda item: item["index"])],
                       "completed_batches": batch_number, "warnings": warnings, "updated_at": _now()}
            return [("cards", card) for card in cards] + [("generation_jobs", updated)]
        records = self.store.mutate(build)
        return [record for collection, record in records if collection == "cards"]

    async def _run(self, job_id, plan, ai):
        try:
            self._update(job_id, status="running")
            for number, batch in enumerate(plan["batches"], 1):
                job = self._get(job_id)
                if job["stop_requested"]:
                    break
                self._current_plan(job)
                context = [SourceBlock.model_validate(block) for block in batch["blocks"]]
                count = batch["count"]
                if not (1 <= count <= 6 and 1 <= len(context) <= 6 and sum(len(block.text) for block in context) <= 16000):
                    raise ValueError("制卡计划超出单批限制，请重新规划。")
                self._update(job_id, current_batch=number)
                previous_questions = [card["question"] for card in self.store.all("cards")
                                      if card.get("document_id") == job["document_id"]
                                      and not card.get("source_outdated") and card.get("status") != "rejected"]
                result = await ai.generate_drafts(context, count, allow_fewer=True, avoid_questions=previous_questions)
                drafts = result.cards
                rejected = getattr(result, "rejected_cards", [])
                if len(drafts) + len(rejected) > count:
                    raise ValueError("模型返回的卡片数量超过本批计划；本批未保存。")
                cards = self._save_batch(job_id, number, drafts, context, count, rejected=rejected,
                                         positions=getattr(result, "card_positions", None))
                for card in cards:
                    try:
                        await self.review_saved_card(card)
                    except Exception:
                        job = self._get(job_id)
                        message = "部分卡片已保存，但自动检查未成功排队；可在卡片中点击保存并检查。"
                        if message not in job["warnings"]:
                            self._update(job_id, warnings=job["warnings"] + [message])
            job = self._get(job_id)
            status = "stopped" if job["stop_requested"] else (
                "completed" if job["generated_count"] == job["target_count"] else
                "failed" if not job["generated_count"] and job.get("rejected_count") else "partial")
            error = "返回的候选均未通过逐卡校验，请展开候选记录查看原因。" if status == "failed" else None
            self._update(job_id, status=status, error=error, finished_at=_now())
        except asyncio.CancelledError:
            self._update(job_id, status="interrupted", error="制卡任务被中断；已保存的卡片仍保留，不会自动续跑。", finished_at=_now())
            raise
        except (AIError, ValueError, HTTPException) as error:
            message = error.detail if isinstance(error, HTTPException) else str(error)
            self._fail(job_id, str(message))
        except Exception:
            self._fail(job_id, "制卡任务未完成；已保存的卡片仍保留。请检查服务后重新开始剩余内容。")

    def _fail(self, job_id, error):
        job = self._get(job_id)
        self._update(job_id, status="partial" if job["generated_count"] else "failed", error=error, finished_at=_now())

    async def recover(self):
        for job in self.store.all("generation_jobs"):
            if job.get("status") in ACTIVE_STATUSES and job["id"] not in self.running:
                self._update(job["id"], status="interrupted", error="上次制卡被服务重启中断；已保存的卡片仍保留，不会自动续跑。", finished_at=_now())

    async def shutdown(self):
        self.closing = True
        # Finishing an in-flight request preserves its cards. Closing the app
        # must not start additional paid requests for the remaining batches.
        for job_id in list(self.running):
            job = self._get(job_id)
            if job["status"] in ACTIVE_STATUSES:
                self._update(job_id, stop_requested=True)
        if self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
