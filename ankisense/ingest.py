import hashlib
import re
from io import BytesIO

from pypdf import PdfReader

from .ocr import package_version, recognize_page, render_page_png


def automatic_page_skip(text):
    compact = re.sub(r"\s+", "", text)
    headings = [re.sub(r"\s+", "", line).strip("•-–") for line in text.splitlines()[:3]]
    if any(line.casefold() in {"目录", "内容提要", "课程安排", "本章内容", "本讲内容", "教学目标", "主要教学目标", "本章教学目标", "本章主要教学目标", "学习目标", "agenda", "outline", "contents"} for line in headings) or re.search(r"分以下.{0,8}(?:部分|章节)介绍", compact[:180]):
        return "目录或课程安排已自动跳过。"
    if re.fullmatch(r"[\w.\-]+\.(?:mp4|avi|mov|wmv)", compact, re.IGNORECASE):
        return "视频文件名没有独立知识内容，已自动跳过。"
    return None


def text_blocks(document_id: str, pages: list[tuple[int, str]]) -> list[dict]:
    blocks = []
    for page, text in pages:
        # Keep exact characters for quote validation; chunking never rewrites text.
        for offset in range(0, len(text), 3000):
            part = text[offset:offset + 3000].strip()
            if part:
                blocks.append({"block_id": f"{document_id}-p{page}-b{offset // 3000 + 1}", "document_id": document_id, "page": page, "text": part})
    return blocks


def parse_pdf(content: bytes, start: int = 1, end: int | None = None):
    """Legacy text-only helper; the application uses parse_pdf_document."""
    if not content.startswith(b"%PDF-"):
        raise ValueError("请选择有效的 PDF 文件。")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF，请先导出未加密副本。")
        total = len(reader.pages)
        end = total if end is None else end
        if not 1 <= start <= end <= total:
            raise ValueError(f"页码范围无效；该 PDF 共 {total} 页。")
        if end - start + 1 > 30:
            raise ValueError("初版每次最多解析 30 页，请选择本章节的页码范围。")
        pages = [(number, reader.pages[number - 1].extract_text() or "") for number in range(start, end + 1)]
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("无法解析此 PDF，请确认文件完整且包含可选择的文字。") from error
    if not any(text.strip() for _, text in pages):
        raise ValueError("未提取到文字；请使用按页 OCR 导入，或粘贴已核对的原文。")
    if sum(len(text) for _, text in pages) > 100000:
        raise ValueError("所选内容过长，请缩小页码范围。")
    digest = hashlib.sha256(content + f":{start}:{end}".encode()).hexdigest()[:24]
    document_id = f"pdf-{digest}"
    empty = [str(page) for page, text in pages if not text.strip()]
    warning = "请对照原 PDF 核对阅读顺序、公式和条件；页码使用 PDF 物理页码。"
    if empty:
        warning += " 以下页面未提取到文字：" + "、".join(empty)
    return document_id, text_blocks(document_id, pages), total, warning


def _has_usable_text(text: str) -> bool:
    """Route sparse or visibly broken text layers to OCR, page by page.

    This is deliberately a conservative heuristic, not a claim that a text
    layer is complete. The proofreading view can force OCR on any page.
    """
    visible = [char for char in text if not char.isspace()]
    if sum(char.isalnum() for char in visible) < 12:
        return False
    damaged = sum(char == "\ufffd" or not char.isprintable() for char in visible)
    return damaged / max(1, len(visible)) < 0.02


def parse_pdf_document(content: bytes, start: int = 1, end: int | None = None,
                       mode: str = "auto", *, max_pages: int = 30,
                       max_characters: int = 100000, progress=None) -> dict:
    """Extract each selected physical page and retain proofreading metadata.

    Unlike the legacy parse_pdf helper, per-page OCR failures are retained in
    the returned document so the user can retry or manually transcribe them.
    """
    if mode not in {"auto", "text", "ocr"}:
        raise ValueError("识别方式无效，请选择自动识别、仅提取文字或全部 OCR。")
    if not content.startswith(b"%PDF-"):
        raise ValueError("请选择有效的 PDF 文件。")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF，请先导出未加密副本。")
        total = len(reader.pages)
        end = total if end is None else end
        if not 1 <= start <= end <= total:
            raise ValueError(f"页码范围无效；该 PDF 共 {total} 页。")
        if end - start + 1 > max_pages:
            raise ValueError(f"每次最多解析 {max_pages} 页，请拆分讲义后导入。")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("无法解析此 PDF，请确认文件完整且未加密。") from error

    pages = []
    for number in range(start, end + 1):
        extraction_failed = False
        try:
            extracted = reader.pages[number - 1].extract_text() or ""
        except Exception:
            extracted = ""
            extraction_failed = True
        use_ocr = mode == "ocr" or (mode == "auto" and not _has_usable_text(extracted))
        if use_ocr:
            try:
                page = recognize_page(content, number)
            except Exception:
                page = {
                    "page": number, "text": "", "raw_text": "", "method": "ocr",
                    "engine": "RapidOCR / ONNX Runtime CPU",
                    "engine_version": package_version("rapidocr"), "status": "failed",
                    "error": "该页本地 OCR 识别失败，请查看原图后重试，或手动输入校订文字。",
                }
            page["extracted_text"] = extracted
        else:
            page = {
                "page": number, "text": extracted, "raw_text": extracted, "method": "text",
                "engine": "pypdf", "engine_version": package_version("pypdf"),
                "status": "failed" if extraction_failed else ("pending" if extracted.strip() else "empty"),
            }
            if extraction_failed:
                page["error"] = "该页文字层提取失败，可尝试本地 OCR 或手动校订。"
        if len(page["text"]) > 50000:
            raise ValueError(f"第 {number} 页文字超过 5 万字，请拆分材料或检查 PDF 文字层后重试。")
        pages.append(page)
        if sum(len(item["text"]) for item in pages) > max_characters:
            raise ValueError("所选内容过长，请缩小页码范围。")
        if progress:
            progress(number - start + 1, end - start + 1)

    # Separate proofreading imports from legacy documents and from forced OCR.
    digest = hashlib.sha256(content + f":{start}:{end}:proof-v1:{mode}".encode()).hexdigest()[:24]
    document_id = f"pdf-{digest}"
    warning = "可直接按讲义制卡并自动检查，逐页核对原文是可选操作。页码为 PDF 物理页码，OCR 可能误识别公式、符号、表格和阅读顺序。"
    failed = [str(page["page"]) for page in pages if page["status"] == "failed"]
    empty = [str(page["page"]) for page in pages if page["status"] == "empty"]
    if failed:
        warning += " 以下页面处理失败，需重试或手动校订：" + "、".join(failed) + "。"
    if empty:
        warning += " 以下页面未识别到文字，请对照原图检查：" + "、".join(empty) + "。"
    return {
        "id": document_id,
        "blocks": text_blocks(document_id, [(page["page"], page["text"]) for page in pages]),
        "total": total,
        "warning": warning,
        "pages": pages,
    }
