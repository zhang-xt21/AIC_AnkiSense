"""Local, source-bound picture suggestions; accepting never approves a card."""

import asyncio
import hashlib
import io
import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from PIL import Image

from .evidence import validate_card
from .media import normalize_image, save_media
from .ocr import render_page_png
from .schemas import CardDraft, ImageRecommendationAdoptRequest, ImageRecommendationRequest, SourceBlock
from .sources import page_id, require_current_source


_PIPELINE_LOCK = threading.Lock()
RECOMMENDATION_VERSION = "citation-pages-v7"
PAGE_RENDERER_VERSION = "pdf-page-v1"
_PUBLIC_CANDIDATE = ("id", "page", "kind", "bbox", "reason", "caption", "preview_url", "page_url", "source_revision")
_OPTIONAL_CANDIDATE = ("original_bbox", "context_notes", "quality_flags")
_CARD_BINDING_FIELDS = ("question", "concept", "evidence")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_bbox(value):
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1 for v in value)
            and value[2] - value[0] >= .005 and value[3] - value[1] >= .005)


def _crop(content, bbox):
    if not _valid_bbox(bbox):
        raise HTTPException(422, "裁剪范围无效，请在当前讲义页内重新框选。")
    with Image.open(io.BytesIO(content)) as original:
        width, height = original.size
        bounds = (math.floor(bbox[0] * width), math.floor(bbox[1] * height),
                  math.ceil(bbox[2] * width), math.ceil(bbox[3] * height))
        if bounds[2] - bounds[0] < 16 or bounds[3] - bounds[1] < 16:
            raise HTTPException(422, "裁剪区域过小，请至少保留 16×16 像素。")
        with original.crop(bounds) as cropped:
            output = io.BytesIO()
            cropped.save(output, "PNG")
    return normalize_image(output.getvalue())


