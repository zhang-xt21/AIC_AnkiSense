"""Portable Anki decks with reviewed evidence, escaped fields and local media."""

import html
import io
import itertools
import json
import re
import sqlite3
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

import genanki
from fastapi import HTTPException

from .evidence import validate_card
from .media import EXPORT_COLUMNS, MAX_EXPORT_BYTES, MAX_UPLOAD_BYTES, html_text, normalize_image, require_media
from .schemas import CardDraft, SourceBlock
from .approval import require_approved_sources, require_image_source

# A released model's ID, field order and template ordinals must remain stable.
MODEL_ID = 1786831001
DECK_ID = 1786831002
MODEL_NAME = "AnkiSense 图文卡片 v1"
DECK_NAME = "AnkiSense"

FRONT = """{{#Question}}<main class="as-card">
  <header><span class="as-brand">AnkiSense</span>{{#Concept}}<span class="as-concept">{{Concept}}</span>{{/Concept}}</header>
  <div class="as-label">先用自己的话回答</div>
  <section class="as-question">{{Question}}</section>
  {{#PracticeURL}}<a class="as-practice" href="{{PracticeURL}}">用自己的话回答 ↗</a>
  <p class="as-hint">在这台电脑上启动 AnkiSense 后可打开练习。</p>{{/PracticeURL}}
</main>{{/Question}}"""

BACK = """{{FrontSide}}
<main class="as-card as-back">
  <hr id="answer">
  <div class="as-label">参考答案</div>
  <section class="as-answer">{{Answer}}</section>
  <details class="as-source"><summary>查看讲义依据</summary>
    <p class="as-source-name">{{SourceFile}}{{#PDFPage}} · PDF 第 {{PDFPage}} 页{{/PDFPage}}</p>
    <blockquote>{{SourceQuote}}</blockquote>
  </details>
</main>"""

CSS = """
.card { margin: 0; padding: 24px 16px; background: #f3f5f1; color: #233c34;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 20px; line-height: 1.7; text-align: left; overflow-wrap: anywhere; }
.as-card { box-sizing: border-box; max-width: 760px; margin: 0 auto; padding: 32px;
  border: 1px solid #dde5dc; border-radius: 18px; background: #fff; }
.as-card header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 28px; }
.as-brand { font-size: 15px; font-weight: 700; letter-spacing: .04em; color: #41725a; }
.as-concept { padding: 3px 10px; border-radius: 6px; background: #edf3e9; font-size: 14px; }
.as-label { margin-bottom: 10px; font-size: 13px; color: #66796e; }
.as-question { font-size: 24px; font-weight: 650; }
.as-answer { margin-bottom: 24px; }
.as-figure { margin: 22px 0; }
.as-card img { display: block; max-width: 100%; height: auto; margin: 0 auto; border-radius: 8px; }
.as-figure figcaption { margin-top: 8px; font-size: 14px; font-weight: 400; color: #66796e; }
.as-practice { display: inline-block; margin-top: 24px; padding: 9px 16px; border-radius: 8px;
  background: #41725a; color: white; text-decoration: none; font-size: 16px; font-weight: 600; }
.as-practice:focus-visible, .as-source summary:focus-visible { outline: 3px solid #a3bc82; outline-offset: 3px; }
.as-hint { margin: 8px 0 0; font-size: 12px; color: #66796e; }
.as-back { margin-top: 16px; }
#answer { height: 0; border: 0; margin: 0; }
.as-source { border-top: 1px solid #dde5dc; padding-top: 16px; font-size: 16px; }
.as-source summary { cursor: pointer; color: #41725a; }
.as-source-name { font-size: 13px; color: #66796e; }
.as-source blockquote { margin: 12px 0 0; padding-left: 16px; border-left: 3px solid #a5b99b; }
.nightMode.card, .night_mode.card { background: #17211d; color: #e5ece4; }
.nightMode .as-card, .night_mode .as-card { background: #222e27; border-color: #425047; }
.nightMode .as-concept, .night_mode .as-concept { background: #354739; }
.nightMode .as-label, .nightMode .as-hint, .nightMode figcaption, .nightMode .as-source-name,
.night_mode .as-label, .night_mode .as-hint, .night_mode figcaption, .night_mode .as-source-name { color: #b4c2b5; }
.nightMode .as-brand, .nightMode summary, .night_mode .as-brand, .night_mode summary { color: #b5d49d; }
.nightMode .as-source, .night_mode .as-source { border-color: #425047; }
@media (max-width: 480px) { .card { padding: 12px 8px; font-size: 18px; }
  .as-card { padding: 22px 18px; border-radius: 12px; } .as-question { font-size: 21px; } }
"""


