"""Small, transactional SQLite store. JSON preserves reviewed card snapshots."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(collection,id))")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS attempt_request ON records(json_extract(payload, '$.request_id')) WHERE collection='attempts'")
            connection.execute("PRAGMA user_version=1")

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
