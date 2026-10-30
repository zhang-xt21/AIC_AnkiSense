"""Check runner guards without reading personal files or opening external sockets."""
import json
import os
from pathlib import Path
import socket
import sqlite3
from threading import Thread

from anki.collection import Collection

test_root = Path(os.environ["ANKISENSE_ANKI_TEST_ROOT"])
runtime_root = Path(os.environ["ANKISENSE_ANKI_RUNTIME_ROOT"])
checks = {}


def denied(name, operation):
    try:
        operation()
    except RuntimeError:
        checks[name] = True
    else:
        raise AssertionError(name + " was unexpectedly permitted")


denied("collection_outside_test_root", lambda: Collection(str(runtime_root.parent / "AnkiData" / "do-not-open.anki2")))
denied("profile_file_read", lambda: (runtime_root.parent / "AnkiData" / "do-not-open.txt").read_bytes())
denied("installation_write", lambda: (runtime_root / "do-not-create.txt").write_text("forbidden"))
denied("database_outside_test_root", lambda: sqlite3.connect(str(test_root.parent / "do-not-create.sqlite3")))
denied("external_dns", lambda: socket.getaddrinfo("example.invalid", 443))
denied("external_numeric_socket", lambda: socket.create_connection(("203.0.113.1", 443), timeout=.1))
allow_loopback = os.environ["ANKISENSE_ANKI_ALLOW_LOOPBACK"] == "1"
if allow_loopback:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        def accept():
            connection, _ = server.accept()
            with connection:
                connection.sendall(b"isolated-loopback-ok")
        thread = Thread(target=accept)
        thread.start()
        with socket.create_connection(server.getsockname(), timeout=2) as client:
            assert client.recv(100) == b"isolated-loopback-ok"
        thread.join(2)
    checks["numeric_loopback_allowed"] = True
    denied("hostname_localhost_denied", lambda: socket.getaddrinfo("localhost", 80))
else:
    denied("default_loopback_denied", lambda: socket.create_connection(("127.0.0.1", 1), timeout=.1))

collection = Collection(str(test_root / "guard-smoke.anki2"))
try:
    assert collection.note_count() == 0
    checks["isolated_collection_allowed"] = True
finally:
    collection.close()
result = {"checks": checks, "passed": len(checks), "allow_loopback": allow_loopback}
(test_root / "runner-guards.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
