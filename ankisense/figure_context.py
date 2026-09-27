"""Conservative page geometry for adding captions and task-relevant context.

This is a layout heuristic, not a claim that an image answers a question. All
coordinates describe the displayed PDF page, normalised from its top-left.
"""
from __future__ import annotations

import math
import re


def _valid(box):
    return (isinstance(box, (list, tuple)) and len(box) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and 0 <= v <= 1 for v in box)
            and box[0] < box[2] and box[1] < box[3])


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def _intersection(first, second):
    return (max(0, min(first[2], second[2]) - max(first[0], second[0]))
            * max(0, min(first[3], second[3]) - max(first[1], second[1])))


def _union(first, second):
    return [min(first[0], second[0]), min(first[1], second[1]),
            max(first[2], second[2]), max(first[3], second[3])]


def _pad(box, amount=.015):
    return [round(max(0., box[0] - amount), 7), round(max(0., box[1] - amount), 7),
            round(min(1., box[2] + amount), 7), round(min(1., box[3] + amount), 7)]


def _tokens(text):
    tokens = set(re.findall(r"[a-z_][a-z0-9_]+", text.lower()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(part[index:index + 2] for index in range(len(part) - 1))
    return tokens


def _related(text, focus):
    # Require more than generic shared words such as “示意图” / “什么”.
    common = _tokens(text) & _tokens(focus)
    common -= {"示意", "意图", "什么", "如何", "表示", "元素", "图中", "关系", "位置"}
    return len(common) >= 2 or any(re.fullmatch(r"[a-z_][a-z0-9_]+", token) for token in common)


def _formula(text):
    return bool(re.search(r"[=<>≤≥∈∑]|[a-zA-Z_]\s*\[[^\]]+\]", text)
                or (re.search(r"\d", text) and len(re.findall(r"[+*/–−]", text)) >= 2))


def _ornament(box):
    width, height = box[2] - box[0], box[3] - box[1]
    return ((width <= .30 and height <= .30 and (box[0] <= .04 or box[2] >= .96)
             and (box[1] <= .04 or box[3] >= .96))
            or ((box[3] <= .15 or box[1] >= .94) and _area(box) <= .15))


def compose_context(region: dict, page: dict, focus: str) -> dict:
    """Return a crop and explanations without modifying the parser cache.

    Adjacent labelled captions/formulas may be added within the same column.
    Sideways prose must also have a meaningful match to the specific card.
    Unrelated columns, another figure, and distant page furniture stop growth.
    Missing positioned text is explicit, so old cached layouts still work.
    """
    original = list(region["bbox"])
    merged = original[:]
    notes, flags = [], []
    texts = page.get("text_regions")
    if not isinstance(texts, list):
        return {"bbox": _pad(original), "original_bbox": original,
                "context_notes": [], "quality_flags": ["layout_context_unavailable"]}
    neighbours = [item for item in page.get("regions", [])
                  if item is not region and _valid(item.get("bbox"))
                  and list(item["bbox"]) != original and _area(item["bbox"]) >= .008
                  and not _ornament(item["bbox"])]
    captions = set(region.get("caption_refs", []))
    width, height = original[2] - original[0], original[3] - original[1]
    proposed = []
    for entry in texts:
        if not isinstance(entry, dict) or not _valid(entry.get("bbox")):
            continue
        box = entry["bbox"]
        text, label = str(entry.get("text", "")).strip(), entry.get("label", "")
        if not text or _intersection(original, box) / _area(box) >= .96:
            continue
        horizontal_gap = max(0., original[0] - box[2], box[0] - original[2])
        vertical_gap = max(0., original[1] - box[3], box[1] - original[3])
        x_overlap = max(0., min(original[2], box[2]) - max(original[0], box[0]))
        y_overlap = max(0., min(original[3], box[3]) - max(original[1], box[1]))
        x_alignment = x_overlap / min(width, box[2] - box[0])
        y_alignment = y_overlap / min(height, box[3] - box[1])
        relevant = _related(text, focus)
        formula = label in {"formula", "code"} or _formula(text)
        caption = (entry.get("ref") in captions or label == "caption"
                   or (len(text) <= 80 and bool(re.match(r"^[(（][a-zA-Z0-9]+[)）]|^图\s*\d", text))))
        # A footer may contain a real formula. Position alone must not erase
        # such evidence, but unassociated headers/footers are never expanded.
        if label in {"page_header", "page_footer", "section_header", "title"} and not (relevant and formula):
            continue
        # A short caption can sit between the diagram and its formula; allow
        # a slightly wider gap only for a formula tied to the current card.
        maximum_gap = .12 if formula and relevant else .075
        same_column = x_alignment >= .55 and vertical_gap <= maximum_gap and box[2] - box[0] <= width * 1.65 + .03
        same_row = y_alignment >= .55 and horizontal_gap <= .035 and box[3] - box[1] <= max(.25, height * .9)
        accepted = ((same_column and (caption or formula or relevant))
                    or (same_row and relevant))
        if not accepted:
            continue
        expanded = _union(original, box)
        # Do not turn a local candidate into an entire slide. Tables may need
        # a nearby explanatory column, but a small icon must stay small.
        if (_area(expanded) > min(.78, _area(original) * 2.2 + .025)
                or len(text) > 450):
            flags.append("context_unresolved")
            continue
        # Ignore tiny decorations that overlap the body, but do not absorb a
        # neighbouring independent diagram to reach a text annotation.
        collision = any(_intersection(expanded, other["bbox"]) - _intersection(original, other["bbox"])
                        > max(.008, _area(other["bbox"]) * .20)
                        for other in neighbours)
        if collision:
            flags.append("context_unresolved")
            continue
        kind = "公式或标注" if formula else ("图注" if caption else "与题目相关的邻文")
        proposed.append((horizontal_gap + vertical_gap, expanded, text, kind))
    for _distance, expanded, text, kind in sorted(proposed, key=lambda entry: entry[0])[:6]:
        union = _union(merged, expanded)
        if (_area(union) > min(.78, _area(original) * 2.2 + .025)
                or any(_intersection(union, other["bbox"]) - _intersection(original, other["bbox"])
                       > max(.008, _area(other["bbox"]) * .20) for other in neighbours)):
            flags.append("context_unresolved")
            continue
        merged = union
        notes.append(f"已保留相邻{kind}：{text[:100]}")
        if "\n" in text and kind == "公式或标注":
            flags.append("mixed_context")
    # A rectangular expansion may intersect nearby line fragments (e.g. an
    # explanation split into several text cells). Complete short lines when
    # that needs only a modest additional margin, without absorbing figures.
    if proposed and merged != original:
        for entry in texts:
            box = entry.get("bbox") if isinstance(entry, dict) else None
            if not _valid(box) or len(str(entry.get("text", ""))) > 100:
                continue
            fraction = _intersection(merged, box) / _area(box)
            if not .03 < fraction < .96 or box[1] < merged[1] or box[3] > merged[3]:
                continue
            expanded = _union(merged, box)
            if (_area(expanded) > min(.78, _area(original) * 2.2 + .025)
                    or expanded[2] - expanded[0] - (merged[2] - merged[0]) > .15
                    or any(_intersection(expanded, other["bbox"]) - _intersection(merged, other["bbox"])
                           > max(.008, _area(other["bbox"]) * .20) for other in neighbours)):
                continue
            merged = expanded
            if "已补齐扩图范围边缘的短行文字；请核对新增上下文。" not in notes:
                notes.append("已补齐扩图范围边缘的短行文字；请核对新增上下文。")
    crop = _pad(merged)
    # Padding itself can run through an unrelated line. Report it without
    # sweeping the whole paragraph into a diagram or silently dropping it.
    for entry in texts:
        box = entry.get("bbox") if isinstance(entry, dict) else None
        if not _valid(box):
            continue
        fraction = _intersection(crop, box) / _area(box)
        if .03 < fraction < .96:
            flags.append("text_at_crop_edge")
            break
    if any(.01 < _intersection(crop, other["bbox"]) / _area(other["bbox"]) < .96
           for other in neighbours):
        flags.append("figure_at_crop_edge")
    if not notes:
        notes.append("未自动扩入邻文；请结合原页核对图中标注。")
    return {"bbox": crop, "original_bbox": original, "context_notes": notes,
            "quality_flags": list(dict.fromkeys(flags))}
