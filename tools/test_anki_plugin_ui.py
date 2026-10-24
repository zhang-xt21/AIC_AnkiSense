"""Offscreen Qt smoke test using real Anki operations and an isolated collection.

Network/download and bundle interpretation are controlled fixtures here. Actual
HTTP + bundle synchronization is exercised separately; no personal Anki is used.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
import weakref
import zipfile

import anki
from anki.collection import Collection, OpChanges
import aqt
from aqt import gui_hooks
from aqt.qt import QApplication, QCoreApplication, QEvent, QFont, QFontDatabase, QMainWindow, QMenu
from aqt.taskman import TaskManager

test_root = Path(os.environ["ANKISENSE_ANKI_TEST_ROOT"])
application = QApplication.instance() or QApplication([])
application.setQuitOnLastWindowClosed(False)
# Qt's offscreen platform does not enumerate Windows fallback fonts reliably.
# Load the existing system font into this test process only; do not install it.
windows_dir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
font_path = Path(windows_dir) / "Fonts/msyh.ttc" if windows_dir else None
if font_path and font_path.is_file():
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        application.setFont(QFont(families[0], 10))
errors = []
def record_exception(kind, value, tb):
    errors.append(f"{kind.__name__}: {value}")
    traceback.print_exception(kind, value, tb)
sys.excepthook = record_exception


class Profile:
    def __init__(self):
        self.profile = {}
        self.saves = 0
    def profileFolder(self, create=False):
        return str(test_root)
    def save(self):
        self.saves += 1


class Progress:
    def __init__(self):
        self.levels = 0
    def start(self, **kwargs):
        self.levels += 1
    def finish(self):
        self.levels -= 1
    def busy(self):
        return self.levels > 0


class Host(QMainWindow):
    def __init__(self):
        super().__init__()
        self.app = application
        self.col = Collection(str(test_root / "ui-smoke.anki2"))
        self.pm = Profile()
        self.addonManager = SimpleNamespace(getConfig=lambda _: {})
        self.form = SimpleNamespace(menuTools=QMenu("Tools", self))
        self.menuBar().addMenu(self.form.menuTools)
        self.background_ops = 0
        self.undo_refreshes = 0
        self.progress = Progress()
        self.taskman = TaskManager(self)
    def weakref(self):
        return weakref.proxy(self)
    def _increase_background_ops(self):
        self.background_ops += 1
    def _decrease_background_ops(self):
        self.background_ops -= 1
    def update_undo_actions(self):
        self.undo_refreshes += 1


def wait_until(predicate, message, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if errors:
            raise AssertionError("Qt callback error: " + "; ".join(errors))
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError(message)


def install_answer_fixture(host, identifier="ui-answer-fixture", guid="uiAnswerFixtureGUID", digest="a"):
    """Sync one real AnkiSense note so answer checks use the production mapping."""
    from addons.ankisense_sync.collection_sync import FIELD_ORDER, MODEL_ID, apply_bundle

    package = Path(__file__).resolve().parents[1] / "addons/ankisense_sync/bootstrap.apkg"
    database = test_root / "answer-fixture-template.sqlite3"
    with zipfile.ZipFile(package) as archive:
        database.write_bytes(archive.read("collection.anki2"))
    with sqlite3.connect(database) as connection:
        models = json.loads(connection.execute("SELECT models FROM col").fetchone()[0])
    native = models[str(MODEL_ID)]
    model = {"id": MODEL_ID, "name": native["name"], "type": "standard",
             "fields": [{"name": field["name"]} for field in native["flds"]],
             "sort_field_index": native["sortf"], "css": native["css"],
             "templates": [{"name": item["name"], "qfmt": item["qfmt"],
                            "afmt": item["afmt"], "ordinal": item["ord"]}
                           for item in native["tmpls"]]}
    card = {"id": identifier, "version": 3, "guid": guid, "content_hash": digest * 64,
            "field_order": FIELD_ORDER,
            "fields": dict(zip(FIELD_ORDER, [identifier, f"解释 {identifier} 的出栈顺序？", "后进先出。", "栈",
                                             "fixture.pdf", "1", "最后压入的元素最先弹出。",
                                             f"http://127.0.0.1:8765/?card={identifier}"])),
            "model": model, "tags": ["AnkiSense"], "media": []}
    server_id = "as-" + "1" * 32
    bundle = {"server_id": server_id, "snapshot_id": "answer-fixture-snapshot",
              "cards": [card], "excluded": [], "media": {}}
    target = host.col.decks.id("AnkiSense Answer Fixture")
    result = apply_bundle(host.col, bundle, target, test_root / "ankisense-sync-state.json")
    assert result["added"] == 1, result
    note_id = host.col.db.scalar("SELECT id FROM notes WHERE guid=?", card["guid"])
    card_id = host.col.db.scalar("SELECT id FROM cards WHERE nid=?", note_id)
    return card, server_id, card_id


def scheduling_snapshot(col, card_id):
    schedule = col.db.first(
        "SELECT type, queue, due, ivl, factor, reps, lapses FROM cards WHERE id=?", card_id)
    return schedule, col.db.scalar("SELECT count(*) FROM revlog WHERE cid=?", card_id)


def cloud_confirmation(answer_ui, choice):
    """Replace the module binding; Qt extension types may reject patching methods."""
    box = SimpleNamespace(StandardButton=answer_ui.QMessageBox.StandardButton,
                          question=lambda *_args, **_kwargs: choice)
    return patch.object(answer_ui, "QMessageBox", box)


class AnswerClientFixture:
    """In-process local-service double; no socket or model is used by UI tests."""

    def __init__(self, card, server_id):
        self.card_data, self.server_id = card, server_id
        self.cloud = False
        self.submit_plan = []
        self.retry_plan = []
        self.submissions = []
        self.retries = []
        self.lookups = []
        self.latest = None
        self.submit_entered = Event()
        self.submit_gate = None

    def status(self):
        return {"application": "AnkiSense", "protocol_version": 1,
                "server_id": self.server_id,
                "capabilities": {"answer_analysis": True, "schedule_write": False},
                "ai": {"mode": "cloud" if self.cloud else "demo",
                       "requires_cloud_confirmation": self.cloud,
                       "configured": True, "images_in_answer_analysis": False},
                "limits": {"answer_characters": 4000, "retry_count": 2}}

    def card(self, card_id, *, expected_hash=None):
        self.lookups.append((card_id, expected_hash))
        assert card_id == self.card_data["id"]
        assert expected_hash == self.card_data["content_hash"]
        return {**self.card_data, "protocol_version": 1}

    def _record(self, answer, status="completed", *, retry_count=0):
        record = {"id": "ui-attempt-0001", "card_id": self.card_data["id"],
                  "status": status, "answer": answer,
                  "retry_count": retry_count, "error": "模拟分析中断" if status == "failed" else None,
                  "diagnosis": ({"verdict": "correct", "reason": "理解了后进先出。",
                                 "addressed_point_ids": [], "missing_point_ids": [],
                                 "contradicted_point_ids": [], "answer_spans": [],
                                 "source_block_ids": []} if status == "completed" else None),
                  "card_snapshot": {"question": self.card_data["fields"]["Question"],
                                    "answer": self.card_data["fields"]["Answer"],
                                    "rubric": []},
                  "source_snapshot": []}
        self.latest = record
        return record

    def submit_answer(self, card_id, card_version, answer, request_id, *, allow_cloud=False):
        self.submissions.append((card_id, card_version, answer, request_id, allow_cloud))
        assert card_id == self.card_data["id"] and card_version == self.card_data["version"]
        self.submit_entered.set()
        if self.submit_gate is not None and not self.submit_gate.wait(5):
            raise AssertionError("Timed out waiting for the UI to release a fixture request")
        planned = self.submit_plan.pop(0) if self.submit_plan else "completed"
        if isinstance(planned, Exception):
            raise planned
        return self._record(answer, planned)

    def attempt(self, attempt_id):
        assert self.latest is not None and attempt_id == self.latest["id"]
        return self.latest

    def retry_attempt(self, attempt_id, expected_retry_count, *, allow_cloud=False):
        self.retries.append((attempt_id, expected_retry_count, allow_cloud))
        assert self.latest is not None and attempt_id == self.latest["id"]
        planned = self.retry_plan.pop(0) if self.retry_plan else "completed"
        if isinstance(planned, Exception):
            raise planned
        return self._record(self.latest["answer"], planned, retry_count=expected_retry_count + 1)


host = Host()
aqt.mw = host
from addons.ankisense_sync import ui

receipts = []
ui.onboarding.send_event = lambda path, client_id, kind="heartbeat", result=None: receipts.append((kind, result))

checks = {}
screenshots = []
release = Event()
hook_calls = []
gui_hooks.operation_did_execute.append(lambda changes, initiator: hook_calls.append(changes))

try:
    ui.register(host)
    ui.register(host)
    actions = [action for action in host.form.menuTools.actions() if action.text() == ui.MENU_TEXT]
    assert len(actions) == 1
    checks["tools_menu_registered_once"] = True
    actions[0].trigger()
    wait_until(lambda: getattr(host, "_ankisense_sync_dialog", None) is not None, "Dialog did not open")
    dialog = host._ankisense_sync_dialog
    assert dialog.isVisible() and not dialog.sync_button.isEnabled()
    checks["dialog_requires_path_and_deck"] = True
    assert host.pm.saves >= 1 and host.pm.profile[ui.CLIENT_KEY].startswith("anki-")
    checks["per_profile_client_id_created"] = True
    ui.show_sync_dialog(host)
    assert host._ankisense_sync_dialog is dialog
    checks["dialog_singleton"] = True
    dialog.path_edit.setText(str(test_root / "plugin-connection.json"))
    dialog.deck_combo.setCurrentIndex(1)
    assert dialog.sync_button.isEnabled()
    filename = test_root / "sync-dialog-initial.png"
    application.processEvents()
    assert dialog.grab().save(str(filename))
    screenshots.append(str(filename))

    ui.check_connection = lambda path, client_id: {"cards": 3, "excluded": 2, "base_url": "http://127.0.0.1:18766", "protocol_version": 1}
    dialog.check_button.click()
    wait_until(lambda: not dialog._busy and "连接成功" in dialog.status_label.text(), "Connection callback did not complete")
    assert "3 张" in dialog.status_label.text() and host.background_ops == 0
    checks["real_queryop_updates_qt_on_success"] = True
    assert host.pm.profile[ui.PATH_KEY] == str(test_root / "plugin-connection.json")
    checks["connection_path_saved_without_credential_display"] = True
    wait_until(lambda: any(kind == "heartbeat" for kind, _ in receipts), "Connection receipt not emitted")
    checks["native_connection_callback_emits_heartbeat"] = True
    auto_path = test_root / "wizard-connection.json"
    auto_path.write_text("{}", encoding="utf-8")
    config = {"setup_id": "wizard-instance", "connection_file": str(auto_path)}
    host.addonManager.getConfig = lambda _: config
    assert ui.onboarding.connection_path(host) == str(auto_path)
    checks["new_desktop_pairing_overrides_old_profile_path_once"] = True
    count_before = len(receipts)
    host._ankisense_presence_timer.timeout.emit()
    wait_until(lambda: len(receipts) > count_before, "Automatic heartbeat did not use background task")
    checks["paired_plugin_sends_automatic_background_heartbeat"] = True
    host.pm.profile[ui.onboarding.SETUP_KEY] = config["setup_id"]
    assert ui.onboarding.connection_path(host) == str(test_root / "plugin-connection.json")
    host.addonManager.getConfig = lambda _: {}

    ui.prepare_bundle = lambda path, client_id: {"cards": [{"id": "ui-fixture"}], "excluded": ["draft"]}
    apply_calls = []
    def apply_fixture(col, bundle, deck_id, state_path):
        assert col is host.col and Path(state_path).parent == test_root
        if not bundle.get("cards"):
            apply_calls.append("empty")
            return {"changes": OpChanges(), "added": 0, "updated": 0, "unchanged": 0, "conflicts": [],
                    "warnings": ["原先同步的卡片已不在本次批准清单，保留 Anki 中的笔记，请回网页核对。"]}
        apply_calls.append(1)
        model = next(model for model in col.models.all() if len(model["flds"]) >= 2 and model["type"] == 0)
        note = col.new_note(model)
        note.fields[0], note.fields[1] = "Qt smoke fixture question", "Qt smoke fixture answer"
        changes = col.add_note(note, deck_id).changes
        return {"changes": changes, "added": 1, "updated": 0, "unchanged": 0, "conflicts": [], "warnings": []}
    ui.apply_bundle = apply_fixture
    initial_count = host.col.note_count()
    dialog.sync_button.click()
    wait_until(lambda: not dialog._busy and "同步完成" in dialog.status_label.text(), "Collection operation did not complete")
    assert host.col.note_count() == initial_count + 1 and apply_calls == [1]
    assert host.undo_refreshes == 1 and hook_calls and host.background_ops == 0 and host.progress.levels == 0
    checks["real_collectionop_writes_only_isolated_note"] = True
    checks["operation_hooks_and_undo_refresh"] = True
    wait_until(lambda: any(kind == "sync" for kind, _ in receipts), "Sync receipt not emitted")
    counts = next(value for kind, value in receipts if kind == "sync")
    assert counts == {"added": 1, "updated": 0, "unchanged": 0, "conflicts": 0, "excluded": 1}
    checks["native_sync_receipt_reports_only_completed_write"] = True
    assert "暂不可同步" in dialog.details.toPlainText()
    filename = test_root / "sync-dialog-completed.png"
    assert dialog.grab().save(str(filename))
    screenshots.append(str(filename))

    ui.prepare_bundle = lambda path, client_id: {"cards": [], "excluded": ["revoked"]}
    dialog.sync_button.click()
    wait_until(lambda: not dialog._busy, "Empty existing-deck callback did not complete")
    assert apply_calls == [1, "empty"] and "不在本次批准清单" in dialog.details.toPlainText()
    checks["empty_catalog_existing_deck_checks_stale_mapping"] = True
    before_decks = [deck.name for deck in host.col.decks.all_names_and_ids()]
    dialog.deck_combo.addItem("待新建：不得生成空牌组", "must-not-create-empty-deck")
    dialog.deck_combo.setCurrentIndex(dialog.deck_combo.count() - 1)
    dialog.sync_button.click()
    wait_until(lambda: not dialog._busy, "Empty new-deck callback did not complete")
    assert apply_calls == [1, "empty"] and [deck.name for deck in host.col.decks.all_names_and_ids()] == before_decks
    assert "保留" in dialog.details.toPlainText()
    checks["empty_catalog_does_not_create_new_deck"] = True
    dialog.deck_combo.setCurrentIndex(1)

    def unavailable(path, client_id):
        raise ui.PluginError(0, "connection_failed", "fixture-private-key-not-for-ui")
    ui.check_connection = unavailable
    dialog.check_button.click()
    wait_until(lambda: not dialog._busy, "Failure callback did not complete")
    assert "无法连接" in dialog.details.toPlainText() and "fixture-private-key" not in dialog.details.toPlainText()
    checks["background_failure_safe_message"] = True

    entered = Event()
    def delayed(path, client_id):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("UI fixture timeout")
        return {"cards": [{"id": "must-not-write"}], "excluded": []}
    ui.prepare_bundle = delayed
    dialog.sync_button.click()
    wait_until(entered.is_set, "Pending download did not start")
    assert dialog._busy and not dialog.close_button.isEnabled()
    dialog.reject()
    assert dialog.isVisible() and dialog._busy
    checks["busy_dialog_cannot_be_closed_early"] = True
    gui_hooks.profile_will_close()
    assert dialog._profile_closed and not dialog.isVisible()
    release.set()
    wait_until(lambda: getattr(host, "_ankisense_sync_dialog", None) is None and host.background_ops == 0, "Profile-close callback cleanup failed")
    assert host.col.note_count() == initial_count + 1 and apply_calls == [1, "empty"]
    checks["profile_close_discards_download_before_write"] = True
    assert not dialog._hook_registered
    checks["profile_close_hook_removed_after_callback"] = True
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    ui.show_sync_dialog(host)
    wait_until(lambda: getattr(host, "_ankisense_sync_dialog", None) is not None, "Dialog did not reopen")
    reopened = host._ankisense_sync_dialog
    reopened.close_button.click()
    wait_until(lambda: getattr(host, "_ankisense_sync_dialog", None) is None, "Idle close did not clean up")
    checks["idle_dialog_reopens_and_closes"] = True

    from addons.ankisense_sync import answer_journal, answer_ui

    # Recent Anki hook objects are callable registries, not iterable sequences.
    answer_ui.register(host)
    assert answer_ui._registered
    checks["answer_hooks_registered_once"] = True

    synced_card, server_id, review_card_id = install_answer_fixture(host)
    host.reviewer = SimpleNamespace(card=host.col.get_card(review_card_id), mw=host)
    host.pm.profile[ui.PATH_KEY] = str(test_root / "plugin-connection.json")
    fixture = AnswerClientFixture(synced_card, server_id)
    schedule_before = scheduling_snapshot(host.col, review_card_id)
    original_link = '<a class="as-practice" href="http://127.0.0.1:8765/?card=ui-answer-fixture">用自己的话回答</a>'
    state_file = test_root / "ankisense-sync-state.json"
    state_backup = test_root / "ankisense-sync-state.json.offline-test"
    state_file.rename(state_backup)
    try:
        assert answer_ui._card_will_show(original_link, host.reviewer.card, "reviewQuestion") == original_link
    finally:
        state_backup.rename(state_file)
    checks["unsynced_apkg_keeps_existing_web_practice_link"] = True
    rewritten = answer_ui._card_will_show(original_link, host.reviewer.card, "reviewQuestion")
    assert "pycmd('ankisense:answer')" in rewritten and "127.0.0.1" not in rewritten
    checks["review_button_opens_native_answer_dialog"] = True

    with (patch.object(answer_ui.PluginClient, "from_connection_file", return_value=fixture),
          patch.object(answer_ui, "showWarning", side_effect=AssertionError("Unexpected answer warning"))):
        answer_ui.show_answer_dialog(host)
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is not None,
                   "Answer dialog did not open")
        answer_dialog = host._ankisense_answer_dialog
        wait_until(lambda: answer_dialog._verified and not answer_dialog._busy,
                   "Answer card verification did not complete")
        assert answer_dialog.submit_button.isEnabled()
        assert synced_card["fields"]["Answer"] not in answer_dialog.question_label.text()
        assert answer_dialog.details.toPlainText() == ""
        assert fixture.lookups == [(synced_card["id"], synced_card["content_hash"])]
        checks["answer_requires_verified_synced_card_and_hides_reference"] = True

        answer_ui.show_answer_dialog(host)
        assert host._ankisense_answer_dialog is answer_dialog
        checks["answer_dialog_singleton"] = True
        with patch.object(answer_ui, "Reviewer", SimpleNamespace):
            assert answer_ui._receive_js_message((False, None), answer_ui.COMMAND, host.reviewer) == (True, None)
        assert host._ankisense_answer_dialog is answer_dialog
        checks["reviewer_bridge_opens_existing_answer_dialog"] = True

        original_answer = "  最后入栈的元素先出栈。\n请保留换行和空格。  "
        answer_dialog.answer_edit.setPlainText(original_answer)
        fixture.submit_gate = Event()
        answer_dialog.submit_button.click()
        wait_until(fixture.submit_entered.is_set, "Answer request did not start")
        assert answer_dialog._busy and not answer_dialog.submit_button.isEnabled()
        answer_dialog._submit()
        assert len(fixture.submissions) == 1
        fixture.submit_gate.set()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "completed",
                   "Answer submission did not complete")
        fixture.submit_gate = None
        first = fixture.submissions[-1]
        assert first[:3] == (synced_card["id"], synced_card["version"], original_answer)
        assert len(first[3]) >= 8 and first[4] is False
        assert answer_dialog.answer_edit.toPlainText() == original_answer
        assert "回答正确" in answer_dialog.details.toPlainText()
        assert synced_card["fields"]["Answer"] in answer_dialog.details.toPlainText()
        checks["answer_submitted_with_version_and_original_text"] = True
        checks["busy_answer_dialog_suppresses_duplicate_request"] = True
        filename = test_root / "answer-dialog-completed.png"
        assert answer_dialog.grab().save(str(filename))
        screenshots.append(str(filename))

        answer_dialog.new_answer_button.click()
        assert answer_dialog.answer_edit.toPlainText() == ""
        retained_answer = "  服务断线时仍应保存这份回答。  "
        answer_dialog.answer_edit.setPlainText(retained_answer)
        fixture.submit_plan = [ui.PluginError(None, "connection_failed", "private fixture detail"), "completed"]
        answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and "无法连接" in answer_dialog.status_label.text(),
                   "Failed answer request did not return")
        failed_request = fixture.submissions[-1]
        assert failed_request[3] != first[3]
        assert answer_dialog.answer_edit.toPlainText() == retained_answer
        assert answer_dialog.answer_edit.isReadOnly() and answer_dialog.submit_button.isEnabled()
        assert "private fixture detail" not in answer_dialog.status_label.text()
        pending = answer_journal.load(test_root, answer_dialog._context)
        assert pending["request_id"] == failed_request[3] and pending["answer"] == retained_answer
        old_context = answer_dialog._context
        answer_dialog.close_button.click()
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                   "Pending answer dialog did not close")
        # Simulate a resync while the POST response was lost. Replay must use
        # the original request ID and card version, never create a new analysis.
        new_context = {**old_context, "card_version": old_context["card_version"] + 1,
                       "content_hash": "b" * 64}
        with patch.object(answer_ui, "_review_context", return_value=new_context):
            answer_ui.show_answer_dialog(host)
        answer_dialog = host._ankisense_answer_dialog
        wait_until(lambda: answer_dialog._verified and not answer_dialog._busy,
                   "Old-version answer did not restore after resync")
        assert answer_dialog.answer_edit.toPlainText() == retained_answer
        assert answer_dialog._journal_context["card_version"] == old_context["card_version"]
        assert answer_journal.load(test_root, answer_dialog._context)["stale"]
        answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "completed",
                   "Same-ID submission recovery did not complete")
        assert fixture.submissions[-1] == failed_request
        assert answer_journal.load(test_root, answer_dialog._context) is None
        checks["failed_submission_preserves_answer_and_reuses_request_id"] = True
        checks["resync_replays_pending_answer_with_original_card_version"] = True
        fixture.card_data = {**synced_card, "version": new_context["card_version"],
                             "content_hash": new_context["content_hash"]}

        answer_dialog.new_answer_button.click()
        answer_dialog.answer_edit.setPlainText("模型失败仍应保留的原回答")
        fixture.submit_plan = ["failed"]
        answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "failed",
                   "Failed analysis record did not appear")
        submitted_count = len(fixture.submissions)
        assert answer_dialog.retry_button.isEnabled()
        assert answer_dialog.answer_edit.toPlainText() == "模型失败仍应保留的原回答"
        assert "回答正确" not in answer_dialog.details.toPlainText()
        answer_dialog.retry_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "completed",
                   "Explicit analysis retry did not complete")
        assert len(fixture.submissions) == submitted_count
        assert fixture.retries[-1] == ("ui-attempt-0001", 0, False)
        checks["failed_analysis_requires_explicit_retry_of_saved_attempt"] = True

        answer_dialog.new_answer_button.click()
        conflict_answer = "版本冲突时保留这份回答"
        answer_dialog.answer_edit.setPlainText(conflict_answer)
        fixture.submit_plan = [ui.PluginError(409, "card_changed", "private fixture detail")]
        answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._blocked,
                   "Version conflict did not block the stale card")
        assert answer_dialog.answer_edit.toPlainText() == conflict_answer
        assert "重新同步" in answer_dialog.status_label.text()
        assert not answer_dialog.submit_button.isEnabled()
        assert "private fixture detail" not in answer_dialog.status_label.text()
        rejected = answer_journal.load(test_root, answer_dialog._context)
        assert rejected["answer"] == conflict_answer and rejected["status"] == "rejected"
        checks["version_conflict_retains_answer_and_blocks_stale_resubmit"] = True

        conflicted_context = answer_dialog._context
        answer_dialog.close_button.click()
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                   "Answer dialog did not close")
        fixture.card_data = synced_card
        answer_ui.show_answer_dialog(host)
        restored_draft = host._ankisense_answer_dialog
        wait_until(lambda: restored_draft._verified and not restored_draft._busy,
                   "Rejected draft did not reopen")
        assert restored_draft.answer_edit.toPlainText() == conflict_answer
        assert restored_draft.submit_button.isEnabled() and restored_draft._request_id is None
        with patch.object(fixture, "card", side_effect=ui.PluginError(404, "not_found", "withdrawn")):
            restored_draft.submit_button.click()
            wait_until(lambda: not restored_draft._busy and restored_draft._blocked,
                       "Withdrawn-card preflight did not preserve the answer")
        assert answer_journal.load(test_root, restored_draft._context)["status"] == "rejected"
        checks["withdrawn_card_preflight_preserves_answer_as_draft"] = True
        restored_draft.close_button.click()
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                   "Rejected draft dialog did not close")
        checks["version_conflict_survives_dialog_reopen_as_draft"] = True
        # The next fixture deliberately changes service mode, so remove only
        # this test card's old draft after verifying the recovery behavior.
        answer_journal.clear(test_root, conflicted_context)
        fixture.cloud = True
        answer_ui.show_answer_dialog(host)
        answer_dialog = host._ankisense_answer_dialog
        wait_until(lambda: answer_dialog._verified and not answer_dialog._busy,
                   "Cloud-mode verification did not complete")
        answer_dialog.answer_edit.setPlainText("需要先确认的云端回答")
        before_cloud = len(fixture.submissions)
        with cloud_confirmation(answer_ui, answer_ui.QMessageBox.StandardButton.No):
            answer_dialog.submit_button.click()
        assert len(fixture.submissions) == before_cloud
        assert answer_dialog.answer_edit.toPlainText() == "需要先确认的云端回答"
        assert answer_dialog._request_id is None
        with cloud_confirmation(answer_ui, answer_ui.QMessageBox.StandardButton.Yes):
            answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "completed",
                   "Confirmed cloud submission did not complete")
        assert fixture.submissions[-1][-1] is True
        checks["cloud_answer_requires_explicit_confirmation"] = True

        answer_dialog.new_answer_button.click()
        answer_dialog.answer_edit.setPlainText("云端重试也需要再次确认")
        fixture.submit_plan = ["failed"]
        with cloud_confirmation(answer_ui, answer_ui.QMessageBox.StandardButton.Yes):
            answer_dialog.submit_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "failed",
                   "Cloud analysis failure did not appear")
        before_retry = len(fixture.retries)
        with cloud_confirmation(answer_ui, answer_ui.QMessageBox.StandardButton.No):
            answer_dialog.retry_button.click()
        assert len(fixture.retries) == before_retry
        with cloud_confirmation(answer_ui, answer_ui.QMessageBox.StandardButton.Yes):
            answer_dialog.retry_button.click()
        wait_until(lambda: not answer_dialog._busy and answer_dialog._attempt_status == "completed",
                   "Confirmed cloud retry did not complete")
        assert fixture.retries[-1] == ("ui-attempt-0001", 0, True)
        checks["cloud_retry_requires_separate_confirmation"] = True

        assert scheduling_snapshot(host.col, review_card_id) == schedule_before
        checks["answer_feedback_does_not_change_anki_rating_or_schedule"] = True
        answer_dialog.close_button.click()
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                   "Answer dialog did not clean up")

        second_card, _, second_review_id = install_answer_fixture(
            host, identifier="ui-answer-second", guid="uiAnswerFixtureGUID2", digest="b")
        second_schedule_before = scheduling_snapshot(host.col, second_review_id)
        fixture.cloud = False
        fixture.submit_entered.clear()
        fixture.submit_gate = Event()
        answer_ui.show_answer_dialog(host)
        old_dialog = host._ankisense_answer_dialog
        old_context = old_dialog._context
        wait_until(lambda: old_dialog._verified and not old_dialog._busy,
                   "First card did not reopen before switching cards")
        old_dialog.answer_edit.setPlainText("换卡时旧请求仍在后台")
        old_dialog.submit_button.click()
        wait_until(fixture.submit_entered.is_set, "Old-card request did not start")
        host.reviewer.card = host.col.get_card(second_review_id)
        answer_ui._reviewer_did_show_question(host.reviewer.card)
        assert old_dialog._closed_profile and not old_dialog.isVisible()
        fixture.submit_gate.set()
        wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                   "Old-card callback did not release its dialog after card switch")
        assert answer_journal.load(test_root, old_context)["answer"] == "换卡时旧请求仍在后台"
        fixture.submit_gate = None
        second_fixture = AnswerClientFixture(second_card, server_id)
        with patch.object(answer_ui.PluginClient, "from_connection_file", return_value=second_fixture):
            answer_ui.show_answer_dialog(host)
            new_dialog = host._ankisense_answer_dialog
            wait_until(lambda: new_dialog._verified and not new_dialog._busy,
                       "New card did not verify after old request completed")
            assert new_dialog._context["card_id"] == second_card["id"]
            new_dialog.answer_edit.setPlainText("新卡片的独立回答")
            new_dialog.submit_button.click()
            wait_until(lambda: not new_dialog._busy and new_dialog._attempt_status == "completed",
                       "New-card request did not complete")
            assert second_fixture.submissions[-1][:2] == (second_card["id"], second_card["version"])
            assert scheduling_snapshot(host.col, second_review_id) == second_schedule_before
            assert scheduling_snapshot(host.col, review_card_id) == schedule_before
            new_dialog.close_button.click()
            wait_until(lambda: getattr(host, "_ankisense_answer_dialog", None) is None,
                       "New-card dialog did not close")
        checks["card_switch_releases_inflight_answer_and_opens_new_card"] = True
    result = {"anki_version": anki.version, "python": sys.version.split()[0], "platform": os.environ["QT_QPA_PLATFORM"],
              "scope": "Real Qt widgets, QueryOp, CollectionOp and TaskManager; fixture local service and isolated synced collection only.",
              "checks": checks, "passed": len(checks), "screenshots": screenshots, "callback_errors": errors}
    (test_root / "ui-smoke-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
finally:
    release.set()
    host.taskman._no_collection_executor.shutdown(wait=True)
    host.taskman._collection_executor.shutdown(wait=True)
    application.processEvents()
    host.col.close()
    host.col = None
    aqt.mw = None
    host.close()
