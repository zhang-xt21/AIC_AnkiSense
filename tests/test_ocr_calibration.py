"""Offline localization, conservative calibration, and source preservation."""

from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from ankisense import ocr
from ankisense.ocr_calibration import MAX_ISSUES, POLICY_VERSION, analyze_page


def png(width=200, height=100):
    output = BytesIO()
    with Image.new("RGB", (width, height), "white") as image:
        image.save(output, format="PNG")
    return output.getvalue()


def engine_result(monkeypatch, result):
    image_bytes = png()
    monkeypatch.setattr(ocr, "render_page_png", lambda content, page: image_bytes)
    monkeypatch.setattr(ocr, "_get_engine", lambda: (lambda image: result, {"engine": "offline"}))
    return image_bytes


def page(text="树表示层级结构。", **changes):
    return {"page": 2, "text": text, "raw_text": text, "method": "ocr", "status": "pending",
            "block_ids": ["lecture-p2-b1-r1"], **changes}


def line(text, score=.99, bbox=None, start=0):
    return {"text": text, "score": score, "bbox": bbox or [.1, .1, .7, .2],
            "start": start, "end": start + len(text)}


def codes(analysis):
    return {item["code"] for item in analysis["issues"]}


def test_recognition_preserves_exact_text_and_collects_json_safe_geometry(monkeypatch):
    result = SimpleNamespace(
        txts=("  树形结构。", "数字 30"), scores=(np.float32(.91), np.float32(.72)),
        boxes=np.array([[[20, 10], [120, 10], [120, 30], [20, 30]],
                        [[40, 40], [180, 40], [180, 70], [40, 70]]]))
    image_bytes = engine_result(monkeypatch, result)
    record = ocr.recognize_page(b"offline PDF", 2)
    assert record["text"] == record["raw_text"] == "  树形结构。\n数字 30"
    assert record["status"] == "pending"
    assert record["image_width"] == 200 and record["image_height"] == 100
    assert len(record["page_image_sha256"]) == 64
    assert record["ocr_lines"][0]["bbox"] == [.1, .1, .6, .3]
    assert record["ocr_lines"][1]["bbox"] == [.2, .4, .9, .7]
    assert record["ocr_lines"][0]["score"] == pytest.approx(.91)
    assert record["ocr_lines"][1]["score"] == pytest.approx(.72)
    for item in record["ocr_lines"]:
        assert record["raw_text"][item["start"]:item["end"]] == item["text"]
    assert image_bytes.startswith(b"\x89PNG")
    json.dumps(record, allow_nan=False)


def test_recognition_without_optional_geometry_does_not_lose_text(monkeypatch):
    engine_result(monkeypatch, SimpleNamespace(txts=("完整原文", "下一行")))
    record = ocr.recognize_page(b"offline PDF", 2)
    assert record["raw_text"] == "完整原文\n下一行"
    assert record["ocr_lines"] == [
        {"text": "完整原文", "bbox": None, "score": None, "start": 0, "end": 4},
        {"text": "下一行", "bbox": None, "score": None, "start": 5, "end": 8}]


def test_invalid_optional_metadata_remains_unknown_and_empty_is_not_confirmed(monkeypatch):
    engine_result(monkeypatch, SimpleNamespace(txts=("原文", "后续"), scores=(float("nan"),),
                                              boxes=[[[float("inf"), 0]] * 4]))
    record = ocr.recognize_page(b"offline PDF", 2)
    assert record["status"] == "pending"
    assert all(item["score"] is None and item["bbox"] is None for item in record["ocr_lines"])
    json.dumps(record, allow_nan=False)
    engine_result(monkeypatch, SimpleNamespace(txts=None))
    empty = ocr.recognize_page(b"offline PDF", 2)
    assert empty["status"] == "empty" and empty["ocr_lines"] == []
    assert analyze_page(empty)["status"] == "needs_confirmation"


def test_local_region_is_only_a_suggestion_with_original_page_coordinates(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "render_page_png", lambda content, page: png())

    def recognize(image_bytes):
        with Image.open(BytesIO(image_bytes)) as image:
            calls.append(image.size)
        return SimpleNamespace(txts=("局部原文",), scores=(.75,),
                               boxes=[[[10, 10], [90, 10], [90, 40], [10, 40]]])

    monkeypatch.setattr(ocr, "_get_engine", lambda: (recognize, {"engine": "offline"}))
    result = ocr.recognize_region(b"offline PDF", 2, [.25, .25, .75, .75])
    assert calls == [(100, 50)]
    assert result["suggestion_only"] is True and result["status"] == "pending"
    assert result["text"] == result["raw_text"] == "局部原文"
    assert result["ocr_lines"][0]["bbox"] == pytest.approx([.3, .35, .7, .65])
    assert result["image_width"] == 200 and result["image_height"] == 100
    assert result["region_image_sha256"] != result["page_image_sha256"]


