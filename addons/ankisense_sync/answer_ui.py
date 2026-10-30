"""Answer an AnkiSense review card in Anki without exposing answers in card HTML."""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
from uuid import uuid4

import aqt
from aqt import gui_hooks
from aqt.operations import QueryOp
from aqt.qt import (QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                    QPushButton, Qt, QTimer, QVBoxLayout)
from aqt.reviewer import Reviewer
from aqt.utils import showWarning

from .client import PluginClient, PluginError
from .collection_sync import MODEL_ID, FIELD_ORDER, _hash
from . import answer_journal
from .ui import PATH_KEY, _client_id


COMMAND = "ankisense:answer"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_CLASS = re.compile(r'''(?:^|\s)class\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))''', re.I)
_registered = False


class AnswerUnavailable(RuntimeError):
    """A safe, user-facing reason this note cannot be analysed yet."""


def _plain(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())


def _review_context(main_window, card=None):
    reviewer = getattr(main_window, "reviewer", None)
    card = card if card is not None else getattr(reviewer, "card", None)
    if main_window.col is None or card is None:
        raise AnswerUnavailable("请先在 Anki 中打开一张 AnkiSense 卡片。")
    note = card.note()
    if note.mid != MODEL_ID:
        raise AnswerUnavailable("当前不是 AnkiSense 卡片。")
    try:
        card_id = note["NoteID"]
        question = note["Question"]
    except (KeyError, IndexError):
        raise AnswerUnavailable("卡片字段不完整，请重新同步。") from None
    if not _ID.fullmatch(card_id):
        raise AnswerUnavailable("卡片身份无效，请重新同步。")
    profile_path = Path(main_window.pm.profileFolder(create=False)).resolve()
    state_path = profile_path / "ankisense-sync-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("servers"), dict):
            raise ValueError
        matches = [(server_id, server["cards"][card_id])
                   for server_id, server in state["servers"].items()
                   if isinstance(server, dict) and isinstance(server.get("cards"), dict)
                   and card_id in server["cards"]]
        if len(matches) != 1:
            raise ValueError
        server_id, tracked = matches[0]
        if (not re.fullmatch(r"as-[0-9a-f]{32}", server_id)
                or not isinstance(tracked, dict)
                or type(tracked.get("note_id")) is not int or tracked["note_id"] != note.id
                or tracked.get("guid") != note.guid
                or tracked.get("fields_hash") != _hash(note.fields)
                or type(tracked.get("card_version")) is not int or tracked["card_version"] < 1
                or not isinstance(tracked.get("content_hash"), str)
                or not _DIGEST.fullmatch(tracked["content_hash"])):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise AnswerUnavailable("这张卡片尚未安全关联本机 AnkiSense，或已在 Anki 中修改。请先重新同步并核对冲突。") from None
    path = main_window.pm.profile.get(PATH_KEY)
    if not isinstance(path, str) or not path.strip():
        raise AnswerUnavailable("请先在“工具 → AnkiSense · 同步卡片”中选择连接文件并同步。")
    return {"card_id": card_id, "card_version": tracked["card_version"],
            "content_hash": tracked["content_hash"], "server_id": server_id,
            "note_id": note.id, "guid": note.guid, "fields": list(note.fields),
            "review_card_id": card.id, "question": _plain(question),
            "profile_path": profile_path, "connection_path": path.strip()}


def _feedback(attempt):
    diagnosis = attempt.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return "分析结果格式不完整，请回到原文核对。"
    title = {"correct": "回答正确", "incomplete": "还不完整",
             "incorrect": "需要修正", "uncertain": "暂时无法判断"}.get(
                 diagnosis.get("verdict"), "请核对本次反馈")
    snapshot = attempt.get("card_snapshot") or {}
    rubric = {point.get("point_id"): point.get("text", "")
              for point in snapshot.get("rubric", []) if isinstance(point, dict)}
    lines = [title, "", str(diagnosis.get("reason") or "请结合讲义核对。")]
    for key, heading in (("addressed_point_ids", "已涉及的要点"),
                         ("missing_point_ids", "需要补充"),
                         ("contradicted_point_ids", "需要修正")):
        points = diagnosis.get(key) or []
        if points:
            lines.extend(["", heading + "："])
            lines.extend("• " + str(rubric.get(item) or item) for item in points)
    source_ids = set(diagnosis.get("source_block_ids") or [])
    sources = [block for block in attempt.get("source_snapshot", [])
               if isinstance(block, dict) and block.get("block_id") in source_ids]
    if sources:
        lines.extend(["", "讲义依据："])
        for block in sources:
            lines.append(f"第 {block.get('page', '?')} 页 · {block.get('text', '')}")
    if snapshot.get("answer"):
        lines.extend(["", "已审核的参考答案：", str(snapshot["answer"])])
    lines.extend(["", "AI 反馈仅供核对；请自己选择 Anki 的复习评分。"])
    return "\n".join(lines)


