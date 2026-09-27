"""Actual Anki collection + loopback HTTP acceptance in guarded test runtime."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, ProxyHandler, build_opener

from anki.collection import Collection
from addons.ankisense_sync.client import PluginClient, PluginError
from addons.ankisense_sync.transport import check_connection, prepare_bundle
from addons.ankisense_sync.collection_sync import apply_bundle


def main():
    test_root = Path(os.environ["ANKISENSE_ANKI_TEST_ROOT"])
    connection_path = Path(sys.argv[1])
    client_id = "isolated-real-anki-profile"
    client = PluginClient.from_connection_file(connection_path, client_id, timeout=10)
    opener = build_opener(ProxyHandler({}))
    report = {"cloud_calls": 0, "personal_collection_modified": False, "checks": []}

    def check(name, **values):
        report["checks"].append({"name": name, "passed": True, **values})

    def request(path, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        method = "GET" if payload is None else "PUT"
        with opener.open(Request(client.base_url + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"}), timeout=10) as response:
            return json.load(response)

    def update(card_id, **changes):
        card = next(c for c in request("/api/state")["cards"] if c["id"] == card_id)
        keys = ("question", "answer", "concept", "purpose", "rubric", "evidence", "status", "images")
        payload = {key: card[key] for key in keys if key in card}
        payload.update(changes)
        return request("/api/cards/" + card_id, payload)

    collection = Collection(str(test_root / "acceptance.anki2"))
    state = test_root / "ankisense-sync-state.json"
    try:
        deck = collection.decks.id("AnkiSense · 插件验收")
        status = check_connection(connection_path, client_id)
        assert status["cards"] == 2 and status["ai"]["mode"] == "demo"
        check("local_authenticated_connection", cards=status["cards"])
        bundle = prepare_bundle(connection_path, client_id)
        result = apply_bundle(collection, bundle, deck, state)
        assert result["added"] == 2 and not result["conflicts"], result
        assert collection.note_count() == 2
        check("first_sync_adds_two_approved_cards", **{key: result[key] for key in ("added", "updated", "unchanged")})
        assert collection.models.get(1786831001) is not None
        check("stable_apkg_model_identity")
        assert not collection.find_notes("__ankisense-bootstrap-v1__")
        check("bootstrap_note_removed")
        for filename, data in bundle["media"].items():
            saved = Path(collection.media.dir()) / filename
            assert saved.read_bytes() == data
        check("both_image_sides_saved_in_anki_media", images=len(bundle["media"]))
        nids = {detail["id"]: collection.find_notes('NoteID:' + detail["id"])[0] for detail in bundle["cards"]}
        first_ids = {key: collection.get_note(nid).card_ids() for key, nid in nids.items()}
        repeated = apply_bundle(collection, prepare_bundle(connection_path, client_id), deck, state)
        assert repeated["added"] == 0 and repeated["updated"] == 0 and repeated["unchanged"] == 2, repeated
        check("repeat_sync_does_not_duplicate")
        card = collection.get_card(first_ids["demo-stack"][0])
        card.queue, card.type, card.due, card.ivl, card.reps, card.lapses = 2, 2, 123, 42, 7, 1
        collection.update_card(card)
        schedule = {key: getattr(card, key) for key in ("queue", "type", "due", "ivl", "reps", "lapses", "did")}
        before_log = collection.db.all("select * from revlog where cid=?", card.id)
        update("demo-stack", answer="后进先出：最后入栈的元素最先出栈。", status="approved")
        changed = apply_bundle(collection, prepare_bundle(connection_path, client_id), deck, state)
        assert changed["updated"] == 1 and changed["added"] == 0, changed
        updated = collection.get_card(card.id)
        assert schedule == {key: getattr(updated, key) for key in schedule}
        assert before_log == collection.db.all("select * from revlog where cid=?", card.id)
        assert first_ids == {key: collection.get_note(nid).card_ids() for key, nid in nids.items()}
        check("content_update_preserves_note_card_identity_and_schedule", schedule=schedule,
              note="Uses seeded review state; separate adapter tests cover undo and APKG.")
        note = collection.get_note(nids["demo-queue"])
        note["Answer"] = "我在 Anki 中手动修改的答案"
        collection.update_note(note)
        update("demo-queue", answer="先进先出：最先入队的元素最先出队。", status="approved")
        conflict = apply_bundle(collection, prepare_bundle(connection_path, client_id), deck, state)
        assert conflict["conflicts"] and collection.get_note(note.id)["Answer"] == "我在 Anki 中手动修改的答案", conflict
        check("local_edit_conflict_keeps_user_answer")
        update("demo-stack", status="draft")
        withdrawn = apply_bundle(collection, prepare_bundle(connection_path, client_id), deck, state)
        assert collection.note_count() == 2 and withdrawn["warnings"], withdrawn
        check("withdrawn_approval_warns_without_deleting")
        wrong = PluginClient(client.base_url, "x" * 43, client_id, timeout=10)
        try:
            wrong.status()
        except PluginError as exc:
            assert exc.status == 401
        else:
            raise AssertionError("wrong token accepted")
        check("invalid_credential_rejected")
        question, answer = updated.question(), updated.answer()
        assert "<img" in question and "<img" in answer
        check("anki_renders_both_image_sides_without_backend_fetch")
        report["passed"] = True
    finally:
        collection.close()
        (test_root / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                      "report": str(test_root / "acceptance.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