class _Snapshot:
    def __init__(self, connection):
        self.connection = connection

    def all(self, collection):
        return [json.loads(row[0]) for row in self.connection.execute(
            "SELECT payload FROM records WHERE collection=? ORDER BY rowid", (collection,))]

    def get(self, collection, identifier):
        row = self.connection.execute("SELECT payload FROM records WHERE collection=? AND id=?",
                                      (collection, identifier)).fetchone()
        return json.loads(row[0]) if row else None


@contextmanager
def export_snapshot(store):
    """One read snapshot keeps cards, approvals and source revisions consistent."""
    with store.connect() as connection:
        connection.execute("BEGIN")
        yield _Snapshot(connection)


def _text(value):
    # Anki parses [sound:...] and LaTeX markers before HTML rendering. Entity
    # encode '[' as well, so user text cannot become an Anki media instruction.
    # The ASCII unit separator is reserved for Anki's internal field delimiter.
    return html_text(str(value).replace("\x1f", "\ufffd").replace("\x00", "\ufffd")).replace("[", "&#91;")


def _practice_url(value, identifier):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"}
                 and re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", parsed.netloc)
                 and (parsed.port is None or 1 <= parsed.port <= 65535)
                 and parsed.path == "/" and not parsed.fragment
                 and parse_qs(parsed.query, strict_parsing=True) == {"card": [identifier]}
                 and not re.search(r"[\s\\<>\"']", value))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(422, "练习链接必须指向本机 AnkiSense，请通过 localhost 或 127.0.0.1 导出。")
    return html.escape(value, quote=True)


def require_exportable(card, store):
    """Reject an approved card whose evidence is no longer reviewable.

    All export formats use this same check against the same database snapshot.
    """
    if card.get("status") != "approved":
        raise HTTPException(409, "卡片尚未批准，请先到卡片审核页确认。")
    if card.get("source_outdated"):
        raise HTTPException(409, "卡片引用的原文已更新，请重新审核后导出。")
    try:
        blocks = []
        # Several distinct quotations may legitimately cite the same block.
        # Load each source once; validate_card still checks every quotation.
        for identifier in dict.fromkeys(evidence["block_id"] for evidence in card["evidence"]):
            block = store.get("blocks", identifier)
            if block is None or block["document_id"] != card["document_id"]:
                raise HTTPException(409, "卡片引用的原文缺失，请重新审核后导出。")
            blocks.append(SourceBlock.model_validate(block))
        validate_card(CardDraft.model_validate({key: card[key] for key in CardDraft.model_fields
                                               if key in card}), blocks)
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(409, "卡片内容或原文引用已失效，请重新审核后导出。") from error
    require_approved_sources(store, card, blocks)
    for attachment in card.get("images") or []:
        try:
            media = store.get("media", attachment["media_id"])
        except (KeyError, TypeError) as error:
            raise HTTPException(409, "卡片配图信息无效，请重新审核配图。") from error
        if media is None:
            raise HTTPException(409, "卡片配图记录缺失，请重新添加图片后审核。")
        source = media.get("source") or {}
        if not source:
            continue  # A manually uploaded image is not bound to a PDF revision.
        if not isinstance(source, dict):
            raise HTTPException(409, "卡片配图来源无效，请重新审核配图。")
        if source.get("document_id") != card["document_id"]:
            raise HTTPException(409, "卡片配图与讲义不一致，请重新审核配图。")
        require_image_source(store, card, media)


