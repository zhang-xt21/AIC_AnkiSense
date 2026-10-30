"""Transactional card removal; lecture files and Anki copies are independent."""
import json
from datetime import datetime, timezone

from fastapi import HTTPException


def delete_cards(store, *, document_id=None, card_id=None, expected_count=None, remove_document=False):
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = [(collection, identifier, json.loads(payload)) for collection, identifier, payload
                in connection.execute("SELECT collection,id,payload FROM records")]
        cards = [record for collection, _, record in rows if collection == "cards"
                 and (record.get("id") == card_id if card_id else record.get("document_id") == document_id)]
        if card_id and not cards:
            raise HTTPException(404, "卡片已删除或不存在。")
        if document_id and not any(collection == "documents" and identifier == document_id for collection, identifier, _ in rows):
            raise HTTPException(404, "学习材料不存在。")
        if expected_count is not None and expected_count != len(cards):
            raise HTTPException(409, "卡片数量已变化，请刷新后重新确认删除范围。")
        documents = {card["document_id"] for card in cards}
        if remove_document:
            documents.add(document_id)
        ids = {card["id"] for card in cards}
        for collection, _, record in rows:
            if (collection == "cards" and record.get("id") in ids
                    and record.get("review_state") in {"pending", "running", "queued"}):
                raise HTTPException(409, "卡片正在检查，请等待本次操作结束后再删除。")
            if (collection in {"auto_card_jobs", "generation_jobs", "material_imports"}
                    and record.get("document_id") in documents
                    and record.get("status") in {"pending", "running", "queued"}):
                raise HTTPException(409, "该材料正在制卡，请先取消任务，待任务停止后再删除。")
            if (record.get("card_id") in ids and record.get("status") in {"pending", "running", "queued"}):
                raise HTTPException(409, "卡片正在检查或分析，请等待本次操作结束后再删除。")
        timestamp = datetime.now(timezone.utc).isoformat()
        for identifier in ids:
            connection.execute("INSERT OR IGNORE INTO records VALUES('deleted_cards',?,?)",
                               (identifier, json.dumps({"id": identifier, "deleted_at": timestamp})))
        for collection, identifier, record in rows:
            owned = (remove_document and (
                (collection == "documents" and identifier == document_id)
                or record.get("document_id") == document_id))
            if owned or (collection == "cards" and identifier in ids) or record.get("card_id") in ids:
                connection.execute("DELETE FROM records WHERE collection=? AND id=?", (collection, identifier))
            elif collection in {"auto_card_jobs", "generation_jobs"} and ids.intersection(record.get("card_ids", [])):
                record["card_ids"] = [value for value in record["card_ids"] if value not in ids]
                record["message"] = f"当前保留 {len(record['card_ids'])} 张卡片。已删除的卡片不会随重试恢复。"
                connection.execute("UPDATE records SET payload=? WHERE collection=? AND id=?",
                                   (json.dumps(record, ensure_ascii=False), collection, identifier))
    return {"deleted_count": len(ids), "deleted_ids": sorted(ids),
            "deleted_document_id": document_id if remove_document else None, "anki_copies_removed": False}
