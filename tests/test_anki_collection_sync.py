"""Real Anki backend tests in temporary collections, via its installed runtime.

Pytest invokes this file once in the official isolated CPython host. No tests
open a user profile, start the GUI, access a network, or call a cloud model.
"""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "addons" / "ankisense_sync" / "collection_sync.py"
BOOTSTRAP = ADAPTER.with_name("bootstrap.apkg")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF1sAAAAASUVORK5CYII=")


def load_adapter():
    spec = importlib.util.spec_from_file_location("collection_sync_under_test", ADAPTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_anki_collection_sync(tmp_path):
    import pytest
    runner = ROOT / "tools" / "anki" / "run_anki_python.ps1"
    runtime = Path(os.environ.get("ANKISENSE_ANKI_RUNTIME", ROOT / "origin" / "Anki"))
    if not (runtime / "python313.dll").is_file():
        pytest.skip("The optional installed official Anki runtime is not available")
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "A PowerShell host is needed for the official embedded runtime"
    result = subprocess.run([shell, "-NoProfile", "-File", str(runner), "-Script", str(Path(__file__).resolve()),
                             "-AnkiRoot", str(runtime),
                             "-DataDirectory", str(tmp_path / "official-anki")],
                            cwd=ROOT, capture_output=True, timeout=120,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0, output
    assert "OK" in output, output


class RealCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from anki.collection import Collection
        cls.Collection = Collection
        cls.adapter = load_adapter()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sync-test-", dir=os.environ["ANKISENSE_ANKI_TEST_ROOT"])
        self.directory = Path(self.temporary.name)
        self.col = self.Collection(str(self.directory / "collection.anki2"))
        self.deck_id = self.col.decks.id("Selected Target")
        self.state_path = self.directory / "plugin-state.json"
        with zipfile.ZipFile(BOOTSTRAP) as archive:
            database = self.directory / "fixture.sqlite3"
            database.write_bytes(archive.read("collection.anki2"))
        db = sqlite3.connect(database)
        try:
            native = json.loads(db.execute("SELECT models FROM col").fetchone()[0])[str(self.adapter.MODEL_ID)]
        finally:
            db.close()
        self.model = {"id": int(native["id"]), "name": native["name"], "type": "standard",
                      "fields": [{"name": field["name"]} for field in native["flds"]],
                      "sort_field_index": native["sortf"], "css": native["css"],
                      "templates": [{"name": item["name"], "qfmt": item["qfmt"], "afmt": item["afmt"],
                                     "ordinal": item["ord"]} for item in native["tmpls"]]}
        self.card = {"id": "fixture-card-1", "version": 1, "guid": "fixtureGUID1", "content_hash": "a" * 64,
                     "field_order": self.adapter.FIELD_ORDER,
                     "fields": dict(zip(self.adapter.FIELD_ORDER, ["fixture-card-1", "Question?", "Answer.", "Concept",
                                                                   "fixture.txt", "", "source text", "http://127.0.0.1:8765/?card=fixture-card-1"])),
                     "model": self.model, "tags": ["AnkiSense", "基础卡片"], "media": []}
        self.bundle = {"server_id": "as-fixture-one", "snapshot_id": "snapshot-1", "cards": [self.card], "excluded": [], "media": {}}

    def tearDown(self):
        self.col.close()
        self.temporary.cleanup()

    def apply(self, bundle=None):
        return self.adapter.apply_bundle(self.col, bundle or self.bundle, self.deck_id, self.state_path)

    def note(self):
        return self.col.get_note(self.col.db.scalar("SELECT id FROM notes WHERE guid=?", self.card["guid"]))

    def add_media(self, card=None, suffix="1", content=PNG):
        card = card or self.card
        filename = "ankisense-img-" + suffix * 32 + ".png"
        card["media"].append({"filename": filename, "sha256": hashlib.sha256(content).hexdigest()})
        card["fields"]["Question"] += '<img src="' + filename + '">'
        self.bundle["media"][filename] = content
        return filename

    def package(self, card=None):
        card = card or self.card
        database = self.directory / "fixture.sqlite3"
        with zipfile.ZipFile(BOOTSTRAP) as archive:
            database.write_bytes(archive.read("collection.anki2"))
        connection = sqlite3.connect(database)
        try:
            values = [card["fields"][name] for name in card["field_order"]]
            connection.execute("UPDATE notes SET guid=?,flds=?,sfld=?,csum=?", (card["guid"], "\x1f".join(values), values[1],
                                                                                 int(hashlib.sha1(values[0].encode()).hexdigest()[:8], 16)))
            connection.commit()
        finally:
            connection.close()
        package = self.directory / "existing.apkg"
        with zipfile.ZipFile(package, "w") as archive:
            archive.write(database, "collection.anki2")
            manifest = {str(index): name for index, name in enumerate(self.bundle["media"])}
            archive.writestr("media", json.dumps(manifest))
            for index, content in enumerate(self.bundle["media"].values()):
                archive.writestr(str(index), content)
        return package

    def import_package(self, path):
        from anki.import_export_pb2 import ImportAnkiPackageRequest
        return self.col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path),
                                         options=self.col._backend.get_import_anki_package_presets()))

    def schedule(self):
        return self.col.db.all("SELECT id,nid,did,type,queue,due,ivl,factor,reps,lapses,left,odue,odid,flags,data FROM cards ORDER BY id")

    def test_new_note_replay_media_and_fixed_identity(self):
        filename = self.add_media()
        result = self.apply()
        self.assertEqual((result["added"], result["updated"], result["conflicts"]), (1, 0, []))
        note = self.note()
        self.assertEqual(note.mid, 1786831001)
        self.assertEqual(note.guid, self.card["guid"])
        self.assertEqual(self.col.get_card(note.card_ids()[0]).did, self.deck_id)
        self.assertEqual((Path(self.col.media.dir()) / filename).read_bytes(), PNG)
        self.assertIn(filename, self.col.get_card(note.card_ids()[0]).question())
        self.assertFalse(self.col.db.scalar("SELECT 1 FROM notes WHERE guid=?", self.adapter.BOOTSTRAP_GUID))
        ids, schedule = (note.id, note.card_ids()), self.schedule()
        repeated = self.apply()
        self.assertEqual((repeated["added"], repeated["updated"], repeated["unchanged"]), (0, 0, 1))
        self.assertEqual((self.note().id, self.note().card_ids()), ids)
        self.assertEqual(self.schedule(), schedule)

    def test_update_preserves_real_review_history_local_tags_and_deck(self):
        self.apply()
        note = self.note()
        self.col.decks.select(self.deck_id)
        reviewing = self.col.sched.getCard()
        self.col.sched.answerCard(reviewing, 3)
        moved = self.col.get_card(note.card_ids()[0])
        moved.did = self.col.decks.id("User Moved Deck")
        self.col.update_card(moved)
        note.tags.append("user-owned")
        self.col.update_note(note)
        before, logs = self.schedule(), self.col.db.all("SELECT * FROM revlog")
        self.card["fields"]["Answer"] = "Updated answer."
        self.card["version"] = 2
        result = self.apply()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.note().id, note.id)
        self.assertEqual(self.note()["Answer"], "Updated answer.")
        self.assertIn("user-owned", self.note().tags)
        self.assertEqual(self.schedule(), before)
        self.assertEqual(self.col.db.all("SELECT * FROM revlog"), logs)
        self.assertEqual(len(logs), 1)

    def test_matching_apkg_is_adopted_without_recreation(self):
        self.import_package(self.package())
        original = self.note()
        before = self.schedule()
        result = self.apply()
        self.assertEqual((result["added"], result["unchanged"]), (0, 1))
        self.assertEqual(self.note().id, original.id)
        self.assertEqual(self.schedule(), before)
        self.card["fields"]["Answer"] = "Updated through plugin"
        self.assertEqual(self.apply()["updated"], 1)
        imported = self.import_package(self.package())
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(len(imported.log.conflicting), 0)
        self.assertEqual(self.note().mid, self.adapter.MODEL_ID)

    def test_untracked_apkg_difference_is_a_conflict(self):
        self.import_package(self.package())
        self.card["fields"]["Answer"] = "Different server answer"
        result = self.apply()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.note()["Answer"], "Answer.")

    def test_local_edits_and_local_template_changes_are_preserved(self):
        self.apply()
        note = self.note()
        note["Answer"] = "My manual answer"
        self.col.update_note(note)
        self.card["fields"]["Answer"] = "New server answer"
        self.assertEqual(len(self.apply()["conflicts"]), 1)
        self.assertEqual(self.note()["Answer"], "My manual answer")
        model = copy.deepcopy(self.col.models.get(self.adapter.MODEL_ID))
        model["css"] += "\n.user-style { color: red; }"
        self.col.models.update_dict(model)
        self.assertEqual(len(self.apply()["conflicts"]), 1)
        self.assertTrue(self.col.models.get(self.adapter.MODEL_ID)["css"].endswith(".user-style { color: red; }"))

    def test_server_presentation_update_uses_untouched_baseline(self):
        self.apply()
        before = self.schedule()
        self.card["model"]["css"] += "\n.server-style { color: green; }"
        result = self.apply()
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["unchanged"], 1)
        self.assertTrue(result["changes"].notetype)
        self.assertEqual(self.schedule(), before)

    def test_undo_new_sync_leaves_no_placeholder_and_no_automatic_recreation(self):
        self.apply()
        self.col.undo()
        self.assertEqual(self.col.note_count(), 0)
        self.assertIsNone(self.col.models.get(self.adapter.MODEL_ID))
        result = self.apply()
        self.assertEqual(result["added"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.col.note_count(), 0)

    def test_undo_update_is_not_silently_reapplied(self):
        self.apply()
        self.card["fields"]["Answer"] = "Updated answer"
        self.apply()
        self.col.undo()
        self.assertEqual(self.note()["Answer"], "Answer.")
        self.assertEqual(len(self.apply()["conflicts"]), 1)
        self.assertEqual(self.note()["Answer"], "Answer.")

    def test_state_loss_or_write_failure_recovers_conservatively(self):
        with patch.object(self.adapter, "_save_state", return_value=False):
            result = self.apply()
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["warnings"])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.apply()["unchanged"], 1)
        self.state_path.unlink()
        self.card["fields"]["Answer"] = "New remote answer"
        self.assertEqual(len(self.apply()["conflicts"]), 1)
        self.assertEqual(self.col.note_count(), 1)

    def test_corrupted_state_is_not_overwritten(self):
        self.apply()
        self.state_path.write_text("damaged mapping", encoding="utf-8")
        result = self.apply()
        self.assertEqual(result["unchanged"], 1)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "damaged mapping")

    def test_same_name_different_media_and_bad_download_are_rejected(self):
        filename = self.add_media()
        self.col.media.write_data(filename, b"user file")
        result = self.apply()
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.col.note_count(), 0)
        self.assertIsNone(self.col.models.get(self.adapter.MODEL_ID))
        self.assertEqual((Path(self.col.media.dir()) / filename).read_bytes(), b"user file")
        self.bundle["media"][filename] = b"bad download"
        self.assertEqual(len(self.apply()["conflicts"]), 1)

    def test_media_failure_does_not_create_half_a_note(self):
        self.add_media()
        with patch.object(self.col.media, "write_data", side_effect=OSError("fixture")):
            result = self.apply()
        self.assertEqual(result["added"], 0)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.col.note_count(), 0)
        self.assertFalse(self.col.db.scalar("SELECT 1 FROM notes WHERE guid=?", self.adapter.BOOTSTRAP_GUID))

    def test_partial_batch_reports_only_complete_notes(self):
        second = copy.deepcopy(self.card)
        second.update(id="fixture-card-2", guid="fixtureGUID2")
        second["fields"]["NoteID"] = second["id"]
        self.bundle["cards"].append(second)
        original = self.col.add_note
        calls = 0
        def fail_second(note, deck):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture failure")
            return original(note, deck)
        with patch.object(self.col, "add_note", side_effect=fail_second):
            result = self.apply()
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(len(json.loads(self.state_path.read_text())['servers'][self.bundle['server_id']]['cards']), 1)

    def test_error_after_atomic_commit_still_reports_and_tracks_complete_note(self):
        original = self.col.add_note
        def fail_after_commit(note, deck):
            original(note, deck)
            raise RuntimeError("third-party hook failed after commit")
        with patch.object(self.col, "add_note", side_effect=fail_after_commit):
            result = self.apply()
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.col.note_count(), 1)
        self.assertEqual(self.col.card_count(), 1)
        self.assertEqual(self.apply()["unchanged"], 1)

    def test_older_remote_version_does_not_roll_back_content(self):
        self.card["version"] = 3
        self.apply()
        self.card["version"] = 2
        self.card["fields"]["Answer"] = "Old backup answer"
        result = self.apply()
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.note()["Answer"], "Answer.")

    def test_bootstrap_ignores_user_deck_config_and_merge_import_preferences(self):
        options = self.col._backend.get_import_anki_package_presets()
        options.with_scheduling = True
        options.with_deck_configs = True
        options.merge_notetypes = True
        original = self.col.import_anki_package
        seen = []
        def checked_import(request):
            seen.append((request.options.with_scheduling, request.options.with_deck_configs, request.options.merge_notetypes))
            return original(request)
        with patch.object(self.col._backend, "get_import_anki_package_presets", return_value=options), patch.object(self.col, "import_anki_package", side_effect=checked_import):
            self.assertEqual(self.apply()["added"], 1)
        self.assertEqual(seen, [(False, False, False)])

    def test_bootstrap_failure_rolls_back_its_note_and_model(self):
        with patch.object(self.col, "remove_notes", side_effect=OSError("fixture failure")):
            with self.assertRaises(self.adapter.SyncError):
                self.apply()
        self.assertEqual(self.col.note_count(), 0)
        self.assertIsNone(self.col.models.get(self.adapter.MODEL_ID))

    def test_existing_reserved_note_is_never_removed(self):
        self.import_package(BOOTSTRAP)
        count = self.col.note_count()
        result = self.apply()
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(self.col.note_count(), count)

    def test_absence_never_deletes_and_another_server_cannot_take_ownership(self):
        self.apply()
        different = copy.deepcopy(self.bundle)
        different["server_id"] = "as-different-source"
        self.assertEqual(len(self.apply(different)["conflicts"]), 1)
        empty = {**self.bundle, "cards": [], "media": {}}
        self.assertTrue(self.apply(empty)["warnings"])
        self.assertEqual(self.col.note_count(), 1)

    def test_inventory_checks_live_notes_and_cached_images(self):
        name = "ankisense-img-" + "1" * 32 + ".png"
        asset = {"filename": name, "sha256": hashlib.sha256(PNG).hexdigest(), "size_bytes": len(PNG)}
        self.card["media"] = [asset]
        self.bundle["media"] = {name: PNG}
        self.apply()
        entry = {key: self.card[key] for key in ("id", "guid", "version", "content_hash", "media")}
        inventory = self.adapter.sync_inventory(self.col, [entry], self.bundle["server_id"], self.state_path)
        self.assertEqual(inventory["unchanged_ids"], [self.card["id"]])
        self.assertEqual(inventory["media"], {name: asset["sha256"]})
        note = self.col.get_note(self.col.find_notes("NoteID:" + self.card["id"])[0])
        note["Answer"] = "Local edit"
        self.col.update_note(note)
        self.assertEqual(self.adapter.sync_inventory(self.col, [entry], self.bundle["server_id"], self.state_path)["unchanged_ids"], [])
        (Path(self.col.media.dir()) / name).unlink()
        self.assertEqual(self.adapter.sync_inventory(self.col, [entry], self.bundle["server_id"], self.state_path)["media"], {})

    def test_one_hundred_note_writes_merge_into_one_undo_step(self):
        cards = self.batch_cards(100)
        bundle = {**self.bundle, "cards": cards}
        result = self.apply(bundle)
        self.assertEqual(result["added"], 100)
        self.assertFalse(result["conflicts"])
        self.assertFalse(result["warnings"])
        self.assertTrue(result["changes"].note)
        self.assertTrue(result["changes"].notetype)
        self.assertEqual(self.col.note_count(), 100)
        before = self.schedule()
        repeated = self.apply(bundle)
        self.assertEqual((repeated["added"], repeated["updated"], repeated["unchanged"]), (0, 0, 100))
        self.assertEqual(self.schedule(), before)
        self.col.undo()
        self.assertEqual(self.col.note_count(), 0)
        self.assertIsNone(self.col.models.get(self.adapter.MODEL_ID))

    def batch_cards(self, count):
        cards = []
        for index in range(count):
            card = copy.deepcopy(self.card)
            card.update(id=f"batch-note-{index}", guid=f"batchGUID{index}")
            card["fields"]["NoteID"] = card["id"]
            cards.append(card)
        return cards

    def test_large_batch_update_preserves_reviews_and_one_step_undo(self):
        cards = self.batch_cards(100)
        bundle = {**self.bundle, "cards": cards}
        self.apply(bundle)
        self.col.decks.select(self.deck_id)
        reviewing = self.col.sched.getCard()
        self.col.sched.answerCard(reviewing, 3)
        before, logs = self.schedule(), self.col.db.all("SELECT * FROM revlog")
        for card in cards:
            card["version"] = 2
            card["fields"]["Answer"] = "Updated answer"
        result = self.apply(bundle)
        self.assertEqual((result["added"], result["updated"], result["unchanged"]), (0, 100, 0))
        self.assertFalse(result["warnings"])
        self.assertEqual(self.schedule(), before)
        self.assertEqual(self.col.db.all("SELECT * FROM revlog"), logs)
        self.col.undo()
        self.assertEqual(self.col.note_count(), 100)
        for note_id in self.col.db.list("SELECT id FROM notes"):
            self.assertEqual(self.col.get_note(note_id)["Answer"], "Answer.")
        self.assertEqual(self.schedule(), before)
        self.assertEqual(self.col.db.all("SELECT * FROM revlog"), logs)

    def test_undo_merge_failure_reports_committed_note_and_can_resume(self):
        cards = self.batch_cards(3)
        bundle = {**self.bundle, "cards": cards}
        original = self.col.merge_undo_entries
        calls = 0
        def fail_after_first_note(target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("fixture undo history cleared")
            return original(target)
        with patch.object(self.col, "merge_undo_entries", side_effect=fail_after_first_note):
            result = self.apply(bundle)
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["stopped"])
        self.assertTrue(result["changes"].note)
        self.assertIn("撤销记录未能合并", result["warnings"][0])
        self.assertEqual(self.col.note_count(), 1)
        resumed = self.apply(bundle)
        self.assertEqual((resumed["added"], resumed["unchanged"]), (2, 1))
        self.assertEqual(self.col.note_count(), 3)
        self.col.undo()
        self.assertEqual(self.col.note_count(), 1)

    def test_verified_cached_image_supports_update_and_races_do_not_overwrite(self):
        name = "ankisense-img-" + "2" * 32 + ".png"
        asset = {"filename": name, "sha256": hashlib.sha256(PNG).hexdigest(), "size_bytes": len(PNG)}
        self.card["media"] = [asset]
        self.bundle["media"] = {name: PNG}
        self.apply()
        changed = copy.deepcopy(self.bundle)
        changed["cards"][0]["version"] = 2
        changed["cards"][0]["fields"]["Answer"] = "Updated remotely"
        changed["media"] = {}
        changed["local_media"] = {name: asset["sha256"]}
        result = self.apply(changed)
        self.assertEqual(result["updated"], 1)
        (Path(self.col.media.dir()) / name).write_bytes(b"local changed image")
        result = self.apply(changed)
        self.assertTrue(result["conflicts"])
        self.assertEqual((Path(self.col.media.dir()) / name).read_bytes(), b"local changed image")

    def test_scoped_and_partial_batches_do_not_warn_about_other_documents(self):
        self.card["source"] = {"document_id": "doc-one"}
        other = copy.deepcopy(self.card)
        other.update(id="fixture-other", guid="fixtureOther")
        other["fields"]["NoteID"] = other["id"]
        other["source"] = {"document_id": "doc-two"}
        self.apply({**self.bundle, "cards": [self.card, other]})
        result = self.apply({**self.bundle, "catalog_ids": [self.card["id"]], "scope_document_id": "doc-one"})
        self.assertFalse(result["warnings"])
        result = self.apply({**self.bundle, "catalog_ids": [self.card["id"], other["id"]]})
        self.assertFalse(result["warnings"])
        self.assertEqual(self.col.note_count(), 2)

    def test_duplicate_identity_and_missing_deck_are_rejected(self):
        self.apply()
        another = self.col.new_note(self.col.models.get(self.adapter.MODEL_ID))
        another.guid = self.card["guid"]
        another.fields = [self.card["fields"][name] for name in self.card["field_order"]]
        self.col.add_note(another, self.deck_id)
        self.assertEqual(len(self.apply()["conflicts"]), 1)
        with self.assertRaises(self.adapter.SyncError):
            self.adapter.apply_bundle(self.col, self.bundle, 987654321, self.state_path)


if __name__ == "__main__":
    unittest.main(argv=[__file__], verbosity=2)
else:
    # The cases require Anki's embedded ABI and are collected by the child only.
    RealCollectionTests.__test__ = False
