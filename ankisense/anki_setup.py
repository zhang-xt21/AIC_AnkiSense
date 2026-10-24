"""Local Anki onboarding. Only this add-on is written; collections stay in Anki."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .plugin_api import local_base_url

ADDON_VERSION = "0.2.0"
ADDON_FILES = ("__init__.py", "ui.py", "onboarding.py", "answer_ui.py", "answer_journal.py", "client.py",
               "transport.py", "collection_sync.py", "manifest.json", "config.json", "config.md", "bootstrap.apkg")
ADDON_ROOT = Path(__file__).resolve().parents[1] / "addons" / "ankisense_sync"


def ordinary(path):
    path = Path(path).absolute()
    if any(item.is_symlink() or item.is_junction() for item in (path, *path.parents)):
        raise ValueError("请选择普通目录，插件安装不支持符号链接或目录联接。")
    return path


def anki_running():
    if os.name != "nt":
        return False
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "if (Get-Process -Name anki -ErrorAction SilentlyContinue) { 'running' }"],
        capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError("无法检查 Anki 是否正在运行，请退出 Anki 后重试。")
    return b"running" in result.stdout


def addon_payload():
    return {name: (ADDON_ROOT / name).read_bytes() for name in ADDON_FILES}


def install_addon(base, connection, storage):
    """Atomic per-file updates with a recoverable backup of the previous add-on."""
    base = ordinary(base)
    if not base.is_dir() or not (base / "prefs21.db").is_file():
        raise ValueError("请选择 Anki 资料根目录（含 prefs21.db），不要选择某个牌组、用户资料或程序目录。请先启动一次 Anki 完成初始化。")
    target = ordinary(base / "addons21" / "ankisense_sync")
    if anki_running():
        raise HTTPException(409, "Anki 正在运行，请正常退出 Anki 后再安装插件；不会强制关闭你的 Anki。")
    payload = addon_payload()
    value = connection.read()
    if not value or not value["enabled"]:
        raise ValueError("请先启用本机连接。")
    config = json.loads(payload["config.json"])
    config.update(connection_file=str(connection.path), setup_id=value["server_id"])
    payload["config.json"] = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
    if target.exists():
        if not target.is_dir():
            raise ValueError("插件安装位置被同名文件占用。")
        for file in target.rglob("*"):
            ordinary(file)
        manifest = target / "manifest.json"
        if not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")).get("package") != "ankisense_sync":
            raise ValueError("目标目录不是可识别的 AnkiSense 插件，未覆盖任何文件。")
        if all((target / name).is_file() and (target / name).read_bytes() == content for name, content in payload.items()):
            return {"installed": True, "changed": False, "addon_version": ADDON_VERSION, "target": str(target)}
        backups = Path(storage) / "anki" / "addon-backups"
        backups.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(backups / (uuid4().hex + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
            for file in target.rglob("*"):
                if file.is_file():
                    archive.write(file, str(file.relative_to(target)))
    target.mkdir(parents=True, exist_ok=True)
    for name, content in payload.items():
        destination = ordinary(target / name)
        temporary = target / ("." + name + "." + uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {"installed": True, "changed": True, "addon_version": ADDON_VERSION, "target": str(target)}


class AnkiSetupService:
    def __init__(self, store, storage, connection):
        self.store, self.storage, self.connection = store, storage, connection
        self.router = APIRouter(prefix="/api/anki/setup", tags=["Anki setup"])
        self.router.add_api_route("", self.status, methods=["GET"])
        self.router.add_api_route("/enable", self.enable, methods=["POST"])
        self.router.add_api_route("/install", self.install, methods=["POST"])
        self.router.add_api_route("/addon", self.download, methods=["GET"])

    def status(self):
        current = self.connection.read()
        fingerprint = hashlib.sha256(current["token"].encode()).hexdigest() if current else None
        clients = [item for item in self.store.all("anki_clients")
                   if current and item.get("connection_id") == fingerprint]
        clients.sort(key=lambda item: item["last_seen"], reverse=True)
        connected = bool(current and current["enabled"] and any(time.time() - item["last_seen"] < 90 for item in clients))
        syncs = sorted((item["last_sync"] for item in clients if item.get("last_sync")), key=lambda item: item["at"], reverse=True)
        installed = self.store.get("anki_setup", "installation")
        return {"connection": self.connection.public(), "connection_file": str(self.connection.path) if current else None,
                "connected": connected, "last_seen": clients[0]["last_seen"] if clients else None,
                "plugin_version": clients[0].get("plugin_version") if clients else None,
                "last_sync": syncs[0] if syncs else None, "installation": installed,
                "addon_version": ADDON_VERSION, "addon_available": all((ADDON_ROOT / name).is_file() for name in ADDON_FILES)}

    async def body(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise HTTPException(415, "请从连接向导提交操作。")
        content = await request.body()
        if len(content) > 8192:
            raise HTTPException(413, "连接设置过长。")
        try:
            body = json.loads(content)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except ValueError:
            raise HTTPException(422, "连接设置格式不正确。") from None

    async def enable(self, request: Request):
        if await self.body(request):
            raise HTTPException(422, "启用连接不需要额外参数。")
        try:
            self.connection.configure(base_url=local_base_url(str(request.base_url).rstrip("/")))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return self.status()

    async def install(self, request: Request):
        body = await self.body(request)
        if set(body) != {"anki_base"} or not isinstance(body["anki_base"], str) or not body["anki_base"] or len(body["anki_base"]) > 2048:
            raise HTTPException(422, "请选择 Anki 资料根目录。")
        import asyncio
        try:
            result = await asyncio.to_thread(install_addon, body["anki_base"], self.connection, self.storage)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(422, "插件未安装。请确认目录含 prefs21.db、没有目录联接，且 Anki 已正常退出。") from None
        self.store.put("anki_setup", {"id": "installation", **result, "at": time.time()})
        return result

    def download(self):
        output = io.BytesIO()
        try:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in addon_payload().items():
                    archive.writestr(name, content)
        except OSError:
            raise HTTPException(503, "插件文件不完整，请更新完整的 AnkiSense 安装包。") from None
        return Response(output.getvalue(), media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="AnkiSense-Sync.ankiaddon"'})
