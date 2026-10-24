"""All exports accept distinct valid quotations from the same source block."""

import io
import sqlite3
import zipfile

import pytest
from fastapi import HTTPException

from ankisense.plugin_sync import PluginSync
from test_card_auto_review import post, ready
from test_figure_recommendations import context, save


EXPORTS = ("/api/export", "/api/export/media", "/api/export/apkg")
QUOTES = ("栈遵循后进先出", "插入和删除在栈顶进行")
BASE = "http://127.0.0.1:8765"


def references(card):
    identifier = card["evidence"][0]["block_id"]
    return [{"block_id": identifier, "quote": quote} for quote in QUOTES]


@pytest.mark.parametrize("method", ["human", "machine"])
def test_all_exports_and_plugin_preserve_multiple_quotes_from_one_block(ready, tmp_path, method):
    client, original, service, calls = ready
    saved = save(client, original, evidence=references(original), status="approved" if method == "human" else "draft")
    assert saved.status_code == 200, saved.text
    card = saved.json()
    if method == "machine":
        review = post(client, card)
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "auto_pass"
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 200, (endpoint, response.text)
    tsv = client.get("/api/export").text
    assert all(quote in tsv for quote in QUOTES)
    package = client.get("/api/export/apkg")
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        database = tmp_path / "exported-collection.anki2"
        database.write_bytes(archive.read("collection.anki2"))
    with sqlite3.connect(database) as connection:
        fields = connection.execute("SELECT flds FROM notes").fetchone()[0].split("\x1f")
    assert all(quote in fields[6] for quote in QUOTES)
    sync = PluginSync(service.store, service.storage)
    assert [item["id"] for item in sync.catalog(BASE)["cards"]] == [card["id"]]
    exported = sync.card(card["id"], BASE)
    assert [item["quote"] for item in exported["source"]["evidence"]] == list(QUOTES)
    assert all(quote in exported["fields"]["SourceQuote"] for quote in QUOTES)
    assert len(calls) == (1 if method == "machine" else 0)


@pytest.mark.parametrize("bad_index", [0, 1])
def test_deduplicating_source_loads_does_not_skip_invalid_quote_validation(context, bad_index):
    client, card, _adapter, storage = context
    card["evidence"] = references(card)
    card["evidence"][bad_index]["quote"] = "原文中不存在的引用。"
    client.app.state.store.put("cards", card)
    for endpoint in EXPORTS:
        response = client.get(endpoint)
        assert response.status_code == 409, (endpoint, response.text)
    sync = PluginSync(client.app.state.store, storage)
    assert sync.catalog(BASE)["cards"] == []
    with pytest.raises(HTTPException) as rejected:
        sync.card(card["id"], BASE)
    assert rejected.value.status_code == 409
