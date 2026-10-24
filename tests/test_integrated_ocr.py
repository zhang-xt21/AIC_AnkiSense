"""Shared OCR metadata and installed-client storage regression checks."""
from io import BytesIO
from types import SimpleNamespace
import sys

from PIL import Image

from ankisense import ocr


def test_ocr_keeps_calibration_geometry_and_quality_metadata(monkeypatch):
    output = BytesIO()
    Image.new("RGB", (100, 50), "white").save(output, format="PNG")
    recognition = SimpleNamespace(txts=["first line", "第二行"], scores=[0.72, 0.96],
                                  boxes=[[[10, 5], [30, 5], [30, 25], [10, 25]],
                                         [[0, 25], [100, 25], [100, 50], [0, 50]]])
    monkeypatch.setattr(ocr, "_get_engine", lambda: (lambda content: recognition, {"engine": "offline"}))
    result = ocr._recognize_image(output.getvalue())
    first, second = result["ocr_lines"]
    assert result["text"] == "first line\n第二行"
    assert first["bbox"] == [0.1, 0.1, 0.3, 0.5]
    assert first["score"] == first["confidence"] == 0.72
    assert first["bbox_pixels"] == recognition.boxes[0]
    assert result["text"][first["start"]:first["end"]] == first["text"]
    assert result["text"][second["start"]:second["end"]] == second["text"]


def test_default_ocr_model_cache_uses_configured_user_data(monkeypatch, tmp_path):
    """A frozen installation must not copy models into its program directory."""
    package = tmp_path / "frozen" / "rapidocr"
    models = package / "models"
    models.mkdir(parents=True)
    for filename in ocr.MODEL_FILES.values():
        (models / filename).write_bytes(b"offline model fixture")
    params = {}
    def engine(**kwargs):
        params.update(kwargs["params"])
        return object()
    fake = SimpleNamespace(__file__=str(package / "__init__.py"), RapidOCR=engine,
                           EngineType=SimpleNamespace(ONNXRUNTIME="onnx"),
                           ModelType=SimpleNamespace(SMALL="small", MOBILE="mobile"),
                           OCRVersion=SimpleNamespace(PPOCRV6="v6", PPOCRV4="v4"))
    monkeypatch.setitem(sys.modules, "rapidocr", fake)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace())
    monkeypatch.setattr(ocr, "_ENGINE", None)
    monkeypatch.setattr(ocr, "_ENGINE_INFO", None)
    profile = tmp_path / "profile" / "data"
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(profile))
    ocr._get_engine()
    assert params["Global.model_root_dir"] == str(profile / "ocr-models")
    assert all((profile / "ocr-models" / name).is_file() for name in ocr.MODEL_FILES.values())
    assert not (tmp_path / "frozen" / "data").exists()
