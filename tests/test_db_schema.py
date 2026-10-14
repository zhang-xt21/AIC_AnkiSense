"""Existing learning data must survive restarts and incompatible versions."""

import sqlite3

import pytest

from ankisense.db import SCHEMA_VERSION, SchemaVersionError, Store


LEGACY_TABLE = (
    "CREATE TABLE records (collection TEXT NOT NULL, id TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY(collection,id))"
)


def version(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def test_new_store_is_versioned_and_can_reopen_without_losing_records(tmp_path):
    path = tmp_path / "data" / "ankisense.sqlite3"
    store = Store(path)
    assert version(path) == SCHEMA_VERSION
    store.put("cards", {"id": "one", "question": "如何入栈？"})
    assert Store(path).get("cards", "one") == {"id": "one", "question": "如何入栈？"}
    with sqlite3.connect(path) as connection:
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='attempt_request'"
        ).fetchone()
    assert index and "UNIQUE INDEX" in index[0]


def test_newer_version_is_rejected_before_any_schema_change(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE future_data (marker TEXT)")
        connection.execute("INSERT INTO future_data VALUES ('keep me')")
        connection.execute("PRAGMA user_version=2")
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="版本为 2"):
        Store(path)

    assert path.read_bytes() == original
    assert version(path) == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT marker FROM future_data").fetchone()[0] == "keep me"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='records'"
        ).fetchone() is None


def test_compatible_unversioned_records_are_migrated_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_TABLE)
        connection.execute(
            "INSERT INTO records VALUES (?, ?, ?)",
            ("cards", "old", '{"id":"old","question":"旧卡片"}'),
        )

    store = Store(path)
    assert version(path) == SCHEMA_VERSION
    assert store.get("cards", "old") == {"id": "old", "question": "旧卡片"}


def test_unversioned_unknown_schema_is_left_untouched(tmp_path):
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")
        connection.execute("INSERT INTO unexpected VALUES ('keep me')")
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="未知结构"):
        Store(path)

    assert path.read_bytes() == original
    assert version(path) == 0


def test_version_one_without_expected_table_or_columns_is_rejected(tmp_path):
    path = tmp_path / "broken.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, payload TEXT)")
        connection.execute("INSERT INTO records VALUES ('keep', '{}')")
        connection.execute("PRAGMA user_version=1")
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="records 表结构"):
        Store(path)

    assert path.read_bytes() == original
    assert version(path) == 1


def test_version_one_with_misnamed_index_is_rejected_without_repair(tmp_path):
    path = tmp_path / "wrong-index.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_TABLE)
        connection.execute("CREATE INDEX attempt_request ON records(id)")
        connection.execute("PRAGMA user_version=1")
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="attempt_request 索引"):
        Store(path)

    assert path.read_bytes() == original


def test_legacy_index_spelling_remains_compatible(tmp_path):
    path = tmp_path / "previous-release.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_TABLE)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS attempt_request ON "
            "records(json_extract(payload, '$.request_id')) WHERE collection='attempts'"
        )
        connection.execute("PRAGMA user_version=1")

    Store(path)
    assert version(path) == 1


def test_index_with_different_json_key_is_rejected(tmp_path):
    path = tmp_path / "wrong-json-key.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(LEGACY_TABLE)
        connection.execute(
            "CREATE UNIQUE INDEX attempt_request ON "
            "records(json_extract(payload, '$.REQUEST_ID')) WHERE collection='attempts'"
        )
        connection.execute("PRAGMA user_version=1")
    original = path.read_bytes()

    with pytest.raises(SchemaVersionError, match="attempt_request 索引"):
        Store(path)

    assert path.read_bytes() == original
