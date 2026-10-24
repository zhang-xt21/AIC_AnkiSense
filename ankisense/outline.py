"""Local outline candidates and explicitly lexical card-coverage hints.

Offsets always refer to unchanged source blocks. These are not an AI syllabus
or a claim of semantic coverage; weak matches remain unconfirmed.
"""
import hashlib
import re

from .source_quality import CIRCLED


HEADING = re.compile(r"^\s*(?:\d{1,2}[.．、]\s*|[（(]\d{1,2}[）)]\s*|[一二三四五六七八九十]+[、．]\s*)\S")


def compact(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text).casefold()


def outline_topics(blocks):
    topics, seen = [], set()
    for block in blocks:
        lines = list(re.finditer(r"[^\n]+", block["text"]))
        in_table = False
        for index, match in enumerate(lines):
            line = match.group().strip()
            if line in {"时间", "年份"} and any("条约" in entry.group() or "内容" in entry.group() for entry in lines[index + 1:index + 4]):
                in_table = True
            if HEADING.match(line) or line.startswith("点拨"):
                in_table = False
            kind = None
            if HEADING.match(line):
                # A parent immediately followed by subheadings is navigation,
                # not an extra knowledge item on top of those children.
                following = lines[index + 1].group().strip() if index + 1 < len(lines) else ""
                if re.match(r"^\d+[.．、]", line) and re.match(r"^[（(]\d+[）)]", following):
                    continue
                kind = "heading"
            elif re.match(f"^[{CIRCLED}]", line) and len(line) >= 12 and not in_table:
                kind = "item"
            elif in_table and re.match(r"^[^《]{0,8}《[^》]+》$", line):
                kind = "table_row"
            elif line.startswith(("关系：", "关系:", "点拨")) and len(line) > 10:
                kind = "relation"
            if not kind:
                continue
            # Multi-line heading/point titles are short previews, not rewritten
            # evidence. Original block text remains the sole citation authority.
            title = (re.split(r"[：:]", line, maxsplit=1)[0] if kind == "heading" else line)[:90]
            key = compact(title)
            if len(key) < 4 or key in seen:
                continue
            seen.add(key)
            start = match.start()
            stop = lines[index + 1].start() if index + 1 < len(lines) else len(block["text"])
            identifier = hashlib.sha256(f"{block['block_id']}:{start}:{title}".encode()).hexdigest()[:18]
            topics.append({"id": identifier, "title": title, "page": block["page"],
                           "block_id": block["block_id"], "start": start, "end": stop, "kind": kind})
    return topics


def topic_weight(topics, group):
    return sum(1 for topic in topics if any(
        topic["block_id"] == window["block"]["block_id"]
        and window["start"] <= topic["start"] < window["end"] for window in group))


def _grams(text):
    value = compact(text)
    return {value[i:i + 2] for i in range(len(value) - 1)}


def coverage_report(topics, cards, *, stale=False):
    results = []
    for topic in topics:
        title = re.sub(r"^\s*[\d（()）.．、①-⑳]+\s*", "", topic["title"])
        grams = _grams(title)
        matches = []
        for card in cards:
            # Sharing a page/quote is insufficient: inspect what the question
            # and rubric test, so a long answer cannot claim adjacent topics.
            if card.get("source_outdated") or not any(e["block_id"] == topic["block_id"] for e in card.get("evidence", [])):
                continue
            target = _grams(card.get("question", "") + card.get("concept", "") +
                            " ".join(point.get("text", "") for point in card.get("rubric", [])))
            if grams and len(grams & target) >= max(4, len(grams) * .55):
                matches.append(card["id"])
        results.append({**topic, "matched_card_ids": matches,
                        "status": "possible_match" if matches and not stale else "unmatched"})
    return {"method": "local-lexical-v1", "stale": stale, "topics": results,
            "matched_count": sum(item["status"] == "possible_match" for item in results),
            "total": len(results), "note": "按提纲标题与题目词面匹配，仅作查漏提示；不代表语义覆盖或审核通过。"}


def duplicate_question(question, previous):
    # Do not discard questions that only differ in a number, formula or a
    # negation. Semantic duplication is addressed by the generation prompt.
    def key(value):
        return re.sub(r"^(?:请问|请回答|请简述)", "", compact(value))
    return any(key(question) == key(other) for other in previous)
