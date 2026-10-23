"""Standalone launcher for the AnkiSense FastAPI backend.

The Electron shell runs this frozen executable in packaged builds instead of
`.venv/Scripts/python.exe -m uvicorn ankisense.app:app`. It accepts the same
--host / --port flags so the shell can keep one spawn path for dev and packaged.
"""

import sys

import uvicorn

from ankisense.app import app


def parse_host_port(argv):
    host, port = "127.0.0.1", 8765
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1
    return host, port


if __name__ == "__main__":
    host, port = parse_host_port(sys.argv)
    uvicorn.run(app, host=host, port=port, log_level="info")
