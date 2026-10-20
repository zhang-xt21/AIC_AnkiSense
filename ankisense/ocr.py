"""Local, bounded PDF rendering and printed-text OCR.

PDFium is not thread-safe, including when separate documents are rendered.
Keep every PDFium object and its cleanup inside the rendering lock. OCR has a
separate lock so viewing an original page need not wait for an inference run.
"""

from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import hashlib
import math
from pathlib import Path
import shutil
import threading
from uuid import uuid4


MAX_RENDER_SIDE = 2400
MAX_RENDER_PIXELS = 4_000_000
PIPELINE_VERSION = "printed-zh-en-v1-ppocrv6-small"
MODEL_FILES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}
_PDFIUM_LOCK = threading.Lock()
_OCR_LOCK = threading.Lock()
_ENGINE = None
_ENGINE_INFO = None


class LocalOCRError(ValueError):
    """An actionable error safe to display without local paths or tracebacks."""


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "未安装"


def render_page_png(content: bytes, page: int) -> bytes:
    """Render one physical PDF page to a capped-size PNG, fully in memory."""
    if not isinstance(page, int) or page < 1:
        raise LocalOCRError("PDF 页码无效。")
    with _PDFIUM_LOCK:
        try:
            import pypdfium2 as pdfium
        except ImportError as error:
            raise LocalOCRError("缺少 PDF 原图组件，请安装项目 requirements.txt 中的依赖后重试。") from error
        document = pdf_page = bitmap = image = None
        try:
            document = pdfium.PdfDocument(content)
            if page > len(document):
                raise LocalOCRError("PDF 页码超出文档范围。")
            pdf_page = document[page - 1]
            width, height = pdf_page.get_size()
            if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                raise LocalOCRError("该页尺寸无效，无法生成原图。")
            # A small margin accounts for PDFium rounding dimensions upwards.
            scale = min(3.0, (MAX_RENDER_SIDE - 1) / max(width, height),
                        math.sqrt((MAX_RENDER_PIXELS - 5000) / width / height))
            if min(width, height) * scale < 1:
                raise LocalOCRError("该页长宽比例异常，无法安全生成原图。")
            bitmap = pdf_page.render(scale=scale, rotation=0, fill_color=(255, 255, 255, 255))
            image = bitmap.to_pil()
            if max(image.size) > MAX_RENDER_SIDE or image.width * image.height > MAX_RENDER_PIXELS:
                raise LocalOCRError("该页图像尺寸超出处理限制。")
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except LocalOCRError:
            raise
        except Exception as error:
            raise LocalOCRError("无法生成该页原图，请确认 PDF 完整且未加密。") from error
        finally:
            if image is not None:
                image.close()
            if bitmap is not None:
                bitmap.close()
            if pdf_page is not None:
                pdf_page.close()
            if document is not None:
                document.close()


