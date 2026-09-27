"""Offscreen Qt smoke test using real Anki operations and an isolated collection.

Network/download and bundle interpretation are controlled fixtures here. Actual
HTTP + bundle synchronization is exercised separately; no personal Anki is used.
"""
import json
import os
from pathlib import Path
import sys
import time
import traceback
from threading import Event
from types import SimpleNamespace
import weakref

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


host = Host()
aqt.mw = host
from addons.ankisense_sync import ui

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
    result = {"anki_version": anki.version, "python": sys.version.split()[0], "platform": os.environ["QT_QPA_PLATFORM"],
              "scope": "Real Qt widgets, QueryOp, CollectionOp and TaskManager; fixture transport/bundle writer; isolated collection only.",
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
