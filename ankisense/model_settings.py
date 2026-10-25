"""Local model configuration with a credential-free HTTP representation."""

import json
import math
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request

from .llm import AIError, AIService, Settings


MAX_BODY_BYTES = 32768


def _text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label}格式不正确。")
    return value.strip()


def _base_url(value):
    value = _text(value, 2048, "模型接口地址").rstrip("/")
    if not value:
        return value
    try:
        url = urlsplit(value)
        # Access port as well: urllib otherwise accepts malformed port values.
        url.port
        valid = url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
        if (not valid or not url.hostname or url.username or url.password or url.query or url.fragment
                or any(char.isspace() for char in value) or "\\" in value):
            raise ValueError
    except ValueError:
        raise ValueError("模型接口地址须为不含账号、查询参数或片段的 HTTPS 地址，或本机 HTTP 地址。") from None
    if url.path.rstrip("/").endswith("/chat/completions"):
        raise ValueError("请填写 Base URL，不要附加 /chat/completions。")
    return value


def _server(value):
    if not value:
        return None
    try:
        url = urlsplit(value)
        return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return None


def parse_update(value, previous):
    """Do not use request validation errors: they can echo an API key input."""
    required = {"mode", "base_url", "model", "timeout_seconds"}
    if not isinstance(value, dict) or not required.issubset(value) or value.keys() - required - {"api_key", "clear_api_key"}:
        raise ValueError("请填写模式、模型接口地址、模型名称和超时秒数。")
    mode = value["mode"]
    if mode not in ("demo", "cloud"):
        raise ValueError("模型模式只能是 demo 或 cloud。")
    timeout = value["timeout_seconds"]
    if type(timeout) not in (int, float) or not 1 <= timeout <= 180 or not math.isfinite(timeout):
        raise ValueError("超时须为 1 到 180 秒。")
    base_url = _base_url(value["base_url"])
    model = _text(value["model"], 200, "模型名称")
    key = _text(value.get("api_key", ""), 4096, "API Key")
    clear = value.get("clear_api_key", False)
    if type(clear) is not bool or (clear and key):
        raise ValueError("清除密钥和填写新密钥不能同时选择。")
    if previous.api_key and not key and not clear and _server(previous.base_url) != _server(base_url):
        raise ValueError("更换模型服务器时，请填写新服务的 API Key，或先勾选清除已保存的密钥。")
    return Settings(mode=mode, base_url=base_url, model=model,
                    api_key="" if clear else key or previous.api_key, timeout_seconds=float(timeout))


def public_settings(settings):
    # Invalid legacy environment URLs could embed credentials. Never echo them.
    try:
        base_url = _base_url(settings.base_url)
    except ValueError:
        base_url = ""
    return {"mode": settings.mode, "base_url": base_url, "model": settings.model,
            "timeout_seconds": settings.timeout_seconds, "api_key_configured": bool(settings.api_key),
            "configured": settings.mode == "demo" or bool(base_url and settings.model and settings.api_key)}


