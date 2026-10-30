"""Local attachment safety, durable snapshots and the Anki media export contract."""

import csv
import importlib
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ankisense.llm import Settings
from ankisense.schemas import CardDraft


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    module = importlib.import_module("ankisense.app")
    app = module.create_app(data_dir=tmp_path / "images-app", settings=Settings(mode="demo"))
    with TestClient(app) as instance:
        yield instance


def state(client):
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    return response.json()


def seed(client):
    assert client.post("/api/demo").status_code == 200
    return state(client)["cards"]


def update(client, card, **changes):
    # Deliberately model an old client which does not know the images field.
    body = {name: card[name] for name in CardDraft.model_fields}
    body["status"] = card["status"]
    body.update(changes)
    return client.put(f"/api/cards/{card['id']}", json=body)


def picture(format="PNG", size=(60, 40), metadata=False):
    output = io.BytesIO()
    with Image.new("RGB", size, "#68a0de") as image:
        options = {}
        if metadata and format == "PNG":
            info = PngImagePlugin.PngInfo()
            info.add_text("Comment", "private original metadata")
            options["pnginfo"] = info
        if metadata and format == "JPEG":
            exif = Image.Exif()
            exif[0x0112] = 6  # EXIF rotation: normalize pixels before stripping.
            exif[0x010E] = "private original metadata"
            options["exif"] = exif
        image.save(output, format=format, **options)
    return output.getvalue()


def upload(client, data=None, filename="../../unsafe<script>.png"):
    response = client.post("/api/card-media", files={
        "file": (filename, picture() if data is None else data, "image/png")})
    assert response.status_code == 200, response.text
    return response.json()


def imported_pdf(client, text="Stacks use LIFO. The last element pushed is the first one popped."):
    writer = PdfWriter()
    for _ in range(2):
        page = writer.add_blank_page(width=240, height=200)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): DictionaryObject({
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                })})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 8 Tf 10 160 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    response = client.post("/api/documents", files={
        "file": ("source.pdf", output.getvalue(), "application/pdf")},
        data={"start_page": "2", "end_page": "2", "mode": "text"})
    assert response.status_code == 200, response.text
    document = response.json()
    page = next(page for page in state(client)["source_pages"] if page["document_id"] == document["id"])
    return document, page


def page_media(client, document, source_page, **changes):
    return client.post("/api/card-media/from-page", json={
        "document_id": document["id"], "page": source_page["page"], "revision": source_page["revision"], **changes})


