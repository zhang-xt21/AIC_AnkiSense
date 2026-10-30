import asyncio
import csv
import hashlib
import io
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .db import Store
from .card_deletion import delete_cards
from .downloads import ExportResponse, TemporaryDirectory, export_error
from .apkg import build_apkg, export_snapshot, require_exportable
from .approval import require_image_source, require_required_image, display_card
from .attempts import AttemptService
from .auto_cards import AutoCardService
from .automatic_import import AutomaticImportService
from .source_calibration import SourceCalibrationService
from .plugin_api import PluginAPI, install_plugin_error_handlers
from .anki_setup import AnkiSetupService
from .figure_recommendations import RecommendationService
from .figure_vision import FigureVisionService
from .image_auto_review import ImageAutoReviewService
from .card_auto_review import CardAutoReviewService
from .generation_jobs import GenerationJobService
from .automatic_illustrations import AutomaticIllustrationService
from .demo import DEMO_ID, seed_records
from .evidence import validate_card
from .ingest import parse_pdf_document, text_blocks
from .ocr import recognize_page, render_page_png
from .media import EXPORT_COLUMNS, MAX_UPLOAD_BYTES, media_zip, normalize_image, require_media, save_media
from .llm import AIError, AIService, Settings
from .model_settings import ModelSettingsFile, install_model_settings, model_request, public_settings
from .paths import resolve_data_dir
from .schemas import AttemptRequest, CardDraft, CardUpdate, GenerateRequest, SourceBlock, TextDocumentRequest, SourceCorrectionRequest, SourceRevisionRequest, SourcePageImageRequest
from .sources import page_id, page_records, require_current_source, save_page_revision
from .source_proofread import SourceProofreadService
from .source_quality import with_quality
from .version import APP_VERSION

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "ankisense" / "static"


def now():
    return datetime.now(timezone.utc).isoformat()


def card_data(card):
    return CardDraft.model_validate({name: card[name] for name in CardDraft.model_fields})


