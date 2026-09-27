"""Isolated plugin snapshots, APKG compatibility, and media access checks."""

import hashlib
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from PIL import Image

from ankisense.apkg import DECK_ID, MODEL_ID, build_apkg
from ankisense.db import Store
from ankisense.demo import seed_records
from ankisense.media import normalize_image, save_media
from ankisense.plugin_sync import PluginSync
from ankisense.sources import page_id


BASE = "http://127.0.0.1:8765"


@pytest.fixture
def sync(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.put_many(seed_records())
    return PluginSync(store, tmp_path)


def approve(sync, identifier="demo-stack", **changes):
    card = {**sync.store.get("cards", identifier), "status": "approved", **changes}
    sync.store.put("cards", card)
    return card


def image(sync, color="green", **options):
    output = io.BytesIO()
    with Image.new("RGB", (30, 20), color) as picture:
        picture.save(output, "PNG")
    return save_media(sync.store, sync.storage, normalize_image(output.getvalue()), **options)


def test_catalog_is_full_deterministic_and_read_only(sync):
    assert sync.catalog(BASE)["cards"] == []
    before = sync.store.all("cards")
    with pytest.raises(HTTPException) as error:
        sync.card(before[0]["id"], BASE)
    assert error.value.status_code == 404
    approve(sync, "demo-stack")
    approve(sync, "demo-queue")
    before = sync.store.all("cards")
    first = sync.catalog(BASE)
    assert first == sync.catalog(BASE + "/")
    assert set(first) == {"protocol_version", "snapshot_id", "complete", "cards", "excluded"}
    assert first["complete"] is True and first["protocol_version"] == 1
    assert [item["id"] for item in first["cards"]] == ["demo-queue", "demo-stack"]
    assert len(first["snapshot_id"]) == 64
    assert first["excluded"] == []
    summary = first["cards"][0]
    assert set(summary) == {"id", "version", "guid", "content_hash", "url", "media"}
    assert summary["url"] == f"{BASE}/api/plugin/v1/cards/demo-queue?expected_hash={summary['content_hash']}"
    assert sync.store.all("cards") == before


def test_plugin_contract_matches_real_apkg_notes_templates_deck_and_media(sync, tmp_path):
    question_image, answer_image = image(sync), image(sync, "red")
    card = approve(sync, question='<script>unsafe</script> [sound:evil.mp3]\nquestion',
                   answer='<img src="https://remote.example/tracker"> answer',
                   images=[{"media_id": question_image["id"], "side": "question", "caption": "<caption>"},
                           {"media_id": answer_image["id"], "side": "answer", "caption": "答案图"}])
    payload = sync.card(card["id"], BASE)
    assert set(payload) == {"protocol_version", "id", "version", "guid", "fields", "field_order",
                            "model", "deck", "tags", "source", "images", "media", "content_hash"}
    document = sync.store.get("documents", card["document_id"])
    values = [card["id"], card["question"], card["answer"], card["concept"], document["filename"], "",
              "\n".join(item["quote"] for item in card["evidence"]), f"{BASE}/?card={card['id']}"]
    package = build_apkg([(card, values)], sync.store, sync.storage)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        database = tmp_path / "package.sqlite3"
        database.write_bytes(archive.read("collection.anki2"))
        with sqlite3.connect(database) as connection:
            guid, model_id, fields, tags = connection.execute("SELECT guid, mid, flds, tags FROM notes").fetchone()
            deck_id, = connection.execute("SELECT did FROM cards").fetchone()
            model_json, = connection.execute("SELECT models FROM col").fetchone()
        exported_model = json.loads(model_json)[str(MODEL_ID)]
        assert payload["guid"] == guid
        assert payload["model"]["id"] == model_id == MODEL_ID
        assert payload["deck"]["id"] == deck_id == DECK_ID
        assert list(payload["fields"]) == payload["field_order"]
        assert list(payload["fields"].values()) == fields.split("\x1f")
        assert set(payload["tags"]) == set(tags.split())
        assert payload["model"]["css"] == exported_model["css"]
        assert payload["model"]["sort_field_index"] == exported_model["sortf"]
        for template, exported in zip(payload["model"]["templates"], exported_model["tmpls"]):
            assert template["ordinal"] == exported["ord"]
            assert all(template[key] == exported[key] for key in ("name", "qfmt", "afmt"))
        manifest = json.loads(archive.read("media"))
        for index, filename in manifest.items():
            asset = next(item for item in payload["media"] if item["filename"] == filename)
            metadata, path = sync.media(asset["id"], asset["sha256"])
            assert path.read_bytes() == archive.read(index)
            assert metadata["sha256"] == hashlib.sha256(archive.read(index)).hexdigest()
            assert asset["url"] == f"{BASE}/api/plugin/v1/media/{asset['id']}?sha256={asset['sha256']}"
    assert question_image["filename"] in payload["fields"]["Question"]
    assert question_image["filename"] not in payload["fields"]["Answer"]
    assert "<script>" not in payload["fields"]["Question"]
    assert "[sound:" not in payload["fields"]["Question"]
    assert '<img src="https://' not in payload["fields"]["Answer"]
    assert str(sync.storage) not in json.dumps(payload)
    assert not ({"schedule", "due", "reps", "interval", "delete"} & payload.keys())


def test_updates_keep_guid_and_change_version_hash_and_snapshot(sync):
    card = approve(sync)
    before = sync.card(card["id"], BASE)
    catalog = sync.catalog(BASE)
    approve(sync, answer="新的已审核答案", version=2)
    after = sync.card(card["id"], BASE)
    assert before["guid"] == after["guid"]
    assert before["content_hash"] != after["content_hash"] and after["version"] == 2
    assert catalog["snapshot_id"] != sync.catalog(BASE)["snapshot_id"]
    assert before["deck"] == after["deck"] and before["model"] == after["model"]
    sync.store.put("cards", {**card, "status": "draft", "version": 3})
    assert sync.catalog(BASE)["cards"] == []
    with pytest.raises(HTTPException) as error:
        sync.card(card["id"], BASE)
    assert error.value.status_code == 404


@pytest.mark.parametrize("state", ["outdated", "unreviewed", "old_revision", "foreign_block",
                                    "invalid_quote", "duplicate_rubric", "missing_document"])
def test_catalog_excludes_approved_cards_with_invalid_evidence(sync, state):
    card = approve(sync)
    block = sync.store.get("blocks", card["evidence"][0]["block_id"])
    if state == "outdated":
        sync.store.put("cards", {**card, "source_outdated": True})
    elif state in {"unreviewed", "old_revision"}:
        sync.store.put("source_pages", {"id": page_id(card["document_id"], block["page"]),
                       "status": "needs_review" if state == "unreviewed" else "reviewed",
                       "block_ids": [block["block_id"]] if state == "unreviewed" else ["new-block"], "revision": 2})
    elif state == "foreign_block":
        sync.store.put("blocks", {**block, "document_id": "other-document"})
    elif state == "invalid_quote":
        sync.store.put("cards", {**card, "evidence": [{"block_id": block["block_id"], "quote": "不存在的原文"}]})
    elif state == "duplicate_rubric":
        sync.store.put("cards", {**card, "rubric": card["rubric"] * 2})
    else:
        sync.store.put("cards", {**card, "document_id": "missing"})
    catalog = sync.catalog(BASE)
    assert catalog["cards"] == [] and catalog["complete"] is True
    assert catalog["excluded"][0]["id"] == card["id"]
    with pytest.raises(HTTPException) as error:
        sync.card(card["id"], BASE)
    assert error.value.status_code == 409


def test_media_needs_a_current_approved_card_and_digest(sync):
    asset = image(sync)
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"])
    assert error.value.status_code == 404
    card = approve(sync, images=[{"media_id": asset["id"], "side": "answer"}])
    metadata = sync.card(card["id"], BASE)["media"][0]
    assert sync.media(asset["id"], metadata["sha256"])[0]["sha256"] == metadata["sha256"]
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"], "0" * 64)
    assert error.value.status_code == 409
    sync.store.put("cards", {**card, "status": "draft"})
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"], metadata["sha256"])
    assert error.value.status_code == 404


@pytest.mark.parametrize("damage", ["missing", "invalid", "oversized", "metadata", "digest"])
def test_invalid_media_prevents_card_sync_and_media_access(sync, damage):
    asset = image(sync)
    card = approve(sync, images=[{"media_id": asset["id"], "side": "question"}])
    _, path = sync.media(asset["id"])
    if damage == "missing":
        path.unlink()
    elif damage == "digest":
        sync.store.put("media", {**asset, "sha256": "0" * 64})
    elif damage == "metadata":
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add_text("note", "unexpected metadata")
        with Image.new("RGB", (30, 20), "green") as picture:
            picture.save(path, "PNG", pnginfo=info)
    else:
        path.write_bytes(b"bad png" if damage == "invalid" else b"x" * (5 * 1024 * 1024 + 1))
    assert sync.catalog(BASE)["cards"] == []
    with pytest.raises(HTTPException) as error:
        sync.card(card["id"], BASE)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"])
    assert error.value.status_code == 404


def test_media_hash_detects_same_version_file_change(sync):
    asset = image(sync)
    card = approve(sync, images=[{"media_id": asset["id"]}])
    before = sync.card(card["id"], BASE)
    _, path = sync.media(asset["id"])
    other = image(sync, "red")
    other_path = sync.storage / "media" / f"{other['id']}.png"
    path.write_bytes(other_path.read_bytes())
    after = sync.card(card["id"], BASE)
    assert before["version"] == after["version"]
    assert before["content_hash"] != after["content_hash"]
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"], before["media"][0]["sha256"])
    assert error.value.status_code == 409


def test_source_image_revision_must_still_be_current(sync):
    card = approve(sync)
    block = sync.store.get("blocks", card["evidence"][0]["block_id"])
    source = {"document_id": card["document_id"], "page": block["page"], "revision": 1}
    sync.store.put("source_pages", {"id": page_id(card["document_id"], block["page"]),
                   "status": "reviewed", "revision": 1, "block_ids": [block["block_id"]]})
    asset = image(sync, source=source)
    approve(sync, images=[{"media_id": asset["id"]}])
    assert len(sync.catalog(BASE)["cards"]) == 1
    sync.store.put("media", {**asset, "source": {**source, "revision": 0}})
    assert sync.catalog(BASE)["cards"] == []
    with pytest.raises(HTTPException) as error:
        sync.media(asset["id"])
    assert error.value.status_code == 404


def test_snapshot_keeps_source_and_card_consistent_during_edit(sync, monkeypatch):
    card = approve(sync)
    expected = sync.card(card["id"], BASE)
    entered, release = Event(), Event()
    original = sync._card

    def delayed(card, reader, base, cache):
        entered.set()
        assert release.wait(10)
        return original(card, reader, base, cache)

    monkeypatch.setattr(sync, "_card", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sync.catalog, BASE)
        try:
            assert entered.wait(10)
            sync.store.put("cards", {**card, "status": "draft", "version": 2})
            block = sync.store.get("blocks", card["evidence"][0]["block_id"])
            sync.store.put("blocks", {**block, "text": "changed source"})
        finally:
            release.set()
        catalog = future.result(timeout=10)
    assert catalog["cards"][0]["content_hash"] == expected["content_hash"]
    assert sync.catalog(BASE)["cards"] == []


@pytest.mark.parametrize("base", ["https://remote.example", "http://localhost@evil.example",
                                   "http://localhost:99999", "http://localhost/prefix"])
def test_nonlocal_or_malformed_origin_is_rejected_even_for_empty_catalog(sync, base):
    with pytest.raises(HTTPException) as error:
        sync.catalog(base)
    assert error.value.status_code == 422
