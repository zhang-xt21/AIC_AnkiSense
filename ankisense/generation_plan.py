"""Local, deterministic planning for a document-wide card generation job.

The estimate is a workload suggestion, not a knowledge-point counter. Source
characters are never rewritten: even a long-block window keeps the original
block ID and a contiguous substring that can be checked against the full block.
"""
import hashlib
import json
import math
import re
from collections import Counter

from fastapi import HTTPException
from .outline import outline_topics, topic_weight
from .source_quality import assess_page


MAX_CARDS = 30
MAX_BATCH_CARDS = 6
MAX_BATCH_BLOCKS = 6
MAX_BATCH_CHARS = 16000
MAX_SOURCE_CHARS = 100000
PLAN_POLICY = "document-generation-v2-outline"


def _natural_key(value):
    return tuple((1, int(part)) if part.isdigit() else (0, part)
                 for part in re.split(r"(\d+)", value))


def _scope(store, document_id, block_ids):
    document = store.get("documents", document_id)
    if not document:
        raise HTTPException(404, "讲义不存在，请重新选择。")
    pages = {item["page"]: item for item in store.all("source_pages")
             if item.get("document_id") == document_id}
    all_blocks = store.all("blocks")
    by_id = {item["block_id"]: item for item in all_blocks}
    selected = block_ids is not None
    if selected:
        if not block_ids or len(block_ids) != len(set(block_ids)):
            raise HTTPException(422, "请至少选择一个原文片段，且不要重复选择。")
        missing = [identifier for identifier in block_ids if identifier not in by_id]
        if missing:
            raise HTTPException(404, "所选原文片段不存在，请刷新后重新选择。")
        candidates = [by_id[identifier] for identifier in block_ids]
        if any(item.get("document_id") != document_id for item in candidates):
            raise HTTPException(422, "一次制卡只能使用当前讲义的原文片段。")
    else:
        candidates = [item for item in all_blocks if item.get("document_id") == document_id]

    usable = []
    for item in candidates:
        record = pages.get(item["page"])
        reason = None
        if not item.get("text", "").strip():
            reason = "原文为空"
        elif record and item["block_id"] not in record.get("block_ids", []):
            reason = "原文已更新，所选片段已过期"
        elif record and (record.get("status") not in {"pending", "reviewed"}
                         or not record.get("text", "").strip()):
            reason = "本页没有成功提取的文字"
        if reason:
            if selected:
                raise HTTPException(409, f"第 {item['page']} 页{reason}，请重新选择当前有效原文。")
            continue
        usable.append({key: item[key] for key in ("block_id", "document_id", "page", "text")})

    def order(block):
        ids = pages.get(block["page"], {}).get("block_ids", [])
        offset = ids.index(block["block_id"]) if block["block_id"] in ids else len(ids)
        return block["page"], offset, _natural_key(block["block_id"])

    usable.sort(key=order)
    valid_pages = {item["page"] for item in usable}
    skipped = []
    if not selected:
        for number in sorted(set(pages) | {item["page"] for item in candidates}):
            if number not in valid_pages:
                page = pages.get(number, {})
                reason = ("文字提取失败" if page.get("status") == "failed"
                          else "没有当前可用的原文片段" if page.get("text", "").strip()
                          else "没有可用文字")
                skipped.append({"page": number, "reason": reason})
    if not usable:
        raise HTTPException(409, "当前范围没有可用于制卡的文字，请先导入原文或重新识别失败页面。")
    if sum(len(item["text"]) for item in usable) > MAX_SOURCE_CHARS:
        raise HTTPException(422, "所选原文超过 10 万字，请缩小制卡范围。")

    identity = {"policy": PLAN_POLICY, "document_id": document_id,
                "pdf_sha256": document.get("pdf_sha256"), "scope": "selected" if selected else "all",
                "blocks": usable,
                "revisions": [[number, pages.get(number, {}).get("revision")]
                              for number in sorted(valid_pages)], "skipped_pages": skipped}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
    return usable, skipped, fingerprint


