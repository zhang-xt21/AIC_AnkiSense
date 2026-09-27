"""Bounded local raster attachments, independent of the text-only AI schema."""

import csv
import html
import io
import math
import re
import warnings
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_INPUT_SIDE = 8192
MAX_INPUT_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 2400
MAX_IMAGE_PIXELS = 4_000_000
MAX_EXPORT_BYTES = 50 * 1024 * 1024
MEDIA_ID = re.compile(r"img-[0-9a-f]{32}\Z")
EXPORT_COLUMNS = "NoteID\tQuestion\tAnswer\tConcept\tSourceFile\tPDFPage\tSourceQuote\tPracticeURL"
IMPORT_INSTRUCTIONS = """AnkiSense 图片卡导入说明

1. 解压此 ZIP。将 media 文件夹中的 PNG 文件直接复制到当前 Anki 配置的 collection.media 文件夹，不要复制整个 media 文件夹，也不要改名。
2. 在 Anki 中导入 ankisense-approved.tsv，分隔符选择制表符，并启用“允许在字段中使用 HTML”。文件也包含 #html:true 导入指令。
3. 使用包含以下 8 个字段的笔记类型，按顺序映射：NoteID、Question、Answer、Concept、SourceFile、PDFPage、SourceQuote、PracticeURL。
4. 将 NoteID 作为第一个字段，以同一 NoteID 更新已有笔记；正面模板使用 {{Question}}，背面模板使用 {{Answer}}。重复导出保留稳定 NoteID 和图片文件名。
5. 本包仅包含已审核卡片及其所用图片。题面图片只在 Question 中，答案图片只在 Answer 中。图片均为本地 PNG，图片说明按普通文字转义。
6. PracticeURL 指向本机 AnkiSense，须先启动应用。图片不参与 AI 的文本评分；讲义原文引用仍是文字证据。

纯文本导出仍可使用应用中的原有 TSV 导出入口。
"""


def normalize_image(content: bytes) -> tuple[bytes, int, int]:
    """Decode allowed formats, reject oversized input, and discard all metadata."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "图片请控制在 5 MiB 以内。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as candidate:
                if candidate.format not in {"PNG", "JPEG", "WEBP"}:
                    raise HTTPException(422, "仅支持 PNG、JPEG、WebP 静态图片。")
                width, height = candidate.size
                if max(width, height) > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:
                    raise HTTPException(422, "图片尺寸过大；最长边最多 8192 像素，总像素最多 1600 万。")
                if getattr(candidate, "n_frames", 1) != 1:
                    raise HTTPException(422, "请上传单帧静态图片。")
                candidate.verify()
            with Image.open(io.BytesIO(content)) as candidate:
                candidate.load()
                with ImageOps.exif_transpose(candidate) as oriented:
                    mode = "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
                    # A new image guarantees EXIF, ICC, comments and textual chunks
                    # cannot survive even when Pillow copies info during conversion.
                    with Image.new(mode, oriented.size) as clean:
                        with oriented.convert(mode) as converted:
                            clean.paste(converted)
                        scale = min(1.0, MAX_IMAGE_SIDE / max(clean.size),
                                    math.sqrt(MAX_IMAGE_PIXELS / (clean.width * clean.height)))
                        clean.thumbnail((max(1, int(clean.width * scale)), max(1, int(clean.height * scale))), Image.Resampling.LANCZOS)
                        output = io.BytesIO()
                        clean.save(output, format="PNG")
                        png = output.getvalue()
                        if len(png) > MAX_UPLOAD_BYTES:
                            raise HTTPException(413, "规范化后的图片超过 5 MiB，请缩小图片后重试。")
                        return png, clean.width, clean.height
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise HTTPException(422, "无法读取图片；请使用完整的 PNG、JPEG 或 WebP 静态图片。") from error


def media_path(storage: Path, identifier: str) -> Path:
    if not MEDIA_ID.fullmatch(identifier):
        raise HTTPException(404, "图片记录不存在。")
    return storage / "media" / f"{identifier}.png"


def require_media(store, storage: Path, identifier: str, document_id: str | None = None):
    path = media_path(storage, identifier)
    record = store.get("media", identifier)
    if not record or not path.is_file():
        raise HTTPException(404, "图片文件不存在，请重新添加图片。")
    if document_id is not None and record.get("source", {}).get("document_id", document_id) != document_id:
        raise HTTPException(422, "讲义页图片必须来自这道题所属的材料。")
    return record, path


def save_media(store, storage: Path, normalized, source: dict | None = None, check=None):
    png, width, height = normalized
    identifier = "img-" + uuid4().hex
    path = media_path(storage, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"id": identifier, "url": f"/api/card-media/{identifier}/file",
              "width": width, "height": height, "filename": f"ankisense-{identifier}.png"}
    if source is not None:
        record["source"] = source.copy()
    # Exclusive creation + fresh IDs keep old attempts' files immutable. There
    # is deliberately no unlink-on-card-edit path: snapshots retain references.
    with path.open("xb") as target:
        target.write(png)
    try:
        store.put_many([("media", record)], check=check)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return record


def html_text(value) -> str:
    return html.escape(str(value), quote=True).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def media_zip(rows, store, storage: Path) -> bytes:
    """Build an HTML TSV and referenced PNG files under one aggregate cap."""
    tsv = io.BytesIO()
    header = f"#separator:Tab\n#html:true\n#columns:{EXPORT_COLUMNS}\n".encode("utf-8")
    instructions = IMPORT_INSTRUCTIONS.encode("utf-8")
    tsv.write(header)
    total = len(header) + len(instructions)
    if total > MAX_EXPORT_BYTES:
        raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
    assets = {}
    for card, values in rows:
        fields = [html_text(value) for value in values]
        for attachment in card.get("images") or []:
            record, path = require_media(store, storage, attachment["media_id"], card["document_id"])
            identifier = record["id"]
            if identifier not in assets:
                size = path.stat().st_size
                total += size
                if size > MAX_UPLOAD_BYTES or total > MAX_EXPORT_BYTES:
                    raise HTTPException(413, "图片导出包超过 50 MiB，请减少本次已审核卡片或图片。")
                assets[identifier] = (path, size)
            caption = html_text(attachment.get("caption", ""))
            field = 1 if attachment.get("side", "answer") == "question" else 2
            # Filenames are regenerated from validated IDs, never user input.
            filename = f"ankisense-{identifier}.png"
            fields[field] += f'<br><img src="{filename}" alt="{html.escape(attachment.get("caption", ""), quote=True)}">'
            if caption:
                fields[field] += f"<br>{caption}"
        row = io.StringIO(newline="")
        csv.writer(row, delimiter="\t", lineterminator="\n").writerow(fields)
        data = row.getvalue().encode("utf-8")
        total += len(data)
        if total > MAX_EXPORT_BYTES:
            raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
        tsv.write(data)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ankisense-approved.tsv", tsv.getvalue())
        archive.writestr("导入说明.txt", instructions)
        for identifier, (path, expected_size) in assets.items():
            # Bound the read too if an external process unexpectedly changed a file.
            with path.open("rb") as source:
                content = source.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "图片文件超过导出限制，请重新添加图片。")
            total += len(content) - expected_size
            if total > MAX_EXPORT_BYTES:
                raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
            archive.writestr(f"media/ankisense-{identifier}.png", content)
            if output.tell() > MAX_EXPORT_BYTES:
                raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
    if output.tell() > MAX_EXPORT_BYTES:
        raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
    return output.getvalue()
