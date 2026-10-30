"""Download a coherent, verified batch before touching an Anki collection."""
from __future__ import annotations

import hashlib
import json
import re

from .client import PluginClient, PluginError

FIELDS = ["NoteID", "Question", "Answer", "Concept", "SourceFile", "PDFPage", "SourceQuote", "PracticeURL"]
MAX_CARDS = 100
MAX_BATCH_MEDIA = 32 * 1024 * 1024


def _fail(message):
    raise PluginError(None, "invalid_sync_data", message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _catalog(client, document_id=None):
    result = client.catalog(document_id) if document_id else client.catalog()
    cards, excluded = result.get("cards"), result.get("excluded")
    if not isinstance(cards, list) or not isinstance(excluded, list):
        _fail("未收到完整卡片清单，请重新检查连接。")
    seen = set()
    for item in cards:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", item["id"])
                or item["id"] in seen or type(item.get("version")) is not int
                or item["version"] < 1 or not isinstance(item.get("content_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["content_hash"])):
            _fail("卡片清单包含重复或无效记录，本次未写入。")
        if (not isinstance(item.get("media"), list) or len(item["media"]) > 4
                or any(not _valid_asset(asset) for asset in item["media"])):
            _fail("配图清单无效，本次未写入。")
        seen.add(item["id"])
    expected = _digest({key: result[key] for key in ("protocol_version", "complete", "cards", "excluded")})
    if result.get("snapshot_id") != expected:
        _fail("卡片清单校验失败，请重新获取。")
    return result


def _valid_asset(media):
    return (isinstance(media, dict) and isinstance(media.get("id"), str)
            and re.fullmatch(r"img-[0-9a-f]{32}", media["id"])
            and media.get("filename") == "ankisense-" + media["id"] + ".png"
            and media.get("content_type") == "image/png"
            and type(media.get("size_bytes")) is int and 0 < media["size_bytes"] <= 5 * 1024 * 1024
            and isinstance(media.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", media["sha256"]))


def _validate_card(card, entry):
    if (type(card.get("protocol_version")) is not int or card["protocol_version"] != 1
            or card.get("id") != entry["id"] or card.get("version") != entry["version"]
            or card.get("guid") != entry.get("guid")
            or card.get("content_hash") != entry["content_hash"]
            or _digest({key: value for key, value in card.items() if key != "content_hash"}) != entry["content_hash"]):
        _fail("卡片内容与清单版本不一致，请重新同步。")
    if (card.get("field_order") != FIELDS or not isinstance(card.get("fields"), dict)
            or set(card["fields"]) != set(FIELDS)
            or not all(isinstance(card["fields"][key], str) for key in FIELDS)
            or card["fields"]["NoteID"] != card["id"]
            or any("\x1f" in value for value in card["fields"].values())):
        _fail("卡片字段与插件版本不兼容。")
    model = card.get("model")
    if (not isinstance(model, dict) or model.get("id") != 1786831001
            or model.get("type") != "standard" or model.get("sort_field_index") != 1
            or not isinstance(model.get("fields"), list)
            or [field.get("name") if isinstance(field, dict) else None for field in model["fields"]] != FIELDS
            or not isinstance(model.get("templates"), list) or len(model["templates"]) != 1
            or not isinstance(model["templates"][0], dict)
            or model["templates"][0].get("ordinal") != 0
            or not all(isinstance(model["templates"][0].get(key), str) for key in ("name", "qfmt", "afmt"))
            or not isinstance(model.get("css"), str) or not isinstance(model.get("name"), str)):
        _fail("卡片模板与插件版本不兼容。")
    if (not isinstance(card.get("tags"), list)
            or any(not isinstance(tag, str) or re.search(r"\s", tag) for tag in card["tags"])
            or not isinstance(card.get("guid"), str) or not card["guid"]
            or not isinstance(card.get("media"), list) or card["media"] != entry.get("media")):
        _fail("卡片标识或配图清单无效。")
    for media in card["media"]:
        if not _valid_asset(media):
            _fail("配图信息不完整，本次未写入。")


def check_connection(path, client_id):
    client = PluginClient.from_connection_file(path, client_id, timeout=15)
    status = client.status()
    catalog = _catalog(client)
    return {"server_id": status["server_id"], "base_url": client.base_url,
            "cards": len(catalog["cards"]), "excluded": len(catalog["excluded"]),
            "documents": catalog.get("documents", []),
            "ai": status.get("ai", {}), "protocol_version": 1}


def _prepare(client, server_id, catalog, entries, inventory=None, document_id=None):
    inventory = inventory or {}
    local_media = inventory.get("media", {})
    cards, media_bytes, media_digests = [], {}, {}
    for entry in entries:
        card = client.card(entry["id"], expected_hash=entry["content_hash"])
        _validate_card(card, entry)
        for asset in card["media"]:
            filename, digest = asset["filename"], asset["sha256"]
            if filename in media_bytes:
                if media_digests[filename] != digest or len(media_bytes[filename]) != asset["size_bytes"]:
                    _fail("不同卡片对同一图片提供了不一致的内容。")
                continue
            if local_media.get(filename) == digest:
                continue
            content = client.media(asset["id"], digest)
            if len(content) != asset["size_bytes"] or hashlib.sha256(content).hexdigest() != digest:
                _fail("图片下载不完整，尚未写入 Anki。")
            media_bytes[filename], media_digests[filename] = content, digest
        cards.append(card)
    # Do not apply a batch that changed while images were downloading.
    if _catalog(client, document_id)["snapshot_id"] != catalog["snapshot_id"]:
        raise PluginError(409, "card_changed", "下载期间卡片或审核状态发生变化，请重新同步。")
    return {"server_id": server_id, "snapshot_id": catalog["snapshot_id"],
            "cards": cards, "excluded": catalog["excluded"], "media": media_bytes,
            "local_media": local_media, "catalog_ids": [item["id"] for item in catalog["cards"]],
            "scope_document_id": document_id}


class SyncSession:
    """One user operation, transferred and checkpointed in bounded batches."""
    def __init__(self, path, client_id, document_id=None):
        self.client = PluginClient.from_connection_file(path, client_id, timeout=30)
        self.server_id = self.client.status()["server_id"]
        self.document_id = document_id
        self.catalog = _catalog(self.client, document_id)
        self.position = 0
        self.total = len(self.catalog["cards"])
        self.started = False

    @property
    def has_more(self):
        return not self.started or self.position < self.total

    def upcoming(self):
        return self.catalog["cards"][self.position:self.position + MAX_CARDS]

    def next_bundle(self, inventory=None):
        inventory = inventory or {}
        unchanged = set(inventory.get("unchanged_ids", []))
        entries, seen, total, skipped = [], set(), 0, 0
        start = self.position
        while self.position < self.total and self.position - start < MAX_CARDS:
            entry = self.catalog["cards"][self.position]
            pending = [asset for asset in entry["media"] if asset["filename"] not in seen
                       and inventory.get("media", {}).get(asset["filename"]) != asset["sha256"]]
            size = sum(asset["size_bytes"] for asset in pending) if entry["id"] not in unchanged else 0
            if self.position > start and total + size > MAX_BATCH_MEDIA:
                break
            if entry["id"] in unchanged:
                skipped += 1
            else:
                entries.append(entry)
                total += size
                seen.update(asset["filename"] for asset in pending)
            self.position += 1
        bundle = _prepare(self.client, self.server_id, self.catalog, entries, inventory, self.document_id)
        bundle["skipped_unchanged"] = skipped
        self.started = True
        return bundle


def prepare_bundle(path, client_id):
    """Compatibility helper for small one-batch Python integrations."""
    session = SyncSession(path, client_id)
    bundle = session.next_bundle()
    if session.has_more:
        _fail("本次内容需要分批同步，请使用新版同步窗口。尚未写入 Anki。")
    return bundle
