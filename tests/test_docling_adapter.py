import json
from pathlib import Path
import subprocess

import pytest

from ankisense import docling_adapter as adapter


@pytest.fixture
def worker(monkeypatch, tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"fixture PDF bytes")
    monkeypatch.setattr(adapter, "status", lambda: {"available": True})
    return source, tmp_path / "cache"


def result():
    return {"parser_version": adapter.PARSER_VERSION, "pages": [
        {"page": 9, "width": 720, "height": 540, "regions": [
            {"bbox": [.1, .2, .6, .8], "kind": "picture", "caption": "", "nearby_text": ""}]}]}


def test_offline_worker_and_cache(monkeypatch, worker):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert kwargs["env"]["TRANSFORMERS_OFFLINE"] == "1"
        assert kwargs["timeout"] == 300
        Path(args[-1]).write_text(json.dumps(result()), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(adapter.subprocess, "run", run)
    first = adapter.extract_regions(worker[0], [9], worker[1])
    assert first == adapter.extract_regions(worker[0], [9], worker[1])
    assert len(calls) == 1
    assert not list(worker[1].glob("*.tmp.json"))


def test_timeout_is_safe_and_does_not_cache(monkeypatch, worker):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired("secret-local-path", 300)
    monkeypatch.setattr(adapter.subprocess, "run", run)
    with pytest.raises(adapter.DoclingError) as caught:
        adapter.extract_regions(worker[0], [9], worker[1])
    assert caught.value.code == "timeout"
    assert "secret" not in str(caught.value)
    assert not list(worker[1].glob("*.json"))


@pytest.mark.parametrize("pages", [[], [0], [True], [1, "2"], list(range(1, 10))])
def test_invalid_pages_never_spawn(monkeypatch, worker, pages):
    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **kw: pytest.fail("must not spawn"))
    with pytest.raises(adapter.DoclingError, match="PDF"):
        adapter.extract_regions(worker[0], pages, worker[1])


@pytest.mark.parametrize("broken", [[], {"parser_version": adapter.PARSER_VERSION, "pages": [None]},
    {"parser_version": adapter.PARSER_VERSION, "pages": [{"page": []}]},
    {"parser_version": adapter.PARSER_VERSION, "pages": [{"page": 9, "width": 1, "height": 1,
        "regions": [{"kind": "picture", "bbox": [0, 0, 1, 1], "caption": []}]}]},
    {"parser_version": adapter.PARSER_VERSION, "pages": [{"page": 9, "width": 1, "height": 1, "regions": [None]}]}])
def test_invalid_cache_content_is_recomputed(monkeypatch, worker, broken):
    def run(args, **kwargs):
        Path(args[-1]).write_text(json.dumps(result()), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(adapter.subprocess, "run", run)
    adapter.extract_regions(worker[0], [9], worker[1])
    next(worker[1].glob("*.json")).write_text(json.dumps(broken), encoding="utf-8")
    assert adapter.extract_regions(worker[0], [9], worker[1]) == result()


def test_worker_failure_discards_stderr(monkeypatch, worker):
    def run(args, **kwargs):
        Path(args[-1]).write_text(json.dumps({"error": "models_missing"}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 1, b"", b"sensitive-path-and-key")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    with pytest.raises(adapter.DoclingError) as caught:
        adapter.extract_regions(worker[0], [9], worker[1])
    assert caught.value.code == "models_missing"
    assert "sensitive" not in str(caught.value)


def test_positioned_text_and_caption_references_survive_validation():
    payload = result()
    payload["pages"][0]["text_regions"] = [{"bbox": [.1, .8, .6, .9], "label": "formula", "text": "s[top]", "ref": "#/texts/1"}]
    payload["pages"][0]["regions"][0]["caption_refs"] = ["#/texts/1"]
    assert adapter._validate(payload, [9]) == payload


@pytest.mark.parametrize("broken", [None, [None], [{"bbox": [0, 0, 2, 1], "label": "text", "text": "x"}],
    [{"bbox": [0, 0, 1, 1], "label": "text", "text": []}],
    [{"bbox": [0, 0, 1, 1], "label": "text", "text": "x", "ref": []}]])
def test_positioned_text_invalid_schema_rejected(broken):
    payload = result()
    payload["pages"][0]["text_regions"] = broken
    with pytest.raises(adapter.DoclingError):
        adapter._validate(payload, [9])


def test_same_fixed_model_assets_accept_older_output_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter, "ROOT", tmp_path)
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(adapter, "_python", lambda: executable)
    models = tmp_path / "data/docling-models"
    models.mkdir(parents=True)
    (models / "weights.bin").write_bytes(b"fixture")
    marker = {"parser_version": "docling-2.130.0-pdfium-heron-8f39ad3-cpu-v2", "docling": "2.130.0",
              "repo_id": "docling-project/docling-layout-heron", "revision": adapter.MODEL_REVISION,
              "files": [{"path": "weights.bin", "bytes": 7}]}
    (models / "ready.json").write_text(json.dumps(marker), encoding="utf-8")
    assert adapter.status()["available"] is True
    marker["revision"] = "different-weights"
    (models / "ready.json").write_text(json.dumps(marker), encoding="utf-8")
    assert adapter.status()["available"] is False


def test_older_parser_output_is_not_reused_as_positioned_layout():
    payload = result()
    payload["parser_version"] = "docling-2.130.0-pdfium-heron-8f39ad3-cpu-v2"
    with pytest.raises(adapter.DoclingError):
        adapter._validate(payload, [9])
