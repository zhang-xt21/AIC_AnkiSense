"""Run the isolated local layout parser without importing Torch into the web app."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
PARSER_VERSION = "docling-2.130.0-pdfium-heron-8f39ad3-cpu-v3"
MODEL_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"
TIMEOUT_SECONDS = 300
_LOCK = threading.Lock()
_MESSAGES = {
    "unavailable": "本机尚未安装配图解析组件，请先运行 tools/setup_docling.ps1。",
    "models_missing": "本地布局模型尚未下载完整，请运行 tools/setup_docling.ps1 完成安装。",
    "timeout": "本次本地图块解析超时，请减少所选页数后重试。",
    "invalid_pdf": "无法读取这些 PDF 页面，请检查原文件或重新导入。",
    "parse_failed": "本次本地图块解析失败，可以重试，或使用整页原图手动裁剪。",
}


class DoclingError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "parse_failed"
        self.safe_message = _MESSAGES[self.code]
        super().__init__(self.safe_message)


def _python() -> Path:
    return ROOT / ".venv-docling" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def status() -> dict:
    """Cheap local readiness check; no import, download or inference."""
    if not _python().is_file():
        code = "unavailable"
    elif not (ROOT / "data/docling-models/ready.json").is_file():
        code = "models_missing"
    else:
        try:
            marker = json.loads((ROOT / "data/docling-models/ready.json").read_text(encoding="utf-8"))
            # Output schema changes do not change the fixed model weights.
            # Existing v2 installations can run v3 without a redundant download.
            compatible = (marker.get("docling") == "2.130.0"
                          and marker.get("repo_id") == "docling-project/docling-layout-heron"
                          and marker.get("revision") == MODEL_REVISION)
            code = "ready" if compatible else "models_missing"
            files = marker.get("files", [])
            if not files or any(not (ROOT / "data/docling-models" / item["path"]).is_file()
                or (ROOT / "data/docling-models" / item["path"]).stat().st_size != item["bytes"] for item in files):
                code = "models_missing"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            code = "models_missing"
    return {"available": code == "ready", "code": code, "parser_version": PARSER_VERSION,
            "message": "本地 Docling 图块解析已就绪。" if code == "ready" else _MESSAGES[code]}


def _validate(result: dict, pages: list[int]) -> dict:
    if not isinstance(result, dict) or result.get("parser_version") != PARSER_VERSION or not isinstance(result.get("pages"), list):
        raise DoclingError("parse_failed")
    if any(not isinstance(p, dict) or type(p.get("page")) is not int for p in result["pages"]) or len(result["pages"]) != len(pages):
        raise DoclingError("parse_failed")
    if {p.get("page") for p in result["pages"]} != set(pages):
        raise DoclingError("parse_failed")
    for page in result["pages"]:
        if not all(isinstance(page.get(k), (int, float)) and math.isfinite(page[k]) and page[k] > 0
                   for k in ("width", "height")):
            raise DoclingError("parse_failed")
        if not isinstance(page.get("regions"), list) or len(page["regions"]) > 200:
            raise DoclingError("parse_failed")
        texts = page.get("text_regions", [])
        if not isinstance(texts, list) or len(texts) > 5000:
            raise DoclingError("parse_failed")
        for text in texts:
            if (not isinstance(text, dict) or not isinstance(text.get("text"), str)
                    or not isinstance(text.get("label"), str)
                    or not _valid_box(text.get("bbox"))
                    or ("ref" in text and not isinstance(text["ref"], str))):
                raise DoclingError("parse_failed")
        for region in page["regions"]:
            if not isinstance(region, dict):
                raise DoclingError("parse_failed")
            if any(key in region and not isinstance(region[key], str) for key in ("caption", "nearby_text")):
                raise DoclingError("parse_failed")
            refs = region.get("caption_refs", [])
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise DoclingError("parse_failed")
            if region.get("kind") not in {"picture", "table"} or not _valid_box(region.get("bbox")):
                raise DoclingError("parse_failed")
    return result


def _valid_box(box):
    return (isinstance(box, list) and len(box) == 4
            and all(isinstance(n, (int, float)) and not isinstance(n, bool)
                    and math.isfinite(n) and 0 <= n <= 1 for n in box)
            and box[0] < box[2] and box[1] < box[3])


def extract_regions(pdf_path: Path, pages: list[int], cache_dir: Path) -> dict:
    """Return immutable original-page regions. Coordinates are normalized top-left.

    A subprocess has a hard deadline and offline model flags. This cache contains
    original PDF layout only, never user corrections or card answers.
    """
    if not isinstance(pages, list) or not pages or len(pages) > 8 or any(type(p) is not int or p < 1 for p in pages):
        raise DoclingError("invalid_pdf")
    pages = sorted(set(pages))
    try:
        pdf_hash = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise DoclingError("invalid_pdf") from exc
    fingerprint = hashlib.sha256(f"{pdf_hash}:{PARSER_VERSION}:{pages}".encode()).hexdigest()
    cache_dir = Path(cache_dir)
    target = cache_dir / f"{fingerprint}.json"
    with _LOCK:
        if target.is_file():
            try:
                return _validate(json.loads(target.read_text(encoding="utf-8")), pages)
            except (OSError, ValueError, DoclingError):
                pass
        readiness = status()
        if not readiness["available"]:
            raise DoclingError(readiness["code"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = cache_dir / f"{fingerprint}-{uuid.uuid4().hex}.tmp.json"
        env = os.environ.copy()
        env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                    "OMP_NUM_THREADS": "4", "DO_NOT_TRACK": "1", "PYTHONUTF8": "1"})
        args = [str(_python()), str(ROOT / "tools/docling_worker.py"), "extract", "--pdf", str(Path(pdf_path).resolve()),
                "--pages", ",".join(map(str, pages)), "--output", str(temporary.resolve())]
        try:
            result = subprocess.run(args, capture_output=True, timeout=TIMEOUT_SECONDS, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                try:
                    payload = json.loads(temporary.read_text(encoding="utf-8"))
                    code = payload.get("error", "parse_failed")
                except (OSError, ValueError):
                    code = "parse_failed"
                raise DoclingError(code)
            payload = _validate(json.loads(temporary.read_text(encoding="utf-8")), pages)
            temporary.replace(target)
            return payload
        except subprocess.TimeoutExpired as exc:
            raise DoclingError("timeout") from exc
        except (OSError, ValueError) as exc:
            raise DoclingError("parse_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
