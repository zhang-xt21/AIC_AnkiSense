"""Source-bound machine review and atomic approval of unchanged saved cards."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence import validate_card
from .approval import (MACHINE_APPROVAL_POLICY, card_binding_hash, require_required_image,
                       display_card, valid_automatic_approval)
from .automatic_policy import POLICY as AUTOMATIC_POLICY
from .image_auto_review import AutoCheckRequest
from .llm import AIError, CardReviewResult, validate_card_review
from .model_policy import generation_options
from .prompts import CARD_REVIEW_PROMPT_VERSION
from .schemas import CardDraft, CardReviewInput, SourceBlock
from .sources import page_id, source_available


POLICY_VERSION = MACHINE_APPROVAL_POLICY
MAX_PDF_BYTES = 50 * 1024 * 1024


class CardAutoReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CardAutoReviewService:
    def __init__(self, store, storage: Path, settings, ai_provider, image_auto_review):
        self.store, self.storage, self.settings = store, storage, settings
        self.ai_provider = ai_provider
        self.image_auto_review = image_auto_review
        self.lock = asyncio.Lock()
        self.running: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(2)
        self.closing = False
        self.router = APIRouter()
        route = "/api/cards/{card_id}/auto-review"
        self.router.add_api_route(route, self.get, methods=["GET"])
        self.router.add_api_route(route, self.check, methods=["POST"])

    def _capture(self, card_id: str, expected_version: int | None = None) -> dict:
        card = self.store.get("cards", card_id)
        if card is None:
            raise HTTPException(404, "卡片不存在。")
        if expected_version is not None and card.get("version") != expected_version:
            raise HTTPException(409, "卡片版本已更新，请刷新后重新机审。")
        reasons: list[str] = []
        document = self.store.get("documents", card.get("document_id", ""))
        if not document or document.get("is_demo") or not document.get("file_url"):
            reasons.append("整卡机审自动通过仅适用于有原始 PDF 的真实讲义；演示或粘贴文本请人工核对。")
        if card.get("source_outdated"):
            reasons.append("卡片引用的原文已更新，请重新制卡或修订引用。")

        blocks: list[SourceBlock] = []
        try:
            draft = CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields})
            for block_id in dict.fromkeys(item.block_id for item in draft.evidence):
                stored = self.store.get("blocks", block_id)
                if not stored or stored.get("document_id") != card.get("document_id"):
                    raise ValueError("引用片段不存在或来自其他材料。")
                blocks.append(SourceBlock.model_validate(stored))
            validate_card(draft, blocks)
            if len({block.document_id for block in blocks}) != 1:
                raise ValueError("引用必须来自同一份材料。")
        except (KeyError, TypeError, ValueError, ValidationError):
            draft = None
            blocks = []
            reasons.append("题目、答案、评分要点或逐字引用格式无效，请先修订卡片。")

        pages: dict[str, dict | None] = {}
        for page in sorted({block.page for block in blocks}):
            current = self.store.get("source_pages", page_id(card["document_id"], page))
            pages[str(page)] = current
            if (not current or current.get("document_id") != card["document_id"]
                    or current.get("page") != page or not source_available(current)
                    or not current.get("text", "").strip()) or any(
                block.block_id not in current.get("block_ids", []) for block in blocks if block.page == page
            ):
                reasons.append(f"引用的第 {page} 页不是当前可用版本，请更新引用或重新提取原文。")
            elif any(block.text not in current.get("text", "") for block in blocks if block.page == page):
                reasons.append(f"引用的第 {page} 页文字与当前校对内容不一致。")

        pdf_hash = None
        if document and document.get("file_url"):
            pdf_path = self.storage / "uploads" / f"{card['document_id']}.pdf"
            try:
                with pdf_path.open("rb") as stream:
                    content = stream.read(MAX_PDF_BYTES + 1)
            except OSError:
                content = b""
            if not content or len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
                reasons.append("原始 PDF 不存在或已损坏，请重新导入讲义。")
            else:
                pdf_hash = _sha(content)
                imported = sorted(record["page"] for record in self.store.all("source_pages")
                                  if record.get("document_id") == card["document_id"])
                mode = document.get("extraction_mode")
                stored_hash = document.get("pdf_sha256")
                legacy_id_matches = bool(imported and imported == list(range(imported[0], imported[-1] + 1))
                                         and mode in {"auto", "text", "ocr"}
                                         and "pdf-" + _sha(content + f":{imported[0]}:{imported[-1]}:proof-v1:{mode}".encode())[:24]
                                         == card["document_id"])
                pdf_matches_import = stored_hash == pdf_hash if stored_hash else legacy_id_matches
                if not pdf_matches_import:
                    reasons.append("原始 PDF 与导入时的文档标识不一致，请重新导入讲义。")

        try:
            require_required_image(self.store, card)
        except HTTPException as error:
            reasons.append(str(error.detail))

        source_reasons = list(reasons)
        image_bindings = []
        for attachment in card.get("images") or []:
            media_id = attachment.get("media_id", "")
            try:
                image = self.image_auto_review._capture(card_id, media_id)
            except (HTTPException, OSError, ValueError, KeyError):
                image = None
            record = self.store.get("image_auto_reviews", image["review_id"]) if image else None
            current_pass = bool(image and not image["reasons"] and record
                                and record.get("fingerprint") == image["fingerprint"]
                                and record.get("status") == "auto_pass" and not record.get("retryable"))
            if not current_pass:
                reasons.append("配图尚未通过当前版本的独立检查；整卡检查会先自动检查配图。")
            image_bindings.append({"attachment": attachment, "media_id": media_id,
                                   "media_source": image["source"] if image else None,
                                   "image_fingerprint": image["fingerprint"] if image else None,
                                   "image_reasons": image["reasons"] if image else None,
                                   "review_id": record.get("review_id") if record else None,
                                   "review_status": record.get("status") if record else None,
                                   "current_pass": current_pass})

        try:
            binding_hash = card_binding_hash(card)
        except (KeyError, ValueError, TypeError, ValidationError):
            binding_hash = _digest({key: card.get(key) for key in
                                    ("id", "version", "document_id", *CardDraft.model_fields, "images", "image_intent")})
        identity = {
            "policy_version": POLICY_VERSION,
            "prompt_version": CARD_REVIEW_PROMPT_VERSION,
            "card_binding_hash": binding_hash,
            "source_outdated": bool(card.get("source_outdated")),
            "source_reasons": source_reasons,
            "document": {key: document.get(key) for key in ("id", "file_url", "is_demo", "page_count", "pdf_sha256")}
            if document else None,
            "pdf_hash": pdf_hash,
            "source_blocks": [block.model_dump() for block in blocks],
            "source_pages": {page: {key: record.get(key) for key in ("revision", "block_ids", "text")}
                             if record else None for page, record in pages.items()},
            "images": image_bindings,
            "mode": self.settings.mode,
            "model": self.settings.model,
            "endpoint_hash": _sha(self.settings.base_url.encode("utf-8")),
        }
        options = generation_options(self.settings.base_url, self.settings.model)
        if options:
            identity["request_options"] = options
        content_identity = {**identity, "source_reasons": source_reasons,
                            "images": [{key: value for key, value in binding.items()
                                        if key not in {"review_id", "review_status", "current_pass"}}
                                       for binding in image_bindings]}
        fingerprint = _digest(identity)
        return {"fingerprint": fingerprint, "review_id": "cardreview-" + fingerprint,
                "card": card, "draft": draft, "blocks": blocks, "reasons": list(dict.fromkeys(reasons)),
                "source_reasons": source_reasons, "content_fingerprint": _digest(content_identity),
                "card_binding_hash": binding_hash,
                "source_pages": identity["source_pages"],
                "image_bindings": image_bindings, "pdf_hash": pdf_hash}

    def _public(self, record: dict, *, cached: bool = False) -> dict:
        card = display_card(self.store, self.store.get("cards", record.get("card_id", "")))
        return {key: record.get(key) for key in ("review_id", "status", "reasons", "checks", "issues", "checked_at", "card_version")} | {
            "current": True, "cached": cached, "stale": False,
            "card": card, "card_status": card.get("status") if card else None,
            "approval": card.get("approval") if card else None,
            "retryable": bool(record.get("retryable")),
            "notice": "当前版本的内容、引用和配图全部通过后自动批准；有疑点的卡片进入待处理。"}

    def _progress(self, captured):
        card = display_card(self.store, self.store.get("cards", captured["card"]["id"]))
        return {"review_id": None, "status": card.get("review_state", "queued"),
                "reasons": [], "checks": None, "issues": [], "checked_at": None,
                "current": True, "cached": False, "stale": False, "retryable": False,
                "card_version": card["version"], "card": card, "card_status": card["status"], "approval": card.get("approval"),
                "notice": "正在自动检查内容、引用和配图，通过后即可学习。"}

    async def get(self, card_id: str):
        saved = self.store.get("cards", card_id)
        if saved and not saved.get("approval") and valid_automatic_approval(self.store, saved):
            # A projection of the original policy is not a new whole-card receipt.
            return {"review_id": None, "status": "auto_pass", "reasons": [], "checks": None,
                    "issues": [], "checked_at": (saved.get("automatic_evidence") or {}).get("checked_at"),
                    "current": True, "cached": True, "stale": False, "retryable": False,
                    "card_version": saved["version"], "card": saved, "card_status": saved["status"],
                    "approval": None, "approval_method": "automatic", "proof_policy": AUTOMATIC_POLICY,
                    "notice": "当前内容仍通过原自动制卡流程的文字与原页检查；此证明保留原策略，未生成新的整卡机审记录。"}
        captured = await asyncio.to_thread(self._capture, card_id)
        if captured["content_fingerprint"] in self.running:
            return self._progress(captured)
        record = self.store.get("card_auto_reviews", captured["review_id"])
        if (record and record.get("fingerprint") == captured["fingerprint"]
                and record.get("policy_version") == POLICY_VERSION
                and record.get("prompt_version") == CARD_REVIEW_PROMPT_VERSION):
            return self._public(record, cached=True)
        stale = any(item.get("card_id") == card_id for item in self.store.all("card_auto_reviews"))
        interrupted = captured["card"].get("review_state") == "interrupted"
        # Missing image reviews are work the pipeline can perform, not a local
        # blocker. Keep old saved picture cards actionable without forcing a save.
        blockers = list(captured["source_reasons"])
        for image in captured["image_bindings"]:
            blockers.extend(image.get("image_reasons") or [])
            if image["image_fingerprint"] is None:
                blockers.append("卡片配图不可用，请重新添加图片。")
        return {"review_id": None, "status": "needs_review" if blockers or interrupted else "unchecked",
                "reasons": list(dict.fromkeys(blockers)) if blockers else captured["reasons"],
                "checks": None, "issues": [], "checked_at": None,
                "current": False, "cached": False, "stale": stale, "retryable": interrupted,
                "card_version": captured["card"]["version"], "card": display_card(self.store, captured["card"]),
                "card_status": display_card(self.store, captured["card"])["status"],
                "approval": captured["card"].get("approval"),
                "notice": "上次检查被中断，请手动重试。" if interrupted else "自动检查通过后即可学习，有疑点的卡片进入待处理。"}

    async def check(self, card_id: str, body: CardAutoReviewRequest):
        captured = await asyncio.to_thread(self._capture, card_id, body.expected_version)
        task, existing = await self._enqueue(captured, retry=True)
        if existing:
            return existing
        return await asyncio.shield(task)

    async def queue(self, card_id: str, expected_version: int):
        """Return immediately after durable enqueue; never retry a prior failure."""
        captured = await asyncio.to_thread(self._capture, card_id, expected_version)
        task, existing = await self._enqueue(captured, retry=False)
        return existing or self._progress(captured)

    async def _enqueue(self, captured, *, retry):
        async with self.lock:
            if self.closing:
                raise HTTPException(503, "服务正在关闭，请稍后重新检查。")
            key = captured["content_fingerprint"]
            task = self.running.get(key)
            if task is not None:
                return task, None
            existing = self.store.get("card_auto_reviews", captured["review_id"])
            if (existing and existing.get("fingerprint") == captured["fingerprint"]
                    and existing.get("policy_version") == POLICY_VERSION
                    and existing.get("prompt_version") == CARD_REVIEW_PROMPT_VERSION
                    and (not retry or not existing.get("retryable"))):
                return None, self._public(existing, cached=True)
            await asyncio.to_thread(self._mark_progress, captured, "queued")
            task = asyncio.create_task(self._pipeline(captured, retry=retry))
            self.running[key] = task
            def finished(done):
                self.running.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Background errors are visible via state, never unobserved.
            task.add_done_callback(finished)
            return task, None

    def _mark_progress(self, captured, state):
        def records():
            current = self._capture(captured["card"]["id"])
            if current["content_fingerprint"] != captured["content_fingerprint"]:
                raise HTTPException(409, "检查期间卡片或来源已变化，请重新检查。")
            return [("cards", {**current["card"], "review_state": state,
                               "review_task_fingerprint": captured["content_fingerprint"]})]
        self.store.mutate(records)

    async def _pipeline(self, initial, *, retry):
        try:
            return await self._execute(initial, retry=retry)
        except BaseException:
            def interrupted():
                card = self.store.get("cards", initial["card"]["id"])
                if (card and card.get("review_task_fingerprint") == initial["content_fingerprint"]
                        and card.get("review_state") in {"queued", "running"}):
                    card["review_state"] = "interrupted"
                    card.pop("review_task_fingerprint", None)
                    return [("cards", card)]
                return []
            await asyncio.to_thread(self.store.mutate, interrupted)
            raise

    async def _execute(self, initial, *, retry):
        async with self.semaphore:
            captured = await asyncio.to_thread(self._capture, initial["card"]["id"])
            if captured["content_fingerprint"] != initial["content_fingerprint"]:
                raise HTTPException(409, "等待期间卡片或来源已变化，请重新检查。")
            await asyncio.to_thread(self._mark_progress, captured, "running")
            if captured["source_reasons"]:
                return await self._save(captured, "needs_review", captured["source_reasons"], None, [], retryable=False)
            if self.settings.mode != "cloud" or not (self.settings.base_url and self.settings.model and self.settings.api_key):
                return await self._save(captured, "needs_review", ["请先配置云端模型，再发起整卡机审。"], None, [], retryable=True)
            try:
                for image in captured["image_bindings"]:
                    if image["current_pass"]:
                        continue
                    card = captured["card"]
                    previous = await self.image_auto_review.get(card["id"], image["media_id"])
                    if not previous.get("current") or (retry and previous.get("retryable")):
                        await self.image_auto_review.check(card["id"], image["media_id"],
                                                           AutoCheckRequest(card_version=card["version"]))
                current = await asyncio.to_thread(self._capture, captured["card"]["id"])
                if current["content_fingerprint"] != initial["content_fingerprint"]:
                    raise HTTPException(409, "检查期间卡片、配图或来源已变化，请重新检查。")
                captured = current
                if captured["reasons"]:
                    retryable = False
                    reasons = []
                    for image in captured["image_bindings"]:
                        if not image["current_pass"]:
                            result = await self.image_auto_review.get(captured["card"]["id"], image["media_id"])
                            reasons.extend(result["reasons"])
                            retryable |= result.get("retryable", False)
                    return await self._save(captured, "needs_review", reasons or captured["reasons"], None, [], retryable=retryable)
                return await self._run(captured)
            except HTTPException as error:
                if error.status_code == 409:
                    raise
                return await self._save(captured, "needs_review", [str(error.detail)], None, [], retryable=True)
            except Exception:
                return await self._save(captured, "needs_review", ["自动检查未完成，请重试或处理卡片。"], None, [], retryable=True)

    async def _run(self, captured: dict):
        try:
            draft = captured["draft"]
            if captured["image_bindings"]:
                verified_images = []
                for binding in captured["image_bindings"]:
                    record = self.store.get("image_auto_reviews", binding["review_id"])
                    verified_images.append({"side": binding["attachment"].get("side", "answer"),
                                            "page": binding["media_source"]["page"],
                                            "description": (record.get("visual") or {}).get("description", "")})
                draft = CardReviewInput(**draft.model_dump(), verified_images=verified_images)
            result = await self.ai_provider().review_card(draft, captured["blocks"])
            checks = result.model_dump() if hasattr(result, "model_dump") else result
            if not isinstance(checks, dict):
                raise ValueError("模型结果格式无效。")
            review = CardReviewResult.model_validate(checks)
            validate_card_review(review, captured["draft"], captured["blocks"])
            checks = review.model_dump()
        except (AIError, ValidationError, ValueError, TypeError, KeyError) as error:
            reason = str(error) if isinstance(error, AIError) else "模型机审结果不完整或证据无效，请重试或人工核对。"
            return await self._save(captured, "needs_review", [reason], None, [], retryable=True)
        all_supported = (checks["decision"] == "pass" and checks["question_answerable"]
                         and checks["answer_relevant"] and checks["answer_supported"]
                         and not checks["unsupported_claims"] and not checks["issues"]
                         and checks["answer_citations"]
                         and checks["question_coverage"] == "complete"
                         and not checks["missing_question_parts"]
                         and all(item["scope"] == "required" for item in checks["rubric_checks"])
                         and all(item["verdict"] == "supported" for item in checks["rubric_checks"]))
        if all_supported:
            status = "auto_pass"
            reasons = ["题目、答案和全部评分要点均获得当前引用的逐字证据，评分条件与题目要求一致。"
                       + ("配图也已分别通过机审。" if captured["image_bindings"] else "")]
        elif (checks["decision"] == "revise" or any(item["verdict"] == "contradicted" for item in checks["rubric_checks"])
              or checks["unsupported_claims"] or checks["question_coverage"] == "incomplete"
              or any(item["scope"] == "background" for item in checks["rubric_checks"])):
            status = "attention"
            reasons = checks["issues"] or checks["unsupported_claims"] or ["卡片内容需要修正。"]
        else:
            status = "needs_review"
            reasons = checks["issues"] or ["题目、答案或引用的支持关系仍不确定，请人工核对。"]
        return await self._save(captured, status, reasons, checks, checks["issues"], retryable=False)

    async def _save(self, captured, status, reasons, checks, issues, *, retryable):
        record = {"id": captured["review_id"], "review_id": captured["review_id"],
                  "fingerprint": captured["fingerprint"], "card_id": captured["card"]["id"],
                  "card_version": captured["card"]["version"], "status": status,
                  "reasons": reasons, "checks": checks, "issues": issues, "checked_at": _now(),
                  "retryable": retryable, "policy_version": POLICY_VERSION,
                  "prompt_version": CARD_REVIEW_PROMPT_VERSION, "model": self.settings.model,
                  "pdf_hash": captured["pdf_hash"], "image_bindings": captured["image_bindings"],
                  "card_binding_hash": captured["card_binding_hash"],
                  "source_blocks": [block.model_dump() for block in captured["blocks"]],
                  "source_pages": captured["source_pages"]}
        def records():
            current = self._capture(captured["card"]["id"])
            if current["fingerprint"] != captured["fingerprint"]:
                raise HTTPException(409, "机审期间卡片、配图或讲义已变化，请刷新后重新机审。")
            card = {**current["card"], "review_state": status}
            card.pop("review_task_fingerprint", None)
            if status == "auto_pass":
                card["status"] = "approved"
                card.pop("approval_method", None)
                card.pop("automatic_evidence", None)
                card["approval"] = {"method": "machine", "review_id": record["review_id"],
                                    "card_version": card["version"], "approved_at": record["checked_at"]}
            elif (card.get("approval") or {}).get("method") == "machine" or card.get("status") != "approved":
                card["status"] = "draft"
                card.pop("approval", None)
            return [("card_auto_reviews", record), ("cards", card)]
        await asyncio.to_thread(self.store.mutate, records)
        return self._public(record)

    async def recover(self):
        """Interrupted background work is surfaced, not silently billed again."""
        def records():
            changed = []
            for card in self.store.all("cards"):
                if card.get("review_state") in {"queued", "running"}:
                    card["review_state"] = "interrupted"
                    card.pop("review_task_fingerprint", None)
                    changed.append(("cards", card))
            return changed
        await asyncio.to_thread(self.store.mutate, records)

    async def shutdown(self):
        self.closing = True
        if self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