def _answer_error(error):
    if isinstance(error, AnswerUnavailable):
        return str(error)
    if isinstance(error, PluginError):
        return {
            "connection_failed": "无法连接本机 AnkiSense。回答已保留；请确认服务运行后检查本次提交。",
            "unauthorized": "连接凭据失效。回答已保留，请在同步窗口重新选择连接文件。",
            "plugin_disabled": "本机插件连接已禁用，请在 AnkiSense 中重新启用。",
            "different_server": "连接到了另一个 AnkiSense 数据目录，请选择原连接文件。",
            "incompatible_protocol": "插件与 AnkiSense 服务版本不兼容，请更新配套版本。",
            "card_changed": "卡片内容已变化。回答已保留，请先重新同步，再对照新题目作答。",
            "not_found": "卡片或回答记录不可用，请检查同步状态。",
            "cloud_confirmation_required": "当前需要云端分析确认。回答未丢失，请再次点击并确认发送范围。",
            "response_too_large": "反馈内容超过插件限制，请在 AnkiSense 中查看该记录。",
        }.get(error.code, "本次分析未完成。回答已保留，请检查连接、卡片版本和服务状态。")
    if isinstance(error, (OSError, ValueError)):
        return "本机连接或同步记录无法读取。回答已保留，请检查连接文件并重新同步。"
    return "本次分析未完成。回答已保留，请检查本机服务后重试。"


class AnswerDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.mw = main_window
        self._collection = main_window.col
        self._context = _review_context(main_window)
        self._journal_context = dict(self._context)
        self._client_id = _client_id(main_window)
        self._busy = False
        self._closed_profile = False
        self._disposed = False
        self._verified = False
        self._blocked = False
        self._ai = {}
        self._stage = "verify"
        self._submitted_text = None
        self._request_id = None
        self._submission_started = False
        self._attempt_id = None
        self._attempt_status = None
        self._retry_count = 0
        self._retry_uncertain = False
        self._cloud_allowed = False
        self._polls = 0
        self._hook_registered = False
        self._journal_error = False
        self._restored_draft = False
        try:
            pending = answer_journal.load(self._context["profile_path"], self._context)
        except (OSError, ValueError):
            pending = None
            self._journal_error = True
            self._blocked = True
        if pending is not None:
            if pending["status"] == "rejected":
                # A version preflight failed before POST. This is only a draft,
                # not an ambiguous paid request, so it can follow a resync.
                self._restored_draft = True
            else:
                self._journal_context.update(card_version=pending["saved_card_version"],
                                             content_hash=pending["saved_content_hash"])
                self._request_id = pending["request_id"]
                self._submitted_text = pending["answer"]
                self._submission_started = True
                self._attempt_id = pending["attempt_id"]
                self._attempt_status = pending["status"]
                self._retry_count = pending["retry_count"]
                # The previous retry may have reached the service before a lost
                # response. Query its state before offering another paid retry.
                self._retry_uncertain = self._attempt_id is not None

        self.setWindowTitle("AnkiSense · 用自己的话回答")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(680, 640)
        layout = QVBoxLayout(self)
        intro = QLabel("先回答当前 Anki 卡片，再查看有讲义依据的反馈。题目图片仍在卡片上。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.question_label = QLabel(self._context["question"] or "当前 AnkiSense 卡片", self)
        self.question_label.setTextFormat(Qt.TextFormat.PlainText)
        self.question_label.setWordWrap(True)
        layout.addWidget(self.question_label)
        layout.addWidget(QLabel("你的回答", self))
        self.answer_edit = QPlainTextEdit(self)
        self.answer_edit.setPlaceholderText("用自己的话解释，不必照抄原文。最多 4000 字。")
        if pending is not None:
            self.answer_edit.setPlainText(pending["answer"])
        layout.addWidget(self.answer_edit, 2)
        self.status_label = QLabel("正在核对卡片版本与本机连接…", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("提交后，分析结果与讲义依据会显示在这里。")
        layout.addWidget(self.details, 3)
        row = QHBoxLayout()
        self.submit_button = QPushButton("提交回答并分析", self)
        self.check_button = QPushButton("检查状态", self)
        self.retry_button = QPushButton("重试分析", self)
        self.new_answer_button = QPushButton("重新作答", self)
        self.close_button = QPushButton("关闭", self)
        for button in (self.submit_button, self.check_button, self.retry_button,
                       self.new_answer_button, self.close_button):
            button.setAutoDefault(False)
            row.addWidget(button)
        layout.addLayout(row)
        self.submit_button.clicked.connect(self._submit)
        self.check_button.clicked.connect(self._check)
        self.retry_button.clicked.connect(self._retry)
        self.new_answer_button.clicked.connect(self._new_answer)
        self.close_button.clicked.connect(self.reject)
        self.finished.connect(self._finished)
        gui_hooks.profile_will_close.append(self._profile_will_close)
        self._hook_registered = True
        self._refresh()
        if self._journal_error:
            self.status_label.setText("上次提交记录无法安全读取；已停止新分析，避免重复提交。请先备份并检查 Anki 资料中的 AnkiSense 回答记录。")
        else:
            QTimer.singleShot(0, self._verify)

    def _current(self):
        if self._closed_profile or self.mw.col is not self._collection:
            return False
        try:
            if Path(self.mw.pm.profileFolder(create=False)).resolve() != self._context["profile_path"]:
                return False
            card = getattr(getattr(self.mw, "reviewer", None), "card", None)
            return (card is not None and card.id == self._context["review_card_id"]
                    and _hash(card.note().fields) == _hash(self._context["fields"]))
        except (OSError, AttributeError):
            return False

    def _discard_if_changed(self):
        if self._current():
            return False
        self._retire()
        return True

    def _retire(self):
        self._closed_profile = True
        self.hide()
        if not self._busy:
            super().reject()

    def _client(self):
        client = PluginClient.from_connection_file(self._context["connection_path"], self._client_id)
        status = client.status()
        if status.get("server_id") != self._context["server_id"]:
            raise PluginError(None, "different_server", "The AnkiSense data directory changed.")
        if status.get("capabilities", {}).get("answer_analysis") is not True:
            raise PluginError(None, "incompatible_protocol", "Answer analysis is unavailable.")
        return client, status

    def _save_pending(self):
        answer_journal.save(self._context["profile_path"], self._journal_context,
                            {"request_id": self._request_id, "answer": self._submitted_text,
                             "attempt_id": self._attempt_id, "status": self._attempt_status,
                             "retry_count": self._retry_count})

    def _clear_pending(self):
        answer_journal.clear(self._context["profile_path"], self._journal_context)

    def _run(self, stage, message, operation, success):
        if self._disposed or self._busy or self._discard_if_changed():
            return
        self._stage = stage
        self._busy = True
        self.status_label.setText(message)
        self._refresh()
        try:
            QueryOp(parent=self, op=lambda _col: operation(), success=success).without_collection().failure(
                self._failed).run_in_background()
        except Exception as error:
            self._failed(error)

    def _verify(self):
        def operation():
            client, status = self._client()
            # A saved but unacknowledged POST must be replayable with the same
            # request ID even if the source card has since acquired a new version.
            if self._request_id is not None:
                return status
            card = client.card(self._context["card_id"], expected_hash=self._context["content_hash"])
            if (card.get("version") != self._context["card_version"]
                    or card.get("guid") != self._context["guid"]
                    or [card.get("fields", {}).get(name) for name in FIELD_ORDER] != self._context["fields"]):
                raise PluginError(409, "card_changed", "The synced Anki card differs from the service.")
            return status
        self._run("verify", "正在核对卡片版本与本机连接…", operation, self._verified_result)

    def _verified_result(self, status):
        self._busy = False
        if self._discard_if_changed():
            return
        self._verified = True
        self._ai = status.get("ai") or {}
        if not self._ai.get("configured") and self._request_id is None:
            self._blocked = True
            self.status_label.setText("云端模型尚未配置，请先在 AnkiSense 的模型设置中完成配置。")
        else:
            mode = "云端模型" if self._ai.get("requires_cloud_confirmation") else "本地演示模式"
            prefix = ("有一份未确认的回答待恢复" if self._request_id else
                      "已恢复旧版本的回答草稿" if self._restored_draft else "卡片版本已核对")
            self.status_label.setText(f"{prefix} · {mode}。AI 反馈不改变 Anki 的复习排期。")
        self._refresh()

    def _confirm_cloud(self):
        if not self._ai.get("requires_cloud_confirmation"):
            return False
        answer = QMessageBox.question(
            self, "确认云端分析",
            "本次分析会把题目、已审核的参考答案、评分要点、关联讲义原文和你的回答发送给已配置的云端模型。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def _submit(self):
        if self._busy or self._blocked or not self._verified:
            return
        if self._discard_if_changed():
            return
        if self._request_id is None:
            answer = self.answer_edit.toPlainText()
            if not answer.strip():
                self.status_label.setText("请先写下你的理解；空白回答不会调用模型。")
                return
            if len(answer) > 4000:
                self.status_label.setText("回答最多 4000 字，请精简后再提交。")
                return
            cloud_allowed = self._confirm_cloud()
            if self._ai.get("requires_cloud_confirmation") and not cloud_allowed:
                self.status_label.setText("已取消云端分析，回答仍在输入框中。")
                return
            self._submitted_text = answer
            self._request_id = uuid4().hex
            self._cloud_allowed = cloud_allowed
            try:
                self._save_pending()
            except (OSError, ValueError):
                self._submitted_text = None
                self._request_id = None
                self._cloud_allowed = False
                self.status_label.setText("无法在当前 Anki 资料中保存提交记录，因此没有发送回答。请检查磁盘空间和资料权限。")
                return
        elif self._ai.get("requires_cloud_confirmation") and not self._cloud_allowed:
            if not self._confirm_cloud():
                return
            self._cloud_allowed = True
        card_id, version = self._context["card_id"], self._journal_context["card_version"]
        answer, request_id, allow_cloud = self._submitted_text, self._request_id, self._cloud_allowed
        first_request = not self._submission_started

        def operation():
            client, status = self._client()
            if status.get("ai", {}).get("requires_cloud_confirmation") and not allow_cloud:
                raise PluginError(409, "cloud_confirmation_required", "Cloud confirmation is required.")
            if first_request:
                try:
                    card = client.card(card_id, expected_hash=self._context["content_hash"])
                except PluginError as error:
                    if error.status not in {404, 409, 422}:
                        raise
                    # This GET failed before any POST. The saved answer may
                    # become a draft after sync, but has not consumed AI work.
                    raise PluginError(409, "card_changed", "The synced card is no longer current.") from None
                if card.get("version") != version:
                    raise PluginError(409, "card_changed", "The card version changed.")
            return client.submit_answer(card_id, version, answer, request_id, allow_cloud=allow_cloud)

        self._submission_started = True
        self._run("submit", "回答正在提交和分析；可继续查看 Anki 卡片。", operation, self._received)

    def _check(self):
        if self._disposed:
            return
        if self._attempt_id is None:
            if not self._verified:
                self._verify()
            else:
                self.status_label.setText("尚无可查询的分析记录；连接中断后可点击“恢复本次提交”。")
            return
        attempt_id = self._attempt_id
        self._run("check", "正在查询本次分析状态…",
                  lambda: self._client()[0].attempt(attempt_id), self._received)

    def _retry(self):
        if (self._busy or self._attempt_status != "failed" or self._attempt_id is None
                or self._retry_count >= 2 or self._retry_uncertain):
            return
        cloud_allowed = self._confirm_cloud()
        if self._ai.get("requires_cloud_confirmation") and not cloud_allowed:
            self.status_label.setText("已取消云端重试，原回答和反馈记录仍保留。")
            return
        attempt_id, count = self._attempt_id, self._retry_count

        def operation():
            client, status = self._client()
            if status.get("ai", {}).get("requires_cloud_confirmation") and not cloud_allowed:
                raise PluginError(409, "cloud_confirmation_required", "Cloud confirmation is required.")
            return client.retry_attempt(attempt_id, count, allow_cloud=cloud_allowed)

        self._run("retry", "正在重试这次已保存的回答…", operation, self._received)

    def _received(self, attempt):
        self._busy = False
        if self._discard_if_changed():
            return
        if (not isinstance(attempt, dict) or not isinstance(attempt.get("id"), str)
                or not _ID.fullmatch(attempt["id"])
                or attempt.get("card_id") != self._context["card_id"]
                or attempt.get("answer") != self._submitted_text
                or attempt.get("status") not in {"pending", "completed", "failed", "unanswered"}
                or type(attempt.get("retry_count")) is not int):
            self._failed(PluginError(None, "invalid_response", "Unexpected answer record."))
            return
        self._attempt_id = attempt["id"]
        self._attempt_status = attempt["status"]
        self._retry_count = attempt["retry_count"]
        self._retry_uncertain = False
        journal_warning = False
        try:
            if self._attempt_status in {"completed", "unanswered"}:
                self._clear_pending()
            else:
                self._save_pending()
        except (OSError, ValueError):
            # The original, already-written request ID remains available for
            # same-ID recovery even when this later journal update fails.
            journal_warning = True
        if self._attempt_status == "pending":
            self.status_label.setText("回答已保存，仍在分析。可以稍后点击“检查状态”。")
            if self._polls < 5:
                self._polls += 1
                QTimer.singleShot(2000, self._check)
        elif self._attempt_status == "completed":
            self.status_label.setText("分析完成。请结合讲义核对，并自行选择 Anki 复习评分。")
            self.details.setPlainText(_feedback(attempt))
        elif self._attempt_status == "failed":
            self.status_label.setText("分析未完成；回答已保存。可以明确点击“重试分析”。")
            self.details.setPlainText(str(attempt.get("error") or "模型分析暂时失败。"))
        else:
            self.status_label.setText("这次没有填写回答，因此没有调用模型。")
        if journal_warning:
            self.status_label.setText(self.status_label.text() + " 本地提交记录更新失败，关闭前请保存这份反馈。")
        self._refresh()

    def _failed(self, error):
        self._busy = False
        if self._discard_if_changed():
            return
        stage = self._stage
        if isinstance(error, PluginError) and error.code == "cloud_confirmation_required":
            self._ai["requires_cloud_confirmation"] = True
            self._cloud_allowed = False
        if isinstance(error, PluginError) and error.code == "card_changed":
            self._blocked = True
            if stage == "submit" and self._attempt_id is None:
                # The GET preflight or the POST version guard confirmed that
                # no attempt was stored. Preserve a resyncable draft.
                try:
                    answer_journal.save(self._context["profile_path"], self._context,
                                        {"request_id": self._request_id,
                                         "answer": self._submitted_text,
                                         "attempt_id": None, "status": "rejected",
                                         "retry_count": 0})
                    self.status_label.setText("卡片已变化，回答已在当前 Anki 资料中保存为草稿。请重新同步后核对新题目。")
                except (OSError, ValueError):
                    self.status_label.setText("卡片已变化，回答仍在窗口中，但本地草稿保存失败。请先复制回答再重新同步。")
                self._refresh()
                return
        if stage == "retry" and isinstance(error, PluginError) and error.code == "connection_failed":
            self._retry_uncertain = True
        self.status_label.setText(_answer_error(error))
        self._refresh()

    def _new_answer(self):
        if self._busy or self._attempt_status not in {"completed", "failed", "unanswered"}:
            return
        if self._retry_uncertain:
            self.status_label.setText("请先检查上一轮分析状态，再开始新回答。")
            return
        try:
            self._clear_pending()
        except (OSError, ValueError):
            self.status_label.setText("无法清除上一轮本地提交记录；已保留原回答，避免下次误恢复。")
            return
        self._submitted_text = None
        self._request_id = None
        self._journal_context = dict(self._context)
        self._submission_started = False
        self._attempt_id = None
        self._attempt_status = None
        self._retry_count = 0
        self._retry_uncertain = False
        self._cloud_allowed = False
        self._polls = 0
        self._restored_draft = False
        self.answer_edit.clear()
        self.details.clear()
        self.status_label.setText("可以重新组织答案并提交一次新分析。")
        self._refresh()

    def _refresh(self):
        available = not self._busy and not self._closed_profile and self._current()
        ready = available and self._verified and not self._blocked
        self.answer_edit.setReadOnly(not ready or self._request_id is not None)
        self.submit_button.setText("恢复本次提交" if self._request_id else "提交回答并分析")
        self.submit_button.setEnabled(ready and self._attempt_id is None)
        self.check_button.setEnabled(available and (self._attempt_id is not None or not self._verified))
        self.retry_button.setEnabled(ready and self._attempt_status == "failed"
                                     and self._retry_count < 2 and not self._retry_uncertain)
        self.new_answer_button.setEnabled(ready and not self._retry_uncertain
                                          and self._attempt_status in {"completed", "failed", "unanswered"})
        self.close_button.setEnabled(not self._busy)

    def _profile_will_close(self):
        self._closed_profile = True
        self.hide()
        if not self._busy:
            QTimer.singleShot(0, self._discard_if_changed)

    def _finished(self, _result):
        self._disposed = True
        if self._hook_registered:
            gui_hooks.profile_will_close.remove(self._profile_will_close)
            self._hook_registered = False
        if getattr(self.mw, "_ankisense_answer_dialog", None) is self:
            self.mw._ankisense_answer_dialog = None

    def reject(self):
        if self._busy:
            self.status_label.setText("分析请求正在进行；可以继续使用 Anki，完成后再关闭此窗口。")
            return
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            self.status_label.setText("分析请求正在进行；可以继续使用 Anki，完成后再关闭此窗口。")
            event.ignore()
            return
        super().closeEvent(event)


def show_answer_dialog(main_window):
    existing = getattr(main_window, "_ankisense_answer_dialog", None)
    if existing is not None:
        if existing._discard_if_changed():
            if existing._busy:
                showWarning("上一张卡片的分析仍在结束，请稍后再打开。", parent=main_window)
                return
        else:
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return
    try:
        dialog = AnswerDialog(main_window)
    except AnswerUnavailable as error:
        showWarning(str(error), parent=main_window)
        return
    except Exception:
        showWarning("无法打开作答窗口。请检查当前 Anki 资料和同步记录。", parent=main_window)
        return
    main_window._ankisense_answer_dialog = dialog
    dialog.show()


def _card_will_show(text, card, kind):
    if kind not in {"reviewQuestion", "reviewAnswer"}:
        return text
    try:
        if card.note().mid != MODEL_ID:
            return text
    except (AttributeError, RuntimeError):
        return text
    hint = "在 Anki 内输入回答；分析需要本机 AnkiSense 服务运行。"
    try:
        _review_context(aqt.mw, card)
    except AnswerUnavailable as error:
        # Keep the action inside Anki and explain the missing association.
        # The dialog validates again before any request; do not silently send
        # the user to a browser when pairing or note validation fails.
        hint = str(error)
    except AttributeError:
        hint = "请在当前 Anki 资料中重新打开卡片，并检查 AnkiSense 同步连接。"

    def has_class(attributes, name):
        match = _CLASS.search(attributes)
        return bool(match and name in html.unescape(next(value for value in match.groups() if value is not None)).split())

    replaced = False
    def native_action(match):
        nonlocal replaced
        if not has_class(match.group("attrs"), "as-practice"):
            return match.group(0)
        replaced = True
        return ('<button type="button" class="as-practice" '
                'style="border:0;font-family:inherit;cursor:pointer" '
                'onclick="pycmd(\'ankisense:answer\'); return false;">在 Anki 内作答</button>')

    # Only the reviewer display changes. The stored template still works in
    # Anki clients without this add-on, including APKG exports and mobile apps.
    text = re.sub(r'<a\b(?P<attrs>[^>]*)>.*?</a\s*>', native_action, text, flags=re.I | re.S)
    if replaced:
        def native_hint(match):
            if (has_class(match.group("attrs"), "as-hint")
                    and _plain(match.group("body")) == "在这台电脑上启动 AnkiSense 后可打开练习。"):
                return '<p class="as-hint">' + html.escape(hint) + '</p>'
            return match.group(0)
        text = re.sub(r'<p\b(?P<attrs>[^>]*)>(?P<body>.*?)</p\s*>', native_hint, text, flags=re.I | re.S)
    return text


def _receive_js_message(handled, message, context):
    if message != COMMAND or not isinstance(context, Reviewer):
        return handled
    card = getattr(context, "card", None)
    if card is None:
        return handled
    try:
        if card.note().mid != MODEL_ID:
            return handled
    except (AttributeError, RuntimeError):
        return handled
    show_answer_dialog(context.mw)
    return (True, None)


def _reviewer_did_show_question(card):
    main_window = getattr(aqt, "mw", None)
    dialog = getattr(main_window, "_ankisense_answer_dialog", None)
    if dialog is not None and card.id != dialog._context["review_card_id"]:
        dialog._retire()


def _reviewer_will_end():
    main_window = getattr(aqt, "mw", None)
    dialog = getattr(main_window, "_ankisense_answer_dialog", None)
    if dialog is not None:
        dialog._retire()


def register(_main_window):
    global _registered
    if _registered:
        return
    gui_hooks.card_will_show.append(_card_will_show)
    gui_hooks.webview_did_receive_js_message.append(_receive_js_message)
    gui_hooks.reviewer_did_show_question.append(_reviewer_did_show_question)
    gui_hooks.reviewer_will_end.append(_reviewer_will_end)
    _registered = True
