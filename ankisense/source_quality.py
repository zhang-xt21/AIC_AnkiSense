"""Cheap extraction warnings, never a claim that a page is correct or complete."""
import re


QUALITY_POLICY = "source-signals-v1"
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def assess_page(page):
    text = page.get("text", "")
    issues = []

    def add(code, message):
        if not any(item["code"] == code for item in issues):
            issues.append({"code": code, "message": message})

    if page.get("status") == "failed" or not text.strip():
        add("no_text", "本页没有成功提取的文字，请查看原图或重新识别。")
    if "\ufffd" in text or re.search(r"[□�]{2,}", text):
        add("unreadable", "存在乱码或缺字占位符。")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Reset a list at ①; only increasing gaps are suspicious. Lists may restart
    # in different table rows, and a page may legitimately start in mid-list.
    previous = None
    for marker in re.findall(f"[{CIRCLED}]", text):
        number = CIRCLED.index(marker) + 1
        if previous is not None and number > previous + 1:
            add("numbering_gap", f"分项编号从 {CIRCLED[previous - 1]} 跳到 {marker}，可能漏项；也可能是排版顺序导致。")
        previous = number
    table = ("|" in text and len(re.findall(r"\|.*\|", text)) >= 3) or (
        len(re.findall(r"\b(?:18|19|20)\d{2}年", text)) >= 3
        and ("条约" in text or "时间" in text))
    if table:
        add("table_order", "检测到表格或多行年份条目，文字顺序与行列对应需要检查。")
    if page.get("method") in {"ocr", "text", None}:
        # A lone ① below a numbered heading is a weak omission signal. Do not
        # invent the missing item: it could continue on the next page.
        sections = re.split(r"(?m)^\s*(?:[（(]\d+[）)]|\d+[.．、])|\n(?=[^\n]{4,40}(?:表|一览|汇总|条约)\n)", text)
        if any(section.count("①") == 1 and not any(c in section for c in CIRCLED[1:])
               and len(section) > 80 for section in sections):
            add("possibly_incomplete_list", "某个分节只识别到①，可能存在漏行或跨页续接，请检查该分节。")
        if lines and len(lines[-1]) > 15 and not re.search(r"[。！？；.!?;：:）)》]$", lines[-1]):
            add("page_continuation", "页尾句子未结束，出题时不要把跨页片段当成完整结论。")
    current_lines = page.get("ocr_lines", []) if page.get("raw_text", text) == text else []
    scores = [line.get("confidence") for line in current_lines
              if isinstance(line.get("confidence"), (int, float))]
    low = sum(score < .8 for score in scores)
    if low:
        add("low_confidence", f"有 {low} 行 OCR 置信度低于 0.80，建议优先核对。置信度不是正确率。")
    if re.search(r"(?:\\frac|\\sum|[∑∫√]|[=≤≥].{0,15}[_^])", text):
        add("formula", "检测到公式，符号或上下标可能在文字提取中丢失。")
    return {"policy": QUALITY_POLICY, "needs_attention": bool(issues), "issues": issues,
            "ocr_line_count": len(current_lines), "low_confidence_lines": low,
            "note": "本机规则筛查；未发现信号不代表原文完整或正确。"}


def with_quality(page):
    return {**page, "quality": assess_page(page)}
