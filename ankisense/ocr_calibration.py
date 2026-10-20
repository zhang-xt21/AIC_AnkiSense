"""Local OCR screening and display suggestions, never source correction.

An engine score is a screening signal, not proof of correctness. This first
policy only declares a page ready after explicit human source confirmation.
All offsets and boxes describe the recorded recognition, not a new source.
"""

import math
import re
from .source_policy import source_usable


POLICY_VERSION = "ocr-screening-v1"
LOW_SCORE_THRESHOLD = .85
MAX_ISSUES = 100

_GARBLED = re.compile(r"\ufffd|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NUMBERS = re.compile(r"[0-9０-９]")
_FORMULAS = re.compile(r"[=<>≤≥≠±×÷∑√∫∞π⁰¹²³⁴⁵⁶⁷⁸⁹₀-₉α-ωΑ-Ω]|\^|公式|方程")
_NEGATION = re.compile(r"不|无|非|未|禁止|\b(?:not|no|never|without|except)\b", re.I)
_UNITS = re.compile(
    r"%|％|℃|°[CF]|百分|千克|毫克|毫米|厘米|千米|毫秒|毫升|摄氏|"
    r"[0-9]\s*(?:kg|mg|g|km|cm|mm|m|ms|s|Hz|MHz|GHz|V|mV|A|mA|"
    r"Pa|MPa|J|kJ|W|kW|mol|mL|L|K|MB|GB)(?![A-Za-z])", re.I)


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [float(item) for item in value]
    except (ValueError, TypeError, OverflowError):
        return None
    if (not all(math.isfinite(item) and 0 <= item <= 1 for item in result)
            or result[0] >= result[2] or result[1] >= result[3]):
        return None
    return result


def _score(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        score = float(value)
        return score if math.isfinite(score) and 0 <= score <= 1 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _current_lines(record, text):
    """Use geometry only when its raw offsets still match current source text."""
    recorded = {}
    if record.get("raw_text", text) == text:
        for line in record.get("ocr_lines", []) or []:
            if not isinstance(line, dict):
                continue
            start, end = line.get("start"), line.get("end")
            if (isinstance(start, int) and not isinstance(start, bool)
                    and isinstance(end, int) and not isinstance(end, bool)
                    and 0 <= start <= end <= len(text)
                    and text[start:end] == line.get("text")):
                recorded[(start, end)] = line
    lines, offset = [], 0
    for segment in text.splitlines(keepends=True):
        value = segment.rstrip("\r\n")
        end = offset + len(value)
        metadata = recorded.get((offset, end), {})
        lines.append({"text": value, "start": offset, "end": end,
                      "bbox": _box(metadata.get("bbox")), "score": _score(metadata.get("score"))})
        offset += len(segment)
    return lines


def _line_blocks(record, text, start, end):
    identifiers = list(record.get("block_ids", []) or [])
    if not identifiers:
        return []
    # text_blocks strips each 3000-character chunk and omits empty chunks.
    chunks = [(offset, offset + len(text[offset:offset + 3000]))
              for offset in range(0, len(text), 3000) if text[offset:offset + 3000].strip()]
    if len(chunks) != len(identifiers):
        return identifiers  # Legacy layouts cannot be localized reliably.
    return [identifier for identifier, (left, right) in zip(identifiers, chunks)
            if start < right and end > left]


def analyze_page(page_record: dict) -> dict:
    """Return bounded, explainable issues without mutating the page or text."""
    text = str(page_record.get("text") or "")
    confirmed = source_usable(page_record) and bool(text.strip())
    confirmed_empty = (page_record.get("status") == "empty" and not text.strip()
                       and bool(page_record.get("reviewed_at")))
    ready = confirmed or confirmed_empty
    issues, corrections = [], []
    lines = _current_lines(page_record, text)
    all_blocks = list(page_record.get("block_ids", []) or [])
    omitted = 0

    def add(code, reason, suggestion, line=None, severity="warning", bbox=None):
        nonlocal omitted
        if len(issues) >= MAX_ISSUES:
            omitted += 1
            return
        issues.append({"code": code, "severity": "info" if ready else severity,
                       "reason": reason, "page": page_record.get("page"),
                       "block_ids": _line_blocks(page_record, text, line["start"], line["end"])
                       if line else list(all_blocks),
                       "bbox": line["bbox"] if line else bbox, "suggestion": suggestion})

    if not ready:
        add("source_unconfirmed", "该页原文尚未由用户确认；识别分数或文字层提取不能证明内容正确。",
            "对照原页确认文字；修正内容后再保存并确认。")
        for reason in page_record.get("calibration_issues", [])[:32]:
            add("calibration_issue", reason, "查看原页校准差异，修订或确认后保存。")
    if page_record.get("status") == "failed":
        add("recognition_failed", "该页识别失败，没有足够文字依据继续制卡。",
            "重新 OCR，或对照原页手工录入并确认。", severity="error")
    if not text.strip():
        if not confirmed_empty:
            add("empty_unverified", "没有识别到文字，不能据此断定原页为空白。",
                "查看原页；有文字则重新识别，无文字则明确确认空白页。")
    elif page_record.get("method") == "ocr" and any(
            line["text"].strip() and (line["score"] is None or line["bbox"] is None) for line in lines):
        add("recognition_metadata_missing", "部分文字缺少可用的识别分数或原图位置，无法可靠定位所有疑点。",
            "对照完整原页核查；较旧的识别结果可重新 OCR 获取定位信息。", severity="info")

    risks = (
        (_GARBLED, "garbled_text", "包含替代字符或不可显示控制字符，可能存在识别或编码损坏。",
         "对照原图核对该处文字；不要凭常识补写。"),
        (_NUMBERS, "critical_number", "含数字，需核对位数、小数点、正负号和相邻条件。",
         "逐字核对原图中的数值，确认没有漏字或符号。"),
        (_FORMULAS, "critical_formula", "含公式或关系符号，文字 OCR 无法证明其结构准确。",
         "对照原图核对公式、上下标和符号；必要时手工录入。"),
        (_NEGATION, "critical_negation", "含否定或限制词，漏识别可能改变结论。",
         "核对原图中的否定词及其作用范围。"),
        (_UNITS, "critical_unit", "含单位或百分比，需核对大小写、量级和对应数值。",
         "对照原图确认单位、前缀及符号，保留其原始含义。"),
    )
    for line in lines:
        if line["score"] is not None and line["score"] < LOW_SCORE_THRESHOLD:
            add("low_recognition_score", "该行识别分数较低；此分数只用于定位疑点，不代表正确概率。",
                "对照此处原图，必要时局部重新识别；新的识别结果仍需核对。", line)
        for pattern, code, reason, suggestion in risks:
            if pattern.search(line["text"]):
                add(code, reason, suggestion, line)

    positioned = [line for line in lines if line["bbox"] and line["text"].strip()]
    # Side-by-side lines with vertical overlap are a layout signal. They may
    # also be a table: this warns about reading order without claiming columns.
    for index, left in enumerate(positioned):
        for right in positioned[index + 1:index + 33]:
            a, b = left["bbox"], right["bbox"]
            overlap = min(a[3], b[3]) - max(a[1], b[1])
            horizontal_gap = max(b[0] - a[2], a[0] - b[2])
            if overlap > .5 * min(a[3] - a[1], b[3] - b[1]) and horizontal_gap >= .04:
                add("reading_order", "原图存在并排文字区域，可能为多栏或表格；识别器行顺序未验证。",
                    "对照原图检查各栏或表格的阅读顺序，勿自动按位置拼接内容。", left)
                break
        else:
            continue
        break

    if "\r" in text:
        corrections.append({"code": "display_line_endings", "page": page_record.get("page"),
                            "reason": "界面展示可统一换行形式；保留所有原文字符和来源版本。",
                            "scope": "display_only", "applied": False,
                            "suggestion": "仅在显示层将 CRLF/CR 显示为换行，不写回来源文字。"})
    if omitted:
        issues.append({"code": "additional_issues", "severity": "info" if ready else "warning",
                       "reason": "疑点较多，已限制展示数量；其余区域仍需对照完整原页核查。",
                       "page": page_record.get("page"), "block_ids": all_blocks,
                       "bbox": None, "suggestion": "逐页核对原文，不将未展示区域视为已通过。"})
    return {"policy_version": POLICY_VERSION, "status": "ready" if ready else "needs_confirmation",
            "issues": issues, "corrections": corrections}