class RecommendationService:
    def __init__(self, store, storage: Path):
        self.store, self.storage = store, storage
        self.tasks = set()
        self.start_lock = asyncio.Lock()
        self.router = APIRouter()
        self.router.add_api_route("/api/cards/{card_id}/image-recommendations", self.start, methods=["POST"])
        self.router.add_api_route("/api/image-recommendations/{job_id}", self.get, methods=["GET"])
        self.router.add_api_route("/api/image-recommendations/{job_id}/cancel", self.cancel, methods=["POST"])
        self.router.add_api_route("/api/image-recommendations/{job_id}/candidates/{candidate_id}/image", self.preview, methods=["GET"])
        self.router.add_api_route("/api/image-recommendations/{job_id}/pages/{page}/image", self.page_preview, methods=["GET"])
        self.router.add_api_route("/api/cards/{card_id}/image-recommendations/adopt", self.adopt, methods=["POST"])

    def recover(self):
        for job in self.store.all("figure_jobs"):
            if job["status"] in {"pending", "running"}:
                job.update(status="failed", message="上次推荐被服务重启中断，请重新获取推荐。", finished_at=_now())
                self.store.put("figure_jobs", job)

    async def shutdown(self):
        self.recover()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def _job(self, identifier):
        if not re.fullmatch(r"figjob-[0-9a-f]{32}", identifier):
            raise HTTPException(404, "配图推荐记录不存在。")
        job = self.store.get("figure_jobs", identifier)
        if not job:
            raise HTTPException(404, "配图推荐记录不存在。")
        return job

    @staticmethod
    def _public(job):
        return {**{key: job.get(key) for key in ("id", "card_id", "card_version", "status", "message", "warning", "parser_failed", "created_at")},
                "candidates": [{**{key: item[key] for key in _PUBLIC_CANDIDATE},
                                **{key: item[key] for key in _OPTIONAL_CANDIDATE if key in item}}
                               for item in job.get("candidates", [])]}

    def _capture(self, card_id, version):
        card = self.store.get("cards", card_id)
        if not card:
            raise HTTPException(404, "卡片不存在。")
        if card["version"] != version:
            raise HTTPException(409, "卡片已更新，请保存并刷新后重新获取推荐。")
        document = self.store.get("documents", card["document_id"])
        if not document or not re.fullmatch(r"pdf-[0-9a-f]{24}", document["id"]):
            raise HTTPException(422, "这张卡片没有可用的原始 PDF；可继续手动上传图片。")
        path = self.storage / "uploads" / f"{document['id']}.pdf"
        if not document.get("file_url") or not path.is_file():
            raise HTTPException(422, "原始 PDF 文件不存在，请重新导入材料或手动上传图片。")
        blocks = []
        for block_id in dict.fromkeys(entry["block_id"] for entry in card["evidence"]):
            record = self.store.get("blocks", block_id)
            if not record or record["document_id"] != document["id"]:
                raise HTTPException(409, "原文引用已失效，请重新审核卡片。")
            blocks.append(SourceBlock.model_validate(record))
        if card.get("source_outdated"):
            raise HTTPException(409, "引用原文已更新，请先保存当前版本的引用。")
        require_current_source(self.store, blocks)
        try:
            validate_card(CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields}), blocks)
        except ValueError as error:
            raise HTTPException(409, "卡片引用需要重新核对后才能推荐配图。") from error
        pages = sorted({block.page for block in blocks})
        revisions = {str(page): (self.store.get("source_pages", page_id(document["id"], page)) or {}).get("revision", 0) for page in pages}
        content = path.read_bytes()
        if len(content) > 10 * 1024 * 1024 or not content.startswith(b"%PDF-"):
            raise HTTPException(422, "原始 PDF 无效或超过 10 MiB，请重新导入材料。")
        return {"card": card, "document_id": document["id"], "path": path,
                "pdf_hash": _hash(content), "pages": pages, "source_revisions": revisions}

    def _assert_current(self, job, version=None):
        captured = self._capture(job["card_id"], job["card_version"] if version is None else version)
        if (captured["card"]["version"] != job["card_version"] or captured["pdf_hash"] != job["pdf_hash"]
                or captured["pages"] != job["pages"] or captured["source_revisions"] != job["source_revisions"]):
            raise HTTPException(409, "卡片、原文或 PDF 已更新，请重新获取推荐。")
        return captured

    async def start(self, card_id: str, body: ImageRecommendationRequest):
        async with self.start_lock:
            captured = await asyncio.to_thread(self._capture, card_id, body.card_version)
            pipeline = PAGE_RENDERER_VERSION
            identity = {"card_id": card_id, "card_version": body.card_version, "pdf_hash": captured["pdf_hash"],
                        "pages": captured["pages"], "source_revisions": captured["source_revisions"], "requested_parser_version": pipeline,
                        "recommendation_version": RECOMMENDATION_VERSION}
            jobs = self.store.all("figure_jobs")
            for job in reversed(jobs):
                if all(job.get(key) == value for key, value in identity.items()) and job["status"] in {"pending", "running", "completed"} and not job.get("parser_failed"):
                    if job["status"] != "completed" or (all(self._candidate_path(job, item).is_file() for item in job["candidates"])
                                                          and all(self._page_path(job, page).is_file() for page in job["pages"])):
                        return self._public(job)
            if sum(job["status"] in {"pending", "running"} for job in jobs) >= 4:
                raise HTTPException(429, "已有多个配图任务排队，请等待当前任务完成。")
            job = {"id": "figjob-" + uuid4().hex, **identity, "document_id": captured["document_id"],
                   "card_binding": {key: captured["card"][key] for key in _CARD_BINDING_FIELDS},
                   "status": "pending", "message": "正在准备引用页原图。", "candidates": [], "created_at": _now()}
            self.store.put_many([("figure_jobs", job)], check=lambda: self._assert_current(job))
            task = asyncio.create_task(asyncio.to_thread(self._run, job["id"]))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return self._public(job)

    def get(self, job_id: str):
        return self._public(self._job(job_id))

    def cancel(self, job_id: str):
        def build():
            job = self._job(job_id)
            if job["status"] in {"pending", "running"}:
                job.update(status="failed", message="已取消原图准备。", finished_at=_now())
                return [("figure_jobs", job)]
            return []
        self.store.mutate(build)
        return self.get(job_id)

    def _transition(self, job_id, expected, check_current=False, **updates):
        def build():
            job = self._job(job_id)
            if job["status"] not in expected:
                return []
            if check_current:
                self._assert_current(job)
            job.update(updates)
            return [("figure_jobs", job)]
        changed = self.store.mutate(build)
        return changed[0][1] if changed else None

    def _candidate_path(self, job, candidate):
        return self.storage / "figure-previews" / job["id"] / f"{candidate['id']}.png"

    def _page_path(self, job, page):
        return self.storage / "figure-previews" / job["id"] / f"page-{page}.png"


    def _run(self, job_id):
        with _PIPELINE_LOCK:
            job = self._transition(job_id, {"pending"}, status="running", message="正在准备引用页原图。")
            if not job:
                return
            try:
                captured = self._assert_current(job)
                content = captured["path"].read_bytes()
                if _hash(content) != job["pdf_hash"]:
                    raise HTTPException(409, "原始 PDF 已更新，请重新选择原图。")
                candidates = []
                for page in job["pages"]:
                    if self._job(job_id)["status"] != "running":
                        return
                    cache = self.storage / "figure-cache" / "pages" / f"{job['pdf_hash']}-{PAGE_RENDERER_VERSION}-{page}.png"
                    if not cache.is_file():
                        _atomic_write(cache, render_page_png(content, page))
                    original = cache.read_bytes()
                    _atomic_write(self._page_path(job, page), original)
                    candidate = {"id": "fig-" + uuid4().hex, "page": page, "kind": "page", "bbox": [0., 0., 1., 1.],
                                 "reason": "来自本卡引用页，可裁剪后加入卡片。", "caption": f"第 {page} 页原图",
                                 "source_revision": job["source_revisions"][str(page)],
                                 "page_url": f"/api/image-recommendations/{job_id}/pages/{page}/image"}
                    candidate["preview_url"] = candidate["page_url"]
                    _atomic_write(self._candidate_path(job, candidate), original)
                    candidates.append(candidate)
                self._transition(job_id, {"running"}, check_current=True, status="completed", message="引用页原图已就绪。",
                                 warning=None, parser_failed=False, parser_version=PAGE_RENDERER_VERSION,
                                 candidates=candidates, finished_at=_now())
            except HTTPException as error:
                self._transition(job_id, {"pending", "running"}, status="failed", message=str(error.detail), finished_at=_now())
            except Exception:
                self._transition(job_id, {"pending", "running"}, status="failed", message="原图准备失败，请重试或上传截图。", finished_at=_now())

    def _candidate(self, job, candidate_id):
        candidate = next((item for item in job.get("candidates", []) if item["id"] == candidate_id), None)
        if not candidate:
            raise HTTPException(404, "配图候选不存在，请重新获取推荐。")
        return candidate

    def preview(self, job_id: str, candidate_id: str):
        job = self._job(job_id)
        candidate = self._candidate(job, candidate_id)
        path = self._candidate_path(job, candidate)
        if not path.is_file():
            raise HTTPException(404, "候选预览已丢失，请重新获取推荐。")
        return FileResponse(path, media_type="image/png")

    def page_preview(self, job_id: str, page: int):
        job = self._job(job_id)
        if page not in job["pages"]:
            raise HTTPException(404, "只能查看这张卡片明确引用的讲义页。")
        path = self._page_path(job, page)
        if not path.is_file():
            raise HTTPException(404, "讲义页预览尚未生成，请等待推荐完成。")
        return FileResponse(path, media_type="image/png")

    async def adopt(self, card_id: str, body: ImageRecommendationAdoptRequest):
        job = self._job(body.job_id)
        if job["card_id"] != card_id:
            raise HTTPException(409, "推荐结果属于另一张卡片，请重新获取。")
        if job["status"] != "completed":
            raise HTTPException(409, "请等待推荐完成后再采纳图片。")
        candidate = self._candidate(job, body.candidate_id)
        self._assert_current(job, body.card_version)
        bbox = body.bbox if body.bbox is not None else candidate["bbox"]
        path = self._page_path(job, candidate["page"])
        if not path.is_file():
            raise HTTPException(404, "讲义页预览已丢失，请重新获取推荐。")
        normalized = await asyncio.to_thread(_crop, path.read_bytes(), bbox)
        source = {"document_id": job["document_id"], "page": candidate["page"], "revision": candidate["source_revision"],
                  "pdf_hash": job["pdf_hash"], "bbox": bbox, "job_id": job["id"], "candidate_id": candidate["id"],
                  "parser_version": job.get("parser_version"), "origin": "figure_recommendation", "card_id": card_id,
                  "card_version": job["card_version"]}
        return save_media(self.store, self.storage, normalized, source=source, check=lambda: self._assert_current(job, body.card_version))

    def validate_new_attachment(self, record, card_id, version, pages, candidate):
        source = record.get("source", {})
        if source.get("card_id") != card_id or source.get("card_version") != version or source.get("page") not in pages:
            raise HTTPException(409, "新配图与当前卡片或引用页不一致，请保存文字后重新获取配图。")
        job = self._job(source["job_id"])
        if job.get("card_binding") != {key: candidate[key] for key in _CARD_BINDING_FIELDS}:
            raise HTTPException(409, "采纳配图后题目、概念或引用发生了变化；请移除本次新配图，保存题目后重新获取推荐。")
        self._assert_current(job, version)
