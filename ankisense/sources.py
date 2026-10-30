"""Immutable source revisions, with separate confirmations of unchanged text."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

from .ingest import text_blocks
from .source_policy import source_usable, machine_verified
from .automatic_policy import automatic_evidence_valid
from .source_quality import assess_page
from .quote_recovery import valid_supplement


def page_id(document_id, page):
    return f"{document_id}-p{page}"


def source_available(page):
    """Usable extraction, without claiming that a human confirmed the page."""
    return bool(page and (page.get("status") in {"pending", "reviewed"} or machine_verified(page)))


def invalidate_approval(card):
    """Remove both proof formats when content, evidence or pictures change."""
    changed = {**card, "review_state": "needs_review"}
    for key in ("approval", "approval_method", "automatic_evidence", "review_task_fingerprint"):
        changed.pop(key, None)
    if changed.get("auto_quality"):
        changed["auto_quality"] = {**changed["auto_quality"], "verdict": "stale"}
    if changed.get("image_result"):
        changed["image_result"] = {**changed["image_result"], "status": "stale"}
    return changed


def _confirmation(current, replacement):
    stamp = datetime.now(timezone.utc).isoformat()
    confirmed = {**current, **replacement, "text": current["text"], "updated_at": stamp}
    confirmation = {"id": "confirm-" + uuid4().hex, "document_id": current["document_id"],
                    "page": current["page"], "revision": current["revision"], "confirmed_at": stamp,
                    "method": "human", "calibration_id": current.get("calibration_id")}
    return [("source_pages", confirmed), ("source_confirmations", confirmation)]


def page_records(document_id, extracted, revision=1, action="import"):
    page = extracted["page"]
    text = extracted.get("text", "")
    blocks = text_blocks(document_id, [(page, text)])
    # Revision snapshots and block IDs never change; confirmations are separate.
    for block in blocks:
        block["block_id"] += f"-r{revision}"
    record = {
        **extracted, "id": page_id(document_id, page), "document_id": document_id,
        "revision": revision, "block_ids": [block["block_id"] for block in blocks],
        "image_url": f"/api/documents/{document_id}/pages/{page}/image",
        "updated_at": datetime.now(timezone.utc).isoformat(), "action": action,
    }
    record["quality"] = assess_page(record)
    snapshot = {**record, "id": f"{record['id']}-r{revision}"}
    return [("source_pages", record), ("source_revisions", snapshot)] + [("blocks", block) for block in blocks]


def require_current_source(store, blocks):
    """Drafting may use extracted text, but never failed, empty or stale pages."""
    for block in blocks:
        if not block.text.strip():
            raise HTTPException(409, f"第 {block.page} 页没有可用于制卡的文字，请重新识别或补充原文。")
        record = store.get("source_pages", page_id(block.document_id, block.page))
        if record is None:  # Existing text, demo and legacy PDF sources remain usable.
            continue
        if block.block_id not in record["block_ids"] and not valid_supplement(store, block, record):
            raise HTTPException(409, "原文已更新；请使用当前版本重新生成卡片，或更新原文引用后再审核。")
        if not source_available(record) or not record.get("text", "").strip():
            raise HTTPException(409, f"第 {block.page} 页未成功提取文字，请重新识别或补充原文后制卡。")


def require_current_reviewed(store, blocks, card=None):
    try:
        automatic = automatic_evidence_valid(card, blocks)
    except (ValueError, KeyError, TypeError, AttributeError):
        automatic = False
    for block in blocks:
        record = store.get("source_pages", page_id(block.document_id, block.page))
        if record is None:  # Older text, demo and legacy PDF sources remain usable.
            continue
        if not source_usable(record) and not (automatic and source_available(record)):
            raise HTTPException(409, f"请先对照原图校对并确认第 {block.page} 页文字。")
        if block.block_id not in record["block_ids"] and not valid_supplement(store, block, record):
            raise HTTPException(409, "原文已更新；请使用当前已校对版本重新生成卡片，或更新原文引用后再审核。")


def save_page_revision(store, document_id, page, expected, replacement, action, extra_records=()):
    def build():
        old = store.get("source_pages", page_id(document_id, page))
        if not old:
            raise HTTPException(404, "该页未导入；请确认所选 PDF 页码范围。")
        if old["revision"] != expected:
            raise HTTPException(409, "此页已在其他操作中更新。编辑内容未保存，请刷新版本后核对再提交。")
        updated = {**old, **replacement, "page": page}
        if action == "proofread" and updated.get("text", "").strip() == old.get("text", "").strip():
            # Confirming an extraction does not change the cited content. Keep
            # its exact text, block IDs and media revision, including PDF edge
            # newlines that the editor trims on submission. Validate expected
            # revision under the writer lock even for an idempotent confirm.
            if old.get("reviewed_at") and old["status"] == updated["status"] and not old.get("error"):
                return [("source_pages", old)] + list(extra_records)
            return _confirmation(old, {**replacement, "action": action}) + list(extra_records)
        if action != "auto_calibrate":
            updated.update(verification=None, calibration_id=None, calibration_status=None, calibration_issues=[])
        other_length = sum(len(item.get("text", "")) for item in store.all("source_pages")
                           if item["document_id"] == document_id and item["page"] != page)
        document = store.get("documents", document_id) or {}
        total_limit = 1_000_000 if document.get("automatic_import") else 100_000
        if len(updated.get("text", "")) > 50000 or other_length + len(updated.get("text", "")) > total_limit:
            raise HTTPException(422, f"原文过长；单页最多 5 万字，所选材料合计最多 {total_limit // 10000} 万字。")
        records = page_records(document_id, updated, old["revision"] + 1, action)
        old_ids = set(old["block_ids"])
        old_ids.update(proof["id"] for proof in store.all("quote_recoveries")
                       if proof.get("parent_id") in old_ids)
        for card in store.all("cards"):
            if old_ids.intersection(entry["block_id"] for entry in card["evidence"]):
                invalidated = invalidate_approval({**card, "status": "draft", "source_outdated": True,
                                                  "version": card["version"] + 1,
                                                  "updated_at": records[0][1]["updated_at"]})
                records.append(("cards", invalidated))
        return records + list(extra_records)

    return store.mutate(build)[0][1]


def confirm_machine_page(store, document_id, page, expected, text):
    """Human confirmation of identical text keeps immutable block identity."""
    def build():
        current = store.get("source_pages", page_id(document_id, page))
        if (not current or current["revision"] != expected or current["text"].strip() != text.strip()
                or not machine_verified(current)):
            raise HTTPException(409, "原文已变化，请刷新后重新确认。")
        return _confirmation(current, {"status": "reviewed", "reviewed_at": datetime.now(timezone.utc).isoformat()})
    return store.mutate(build)[0][1]
