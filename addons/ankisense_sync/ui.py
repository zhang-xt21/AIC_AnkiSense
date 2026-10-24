"""Small Anki-native dialog for verified local card synchronization."""
from __future__ import annotations

from pathlib import Path
import re
from uuid import uuid4

from aqt import gui_hooks
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QAction, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, Qt, QTimer, QVBoxLayout,
)
from aqt.utils import showWarning

from .client import PluginError
from .collection_sync import SyncError, apply_bundle
from .transport import check_connection, prepare_bundle


CLIENT_KEY = "ankisense_sync_client_id"
PATH_KEY = "ankisense_sync_connection_file"
DECK_KEY = "ankisense_sync_deck_id"
MENU_TEXT = "AnkiSense · 同步卡片…"


class _ProfileChanged(RuntimeError):
    pass


def _client_id(main_window):
    value = main_window.pm.profile.get(CLIENT_KEY)
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", value):
        value = "anki-" + uuid4().hex
        main_window.pm.profile[CLIENT_KEY] = value
        main_window.pm.save()
    return value


def _error_message(error, stage):
    if isinstance(error, _ProfileChanged):
        return "Anki 资料已切换，本次操作已停止。请在当前资料中重新打开同步窗口。"
    if isinstance(error, SyncError):
        return str(error)
    if isinstance(error, PluginError):
        messages = {
            "connection_failed": "无法连接本机 AnkiSense。请确认服务正在运行，再检查连接文件中的本机地址。",
            "unauthorized": "连接凭据已失效。请重新选择当前的 plugin-connection.json；若已重置连接，请使用新文件。",
            "plugin_disabled": "AnkiSense 的插件连接尚未启用。请先启用本机插件连接，再重试。",
            "invalid_client": "当前 Anki 资料的连接标识无效，请关闭此窗口后重新打开。",
            "different_server": "连接文件与当前 AnkiSense 数据目录不一致。请重新选择该数据目录的连接文件。",
            "incompatible_protocol": "AnkiSense 接口版本与此插件不兼容，请更新到配套版本。",
            "incomplete_catalog": "未收到完整的卡片清单。请检查服务后重新同步。",
            "card_changed": "下载期间卡片或审核状态发生变化，请重新同步。",
            "media_changed": "配图已更新，请重新同步以获取完整版本。",
            "media_missing": "部分配图不存在，请先在 AnkiSense 中修复配图后重新同步。",
            "media_mismatch": "配图校验未通过，请重新同步；若仍失败，请在 AnkiSense 中重新保存配图。",
            "redirect_blocked": "本机地址发生重定向，已停止连接。请选择正确的 AnkiSense 连接文件。",
            "response_too_large": "本次返回的数据超过插件限制，请减少单次同步的卡片或配图数量。",
            "invalid_response": "本机服务返回了无法识别的数据，请确认连接的是 AnkiSense。",
            "not_found": "卡片或配图已不可用，请在 AnkiSense 中检查后重新同步。",
            "conflict": "卡片状态已变化，请检查连接后重新同步。",
        }
        if error.code == "invalid_sync_data":
            # These are fixed, locally authored transport-validation messages.
            return str(error)
        return messages.get(error.code, "AnkiSense 未能完成本次请求，请检查本机服务和连接配置后重试。")
    if isinstance(error, ValueError):
        return "连接文件无效或尚未启用。请选择当前本机 AnkiSense 的 plugin-connection.json。"
    if isinstance(error, OSError):
        return "无法读取或保存本机文件。请检查连接文件和当前 Anki 资料目录的访问权限。"
    if stage == "write":
        return "同步写入未能完成。请检查 Anki 的资料和媒体目录，然后重新检查连接。"
    return "操作未能完成，请检查本机 AnkiSense 服务和连接文件后重试。"


class SyncDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.mw = main_window
        self._collection = main_window.col
        self._profile_path = Path(main_window.pm.profileFolder(create=False)).resolve()
        self._client_id = _client_id(main_window)
        self._busy = False
        self._profile_closed = False
        self._stage = "connection"
        self._excluded = 0
        self._empty_catalog = False
        self._active_path = ""
        self._target = None
        self._hook_registered = False

        self.setWindowTitle("AnkiSense · 同步卡片")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(620)
        self.resize(680, 470)
        layout = QVBoxLayout(self)
        intro = QLabel("从本机 AnkiSense 同步已批准的卡片，保留 Anki 的学习进度。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("连接文件", self))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText("选择本机 plugin-connection.json")
        self.path_edit.setText(self._initial_path())
        self.browse_button = QPushButton("选择文件…", self)
        self.browse_button.setAutoDefault(False)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_button)
        layout.addLayout(path_row)
        path_hint = QLabel("连接凭据只从文件读取，不在窗口中显示。", self)
        path_hint.setWordWrap(True)
        layout.addWidget(path_hint)

        layout.addWidget(QLabel("新增卡片的目标牌组", self))
        deck_row = QHBoxLayout()
        self.deck_combo = QComboBox(self)
        self.deck_combo.addItem("请选择目标牌组…", None)
        self.new_deck_button = QPushButton("新建牌组…", self)
        self.new_deck_button.setAutoDefault(False)
        deck_row.addWidget(self.deck_combo, 1)
        deck_row.addWidget(self.new_deck_button)
        layout.addLayout(deck_row)
        self._load_decks()
        deck_hint = QLabel("已有卡片保持原牌组；在 Anki 中手工修改过的内容会列为冲突，供你核对。", self)
        deck_hint.setWordWrap(True)
        layout.addWidget(deck_hint)

        self.status_label = QLabel("选择连接文件和目标牌组后，即可检查连接或同步。", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("连接检查、同步计数和需要核对的冲突会显示在这里。")
        layout.addWidget(self.details, 1)

        buttons = QHBoxLayout()
        self.check_button = QPushButton("检查连接", self)
        self.sync_button = QPushButton("同步已批准卡片", self)
        self.close_button = QPushButton("关闭", self)
        for button in (self.check_button, self.sync_button, self.close_button):
            button.setAutoDefault(False)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.sync_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.browse_button.clicked.connect(self._choose_file)
        self.new_deck_button.clicked.connect(self._choose_new_deck)
        self.path_edit.textChanged.connect(self._path_changed)
        self.deck_combo.currentIndexChanged.connect(self._refresh_controls)
        self.check_button.clicked.connect(self._check)
        self.sync_button.clicked.connect(self._sync)
        self.close_button.clicked.connect(self.reject)
        self.finished.connect(self._finished)
        gui_hooks.profile_will_close.append(self._profile_will_close)
        self._hook_registered = True
        self._refresh_controls()

    def _initial_path(self):
        value = self.mw.pm.profile.get(PATH_KEY)
        if isinstance(value, str):
            return value
        try:
            config = self.mw.addonManager.getConfig(__name__) or {}
            value = config.get("connection_file", "")
            return value if isinstance(value, str) else ""
        except Exception:
            return ""

    def _load_decks(self):
        preferred = self.mw.pm.profile.get(DECK_KEY)
        selected = 0
        for deck in self._collection.decks.all_names_and_ids(include_filtered=False):
            identifier = int(deck.id)
            self.deck_combo.addItem(deck.name, identifier)
            index = self.deck_combo.count() - 1
            if identifier == preferred:
                selected = index
            elif not selected and deck.name == "AnkiSense":
                selected = index
        self.deck_combo.setCurrentIndex(selected)

    def _refresh_controls(self, *_args):
        available = not self._busy and not self._profile_closed
        for widget in (self.path_edit, self.browse_button, self.deck_combo, self.new_deck_button):
            widget.setEnabled(available)
        has_path = bool(self.path_edit.text().strip())
        self.check_button.setEnabled(available and has_path)
        self.sync_button.setEnabled(available and has_path and self.deck_combo.currentData() is not None)
        self.close_button.setEnabled(not self._busy)

    def _path_changed(self, *_args):
        if not self._busy:
            self.status_label.setText("连接文件已更改，请重新检查连接。")
            self.details.clear()
        self._refresh_controls()

    def _set_busy(self, busy, message=None):
        self._busy = busy
        if message is not None:
            self.status_label.setText(message)
        self._refresh_controls()

    def _profile_is_current(self):
        if self._profile_closed or self.mw.col is not self._collection:
            return False
        try:
            return Path(self.mw.pm.profileFolder(create=False)).resolve() == self._profile_path
        except Exception:
            return False

    def _close_if_profile_changed(self):
        if self._profile_is_current():
            return False
        self._profile_closed = True
        self._busy = False
        super().reject()
        return True

    def _choose_file(self):
        if self._busy:
            return
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "选择 AnkiSense 本机连接文件", self.path_edit.text().strip(),
            "JSON 文件 (*.json);;所有文件 (*)")
        if path:
            self.path_edit.setText(path)

    def _choose_new_deck(self):
        if self._busy or self._close_if_profile_changed():
            return
        name, accepted = QInputDialog.getText(self, "新建同步目标牌组", "牌组名称：", text="AnkiSense")
        if not accepted:
            return
        name = name.strip()
        if not name or len(name) > 150 or any(not part.strip() for part in name.split("::")):
            self.status_label.setText("请填写有效的牌组名称（最多 150 个字符）；层级之间使用 ::。")
            return
        for index in range(1, self.deck_combo.count()):
            if self.deck_combo.itemText(index) == name:
                self.deck_combo.setCurrentIndex(index)
                self.status_label.setText("该牌组已存在，已选为同步目标。")
                return
        self.deck_combo.addItem(f"新建：{name}（同步时创建）", name)
        self.deck_combo.setCurrentIndex(self.deck_combo.count() - 1)
        self.status_label.setText("新牌组将在你点击“同步已批准卡片”后创建。")

    def _start(self, stage, message):
        if self._busy or self._close_if_profile_changed():
            return False
        self._active_path = self.path_edit.text().strip()
        if not self._active_path:
            self.status_label.setText("请先选择本机连接文件。")
            return False
        self._stage = stage
        self.details.clear()
        self._set_busy(True, message)
        return True

    def _check(self):
        if not self._start("connection", "正在检查本机连接和已批准卡片清单…"):
            return
        path, client_id = self._active_path, self._client_id
        try:
            QueryOp(parent=self, op=lambda _col: check_connection(path, client_id),
                    success=self._checked).without_collection().failure(self._failed).run_in_background()
        except Exception as error:
            self._failed(error)

    def _remember(self, deck_id=None):
        self.mw.pm.profile[PATH_KEY] = self._active_path
        if deck_id is not None:
            self.mw.pm.profile[DECK_KEY] = deck_id
        try:
            self.mw.pm.save()
            return ""
        except Exception:
            return "本次操作已完成，但连接偏好未能保存，下次可能需要重新选择。"

    def _checked(self, result):
        if self._close_if_profile_changed():
            return
        warning = self._remember()
        self._set_busy(False, f"连接成功：{result['cards']} 张卡片可同步，{result['excluded']} 张暂不可同步。")
        lines = [f"本机服务：{result['base_url']}", f"接口版本：{result['protocol_version']}",
                 "同步会下载卡片文字和配图，不会触发 AI 答案分析。"]
        if warning:
            lines.append(warning)
        self.details.setPlainText("\n".join(lines))

    def _sync(self):
        target = self.deck_combo.currentData()
        if target is None:
            self.status_label.setText("请明确选择新增卡片的目标牌组。")
            return
        if not self._start("download", "正在下载并核对已批准卡片及配图…"):
            return
        self._target = target
        path, client_id = self._active_path, self._client_id
        try:
            QueryOp(parent=self, op=lambda _col: prepare_bundle(path, client_id),
                    success=self._prepared).without_collection().failure(self._failed).run_in_background()
        except Exception as error:
            self._failed(error)

    def _prepared(self, bundle):
        if self._close_if_profile_changed():
            return
        self._excluded = len(bundle.get("excluded", []))
        self._empty_catalog = not bundle.get("cards")
        if self._empty_catalog and isinstance(self._target, str):
            self._set_busy(False, "当前没有可同步的已批准卡片。")
            self.details.setPlainText(
                f"本次清单有 {self._excluded} 张卡片暂不可同步，因此未创建新牌组。\n"
                "已有 Anki 笔记会保留。此前同步的卡片可能已撤销批准或来源已过期，请回 AnkiSense 网页核对。\n"
                "可选择一个已有牌组再次同步，以核对已有的同步记录。")
            return
        self._stage = "write"
        self.status_label.setText("正在核对已有同步记录…" if self._empty_catalog
                                  else "下载校验完成，正在写入当前 Anki 资料…")
        target = self._target
        state_path = self._profile_path / "ankisense-sync-state.json"
        outcome = {}

        def write(col):
            # CollectionOp passes the collection current when the queued job
            # starts. Never use a new profile if the user switched meanwhile.
            if self._profile_closed or col is not self._collection:
                raise _ProfileChanged()
            deck_id = int(col.decks.id(target)) if isinstance(target, str) else int(target)
            outcome["deck_id"] = deck_id
            outcome["result"] = apply_bundle(col, bundle, deck_id, state_path)
            # CollectionOp requires OpChanges (or an object with .changes), not
            # a plain dictionary. Anki then refreshes undo and collection views.
            return outcome["result"]["changes"]

        try:
            CollectionOp(parent=self, op=write).success(
                lambda _changes: self._applied(outcome)).failure(self._failed).run_in_background()
        except Exception as error:
            self._failed(error)

    def _applied(self, outcome):
        if self._close_if_profile_changed():
            return
        result = outcome["result"]
        conflicts = result.get("conflicts", [])
        warnings = list(result.get("warnings", []))
        preference_warning = self._remember(outcome["deck_id"])
        if preference_warning:
            warnings.append(preference_warning)
        self._set_busy(False, f"同步完成：新增 {result['added']} · 更新 {result['updated']} · "
                             f"未变化 {result['unchanged']} · 冲突 {len(conflicts)}")
        lines = []
        if self._empty_catalog:
            lines.append("当前没有可同步的已批准卡片，已有 Anki 笔记会保留。")
            lines.append("此前同步的卡片可能已撤销批准或来源已过期，请回 AnkiSense 网页核对。")
        if self._excluded:
            lines.append(f"另有 {self._excluded} 张卡片暂不可同步，已跳过。")
        if conflicts:
            lines.append("以下内容需要核对，已保留 Anki 中的版本：")
            lines.extend(f"• {item}" for item in conflicts[:30])
            if len(conflicts) > 30:
                lines.append(f"其余 {len(conflicts) - 30} 项冲突未展开。")
        if warnings:
            lines.append("需要留意：")
            lines.extend(f"• {item}" for item in warnings[:20])
            if len(warnings) > 20:
                lines.append(f"其余 {len(warnings) - 20} 条提示未展开。")
        if not lines:
            lines.append("已完成同步，可以关闭窗口并开始学习。")
        self.details.setPlainText("\n".join(lines))
        # Refresh the target list after a newly created deck, keeping the deck
        # selected for another synchronization in this dialog.
        self.deck_combo.clear()
        self.deck_combo.addItem("请选择目标牌组…", None)
        self._load_decks()
        self._refresh_controls()

    def _failed(self, error):
        if self._close_if_profile_changed():
            return
        message = _error_message(error, self._stage)
        self._set_busy(False, "本次操作未完成。")
        self.details.setPlainText(message)

    def _profile_will_close(self):
        self._profile_closed = True
        self.hide()
        # Keep the dialog alive until a running callback arrives. In particular,
        # a completed download must be discarded instead of written elsewhere.
        if not self._busy:
            # Removing our hook while Anki is iterating it could skip the next
            # add-on's callback. Close on the next Qt turn instead.
            QTimer.singleShot(0, self._close_if_profile_changed)

    def _finished(self, _result):
        if self._hook_registered:
            gui_hooks.profile_will_close.remove(self._profile_will_close)
            self._hook_registered = False
        if getattr(self.mw, "_ankisense_sync_dialog", None) is self:
            self.mw._ankisense_sync_dialog = None

    def reject(self):
        if self._busy:
            self.status_label.setText("操作正在进行，请等待结果后再关闭窗口。")
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            self.status_label.setText("操作正在进行，请等待结果后再关闭窗口。")
            event.ignore()
            return
        super().closeEvent(event)


def show_sync_dialog(main_window):
    if main_window.col is None:
        showWarning("请先打开一个 Anki 资料，再同步卡片。", parent=main_window)
        return
    existing = getattr(main_window, "_ankisense_sync_dialog", None)
    if existing is not None:
        if existing._profile_closed:
            showWarning("上一个 Anki 资料的连接任务正在结束，请稍后再打开同步窗口。", parent=main_window)
            return
        existing.show()
        existing.raise_()
        existing.activateWindow()
        return
    try:
        dialog = SyncDialog(main_window)
    except Exception:
        showWarning("无法打开同步窗口。请确认 Anki 资料已载入且资料目录可写。", parent=main_window)
        return
    main_window._ankisense_sync_dialog = dialog
    dialog.show()


def register(main_window):
    # The reviewer action is installed alongside the sync menu. Import here to
    # keep the existing add-on entry point and its non-Anki client import safe.
    from .answer_ui import register as register_answer

    register_answer(main_window)
    if getattr(main_window, "_ankisense_sync_action", None) is not None:
        return
    action = QAction(MENU_TEXT, main_window)
    action.triggered.connect(lambda _checked=False: show_sync_dialog(main_window))
    main_window.form.menuTools.addAction(action)
    main_window._ankisense_sync_action = action