@pytest.mark.parametrize("bbox", [None, [], [0, 0, 0, 1], [-.1, 0, 1, 1],
                                  [0, 0, float("nan"), 1], [0, 0, True, 1]])
def test_invalid_region_never_renders_or_runs_ocr(monkeypatch, bbox):
    def forbidden(*args):
        pytest.fail("invalid crop must be rejected before rendering")

    monkeypatch.setattr(ocr, "render_page_png", forbidden)
    with pytest.raises(ocr.LocalOCRError):
        ocr.recognize_region(b"offline PDF", 2, bbox)


def test_high_scores_and_text_extraction_cannot_confirm_source():
    text = "树表示层级结构。"
    for method in ("ocr", "text"):
        source = page(text, method=method, ocr_lines=[line(text, score=1)])
        original = deepcopy(source)
        result = analyze_page(source)
        assert result["policy_version"] == POLICY_VERSION
        assert result["status"] == "needs_confirmation"
        assert "source_unconfirmed" in codes(result)
        assert result["corrections"] == []
        assert source == original


def test_risks_are_localized_and_never_guessed_away():
    text = "不能超过 30 kg，x² = 2，原文\ufffd。"
    source = page(text, ocr_lines=[line(text, score=.6)])
    before = deepcopy(source)
    result = analyze_page(source)
    assert {"garbled_text", "critical_number", "critical_formula", "critical_negation",
            "critical_unit", "low_recognition_score"}.issubset(codes(result))
    located = [item for item in result["issues"] if item["code"] != "source_unconfirmed"]
    assert all(item["page"] == 2 and item["bbox"] == [.1, .1, .7, .2] for item in located)
    assert all(item["block_ids"] == ["lecture-p2-b1-r1"] for item in located)
    assert result["corrections"] == []
    assert source == before


def test_manual_confirmation_is_ready_and_risk_information_does_not_block():
    text = "不能超过 30 kg。"
    source = page(text, status="reviewed", reviewed_at="2026-10-13T00:00:00Z",
                  ocr_lines=[line(text, score=.6)])
    result = analyze_page(source)
    assert result["status"] == "ready"
    assert "critical_number" in codes(result) and "source_unconfirmed" not in codes(result)
    assert all(item["severity"] == "info" for item in result["issues"])


def test_old_ocr_boxes_and_scores_are_not_assigned_to_corrected_source():
    source = page("已人工校订的新文字。", raw_text="误识别文字。", status="reviewed",
                  ocr_lines=[line("误识别文字。", score=.2)])
    result = analyze_page(source)
    assert result["status"] == "ready"
    assert "low_recognition_score" not in codes(result)
    assert all(item["bbox"] is None for item in result["issues"])


def test_blank_page_requires_explicit_confirmation_and_failure_stays_visible():
    unconfirmed = analyze_page(page("", status="empty"))
    assert unconfirmed["status"] == "needs_confirmation"
    assert "empty_unverified" in codes(unconfirmed)
    confirmed = analyze_page(page("", status="empty", reviewed_at="confirmed"))
    assert confirmed["status"] == "ready" and confirmed["issues"] == []
    failed = analyze_page(page("", status="failed"))
    assert failed["status"] == "needs_confirmation"
    assert any(item["code"] == "recognition_failed" and item["severity"] == "error"
               for item in failed["issues"])


def test_parallel_regions_warn_about_order_without_reordering_text():
    text = "右侧栏目\n左侧栏目"
    source = page(text, ocr_lines=[line("右侧栏目", bbox=[.6, .1, .9, .2]),
                                  line("左侧栏目", bbox=[.1, .1, .4, .2], start=5)])
    original = deepcopy(source)
    result = analyze_page(source)
    assert "reading_order" in codes(result)
    assert result["status"] == "needs_confirmation"
    assert source == original


def test_display_line_ending_suggestion_does_not_change_source_or_approval():
    source = page("第一行\r\n第二行\r")
    original = deepcopy(source)
    result = analyze_page(source)
    assert result["corrections"][0]["scope"] == "display_only"
    assert result["corrections"][0]["applied"] is False
    assert result["status"] == "needs_confirmation"
    assert source == original


def test_issue_on_chunk_boundary_names_both_immutable_source_blocks():
    text = "字" * 2998 + "30kg"
    source = page(text, block_ids=["lecture-p2-b1-r7", "lecture-p2-b2-r7"])
    result = analyze_page(source)
    issue = next(item for item in result["issues"] if item["code"] == "critical_number")
    assert issue["block_ids"] == source["block_ids"]


def test_many_risk_lines_are_bounded_without_claiming_unlisted_text_is_safe():
    source = page("\n".join("数字 30 kg" for _ in range(MAX_ISSUES)))
    result = analyze_page(source)
    assert len(result["issues"]) <= MAX_ISSUES + 1
    assert result["issues"][-1]["code"] == "additional_issues"
    assert result["status"] == "needs_confirmation"
    json.dumps(result, ensure_ascii=False, allow_nan=False)
