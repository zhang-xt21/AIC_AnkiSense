"""Versioned, local API for a future Anki desktop add-on.

The API exposes reviewed content and answer analysis, never Anki scheduling.
Connection credentials are separate from model credentials and can be rotated.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .media import MAX_UPLOAD_BYTES
from .plugin_sync import PluginSync

PREFIX = "/api/plugin/v1"
PROTOCOL_VERSION = 1
CLIENT_PATTERN = r"^[a-zA-Z0-9_-]{8,80}$"
BEARER = HTTPBearer(auto_error=False, description="本机插件连接文件中的 token；不是云端模型 API Key。")


def error(status, code, message):
    return HTTPException(status, {"code": code, "message": message},
                         headers={"WWW-Authenticate": "Bearer"} if status == 401 else None)


def local_base_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost"}
                or url.username is not None or url.password is not None or url.query or url.fragment
                or url.path not in {"", "/"} or url.port == 0 or url.netloc.endswith(":")
                or re.search(r"[\s\x00-\x20\x7f\\]", value)):
            raise ValueError
        return f"{url.scheme}://{url.hostname}" + (f":{url.port}" if url.port is not None else "")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("插件地址必须是 127.0.0.1 或 localhost 上的本机 HTTP(S) 服务。") from None


class PluginConnection:
    def __init__(self, storage: Path):
        self.path = Path(storage) / "plugin-connection.json"

    def read(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                value = json.loads(stream.read(16385))
            if (type(value.get("protocol_version")) is not int or value["protocol_version"] != PROTOCOL_VERSION or type(value.get("enabled")) is not bool
                    or not re.fullmatch(r"as-[0-9a-f]{32}", value.get("server_id", ""))
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{40,128}", value.get("token", ""))):
                raise ValueError
            value["base_url"] = local_base_url(value["base_url"])
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def configure(self, *, enabled=True, rotate=False, base_url=None):
        value = self.read()
        if value is None:
            if self.path.exists():
                raise ValueError("插件连接文件无法读取，请先检查或备份此文件。")
            value = {"protocol_version": PROTOCOL_VERSION, "server_id": "as-" + uuid4().hex,
                     "token": secrets.token_urlsafe(32), "base_url": "http://127.0.0.1:8765"}
        value["enabled"] = enabled
        if rotate:
            value["token"] = secrets.token_urlsafe(32)
        if base_url is not None:
            value["base_url"] = local_base_url(base_url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".plugin-connection-" + uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.public()

    def public(self):
        value = self.read()
        return {"configured": value is not None, "enabled": bool(value and value["enabled"]),
                "protocol_version": PROTOCOL_VERSION, "server_id": value["server_id"] if value else None,
                "base_url": value["base_url"] if value else None}

    def authorize(self, credentials: HTTPAuthorizationCredentials | None = Depends(BEARER),
                  client_id: str | None = Header(default=None, alias="X-AnkiSense-Client",
                                                description="每个 Anki 本地配置固定保存的随机标识，8–80 位字母、数字、下划线或连字符。")):
        value = self.read()
        if not value or not value["enabled"]:
            raise error(503, "plugin_disabled", "插件连接尚未启用，请运行本机连接配置工具。")
        credential = credentials.credentials if credentials else ""
        if (not credentials or credentials.scheme.lower() != "bearer" or not re.fullmatch(r"[a-zA-Z0-9_-]{40,128}", credential)
                or not secrets.compare_digest(credential, value["token"])):
            raise error(401, "unauthorized", "插件连接凭据无效，请重新连接本机 AnkiSense。")
        if not client_id or not re.fullmatch(CLIENT_PATTERN, client_id):
            raise error(400, "invalid_client", "插件需要提供固定的本地配置标识 X-AnkiSense-Client。")
        return {"client_id": client_id, "server_id": value["server_id"], "base_url": value["base_url"]}


class PluginAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card_id: str = Field(min_length=1, max_length=100)
    card_version: int = Field(ge=1, strict=True)
    answer: str = Field(max_length=4000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    allow_cloud: bool = Field(default=False, strict=True)


class PluginRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_retry_count: int = Field(ge=0, le=2, strict=True)
    allow_cloud: bool = Field(default=False, strict=True)


class PluginAPI:
    def __init__(self, store, storage, settings, attempts):
        self.store, self.settings, self.attempts = store, settings, attempts
        self.connection = PluginConnection(storage)
        self.sync = PluginSync(store, storage)
        self.router = APIRouter(prefix=PREFIX, tags=["Anki plugin v1"])
        authorize = self.connection.authorize

        @self.router.get("/status")
        def status(identity=Depends(authorize)):
            settings = self.settings
            return {"application": "AnkiSense", "protocol_version": PROTOCOL_VERSION,
                    "server_id": identity["server_id"], "client_id": identity["client_id"],
                    "capabilities": {"approved_cards": True, "media_sha256": True, "answer_analysis": True,
                                     "attempt_polling": True, "schedule_write": False, "automatic_deletion": False,
                                     "sync_mode": "full_manifest"},
                    "ai": {"mode": settings.mode, "model": settings.model if settings.mode == "cloud" else "demo-rules-v1",
                           "configured": settings.mode == "demo" or bool(settings.api_key and settings.base_url and settings.model),
                           "requires_cloud_confirmation": settings.mode == "cloud", "images_in_answer_analysis": False},
                    "limits": {"answer_characters": 4000, "retry_count": 2,
                               "recommended_timeout_seconds": settings.timeout_seconds + 30}}

        @self.router.get("/cards")
        async def cards(identity=Depends(authorize)):
            result = await asyncio.to_thread(self.sync.catalog, identity["base_url"])
            return {**result, "server_id": identity["server_id"]}

        @self.router.get("/cards/{card_id}")
        async def card(card_id: str, expected_hash: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"), identity=Depends(authorize)):
            result = await asyncio.to_thread(self.sync.card, card_id, identity["base_url"])
            if expected_hash is not None and result["content_hash"] != expected_hash:
                raise error(409, "card_changed", "卡片已变化，请重新读取同步清单。")
            return result

        @self.router.get("/media/{media_id}")
        async def media(media_id: str, sha256: str = Query(pattern=r"^[0-9a-f]{64}$"), identity=Depends(authorize)):
            def read():
                metadata, path = self.sync.media(media_id, expected_sha256=sha256)
                with path.open("rb") as stream:
                    content = stream.read(MAX_UPLOAD_BYTES + 1)
                if len(content) > MAX_UPLOAD_BYTES or hashlib.sha256(content).hexdigest() != sha256:
                    raise error(409, "media_changed", "配图已变化，请重新同步卡片与图片。")
                return content
            try:
                content = await asyncio.to_thread(read)
            except OSError:
                raise error(404, "media_missing", "配图文件无法读取，请在网页中重新添加。") from None
            return Response(content, media_type="image/png", headers={"ETag": f'"{sha256}"',
                            "Content-Disposition": f'attachment; filename="ankisense-{media_id}.png"'})

        @self.router.post("/attempts")
        async def submit(body: PluginAttempt, identity=Depends(authorize)):
            # Namespace request IDs by profile, and keep them separate from web requests.
            request_key = "anki-" + hashlib.sha256((identity["client_id"] + "\0" + body.request_id).encode()).hexdigest()
            if not self.store.find_request(request_key):
                self.confirm_cloud(body.allow_cloud, body.answer)
            return await attempts.submit(body.card_id, body.answer, request_key,
                                         expected_version=body.card_version, origin="anki", client_id=identity["client_id"])

        @self.router.get("/attempts/{attempt_id}")
        def attempt(attempt_id: str, identity=Depends(authorize)):
            return attempts.get(attempt_id, expected_origin="anki", client_id=identity["client_id"])

        @self.router.post("/attempts/{attempt_id}/retry")
        async def retry(attempt_id: str, body: PluginRetry, identity=Depends(authorize)):
            previous = attempts.get(attempt_id, expected_origin="anki", client_id=identity["client_id"])
            self.confirm_cloud(body.allow_cloud, previous["answer"])
            return await attempts.retry(attempt_id, expected_origin="anki", client_id=identity["client_id"],
                                        expected_retry_count=body.expected_retry_count)

    def confirm_cloud(self, consent, answer):
        if self.settings.mode == "cloud" and answer.strip() and not consent:
            raise error(409, "cloud_confirmation_required", "本次分析会发送题目、参考答案、评分要点、关联原文和你的回答到配置的云端模型。请在插件中确认后提交。")


def install_plugin_error_handlers(app):
    @app.exception_handler(HTTPException)
    async def http_error(request, exception):
        if not request.url.path.startswith(PREFIX + "/"):
            return await http_exception_handler(request, exception)
        detail = exception.detail if isinstance(exception.detail, dict) else {
            "code": {404: "not_found", 409: "conflict", 422: "invalid_content"}.get(exception.status_code, "request_failed"),
            "message": str(exception.detail)}
        return JSONResponse({"detail": detail}, status_code=exception.status_code, headers=exception.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exception):
        if not request.url.path.startswith(PREFIX + "/"):
            return await request_validation_exception_handler(request, exception)
        return JSONResponse({"detail": {"code": "invalid_request", "message": "插件请求格式不正确，请检查字段、版本号及长度。"}}, status_code=422)
