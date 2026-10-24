"""Isolated, offline server for the visual workbench browser checks.

The runner supplies a fresh directory beneath this folder. Production data,
personal Anki collections, .env credentials and cloud services are not used.
"""

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT / ".ui-checks" / "20260927-workbench"
DATA = Path(os.environ.get("ANKISENSE_UI_DATA", str(OUTPUT / "data"))).resolve()
if not DATA.is_relative_to(OUTPUT):
    raise RuntimeError("UI fixture data must stay inside the isolated check folder")
HERE = DATA.parent
HERE.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT))


def prepare_image():
    from PIL import Image, ImageDraw

    path = HERE / "stack-fixture.png"
    image = Image.new("RGB", (640, 360), "#f6f8f3")
    draw = ImageDraw.Draw(image)
    draw.text((32, 20), "WORKBENCH TEST FIXTURE - STACK / LIFO", fill="#254c3b")
    for y, label in [(92, "C - top"), (156, "B"), (220, "A")]:
        draw.rounded_rectangle((115, y, 365, y + 48), radius=8, fill="#d4e7d8", outline="#397457", width=3)
        draw.text((140, y + 17), label, fill="#173727")
    draw.line((460, 120, 390, 120), fill="#397457", width=6)
    draw.polygon([(387, 120), (406, 108), (406, 132)], fill="#397457")
    draw.text((430, 145), "pop first", fill="#173727")
    image.save(path)
    return path


prepare_image()
if "--prepare-only" in sys.argv:
    print("Prepared offline stack-fixture.png")
    raise SystemExit(0)

os.environ["ANKISENSE_DATA_DIR"] = str(DATA)
os.environ.update(AI_MODE="demo", AI_API_KEY="", AI_BASE_URL="", AI_MODEL="")

from ankisense.llm import AIService, Settings

# app.py constructs an ASGI app during import. Replace its environment reader
# first, so even the module-level app never reads the user's .env file.
Settings.from_env = classmethod(lambda cls: cls(mode="demo"))


async def deny_cloud(*args, **kwargs):
    raise RuntimeError("Cloud model calls are disabled in the UI test fixture")


AIService._request = deny_cloud
from ankisense.app import create_app

app = create_app(data_dir=DATA, settings=Settings(mode="demo"))


def seed_pdf_card():
    """Create actual PDF page images without OCR, Docling or any cloud model."""
    from datetime import datetime, timezone
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    from fastapi.testclient import TestClient

    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    picture = Image.open(HERE / "stack-fixture.png").convert("RGB")
    xobject = DecodedStreamObject()
    xobject.set_data(picture.tobytes())
    xobject.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(picture.width), NameObject("/Height"): NumberObject(picture.height), NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    picture_ref = writer._add_object(xobject.flate_encode())
    lines = [
        ["WORKBENCH PDF FIXTURE - STACK BASICS", "A stack removes the most recently inserted element first.", "This order is called last in, first out, or LIFO.", "The top of the stack is the insertion and removal end."],
        ["WORKBENCH PDF FIXTURE - STACK EXAMPLE", "Push A, then B, then C onto an initially empty stack.", "The first pop returns C. The next pops return B and A.", "Check whether the stack is empty before removing an item."],
    ]
    for page_lines in lines:
        page = writer.add_blank_page(width=640, height=700)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}), NameObject("/XObject"): DictionaryObject({NameObject("/Img"): picture_ref})})
        source = "BT /F1 16 Tf 40 650 Td " + " 0 -28 Td ".join(f"({line}) Tj" for line in page_lines) + " ET\nq 560 0 0 315 40 170 cm /Img Do Q"
        stream = DecodedStreamObject(); stream.set_data(source.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    path = HERE / "workbench-fixture.pdf"
    writer.write(path)
    with TestClient(app) as client:
        imported = client.post("/api/documents", files={"file": (path.name, path.read_bytes(), "application/pdf")}, data={"mode": "text", "start_page": "1", "end_page": "2"})
        imported.raise_for_status(); document = imported.json()
        state = client.get("/api/state").json()
        for page in state["source_pages"]:
            if page["document_id"] == document["id"]:
                response = client.put(f"/api/documents/{document['id']}/pages/{page['page']}", json={"revision": page["revision"], "text": page["text"]})
                response.raise_for_status()
        media = client.post("/api/card-media", files={"file": ("stack-fixture.png", (HERE / "stack-fixture.png").read_bytes(), "image/png")})
        media.raise_for_status()
        state = client.get("/api/state").json()
        first_page = next(page for page in state["source_pages"] if page["document_id"] == document["id"] and page["page"] == 1)
        block = next(block for block in state["blocks"] if block["block_id"] == first_page["block_ids"][0])
        card = {"id": "fixture-pdf-stack", "document_id": document["id"], "question": "How does a stack remove its elements?", "answer": "The last inserted element is removed first (LIFO).", "concept": "Stack / LIFO", "kind": "basic", "rubric": [{"point_id": "fixture_lifo", "text": "Explain last in, first out."}], "evidence": [{"block_id": block["block_id"], "quote": block["text"]}], "images": [{"media_id": media.json()["id"], "side": "answer", "caption": "Self-authored stack illustration"}], "status": "draft", "version": 1, "is_demo": False, "created_at": datetime.now(timezone.utc).isoformat()}
        # There is no public manual-card creation endpoint. This fixture card is
        # seeded only in the isolated database; all subsequent edits use the API.
        app.state.store.put("cards", card)


seed_pdf_card()
# TestClient completed the seeding application's lifespan. Construct fresh
# runtime services for Uvicorn so attempts are not left in shutdown state.
app = create_app(data_dir=DATA, settings=Settings(mode="demo"))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ANKISENSE_UI_PORT", "8976")), log_level="warning")