def anki_model():
    """The shared model contract used by package export and plugin sync."""
    return genanki.Model(MODEL_ID, MODEL_NAME,
                         fields=[{"name": name} for name in EXPORT_COLUMNS.split("\t")],
                         templates=[{"name": "理解与回忆", "qfmt": FRONT, "afmt": BACK}],
                         css=CSS, sort_field_index=1)


def media_filename(identifier):
    return f"ankisense-{identifier}.png"


def note_guid(identifier):
    return genanki.guid_for("AnkiSense-card-v1", identifier)


def note_tags(card):
    tags = ["AnkiSense", "补充练习" if card["kind"] == "remediation" else "基础卡片"]
    if card.get("is_demo"):
        tags.append("自编演示")
    return tags


def note_fields(card, values):
    """Render a validated note, keeping all user text inert in Anki."""
    fields = [_text(value) for value in values]
    fields[7] = _practice_url(values[7], card["id"])
    for attachment in card.get("images") or []:
        caption = _text(attachment.get("caption", ""))
        filename = media_filename(attachment["media_id"])
        field = 1 if attachment.get("side", "answer") == "question" else 2
        fields[field] += f'<figure class="as-figure"><img src="{filename}" alt="配图">'
        if caption:
            fields[field] += f"<figcaption>{caption}</figcaption>"
        fields[field] += "</figure>"
    return fields


def build_apkg(rows, store, storage: Path) -> bytes:
    """Build with genanki's DB writer inside an owned, automatically cleaned dir.

    genanki 0.13.1's write_to_file leaves its mkstemp DB behind. Calling the DB
    writer directly avoids both that leak and process-global tempfile patches.
    """
    model = anki_model()
    deck = genanki.Deck(DECK_ID, DECK_NAME)
    assets = {}
    total = len(CSS.encode("utf-8")) + len(FRONT.encode("utf-8")) + len(BACK.encode("utf-8"))
    for card, values in rows:
        if card["status"] != "approved":
            continue
        require_exportable(card, store)
        fields = note_fields(card, values)
        for attachment in card.get("images") or []:
            _, path = require_media(store, storage, attachment["media_id"], card["document_id"])
            identifier = attachment["media_id"]
            if identifier not in assets:
                try:
                    with path.open("rb") as source:
                        content = source.read(MAX_UPLOAD_BYTES + 1)
                except FileNotFoundError as error:
                    raise HTTPException(404, "图片文件不存在，请重新添加图片。") from error
                if len(content) > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "图片文件超过导出限制，请重新添加图片。")
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise HTTPException(422, "卡片图片已损坏，请重新添加图片后再导出。")
                try:
                    png, _, _ = normalize_image(content)
                except HTTPException as error:
                    raise HTTPException(error.status_code, "卡片图片无法读取或超过限制，请重新添加图片后再导出。") from error
                assets[identifier] = png
                total += len(png)
        total += sum(len(field.encode("utf-8")) for field in fields)
        if total > MAX_EXPORT_BYTES:
            raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
        deck.add_note(genanki.Note(model=model, fields=fields,
                                  guid=note_guid(card["id"]), tags=note_tags(card),
                                  due=len(deck.notes) + 1))
    if not deck.notes:
        raise HTTPException(422, "还没有已批准的卡片，请先到卡片审核页批准卡片。")
    with TemporaryDirectory(prefix="ankisense-apkg-") as temporary:
        database = Path(temporary) / "collection.anki2"
        timestamp = time.time()
        connection = sqlite3.connect(database)
        try:
            genanki.Package(deck).write_to_db(connection.cursor(), timestamp, itertools.count(int(timestamp * 1000)))
            connection.commit()
        finally:
            connection.close()
        if database.stat().st_size + sum(map(len, assets.values())) > MAX_EXPORT_BYTES:
            raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database, "collection.anki2")
            media = {str(index): media_filename(identifier) for index, identifier in enumerate(assets)}
            archive.writestr("media", json.dumps(media, ensure_ascii=False))
            for index, content in enumerate(assets.values()):
                archive.writestr(str(index), content)
        if output.tell() > MAX_EXPORT_BYTES:
            raise HTTPException(413, "导出包超过 50 MiB，请减少本次已审核卡片或图片。")
        return output.getvalue()
