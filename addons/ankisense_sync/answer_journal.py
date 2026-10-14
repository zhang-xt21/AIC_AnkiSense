"""Private, per-card journal for an Anki answer awaiting confirmation.

The original request ID and answer must survive a lost HTTP response so the
same submission can be replayed without starting another model analysis.
Cloud consent is deliberately never stored here.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


MAX_BYTES = 64 * 1024
_FORMAT_VERSION = 1
_UNSAFE_MESSAGE = "Answer journal is unreadable or unsafe"
_SERVER = re.compile(r"as-[0-9a-f]{32}\Z")
_CARD = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST = re.compile(r"[a-zA-Z0-9_-]{8,100}\Z")
_ATTEMPT = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
_STATUSES = {"pending", "completed", "failed", "unanswered", "rejected"}
_RECORD_KEYS = {"request_id", "answer", "attempt_id", "status", "retry_count"}


def _context(context):
    if not isinstance(context, Mapping):
        raise ValueError("Invalid answer context")
    server_id = context.get("server_id")
    card_id = context.get("card_id")
    card_version = context.get("card_version")
    content_hash = context.get("content_hash")
    if (not isinstance(server_id, str) or not _SERVER.fullmatch(server_id)
            or not isinstance(card_id, str) or not _CARD.fullmatch(card_id)
            or type(card_version) is not int or card_version < 1
            or not isinstance(content_hash, str) or not _DIGEST.fullmatch(content_hash)):
        raise ValueError("Invalid answer context")
    return {"server_id": server_id, "card_id": card_id,
            "card_version": card_version, "content_hash": content_hash}


def _record(record):
    if not isinstance(record, Mapping) or set(record) - _RECORD_KEYS:
        raise ValueError("Invalid answer journal record")
    if not {"request_id", "answer", "retry_count"}.issubset(record):
        raise ValueError("Incomplete answer journal record")
    request_id = record["request_id"]
    answer = record["answer"]
    attempt_id = record.get("attempt_id")
    status = record.get("status")
    retry_count = record["retry_count"]
    if (not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id)
            or not isinstance(answer, str) or len(answer) > 4000
            or (attempt_id is not None and (not isinstance(attempt_id, str)
                                            or not _ATTEMPT.fullmatch(attempt_id)))
            or (status is not None and (not isinstance(status, str)
                                        or status not in _STATUSES))
            or (status not in {None, "rejected"} and attempt_id is None)
            or (status == "rejected" and attempt_id is not None)
            or type(retry_count) is not int or not 0 <= retry_count <= 2):
        raise ValueError("Invalid answer journal record")
    try:
        answer.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid answer journal record") from None
    return {"request_id": request_id, "answer": answer, "attempt_id": attempt_id,
            "status": status, "retry_count": retry_count}


def _journal_path(profile_path, context):
    identity = _context(context)
    try:
        directory = Path(profile_path)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError
        directory = directory.resolve(strict=True)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ValueError("Invalid Anki profile directory") from None
    digest = hashlib.sha256(
        (identity["server_id"] + "\0" + identity["card_id"]).encode("ascii")
    ).hexdigest()
    return directory / (".ankisense-answer-" + digest + ".json"), identity


def _regular_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return not (getattr(info, "st_file_attributes", 0) & reparse)


def _inspect_existing(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not _regular_file(info):
        raise ValueError("Unsafe answer journal path")
    return info


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate answer journal field")
        value[key] = item
    return value


def load(profile_path, context):
    """Return a valid answer, None only if absent, or reject unsafe data."""
    path, identity = _journal_path(profile_path, context)
    try:
        before = _inspect_existing(path)
    except (OSError, ValueError):
        raise ValueError(_UNSAFE_MESSAGE) from None
    if before is None:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            after = _inspect_existing(path)
            if (after is None or not _regular_file(opened)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)):
                raise ValueError
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict)
                or set(value) != {"version", *identity, "record"}
                or type(value["version"]) is not int or value["version"] != _FORMAT_VERSION
                or any(value[key] != identity[key] for key in ("server_id", "card_id"))
                or type(value["card_version"]) is not int or value["card_version"] < 1
                or not isinstance(value["content_hash"], str)
                or not _DIGEST.fullmatch(value["content_hash"])):
            raise ValueError
        record = _record(value["record"])
        stale = (value["card_version"] != identity["card_version"]
                 or value["content_hash"] != identity["content_hash"])
        # A submitted request must keep its original card version after a
        # resync, so its request ID can be replayed without a second analysis.
        # The caller may show a new answer only after this record is resolved.
        return {**record, "stale": stale,
                "saved_card_version": value["card_version"],
                "saved_content_hash": value["content_hash"]}
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise ValueError(_UNSAFE_MESSAGE) from None


def save(profile_path, context, record):
    """Atomically write a private journal; reject links and invalid content."""
    path, identity = _journal_path(profile_path, context)
    payload = {"version": _FORMAT_VERSION, **identity, "record": _record(record)}
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Answer journal exceeds size limit")
    _inspect_existing(path)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # os.replace replaces a destination link itself; it never writes to
        # the link target. Still refuse an existing link for a clear contract.
        _inspect_existing(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear(profile_path, context):
    """Delete only this card's regular journal file, never a link target."""
    path, _identity = _journal_path(profile_path, context)
    try:
        if _inspect_existing(path) is not None:
            path.unlink()
    except FileNotFoundError:
        pass
