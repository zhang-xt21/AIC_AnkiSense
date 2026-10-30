"""Read signatures and exercise only a newly created isolated collection."""
import inspect
import json
import os
from pathlib import Path
import sys

import anki
from anki.collection import Collection
from anki.models import ModelManager
from anki.media import MediaManager
from anki.decks import DeckManager
import aqt
from aqt import gui_hooks
from aqt.operations import CollectionOp, QueryOp


def signature(obj):
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return "unavailable"


def details(cls, names):
    return {name: {"signature": signature(getattr(cls, name)), "doc": (inspect.getdoc(getattr(cls, name)) or "")[:1400]}
            for name in names if hasattr(cls, name)}


test_root = Path(os.environ["ANKISENSE_ANKI_TEST_ROOT"])
result = {
    "python": sys.version, "anki_version": getattr(anki, "version", None), "aqt_imported": True,
    "query_op": details(QueryOp, ["__init__", "success", "failure", "run_in_background", "without_collection", "with_progress"]),
    "collection_op": details(CollectionOp, ["__init__", "success", "failure", "run_in_background", "with_progress"]),
    "collection": details(Collection, ["__init__", "add_note", "add_notes", "update_note", "update_notes", "find_notes", "get_note", "new_note", "get_card", "update_card", "save", "close", "add_custom_undo_entry", "merge_undo_entries"]),
    "models": details(ModelManager, ["new", "add", "by_name", "get", "update_dict", "save", "new_field", "add_field", "new_template", "add_template"]),
    "media": details(MediaManager, ["write_data", "add_file", "dir", "check", "files_in_str"]),
    "decks": details(DeckManager, ["id", "all_names_and_ids", "get"]),
    "main_window": details(aqt.AnkiQt, ["reset"]),
    "hooks": {name: {"present": hasattr(gui_hooks, name), "signature": signature(getattr(gui_hooks, name)) if hasattr(gui_hooks, name) else None,
                     "doc": (inspect.getdoc(getattr(gui_hooks, name)) or "")[:1000] if hasattr(gui_hooks, name) else None}
              for name in ["profile_will_close", "profile_did_open", "main_window_did_init", "operation_did_execute"]},
}
collection = Collection(str(test_root / "inspection.anki2"))
try:
    result["collection_smoke"] = {"note_count": collection.note_count(),
                                  "deck_names": [{"id": item.id, "name": item.name} for item in collection.decks.all_names_and_ids()],
                                  "media_dir": collection.media.dir(), "notetype_count": len(collection.models.all())}
finally:
    collection.close()
(test_root / "runtime-inspection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"anki_version": result["anki_version"], "python": sys.version.split()[0], "aqt_imported": True,
                  "result": str(test_root / "runtime-inspection.json"), "collection_smoke": result["collection_smoke"]}, ensure_ascii=False))