def unpack_export(client):
    response = client.get("/api/export/media")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    lines = archive.read("ankisense-approved.tsv").decode("utf-8").split("\n", 3)
    assert lines[0] == "#separator:Tab"
    assert lines[1] == "#html:true"
    assert lines[2].split("\t")[0] == "#columns:NoteID"
    return archive, list(csv.reader(io.StringIO(lines[3]), delimiter="\t"))


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_upload_normalizes_allowed_content_to_private_metadata_free_png(client, format):
    media = upload(client, picture(format, metadata=True))
    assert media["filename"] == f"ankisense-{media['id']}.png"
    assert "source" not in media
    assert media["width"] == (40 if format == "JPEG" else 60)
    assert media["height"] == (60 if format == "JPEG" else 40)
    assert "unsafe" not in str(media)
    assert state(client)["media"] == [media]
    response = client.get(media["url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert b"private original metadata" not in response.content
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == "PNG"
        assert not image.info
        assert not image.getexif()
    assert client.get("/api/card-media/not-a-safe-id/file").status_code == 404


@pytest.mark.parametrize("data,status", [
    (b"not an image", 422),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 422),
    (picture("GIF"), 422),
    (picture("BMP"), 422),
    (picture()[:30], 422),
    (b"x" * (5 * 1024 * 1024 + 1), 413),
    (picture(size=(8193, 1)), 422),
    (picture(size=(4100, 4000)), 422),
], ids=["not-image", "svg", "gif", "bmp", "truncated", "bytes-limit", "side-limit", "pixels-limit"])
def test_bad_or_oversized_upload_creates_no_media(client, data, status):
    response = client.post("/api/card-media", files={"file": ("spoof.png", data, "image/png")})
    assert response.status_code == status, response.text
    assert state(client)["media"] == []
    media_dir = client.app.state.store.path.parent / "media"
    assert not media_dir.exists() or not list(media_dir.iterdir())


def test_animated_image_rejected_and_large_allowed_picture_is_downscaled(client):
    animated = io.BytesIO()
    with Image.new("RGB", (10, 10), "red") as first, Image.new("RGB", (10, 10), "blue") as second:
        first.save(animated, format="PNG", save_all=True, append_images=[second], duration=100)
    response = client.post("/api/card-media", files={"file": ("animated.png", animated.getvalue(), "image/png")})
    assert response.status_code == 422, response.text
    media = upload(client, picture(size=(3000, 2000)))
    assert max(media["width"], media["height"]) <= 2400
    assert media["width"] * media["height"] <= 4_000_000


def test_legacy_cards_omitted_and_null_images_preserve_links_explicit_empty_unlinks(client):
    card = seed(client)[0]
    assert "images" not in card
    saved = update(client, card, question="旧版客户端保存的题目")
    assert saved.status_code == 200, saved.text
    assert "images" not in saved.json()
    media = upload(client)
    saved = update(client, card, images=[{"media_id": media["id"]}], status="approved")
    assert saved.status_code == 200, saved.text
    linked = saved.json()
    assert linked["images"] == [{"media_id": media["id"], "side": "answer", "caption": ""}]
    assert linked["version"] == card["version"] + 2
    for changes in ({}, {"images": None}):
        saved = update(client, linked, **changes)
        assert saved.status_code == 200, saved.text
        assert saved.json()["images"] == linked["images"]
    removed = update(client, linked, images=[])
    assert removed.status_code == 200, removed.text
    assert removed.json()["images"] == []
    assert client.get(media["url"]).status_code == 200
    assert state(client)["media"] == [media]


def test_image_schema_and_missing_records_or_files_cannot_be_saved(client):
    card = seed(client)[0]
    media = upload(client)
    invalid = [
        ([{"media_id": "https://example.com/image.png"}], 422),
        ([{"media_id": media["id"], "url": "https://example.com"}], 422),
        ([{"media_id": media["id"], "side": "both"}], 422),
        ([{"media_id": media["id"], "caption": "x" * 501}], 422),
        ([{"media_id": media["id"]}] * 5, 422),
        ([{"media_id": "img-" + "0" * 32}], 404),
    ]
    for images, status in invalid:
        response = update(client, card, images=images, status="approved")
        assert response.status_code == status, response.text
        assert client.app.state.store.get("cards", card["id"]) == card
    path = client.app.state.store.path.parent / "media" / f"{media['id']}.png"
    path.unlink()
    assert update(client, card, images=[{"media_id": media["id"]}]).status_code == 404
    assert client.get(media["url"]).status_code == 404


def test_attempts_keep_immutable_images_after_card_edit_and_restart_without_ai_image_data(client, monkeypatch):
    card = seed(client)[0]
    original = upload(client)
    attached = [{"media_id": original["id"], "side": "answer", "caption": "原始图片说明"}]
    card = update(client, card, images=attached, status="approved").json()
    seen = []
    diagnose = client.app.state.ai.diagnose_answer

    async def text_only(model, blocks, answer):
        seen.append(model.model_dump())
        assert set(model.model_dump()) == set(CardDraft.model_fields)
        assert "images" not in model.model_dump()
        return await diagnose(model, blocks, answer)

    monkeypatch.setattr(client.app.state.ai, "diagnose_answer", text_only)
    response = client.post("/api/attempts", json={"card_id": card["id"], "answer": "后进先出", "request_id": "media-history-001"})
    assert response.status_code == 200, response.text
    historical = response.json()
    assert historical["status"] == "completed"
    assert seen
    assert historical["card_snapshot"]["images"] == attached
    replacement = upload(client, picture(size=(20, 30)))
    saved = update(client, card, images=[{"media_id": replacement["id"], "caption": "新版本图片"}])
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == card["version"] + 1
    assert update(client, saved.json(), images=[]).status_code == 200
    assert client.get(f"/api/attempts/{historical['id']}").json() == historical
    old_bytes = client.get(original["url"]).content
    module = importlib.import_module("ankisense.app")
    restarted = module.create_app(data_dir=client.app.state.store.path.parent, settings=Settings(mode="demo"))
    with TestClient(restarted) as after_restart:
        assert after_restart.get(f"/api/attempts/{historical['id']}").json() == historical
        assert after_restart.get(original["url"]).content == old_bytes
        assert len(state(after_restart)["media"]) == 2


def test_page_copy_is_immutable_and_checks_imported_page_revision_and_ownership(client):
    document, page = imported_pdf(client)
    response = page_media(client, document, page)
    assert response.status_code == 200, response.text
    media = response.json()
    assert media["source"] == {"document_id": document["id"], "page": 2, "revision": 1}
    original_png = client.get(media["url"]).content
    assert original_png.startswith(b"\x89PNG")
    card = seed(client)[0]
    assert update(client, card, images=[{"media_id": media["id"]}]).status_code == 422
    # A card whose evidence is bound to the same PDF may attach its page image.
    block = client.app.state.store.get("blocks", page["block_ids"][0])
    pdf_card = {**card, "id": "local-pdf-card", "document_id": document["id"],
                "evidence": [{"block_id": block["block_id"], "quote": "Stacks use LIFO."}]}
    client.app.state.store.put("cards", pdf_card)
    assert update(client, pdf_card, images=[{"media_id": media["id"]}]).status_code == 200
    assert page_media(client, document, page, page=1).status_code == 404
    assert page_media(client, document, page, page=3).status_code == 404
    assert page_media(client, document, page, document_id="pdf-missing").status_code == 404
    assert page_media(client, document, page, page=0).status_code == 422
    assert page_media(client, document, page, revision=2).status_code == 409
    corrected = client.put(f"/api/documents/{document['id']}/pages/2", json={
        "revision": 1, "text": page["text"] + " Another statement."})
    assert corrected.status_code == 200, corrected.text
    assert page_media(client, document, page).status_code == 409
    assert client.get(media["url"]).content == original_png
    newer = page_media(client, document, corrected.json())
    assert newer.status_code == 200, newer.text
    assert newer.json()["id"] != media["id"]
    assert newer.json()["source"]["revision"] == 2
    assert client.get(newer.json()["url"]).content == original_png
    text_document = client.post("/api/documents/text", json={"title": "纯文本", "text": "纯文本不能创建 PDF 页图片。" * 3}).json()
    assert page_media(client, text_document, page).status_code == 404


@pytest.mark.parametrize("endpoint", ["/api/export", "/api/export/media", "/api/export/apkg"])
@pytest.mark.parametrize("media_state", ["old_revision", "wrong_document", "missing_record"])
def test_all_exports_refuse_an_approved_card_with_stale_page_picture(client, endpoint, media_state):
    document, page = imported_pdf(client)
    reviewed = client.put(f"/api/documents/{document['id']}/pages/{page['page']}", json={
        "revision": page["revision"], "text": page["text"]})
    assert reviewed.status_code == 200, reviewed.text
    page = reviewed.json()
    media = page_media(client, document, page).json()
    block = client.app.state.store.get("blocks", page["block_ids"][0])
    original = seed(client)[0]
    card = {**original, "id": "pdf-picture-export", "document_id": document["id"],
            "evidence": [{"block_id": block["block_id"], "quote": "Stacks use LIFO."}]}
    store = client.app.state.store
    store.put("cards", card)
    approved = update(client, card, images=[{"media_id": media["id"]}], status="approved")
    assert approved.status_code == 200, approved.text
    if media_state == "missing_record":
        with store.connect() as connection:
            connection.execute("DELETE FROM records WHERE collection='media' AND id=?", (media["id"],))
    else:
        source = {**media["source"],
                  "revision": 0 if media_state == "old_revision" else media["source"]["revision"],
                  "document_id": "other-document" if media_state == "wrong_document" else document["id"]}
        store.put("media", {**media, "source": source})
    response = client.get(endpoint)
    assert response.status_code == 409, response.text
    assert "配图" in response.json()["detail"]


def test_source_changed_during_page_copy_discards_uncommitted_image(client, monkeypatch):
    module = importlib.import_module("ankisense.app")
    document, page = imported_pdf(client)
    entered, release = Event(), Event()

    def render(content, page_number):
        entered.set()
        assert release.wait(10)
        return picture()

    monkeypatch.setattr(module, "render_page_png", render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(page_media, client, document, page)
        try:
            assert entered.wait(10)
            changed = client.put(f"/api/documents/{document['id']}/pages/2", json={
                "revision": 1, "text": page["text"] + " Changed during rendering."})
            assert changed.status_code == 200, changed.text
        finally:
            release.set()
        assert result.result(timeout=10).status_code == 409
    assert state(client)["media"] == []
    media_dir = client.app.state.store.path.parent / "media"
    assert not list(media_dir.iterdir())


def test_media_export_escapes_all_text_places_sides_and_copies_only_approved_assets(client):
    cards = seed(client)
    question_image = upload(client)
    answer_image = upload(client)
    unapproved_image = upload(client)
    question = '<script>alert("question")</script>\nQuestion & text'
    answer = '<img src="https://example.com/track" onerror="alert(1)">\nAnswer'
    caption = '<b onclick="bad()">label & "quote"</b>'
    approved = update(client, cards[0], status="approved", question=question, answer=answer,
        concept="<concept>&", images=[
            {"media_id": question_image["id"], "side": "question", "caption": caption},
            {"media_id": answer_image["id"], "side": "answer", "caption": "答案图"}])
    assert approved.status_code == 200, approved.text
    assert update(client, cards[1], images=[{"media_id": unapproved_image["id"]}]).status_code == 200
    archive, rows = unpack_export(client)
    assert len(rows) == 1
    row = rows[0]
    assert len(row) == 8
    assert row[0] == cards[0]["id"]
    assert '<script>' not in row[1]
    assert '&lt;script&gt;' in row[1]
    assert '&amp; text' in row[1]
    assert '<br>' in row[1]
    assert '<b onclick=' not in row[1]
    assert '&lt;b onclick=&quot;bad()&quot;&gt;' in row[1]
    assert f'<img src="{question_image["filename"]}"' in row[1]
    assert answer_image["filename"] not in row[1]
    assert f'<img src="{answer_image["filename"]}"' in row[2]
    assert question_image["filename"] not in row[2]
    assert '<img src="https://' not in row[2]
    assert row[3] == "&lt;concept&gt;&amp;"
    assert set(archive.namelist()) == {"ankisense-approved.tsv", "导入说明.txt",
        f'media/{question_image["filename"]}', f'media/{answer_image["filename"]}'}
    for media in (question_image, answer_image):
        assert archive.read(f'media/{media["filename"]}') == client.get(media["url"]).content
    instructions = archive.read("导入说明.txt").decode("utf-8")
    assert "collection.media" in instructions
    assert "HTML" in instructions and "NoteID" in instructions
    pure_text = client.get("/api/export").text.split("\n", 3)
    assert pure_text[1] == "#html:false"
    old_rows = list(csv.reader(io.StringIO(pure_text[3]), delimiter="\t"))
    assert old_rows[0][0] == row[0]
    assert old_rows[0][1:3] == [question, answer]
    _, repeated_rows = unpack_export(client)
    assert repeated_rows == rows


def test_media_export_handles_old_text_only_cards_and_empty_selection(client):
    card = seed(client)[0]
    archive, rows = unpack_export(client)
    assert rows == []
    assert len(archive.namelist()) == 2
    assert update(client, card, status="approved").status_code == 200
    archive, rows = unpack_export(client)
    assert rows[0][0] == card["id"]
    assert rows[0][1:3] == [card["question"], card["answer"]]
    assert len(rows[0]) == 8
    assert len(archive.namelist()) == 2


def test_export_rejects_missing_assets(client):
    card = seed(client)[0]
    media = upload(client)
    assert update(client, card, status="approved", images=[{"media_id": media["id"]}]).status_code == 200
    assert client.get("/api/export/media").status_code == 200
    (client.app.state.store.path.parent / "media" / f"{media['id']}.png").unlink()
    assert client.get("/api/export/media").status_code == 404
