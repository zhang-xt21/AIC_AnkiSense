"""Immutable source revisions alongside the existing four-field evidence blocks."""
from datetime import datetime, timezone

from fastapi import HTTPException

from .ingest import text_blocks


def page_id(document_id, page):
    return f"{document_id}-p{page}"


def page_records(document_id, extracted, revision=1, action="import"):
    page = extracted["page"]
    text = extracted.get("text", "")
    blocks = text_blocks(document_id, [(page, text)])
    # A revision is never overwritten. Cards continue to resolve old block IDs.
    for block in blocks:
        block["block_id"] += f"-r{revision}"
    record = {
        **extracted, "id": page_id(document_id, page), "document_id": document_id,
        "revision": revision, "block_ids": [block["block_id"] for block in blocks],
        "image_url": f"/api/documents/{document_id}/pages/{page}/image",
        "updated_at": datetime.now(timezone.utc).isoformat(), "action": action,
    }
    snapshot = {**record, "id": f"{record['id']}-r{revision}"}
    return [("source_pages", record), ("source_revisions", snapshot)] + [("blocks", block) for block in blocks]


def require_current_reviewed(store, blocks):
    for block in blocks:
        record = store.get("source_pages", page_id(block.document_id, block.page))
        if record is None:  # Existing text, demo and legacy PDF sources remain usable.
            continue
        if record["status"] != "reviewed":
            raise HTTPException(409, f"请先对照原图校对并确认第 {block.page} 页文字。")
        if block.block_id not in record["block_ids"]:
            raise HTTPException(409, "原文已更新；请使用当前已校对版本重新生成卡片，或更新原文引用后再审核。")


def save_page_revision(store, document_id, page, expected, replacement, action):
    def build():
        old = store.get("source_pages", page_id(document_id, page))
        if not old:
            raise HTTPException(404, "该页未导入；请确认所选 PDF 页码范围。")
        if old["revision"] != expected:
            raise HTTPException(409, "此页已在其他操作中更新。编辑内容未保存，请刷新版本后核对再提交。")
        updated = {**old, **replacement, "page": page}
        other_length = sum(len(item.get("text", "")) for item in store.all("source_pages")
                           if item["document_id"] == document_id and item["page"] != page)
        if len(updated.get("text", "")) > 50000 or other_length + len(updated.get("text", "")) > 100000:
            raise HTTPException(422, "原文过长；单页最多 5 万字，所选材料合计最多 10 万字。")
        records = page_records(document_id, updated, old["revision"] + 1, action)
        old_ids = set(old["block_ids"])
        for card in store.all("cards"):
            if old_ids.intersection(entry["block_id"] for entry in card["evidence"]):
                records.append(("cards", {**card, "status": "draft", "source_outdated": True,
                                         "version": card["version"] + 1,
                                         "updated_at": records[0][1]["updated_at"]}))
        return records

    return store.mutate(build)[0][1]
