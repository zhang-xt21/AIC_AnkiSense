import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ankisense.card_deletion import delete_cards
from ankisense.db import Store


def seeded(tmp_path):
    store = Store(tmp_path / "delete.sqlite3")
    store.put_many([("documents", {"id": "d1"}), ("documents", {"id": "d2"}),
                    ("cards", {"id": "c1", "document_id": "d1"}),
                    ("cards", {"id": "c2", "document_id": "d1"}),
                    ("cards", {"id": "c3", "document_id": "d2"}),
                    ("attempts", {"id": "a1", "card_id": "c1", "status": "completed", "request_id": "r1"}),
                    ("card_auto_reviews", {"id": "v1", "card_id": "c1", "status": "completed"}),
                    ("auto_card_jobs", {"id": "j1", "document_id": "d1", "status": "completed", "card_ids": ["c1", "c2"]}),
                    ("media", {"id": "shared-picture"})])
    return store


def test_single_delete_is_scoped_and_late_writes_cannot_restore_it(tmp_path):
    store = seeded(tmp_path)
    result = delete_cards(store, card_id="c1")
    assert result["deleted_ids"] == ["c1"]
    assert store.get("cards", "c1") is None
    assert store.get("attempts", "a1") is None
    assert store.get("card_auto_reviews", "v1") is None
    assert store.get("documents", "d1") and store.get("media", "shared-picture")
    assert store.get("cards", "c2") and store.get("cards", "c3")
    store.put("cards", {"id": "c1", "document_id": "d1"})
    store.put("attempts", {"id": "a2", "card_id": "c1", "request_id": "r2"})
    store.put("auto_card_jobs", {"id": "j1", "status": "completed", "card_ids": ["c1", "c2"]})
    assert store.get("cards", "c1") is None and store.get("attempts", "a2") is None
    assert store.get("auto_card_jobs", "j1")["card_ids"] == ["c2"]


def test_bulk_requires_current_count_and_preserves_lecture_and_other_cards(tmp_path):
    store = seeded(tmp_path)
    with pytest.raises(HTTPException) as error:
        delete_cards(store, document_id="d1", expected_count=1)
    assert error.value.status_code == 409 and len(store.all("cards")) == 3
    assert delete_cards(store, document_id="d1", expected_count=2)["deleted_count"] == 2
    assert [card["id"] for card in store.all("cards")] == ["c3"]
    assert store.get("documents", "d1")


@pytest.mark.parametrize("collection,record", [
    ("auto_card_jobs", {"id": "running", "document_id": "d1", "status": "running"}),
    ("card_auto_reviews", {"id": "running", "card_id": "c1", "status": "pending"}),
])
def test_active_work_blocks_deletion_atomically(tmp_path, collection, record):
    store = seeded(tmp_path)
    store.put(collection, record)
    with pytest.raises(HTTPException) as error:
        delete_cards(store, document_id="d1", expected_count=2)
    assert error.value.status_code == 409
    assert len(store.all("cards")) == 3 and not store.all("deleted_cards")


def test_delete_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app
    from ankisense.llm import Settings
    app = create_app(data_dir=tmp_path / "api", settings=Settings(mode="demo"))
    with TestClient(app) as client:
        store = app.state.store
        store.put("documents", {"id": "d1"})
        store.put("cards", {"id": "c1", "document_id": "d1"})
        assert client.delete("/api/documents/d1/cards?expected_count=2").status_code == 409
        assert client.delete("/api/cards/c1").json()["deleted_count"] == 1
        assert client.delete("/api/cards/c1").status_code == 404
        assert client.delete("/api/documents/missing/cards?expected_count=0").status_code == 404
        assert client.delete("/api/documents/d1/cards?expected_count=0").json()["deleted_count"] == 0
        assert client.delete("/api/documents/d1?expected_count=0").json()["deleted_document_id"] == "d1"
        assert store.get("documents", "d1") is None


def test_material_deletion_removes_records_and_does_not_affect_other_material(tmp_path):
    store = seeded(tmp_path)
    store.put_many([("material_imports", {"id": "i1", "document_id": "d1", "job_id": "j1", "status": "completed"}),
                    ("source_pages", {"id": "d1-p1", "document_id": "d1", "status": "pending"}),
                    ("blocks", {"block_id": "b1", "document_id": "d1"}),
                    ("material_imports", {"id": "i2", "document_id": "d2", "status": "completed"})])
    result = delete_cards(store,document_id="d1",expected_count=2,remove_document=True)
    assert result["deleted_document_id"] == "d1" and result["deleted_count"] == 2
    for collection, identifier in [("documents","d1"),("material_imports","i1"),("auto_card_jobs","j1"),
                                   ("source_pages","d1-p1"),("blocks","b1"),("attempts","a1")]:
        assert store.get(collection, identifier) is None
    assert store.get("documents","d2") and store.get("cards","c3") and store.get("material_imports","i2")


def test_zero_card_material_can_be_removed_but_not_during_generation(tmp_path):
    store = seeded(tmp_path)
    delete_cards(store,document_id="d1",expected_count=2)
    store.put("auto_card_jobs", {"id":"j1","document_id":"d1","status":"running","card_ids":[]})
    with pytest.raises(HTTPException) as error:
        delete_cards(store,document_id="d1",expected_count=0,remove_document=True)
    assert error.value.status_code == 409 and store.get("documents","d1")
    store.put("auto_card_jobs", {"id":"j1","document_id":"d1","status":"completed","card_ids":[]})
    assert delete_cards(store,document_id="d1",expected_count=0,remove_document=True)["deleted_document_id"] == "d1"
