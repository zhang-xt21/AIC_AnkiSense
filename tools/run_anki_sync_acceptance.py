"""Start an isolated demo server and test the plugin against real Anki APIs."""
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from PIL import Image
from ankisense.db import Store
from ankisense.demo import seed_records
from ankisense.media import normalize_image, save_media
from ankisense.plugin_api import PluginConnection


def main():
    run = ROOT / "测试记录" / "2026-09-27-Anki插件" / ("live-" + datetime.now().strftime("%H%M%S") + "-" + uuid4().hex[:6])
    storage = run / "server-data"
    storage.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    store = Store(storage / "ankisense.sqlite3")
    store.put_many(seed_records())
    attachments = []
    for side, color in (("question", "green"), ("answer", "blue")):
        buffer = io.BytesIO()
        Image.new("RGB", (160, 90), color).save(buffer, "PNG")
        image = save_media(store, storage, normalize_image(buffer.getvalue()))
        attachments.append({"media_id": image["id"], "side": side, "caption": "插件联调图片"})
    for identifier in ("demo-stack", "demo-queue"):
        card = store.get("cards", identifier)
        card["status"] = "approved"
        if identifier == "demo-stack":
            card["images"] = attachments
        store.put("cards", card)
    connection = PluginConnection(storage)
    connection.configure(base_url=base)
    env = dict(os.environ, AI_MODE="demo", ANKISENSE_DATA_DIR=str(storage), PYTHONUNBUFFERED="1")
    process = None
    try:
        with (run / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "ankisense.app:app", "--host", "127.0.0.1",
                                        "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with httpx.Client(trust_env=False, timeout=2) as http:
                deadline = time.monotonic() + 25
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Isolated server exited.")
                    try:
                        if http.get(base + "/health").json().get("mode") == "demo":
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Isolated server did not start.")
                    time.sleep(.1)
            shell = shutil.which("pwsh") or shutil.which("powershell")
            command = [shell, "-NoProfile", "-File", str(ROOT / "tools/run_anki_python.ps1"),
                       "-Script", str(ROOT / "tools/verify_anki_sync.py"), "-WorkDirectory", str(ROOT),
                       "-DataDirectory", str(run / "anki-profile"), "-ArgumentsJson", json.dumps([str(connection.path)]),
                       "-AllowLoopback"]
            result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            (run / "anki-runtime.log").write_bytes(result.stdout + result.stderr)
            print(result.stdout.decode("utf-8", errors="replace"))
            if result.returncode:
                print(result.stderr.decode("utf-8", errors="replace"))
                raise RuntimeError("Real Anki acceptance failed; see " + str(run))
    finally:
        connection.configure(enabled=False)
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
    print(json.dumps({"run": str(run), "cloud_calls": 0, "owned_server_closed": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