class ModelSettingsFile:
    def __init__(self, storage):
        self.path = Path(storage) / "model-settings.json"

    def load(self, fallback=None):
        if not self.path.exists():
            return fallback or Settings.from_env()
        try:
            if self.path.stat().st_size > MAX_BODY_BYTES:
                raise ValueError
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_update(data, Settings())
        except (OSError, ValueError, TypeError):
            raise AIError("本机模型设置文件无法读取，请检查 data 目录中的 model-settings.json。") from None

    def save(self, settings):
        temporary = self.path.with_name(".model-settings-" + uuid4().hex + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(settings), stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except OSError:
            raise HTTPException(500, "模型设置未保存，请检查本机数据目录是否可写；原配置保持不变。") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def model_request(request):
    if request.method == "PUT" and request.url.path.startswith("/api/cards/"):
        return True  # Saving a draft now enqueues automatic model review.
    if request.method != "POST":
        return False
    path = request.url.path
    return (path in {"/api/card-drafts", "/api/generation-jobs", "/api/settings/ai/test"} or path in {"/api/attempts", "/api/plugin/v1/attempts"}
            or (path.startswith(("/api/attempts/", "/api/plugin/v1/attempts/")) and path.endswith("/retry"))
            or path == "/api/material-imports"
            or (path.startswith("/api/material-imports/") and path.endswith("/retry"))
            or (path.startswith("/api/documents/") and path.endswith(("/ai-proofread", "/calibration", "/auto-card-jobs")))
            or (path.startswith("/api/auto-card-jobs/") and path.endswith("/retry"))
            or (path.startswith("/api/cards/") and path.endswith("/image-recommendations/check"))
            or (path.startswith("/api/cards/") and path.endswith("/auto-quality-check"))
            or (path.startswith("/api/cards/") and path.endswith("/auto-check"))
            or (path.startswith("/api/cards/") and path.endswith("/auto-illustration"))
            or (path.startswith("/api/cards/") and path.endswith("/auto-review")))


def install_model_settings(app, configuration):
    # A result describes this process's last explicit test, never a live monitor.
    app.state.model_connection = {"status": "unverified"}

    @app.get("/api/settings/ai")
    async def read_settings():
        return {**public_settings(app.state.settings), "connection": app.state.model_connection}

    @app.post("/api/settings/ai/test")
    async def test_connection():
        settings = app.state.settings
        if settings.mode != "cloud" or not public_settings(settings)["configured"]:
            raise HTTPException(422, "请先保存完整的自配 API 设置，再验证连接。")
        if app.state.model_connection["status"] == "testing":
            raise HTTPException(409, "连接验证正在进行，请等待结果。")
        app.state.model_connection = {"status": "testing"}
        started = perf_counter()
        try:
            # A separate adapter keeps the test's usage out of learning tasks.
            await AIService(settings).verify_connection()
            result = {"status": "success", "message": "连接验证成功，模型已返回有效的测试 JSON。"}
        except AIError as error:
            result = {"status": "error", "message": error.message, "code": error.code or "invalid_response"}
        except Exception:
            result = {"status": "error", "message": "连接验证未完成，请稍后重试。", "code": "unavailable"}
        result.update(checked_at=datetime.now(timezone.utc).isoformat(),
                      latency_ms=round((perf_counter() - started) * 1000))
        app.state.model_connection = result
        return result

    @app.put("/api/settings/ai")
    async def update_settings(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "模型设置须使用 JSON 格式提交。")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_BODY_BYTES:
                raise HTTPException(413, "模型设置内容过长。")
        try:
            data = json.loads(content)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "模型设置 JSON 格式不正确。") from None
        try:
            if not isinstance(data, dict):
                raise ValueError("模型设置须为 JSON 对象。")
            resume = data.pop("resume_active_jobs", False)
            if type(resume) is not bool:
                raise ValueError("保存后继续制卡须为开关选项。")
            updated = parse_update(data, app.state.settings)
        except ValueError as error:
            # These validation messages contain fixed text, never submitted values.
            raise HTTPException(422, str(error)) from None
        if (app.state.active_model_requests or app.state.source_proofread.running
                or app.state.image_checks.running or app.state.image_auto_review.running
                or app.state.card_auto_review.running or app.state.generation_jobs.running
                or app.state.automatic_illustrations.running
                or any(item.get("status") == "pending" for item in app.state.store.all("attempts"))):
            raise HTTPException(409, "已有生成、答题分析或独立图片检查在进行，请等待完成后再保存模型设置。")
        active_jobs = [job for job in app.state.store.all("auto_card_jobs") if job.get("status") in {"pending", "running"}]
        pending_imports = [item for item in app.state.store.all("material_imports")
                           if not item.get("job_id") and item.get("status") in {"pending", "running"}]
        if (active_jobs or pending_imports) and not resume:
            raise HTTPException(409, "制卡正在进行；请勾选「保存后用新模型继续当前制卡」，或先取消任务再保存。")
        if (active_jobs or pending_imports) and (updated.mode != "cloud" or not public_settings(updated)["configured"]):
            raise HTTPException(422, "继续制卡需要完整的云端配置；切换演示模式或清除密钥前，请先取消制卡任务。")
        restart_jobs = active_jobs if updated != app.state.settings else []
        app.state.model_settings_switching = True
        try:
            for job in restart_jobs:
                await app.state.auto_cards.cancel(job["id"])
            apply_settings(updated)
        except HTTPException as error:
            if restart_jobs:
                raise HTTPException(error.status_code, "设置未保存；当前制卡已暂停，已保存卡片保留。请修正后保存，并在结果页继续任务。") from None
            raise
        finally:
            app.state.model_settings_switching = False
        result = {**public_settings(updated), "connection": app.state.model_connection}
        if resume:
            resumed, not_resumed = [], []
            # Only jobs active when the user saved are restarted. Existing
            # partial/failed/completed/cancelled jobs remain explicit actions.
            for job in restart_jobs:
                try:
                    await app.state.auto_cards._retry(job["id"], allow_model_change=True)
                    resumed.append(job["id"])
                except Exception:
                    not_resumed.append(job["id"])
            result["task_switch"] = {"resumed": len(resumed), "not_resumed": len(not_resumed),
                                     "preparing": len(pending_imports)}
            if active_jobs and not restart_jobs:
                result["task_switch"]["unchanged"] = len(active_jobs)
        return result

    def apply_settings(updated):
        # New writes are gated during the switch. Persist before replacing
        # adapters so a failed save leaves the runtime configuration intact.
        configuration.save(updated)
        app.state.settings = updated
        app.state.ai = AIService(updated)
        app.state.attempts.settings = updated
        app.state.plugin.settings = updated
        app.state.image_checks.settings = updated
        app.state.image_auto_review.settings = updated
        app.state.card_auto_review.settings = updated
        app.state.model_connection = {"status": "unverified"}
        return {**public_settings(updated), "connection": app.state.model_connection}