def create_app(data_dir: Path | None = None, settings: Settings | None = None, ai_service=None):
    storage = resolve_data_dir(data_dir)
    configuration = ModelSettingsFile(storage)
    settings = configuration.load(settings)
    store = Store(storage / "ankisense.sqlite3")
    recommendations = RecommendationService(store, storage)
    image_checks = FigureVisionService(recommendations, settings)
    image_auto_review = ImageAutoReviewService(store, storage, settings)
    card_auto_review = CardAutoReviewService(store, storage, settings, lambda: app.state.ai, image_auto_review)
    source_proofread = SourceProofreadService(store, storage, lambda: app.state.ai)
    @asynccontextmanager
    async def lifespan(application):
        # Recover on service startup, never merely when another tool imports the module.
        for attempt in store.all("attempts"):
            if attempt["status"] == "pending":
                attempt.update(status="failed", error="上次分析被服务重启中断；答案已保存，可以重试。")
                store.put("attempts", attempt)
        recommendations.recover()
        auto_cards.recover()
        automatic_imports.recover()
        await generation_jobs.recover()
        await automatic_illustrations.recover()
        await card_auto_review.recover()
        try:
            yield
        finally:
            automatic_illustrations.closing = True
            await automatic_imports.shutdown()
            await auto_cards.shutdown()
            await generation_jobs.shutdown()
            await automatic_illustrations.shutdown()
            await application.state.attempts.shutdown()
            await image_checks.shutdown()
            await card_auto_review.shutdown()
            await image_auto_review.shutdown()
            await source_proofread.shutdown()
            await recommendations.shutdown()

    app = FastAPI(title="AnkiSense", version=APP_VERSION, lifespan=lifespan)
    app.state.store = store
    app.state.ai = ai_service or AIService(settings)
    app.state.settings = settings
    app.state.attempts = AttemptService(store, settings, lambda: app.state.ai)
    app.state.plugin = PluginAPI(store, storage, settings, app.state.attempts)
    app.state.recommendations = recommendations
    app.state.image_checks = image_checks
    app.state.image_auto_review = image_auto_review
    app.state.card_auto_review = card_auto_review
    app.state.source_proofread = source_proofread
    app.state.active_model_requests = 0
    app.state.model_settings_switching = False
    calibration = SourceCalibrationService(store, storage, lambda: app.state.ai, lambda: app.state.settings)
    app.state.source_calibration = calibration
    auto_cards = AutoCardService(store, storage, lambda: app.state.ai, lambda: app.state.settings,
                                 recommendations, image_checks, calibration)
    auto_cards.settings_switching = lambda: app.state.model_settings_switching
    app.state.auto_cards = auto_cards
    automatic_imports = AutomaticImportService(store, storage, auto_cards)
    app.state.automatic_imports = automatic_imports
    install_model_settings(app, configuration)
    app.include_router(auto_cards.router)
    app.include_router(automatic_imports.router)
    app.include_router(calibration.router)
    app.include_router(recommendations.router)
    app.include_router(image_checks.router)
    app.include_router(image_auto_review.router)
    app.include_router(card_auto_review.router)
    app.include_router(source_proofread.router)
    app.include_router(app.state.plugin.router)
    app.state.anki_setup = AnkiSetupService(store, storage, app.state.plugin.connection)
    app.include_router(app.state.anki_setup.router)
    install_plugin_error_handlers(app)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    locks: dict[str, asyncio.Lock] = {}

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).netloc != request.headers.get("host") or urlparse(origin).scheme not in {"http", "https"}):
                return JSONResponse({"detail": "仅接受来自本地应用页面的操作。"}, status_code=403)
        uses_model = model_request(request)
        if app.state.model_settings_switching and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return JSONResponse({"detail": "正在切换模型并暂停制卡，请稍后再操作。"}, status_code=409)
        if uses_model:
            app.state.active_model_requests += 1
        try:
            response = await call_next(request)
        finally:
            if uses_model:
                app.state.active_model_requests -= 1
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_record(collection, identifier, reader=None):
        record = (reader or store).get(collection, identifier)
        if record is None:
            raise HTTPException(404, "记录不存在。")
        return record

    def source_models(card, reader=None):
        ids = {entry["block_id"] for entry in card["evidence"]}
        return [SourceBlock.model_validate(get_record("blocks", block_id, reader)) for block_id in sorted(ids)]

    async def review_saved_card(card):
        settings = app.state.settings
        if card["status"] == "draft" and settings.mode == "cloud" and settings.api_key and settings.model and settings.base_url:
            try:
                await card_auto_review.queue(card["id"], card["version"])
            except Exception:
                # Content has already been committed. Return that saved card
                # instead of making users regenerate a batch after a queue error.
                def interrupted():
                    current = get_record("cards", card["id"])
                    if current["version"] != card["version"] or current["status"] != "draft":
                        return []
                    current["review_state"] = "interrupted"
                    current.pop("review_task_fingerprint", None)
                    return [("cards", current)]
                store.mutate(interrupted)
        return get_record("cards", card["id"])

    automatic_illustrations = AutomaticIllustrationService(store, recommendations, image_auto_review,
                                                          review_saved_card)
    app.state.automatic_illustrations = automatic_illustrations
    app.include_router(automatic_illustrations.router)

    async def prepare_generated_card(card):
        job = store.get("generation_jobs", card.get("generation_job_id", "")) or {}
        return await automatic_illustrations.queue(card, enabled=job.get("auto_images", True))

    generation_jobs = GenerationJobService(store, lambda: app.state.settings,
                                           lambda: app.state.ai, prepare_generated_card)
    app.state.generation_jobs = generation_jobs
    app.include_router(generation_jobs.router)

    @app.get("/health")
    def health():
        return {"application": "AnkiSense", "api_version": 1,
                "status": "ok", "mode": app.state.settings.mode,
                "app_version": APP_VERSION, "capabilities": {"anki_setup": True, "ai_connection_test": True, "ai_task_switch": True, "large_exports": True}}

    @app.get("/api/state")
    def state():
        settings = app.state.settings
        cards = [display_card(store, card) for card in store.all("cards")]
        return {"app_version": APP_VERSION, "mode": settings.mode, "model": settings.model if settings.mode == "cloud" else "内置演示规则", "configured": public_settings(settings)["configured"], "model_connection": app.state.model_connection, "image_check": image_checks.status(), "generation_jobs": generation_jobs.list_jobs(), **{name: store.all(name) for name in ("documents", "blocks", "attempts", "source_proposals", "media")}, "cards": cards, "source_pages": [with_quality(page) for page in store.all("source_pages")]}

    @app.delete("/api/cards/{identifier}")
    def delete_card(identifier: str):
        return delete_cards(store, card_id=identifier)

    @app.delete("/api/documents/{identifier}/cards")
    def clear_document_cards(identifier: str, expected_count: int):
        if expected_count < 0:
            raise HTTPException(422, "卡片数量不能为负数。")
        return delete_cards(store, document_id=identifier, expected_count=expected_count)

    @app.delete("/api/documents/{identifier}")
    def delete_document(identifier: str, expected_count: int):
        if expected_count < 0:
            raise HTTPException(422, "卡片数量不能为负数。")
        return delete_cards(store, document_id=identifier, expected_count=expected_count, remove_document=True)

    @app.post("/api/demo")
    def demo():
        if not store.get("documents", DEMO_ID):
            store.put_many(seed_records())
        return {"document_id": DEMO_ID}

    @app.post("/api/documents/text")
    def import_text(body: TextDocumentRequest):
        identifier = "text-" + hashlib.sha256(body.text.encode()).hexdigest()[:24]
        existing = store.get("documents", identifier)
        if existing:
            return existing
        document = {"id": identifier, "title": body.title, "filename": f"{body.title}.txt", "is_demo": False, "created_at": now(), "page_count": 1, "warning": "手工粘贴的原文；页码为文本段落占位，不能视为真实 PDF 页码。"}
        store.put_many([("documents", document)] + [("blocks", block) for block in text_blocks(identifier, [(1, body.text)])])
        return document

    @app.post("/api/documents")
    async def import_pdf(file: UploadFile = File(...), start_page: int = Form(1), end_page: int | None = Form(None), mode: str = Form("auto")):
        content = await file.read(10 * 1024 * 1024 + 1)
        await file.close()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "初版 PDF 文件请控制在 10 MB 以内。")
        try:
            result = await asyncio.to_thread(parse_pdf_document, content, start_page, end_page, mode)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        identifier = result["id"]
        existing = store.get("documents", identifier)
        filename = (file.filename or "lecture.pdf").replace("\\", "/").split("/")[-1][:150]
        upload_dir = storage / "uploads"
        upload_dir.mkdir(exist_ok=True)
        original_path = upload_dir / f"{identifier}.pdf"
        if not original_path.is_file():
            temporary = upload_dir / f".{identifier}-{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(content)
                temporary.replace(original_path)
            finally:
                temporary.unlink(missing_ok=True)
        if existing:
            return existing
        document = {"id": identifier, "title": Path(filename).stem, "filename": filename, "is_demo": False, "created_at": now(), "page_count": result["total"], "warning": result["warning"], "file_url": f"/api/documents/{identifier}/file", "extraction_mode": mode, "pdf_sha256": hashlib.sha256(content).hexdigest()}
        records = [("documents", document)]
        for page in result["pages"]:
            records.extend(page_records(identifier, page))
        # Duplicate uploads may finish concurrently; never overwrite saved proofreading.
        saved = store.mutate(lambda: [] if store.get("documents", identifier) else records)
        if not saved:
            return get_record("documents", identifier)
        return document

    @app.get("/api/documents/{identifier}/file")
    def document_file(identifier: str):
        document = get_record("documents", identifier)
        if not document.get("file_url"):
            raise HTTPException(404, "该材料没有原始 PDF。")
        path = storage / "uploads" / f"{document['id']}.pdf"
        if not path.is_file():
            raise HTTPException(404, "原始 PDF 文件不存在，请重新导入材料。")
        return FileResponse(path, filename=Path(document["filename"]).with_suffix(".pdf").name, media_type="application/pdf")

    def page_file(identifier, page):
        get_record("source_pages", page_id(identifier, page))
        document = get_record("documents", identifier)
        path = storage / "uploads" / f"{document['id']}.pdf"
        if not document.get("file_url") or not path.is_file():
            raise HTTPException(404, "原始 PDF 文件不存在，请重新导入材料。")
        return path

    @app.post("/api/card-media")
    async def upload_card_media(file: UploadFile = File(...)):
        try:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
        finally:
            await file.close()
        normalized = await asyncio.to_thread(normalize_image, content)
        return save_media(store, storage, normalized)

    @app.post("/api/card-media/from-page")
    async def copy_page_media(body: SourcePageImageRequest):
        path = page_file(body.document_id, body.page)
        def check_revision():
            current = get_record("source_pages", page_id(body.document_id, body.page))
            if current["revision"] != body.revision:
                raise HTTPException(409, "该讲义页已更新，请刷新版本后重新添加原图。")
        check_revision()
        try:
            png = await asyncio.to_thread(render_page_png, path.read_bytes(), body.page)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        normalized = await asyncio.to_thread(normalize_image, png)
        return save_media(store, storage, normalized, source=body.model_dump(), check=check_revision)

    @app.get("/api/card-media/{identifier}/file")
    def card_media_file(identifier: str):
        _, path = require_media(store, storage, identifier)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/documents/{identifier}/pages/{page}/image")
    async def page_image(identifier: str, page: int):
        path = page_file(identifier, page)
        image_dir = storage / "page-images" / identifier
        target = image_dir / f"{page}.png"
        if not target.is_file():
            async with locks.setdefault(f"image:{identifier}:{page}", asyncio.Lock()):
                if not target.is_file():
                    try:
                        png = await asyncio.to_thread(render_page_png, path.read_bytes(), page)
                    except ValueError as error:
                        raise HTTPException(422, str(error)) from error
                    image_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(png)
        return FileResponse(target, media_type="image/png")

    @app.get("/api/documents/{identifier}/pages/{page}/revisions")
    def page_history(identifier: str, page: int):
        get_record("source_pages", page_id(identifier, page))
        return {"revisions": [item for item in store.all("source_revisions") if item["document_id"] == identifier and item["page"] == page],
                "confirmations": [item for item in store.all("source_confirmations") if item["document_id"] == identifier and item["page"] == page]}

    @app.put("/api/documents/{identifier}/pages/{page}")
    def correct_page(identifier: str, page: int, body: SourceCorrectionRequest):
        get_record("documents", identifier)
        text = body.text.strip()
        return save_page_revision(store, identifier, page, body.revision,
                                  {"text": text, "status": "reviewed" if text else "empty", "error": None,
                                   "reviewed_at": now()}, "proofread")

    @app.post("/api/documents/{identifier}/pages/{page}/ocr")
    async def reocr_page(identifier: str, page: int, body: SourceRevisionRequest):
        path = page_file(identifier, page)
        old = get_record("source_pages", page_id(identifier, page))
        if old["revision"] != body.revision:
            raise HTTPException(409, "此页已更新，请刷新版本后重试识别。")
        try:
            result = await asyncio.to_thread(recognize_page, path.read_bytes(), page)
        except Exception:
            result = {"page": page, "text": "", "raw_text": "", "method": "ocr", "engine": "RapidOCR", "engine_version": "unknown", "status": "failed", "error": "本地 OCR 未完成；可以重试识别，或对照原图手工录入。"}
        result["reviewed_at"] = None
        result.setdefault("error", None)
        result.setdefault("models", [])
        result.setdefault("pipeline_version", None)
        # A failed/legacy engine response must not inherit previous geometry.
        for key, default in (("ocr_lines", []), ("image_width", None), ("image_height", None), ("page_image_sha256", None)):
            result.setdefault(key, default)
        return save_page_revision(store, identifier, page, body.revision, result, "ocr")

    @app.post("/api/card-drafts")
    async def generate(body: GenerateRequest):
        blocks = [SourceBlock.model_validate(get_record("blocks", block_id)) for block_id in dict.fromkeys(body.block_ids)]
        require_current_source(store, blocks)
        if len({block.document_id for block in blocks}) != 1:
            raise HTTPException(422, "每次请选择同一份材料中的片段。")
        if sum(len(block.text) for block in blocks) > 16000:
            raise HTTPException(422, "本次选中的原文过长，请减少片段。")
        try:
            batch = await app.state.ai.generate_drafts(blocks, body.count)
            if len(batch.cards) > body.count:
                raise ValueError("模型返回题量超过本次请求。")
            for card in batch.cards:
                validate_card(card, blocks)
        except (AIError, ValueError) as error:
            raise HTTPException(422, str(error)) from error
        document = get_record("documents", blocks[0].document_id)
        cards = [{**card.model_dump(), "id": str(uuid4()), "document_id": document["id"], "status": "draft", "version": 1, "is_demo": document["is_demo"], "created_at": now()} for card in batch.cards]
        store.put_many([("cards", card) for card in cards], check=lambda: require_current_source(store, blocks))
        return {"cards": [await review_saved_card(card) for card in cards]}

    def persist_card(identifier: str, body: CardUpdate):
        old = get_record("cards", identifier)
        if body.expected_version is not None and body.expected_version != old["version"]:
            raise HTTPException(409, "卡片版本已更新，请刷新后重新核对；当前草稿未覆盖已保存的内容。")
        changes = body.model_dump(exclude_none=True, exclude={"expected_version"})
        candidate = {**old, **changes, "version": old["version"] + 1, "updated_at": now()}
        if old.get("auto_quality"):
            content_changed = any(candidate.get(name) != old.get(name)
                                  for name in (*CardDraft.model_fields, "images", "image_intent"))
            candidate["auto_quality"] = {**old["auto_quality"], "card_version": candidate["version"],
                                         **({"verdict": "stale"} if content_changed else {})}
            if content_changed and old.get("image_result"):
                candidate["image_result"] = {**old["image_result"], "status": "stale",
                                             "message": "文字、引用或配图已修改，旧配图检查已过期；请在配图区域重新核对。"}
        # A saved edit creates a new content version. Only a fresh check or an
        # explicit human approval can release it for learning again.
        candidate.pop("approval", None)
        candidate.pop("approval_method", None)
        candidate.pop("automatic_evidence", None)
        candidate["review_state"] = "needs_review"
        if (candidate.get("auto_illustration") or {}).get("state") in {"queued", "matching", "checking"}:
            candidate["auto_illustration"] = {**candidate["auto_illustration"], "state": "interrupted",
                                              "message": "卡片已修改，旧配图任务不会覆盖本次编辑；可重新匹配配图。"}
        try:
            blocks = source_models(candidate)
            if any(block.document_id != old["document_id"] for block in blocks):
                raise ValueError("引用必须来自这道题所属的材料。")
            validate_card(card_data(candidate), blocks)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        new_suggestions = []
        old_ids = {item["media_id"] for item in old.get("images") or []}
        intent = old.get("image_intent") or {}
        cited_ids = {block.block_id for block in blocks}
        if (intent.get("need") in {"helpful", "required"}
                and not set(intent.get("block_ids") or []) <= cited_ids):
            # Editors update evidence, not generation metadata. Keep the image
            # requirement usable after a source revision or a deliberate move
            # to different source pages; never silently turn a required image off.
            previous_pages = {(store.get("blocks", block_id) or {}).get("page")
                              for block_id in intent["block_ids"]}
            current_pages = {block.page for block in blocks}
            rebound = [block for block in blocks if block.page in previous_pages]
            if not previous_pages <= current_pages or not rebound:
                rebound = blocks
            candidate["image_intent"] = {**intent, "block_ids": [block.block_id for block in rebound]}
            candidate["status"] = "draft"
            target_pages = {block.page for block in rebound}
            retained = []
            for attachment in candidate.get("images") or []:
                media = store.get("media", attachment["media_id"])
                source = (media or {}).get("source") or {}
                current_page = store.get("source_pages", page_id(old["document_id"], source.get("page"))) if source else None
                outdated = (not media or (source and (
                    source.get("document_id") != old["document_id"] or source.get("page") not in target_pages
                    or not current_page or source.get("revision") != current_page.get("revision"))))
                if attachment["media_id"] in old_ids and outdated:
                    continue  # Detach the stale reference; retain its media file.
                retained.append(attachment)
            candidate["images"] = retained
            candidate["auto_illustration"] = {"state": "interrupted", "card_version": candidate["version"],
                                               "updated_at": now(),
                                               "message": "引用已更新，配图要求已关联当前原文；旧版本或其他页的配图已移除，请重新匹配并检查。"}
        for attachment in candidate.get("images") or []:
            record, _ = require_media(store, storage, attachment["media_id"], old["document_id"])
            # New suggestions enter the automatic text-and-image check as drafts.
            if record.get("source", {}).get("origin") == "figure_recommendation" and attachment["media_id"] not in old_ids:
                if candidate["status"] == "approved":
                    candidate["status"] = "draft"
                new_suggestions.append(record)
        if candidate["status"] == "approved":
            candidate["approval"] = {"method": "human", "card_version": candidate["version"], "approved_at": now()}
            candidate["review_state"] = "human_approved"
        def check_card_version():
            if get_record("cards", identifier)["version"] != old["version"]:
                raise HTTPException(409, "卡片或原文刚刚更新，请刷新后核对再保存。")
            for media_record in new_suggestions:
                recommendations.validate_new_attachment(media_record, identifier, old["version"], {block.page for block in blocks}, candidate)
            try:
                require_current_source(store, blocks)
            except HTTPException:
                if candidate["status"] == "approved":
                    raise
                candidate["source_outdated"] = True
            else:
                candidate["source_outdated"] = False
            if candidate["status"] == "approved":
                require_required_image(store, candidate)
                for attachment in candidate.get("images") or []:
                    require_image_source(store, candidate, get_record("media", attachment["media_id"]))
        store.put_many([("cards", candidate)], check=check_card_version)
        return candidate

    @app.put("/api/cards/{identifier}")
    async def update_card(identifier: str, body: CardUpdate):
        candidate = await asyncio.to_thread(persist_card, identifier, body)
        return await review_saved_card(candidate)

    @app.post("/api/attempts")
    async def submit(body: AttemptRequest):
        return await app.state.attempts.submit(body.card_id, body.answer, body.request_id)

    @app.get("/api/attempts/{identifier}")
    def attempt_detail(identifier: str):
        return app.state.attempts.get(identifier)

    @app.post("/api/attempts/{identifier}/retry")
    async def retry(identifier: str):
        return await app.state.attempts.retry(identifier)

    def export_records(request, reader=None, document_id=None):
        source = reader or store
        if document_id:
            get_record("documents", document_id, reader)
        for card in source.all("cards"):
            if card["status"] != "approved" or (document_id and card["document_id"] != document_id):
                continue
            require_exportable(card, source)
            document = get_record("documents", card["document_id"], reader)
            blocks = source_models(card, reader)
            pages = ", ".join(str(page) for page in sorted({block.page for block in blocks})) if document["id"].startswith("pdf-") else ""
            yield card, [card["id"], card["question"], card["answer"], card["concept"], document["filename"], pages, "\n".join(entry["quote"] for entry in card["evidence"]), f"{str(request.base_url).rstrip('/')}/?card={card['id']}"]

    @app.get("/api/export")
    def export(request: Request, document_id: str | None = None):
        with export_snapshot(store) as snapshot:
            stream = io.StringIO(newline="")
            # Anki import directives; data has no header note and HTML is disabled.
            stream.write(f"#separator:Tab\n#html:false\n#columns:{EXPORT_COLUMNS}\n")
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            for _, values in export_records(request, snapshot, document_id):
                writer.writerow(values)
        return Response(stream.getvalue().encode("utf-8"), media_type="text/tab-separated-values; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ankisense-approved.tsv"'})

    def archive_response(request, builder, filename, media_type, document_id):
        try:
            temporary = TemporaryDirectory(prefix="ankisense-download-")
        except OSError as error:
            raise export_error(error) from error
        try:
            target = Path(temporary.name) / filename
            with export_snapshot(store) as snapshot:
                builder(export_records(request, snapshot, document_id), snapshot, storage, target)
            return ExportResponse(target, temporary, filename=filename, media_type=media_type)
        except OSError as error:
            temporary.cleanup()
            raise export_error(error) from error
        except BaseException:
            temporary.cleanup()
            raise

    @app.get("/api/export/media")
    def export_with_media(request: Request, document_id: str | None = None):
        return archive_response(request, media_zip, "ankisense-approved-media.zip", "application/zip", document_id)

    @app.get("/api/export/apkg")
    def export_deck(request: Request, document_id: str | None = None):
        return archive_response(request, build_apkg, "ankisense-approved.apkg", "application/octet-stream", document_id)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
