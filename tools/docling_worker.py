"""Isolated, offline CPU layout inference. Run setup once to prefetch weights."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data/docling-models"
PARSER_VERSION = "docling-2.130.0-pdfium-heron-8f39ad3-cpu-v3"
MODEL_REVISION = "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8"


def download_models():
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from docling.models.utils.hf_model_download import download_hf_model
    MODELS.mkdir(parents=True, exist_ok=True)
    spec = LayoutObjectDetectionOptions().model_spec
    # The upstream all-model downloader also fetches alternative ONNX engines.
    # Only the default local Torch layout engine is used by this worker.
    folder = MODELS / spec.repo_id.replace("/", "--")
    download_hf_model(repo_id=spec.repo_id, revision=MODEL_REVISION, local_dir=folder, progress=True)
    files = []
    for item in sorted(folder.rglob("*")):
        if item.is_file() and ".cache" not in item.parts:
            digest = hashlib.sha256()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            files.append({"path": item.relative_to(MODELS).as_posix(), "bytes": item.stat().st_size, "sha256": digest.hexdigest()})
    if not any(item["path"].endswith((".safetensors", ".bin", ".pt")) for item in files):
        raise RuntimeError("Layout weights were not downloaded")
    (MODELS / "ready.json").write_text(json.dumps({"parser_version": PARSER_VERSION,
        "docling": importlib.metadata.version("docling"), "created_at": time.time(),
        "repo_id": spec.repo_id, "revision": MODEL_REVISION, "files": files}, indent=2), encoding="utf-8")


def normalized_box(box, width, height):
    """Docling provenance can be bottom-left; export one unambiguous convention."""
    box = box.to_top_left_origin(page_height=height)
    values = [box.l / width, box.t / height, box.r / width, box.b / height]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("invalid bbox")
    return [round(max(0.0, min(1.0, v)), 7) for v in values]


def normalize_pdf(source: Path, pages: list[int], destination: Path):
    """Flatten /Rotate and translate CropBox so layout and displayed page agree."""
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import RectangleObject
    reader = PdfReader(source)
    writer = PdfWriter()
    for number in pages:
        if number < 1 or number > len(reader.pages):
            raise ValueError("page outside document")
        page = reader.pages[number - 1]
        if page.rotation:
            page.transfer_rotation_to_content()
        crop = page.cropbox
        x, y = float(crop.left), float(crop.bottom)
        width, height = float(crop.width), float(crop.height)
        if width <= 0 or height <= 0 or width * height > 20_000_000:
            raise ValueError("invalid page size")
        page.add_transformation(Transformation().translate(-x, -y))
        page.mediabox = RectangleObject([0, 0, width, height])
        page.cropbox = RectangleObject([0, 0, width, height])
        page.trimbox = RectangleObject([0, 0, width, height])
        page.bleedbox = RectangleObject([0, 0, width, height])
        page.artbox = RectangleObject([0, 0, width, height])
        writer.add_page(page)
    with destination.open("wb") as stream:
        writer.write(stream)


def extract(pdf: Path, pages: list[int], output: Path):
    # Set offline flags before importing Hugging Face / transformers.
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "OMP_NUM_THREADS": "4", "DO_NOT_TRACK": "1"})
    if not (MODELS / "ready.json").is_file():
        raise FileNotFoundError("models_missing")
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling_core.types.doc import PictureItem, TableItem, TextItem

    started = time.perf_counter()
    options = PdfPipelineOptions(artifacts_path=MODELS, do_ocr=False, do_table_structure=False,
        enable_remote_services=False, do_picture_classification=False, do_picture_description=False)
    options.layout_options.model_spec.revision = MODEL_REVISION
    options.accelerator_options = AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CPU)
    # The native docling-parse backend cannot locate its glyph resources when
    # installed in this Windows Unicode path. Docling's supported PDFium backend
    # reads the same local PDF without that native filename limitation.
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_options=options, backend=PyPdfiumDocumentBackend)})
    with tempfile.TemporaryDirectory(prefix="docling-pages-", dir=output.parent) as working:
        normalized = Path(working) / "selected.pdf"
        normalize_pdf(pdf, pages, normalized)
        conversion = converter.convert(normalized, max_num_pages=8, max_file_size=20 * 1024 * 1024)
        document = conversion.document
        parsed = {}
        text_items = {}
        for number, page in document.pages.items():
            parsed[number] = {"page": pages[number - 1], "width": page.size.width,
                              "height": page.size.height, "regions": [], "text_regions": []}
            text_items[number] = []
        for item, _level in document.iterate_items():
            if isinstance(item, TextItem):
                for provenance in item.prov:
                    if provenance.page_no in parsed and item.text.strip():
                        page = parsed[provenance.page_no]
                        bbox = normalized_box(provenance.bbox, page["width"], page["height"])
                        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                            continue
                        label = getattr(item.label, "value", str(item.label))
                        entry = {"bbox": bbox, "text": item.text[:4000], "label": label,
                                 "ref": str(item.self_ref)}
                        page["text_regions"].append(entry)
                        text_items[provenance.page_no].append((bbox, item.text))
        for item, _level in document.iterate_items():
            if not isinstance(item, (PictureItem, TableItem)):
                continue
            for provenance in item.prov:
                page = parsed.get(provenance.page_no)
                if not page:
                    continue
                bbox = normalized_box(provenance.bbox, page["width"], page["height"])
                if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                    continue
                centre_x = (bbox[0] + bbox[2]) / 2
                centre_y = (bbox[1] + bbox[3]) / 2
                nearby = sorted(text_items[provenance.page_no], key=lambda entry:
                    max(0, entry[0][0] - bbox[2], bbox[0] - entry[0][2]) ** 2
                    + max(0, entry[0][1] - bbox[3], bbox[1] - entry[0][3]) ** 2
                    + .01 * (((entry[0][0] + entry[0][2]) / 2 - centre_x) ** 2
                              + ((entry[0][1] + entry[0][3]) / 2 - centre_y) ** 2))
                caption = item.caption_text(document) if hasattr(item, "caption_text") else ""
                page["regions"].append({"bbox": bbox, "kind": "picture" if isinstance(item, PictureItem) else "table",
                    "caption": caption[:2000], "nearby_text": "\n".join(t for _, t in nearby[:4])[:4000],
                    "caption_refs": [str(reference.cref) for reference in getattr(item, "captions", [])]})
        if set(parsed) != set(range(1, len(pages) + 1)):
            raise RuntimeError("incomplete conversion")
        result = {"parser_version": PARSER_VERSION, "pages": list(parsed.values()),
                  "elapsed_seconds": round(time.perf_counter() - started, 3),
                  "configuration": "PDFium backend; CPU layout; OCR, table structure, picture description disabled"}
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["download-models", "extract"])
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--pages")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "download-models":
        download_models()
        return
    if not args.pdf or not args.pages or not args.output:
        parser.error("extract requires --pdf, --pages, --output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        pages = sorted(set(map(int, args.pages.split(","))))
        if not pages or len(pages) > 8:
            raise ValueError("invalid pages")
        extract(args.pdf, pages, args.output)
    except Exception as exc:
        code = "models_missing" if isinstance(exc, FileNotFoundError) and str(exc) == "models_missing" else "parse_failed"
        if isinstance(exc, ValueError):
            code = "invalid_pdf"
        args.output.write_text(json.dumps({"error": code}), encoding="utf-8")
        # Details only in worker stderr; adapter never sends them to the browser.
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
