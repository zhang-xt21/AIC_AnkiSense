"""One upload starts the complete local import and cloud card pipeline."""
import asyncio
import hashlib
import threading
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .auto_cards import ACTIVE, AutoCardRequest, now
from .ingest import parse_pdf_document
from .presentations import convert_to_pdf, slide_text
from .sources import page_records

MAX_FILE = 50 * 1024 * 1024


class ImportCancelled(Exception):
    """Cooperatively stop the local parser at its next page boundary."""


class AutomaticImportService:
    def __init__(self, store, storage, cards):
        self.store, self.storage, self.cards = store, storage, cards
        self.tasks = {}
        self.stops = {}
        self.queue = asyncio.Lock()
        self.lock = asyncio.Lock()
        self.router = APIRouter()
        self.router.add_api_route("/api/material-imports", self.start, methods=["POST"])
        self.router.add_api_route("/api/material-imports", self.list, methods=["GET"])
        self.router.add_api_route("/api/material-imports/{identifier}", self.get, methods=["GET"])
        self.router.add_api_route("/api/material-imports/{identifier}/retry", self.retry, methods=["POST"])
        self.router.add_api_route("/api/material-imports/{identifier}/cancel", self.cancel, methods=["POST"])

    def public(self, record):
        result = {k: record.get(k) for k in ("id", "filename", "status", "message", "progress", "document_id", "job_id", "error", "created_at", "updated_at", "options", "attempt")}
        if record.get("job_id"):
            result["job"] = self.cards.get(record["job_id"])
            result["status"] = result["job"]["status"]
            result["message"] = result["job"]["message"]
            result["error"] = result["job"].get("error")
            result["progress"] = result["job"].get("progress")
            result["plan"] = result["job"].get("plan")
            result["failure"] = result["job"].get("failure")
        result["retryable"] = (result.get("job") or {}).get("retryable", result["status"] in {"partial", "failed", "cancelled", "awaiting_confirmation", "interrupted"})
        return result

    def list(self):
        return {"imports": [self.public(r) for r in reversed(self.store.all("material_imports"))]}

    def get(self, identifier: str):
        record = self.store.get("material_imports", identifier)
        if not record:
            raise HTTPException(404, "导入任务不存在。")
        return self.public(record)

    def update(self, identifier, **values):
        def build():
            record = self.store.get("material_imports", identifier)
            return [("material_imports", {**record, **values, "updated_at": now()})]
        self.store.mutate(build)

    async def start(self, file: UploadFile = File(...), count: int | None = Form(default=None, ge=1, le=600),
                    auto_images: bool = Form(default=True)):
        self.cards._configuration()
        filename = (file.filename or "lecture.pdf").replace("\\", "/").split("/")[-1][:150]
        extension = Path(filename).suffix.lower()
        if extension not in {".pdf", ".pptx", ".ppt"}:
            await file.close()
            raise HTTPException(422, "请选择 PPT、PPTX 或 PDF 讲义。")
        content = await file.read(MAX_FILE + 1)
        await file.close()
        if len(content) > MAX_FILE:
            raise HTTPException(413, "讲义文件请控制在 50 MB 以内。")
        if not content or (extension == ".pdf" and not content.startswith(b"%PDF-")) or (extension == ".pptx" and not content.startswith(b"PK")) or (extension == ".ppt" and not content.startswith(bytes.fromhex("d0cf11e0a1b11e1"))):
            raise HTTPException(422, "文件内容与格式不符，请选择有效的讲义。")
        identifier = "import-" + hashlib.sha256(content + extension.encode()).hexdigest()[:24]
        async with self.lock:
            return self._start_uploaded(identifier, filename, extension, content, count, auto_images)

    def _start_uploaded(self, identifier, filename, extension, content, count, auto_images):
        existing = self.store.get("material_imports", identifier)
        if existing:
            return self.public(existing)
        directory = self.storage / "incoming" / identifier
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("lecture" + extension)
        path.write_bytes(content)
        record = {"id": identifier, "filename": filename, "extension": extension, "status": "pending",
                  "message": "讲义已接收，正在自动处理。", "document_id": None, "job_id": None,
                  "created_at": now(), "updated_at": now(), "error": None, "attempt": 1,
                  "options": {"count": count, "auto_images": auto_images}}
        self.store.put("material_imports", record)
        self.launch(identifier)
        return self.public(record)

    async def retry(self, identifier: str):
        """Retry explicitly from saved upload bytes; repeated clicks are idempotent."""
        async with self.lock:
            self.cards._configuration()
            record = self.store.get("material_imports", identifier)
            if not record:
                raise HTTPException(404, "导入任务不存在。")
            current = self.public(record)
            if current["status"] in ACTIVE or identifier in self.tasks:
                return current
            if not current["retryable"]:
                raise HTTPException(409, "此导入无需重试；来源变化后请重新导入材料。")
            if record.get("job_id"):
                await self.cards.retry(record["job_id"])
            else:
                self.update(identifier, status="pending", error=None,
                            attempt=record.get("attempt", 1) + 1, message="正在重试已上传的讲义。")
                self.launch(identifier)
            return self.public(self.store.get("material_imports", identifier))

    async def cancel(self, identifier: str):
        async with self.lock:
            record = self.store.get("material_imports", identifier)
            if not record:
                raise HTTPException(404, "导入任务不存在。")
            if record.get("job_id"):
                await self.cards.cancel(record["job_id"])
            elif record["status"] in ACTIVE:
                self.update(identifier, status="cancelled", error=None,
                            message="已取消后续处理；上传文件保留，可重新继续。当前页识别或转换可能仍在结束。")
                if stop := self.stops.get(identifier):
                    stop.set()
                if task := self.tasks.get(identifier):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            return self.public(self.store.get("material_imports", identifier))

    def launch(self, identifier):
        if identifier in self.tasks and not self.tasks[identifier].done():
            return
        stop = threading.Event()
        self.stops[identifier] = stop
        task = asyncio.create_task(self.run(identifier, stop))
        self.tasks[identifier] = task
        def done(completed):
            if self.tasks.get(identifier) is completed:
                self.tasks.pop(identifier, None)
                self.stops.pop(identifier, None)
        task.add_done_callback(done)

    def recover(self):
        for record in self.store.all("material_imports"):
            if record["status"] in ACTIVE and not record.get("job_id"):
                self.launch(record["id"])

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, identifier, stop):
        async with self.queue:
            try:
                record = self.store.get("material_imports", identifier)
                directory = self.storage / "incoming" / identifier
                path = directory / ("lecture" + record["extension"])
                self.update(identifier, status="running", message="正在读取讲义原文和页面。")
                native = await asyncio.to_thread(slide_text, path) if record["extension"] == ".pptx" else None
                if record["extension"] != ".pdf":
                    self.update(identifier, message="正在自动转换幻灯片，保留原页图表。")
                    content = await asyncio.to_thread(convert_to_pdf, path, directory)
                else:
                    content = await asyncio.to_thread(path.read_bytes)
                def progress(completed, total):
                    def build():
                        record = self.store.get("material_imports", identifier)
                        if stop.is_set() or record["status"] not in ACTIVE:
                            raise ImportCancelled()
                        return [("material_imports", {**record, "progress": {"completed": completed, "total": total},
                            "message": f"正在识别讲义 {completed}/{total} 页。", "updated_at": now()})]
                    self.store.mutate(build)
                result = await asyncio.to_thread(parse_pdf_document, content, max_pages=500, max_characters=1000000, progress=progress)
                doc_id = "pdf-" + hashlib.sha256((identifier + ":automatic-v1").encode()).hexdigest()[:24]
                uploads = self.storage / "uploads"
                uploads.mkdir(exist_ok=True)
                (uploads / f"{doc_id}.pdf").write_bytes(content)
                document = {"id": doc_id, "title": Path(record["filename"]).stem, "filename": record["filename"],
                            "page_count": result["total"], "file_url": f"/api/documents/{doc_id}/file",
                            "is_demo": False, "created_at": now(), "automatic_import": True,
                            "warning": "系统自动对照原页检查卡牌；原文逐页校对是可选操作。", "extraction_mode": "auto",
                            "pdf_sha256": hashlib.sha256(content).hexdigest()}
                records = [("documents", document)]
                for index, page in enumerate(result["pages"]):
                    if native and len(native) == result["total"] and native[index].strip():
                        page.update(text=native[index], raw_text=native[index], method="text", engine="pptx-native", status="pending")
                    records.extend(page_records(doc_id, page))
                self.store.mutate(lambda: [] if self.store.get("documents", doc_id) else records)
                self.update(identifier, document_id=doc_id, message="讲义已识别，正在自动启动制卡。")
                while any(j["status"] in ACTIVE for j in self.store.all("auto_card_jobs")):
                    self.update(identifier, message="讲义已就绪，正在等待前一份材料处理完成。")
                    await asyncio.sleep(1)
                options = record.get("options") or {}
                job = await self.cards.start(doc_id, AutoCardRequest(max_cards=600,
                    count=options.get("count"), image_mode="source_page" if options.get("auto_images", True) else "off", autonomous=True))
                self.update(identifier, job_id=job["id"], status="completed", message="已开始自动制卡。")
            except asyncio.CancelledError:
                stop.set()
                if self.store.get("material_imports", identifier)["status"] in ACTIVE:
                    self.update(identifier, status="pending", message="服务恢复后会自动继续本地识别。")
            except ImportCancelled:
                pass
            except Exception as error:
                message = error.detail if isinstance(error, HTTPException) else str(error) if isinstance(error, ValueError) else "讲义导入未完成，请检查文件后重新导入。"
                self.update(identifier, status="failed", error=message, message=message)
