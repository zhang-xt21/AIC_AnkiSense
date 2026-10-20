"""Bounded live evaluation. Reads saved model settings without printing secrets."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from ankisense.ingest import parse_pdf_document
from ankisense.llm import Settings
from ankisense.model_settings import ModelSettingsFile
from ankisense.sources import page_records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--settings-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=int, default=4)
    parser.add_argument("--end", type=int, default=8)
    parser.add_argument("--max-cards", type=int, default=12)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    settings = ModelSettingsFile(args.settings_dir).load(Settings.from_env())
    os.environ["ANKISENSE_DATA_DIR"] = str(args.data_dir.resolve())
    from ankisense.app import create_app
    app = create_app(args.data_dir, settings)
    content = args.file.read_bytes()
    parsed = parse_pdf_document(content, args.start, args.end)
    identifier = parsed["id"]
    document = {"id": identifier, "title": args.file.stem, "filename": args.file.name, "is_demo": False,
                "file_url": f"/api/documents/{identifier}/file", "page_count": parsed["total"], "automatic_import": True}
    uploads = args.data_dir / "uploads"
    uploads.mkdir(exist_ok=True)
    (uploads / f"{identifier}.pdf").write_bytes(content)
    records = [("documents", document)]
    for page in parsed["pages"]:
        records.extend(page_records(identifier, page))
    app.state.store.put_many(records)
    started = time.monotonic()
    with TestClient(app, base_url="http://127.0.0.1:8798") as client:
        response = client.post(f"/api/documents/{identifier}/auto-card-jobs", json={"autonomous": True, "max_cards": args.max_cards, "image_mode": "source_page"})
        response.raise_for_status()
        job = response.json()
        previous = None
        while job["status"] in {"pending", "running"}:
            marker = [job["stage"], job["progress"], len(job["card_ids"])]
            if marker != previous:
                print(json.dumps({"stage": job["stage"], "progress": job["progress"], "cards": len(job["card_ids"])}, ensure_ascii=False), flush=True)
                previous = marker
            time.sleep(1)
            job = client.get(f"/api/auto-card-jobs/{job['id']}").json()
        cards = [c for c in app.state.store.all("cards") if c.get("auto_job_id") == job["id"]]
        exported = client.get("/api/export/apkg")
        if exported.status_code == 200:
            (args.data_dir / "cards.apkg").write_bytes(exported.content)
        report = {"model": settings.model, "input_file": str(args.file), "pages": [args.start, args.end], "seconds": round(time.monotonic() - started, 1),
                  "job": job, "card_count": len(cards), "ready_count": sum(c["status"] == "approved" for c in cards), "cards": cards,
                  "export_status": exported.status_code}
        (args.data_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report[k] for k in ("model", "seconds", "card_count", "ready_count", "export_status")}, ensure_ascii=False), flush=True)
        if not cards or job["status"] != "completed" or exported.status_code != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
