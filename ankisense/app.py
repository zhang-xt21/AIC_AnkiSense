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
from .apkg import build_apkg, export_snapshot, require_exportable
from .attempts import AttemptService
from .plugin_api import PluginAPI, install_plugin_error_handlers
from .figure_recommendations import RecommendationService
from .figure_vision import FigureVisionService
from .demo import DEMO_ID, seed_records
from .evidence import validate_card
from .ingest import parse_pdf_document, text_blocks
from .ocr import recognize_page, render_page_png
from .media import EXPORT_COLUMNS, MAX_UPLOAD_BYTES, media_zip, normalize_image, require_media, save_media
from .llm import AIError, AIService, Settings
from .model_settings import ModelSettingsFile, install_model_settings, model_request
from .paths import resolve_data_dir
from .schemas import AttemptRequest, CardDraft, CardUpdate, GenerateRequest, SourceBlock, TextDocumentRequest, SourceCorrectionRequest, SourceRevisionRequest, SourcePageImageRequest
from .sources import page_id, page_records, require_current_reviewed, save_page_revision

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
    @asynccontextmanager
    async def lifespan(application):
        # Recover on service startup, never merely when another tool imports the module.
        for attempt in store.all("attempts"):
            if attempt["status"] == "pending":
                attempt.update(status="failed", error="上次分析被服务重启中断；答案已保存，可以重试。")
                store.put("attempts", attempt)
        recommendations.recover()
        try:
            yield
        finally:
            await application.state.attempts.shutdown()
            await image_checks.shutdown()
            await recommendations.shutdown()

    app = FastAPI(title="AnkiSense", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.ai = ai_service or AIService(settings)
    app.state.settings = settings
    app.state.attempts = AttemptService(store, settings, lambda: app.state.ai)
    app.state.plugin = PluginAPI(store, storage, settings, app.state.attempts)
    app.state.recommendations = recommendations
    app.state.image_checks = image_checks
    app.state.active_model_requests = 0
    install_model_settings(app, configuration)
    app.include_router(recommendations.router)
    app.include_router(image_checks.router)
    app.include_router(app.state.plugin.router)
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

    @app.get("/health")
    def health():
        return {"application": "AnkiSense", "api_version": 1,
                "status": "ok", "mode": app.state.settings.mode}

    @app.get("/api/state")
    def state():
        settings = app.state.settings
        return {"mode": settings.mode, "model": settings.model if settings.mode == "cloud" else "内置演示规则", "configured": settings.mode == "demo" or bool(settings.api_key and settings.model and settings.base_url), "image_check": image_checks.status(), **{name: store.all(name) for name in ("documents", "blocks", "cards", "attempts", "source_pages", "media")}}

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
        document = {"id": identifier, "title": Path(filename).stem, "filename": filename, "is_demo": False, "created_at": now(), "page_count": result["total"], "warning": result["warning"], "file_url": f"/api/documents/{identifier}/file", "extraction_mode": mode}
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
        return FileResponse(path, filename=document["filename"], media_type="application/pdf")

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
        return {"revisions": [item for item in store.all("source_revisions") if item["document_id"] == identifier and item["page"] == page]}

    @app.put("/api/documents/{identifier}/pages/{page}")
    def correct_page(identifier: str, page: int, body: SourceCorrectionRequest):
        get_record("documents", identifier)
        old = get_record("source_pages", page_id(identifier, page))
        text = body.text.strip()
        if old["revision"] == body.revision and old["text"] == text and old.get("reviewed_at") and old["status"] in {"reviewed", "empty"}:
            return old
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
        return save_page_revision(store, identifier, page, body.revision, result, "ocr")

    @app.post("/api/card-drafts")
    async def generate(body: GenerateRequest):
        blocks = [SourceBlock.model_validate(get_record("blocks", block_id)) for block_id in dict.fromkeys(body.block_ids)]
        require_current_reviewed(store, blocks)
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
        store.put_many([("cards", card) for card in cards], check=lambda: require_current_reviewed(store, blocks))
        return {"cards": cards}

    @app.put("/api/cards/{identifier}")
    def update_card(identifier: str, body: CardUpdate):
        old = get_record("cards", identifier)
        if body.expected_version is not None and body.expected_version != old["version"]:
            raise HTTPException(409, "卡片版本已更新，请刷新后重新核对；当前草稿未覆盖已保存的内容。")
        changes = body.model_dump(exclude_none=True, exclude={"expected_version"})
        candidate = {**old, **changes, "version": old["version"] + 1, "updated_at": now()}
        try:
            blocks = source_models(candidate)
            if any(block.document_id != old["document_id"] for block in blocks):
                raise ValueError("引用必须来自这道题所属的材料。")
            validate_card(card_data(candidate), blocks)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        new_suggestions = []
        old_ids = {item["media_id"] for item in old.get("images") or []}
        for attachment in candidate.get("images") or []:
            record, _ = require_media(store, storage, attachment["media_id"], old["document_id"])
            # Newly accepted suggestions must be saved as a draft first. A
            # separate explicit approval reviews the final text and pictures.
            if record.get("source", {}).get("origin") == "figure_recommendation" and attachment["media_id"] not in old_ids:
                if candidate["status"] == "approved":
                    candidate["status"] = "draft"
                new_suggestions.append(record)
        if candidate["status"] == "approved":
            candidate["source_outdated"] = False
        def check_card_version():
            if get_record("cards", identifier)["version"] != old["version"]:
                raise HTTPException(409, "卡片或原文刚刚更新，请刷新后核对再保存。")
            for media_record in new_suggestions:
                recommendations.validate_new_attachment(media_record, identifier, old["version"], {block.page for block in blocks}, candidate)
            if candidate["status"] == "approved":
                require_current_reviewed(store, blocks)
        store.put_many([("cards", candidate)], check=check_card_version)
        return candidate

    @app.post("/api/attempts")
    async def submit(body: AttemptRequest):
        return await app.state.attempts.submit(body.card_id, body.answer, body.request_id)

    @app.get("/api/attempts/{identifier}")
    def attempt_detail(identifier: str):
        return app.state.attempts.get(identifier)

    @app.post("/api/attempts/{identifier}/retry")
    async def retry(identifier: str):
        return await app.state.attempts.retry(identifier)

    def export_records(request, reader=None):
        source = reader or store
        for card in source.all("cards"):
            if card["status"] != "approved":
                continue
            require_exportable(card, source)
            document = get_record("documents", card["document_id"], reader)
            blocks = source_models(card, reader)
            pages = ", ".join(str(page) for page in sorted({block.page for block in blocks})) if document["id"].startswith("pdf-") else ""
            yield card, [card["id"], card["question"], card["answer"], card["concept"], document["filename"], pages, "\n".join(entry["quote"] for entry in card["evidence"]), f"{str(request.base_url).rstrip('/')}/?card={card['id']}"]

    @app.get("/api/export")
    def export(request: Request):
        with export_snapshot(store) as snapshot:
            stream = io.StringIO(newline="")
            # Anki import directives; data has no header note and HTML is disabled.
            stream.write(f"#separator:Tab\n#html:false\n#columns:{EXPORT_COLUMNS}\n")
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            for _, values in export_records(request, snapshot):
                writer.writerow(values)
        return Response(stream.getvalue().encode("utf-8"), media_type="text/tab-separated-values; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="ankisense-approved.tsv"'})

    @app.get("/api/export/media")
    def export_with_media(request: Request):
        with export_snapshot(store) as snapshot:
            content = media_zip(export_records(request, snapshot), snapshot, storage)
        return Response(content, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="ankisense-approved-media.zip"'})

    @app.get("/api/export/apkg")
    def export_deck(request: Request):
        with export_snapshot(store) as snapshot:
            content = build_apkg(export_records(request, snapshot), snapshot, storage)
        return Response(content, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="ankisense-approved.apkg"'})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