def _cut(text, limit):
    """Prefer a paragraph, line or sentence end near the requested boundary."""
    lower = max(1, limit // 2)
    for marker in ("\n\n", "\n", "。", "！", "？", ". ", "; ", "；"):
        position = text.rfind(marker, lower, limit)
        if position >= 0:
            return position + len(marker)
    return limit


def _windows(block):
    text, start = block["text"], 0
    while start < len(text):
        end = len(text) if len(text) - start <= MAX_BATCH_CHARS else start + _cut(text[start:], MAX_BATCH_CHARS)
        if text[start:end].strip():
            yield {"block": {**block, "text": text[start:end]}, "start": start, "end": end}
        start = end


def _pack(windows):
    groups, current = [], []
    for window in windows:
        block = window["block"]
        if current and (len(current) == MAX_BATCH_BLOCKS
                        or sum(len(item["block"]["text"]) for item in current) + len(block["text"]) > MAX_BATCH_CHARS
                        or any(item["block"]["block_id"] == block["block_id"] for item in current)):
            groups.append(current)
            current = []
        current.append(window)
    if current:
        groups.append(current)
    return groups


def _estimate_chars(blocks):
    # Repeated adjacent page-edge lines affect only the estimate. Keeping every
    # original character in model context avoids accidentally deleting a title
    # or a short sentence that is itself the material the user wants to study.
    texts = {}
    for block in blocks:
        texts.setdefault(block["page"], []).append(block["text"])
    edges, total = [], sum(len(re.sub(r"\s", "", block["text"])) for block in blocks)
    for page in sorted(texts):
        lines = [line.strip() for line in "\n".join(texts[page]).splitlines() if line.strip()]
        edges.append(set(line for line in (lines[:1] + lines[-1:]) if 2 <= len(line) <= 80))
    repeats = Counter(line for edge in edges for line in edge)
    discount = sum((amount - 1) * len(re.sub(r"\s", "", line)) for line, amount in repeats.items()
                   if amount >= 3 and any(line in edges[index] & edges[index + 1]
                                         for index in range(len(edges) - 1)))
    return max(1, total - discount)


def _enough_groups(groups, target):
    """Split where useful; a large explicit target may reuse small contexts."""
    groups = [list(group) for group in groups]
    required = math.ceil(target / MAX_BATCH_CARDS)
    reused = False
    while len(groups) < required:
        choices = [(sum(len(window["block"]["text"]) for window in group), index)
                   for index, group in enumerate(groups)
                   if len(group) > 1 or len(group[0]["block"]["text"]) >= 400]
        if not choices:
            original = list(groups)
            groups.extend(original[index % len(original)] for index in range(required - len(groups)))
            return groups, True
        _, index = max(choices)
        group = groups[index]
        if len(group) > 1:
            middle = len(group) // 2
            replacement = [group[:middle], group[middle:]]
        else:
            window, text = group[0], group[0]["block"]["text"]
            middle = _cut(text, len(text) // 2)
            if not text[:middle].strip() or not text[middle:].strip():
                groups.append(group)
                reused = True
                continue
            left = {**window, "block": {**window["block"], "text": text[:middle]},
                    "end": window["start"] + middle}
            right = {**window, "block": {**window["block"], "text": text[middle:]},
                     "start": window["start"] + middle}
            replacement = [[left], [right]]
        groups[index:index + 1] = replacement
    return groups, reused


def _coverage(blocks, selected):
    ranges = {}
    for group in selected:
        for window in group:
            ranges.setdefault(window["block"]["block_id"], []).append((window["start"], window["end"]))
    lengths = {}
    for identifier, intervals in ranges.items():
        end, length = 0, 0
        for start, stop in sorted(intervals):
            length += max(0, stop - max(start, end))
            end = max(end, stop)
        lengths[identifier] = length
    omitted = [block["block_id"] for block in blocks if block["block_id"] not in ranges]
    partial = sorted({block["page"] for block in blocks
                      if 0 < lengths.get(block["block_id"], 0) < len(block["text"])
                      or (block["block_id"] in omitted and any(other["page"] == block["page"]
                          and other["block_id"] in ranges for other in blocks))})
    covered = sum(lengths.values())
    return covered, omitted, partial


def build_plan(store, document_id, block_ids=None, count=None):
    """Build a source-bound plan without saving data or calling a model."""
    if count is not None and (type(count) is not int or not 1 <= count <= MAX_CARDS):
        raise HTTPException(422, "制卡目标数量须为 1 到 30 的整数。")
    blocks, skipped, fingerprint = _scope(store, document_id, block_ids)
    windows = [window for block in blocks for window in _windows(block)]
    base = _pack(windows)
    estimated = _estimate_chars(blocks)
    topics = outline_topics(blocks)
    # Numbered subpoints are often parts of one answer, not standalone cards.
    structure_count = sum({"item": .25, "relation": .5}.get(topic["kind"], 1) for topic in topics)
    recommended = min(MAX_CARDS, max(len(base), math.ceil(estimated / 700), math.ceil(structure_count)))
    target = recommended if count is None else count
    groups, reused = _enough_groups(base, target)
    if target < len(groups):
        # A single card samples the middle; multiple cards span both ends.
        indices = ([len(groups) // 2] if target == 1 else
                   [round(index * (len(groups) - 1) / (target - 1)) for index in range(target)])
        groups = [groups[index] for index in indices]
    counts = [1] * len(groups)
    for _ in range(target - len(groups)):
        index = max((index for index in range(len(groups)) if counts[index] < MAX_BATCH_CARDS),
                    key=lambda index: (topic_weight(topics, groups[index]) * 350 +
                                       sum(len(window["block"]["text"]) for window in groups[index])) / (counts[index] + 1))
        counts[index] += 1
    source_pages = sorted({block["page"] for block in blocks})
    selected_pages = sorted({window["block"]["page"] for group in groups for window in group})
    covered, omitted_blocks, partial_pages = _coverage(blocks, groups)
    total_chars = sum(len(block["text"]) for block in blocks)
    partial_scope = covered < total_chars
    warnings = []
    page_numbers = set(source_pages)
    source_signals = [{"page": page["page"], **assess_page(page)} for page in store.all("source_pages")
                      if page.get("document_id") == document_id and page["page"] in page_numbers]
    suspicious = [item["page"] for item in source_signals if item["needs_attention"]]
    if suspicious:
        warnings.append("原文筛查提示第 " + "、".join(map(str, suspicious)) + " 页存在提取疑点，可仅对这些页面进行 AI 校对；未校对时仍可生成草稿，不要将残缺片段作为完整答案。")
    if skipped:
        warnings.append("已跳过没有有效文字的页面：" + "、".join(str(item["page"]) for item in skipped) + "。")
    if partial_scope:
        warnings.append(f"本次目标较少，将均匀选取部分原文，约覆盖 {covered / total_chars:.0%} 的有效文字；未包含的内容不会在本次出题。可增加数量或分次制卡。")
    if reused:
        warnings.append("目标数量相对材料较多，部分批次会使用相同原文；重复题或依据不足的题目不会保留，实际数量可能少于目标。")
    batches = [{"index": index + 1, "count": counts[index],
                "pages": sorted({window["block"]["page"] for window in group}),
                "chars": sum(len(window["block"]["text"]) for window in group),
                "blocks": [dict(window["block"]) for window in group]}
               for index, group in enumerate(groups)]
    return {
        "document_id": document_id, "scope": "selected" if block_ids is not None else "all",
        "source_fingerprint": fingerprint, "recommended_count": recommended, "target_count": target,
        "min_count": 1, "max_count": MAX_CARDS, "total_batches": len(batches),
        "suggestion_reason": f"按 {len(source_pages)} 页、约 {estimated} 个非空白字符、{len(topics)} 个提纲候选及分批覆盖需要估计。编号子项合并计量，这是制卡数量估计，不代表知识点总数；实际保留数量可能更少。",
        "stats": {"source_block_count": len(blocks), "source_page_count": len(source_pages),
                  "source_chars": total_chars, "batch_count": len(batches),
                  "selected_page_count": len(selected_pages), "coverage_ratio": round(covered / total_chars, 4)},
        "source_pages": source_pages, "selected_pages": selected_pages, "topics": topics,
        "source_signals": source_signals,
        "omitted_pages": sorted(set(source_pages) - set(selected_pages)),
        "omitted_blocks": omitted_blocks, "partial_pages": partial_pages, "partial_scope": partial_scope,
        "skipped_pages": skipped, "warnings": warnings, "batches": batches,
    }


def public_plan(plan):
    """Keep the UI response small and avoid repeating source text over the API."""
    return {**plan, "batches": [{key: value for key, value in batch.items() if key != "blocks"}
                               | {"block_ids": [block["block_id"] for block in batch["blocks"]]}
                               for batch in plan["batches"]]}
