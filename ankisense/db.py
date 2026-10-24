"""Small, transactional SQLite store. JSON preserves reviewed card snapshots."""
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


SCHEMA_VERSION = 1
_RECORDS_SQL = (
    "CREATE TABLE records (collection TEXT NOT NULL, id TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY(collection,id))"
)
_ATTEMPT_INDEX_SQL = (
    "CREATE UNIQUE INDEX attempt_request ON records(json_extract(payload, '$.request_id')) "
    "WHERE collection='attempts'"
)


class SchemaVersionError(RuntimeError):
    """The existing database cannot safely be opened with this code version."""


def _check_records_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute("PRAGMA table_info(records)").fetchall()
    expected = [
        ("collection", "TEXT", 1, 1),
        ("id", "TEXT", 1, 2),
        ("payload", "TEXT", 1, 0),
    ]
    actual = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if actual != expected:
        raise SchemaVersionError("数据库 records 表结构不兼容；请使用匹配的版本或先备份并迁移。")


def _check_attempt_index(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT type, tbl_name, sql FROM sqlite_master WHERE name='attempt_request'"
    ).fetchone()
    if existing is None:
        # A compatible older store can be upgraded without touching its records.
        connection.execute(_ATTEMPT_INDEX_SQL)
    else:
        kind, table, sql = existing
        # Match only the two statements shipped by this project. Lowercasing
        # SQL would also lowercase the case-sensitive JSON path literal and
        # could accept an index on a different key.
        known_sql = {_ATTEMPT_INDEX_SQL,
                     _ATTEMPT_INDEX_SQL.replace(" INDEX ", " INDEX IF NOT EXISTS ", 1)}
        if kind != "index" or table != "records" or sql not in known_sql:
            raise SchemaVersionError("数据库 attempt_request 索引结构不兼容；未修改原数据库。")


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # Reject a newer database before acquiring a writer lock or
            # changing its journal mode. Recheck under the lock below.
            with closing(sqlite3.connect(path, timeout=10)) as existing:
                version = existing.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise SchemaVersionError(
                        f"数据库版本为 {version}，当前程序仅支持版本 {SCHEMA_VERSION}；未修改原数据库。"
                    )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise SchemaVersionError(
                    f"数据库版本为 {version}，当前程序仅支持版本 {SCHEMA_VERSION}；未修改原数据库。"
                )
            has_records = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'"
            ).fetchone() is not None
            if version == SCHEMA_VERSION and not has_records:
                raise SchemaVersionError("数据库标记为版本 1，但 records 表不存在；未修改原数据库。")
            if version == 0 and not has_records:
                # Only an empty, new SQLite file can be initialized as version 1.
                objects = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if objects:
                    raise SchemaVersionError("未标记版本的数据库包含未知结构；未修改原数据库。")
                connection.execute(_RECORDS_SQL)
            else:
                _check_records_table(connection)
                if version == 0:
                    # Legacy v0 can be migrated only when it contains the known records table.
                    unknown = connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE name NOT LIKE 'sqlite_%' AND name NOT IN ('records', 'attempt_request')"
                    ).fetchall()
                    if unknown:
                        raise SchemaVersionError("未标记版本的数据库包含未知结构；未修改原数据库。")
            _check_attempt_index(connection)
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # Changing the journal mode is a database write. Do it only after validation.
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def all(self, collection: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM records WHERE collection=? ORDER BY rowid", (collection,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, collection: str, record_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM records WHERE collection=? AND id=?", (collection, record_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, collection: str, record: dict):
        self.put_many([(collection, record)])

    def put_many(self, records: list[tuple[str, dict]], check=None):
        return self.mutate(lambda: records, check=check)

    def mutate(self, build_records, check=None):
        """Read, validate and write one revision under the SQLite writer lock.

        The builder may use get/all (read connections) while this transaction
        prevents another writer from changing their values before commit.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if check:
                check()
            records = build_records()
            connection.executemany(
                "INSERT INTO records(collection,id,payload) VALUES(?,?,?) ON CONFLICT(collection,id) DO UPDATE SET payload=excluded.payload",
                [(collection, record.get("id", record.get("block_id")), json.dumps(record, ensure_ascii=False)) for collection, record in records],
            )
        return records

    def find_request(self, request_id: str):
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM records WHERE collection='attempts' AND json_extract(payload,'$.request_id')=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None
