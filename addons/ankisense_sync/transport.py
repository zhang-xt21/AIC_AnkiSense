"""Download a coherent, verified batch before touching an Anki collection."""
from __future__ import annotations

import hashlib
import json
import re

from .client import PluginClient, PluginError

FIELDS = ["NoteID", "Question", "Answer", "Concept", "SourceFile", "PDFPage", "SourceQuote", "PracticeURL"]
MAX_CARDS = 500
MAX_BATCH_MEDIA = 64 * 1024 * 1024


def _fail(message):
    raise PluginError(None, "invalid_sync_data", message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _catalog(client):
    result = client.catalog()
    cards, excluded = result.get("cards"), result.get("excluded")
    if not isinstance(cards, list) or not isinstance(excluded, list):
        _fail("未收到完整卡片清单，请重新检查连接。")
    if len(cards) > MAX_CARDS:
        _fail(f"当前插件单次最多同步 {MAX_CARDS} 张卡片；本次尚未写入。")
    seen = set()
    for item in cards:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", item["id"])
                or item["id"] in seen or type(item.get("version")) is not int
                or item["version"] < 1 or not isinstance(item.get("content_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["content_hash"])):
            _fail("卡片清单包含重复或无效记录，本次未写入。")
        seen.add(item["id"])
    expected = _digest({key: result[key] for key in ("protocol_version", "complete", "cards", "excluded")})
    if result.get("snapshot_id") != expected:
        _fail("卡片清单校验失败，请重新获取。")
    return result


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
        if (not isinstance(media, dict) or not isinstance(media.get("id"), str)
                or not re.fullmatch(r"img-[0-9a-f]{32}", media["id"])
                or media.get("filename") != "ankisense-" + media["id"] + ".png"
                or media.get("content_type") != "image/png"
                or type(media.get("size_bytes")) is not int or not 0 < media["size_bytes"] <= 5 * 1024 * 1024
                or not isinstance(media.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", media["sha256"])):
            _fail("配图信息不完整，本次未写入。")


def check_connection(path, client_id):
    client = PluginClient.from_connection_file(path, client_id, timeout=15)
    status = client.status()
    catalog = _catalog(client)
    return {"server_id": status["server_id"], "base_url": client.base_url,
            "cards": len(catalog["cards"]), "excluded": len(catalog["excluded"]),
            "ai": status.get("ai", {}), "protocol_version": 1}


def prepare_bundle(path, client_id):
    client = PluginClient.from_connection_file(path, client_id, timeout=30)
    status = client.status()
    catalog = _catalog(client)
    cards, media_bytes, media_digests = [], {}, {}
    total = 0
    for entry in catalog["cards"]:
        card = client.card(entry["id"], expected_hash=entry["content_hash"])
        _validate_card(card, entry)
        for asset in card["media"]:
            filename, digest = asset["filename"], asset["sha256"]
            if filename in media_bytes:
                if media_digests[filename] != digest or len(media_bytes[filename]) != asset["size_bytes"]:
                    _fail("不同卡片对同一图片提供了不一致的内容。")
                continue
            total += asset["size_bytes"]
            if total > MAX_BATCH_MEDIA:
                _fail("本次配图超过 64 MB，尚未写入 Anki。")
            content = client.media(asset["id"], digest)
            if len(content) != asset["size_bytes"] or hashlib.sha256(content).hexdigest() != digest:
                _fail("图片下载不完整，尚未写入 Anki。")
            media_bytes[filename], media_digests[filename] = content, digest
        cards.append(card)
    # Do not apply a batch that changed while images were downloading.
    if _catalog(client)["snapshot_id"] != catalog["snapshot_id"]:
        raise PluginError(409, "card_changed", "下载期间卡片或审核状态发生变化，请重新同步。")
    return {"server_id": status["server_id"], "snapshot_id": catalog["snapshot_id"],
            "cards": cards, "excluded": catalog["excluded"], "media": media_bytes}
