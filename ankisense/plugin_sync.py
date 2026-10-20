"""Read-only, versioned Anki note snapshots shared with APKG export.

A complete catalog describes what may be synced now; absence is never an Anki
deletion instruction. Scheduling and review history belong to the Anki client.
"""

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from .apkg import (DECK_ID, DECK_NAME, _practice_url, anki_model, export_snapshot,
                   media_filename, note_fields, note_guid, note_tags)
from .evidence import validate_card
from .media import MAX_UPLOAD_BYTES, normalize_image, require_media
from .schemas import CardUpdate, SourceBlock
from .sources import page_id, require_current_reviewed
from .source_policy import source_usable


PROTOCOL_VERSION = 1
API_PREFIX = "/api/plugin/v1"


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _base_url(value):
    base = str(value).rstrip("/")
    # Apply the same local-origin and injection checks as exported Anki links.
    _practice_url(f"{base}/?card=plugin-origin-check", "plugin-origin-check")
    return base


def model_contract():
    model = anki_model()
    return {"id": model.model_id, "name": model.name, "type": "standard",
            "fields": model.fields, "sort_field_index": model.sort_field_index,
            "templates": [{**template, "ordinal": index}
                          for index, template in enumerate(model.templates)],
            "css": model.css}


class PluginSync:
    def __init__(self, store, storage: Path):
        self.store = store
        self.storage = Path(storage)

    def _validated(self, card, reader):
        """Check the complete approved card against one current DB snapshot."""
        if not card or card.get("status") != "approved":
            raise HTTPException(404, "这张卡片尚未批准或不存在。")
        try:
            if (not isinstance(card["id"], str) or not card["id"]
                    or type(card["version"]) is not int or card["version"] < 1):
                raise ValueError("invalid identity or version")
            draft = CardUpdate.model_validate({key: card[key] for key in CardUpdate.model_fields
                                              if key in card})
            document = reader.get("documents", card["document_id"])
            if not document or not isinstance(document.get("filename"), str):
                raise ValueError("missing source document")
            blocks = []
            for identifier in sorted({entry.block_id for entry in draft.evidence}):
                block = reader.get("blocks", identifier)
                if not block or block["document_id"] != card["document_id"]:
                    raise ValueError("missing or foreign evidence")
                blocks.append(SourceBlock.model_validate(block))
            validate_card(draft, blocks)
            if card.get("source_outdated"):
                raise HTTPException(409, "卡片引用的原文已更新，请重新审核后同步。")
            require_current_reviewed(reader, blocks, card)
            return document, blocks
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(409, "卡片内容或引用无效，请重新审核后同步。") from error

    def _asset(self, reader, identifier, document_id):
        """Only normalized, bounded PNGs are transferred under APKG filenames."""
        try:
            record, path = require_media(reader, self.storage, identifier, document_id)
            # IDs constrain filenames; additionally refuse symlinks escaping storage.
            if path.resolve().parent != self.storage.resolve() / "media":
                raise HTTPException(409, "图片文件位置异常，请重新添加图片。")
            source = record.get("source") or {}
            if source:
                current = reader.get("source_pages", page_id(document_id, source.get("page")))
                if (not current or (not source_usable(current) and source.get("origin") != "automatic_source_page")
                        or current.get("revision") != source.get("revision")):
                    raise HTTPException(409, "配图所用的原文页已更新或未校对，请重新审核配图。")
            with path.open("rb") as handle:
                content = handle.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(409, "图片已损坏或超过限制，请重新添加图片。")
            normalized, width, height = normalize_image(content)
            if normalized != content:
                raise HTTPException(409, "图片文件已发生变化，请重新添加图片后审核。")
            digest = hashlib.sha256(content).hexdigest()
            if record.get("sha256") and record["sha256"] != digest:
                raise HTTPException(409, "图片校验失败，请重新添加图片后审核。")
            metadata = {"id": identifier, "filename": media_filename(identifier),
                        "content_type": "image/png", "sha256": digest,
                        "size_bytes": len(content), "width": width, "height": height}
            return metadata, path
        except HTTPException as error:
            if error.status_code == 409:
                raise
            raise HTTPException(409, "卡片配图缺失或无法读取，请重新添加图片后审核。") from error
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(409, "卡片配图无法读取，请重新添加图片后审核。") from error

    def _assets(self, card, reader, cache):
        assets = []
        seen = set()
        for attachment in card.get("images") or []:
            identifier = attachment["media_id"]
            key = (identifier, card["document_id"])
            if key not in cache:
                cache[key] = self._asset(reader, identifier, card["document_id"])
            if identifier not in seen:
                assets.append(cache[key][0])
                seen.add(identifier)
        return assets

    def _card(self, card, reader, base, cache):
        document, blocks = self._validated(card, reader)
        assets = self._assets(card, reader, cache)
        media = [{**asset, "url": f"{base}{API_PREFIX}/media/{asset['id']}?sha256={asset['sha256']}"}
                 for asset in assets]
        pages = sorted({block.page for block in blocks})
        values = [card["id"], card["question"], card["answer"], card["concept"],
                  document["filename"], ", ".join(map(str, pages)) if document["id"].startswith("pdf-") else "",
                  "\n".join(entry["quote"] for entry in card["evidence"]),
                  f"{base}/?card={quote(card['id'], safe='')}"]
        model = model_contract()
        field_order = [field["name"] for field in model["fields"]]
        blocks_by_id = {block.block_id: block for block in blocks}
        evidence = []
        for entry in card["evidence"]:
            block = blocks_by_id[entry["block_id"]]
            source_page = reader.get("source_pages", page_id(card["document_id"], block.page))
            evidence.append({"block_id": block.block_id, "quote": entry["quote"],
                             "page": block.page,
                             "source_revision": source_page.get("revision") if source_page else None})
        payload = {"protocol_version": PROTOCOL_VERSION, "id": card["id"],
                   "version": card["version"], "guid": note_guid(card["id"]),
                   "fields": dict(zip(field_order, note_fields(card, values))),
                   "field_order": field_order, "model": model,
                   "deck": {"id": DECK_ID, "name": DECK_NAME}, "tags": note_tags(card),
                   "source": {"document_id": card["document_id"], "filename": document["filename"],
                              "is_demo": bool(card.get("is_demo")), "evidence": evidence},
                   "images": [{"media_id": item["media_id"], "side": item.get("side", "answer"),
                               "caption": item.get("caption", "")} for item in card.get("images") or []],
                   "media": media}
        return {**payload, "content_hash": _digest(payload)}

    def catalog(self, base_url):
        """Return a full deterministic manifest; never infer client deletions."""
        base = _base_url(base_url)
        cards, excluded, cache = [], [], {}
        with export_snapshot(self.store) as reader:
            for card in sorted(reader.all("cards"), key=lambda item: item["id"]):
                if card.get("status") != "approved":
                    continue
                try:
                    payload = self._card(card, reader, base, cache)
                except HTTPException as error:
                    excluded.append({"id": card["id"], "version": card.get("version"),
                                     "reason": error.detail})
                    continue
                cards.append({key: payload[key] for key in ("id", "version", "guid", "content_hash", "media")})
                cards[-1]["url"] = f"{base}{API_PREFIX}/cards/{quote(card['id'], safe='')}?expected_hash={payload['content_hash']}"
        manifest = {"protocol_version": PROTOCOL_VERSION, "complete": True,
                    "cards": cards, "excluded": excluded}
        return {**manifest, "snapshot_id": _digest(manifest)}

    def card(self, identifier, base_url):
        base = _base_url(base_url)
        with export_snapshot(self.store) as reader:
            return self._card(reader.get("cards", identifier), reader, base, {})

    def media(self, identifier, expected_sha256=None):
        """Return metadata/path only while a complete eligible card references it.

        Callers pass the catalog digest and clients verify the received bytes.
        The path is an internal return value and must never enter JSON responses.
        """
        with export_snapshot(self.store) as reader:
            cache = {}
            for card in reader.all("cards"):
                if card.get("status") != "approved" or not any(
                        item.get("media_id") == identifier for item in card.get("images") or []):
                    continue
                try:
                    self._validated(card, reader)
                    self._assets(card, reader, cache)
                except HTTPException:
                    continue
                metadata, path = cache[(identifier, card["document_id"])]
                if expected_sha256 is not None and expected_sha256 != metadata["sha256"]:
                    raise HTTPException(409, "图片版本已变化，请重新获取同步清单。")
                return metadata, path
        raise HTTPException(404, "没有当前已审核的卡片引用这张图片。")
