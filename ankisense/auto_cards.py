"""Recoverable document-wide card generation. Machine checks never approve cards."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field, model_validator

from .card_quality import rule_issues, validate_quality_report
from .evidence import validate_card
from .figure_vision import FigureCheckRequest
from .llm import AIError
from .ocr_calibration import analyze_page
from .prompts import AUTO_CARD_PROMPT_VERSION
from .schemas import (CardDraft, ImageRecommendationAdoptRequest,
                      ImageRecommendationRequest, SourceBlock, StrictModel)
from .sources import require_current_reviewed
from .source_policy import source_usable
from .source_calibration_models import CalibrationRequest


PIPELINE_VERSION = "auto-cards-v1"
ACTIVE = {"pending", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class AutoCardRequest(StrictModel):
    max_cards: int = Field(default=30, ge=1, le=600, strict=True)
    image_mode: Literal["off", "recommend", "checked", "source_page"] = "recommend"
    source_mode: Literal["manual", "visual"] = "manual"
    autonomous: bool = False

    @model_validator(mode="after")
    def manual_limit(self):
        if not self.autonomous and self.max_cards > 60:
            raise ValueError("手动批次最多 60 张；整份讲义请使用自动导入。")
        return self


class QualityCheckRequest(StrictModel):
    expected_version: int = Field(ge=1, strict=True)


def batch_blocks(blocks):
    """Keep page/paragraph order and stay below the existing model input limits."""
    batches, batch, size = [], [], 0
    for block in blocks:
        if len(block.text) > 16000:
            raise HTTPException(422, "单个原文片段过长，请先拆分材料。")
        if batch and (len(batch) == 3 or size + len(block.text) > 9000):
            batches.append(batch)
            batch, size = [], 0
        batch.append(block)
        size += len(block.text)
    if batch:
        batches.append(batch)
    return batches


def context_blocks(blocks, focus):
    indices = {block.block_id: index for index, block in enumerate(blocks)}
    selected = {indices[b.block_id] for b in focus}
    size = sum(len(b.text) for b in focus)
    for neighbour in (min(selected) - 1, max(selected) + 1):
        if 0 <= neighbour < len(blocks) and len(selected) < 6 and size + len(blocks[neighbour].text) <= 16000:
            selected.add(neighbour)
            size += len(blocks[neighbour].text)
    return [blocks[index] for index in sorted(selected)]


class AutoCardService:
    def __init__(self, store, storage: Path, ai, settings, recommendations, image_checks, calibration=None):
        self.store, self.storage = store, storage
        self.ai, self.settings = ai, settings
        self.recommendations, self.image_checks = recommendations, image_checks
        self.calibration = calibration
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.router = APIRouter()
        self.router.add_api_route("/api/documents/{document_id}/auto-card-jobs", self.list, methods=["GET"])
        self.router.add_api_route("/api/documents/{document_id}/auto-card-jobs", self.start, methods=["POST"])
        self.router.add_api_route("/api/auto-card-jobs/{job_id}", self.get, methods=["GET"])
        self.router.add_api_route("/api/auto-card-jobs/{job_id}/retry", self.retry, methods=["POST"])
        self.router.add_api_route("/api/auto-card-jobs/{job_id}/cancel", self.cancel, methods=["POST"])
        self.router.add_api_route("/api/cards/{card_id}/auto-quality-check", self.recheck_card, methods=["POST"])

    def recover(self):
        for job in self.store.all("auto_card_jobs"):
            if job["status"] in ACTIVE:
                try:
                    same_config = job.get("config") == self._configuration()
                except HTTPException:
                    same_config = False
                if job.get("options", {}).get("autonomous") and same_config:
                    self._launch(job["id"])
                    continue
                job.update(status="failed", error="服务重启中断了任务；已完成的卡片保留，可继续处理。", message="任务中断，可重试。", updated_at=now())
                self.store.put("auto_card_jobs", job)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _job(self, job_id):
        job = self.store.get("auto_card_jobs", job_id)
        if not job:
            raise HTTPException(404, "自动制卡任务不存在。")
        return job

    @staticmethod
    def _public(job):
        keys = ("id", "document_id", "status", "stage", "progress", "card_ids", "issues",
                "message", "error", "created_at", "updated_at", "options", "skipped_count",
                "knowledge_count", "calibration", "pipeline_version", "prompt_version")
        return {key: job.get(key) for key in keys}

    def list(self, document_id: str):
        if not self.store.get("documents", document_id):
            raise HTTPException(404, "材料不存在。")
        return {"jobs": [self._public(job) for job in reversed(self.store.all("auto_card_jobs")) if job["document_id"] == document_id]}

    def get(self, job_id: str):
        return self._public(self._job(job_id))

    async def recheck_card(self, card_id: str, body: QualityCheckRequest):
        self._configuration()
        original = self.store.get("cards", card_id)
        if not original:
            raise HTTPException(404, "卡片不存在。")
        if original["version"] != body.expected_version:
            raise HTTPException(409, "卡片已更新，请载入最新版本再检查。")
        sources = [self.store.get("blocks", identifier) for identifier in dict.fromkeys(e["block_id"] for e in original["evidence"])]
        if any(source is None or source["document_id"] != original["document_id"] for source in sources):
            raise HTTPException(409, "原文引用已失效，请修正引用后重新检查。")
        blocks = [SourceBlock.model_validate(source) for source in sources]
        require_current_reviewed(self.store, blocks)
        card = CardDraft.model_validate({key: original[key] for key in CardDraft.model_fields})
        try:
            report = await self.ai().review_card(card, blocks)
            validate_quality_report(report, card, blocks)
            others = [c for c in self.store.all("cards") if c["id"] != card_id and c["document_id"] == original["document_id"] and not c.get("source_outdated") and c["status"] != "rejected"]
            duplicates = rule_issues(card, blocks, others)
            quality = {**report.model_dump(), "scope": "text", "card_version": original["version"],
                       "source_block_ids": [b.block_id for b in blocks], "prompt_version": AUTO_CARD_PROMPT_VERSION,
                       "checked_at": now(), "repair_attempts": 0}
            if duplicates:
                quality.update(verdict="needs_review", issues=quality["issues"] + [i.model_dump() for i in duplicates])
        except (AIError, ValueError) as error:
            raise HTTPException(422, error.message if isinstance(error, AIError) else "质量检查结果未通过校验。") from None
        def commit():
            current = self.store.get("cards", card_id)
            if current["version"] != original["version"] or current.get("source_outdated"):
                raise HTTPException(409, "检查期间卡片或来源发生变化，请重新核对。")
            require_current_reviewed(self.store, blocks)
            # Checking does not alter a human's approval or content version.
            return [("cards", {**current, "auto_quality": quality})]
        self.store.mutate(commit)
        return self.store.get("cards", card_id)

    def _capture(self, document_id, autonomous=False):
        document = self.store.get("documents", document_id)
        if not document:
            raise HTTPException(404, "材料不存在。")
        pages = sorted((p for p in self.store.all("source_pages") if p["document_id"] == document_id), key=lambda p: p["page"])
        ids = {identifier for page in pages for identifier in page["block_ids"]} if pages else None
        blocks = [SourceBlock.model_validate(block) for block in self.store.all("blocks")
                  if block["document_id"] == document_id and (ids is None or block["block_id"] in ids)]
        blocks.sort(key=lambda b: (b.page, int(b.block_id.split("-b")[-1].split("-r")[0]) if "-b" in b.block_id and b.block_id.split("-b")[-1].split("-r")[0].isdigit() else 0, b.block_id))
        reviewed_pages = {p["page"] for p in pages if source_usable(p)}
        eligible = [b for b in blocks if autonomous or not pages or b.page in reviewed_pages]
        calibration = [{"page": p["page"], "revision": p["revision"], **analyze_page(p)} for p in pages]
        issues = [issue for report in calibration if report["status"] != "ready" for issue in report["issues"]]
        for report in calibration:
            if report["status"] != "ready" and not report["issues"]:
                issues.append({"code": "source_unconfirmed", "severity": "warning", "page": report["page"], "reason": "本页原文尚未确认，请检查自动校准结果并保存校对。", "suggestion": "确认本页后继续任务。"})
        manifest = [[p["page"], p["revision"], source_usable(p) or (p["status"] == "empty" and bool(p.get("reviewed_at"))), p["block_ids"]] for p in pages]
        return document, eligible, calibration, issues, digest([manifest, [b.model_dump() for b in blocks]])

    def _configuration(self):
        settings = self.settings()
        if settings.mode != "cloud" or not all((settings.api_key, settings.model, settings.base_url)):
            raise HTTPException(422, "自动制卡需要先在模型设置中配置云端模型。")
        return {"model": settings.model, "endpoint_hash": digest(settings.base_url), "prompt_version": AUTO_CARD_PROMPT_VERSION}

    async def start(self, document_id: str, body: AutoCardRequest):
        async with self.lock:
            config = self._configuration()
            _, blocks, calibration, issues, signature = self._capture(document_id, body.autonomous)
            if not blocks and not any(c["status"] != "ready" for c in calibration):
                raise HTTPException(422, "这份材料没有可制卡的原文。")
            identity = digest([document_id, signature, body.model_dump(), config, PIPELINE_VERSION])
            jobs = self.store.all("auto_card_jobs")
            for job in reversed(jobs):
                if job["identity"] == identity:
                    return self._public(job)
                if job["document_id"] == document_id and job["status"] in ACTIVE:
                    raise HTTPException(409, "这份材料已有自动制卡任务，请等待或取消当前任务。")
            if any(job["status"] in ACTIVE for job in jobs):
                raise HTTPException(429, "已有自动制卡任务在运行，请等待完成后再开始。")
            job = {"id": "autjob-" + uuid4().hex, "document_id": document_id, "identity": identity,
                   "source_signature": signature,
                   "config": config, "options": body.model_dump(), "status": "pending", "stage": "source",
                   "progress": {"completed": 0, "total": len(batch_blocks(blocks))}, "card_ids": [],
                   "batches": [], "issues": issues, "calibration": calibration, "skipped_count": 0,
                   "knowledge_count": 0, "message": "正在准备原文与自动校准结果。", "error": None,
                   "pipeline_version": PIPELINE_VERSION, "prompt_version": AUTO_CARD_PROMPT_VERSION,
                   "created_at": now(), "updated_at": now()}
            self.store.put("auto_card_jobs", job)
            self._launch(job["id"])
            return self._public(job)

    def _launch(self, job_id):
        task = asyncio.create_task(self._run(job_id, self.ai()))
        self.tasks[job_id] = task
        def done(completed):
            if self.tasks.get(job_id) is completed:
                self.tasks.pop(job_id, None)
        task.add_done_callback(done)

    async def retry(self, job_id: str):
        async with self.lock:
            job = self._job(job_id)
            if job["status"] in ACTIVE:
                return self._public(job)
            if job["status"] not in {"failed", "cancelled", "awaiting_confirmation"}:
                raise HTTPException(409, "此任务无需重试；来源变化后请创建新任务。")
            if self._configuration() != job["config"]:
                raise HTTPException(409, "模型或提示版本已变化，请创建新任务。")
            if any(item["status"] in ACTIVE for item in self.store.all("auto_card_jobs")) or job_id in self.tasks:
                raise HTTPException(409, "前一个任务仍在结束，请稍后重试。")
            # Completed batches must still refer to the same eligible sources.
            for batch in job["batches"]:
                sources = [self.store.get("blocks", identifier) for identifier in batch.get("context_block_ids", batch["block_ids"])]
                if any(b is None for b in sources):
                    raise HTTPException(409, "已完成批次的来源失效，请创建新任务。")
                if not job["options"].get("autonomous"):
                    require_current_reviewed(self.store, [SourceBlock.model_validate(b) for b in sources])
            job.update(status="pending", error=None, message="继续处理未完成的批次。", updated_at=now())
            self.store.put("auto_card_jobs", job)
            self._launch(job_id)
            return self._public(job)

    async def cancel(self, job_id: str):
        async with self.lock:
            job = self._job(job_id)
            if job["status"] in ACTIVE:
                job.update(status="cancelled", message="任务已取消；已经保存的卡片保留。", updated_at=now())
                self.store.put("auto_card_jobs", job)
                task = self.tasks.get(job_id)
                if task:
                    task.cancel()
            return self._public(job)

    def _update(self, job_id, **values):
        def build():
            job = self._job(job_id)
            if job["status"] not in ACTIVE:
                raise asyncio.CancelledError()
            job.update(values, updated_at=now())
            return [("auto_card_jobs", job)]
        return self.store.mutate(build)[0][1]

    def _check(self, job_id, blocks, signature):
        job = self._job(job_id)
        if job["status"] not in ACTIVE:
            raise asyncio.CancelledError()
        if not job["options"].get("autonomous"):
            require_current_reviewed(self.store, blocks)
        if self._capture(job["document_id"], job["options"].get("autonomous", False))[-1] != signature:
            raise HTTPException(409, "任务运行时原文发生变化；已完成卡片保留，请基于当前原文重新开始。")

    async def _run(self, job_id, ai):
        if self._job(job_id)["options"].get("autonomous"):
            from .automatic_runner import run_automatic
            await run_automatic(self, job_id, ai)
            return
        try:
            job = self._job(job_id)
            self._update(job_id, status="running")
            if job["options"].get("source_mode") == "visual" and self.calibration:
                pages = sorted((p for p in self.store.all("source_pages") if p["document_id"] == job["document_id"]), key=lambda p: p["page"])
                for page in pages:
                    if source_usable(page) or (page["status"] == "empty" and page.get("reviewed_at")):
                        continue
                    self._update(job_id, stage="source", message=f"正在对照原页校准第 {page['page']} 页。")
                    try:
                        await self.calibration.calibrate(job["document_id"], page["page"], CalibrationRequest(revision=page["revision"]))
                    except HTTPException as error:
                        if error.status_code == 409:
                            raise
                        # Failed image capabilities leave explicit pending sources, not false passes.
                        current = self.store.get("source_pages", page["id"])
                        if current["revision"] != page["revision"]:
                            raise HTTPException(409, "校准期间原文已变化，请刷新后重新开始。")
                        self.store.put("source_pages", {**current, "calibration_issues": ["自动原页校准未完成，请检查模型图片能力，或手动确认。"]})
            document, blocks, calibration, source_issues, signature = self._capture(job["document_id"])
            done_ids = {identifier for batch in job["batches"] for identifier in batch["block_ids"]}
            batches = batch_blocks([b for b in blocks if b.block_id not in done_ids])
            total = len(job["batches"]) + len(batches)
            old_issues = [i for i in job["issues"] if i.get("stage") != "source" and not i.get("page")]
            source_issues = [{**i, "stage": "source"} for i in source_issues]
            self._update(job_id, status="running", stage="source", calibration=calibration,
                         identity=digest([job["document_id"], signature, job["options"], job["config"], PIPELINE_VERSION]),
                         issues=source_issues + old_issues, progress={"completed": len(job["batches"]), "total": total})
            for focus in batches:
                batch = context_blocks(blocks, focus)
                job = self._job(job_id)
                remaining = job["options"]["max_cards"] - len(job["card_ids"])
                if remaining <= 0:
                    self._update(job_id, issues=job["issues"] + [{"code": "card_limit", "severity": "warning", "reason": "达到本次卡片数量上限，剩余原文未处理。", "suggestion": "提高上限后创建新任务。"}])
                    break
                self._check(job_id, batch, signature)
                self._update(job_id, stage="knowledge", message="正在提取有原文依据的知识点。")
                points = (await ai.extract_knowledge(batch, focus_block_ids=[b.block_id for b in focus])).points
                self._check(job_id, batch, signature)
                selected = points[:remaining]
                if selected:
                    self._update(job_id, stage="generation", message=f"正在生成 {len(selected)} 张知识点卡片。")
                    generated_cards = []
                    for offset in range(0, len(selected), 3):
                        generated = await ai.generate_knowledge_cards(batch, selected[offset:offset + 3])
                        self._check(job_id, batch, signature)
                        generated_cards.extend(generated.cards)
                    records, quality_issues, skipped = [], [], 0
                    for item in generated_cards:
                        self._update(job_id, stage="quality", message="正在独立核对答案、评分点和引用。")
                        card = item.card
                        validate_card(card, batch)
                        report = await ai.review_card(card, batch)
                        validate_quality_report(report, card, batch)
                        self._check(job_id, batch, signature)
                        repairs, previous = 0, None
                        if report.issues:
                            try:
                                repaired = await ai.repair_card(card, batch, report)
                                self._check(job_id, batch, signature)
                                validate_card(repaired, batch)
                                checked = await ai.review_card(repaired, batch)
                                validate_quality_report(checked, repaired, batch)
                                self._check(job_id, batch, signature)
                                repairs = 1
                                previous = {"card": card.model_dump(), "quality": report.model_dump()}
                                card, report = repaired, checked
                            except AIError:
                                # Failed repair does not turn the original into a passed card.
                                repairs = 1
                        existing = [c for c in self.store.all("cards") if c["document_id"] == document["id"] and not c.get("source_outdated") and c.get("status") != "rejected"]
                        existing.extend(records)
                        duplicates = rule_issues(card, batch, existing)
                        if duplicates:
                            skipped += 1
                            quality_issues.extend({**i.model_dump(), "stage": "quality"} for i in duplicates)
                            continue
                        identifier = "autcard-" + uuid4().hex
                        quality = {**report.model_dump(), "scope": "text", "card_version": 1, "source_block_ids": [b.block_id for b in batch],
                                   "prompt_version": AUTO_CARD_PROMPT_VERSION, "repair_attempts": repairs,
                                   "checked_at": now(), "repair_history": previous}
                        records.append({**card.model_dump(), "id": identifier, "document_id": document["id"],
                                        "status": "draft", "version": 1, "is_demo": document["is_demo"],
                                        "created_at": now(), "auto_job_id": job_id,
                                        "knowledge_id": f"{job_id}:b{len(job['batches']) + 1}:{item.knowledge_id}",
                                        "auto_quality": quality})
                        quality_issues.extend({**issue.model_dump(), "stage": "quality", "card_id": identifier} for issue in report.issues)
                else:
                    records, quality_issues, skipped = [], [{"code": "no_knowledge", "severity": "warning", "stage": "knowledge", "reason": "本批原文没有提取到可独立制卡的知识点。", "block_ids": [b.block_id for b in batch]}], 0
                if len(selected) < len(points):
                    quality_issues.append({"code": "card_limit", "severity": "warning", "reason": "本批部分知识点超过本次卡片上限，未生成。"})
                # Commit cards and the checkpoint together: interrupted batches cannot duplicate cards.
                def commit():
                    self._check(job_id, batch, signature)
                    current = self._job(job_id)
                    checkpoint = {"block_ids": [b.block_id for b in focus], "context_block_ids": [b.block_id for b in batch],
                                  "knowledge": [p.model_dump() for p in points], "card_ids": [c["id"] for c in records]}
                    current.update(batches=current["batches"] + [checkpoint], card_ids=current["card_ids"] + checkpoint["card_ids"],
                                   issues=current["issues"] + quality_issues, skipped_count=current["skipped_count"] + skipped,
                                   knowledge_count=current["knowledge_count"] + len(points),
                                   progress={"completed": len(current["batches"]) + 1, "total": total}, updated_at=now())
                    return [("cards", c) for c in records] + [("auto_card_jobs", current)]
                self.store.mutate(commit)
            # Attach only to unchanged, newly generated drafts. Retry can resume image preparation too.
            job = self._job(job_id)
            for card_id in job["card_ids"]:
                card = self.store.get("cards", card_id)
                if (card and not card.get("image_result") and card["version"] == 1
                        and card["status"] == "draft" and not card.get("source_outdated")):
                    self._update(job_id, stage="images", message="正在准备讲义配图。")
                    await self._images(job_id, card, job["options"]["image_mode"], signature)
            self._check(job_id, blocks, signature)
            awaiting = any(c["status"] != "ready" for c in calibration)
            count = len(self._job(job_id)["card_ids"])
            self._update(job_id, status="awaiting_confirmation" if awaiting else "completed", stage="source" if awaiting else "complete",
                         message=f"已保存 {count} 张卡片草稿。" + ("请确认待处理原文，再继续剩余页面。" if awaiting else "请查看质量疑点并核对批准。"))
        except asyncio.CancelledError:
            job = self._job(job_id)
            if job["status"] in ACTIVE:
                job.update(status="failed", error="任务被中断；已完成结果保留，可以重试。", message="任务中断。", updated_at=now())
                self.store.put("auto_card_jobs", job)
        except (AIError, HTTPException, ValueError) as error:
            job = self._job(job_id)
            if job["status"] in ACTIVE:
                message = error.message if isinstance(error, AIError) else (error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else "模型结果未通过校验，请重试。")
                job.update(status="outdated" if isinstance(error, HTTPException) and error.status_code == 409 else "failed",
                           error=message, message="任务停止，已完成的卡片保留。", updated_at=now())
                self.store.put("auto_card_jobs", job)
        except Exception:
            job = self._job(job_id)
            if job["status"] in ACTIVE:
                job.update(status="failed", error="自动制卡未完成；已保存结果保留，请重试。", message="任务停止。", updated_at=now())
                self.store.put("auto_card_jobs", job)

    async def _images(self, job_id, card, mode, signature):
        result = {"status": "skipped", "message": "本次未启用配图。"}
        try:
            if mode != "off" and card["document_id"].startswith("pdf-"):
                recommendation = await self.recommendations.start(card["id"], ImageRecommendationRequest(card_version=card["version"]))
                for _ in range(330):
                    self._check(job_id, [], signature)
                    recommendation = self.recommendations.get(recommendation["id"])
                    if recommendation["status"] not in ACTIVE:
                        break
                    await asyncio.sleep(1)
                if recommendation["status"] != "completed":
                    result = {"status": "needs_review", "message": "配图推荐未完成，可在工作台重新推荐。", "job_id": recommendation["id"]}
                else:
                    candidates = recommendation.get("candidates", [])
                    result = {"status": "recommended", "message": "配图候选已准备，请在工作台核对。", "job_id": recommendation["id"], "candidate_ids": [c["id"] for c in candidates]}
                    safe = [c for c in candidates if c.get("kind") != "page" and not c.get("quality_flags")]
                    if mode == "checked" and len(safe) == 1:
                        candidate = safe[0]
                        checked = await self.image_checks.check(card["id"], FigureCheckRequest(job_id=recommendation["id"], candidate_id=candidate["id"], card_version=card["version"], side="answer"))
                        if (checked["classification"]["label"] in {"diagram", "table", "chart", "formula", "photo"}
                                and checked["relation"] == "supported" and checked["completeness"] == "complete" and checked["legibility"] == "clear"):
                            media = await self.recommendations.adopt(card["id"], ImageRecommendationAdoptRequest(job_id=recommendation["id"], candidate_id=candidate["id"], card_version=card["version"]))
                            result.update(status="attached", message="已将检查通过的讲义图附在答案侧，仍需核对批准。", check_id=checked["id"])
                            card = {**card, "images": [{"media_id": media["id"], "side": "answer", "caption": f"讲义第 {candidate['page']} 页"}], "version": card["version"] + 1}
                            card["auto_quality"] = {**card["auto_quality"], "card_version": card["version"]}
                        else:
                            result.update(status="needs_review", message="配图检查发现疑点或无法确定，请人工选择。", check=checked)
                    elif mode == "checked":
                        result.update(status="needs_review", message="没有唯一且无裁剪疑点的候选，请人工选择。")
            elif mode != "off":
                result["message"] = "纯文本材料没有讲义图片，可继续使用文字卡片。"
        except HTTPException as error:
            if error.status_code == 409:
                raise
            result = {"status": "needs_review", "message": "配图或视觉检查暂不可用，可在工作台继续处理。"}
        except (ValueError, AIError):
            result = {"status": "needs_review", "message": "本次配图未通过检查，可在工作台继续处理。"}
        def commit():
            self._check(job_id, [], signature)
            old = self.store.get("cards", card["id"])
            expected = card["version"] - (1 if result["status"] == "attached" else 0)
            if old["version"] != expected or old["status"] != "draft":
                return []  # User edits win over background image preparation.
            return [("cards", {**card, "image_result": result})]
        self.store.mutate(commit)
