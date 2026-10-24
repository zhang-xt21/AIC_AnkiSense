"""Conservative, source-bound review of a *saved* card picture.

This service only reviews a picture. It never changes the card's approval state.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence import validate_card
from .figure_recommendations import _crop, _likely_decoration, _now, _padded_bbox, _semantic_tokens, _valid_bbox
from .figure_vision import CARD_FIELDS, PROMPT_VERSION, VisualResult, VisionError, build_prompt, capability, run_enrichment, safe_diagnostics
from .media import MAX_UPLOAD_BYTES, require_media, normalize_image
from .ocr import render_page_png
from .schemas import CardDraft, SourceBlock
from .sources import page_id, require_current_source, source_available, invalidate_approval
from .figure_vision import _digest


POLICY_VERSION = "saved-diagram-auto-review-v3-decorations"
MAX_PDF_BYTES = 50 * 1024 * 1024


class AutoCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_version: int = Field(ge=1, strict=True)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _content_regions(regions, targets):
    """Remove only the same obvious picture ornaments as recommendation ranking.

    Work from the full original layout, not ranked/limited candidates. Unknown
    regions and tables always remain ambiguous; missing captions are simply empty
    captions, while nearby body text cannot rescue a small page-corner ornament.
    """
    result = []
    for region in regions:
        if not isinstance(region, dict) or not _valid_bbox(region.get("bbox")):
            return None
        caption = region.get("caption")
        normalized = {**region, "caption": caption if isinstance(caption, str) else ""}
        if region.get("kind") == "picture" and _likely_decoration(normalized, targets):
            continue
        result.append(region)
    return result


def _matches_original_region(candidate, region):
    original = candidate.get("original_bbox")
    actual = region["bbox"]
    if original is not None:
        return _valid_bbox(original) and all(abs(a - b) <= .000001 for a, b in zip(original, actual))
    # Older recommendation records only stored the final padded box. Support
    # the known legacy crop, not arbitrary large crops containing this region.
    cropped = candidate.get("bbox")
    return _valid_bbox(cropped) and any(all(abs(a - b) <= .000001 for a, b in zip(cropped, expected))
                                       for expected in (actual, _padded_bbox(actual)))


class ImageAutoReviewService:
    def __init__(self, store, storage: Path, settings):
        self.store, self.storage, self.settings = store, storage, settings
        self.lock = asyncio.Lock()
        self.running: dict[str, asyncio.Task] = {}
        self.router = APIRouter()
        route = "/api/cards/{card_id}/images/{media_id}/auto-check"
        self.router.add_api_route(route, self.get, methods=["GET"])
        self.router.add_api_route(route, self.check, methods=["POST"])

    def _capture(self, card_id: str, media_id: str, version: int | None = None, *, card_override=None) -> dict:
        card = card_override if card_override is not None else self.store.get("cards", card_id)
        if card is None:
            raise HTTPException(404, "卡片不存在。")
        if version is not None and card.get("version") != version:
            raise HTTPException(409, "卡片已更新，请刷新后重新检查配图。")
        attachment = next((item for item in card.get("images") or [] if item.get("media_id") == media_id), None)
        if attachment is None:
            raise HTTPException(404, "这张图片尚未保存到当前卡片。")
        media, media_file = require_media(self.store, self.storage, media_id, card["document_id"])
        with media_file.open("rb") as stream:
            image = stream.read(MAX_UPLOAD_BYTES + 1)
        source = media.get("source") or {}
        reasons: list[str] = []
        blocks = []
        try:
            for block_id in dict.fromkeys(item["block_id"] for item in card["evidence"]):
                record = self.store.get("blocks", block_id)
                if not record or record.get("document_id") != card["document_id"]:
                    raise ValueError("卡片引用的原文片段不存在。")
                blocks.append(SourceBlock.model_validate(record))
            validate_card(CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields}), blocks)
            require_current_source(self.store, blocks)
        except (KeyError, ValueError, ValidationError, HTTPException):
            reasons.append("卡片引用或原文版本已失效，请更新原文引用。")
        if card.get("source_outdated"):
            reasons.append("卡片引用的原文已更新。")
        if len(image) > MAX_UPLOAD_BYTES:
            reasons.append("卡片图片文件超过大小限制，需重新添加。")
        cited_pages = {block.page for block in blocks}
        cited_source_pages = {}
        for cited_page in cited_pages:
            current = self.store.get("source_pages", page_id(card["document_id"], cited_page))
            cited_source_pages[str(cited_page)] = current
            if (not source_available(current)
                    or not current.get("text", "").strip()) or any(
                block.block_id not in current.get("block_ids", []) for block in blocks if block.page == cited_page
            ):
                reasons.append(f"引用的第 {cited_page} 页不是当前可用原文。")
            elif any(block.text not in current["text"] for block in blocks if block.page == cited_page):
                reasons.append(f"引用的第 {cited_page} 页文字与当前原文不一致。")
        page = source.get("page")
        source_page = cited_source_pages.get(str(page)) if isinstance(page, int) and page >= 1 else None
        page_mode = source.get("origin") == "automatic_source_page"
        if source.get("origin") not in {"figure_recommendation", "automatic_source_page"}:
            reasons.append("只有从讲义独立图块推荐中采纳的图片可以自动通过；手动配图请人工核对。")
        if (not page_mode and source.get("card_id") != card_id) or page not in cited_pages:
            reasons.append("图片未绑定当前卡片明确引用的讲义页。")
        if (not source_available(source_page)
                or source_page.get("revision") != source.get("revision")):
            reasons.append("配图所在页的原文版本已变化或不可用。")

        pdf_file = self.storage / "uploads" / f"{card['document_id']}.pdf"
        if pdf_file.is_file():
            with pdf_file.open("rb") as stream:
                pdf = stream.read(MAX_PDF_BYTES + 1)
        else:
            pdf = b""
        pdf_hash = _sha(pdf) if pdf else None
        document = self.store.get("documents", card["document_id"]) or {}
        expected_pdf_hash = source.get("pdf_hash") or (document.get("pdf_sha256") if page_mode else None)
        if (not pdf or len(pdf) > MAX_PDF_BYTES or not pdf.startswith(b"%PDF-")
                or (expected_pdf_hash is not None and pdf_hash != expected_pdf_hash)
                or (not page_mode and expected_pdf_hash is None)):
            reasons.append("原 PDF 已变化或不可用，请重新推荐配图。")

        job = candidate = figure_page = None
        if not page_mode:
            job = self.store.get("figure_jobs", source.get("job_id", "")) if source.get("job_id") else None
            candidate = next((item for item in job.get("candidates", []) if item.get("id") == source.get("candidate_id")), None) if job else None
            if not job or job.get("card_id") != card_id or job.get("document_id") != card["document_id"] or job.get("status") != "completed":
                reasons.append("原配图推荐记录不可用。")
            elif (job.get("parser_failed") or job.get("warning") or job.get("parser_version") in {None, "unavailable"}
                  or job.get("pdf_hash") != pdf_hash):
                reasons.append("原图块解析不完整或含有警告，需人工核对。")
            if not candidate or candidate.get("page") != page or candidate.get("kind") != "picture":
                reasons.append("整页原图、表格或未识别为独立图块的图片需人工核对。")
            else:
                if candidate.get("quality_flags"):
                    reasons.append("图块或图注存在质量提示，需人工核对。")
                if source.get("bbox") != candidate.get("bbox"):
                    reasons.append("图片曾手动调整裁剪范围，需人工核对。")
                if candidate.get("source_revision") != source.get("revision"):
                    reasons.append("推荐图块与保存图片的讲义版本不一致。")
                matching = [item for item in job.get("candidates", [])
                            if item.get("page") == page and item.get("kind") == "picture"]
                if len(matching) != 1:
                    reasons.append("本页有多幅相似候选图，需人工确认选图是否正确。")
            # The ranked recommendation list is filtered and capped at three items.
            # Only the unranked page layout can establish a single content picture.
            # Obvious page ornaments use the exact same filter and tokens as ranking.
            figure_page = None
            if job and pdf_hash and isinstance(page, int) and page >= 1:
                figure_page_id = "figpage-" + _sha(f"{pdf_hash}:{job.get('parser_version')}:{page}".encode())
                figure_page = self.store.get("figure_pages", figure_page_id)
            if (not figure_page or figure_page.get("pdf_hash") != pdf_hash
                    or figure_page.get("parser_version") != job.get("parser_version")
                    or figure_page.get("page") != page or figure_page.get("warning")
                    or figure_page.get("regions_truncated") is not False
                    or not isinstance(figure_page.get("regions"), list)):
                reasons.append("原始图块记录缺失、截断或有疑点，需人工核对。")
            else:
                targets = _semantic_tokens(" ".join([card.get("question", ""), card.get("concept", "")]
                                           + [entry.get("quote", "") for entry in card.get("evidence", [])]))
                content_regions = _content_regions(figure_page["regions"], targets)
                if (content_regions is None or len(content_regions) != 1
                        or content_regions[0].get("kind") != "picture"):
                    reasons.append("原页有多个正文图表区域或没有独立图块，需人工确认选图是否正确。")
                elif not candidate or not _matches_original_region(candidate, content_regions[0]):
                    reasons.append("候选图与原页独立图块的位置不一致，需重新匹配配图。")
        bbox = source.get("bbox")
        if not page_mode and not _valid_bbox(bbox):
            reasons.append("裁剪范围无效，需人工核对。")
        side = attachment.get("side", "answer")
        if side not in {"question", "answer"}:
            reasons.append("卡片图片展示位置无效。")

        page_image = None
        if not reasons:
            try:
                page_image = render_page_png(pdf, page)
                expected = normalize_image(page_image)[0] if page_mode else _crop(page_image, bbox)[0]
                if page_mode and expected_pdf_hash is None:
                    proof_hash = (card.get("automatic_evidence") or {}).get("page_images", {}).get(str(page))
                    if proof_hash != _sha(page_image):
                        reasons.append("原始 PDF 缺少可验证的导入指纹，请重新导入讲义。")
            except (ValueError, OSError, HTTPException):
                reasons.append("无法从当前 PDF 重新生成讲义原页和裁图。")
            else:
                if expected != image:
                    reasons.append("卡片中保存的图片与当前 PDF 原页裁图不一致。")

        binding = {key: card.get(key) for key in CARD_FIELDS}
        source_blocks = [block.model_dump() for block in blocks]
        identity = {"policy_version": POLICY_VERSION, "card": binding, "document_id": card["document_id"],
                    "media_id": media_id, "image_hash": _sha(image), "source": source,
                    "attachment": attachment, "pdf_hash": pdf_hash, "page_hash": _sha(page_image) if page_image else None,
                    "source_page_revision": source_page.get("revision") if source_page else None,
                    "source_page_available": source_available(source_page),
                    "cited_source_pages": {number: {key: record.get(key) for key in ("revision", "block_ids", "text")}
                                           if record else None for number, record in cited_source_pages.items()},
                    "source_blocks": source_blocks,
                    "job": {key: job.get(key) for key in ("status", "parser_failed", "warning", "parser_version", "pdf_hash")}
                           if job else None,
                    "candidate": candidate, "similar_picture_count": len([item for item in job.get("candidates", [])
                        if item.get("page") == page and item.get("kind") == "picture"]) if job else None,
                    "figure_page": figure_page,
                    "model": self.settings.model, "endpoint_hash": _digest(self.settings.base_url),
                    "prompt_version": PROMPT_VERSION}
        identity["reasons"] = reasons
        fingerprint = _digest(identity)
        return {"fingerprint": fingerprint, "review_id": "imgreview-" + fingerprint,
                "card": card, "binding": binding, "blocks": source_blocks, "media_id": media_id,
                "image": image, "page_image": page_image, "bbox": bbox, "side": side,
                "source": source, "pdf_hash": pdf_hash, "image_hash": _sha(image), "reasons": reasons}

    @staticmethod
    def _public(record: dict, *, cached: bool = False) -> dict:
        return {key: record.get(key) for key in ("review_id", "status", "reasons", "visual", "checked_at")} | {
            "current": True, "cached": cached, "stale": False, "retryable": bool(record.get("retryable")),
            "notice": "仅表示配图机器检查结果，不代表整张卡片已批准。"}

    async def get(self, card_id: str, media_id: str):
        captured = await asyncio.to_thread(self._capture, card_id, media_id)
        record = self.store.get("image_auto_reviews", captured["review_id"])
        if record and record.get("fingerprint") == captured["fingerprint"]:
            return self._public(record, cached=True)
        stale = any(item.get("card_id") == card_id and item.get("media_id") == media_id
                    for item in self.store.all("image_auto_reviews"))
        return {"review_id": None, "status": "needs_review" if captured["reasons"] else "unchecked",
                "reasons": captured["reasons"], "visual": None, "checked_at": None,
                "current": False, "cached": False, "stale": stale, "retryable": False,
                "notice": "仅表示配图机器检查结果，不代表整张卡片已批准。"}

    async def check(self, card_id: str, media_id: str, body: AutoCheckRequest):
        captured = await asyncio.to_thread(self._capture, card_id, media_id, body.card_version)
        if captured["reasons"]:
            return await self._save(captured, "needs_review", captured["reasons"], None, retryable=False)
        readiness = capability(self.settings)
        if not readiness["available"]:
            return await self._save(captured, "needs_review", [readiness["message"]], None, retryable=True)
        async with self.lock:
            existing = self.store.get("image_auto_reviews", captured["review_id"])
            if existing and existing.get("fingerprint") == captured["fingerprint"] and not existing.get("retryable"):
                return self._public(existing, cached=True)
            task = self.running.get(captured["fingerprint"])
            if task is None:
                if len(self.running) >= 2:
                    raise HTTPException(429, "已有两个配图审核在进行，请等待完成。")
                task = asyncio.create_task(self._run(captured))
                self.running[captured["fingerprint"]] = task
                task.add_done_callback(lambda _done, key=captured["fingerprint"]: self.running.pop(key, None))
        return await asyncio.shield(task)

    async def _run(self, captured):
        return await self._save(captured, **await self._evaluate(captured))

    async def _evaluate(self, captured):
        """Run the existing visual policy without changing any persisted card."""
        if captured["reasons"]:
            return {"status": "needs_review", "reasons": captured["reasons"], "visual": None, "retryable": False}
        readiness = capability(self.settings)
        if not readiness["available"]:
            return {"status": "needs_review", "reasons": [readiness["message"]], "visual": None, "retryable": True}
        prompt = build_prompt(captured["binding"], captured["blocks"], captured["bbox"] or [0, 0, 1, 1], captured["side"])
        try:
            enriched = await asyncio.to_thread(run_enrichment, self.settings, captured["image"], captured["page_image"], prompt)
            visual = VisualResult.model_validate(enriched["result"]).model_dump()
        except VisionError as error:
            return {"status": "needs_review", "reasons": [str(error)], "visual": None,
                    "retryable": True, "diagnostics": error.diagnostics}
        except (KeyError, ValueError, TypeError, ValidationError):
            return {"status": "needs_review", "reasons": ["模型检查结果无效，请人工核对或重试。"],
                    "visual": None, "retryable": True}
        if (visual["classification"] == "diagram" and visual["relation"] == "supported"
                and visual["completeness"] == "complete" and visual["legibility"] == "clear"
                and (captured["side"] == "answer" or visual["answer_leakage"] == "no")):
            status = "auto_pass"
        elif (visual["relation"] == "unrelated" or visual["completeness"] == "incomplete"
              or visual["legibility"] == "unclear"
              or (captured["side"] == "question" and visual["answer_leakage"] == "yes")):
            status = "attention"
        else:
            status = "needs_review"
        reasons = visual["reasons"] or ["图片检查完成。"]
        if status == "needs_review" and visual["classification"] != "diagram":
            reasons = ["第一版仅允许独立示意图自动通过；其他图类需人工核对。", *reasons]
        return {"status": status, "reasons": reasons, "visual": visual, "retryable": False,
                "diagnostics": safe_diagnostics(enriched.get("diagnostics"))}

    async def _save(self, captured, status, reasons, visual, *, retryable, diagnostics=None):
        current = await asyncio.to_thread(self._capture, captured["card"]["id"], captured["media_id"])
        if current["fingerprint"] != captured["fingerprint"]:
            raise HTTPException(409, "检查期间卡片、图片或原文已变化，请重新检查。")
        record = self._record(captured, status, reasons, visual, retryable=retryable, diagnostics=diagnostics)
        def still_current():
            if self._capture(captured["card"]["id"], captured["media_id"])["fingerprint"] != captured["fingerprint"]:
                raise HTTPException(409, "检查期间卡片、图片或原文已变化，请重新检查。")
        await asyncio.to_thread(self.store.put_many, [("image_auto_reviews", record)], still_current)
        return self._public(record)

    def _record(self, captured, status, reasons, visual, *, retryable, diagnostics=None):
        return {"id": captured["review_id"], "review_id": captured["review_id"],
                  "fingerprint": captured["fingerprint"], "card_id": captured["card"]["id"],
                  "card_version": captured["card"]["version"], "media_id": captured["media_id"],
                  "status": status, "reasons": reasons, "visual": visual, "checked_at": _now(),
                  "retryable": retryable, "image_hash": captured["image_hash"],
                  "pdf_hash": captured["pdf_hash"], "bbox": captured["bbox"], "side": captured["side"],
                  "source_revision": captured["source"].get("revision"),
                  "model": self.settings.model, "prompt_version": PROMPT_VERSION,
                  "policy_version": POLICY_VERSION, "diagnostics": safe_diagnostics(diagnostics)}

    @staticmethod
    def _candidate_binding(card):
        return _digest({key: card.get(key) for key in (*CARD_FIELDS, "document_id", "source_outdated", "image_intent")})

    @staticmethod
    def _prospective(card, attachment, metadata):
        proposed = invalidate_approval(copy.deepcopy(card))
        proposed.update(version=card["version"] + 1, status="draft", images=[copy.deepcopy(attachment)], updated_at=_now())
        for key in ("approval", "review_state", "review_task_fingerprint"):
            proposed.pop(key, None)
        if metadata is not None:
            proposed["auto_illustration"] = copy.deepcopy(metadata)
        return proposed

    async def check_candidate(self, card_id, attachment, card_version, *, metadata=None, should_continue=None):
        """Check a prospective attachment; publish it and its receipt only on pass.

        Both the model prompt and saved receipt bind the resulting card version,
        so the ordinary GET/whole-card review reuses this result without another
        visual request. Approval metadata from concurrent text review is harmless;
        edits to content, images, sources or model invalidate the transaction.
        """
        original = self.store.get("cards", card_id)
        if not original or original.get("version") != card_version or original.get("images"):
            raise HTTPException(409, "卡片已更新或已有配图，自动配图没有覆盖现有内容。")
        binding = self._candidate_binding(original)
        proposed = self._prospective(original, attachment, metadata)
        captured = await asyncio.to_thread(self._capture, card_id, attachment["media_id"], card_override=proposed)
        async with self.lock:
            task = self.running.get(captured["fingerprint"])
            if task is None:
                if len(self.running) >= 2:
                    raise HTTPException(429, "已有两个配图审核在进行，请等待完成后重试。")
                task = asyncio.create_task(self._check_candidate(captured, binding, attachment, metadata, should_continue))
                self.running[captured["fingerprint"]] = task
                task.add_done_callback(lambda _done, key=captured["fingerprint"]: self.running.pop(key, None))
        return await asyncio.shield(task)

    async def _check_candidate(self, captured, original_binding, attachment, metadata, should_continue):
        if should_continue is not None and not should_continue():
            return {"status": "needs_review", "attached": False, "interrupted": True,
                    "reasons": ["服务已关闭，未开始图片检查；可主动重试。"]}
        result = await self._evaluate(captured)
        record = self._record(captured, **result)
        if result["status"] != "auto_pass":
            return {**self._public(record), "attached": False}

        def records():
            current = self.store.get("cards", captured["card"]["id"])
            if not current or self._candidate_binding(current) != original_binding or current.get("images"):
                raise HTTPException(409, "检查期间卡片已修改，未自动添加配图。")
            proposed = self._prospective(current, attachment, metadata)
            fresh = self._capture(current["id"], attachment["media_id"], card_override=proposed)
            if fresh["fingerprint"] != captured["fingerprint"]:
                raise HTTPException(409, "检查期间图片、原文或模型已变化，未自动添加配图。")
            return [("cards", proposed), ("image_auto_reviews", record)]

        committed = await asyncio.to_thread(self.store.mutate, records)
        return {**self._public(record), "attached": True, "card": committed[0][1]}

    async def shutdown(self):
        if self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
