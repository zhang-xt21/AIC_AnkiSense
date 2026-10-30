"""Offline studio fixture with a current, unconfirmed source page.

The base fixture owns and validates the isolated data directory. A pre-seeded
draft represents the result of generation; the browser mocks only that model
response, while confirmation and card approval use the actual local API.
"""

import os
import runpy
from pathlib import Path

import uvicorn

fixture = runpy.run_path(str(Path(__file__).with_name("workbench_fixture.py")))
app = fixture["app"]
store = app.state.store
card = store.get("cards", "fixture-pdf-stack")
for source in store.all("source_pages"):
    if source["document_id"] == card["document_id"] and source["page"] == 1:
        store.put("source_pages", {**source, "status": "pending", "reviewed_at": None})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ANKISENSE_UI_PORT", "8976")), log_level="warning")
