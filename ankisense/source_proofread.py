"""Explicit, page-scoped visual proofreading suggestions.

The model never changes source_pages. A person must still save a corrected page
through the existing revision-aware proofreading endpoint.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from .llm import AIError, SourceProofreadResult
from .model_policy import generation_options
from .ocr import LocalOCRError, render_page_png
from .schemas import SourceRevisionRequest
from .sources import page_id


PROMPT_VERSION = "source-visual-proofread-v1"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_CHARS = 12000


class SourceProofreadService:
    def __init__(self, store, storage: Path, ai_provider):
        self.store = store
        self.storage = storage
        self.ai_provider = ai_provider
        self.lock = asyncio.Lock()
        self.running: dict[str, asyncio.Task] = {}
        self.router = APIRouter()
        self.router.add_api_route(
            "/api/documents/{document_id}/pages/{page}/ai-proofread",
            self.proofread, methods=["POST"],
        )

    def _capture(self, document_id: str, page: int, expected_revision: int):
        document = self.store.get("documents", document_id)
        source = self.store.get("source_pages", page_id(document_id, page))
        if not document or not source or not document.get("file_url"):
            raise HTTPException(404, "该页没有已导入的 PDF 原图。")
        if source["revision"] != expected_revision:
            raise HTTPException(409, "此页已更新，请刷新后重新获取 AI 校对建议。")
        original = source.get("text", "")
        if len(original) > MAX_SOURCE_CHARS:
            raise HTTPException(422, "本页原文超过 1.2 万字，暂不支持 AI 逐页校对；仍可手动校对。")
        path = self.storage / "uploads" / f"{document_id}.pdf"
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_PDF_BYTES + 1)
        except OSError:
            raise HTTPException(404, "原始 PDF 文件不存在，请重新导入材料。") from None
        pdf_hash = hashlib.sha256(content).hexdigest()
        imported_hash = document.get("pdf_sha256")
        if not imported_hash:
            # Older proof-v1 imports kept the original PDF but not its hash.
            # Their stable document ID still binds the bytes, page range and
            # extraction mode. Do not silently trust or rewrite the old row.
            imported_pages = [item["page"] for item in self.store.all("source_pages")
                              if item.get("document_id") == document_id]
            mode = document.get("extraction_mode")
            legacy_matches = (
                bool(imported_pages) and mode in {"auto", "text", "ocr"}
                and "pdf-" + hashlib.sha256(
                    content + f":{min(imported_pages)}:{max(imported_pages)}:proof-v1:{mode}".encode()
                ).hexdigest()[:24] == document_id
            )
        else:
            legacy_matches = False
        if (not content.startswith(b"%PDF-") or len(content) > MAX_PDF_BYTES
                or not (imported_hash == pdf_hash if imported_hash else legacy_matches)):
            raise HTTPException(409, "原始 PDF 与导入时的文件不一致，请重新导入后校对。")
        settings = self.ai_provider().settings
        identity = {
            "document_id": document_id, "page": page, "revision": expected_revision,
            "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "pdf_sha256": pdf_hash,
            "model": settings.model,
            "endpoint_sha256": hashlib.sha256(settings.base_url.encode("utf-8")).hexdigest(),
            "prompt_version": PROMPT_VERSION,
        }
        options = generation_options(settings.base_url, settings.model)
        if options:
            identity["request_options"] = options
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        return {
            "id": "sourceproof-" + fingerprint, "fingerprint": fingerprint,
            "document_id": document_id, "page": page, "revision": expected_revision,
            "text": original, "pdf": content, "model": settings.model,
        }

    async def proofread(self, document_id: str, page: int, body: SourceRevisionRequest):
        ai = self.ai_provider()
        if (ai.settings.mode != "cloud" or not all((ai.settings.base_url, ai.settings.model, ai.settings.api_key))):
            raise HTTPException(503, "AI 视觉校对需要先配置支持图片输入的云端模型。")
        captured = await asyncio.to_thread(self._capture, document_id, page, body.revision)
        async with self.lock:
            cached = await asyncio.to_thread(self.store.get, "source_proposals", captured["id"])
            if cached:
                current = await asyncio.to_thread(self._capture, document_id, page, body.revision)
                if current["fingerprint"] != captured["fingerprint"]:
                    raise HTTPException(409, "校对页已变化，请刷新后重新获取建议。")
                return {**cached, "cached": True}
            task = self.running.get(captured["fingerprint"])
            if task is None:
                if len(self.running) >= 2:
                    raise HTTPException(429, "已有两页正在进行 AI 校对，请稍后重试。")
                task = asyncio.create_task(self._run(captured))
                self.running[captured["fingerprint"]] = task
                task.add_done_callback(lambda _done, key=captured["fingerprint"]: self.running.pop(key, None))
        return await asyncio.shield(task)

    async def _run(self, captured):
        try:
            image = await asyncio.to_thread(render_page_png, captured["pdf"], captured["page"])
        except LocalOCRError as error:
            raise HTTPException(422, str(error)) from None
        if len(image) > MAX_IMAGE_BYTES:
            raise HTTPException(422, "本页原图过大，暂不支持 AI 视觉校对；仍可手动校对。")
        try:
            suggestion = await self.ai_provider().proofread_page(image, captured["text"], captured["page"])
            suggestion = SourceProofreadResult.model_validate(
                suggestion.model_dump() if hasattr(suggestion, "model_dump") else suggestion
            )
        except AIError as error:
            raise HTTPException(502, str(error)) from None
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise HTTPException(502, "模型未返回有效的逐页视觉校对结果，请手动校对或更换视觉模型。") from None
        except Exception:
            # Provider adapters can fail with request data in their exception.
            # Never expose that data through the local API response.
            raise HTTPException(502, "AI 视觉校对未完成；原文未更改，请稍后重试。") from None

        record = {
            "id": captured["id"], "document_id": captured["document_id"],
            "page": captured["page"], "revision": captured["revision"],
            "suggested_text": suggestion.suggested_text,
            "issues": suggestion.issues, "uncertain": suggestion.uncertain,
            "model": captured["model"],
            "created_at": datetime.now(timezone.utc).isoformat(), "cached": False,
        }

        def still_current():
            current = self._capture(captured["document_id"], captured["page"], captured["revision"])
            if current["fingerprint"] != captured["fingerprint"]:
                raise HTTPException(409, "AI 校对期间原文或模型设置已变化，请刷新后重试。")

        await asyncio.to_thread(self.store.put_many, [("source_proposals", record)], still_current)
        return record

    async def shutdown(self):
        if self.running:
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
