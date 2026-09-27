"""Real APKG contents, media safety, identities and consistent export snapshots."""

import importlib
import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ankisense.llm import Settings
from ankisense.schemas import CardUpdate


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    module = importlib.import_module("ankisense.app")
    app = module.create_app(data_dir=tmp_path / "test-app", settings=Settings(mode="demo"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as instance:
        yield instance


def seed(client):
    assert client.post("/api/demo").status_code == 200
    return client.app.state.store.all("cards")


def change(client, card, **changes):
    body = {key: card[key] for key in CardUpdate.model_fields if key in card}
    body.update(changes)
    response = client.put(f"/api/cards/{card['id']}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, color="green"):
    output = io.BytesIO()
    with Image.new("RGB", (40, 20), color) as picture:
        picture.save(output, "PNG")
    response = client.post("/api/card-media", files={"file": ("image.png", output.getvalue(), "image/png")})
    assert response.status_code == 200, response.text
    return response.json()


def inspect_package(response, tmp_path, filename="inspection.sqlite3"):
    assert response.status_code == 200, response.text if response.status_code != 200 else ""
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="ankisense-approved.apkg"'
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    database = tmp_path / filename
    database.write_bytes(archive.read("collection.anki2"))
    connection = sqlite3.connect(database)
    try:
        notes = connection.execute("SELECT guid, mid, flds, tags FROM notes ORDER BY id").fetchall()
        cards = connection.execute("SELECT nid, did, ord, type, queue, reps FROM cards ORDER BY id").fetchall()
        models, decks = connection.execute("SELECT models, decks FROM col").fetchone()
    finally:
        connection.close()
    return archive, [(guid, mid, fields.split("\x1f"), tags) for guid, mid, fields, tags in notes], cards, json.loads(models), json.loads(decks)


def test_empty_export_is_actionable_and_only_approved_cards_are_packaged(client, tmp_path):
    assert client.get("/api/export/apkg").status_code == 422
    cards = seed(client)
    assert "批准" in client.get("/api/export/apkg").json()["detail"]
    approved = change(client, cards[0], status="approved")
    change(client, cards[1], status="rejected")
    archive, notes, generated_cards, models, decks = inspect_package(client.get("/api/export/apkg"), tmp_path)
    assert len(notes) == len(generated_cards) == 1
    assert notes[0][2] == [approved["id"], approved["question"], approved["answer"], approved["concept"],
                           "自编演示讲义.txt", "", "\n".join(e["quote"] for e in approved["evidence"]).replace("\n", "<br>"),
                           f"http://127.0.0.1:8765/?card={approved['id']}"]
    assert "自编演示" in notes[0][3]
    assert generated_cards[0][2:] == (0, 0, 0, 0)
    assert json.loads(archive.read("media")) == {}
    model = models[str(notes[0][1])]
    assert [field["name"] for field in model["flds"]] == ["NoteID", "Question", "Answer", "Concept", "SourceFile", "PDFPage", "SourceQuote", "PracticeURL"]
    assert len(model["tmpls"]) == 1
    assert "{{Answer}}" not in model["tmpls"][0]["qfmt"]
    assert "{{SourceQuote}}" not in model["tmpls"][0]["qfmt"]
    assert "{{Answer}}" in model["tmpls"][0]["afmt"]
    assert "nightMode" in model["css"] and "@media" in model["css"]
    assert any(deck["name"] == "AnkiSense" for deck in decks.values())


def test_packaged_media_keeps_sides_and_all_user_text_is_inert(client, tmp_path):
    cards = seed(client)
    question_media, answer_media, excluded_media = upload(client), upload(client, "red"), upload(client, "blue")
    question = '<script>alert("unsafe")</script>\n[sound:evil.mp3] [latex]unsafe[/latex] {{Answer}}'
    answer = '<img src="https://example.com/track" onerror="evil()"> & answer'
    caption = '<a href="javascript:evil()">caption</a>'
    card = change(client, cards[0], status="approved", question=question, answer=answer,
                  concept="<concept>", images=[
                      {"media_id": question_media["id"], "side": "question", "caption": caption},
                      {"media_id": answer_media["id"], "side": "answer", "caption": "答案配图"}])
    change(client, cards[1], images=[{"media_id": excluded_media["id"]}])
    archive, notes, *_ = inspect_package(client.get("/api/export/apkg"), tmp_path)
    fields = notes[0][2]
    assert len(fields) == 8
    assert "<script>" not in fields[1] and "&lt;script&gt;" in fields[1]
    assert "[sound:" not in fields[1] and "&#91;sound:" in fields[1]
    assert "[latex]" not in fields[1]
    assert '<a href="javascript:' not in fields[1] and "&lt;a href=" in fields[1]
    assert '<img src="https://' not in fields[2]
    assert fields[3] == "&lt;concept&gt;"
    assert question_media["filename"] in fields[1] and question_media["filename"] not in fields[2]
    assert answer_media["filename"] in fields[2] and answer_media["filename"] not in fields[1]
    manifest = json.loads(archive.read("media"))
    assert set(manifest.values()) == {question_media["filename"], answer_media["filename"]}
    for index, filename in manifest.items():
        original = question_media if filename == question_media["filename"] else answer_media
        assert archive.read(index) == client.get(original["url"]).content
    assert set(archive.namelist()) == {"collection.anki2", "media", "0", "1"}


def test_editing_and_reexport_preserves_note_model_and_deck_identity(client, tmp_path):
    card = change(client, seed(client)[0], status="approved")
    _, original, original_cards, original_models, _ = inspect_package(client.get("/api/export/apkg"), tmp_path)
    change(client, card, question="修改题目仍是同一张卡片？", answer="仍是同一张卡片。")
    _, changed, changed_cards, changed_models, _ = inspect_package(client.get("/api/export/apkg"), tmp_path, "changed.sqlite3")
    assert original[0][:2] == changed[0][:2]
    assert original[0][2][0] == changed[0][2][0]
    assert original[0][2][1:3] != changed[0][2][1:3]
    assert original_cards[0][1] == changed_cards[0][1]
    assert original_models.keys() == changed_models.keys()


@pytest.mark.parametrize("damage,status", [("missing", 404), ("not-png", 422), ("truncated", 422), ("oversized", 413)])
def test_damaged_or_missing_media_fails_without_a_partial_deck(client, damage, status):
    card = seed(client)[0]
    media = upload(client)
    change(client, card, status="approved", images=[{"media_id": media["id"]}])
    path = client.app.state.store.path.parent / "media" / f"{media['id']}.png"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes({"not-png": b"not a png", "truncated": b"\x89PNG\r\n\x1a\nxxx", "oversized": b"x" * (5 * 1024 * 1024 + 1)}[damage])
    response = client.get("/api/export/apkg")
    assert response.status_code == status, response.text
    assert "detail" in response.json()
    assert "content-disposition" not in response.headers


def test_apkg_has_an_aggregate_limit(client, monkeypatch):
    from ankisense import apkg
    change(client, seed(client)[0], status="approved")
    monkeypatch.setattr(apkg, "MAX_EXPORT_BYTES", 100)
    response = client.get("/api/export/apkg")
    assert response.status_code == 413


@pytest.mark.parametrize("state", ["needs_review", "old_revision", "source_outdated"])
def test_export_refuses_unreviewed_or_stale_evidence_even_if_card_is_approved(client, state):
    from ankisense.sources import page_id
    card = change(client, seed(client)[0], status="approved")
    store = client.app.state.store
    block_id = card["evidence"][0]["block_id"]
    block = store.get("blocks", block_id)
    if state == "source_outdated":
        store.put("cards", {**card, "source_outdated": True})
    else:
        store.put("source_pages", {"id": page_id(card["document_id"], block["page"]),
                  "status": "needs_review" if state == "needs_review" else "reviewed",
                  "block_ids": [block_id] if state == "needs_review" else ["other-current-block"]})
    assert client.get("/api/export/apkg").status_code == 409


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://remote.example/?card=demo", "http://localhost@evil.example/?card=demo", "http://localhost/?card=other", "http://localhost/?card=demo&next=evil", 'http://localhost/?card=demo" onclick="evil()', "http://localhost:99999/?card=demo"])
def test_template_link_accepts_only_matching_local_practice_urls(url):
    from fastapi import HTTPException
    from ankisense.apkg import _practice_url
    with pytest.raises(HTTPException) as error:
        _practice_url(url, "demo")
    assert error.value.status_code == 422


def test_parallel_exports_are_independent_and_do_not_leak_tempfiles(client, tmp_path, monkeypatch):
    import tempfile
    from ankisense import apkg
    card = change(client, seed(client)[0], status="approved")
    temporary_root = tmp_path / "export-temp"
    temporary_root.mkdir()
    monkeypatch.setattr(apkg, "TemporaryDirectory", lambda **options: tempfile.TemporaryDirectory(dir=temporary_root, **options))
    before = client.app.state.store.all("cards")
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: client.get("/api/export/apkg"), range(3)))
    for index, response in enumerate(responses):
        _, notes, *_ = inspect_package(response, tmp_path, f"parallel-{index}.sqlite3")
        assert notes[0][2][0] == card["id"]
    assert list(temporary_root.iterdir()) == []
    assert client.app.state.store.all("cards") == before


def test_export_keeps_a_consistent_snapshot_when_card_is_edited(client, tmp_path, monkeypatch):
    module = importlib.import_module("ankisense.app")
    original = module.build_apkg
    card = change(client, seed(client)[0], status="approved")
    entered, release = Event(), Event()

    def delayed(rows, reader, storage):
        saved_rows = list(rows)
        entered.set()
        assert release.wait(10)
        # The export still sees the approved version selected before the edit.
        assert reader.get("cards", card["id"])["status"] == "approved"
        return original(saved_rows, reader, storage)

    monkeypatch.setattr(module, "build_apkg", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(client.get, "/api/export/apkg")
        try:
            assert entered.wait(10)
            change(client, card, status="draft", question="正在编辑的新题目")
        finally:
            release.set()
        response = result.result(timeout=10)
    _, notes, *_ = inspect_package(response, tmp_path)
    assert notes[0][2][1] == card["question"]
    assert client.app.state.store.get("cards", card["id"])["status"] == "draft"
