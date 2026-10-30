"""Bounded, source-bound supplementary transcription for rejected quotations."""
import asyncio
import hashlib
import json
from difflib import SequenceMatcher
from io import BytesIO

from .ocr import recognize_region, render_page_png
from .schemas import SourceBlock
from .source_quotes import _layout, source_quote


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def valid_supplement(store, block, page):
    proof = store.get("quote_recoveries", block.block_id)
    if not proof or not proof.get("matched") or proof.get("document_id") != block.document_id:
        return False
    parent = store.get("blocks", proof.get("parent_id", ""))
    document = store.get("documents", block.document_id) or {}
    if document.get("pdf_sha256") and document["pdf_sha256"] != proof.get("pdf_sha256"):
        return False
    return bool(parent and page and proof.get("page") == page["page"] == block.page
                and proof.get("revision") == page["revision"]
                and proof["parent_id"] in page["block_ids"]
                and proof.get("parent_hash") == digest(parent["text"])
                and proof.get("text_hash") == digest(block.text))


def region_for(quote, source, lines):
    wanted, available = _layout(quote)[0], _layout(source)[0]
    match = SequenceMatcher(None, wanted, available, autojunk=False).find_longest_match()
    if match.size < 16 or match.size / max(1, len(wanted)) < .5:
        return None
    anchor = wanted[match.a:match.a + min(30, match.size)]
    boxes = [line["bbox"] for line in lines if line.get("bbox") and anchor in _layout(line.get("text", ""))[0]]
    if len(boxes) != 1:
        return None
    left, top, right, bottom = boxes[0]
    return [max(0, left-.025), max(0, top-.04), min(1, right+.025), min(1, bottom+.10)]


async def recover_points(store, pdf, blocks, rejected, ai, check, job_id):
    """Return valid candidate siblings and new immutable blocks; never approve."""
    from .draft_validation import select_knowledge_points
    recovered, remaining, supplements = [], [], {}
    mapping = {block.block_id: block for block in blocks}
    for item in rejected:
        try:
            if item.get("preview_truncated") or "连续原文" not in item.get("reason", ""):
                remaining.append(item); continue
            raw = json.loads(item.get("draft_preview", "{}"))
            if raw.get("knowledge_id") != item.get("knowledge_id"):
                remaining.append(item); continue
            for evidence in raw.get("evidence", []):
                parent = mapping.get(evidence.get("block_id"))
                quote = evidence.get("quote")
                if not parent or not isinstance(quote, str) or source_quote(quote, parent.text) in parent.text:
                    continue
                page = store.get("source_pages", f"{parent.document_id}-p{parent.page}")
                if not page or page.get("text") != page.get("raw_text"):
                    continue
                bbox = region_for(quote, parent.text, page.get("ocr_lines", []))
                if bbox is None:
                    continue
                key = "quote-fix-" + digest([job_id, parent.block_id, page["revision"], digest(parent.text), quote,
                                            hashlib.sha256(pdf).hexdigest()])[:32]
                proof = store.get("quote_recoveries", key)
                if proof is None:
                    check()
                    proof = dict(id=key, parent_id=parent.block_id, parent_hash=digest(parent.text),
                                 document_id=parent.document_id, page=parent.page, revision=page["revision"],
                                 bbox=bbox, pdf_sha256=hashlib.sha256(pdf).hexdigest(),
                                 matched=False, local_attempts=1, visual_attempts=0)
                    # Persist the attempt before work, so a restart cannot loop paid calls.
                    store.put("quote_recoveries", proof)
                    try:
                        local = await asyncio.to_thread(recognize_region, pdf, parent.page, bbox)
                    except ValueError:
                        local = {"text": ""}
                    text = local.get("text", "")
                    method = "local_ocr"
                    if source_quote(quote, text) not in text and hasattr(ai, "transcribe_source_page"):
                        check(); proof["visual_attempts"] = 1; store.put("quote_recoveries", proof)
                        png = await asyncio.to_thread(render_page_png, pdf, parent.page)
                        from PIL import Image
                        with Image.open(BytesIO(png)) as image:
                            pixels = [int(bbox[0]*image.width), int(bbox[1]*image.height),
                                      int(bbox[2]*image.width), int(bbox[3]*image.height)]
                            with image.crop(pixels) as crop:
                                output = BytesIO(); crop.save(output, format="PNG")
                        # Never send the desired quote: independently transcribe pixels.
                        proof["crop_sha256"] = hashlib.sha256(output.getvalue()).hexdigest()
                        store.put("quote_recoveries", proof)
                        transcription = await ai.transcribe_source_page(output.getvalue())
                        text = transcription.text if not transcription.uncertainties else ""
                        method = "visual"
                    check()
                    if text.strip() and source_quote(quote, text) in text:
                        block = SourceBlock(block_id=key, document_id=parent.document_id, page=parent.page, text=text)
                        proof.update(matched=True, method=method, text_hash=digest(text))
                        store.put_many([("quote_recoveries", proof), ("blocks", block.model_dump())])
                saved = store.get("blocks", key)
                if saved:
                    block = SourceBlock.model_validate(saved)
                    if valid_supplement(store, block, page) and source_quote(quote, block.text) in block.text:
                        evidence["block_id"] = key
                        supplements[key] = block
            selected = select_knowledge_points([raw], [*blocks, *supplements.values()], list(mapping))
            # Supplement references retain their parent page's focus membership.
            if not selected.points:
                selected = select_knowledge_points([raw], [*blocks, *supplements.values()],
                                                   [*mapping, *supplements])
            recovered.extend(selected.points)
            remaining.extend(selected.rejected_points)
        except (ValueError, TypeError, KeyError):
            remaining.append(item)
        except Exception as error:
            from .llm import AIError
            if isinstance(error, AIError):
                remaining.append({**item, "reason": "引用补识别未完成：" + error.message})
            else:
                # Cancellation/source changes must stop the owning task.
                raise
    return recovered, remaining, list(supplements.values())
