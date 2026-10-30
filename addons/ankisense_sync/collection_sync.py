"""Conservative Anki collection writes. Network work happens before this module.

Call only inside Anki's CollectionOp. Every note is committed by one collection
API operation; scheduling and existing deck choices are never written here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4


MODEL_ID = 1786831001
FIELD_ORDER = ["NoteID", "Question", "Answer", "Concept", "SourceFile", "PDFPage", "SourceQuote", "PracticeURL"]
BOOTSTRAP_GUID = "Doezn{*NY}"
BOOTSTRAP_NOTE_ID = "__ankisense-bootstrap-v1__"
MEDIA_NAME = re.compile(r"ankisense-img-[0-9a-f]{32}\.png\Z")


class SyncError(RuntimeError):
    """A fixed, safe Chinese message that may be displayed in Anki."""


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _model_view(model, *, native=False):
    if native:
        return {"id": model["id"], "name": model["name"], "type": model["type"],
                "fields": [field["name"] for field in model["flds"]],
                "sort": model["sortf"], "css": model["css"],
                "templates": [{"name": template["name"], "qfmt": template["qfmt"],
                               "afmt": template["afmt"], "ordinal": template["ord"]}
                              for template in model["tmpls"]]}
    return {"id": model["id"], "name": model["name"], "type": 0 if model["type"] == "standard" else -1,
            "fields": [field["name"] for field in model["fields"]],
            "sort": model["sort_field_index"], "css": model["css"], "templates": model["templates"]}


def _load_state(path, warnings):
    if not path.exists():
        return {"version": 1, "servers": {}, "models": {}}
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if (value.get("version") != 1 or not isinstance(value.get("servers"), dict)
                or not isinstance(value.get("models"), dict)):
            raise ValueError
        for server in value["servers"].values():
            if not isinstance(server, dict) or not isinstance(server.get("cards"), dict):
                raise ValueError
            for entry in server["cards"].values():
                if (not isinstance(entry, dict) or type(entry.get("note_id")) is not int
                        or not isinstance(entry.get("guid"), str)
                        or not isinstance(entry.get("fields_hash"), str)
                        or type(entry.get("card_version", 0)) is not int):
                    raise ValueError
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        warnings.append("本地同步记录无法读取；本次只会接管内容完全相同的已有笔记，差异将列为冲突。")
        # Do not silently replace a damaged file; the caller can retain it for recovery.
        return {"version": 1, "servers": {}, "models": {}, "unreadable": True}


def _save_state(path, state):
    if state.get("unreadable"):
        return False
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _note_ids(col, card):
    # Read-only SQL through Anki's own DB proxy avoids search-language escaping
    # and matches the exact first field, including APKGs imported before pairing.
    return col.db.list("SELECT id FROM notes WHERE guid=? OR "
                       "(mid=? AND substr(flds,1,instr(flds,char(31))-1)=?)",
                       card["guid"], MODEL_ID, card["id"])


def _media_plan(col, card, bundle):
    directory = Path(col.media.dir()).resolve()
    pending = []
    for asset in card["media"]:
        name = asset["filename"]
        if not MEDIA_NAME.fullmatch(name):
            raise SyncError("配图文件名无效，未写入卡片。")
        content = bundle["media"].get(name)
        path = directory / name
        if path.resolve().parent != directory:
            raise SyncError("Anki 中的同名配图位置异常，未覆盖图片。")
        if content is None and bundle.get("local_media", {}).get(name) == asset["sha256"]:
            if _file_matches(path, asset):
                continue
            raise SyncError("已缓存配图在同步期间变化或丢失，请重新同步。")
        if (not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != asset["sha256"]):
            raise SyncError("配图下载不完整或校验失败，未写入卡片。")
        if path.exists():
            try:
                if path.read_bytes() != content:
                    raise SyncError("Anki 中有同名但内容不同的配图，未覆盖图片。")
            except OSError:
                raise SyncError("Anki 中的同名配图无法读取，未写入卡片。") from None
        else:
            pending.append((name, content))
    return pending


def _file_matches(path, asset):
    try:
        if path.is_symlink() or path.stat().st_size != asset["size_bytes"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == asset["sha256"]
    except OSError:
        return False


def sync_inventory(col, entries, server_id, state_path):
    """Read current Anki notes/media, not just an optimistic saved checkpoint."""
    state = _load_state(Path(state_path), [])
    tracked = state["servers"].get(server_id, {}).get("cards", {})
    native = col.models.get(MODEL_ID)
    matching_model = native is not None and state["models"].get(str(MODEL_ID)) == _hash(_model_view(native, native=True))
    directory = Path(col.media.dir()).resolve()
    media, unchanged = {}, []
    for entry in entries:
        for asset in entry["media"]:
            name = asset["filename"]
            if MEDIA_NAME.fullmatch(name) and _file_matches(directory / name, asset):
                media[name] = asset["sha256"]
        baseline = tracked.get(entry["id"])
        if not matching_model or not baseline or baseline.get("content_hash") != entry["content_hash"]:
            continue
        if baseline["guid"] != entry["guid"] or baseline.get("card_version") != entry["version"]:
            continue
        ids = _note_ids(col, entry)
        if ids != [baseline["note_id"]]:
            continue
        note = col.get_note(ids[0])
        if (note.mid == MODEL_ID and note.guid == entry["guid"] and note.fields[0] == entry["id"]
                and _hash(note.fields) == baseline["fields_hash"]
                and all(media.get(asset["filename"]) == asset["sha256"] for asset in entry["media"])):
            unchanged.append(entry["id"])
    return {"media": media, "unchanged_ids": unchanged}


def _install_model(col, remote):
    """Import a disposable note so Anki itself creates the stable APKG model ID."""
    from anki.import_export_pb2 import ImportAnkiPackageRequest

    if col.db.scalar("SELECT 1 FROM notes WHERE guid=?", BOOTSTRAP_GUID):
        raise SyncError("发现保留的模板初始化笔记，请先检查该笔记；未删除现有内容。")
    path = Path(__file__).with_name("bootstrap.apkg")
    if not path.is_file():
        raise SyncError("插件缺少模板初始化资源，请重新安装完整插件包。")
    options = col._backend.get_import_anki_package_presets()
    # The user's last APKG import preferences must not alter any deck options
    # or merge this fixed model into an unrelated note type during bootstrap.
    options.with_scheduling = False
    options.with_deck_configs = False
    options.merge_notetypes = False
    col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path), options=options))
    identifiers = col.db.list("SELECT id FROM notes WHERE guid=?", BOOTSTRAP_GUID)
    if len(identifiers) != 1:
        raise SyncError("模板初始化未完成，已停止同步。")
    marker = col.get_note(identifiers[0])
    if marker.mid != MODEL_ID or marker.fields[0] != BOOTSTRAP_NOTE_ID:
        raise SyncError("模板初始化内容不匹配，已停止同步。")
    col.remove_notes(identifiers)
    installed = col.models.get(MODEL_ID)
    if installed is None or _model_view(installed, native=True) != remote:
        raise SyncError("插件模板资源与服务模板不一致，请更新插件后再同步。")
    return installed


def apply_bundle(col, bundle, deck_id, state_path):
    """Apply a fully downloaded bundle, preserving identities and user changes.

    Returned ``changes`` is an Anki OpChanges for CollectionOp. Conflicts are
    skipped; a runtime write failure stops the batch and reports prior successes.
    External state is a checkpoint, not authority over the current collection:
    deleted or undone notes are never silently recreated or overwritten.
    """
    from anki.collection_pb2 import OpChanges

    result = {"added": 0, "updated": 0, "unchanged": 0, "conflicts": [], "warnings": [], "changes": OpChanges()}
    try:
        if type(deck_id) is not int or col.decks.get(deck_id, default=False) is None:
            raise SyncError("目标牌组不存在，请重新选择牌组。")
        if (not isinstance(bundle["server_id"], str) or not isinstance(bundle["cards"], list)
                or not isinstance(bundle["media"], dict)):
            raise ValueError
        cards = bundle["cards"]
        if len({card["id"] for card in cards}) != len(cards) or len({card["guid"] for card in cards}) != len(cards):
            raise ValueError
        for card in cards:
            if (card["field_order"] != FIELD_ORDER or set(card["fields"]) != set(FIELD_ORDER)
                    or card["fields"]["NoteID"] != card["id"] or not card["fields"]["Question"]
                    or card["model"]["id"] != MODEL_ID
                    or any(not isinstance(value, str) or "\x1f" in value or "\x00" in value for value in card["fields"].values())):
                raise ValueError
        remote = _model_view(cards[0]["model"]) if cards else None
        if any(_model_view(card["model"]) != remote for card in cards):
            raise ValueError
        if remote and (remote["type"] != 0 or remote["fields"] != FIELD_ORDER
                       or len(remote["templates"]) != 1 or remote["templates"][0]["ordinal"] != 0):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SyncError("同步数据格式不完整，未写入 Anki。") from None

    path = Path(state_path)
    state = _load_state(path, result["warnings"])
    server = state["servers"].setdefault(bundle["server_id"], {"cards": {}})
    tracked = server["cards"]
    present = set(bundle.get("catalog_ids", [card["id"] for card in cards]))
    scope = bundle.get("scope_document_id")
    absent = {identifier for identifier, item in tracked.items() if identifier not in present
              and (not scope or item.get("document_id") == scope)}
    if absent:
        result["warnings"].append(f"有 {len(absent)} 张已同步笔记未出现在当前批准清单，可能已撤销批准或来源过期；保留原笔记，请回到工作台核对。")
    if bundle.get("excluded"):
        result["warnings"].append(f"服务端另有 {len(bundle['excluded'])} 张卡片因校验未通过而未同步。")
    if not cards:
        return result

    if col.db.scalar("SELECT 1 FROM notes WHERE guid=?", BOOTSTRAP_GUID):
        result["conflicts"].append("发现保留的模板初始化笔记，请先检查该笔记；未删除或修改现有内容。")
        return result

    native_model = col.models.get(MODEL_ID)
    model_update = False
    if native_model is not None:
        native = _model_view(native_model, native=True)
        if native != remote:
            previous = state["models"].get(str(MODEL_ID))
            # Schema changes can create/delete cards. Only presentation updates
            # are allowed, and only when the old presentation is still untouched.
            same_schema = all(native[key] == remote[key] for key in ("id", "type", "fields", "sort"))
            same_ordinals = [item["ordinal"] for item in native["templates"]] == [item["ordinal"] for item in remote["templates"]]
            if previous != _hash(native) or not same_schema or not same_ordinals:
                result["conflicts"].append("AnkiSense 笔记类型或模板已在 Anki 中改动，未覆盖模板和笔记。")
                return result
            model_update = True

    plans = []
    for card in cards:
        label = card["id"]
        baseline = tracked.get(label)
        try:
            if baseline and card["version"] < baseline.get("card_version", 0):
                raise SyncError("服务卡片版本早于上次同步，未用旧内容覆盖笔记。")
            media = _media_plan(col, card, bundle)
            identifiers = _note_ids(col, card)
            if baseline and baseline["note_id"] not in identifiers:
                # Distinguish deletion/undo from a matching live GUID moved by an import.
                if not identifiers:
                    raise SyncError("原笔记已删除或已撤销同步，未自动重新创建。")
            if len(identifiers) > 1:
                raise SyncError("存在重复 GUID 或 NoteID，请先整理重复笔记。")
            note = col.get_note(identifiers[0]) if identifiers else None
            incoming = [card["fields"][name] for name in FIELD_ORDER]
            if note is not None:
                if note.mid != MODEL_ID or note.guid != card["guid"] or note.fields[0] != label:
                    raise SyncError("已有笔记的身份或笔记类型不匹配，未接管该笔记。")
                for other_id, other in state["servers"].items():
                    if other_id != bundle["server_id"] and any(
                            entry["note_id"] == note.id or entry["guid"] == note.guid for entry in other["cards"].values()):
                        raise SyncError("该笔记已关联另一个 AnkiSense 资料目录，未覆盖原关联。")
                if note.fields != incoming:
                    if not baseline:
                        raise SyncError("已有 APKG 笔记与服务内容不同；首次关联只接管内容完全相同的笔记。")
                    if baseline["guid"] != note.guid or baseline["fields_hash"] != _hash(note.fields):
                        raise SyncError("该笔记已在 Anki 中编辑或撤销，未覆盖本地内容。")
                action = "unchanged" if note.fields == incoming else "updated"
            else:
                action = "added"
            plans.append((card, note, incoming, media, action))
        except SyncError as error:
            result["conflicts"].append(f"{label}：{error}")

    if not plans:
        return result
    undo_target = None
    state_warning = False

    def begin_undo():
        nonlocal undo_target
        if undo_target is None:
            undo_target = col.add_custom_undo_entry("AnkiSense 同步")

    def merge_undo():
        nonlocal undo_target
        if undo_target is None:
            return
        try:
            # Anki's undo history is bounded. Keep our marker at the top as we
            # write, instead of letting a large batch evict it before merging.
            result["changes"] = col.merge_undo_entries(undo_target)
        except Exception:
            # Merging is bookkeeping after committed writes, not a rollback.
            # Return the real counts and invalidate views even if a hook or
            # another add-on cleared the undo history during this operation.
            undo_target = None
            result["changes"] = OpChanges(card=True, note=True, deck=True, tag=True,
                                          notetype=True, browser_table=True,
                                          browser_sidebar=True, note_text=True,
                                          study_queues=True)
            result["warnings"].append("同步撤销记录未能合并，已停止后续卡片；已经写入的卡片已保留。可重新同步继续，本批内容可能需要分多次撤销。")
            result["stopped"] = True

    def persist():
        nonlocal state_warning
        if not _save_state(path, state) and not state_warning:
            result["warnings"].append("同步记录未能保存；本次已成功的笔记仍保留，下次将保守核对已有内容。")
            state_warning = True

    try:
        if native_model is None:
            begin_undo()
            try:
                native_model = _install_model(col, remote)
            except Exception:
                # No real cards have been touched yet. Undo all bootstrap writes,
                # including its temporary note, rather than leaving a half setup.
                col.merge_undo_entries(undo_target)
                col.undo()
                undo_target = None
                raise SyncError("模板初始化失败，已撤销初始化；请检查插件资源后重试。") from None
        elif model_update:
            begin_undo()
            updated = copy.deepcopy(native_model)
            updated["name"], updated["css"] = remote["name"], remote["css"]
            for template, replacement in zip(updated["tmpls"], remote["templates"]):
                for key in ("name", "qfmt", "afmt"):
                    template[key] = replacement[key]
            col.models.update_dict(updated)
            native_model = col.models.get(MODEL_ID)
        state["models"][str(MODEL_ID)] = _hash(remote)
        merge_undo()
        if result.get("stopped"):
            return result
        for card, note, incoming, media, action in plans:
            committed = False

            def checkpoint():
                nonlocal committed
                result[action] += 1
                tracked[card["id"]] = {"guid": card["guid"], "note_id": note.id,
                                       "fields_hash": _hash(incoming), "card_version": card["version"],
                                       "content_hash": card["content_hash"],
                                       "document_id": card.get("source", {}).get("document_id")}
                server["snapshot_id"] = bundle["snapshot_id"]
                committed = True
                persist()

            try:
                media = _media_plan(col, card, bundle)
                for filename, content in media:
                    # Recheck after planning; write_data itself never overwrites a
                    # differing file, and its renamed return value must be honored.
                    current = Path(col.media.dir()) / filename
                    if current.exists() and current.read_bytes() != content:
                        raise SyncError("同名配图刚刚发生变化，已停止后续写入。")
                    actual = col.media.write_data(filename, content)
                    if actual != filename:
                        raise SyncError("Anki 调整了配图文件名，已停止后续写入。")
                if action == "added":
                    begin_undo()
                    note = col.new_note(native_model)
                    note.guid = card["guid"]
                    note.fields = incoming
                    note.tags = list(card["tags"])
                    col.add_note(note, deck_id)
                elif action == "updated":
                    begin_undo()
                    note.fields = incoming
                    # Preserve the user's local tags and deck; incoming AnkiSense
                    # tags may be added without removing user organization.
                    note.tags = sorted(set(note.tags) | set(card["tags"]))
                    col.update_note(note)
                checkpoint()
            except Exception:
                # A third-party Anki hook may raise after the atomic write has
                # committed. Re-read instead of reporting that complete note as
                # unwritten, or losing its safe checkpoint for the next sync.
                if not committed:
                    try:
                        identifiers = _note_ids(col, card)
                        if len(identifiers) == 1:
                            saved = col.get_note(identifiers[0])
                            if saved.mid == MODEL_ID and saved.guid == card["guid"] and saved.fields == incoming:
                                note = saved
                                checkpoint()
                    except Exception:
                        pass
                result["warnings"].append("写入中途遇到问题，已停止后续卡片；此前成功的笔记已保留。未引用的新图片可由 Anki 的媒体检查清理。")
                result["stopped"] = True
            finally:
                merge_undo()
            if result.get("stopped"):
                break
    finally:
        merge_undo()
    return result
