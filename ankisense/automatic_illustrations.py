"""Attach one verified lecture figure without blocking useful text cards.

The service reuses local recommendation/page caches and the saved-picture
review policy. It never substitutes a whole page or retries a cloud request.
"""
from __future__ import annotations

import asyncio
import hashlib
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .figure_recommendations import _now
from .figure_vision import _digest
from .schemas import ImageRecommendationAdoptRequest, ImageRecommendationRequest
from .sources import page_id


ACTIVE_STATES = {"queued", "matching", "checking"}


class RetryIllustrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_version: int = Field(strict=True, ge=1)


class AutomaticIllustrationService:
    def __init__(self, store, recommendations, image_auto_review, review_saved_card):
        self.store = store
        self.recommendations = recommendations
        self.image_auto_review = image_auto_review
        self.review_saved_card = review_saved_card
        self.running: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.closing = False
        self.router = APIRouter()
        self.router.add_api_route("/api/cards/{card_id}/auto-illustration", self.retry, methods=["POST"])

    def _snapshot(self, card):
        blocks, pages = [], {}
        for evidence in card.get("evidence", []):
            block = self.store.get("blocks", evidence["block_id"])
            if block is None:
                blocks.append(None)
                continue
            blocks.append(block)
            number = block["page"]
            page = self.store.get("source_pages", page_id(card["document_id"], number))
            pages[str(number)] = ({key: page.get(key) for key in ("revision", "block_ids", "text")}
                                  | {"available": page.get("status") in {"pending", "reviewed"}}) if page else None
        pdf = self.recommendations.storage / "uploads" / f"{card['document_id']}.pdf"
        pdf_hash = None
        if pdf.is_file():
            with pdf.open("rb") as stream:
                pdf_hash = hashlib.sha256(stream.read(10 * 1024 * 1024 + 1)).hexdigest()
        return _digest({"card": self.image_auto_review._candidate_binding(card),
                        "blocks": blocks, "pages": pages, "pdf_hash": pdf_hash})

    def _current(self, card_id, snapshot):
        card = self.store.get("cards", card_id)
        if card is None or self._snapshot(card) != snapshot:
            raise HTTPException(409, "卡片或讲义已更新，旧的自动配图结果已丢弃。")
        return card

    async def _update(self, card_id, snapshot, token, state, message, **extra):
        def records():
            card = self._current(card_id, snapshot)
            card["auto_illustration"] = {"state": state, "message": message, "updated_at": _now(),
                                         "card_version": card["version"], "task_id": token, **extra}
            return [("cards", card)]
        result = await asyncio.to_thread(self.store.mutate, records)
        return result[0][1]

    async def queue(self, card, *, enabled=True, retry=False):
        """Enqueue matching quickly; ordinary cards can be text-reviewed now."""
        async with self.lock:
            latest = self.store.get("cards", card["id"])
            if not latest or latest.get("version") != card.get("version"):
                raise HTTPException(409, "卡片已更新，请刷新后再试。")
            if card["id"] in self.running:
                return latest
            prior = latest.get("auto_illustration") or {}
            if not retry and prior.get("card_version") == latest["version"] and prior.get("state"):
                return latest
            snapshot = await asyncio.to_thread(self._snapshot, latest)
            token = uuid4().hex
            need = (latest.get("image_intent") or {}).get("need", "none")
            if not enabled or need == "none" or latest.get("images"):
                message = ("已保留现有配图。" if latest.get("images") else
                           "本次已关闭自动配图。" if not enabled else "这张卡片无需配图。")
                latest = await self._update(latest["id"], snapshot, token, "skipped", message)
                return await self.review_saved_card(latest)
            if self.closing:
                return await self._update(latest["id"], snapshot, token, "interrupted", "服务正在关闭，未开始自动配图。")
            latest = await self._update(latest["id"], snapshot, token, "queued", "正在等待匹配引用页中的讲义图片。")
            if need == "helpful":
                # Queueing text review does not wait for a model result. Its
                # eventual metadata-only approval must not invalidate matching.
                latest = await self.review_saved_card(latest)
            task = asyncio.create_task(self._run(latest["id"], snapshot, token, need))
            self.running[latest["id"]] = task
            task.add_done_callback(lambda done, key=latest["id"]: self._finished(key, done))
            return self.store.get("cards", latest["id"])

    def _finished(self, key, task):
        if self.running.get(key) is task:
            self.running.pop(key, None)
        if not task.cancelled():
            task.exception()  # Consume unexpected failures; _run persists user-visible errors.

    async def retry(self, card_id: str, body: RetryIllustrationRequest):
        card = self.store.get("cards", card_id)
        if not card:
            raise HTTPException(404, "卡片不存在。")
        if card.get("version") != body.card_version:
            raise HTTPException(409, "卡片已更新，请刷新后重试。")
        return await self.queue(card, retry=True)

    async def _run(self, card_id, snapshot, token, need):
        try:
            async with self.slots:
                if self.closing:
                    await self._update(card_id, snapshot, token, "interrupted", "服务已关闭，未开始匹配；可主动重试。")
                    return
                card = await self._update(card_id, snapshot, token, "matching", "正在本机匹配引用页中的讲义图片。")
                try:
                    public = await self.recommendations.start(card_id, ImageRecommendationRequest(card_version=card["version"]))
                except HTTPException as error:
                    if error.status_code == 422:
                        await self._update(card_id, snapshot, token, "not_found", "没有可用的原始 PDF，可在制卡工作台手动添加图片。")
                        return
                    raise
                job_id = public["id"]
                while public["status"] in {"pending", "running"}:
                    if self.closing:
                        await self._update(card_id, snapshot, token, "interrupted", "配图匹配已停止，未发起新的图片检查；可主动重试。", job_id=job_id)
                        return
                    await asyncio.sleep(.1)
                    await asyncio.to_thread(self._current, card_id, snapshot)
                    public = self.recommendations.get(job_id)
                await asyncio.to_thread(self._current, card_id, snapshot)
                if public["status"] != "completed":
                    await self._update(card_id, snapshot, token, "failed", public.get("message") or "本地配图匹配失败，可主动重试。", job_id=job_id)
                    return
                job = self.store.get("figure_jobs", job_id)
                intended_ids = set((card.get("image_intent") or {}).get("block_ids") or [])
                cited_ids = {item["block_id"] for item in card.get("evidence", [])}
                intended_pages = {(self.store.get("blocks", identifier) or {}).get("page")
                                  for identifier in intended_ids & cited_ids}
                pictures = [candidate for candidate in job.get("candidates", [])
                            if candidate.get("kind") == "picture" and candidate.get("page") in intended_pages]
                if job.get("parser_failed") or job.get("warning"):
                    reason = job.get("warning") or job.get("message") or "图块解析存在提示。"
                    await self._update(card_id, snapshot, token, "needs_review",
                                       f"{reason} 未自动添加图片；可查看推荐后手动选图。", job_id=job_id)
                    return
                if not pictures:
                    await self._update(card_id, snapshot, token, "not_found", "引用页没有找到合适的独立图块。", job_id=job_id)
                    return
                if len(pictures) != 1 or pictures[0].get("quality_flags"):
                    await self._update(card_id, snapshot, token, "needs_review", "存在多个候选图或裁剪疑点，未自动选图；可到制卡工作台确认。", job_id=job_id)
                    return
                if self.closing:
                    await self._update(card_id, snapshot, token, "interrupted", "服务已关闭，未发起图片检查；可主动重试。", job_id=job_id)
                    return
                candidate = pictures[0]
                media = await self.recommendations.adopt(card_id, ImageRecommendationAdoptRequest(
                    card_version=card["version"], job_id=job_id, candidate_id=candidate["id"]))
                await self._update(card_id, snapshot, token, "checking", "正在检查图片与卡片的关联、完整性和答案泄露。", job_id=job_id)
                if self.closing:
                    await self._update(card_id, snapshot, token, "interrupted", "服务已关闭，未发起图片检查；可主动重试。", job_id=job_id)
                    return
                side = "question" if need == "required" else "answer"
                attachment = {"media_id": media["id"], "side": side, "caption": f"讲义第 {candidate['page']} 页"}
                metadata = {"state": "attached", "message": "已添加通过检查的讲义配图，等待整卡检查。",
                            "updated_at": _now(), "card_version": card["version"] + 1, "task_id": token,
                            "job_id": job_id, "media_id": media["id"], "page": candidate["page"], "side": side}
                result = await self.image_auto_review.check_candidate(card_id, attachment, card["version"], metadata=metadata,
                                                                       should_continue=lambda: not self.closing)
                if result.get("attached"):
                    if not self.closing:
                        await self.review_saved_card(result["card"])
                    else:
                        def interrupted_review():
                            current = self.store.get("cards", card_id)
                            if not current or current["version"] != result["card"]["version"]:
                                return []
                            current["review_state"] = "interrupted"
                            current["auto_illustration"]["message"] = "配图已添加；整卡检查因服务关闭未开始，可重新机审。"
                            return [("cards", current)]
                        await asyncio.to_thread(self.store.mutate, interrupted_review)
                    return
                state = "interrupted" if result.get("interrupted") else "failed" if result.get("retryable") else "needs_review"
                message = "；".join(result.get("reasons") or ["图片检查尚未通过，未自动附图。"])
                await self._update(card_id, snapshot, token, state, message, job_id=job_id)
        except HTTPException as error:
            if error.status_code == 409:
                await self._mark_stale(card_id, token)
                return
            await self._fail_current(card_id, snapshot, token, str(error.detail))
        except asyncio.CancelledError:
            await self._fail_current(card_id, snapshot, token, "自动配图已中断，可主动重试。", state="interrupted")
            raise
        except Exception:
            await self._fail_current(card_id, snapshot, token, "自动配图失败，原有内容已保留；可主动重试或手动选图。")

    async def _mark_stale(self, card_id, token):
        def records():
            card = self.store.get("cards", card_id)
            state = (card or {}).get("auto_illustration") or {}
            if not card or state.get("task_id") != token or state.get("state") not in ACTIVE_STATES:
                return []
            card["auto_illustration"] = {**state, "state": "interrupted", "message": "卡片或讲义已更新，旧的配图结果已丢弃。", "updated_at": _now()}
            return [("cards", card)]
        await asyncio.to_thread(self.store.mutate, records)

    async def _fail_current(self, card_id, snapshot, token, message, *, state="failed"):
        try:
            await self._update(card_id, snapshot, token, state, message)
        except HTTPException:
            await self._mark_stale(card_id, token)

    async def recover(self):
        def records():
            changes = []
            for card in self.store.all("cards"):
                state = card.get("auto_illustration") or {}
                if state.get("state") in ACTIVE_STATES:
                    card["auto_illustration"] = {**state, "state": "interrupted", "message": "上次自动配图被服务重启中断；可主动重试。", "updated_at": _now()}
                    changes.append(("cards", card))
            return changes
        await asyncio.to_thread(self.store.mutate, records)

    async def shutdown(self):
        self.closing = True
        if self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
