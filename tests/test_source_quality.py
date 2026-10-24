"""Extraction triage preserves originals and does not certify completeness."""
from types import SimpleNamespace
from io import BytesIO

from PIL import Image

from ankisense.source_quality import assess_page


def page_png():
    output = BytesIO()
    Image.new("RGB", (30, 25), "white").save(output, format="PNG")
    return output.getvalue()


def test_numbering_gap_is_detected_without_confusing_list_restarts():
    assert "numbering_gap" in {i["code"] for i in assess_page({"text": "①甲\n③丙"})["issues"]}
    assert not assess_page({"text": "①甲②乙\n①新的甲②新的乙"})["needs_attention"]


def test_handbook_style_missing_list_item_and_table_are_suspect():
    text = "(2）半封建社会的原因\n①资本主义生产关系已经出现，已不是完全的封建社会。\n" + "主" * 65 + "要形式。\n近代以来签订的部分不平等条约\n时间\n条约\n1842年\n1843年\n1901年\n"
    codes = {i["code"] for i in assess_page({"method": "ocr", "text": text})["issues"]}
    assert {"possibly_incomplete_list", "table_order"} <= codes


def test_scores_are_warnings_not_a_correctness_certificate_or_current_after_edit():
    page = {"text": "原始内容。", "raw_text": "原始内容。", "ocr_lines": [{"confidence": .99}, {"confidence": .6}]}
    assert assess_page(page)["low_confidence_lines"] == 1
    page["text"] = "修订后的内容。"
    assert assess_page(page)["ocr_line_count"] == 0
    assert "不代表" in assess_page({"text": "普通完整定义。"})["note"]


def test_ocr_retains_line_scores_and_positions_without_rewriting_text(monkeypatch):
    from ankisense import ocr
    recognition = SimpleNamespace(txts=["第一行", "第二行"], scores=[.98, .62],
                                  boxes=[[[0, 1], [20, 1], [20, 8], [0, 8]], [[0, 12], [25, 12], [25, 20], [0, 20]]])
    monkeypatch.setattr(ocr, "render_page_png", lambda *_: page_png())
    monkeypatch.setattr(ocr, "_get_engine", lambda: (lambda _: recognition, {}))
    result = ocr.recognize_page(b"pdf", 1)
    assert result["raw_text"] == result["text"] == "第一行\n第二行"
    assert result["ocr_lines"][1]["confidence"] == .62
    assert result["ocr_lines"][1]["bbox_pixels"] == [[0, 12], [25, 12], [25, 20], [0, 20]]


def test_old_engine_without_scores_or_boxes_still_returns_text(monkeypatch):
    from ankisense import ocr
    monkeypatch.setattr(ocr, "render_page_png", lambda *_: page_png())
    monkeypatch.setattr(ocr, "_get_engine", lambda: (lambda _: SimpleNamespace(txts=["原文"]), {}))
    result = ocr.recognize_page(b"pdf", 1)
    assert result["text"] == "原文"
    assert result["ocr_lines"] == [{"text": "原文", "bbox": None, "score": None, "start": 0, "end": 2}]