def _get_engine(model_root=None):
    """Called under _OCR_LOCK; failed initialization may be retried later."""
    global _ENGINE, _ENGINE_INFO
    if _ENGINE is not None:
        return _ENGINE, _ENGINE_INFO
    try:
        import rapidocr
        from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR
        import onnxruntime  # noqa: F401: fail with an actionable message early
    except ImportError as error:
        raise LocalOCRError("本地 OCR 依赖未安装，请安装项目 requirements.txt 中的依赖后重试。") from error
    model_root = Path(model_root) if model_root is not None else Path(__file__).resolve().parents[1] / "data" / "ocr-models"
    try:
        model_root.mkdir(parents=True, exist_ok=True)
        bundled = Path(rapidocr.__file__).resolve().parent / "models"
        models = []
        for filename in MODEL_FILES.values():
            source, target = bundled / filename, model_root / filename
            if not target.is_file():
                # Current pinned wheel bundles these weights. An explicit
                # model_path below prevents RapidOCR downloading on first use.
                if not source.is_file():
                    raise LocalOCRError("本地 OCR 模型文件缺失，请重新安装项目锁定版本的 rapidocr 后重试。")
                temporary = model_root / f".{filename}.{uuid4().hex}.tmp"
                try:
                    shutil.copyfile(source, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            models.append({"filename": filename, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        params = {
            "Global.model_root_dir": str(model_root),
            "Global.log_level": "error",
            "Global.max_side_len": MAX_RENDER_SIDE,
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.use_dml": False,
            "Det.model_type": ModelType.SMALL, "Det.ocr_version": OCRVersion.PPOCRV6,
            "Cls.model_type": ModelType.MOBILE, "Cls.ocr_version": OCRVersion.PPOCRV4,
            "Rec.model_type": ModelType.SMALL, "Rec.ocr_version": OCRVersion.PPOCRV6,
        }
        params.update({f"{stage}.model_path": str(model_root / filename) for stage, filename in MODEL_FILES.items()})
        engine = RapidOCR(params=params)
    except LocalOCRError:
        raise
    except Exception as error:
        raise LocalOCRError("本地 OCR 模型未就绪，请完成 OCR 模型安装后重试；原 PDF 已保留。") from error
    _ENGINE = engine
    _ENGINE_INFO = {
        "engine": "RapidOCR / ONNX Runtime CPU · PP-OCRv6 small",
        "engine_version": f"RapidOCR {package_version('rapidocr')}; ONNX Runtime {package_version('onnxruntime')}",
        "pipeline_version": PIPELINE_VERSION,
        "models": models,
    }
    return _ENGINE, _ENGINE_INFO


def _optional_item(values, index):
    try:
        return values[index]
    except (IndexError, KeyError, TypeError):
        return None


def _raw_score(value):
    # Preserve a finite engine score, not a calibrated correctness probability.
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _normalized_box(box, width, height):
    if box is None or not width or not height:
        return None
    try:
        if hasattr(box, "tolist"):
            box = box.tolist()
        if len(box) == 4 and all(isinstance(point, (int, float)) for point in box):
            left, top, right, bottom = map(float, box)
        else:
            points = [(float(point[0]), float(point[1])) for point in box]
            if len(points) < 4:
                return None
            left, right = min(x for x, _ in points), max(x for x, _ in points)
            top, bottom = min(y for _, y in points), max(y for _, y in points)
        if not all(math.isfinite(value) for value in (left, top, right, bottom)):
            return None
        result = [max(0.0, min(1.0, left / width)), max(0.0, min(1.0, top / height)),
                  max(0.0, min(1.0, right / width)), max(0.0, min(1.0, bottom / height))]
        return result if result[0] < result[2] and result[1] < result[3] else None
    except (IndexError, ValueError, TypeError, OverflowError):
        return None


def _recognize_image(image_bytes: bytes) -> dict:
    """Collect line geometry while keeping the engine's exact text and order."""
    from PIL import Image

    with Image.open(BytesIO(image_bytes)) as original:
        width, height = original.size
    with _OCR_LOCK:
        engine, info = _get_engine()
        recognition = engine(image_bytes)
        recognized = getattr(recognition, "txts", None)
        texts = [] if recognized is None else [str(line) for line in recognized]
        boxes = getattr(recognition, "boxes", None)
        scores = getattr(recognition, "scores", None)
        lines, offset = [], 0
        for index, text in enumerate(texts):
            lines.append({"text": text,
                          "bbox": _normalized_box(_optional_item(boxes, index), width, height),
                          "score": _raw_score(_optional_item(scores, index)),
                          "start": offset, "end": offset + len(text)})
            offset += len(text) + 1  # Newlines join lines without rewriting them.
        text = "\n".join(texts)
        return {**info, "text": text, "raw_text": text, "ocr_lines": lines,
                "image_width": width, "image_height": height,
                "page_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "status": "pending" if text.strip() else "empty"}


def recognize_page(content: bytes, page: int) -> dict:
    """Return recognition text or an explicit empty/failed page result.

    Text is the OCR engine's line order, without spelling correction, so the
    original recognition remains available during human proofreading.
    """
    result = {
        "page": page, "text": "", "raw_text": "", "ocr_lines": [], "method": "ocr",
        "image_width": None, "image_height": None, "page_image_sha256": None,
        "engine": "RapidOCR / ONNX Runtime CPU · PP-OCRv6 small",
        "engine_version": f"RapidOCR {package_version('rapidocr')}; ONNX Runtime {package_version('onnxruntime')}",
        "pipeline_version": PIPELINE_VERSION,
        "status": "failed",
    }
    try:
        image_bytes = render_page_png(content, page)
        result.update(_recognize_image(image_bytes))
        return result
    except LocalOCRError as error:
        result["error"] = str(error)
    except Exception:
        result["error"] = "该页本地 OCR 识别失败，请查看原图后重试，或手动输入校订文字。"
    return result


def recognize_region(content: bytes, page: int, bbox: list[float]) -> dict:
    """Re-read an original-page crop as a suggestion; never save or approve it."""
    from PIL import Image

    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise LocalOCRError("局部识别范围须为原页内的有效矩形。")
    values = [_raw_score(value) for value in bbox]
    if (any(value is None or not 0 <= value <= 1 for value in values)
            or values[0] >= values[2] or values[1] >= values[3]):
        raise LocalOCRError("局部识别范围须为原页内的有效矩形。")
    image_bytes = render_page_png(content, page)
    with Image.open(BytesIO(image_bytes)) as original:
        width, height = original.size
        pixels = (math.floor(values[0] * width), math.floor(values[1] * height),
                  math.ceil(values[2] * width), math.ceil(values[3] * height))
        effective = [pixels[0] / width, pixels[1] / height,
                     pixels[2] / width, pixels[3] / height]
        with original.crop(pixels) as crop:
            output = BytesIO()
            crop.save(output, format="PNG")
    result = _recognize_image(output.getvalue())
    for line in result["ocr_lines"]:
        if line["bbox"]:
            left, top, right, bottom = line["bbox"]
            line["bbox"] = [effective[0] + left * (effective[2] - effective[0]),
                            effective[1] + top * (effective[3] - effective[1]),
                            effective[0] + right * (effective[2] - effective[0]),
                            effective[1] + bottom * (effective[3] - effective[1])]
    result.update(page=page, method="ocr", suggestion_only=True, region_bbox=effective,
                  image_width=width, image_height=height,
                  region_image_sha256=result["page_image_sha256"],
                  page_image_sha256=hashlib.sha256(image_bytes).hexdigest())
    return result
