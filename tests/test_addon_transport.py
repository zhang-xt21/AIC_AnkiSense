"""Transport staging uses real server payloads and never opens Anki data."""
import copy
import hashlib
import io
import random
from pathlib import Path

import pytest
from PIL import Image

from addons.ankisense_sync import transport
from addons.ankisense_sync.client import PluginError
from ankisense.db import Store
from ankisense.demo import seed_records
from ankisense.media import normalize_image, save_media
from ankisense.plugin_sync import PluginSync


@pytest.fixture
def source(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    store.put_many(seed_records())
    image = io.BytesIO()
    Image.new("RGB", (24, 16), "green").save(image, "PNG")
    asset = save_media(store, tmp_path, normalize_image(image.getvalue()))
    card = store.get("cards", "demo-stack")
    card.update(status="approved", images=[{"media_id": asset["id"], "side": "answer", "caption": "原图"}])
    store.put("cards", card)
    sync = PluginSync(store, tmp_path)
    server_id = "as-" + "1" * 32
    catalog = {**sync.catalog("http://127.0.0.1:8765"), "server_id": server_id}
    detail = sync.card(card["id"], "http://127.0.0.1:8765")
    _, file = sync.media(asset["id"])

    class Client:
        base_url = "http://127.0.0.1:8765"

        def __init__(self):
            self.calls = []
            self.catalog_value = catalog
            self.detail = detail
            self.bytes = file.read_bytes()

        def status(self):
            self.calls.append("status")
            return {"server_id": server_id, "ai": {"mode": "demo"}}

        def catalog(self):
            self.calls.append("catalog")
            return copy.deepcopy(self.catalog_value)

        def card(self, identifier, expected_hash=None):
            self.calls.append("card")
            assert identifier == self.detail["id"]
            return copy.deepcopy(self.detail)

        def media(self, identifier, digest):
            self.calls.append("media")
            return self.bytes

    client = Client()
    monkeypatch.setattr(transport.PluginClient, "from_connection_file", lambda *a, **kw: client)
    return client


def rehash(client):
    client.detail["content_hash"] = transport._digest({k: v for k, v in client.detail.items() if k != "content_hash"})
    entry = client.catalog_value["cards"][0]
    entry.update({k: copy.deepcopy(client.detail[k]) for k in ("id", "version", "guid", "content_hash", "media")})
    client.catalog_value["snapshot_id"] = transport._digest({k: client.catalog_value[k] for k in ("protocol_version", "complete", "cards", "excluded")})


def test_connection_is_read_only_and_has_no_credentials(source):
    result = transport.check_connection("unused", "profile-001")
    assert result["cards"] == 1 and result["excluded"] == 0
    assert source.calls == ["status", "catalog"]
    assert not {"token", "api_key"} & result.keys()


def test_stages_complete_approved_card_and_image(source):
    bundle = transport.prepare_bundle("unused", "profile-001")
    assert len(bundle["cards"]) == 1
    filename = bundle["cards"][0]["media"][0]["filename"]
    assert bundle["media"][filename] == source.bytes
    assert source.calls == ["status", "catalog", "card", "media", "catalog"]


def test_download_failure_does_not_return_partial_bundle(source):
    source.media = lambda *args: (_ for _ in ()).throw(PluginError(None, "connection_failed", "failed"))
    with pytest.raises(PluginError, match="failed"):
        transport.prepare_bundle("unused", "profile-001")


def test_truncated_image_is_rejected(source):
    source.bytes = source.bytes[:-10]
    with pytest.raises(PluginError, match="图片下载不完整"):
        transport.prepare_bundle("unused", "profile-001")


def test_modified_content_without_hash_is_rejected(source):
    source.detail["fields"]["Answer"] += "tamper"
    with pytest.raises(PluginError, match="版本不一致"):
        transport.prepare_bundle("unused", "profile-001")
    assert "media" not in source.calls


@pytest.mark.parametrize("mutation", [
    lambda card: card.update(field_order=["Answer", "Question"]),
    lambda card: card["fields"].update(NoteID="other-card"),
    lambda card: card["fields"].update(Answer="two\x1ffields"),
    lambda card: card["model"].update(id=123),
    lambda card: card["model"].update(type="cloze"),
    lambda card: card["model"]["templates"].append(dict(card["model"]["templates"][0])),
    lambda card: card["media"][0].update(filename="../../outside.png"),
    lambda card: card["media"][0].update(size_bytes=6 * 1024 * 1024),
    lambda card: card["media"][0].update(sha256="invalid"),
    lambda card: card.update(tags=["two tags"]),
])
def test_incompatible_payload_rejected_before_download(source, mutation):
    mutation(source.detail)
    rehash(source)
    with pytest.raises(PluginError):
        transport.prepare_bundle("unused", "profile-001")
    assert "media" not in source.calls


def test_changed_approval_during_download_aborts(source):
    original = source.media

    def withdraw(*args):
        result = original(*args)
        source.catalog_value["cards"] = []
        source.catalog_value["snapshot_id"] = transport._digest({k: source.catalog_value[k] for k in ("protocol_version", "complete", "cards", "excluded")})
        return result

    source.media = withdraw
    with pytest.raises(PluginError, match="审核状态发生变化"):
        transport.prepare_bundle("unused", "profile-001")


def test_duplicate_manifest_entries_fail(source):
    source.catalog_value["cards"] *= 2
    with pytest.raises(PluginError, match="重复"):
        transport.prepare_bundle("unused", "profile-001")


def test_an_individual_card_is_still_transferred_when_batch_target_is_small(source, monkeypatch):
    monkeypatch.setattr(transport, "MAX_BATCH_MEDIA", 1)
    bundle = transport.SyncSession("unused", "profile-001").next_bundle()
    assert len(bundle["cards"]) == 1 and bundle["media"]


def test_verified_local_media_is_not_downloaded(source):
    asset = source.detail["media"][0]
    bundle = transport.SyncSession("unused", "profile-001").next_bundle({"media": {asset["filename"]: asset["sha256"]}})
    assert bundle["cards"] and bundle["media"] == {} and "media" not in source.calls


def test_unchanged_notes_are_not_downloaded(source):
    bundle = transport.SyncSession("unused", "profile-001").next_bundle({"unchanged_ids": [source.detail["id"]]})
    assert bundle["skipped_unchanged"] == 1 and bundle["cards"] == []
    assert "card" not in source.calls and "media" not in source.calls


def populate(source, count, media_bytes=None):
    details, images = {}, {}
    for index in range(count):
        card = copy.deepcopy(source.detail)
        card["id"], card["guid"] = f"bulk-{index}", f"bulk-guid-{index}"
        card["fields"]["NoteID"] = card["id"]
        if media_bytes is not None:
            asset = card["media"][0]
            asset["id"] = "img-" + f"{index:032x}"
            asset["filename"] = "ankisense-" + asset["id"] + ".png"
            asset["size_bytes"] = len(media_bytes)
            asset["sha256"] = hashlib.sha256(media_bytes).hexdigest()
            images[asset["id"]] = media_bytes
        else:
            card["media"] = []
        card["content_hash"] = transport._digest({key: value for key, value in card.items() if key != "content_hash"})
        details[card["id"]] = card
    source.catalog_value["cards"] = [{key: copy.deepcopy(card[key]) for key in
        ("id", "version", "guid", "content_hash", "media")} for card in details.values()]
    source.catalog_value["snapshot_id"] = transport._digest({key: source.catalog_value[key]
        for key in ("protocol_version", "complete", "cards", "excluded")})
    source.card = lambda identifier, expected_hash=None: copy.deepcopy(details[identifier])
    source.media = lambda identifier, digest: images[identifier]


def test_more_than_500_cards_are_partitioned_without_loss(source):
    populate(source, 503)
    session = transport.SyncSession("unused", "profile-001")
    identifiers = []
    while session.has_more:
        bundle = session.next_bundle()
        assert len(bundle["cards"]) <= transport.MAX_CARDS
        identifiers.extend(card["id"] for card in bundle["cards"])
        assert len(bundle["catalog_ids"]) == 503
    assert len(identifiers) == len(set(identifiers)) == 503


def test_more_than_64_mib_of_images_is_partitioned(source):
    output = io.BytesIO()
    with Image.frombytes("RGB", (1200, 1100), random.Random(64).randbytes(1200 * 1100 * 3)) as image:
        image.save(output, "PNG")
    png = normalize_image(output.getvalue())[0]
    populate(source, 18, png)
    session = transport.SyncSession("unused", "profile-001")
    total, cards, batches = 0, 0, 0
    while session.has_more:
        bundle = session.next_bundle()
        size = sum(len(content) for content in bundle["media"].values())
        assert size <= transport.MAX_BATCH_MEDIA
        total += size
        cards += len(bundle["cards"])
        batches += 1
    assert total > 64 * 1024 * 1024 and cards == 18 and batches > 1


def test_scoped_session_preserves_selection_for_final_validation(source):
    scopes = []
    original = source.catalog
    def catalog(document_id=None):
        scopes.append(document_id)
        return original()
    source.catalog = catalog
    session = transport.SyncSession("unused", "profile-001", "document-one")
    bundle = session.next_bundle()
    assert scopes == ["document-one", "document-one"]
    assert bundle["scope_document_id"] == "document-one"


def test_vendored_client_matches_tested_example():
    root = Path(__file__).resolve().parents[1]
    assert (root / "examples/anki_plugin_client.py").read_bytes() == (root / "addons/ankisense_sync/client.py").read_bytes()
