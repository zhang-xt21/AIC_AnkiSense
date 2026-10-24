"""One offline journey from a lecture page to an Anki-ready illustrated card."""

import io
import zipfile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from ankisense.llm import Settings
from ankisense.plugin_api import PREFIX
from ankisense.schemas import CardUpdate, DraftBatch


def lecture_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (A stack removes the last inserted item first, called LIFO.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_proofread_pdf_generates_reviewed_illustrated_card_for_anki(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app

    app = create_app(data_dir=tmp_path / "flow", settings=Settings(mode="demo"))
    app.state.plugin.connection.configure()
    connection = app.state.plugin.connection.read()
    headers = {"Authorization": "Bearer " + connection["token"], "X-AnkiSense-Client": "flow-profile-001"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        imported = client.post("/api/documents", files={"file": ("stack.pdf", lecture_pdf(), "application/pdf")},
                               data={"mode": "text", "start_page": "1", "end_page": "1"})
        assert imported.status_code == 200, imported.text
        document = imported.json()
        page = next(item for item in client.get("/api/state").json()["source_pages"] if item["document_id"] == document["id"])
        assert "LIFO" in page["text"]
        corrected = client.put(f"/api/documents/{document['id']}/pages/1", json={"revision": page["revision"], "text": page["text"]})
        assert corrected.status_code == 200, corrected.text
        block = next(item for item in client.get("/api/state").json()["blocks"]
                     if item["block_id"] == corrected.json()["block_ids"][0])

        async def generate(selected, count):
            assert count == 1 and [item.block_id for item in selected] == [block["block_id"]]
            return DraftBatch.model_validate({"cards": [{
                "question": "Which item leaves a stack first?",
                "answer": "The last inserted item leaves first (LIFO).",
                "concept": "Stack",
                "rubric": [{"point_id": "lifo", "text": "The last inserted item leaves first."}],
                "evidence": [{"block_id": block["block_id"], "quote": "last inserted item first"}],
            }]})

        monkeypatch.setattr(app.state.ai, "generate_drafts", generate)
        draft_response = client.post("/api/card-drafts", json={"block_ids": [block["block_id"]], "count": 1})
        assert draft_response.status_code == 200, draft_response.text
        draft = draft_response.json()["cards"][0]
        assert draft["status"] == "draft"
        assert client.get(PREFIX + "/cards", headers=headers).json()["cards"] == []

        picture = client.post("/api/card-media/from-page", json={"document_id": document["id"], "page": 1,
                                                              "revision": corrected.json()["revision"]})
        assert picture.status_code == 200, picture.text
        body = {key: draft[key] for key in CardUpdate.model_fields if key in draft}
        body.update(status="approved", images=[{"media_id": picture.json()["id"], "side": "answer",
                                                "caption": "Lecture page 1"}], expected_version=draft["version"])
        approved_response = client.put(f"/api/cards/{draft['id']}", json=body)
        assert approved_response.status_code == 200, approved_response.text
        approved = approved_response.json()
        assert approved["status"] == "approved"

        catalog = client.get(PREFIX + "/cards", headers=headers).json()["cards"]
        assert len(catalog) == 1 and catalog[0]["id"] == draft["id"]
        detail = client.get(PREFIX + f"/cards/{draft['id']}",
                            params={"expected_hash": catalog[0]["content_hash"]}, headers=headers)
        assert detail.status_code == 200, detail.text
        assert "<img" in detail.json()["fields"]["Answer"]
        assert "<img" not in detail.json()["fields"]["Question"]
        assert client.get("/api/export").status_code == 200
        media_export = client.get("/api/export/media")
        assert media_export.status_code == 200
        with zipfile.ZipFile(io.BytesIO(media_export.content)) as archive:
            assert any(name.endswith(".png") for name in archive.namelist())
        package = client.get("/api/export/apkg")
        assert package.status_code == 200 and package.content.startswith(b"PK")

        attempt = client.post(PREFIX + "/attempts", headers=headers, json={
            "card_id": draft["id"], "card_version": approved["version"],
            "answer": "The last inserted item leaves first.", "request_id": "flow-attempt-001"})
        assert attempt.status_code == 200, attempt.text
        assert attempt.json()["answer"] == "The last inserted item leaves first."
