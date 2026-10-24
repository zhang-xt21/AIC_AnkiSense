"""User cancellation and service failure handling, without paid model calls."""
import asyncio
import threading
import time

import pytest

from ankisense.llm import AIError, Settings
from test_automatic_import import client, lecture_pdf, upload, wait_import


@pytest.mark.parametrize("code,expected_calls", [("connection", 3), ("timeout", 3), ("unavailable", 3),
                                                 ("authentication", 1), ("configuration", 1), ("rate_limit", 1)])
def test_service_outage_stops_remaining_batches(client, code, expected_calls):
    calls = []
    async def fail(*args):
        calls.append(1)
        raise AIError("Safe fixture error", code=code)
    client.app.state.ai.extract_knowledge_visual = fail
    result = wait_import(client, upload(client, lecture_pdf(30)))
    assert result["status"] == "failed" and result["failure"]["code"] == code
    assert result["retryable"] and result["job"]["card_ids"] == []
    assert len(calls) == expected_calls
    assert result["job"]["progress"]["completed"] < result["job"]["progress"]["total"]
    time.sleep(.05)
    assert len(calls) == expected_calls, "Stopped jobs must never silently resume paid requests"


def test_outage_keeps_cards_and_explicit_retry_uses_fixed_configuration(client):
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    calls = []
    failing = True
    async def extract(blocks, focus, images, pages):
        calls.append(pages)
        if pages[0] > 3 and failing:
            raise AIError("offline", code="connection")
        return await original(blocks, focus, images, pages)
    ai.extract_knowledge_visual = extract
    first = wait_import(client, upload(client, lecture_pdf(15)))
    assert first["status"] == "partial" and first["failure"]["code"] == "connection"
    saved = set(first["job"]["card_ids"])
    assert len(saved) == 3
    client.app.state.settings = Settings(mode="cloud", base_url="https://fixed.invalid/v1", model="fixed-fixture", api_key="test-placeholder")
    failing = False
    assert client.post(f"/api/material-imports/{first['id']}/retry").status_code == 200
    result = wait_import(client, first)
    assert result["status"] == "completed" and not result.get("failure")
    assert len(result["job"]["card_ids"]) == 15
    assert saved <= set(result["job"]["card_ids"])
    assert calls.count([1, 2, 3]) == 1


def test_cancel_inflight_model_request_preserves_cards_and_can_resume(client):
    ai = client.app.state.ai
    original = ai.extract_knowledge_visual
    entered, cancelled = threading.Event(), threading.Event()
    calls = []
    async def extract(blocks, focus, images, pages):
        calls.append(pages)
        if pages[0] > 3 and not entered.is_set():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await original(blocks, focus, images, pages)
    ai.extract_knowledge_visual = extract
    record = upload(client, lecture_pdf(7))
    assert entered.wait(5)
    response = client.post(f"/api/material-imports/{record['id']}/cancel")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "cancelled" and result["retryable"]
    assert cancelled.is_set() and len(result["job"]["card_ids"]) == 3
    assert client.post(f"/api/material-imports/{record['id']}/cancel").json()["status"] == "cancelled"
    assert calls == [[1, 2, 3], [4, 5, 6]]
    assert client.post(f"/api/material-imports/{record['id']}/retry").status_code == 200
    result = wait_import(client, record)
    assert result["status"] == "completed" and len(result["job"]["card_ids"]) == 7
    assert calls.count([1, 2, 3]) == 1


def test_cancel_local_parser_does_not_start_cloud_work_or_overwrite_cancelled_status(client, monkeypatch):
    import ankisense.automatic_import as module
    original = module.parse_pdf_document
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def parse(content, **options):
        entered.set()
        assert release.wait(5)
        try:
            options["progress"](1, 2)
            raise AssertionError("A cancelled parser must stop at the page boundary")
        finally:
            exited.set()
    monkeypatch.setattr(module, "parse_pdf_document", parse)
    record = upload(client)
    try:
        assert entered.wait(3)
        result = client.post(f"/api/material-imports/{record['id']}/cancel").json()
        assert result["status"] == "cancelled" and result["retryable"]
    finally:
        release.set()
    assert exited.wait(3)
    assert client.get(f"/api/material-imports/{record['id']}").json()["status"] == "cancelled"
    assert client.app.state.store.all("auto_card_jobs") == []
    monkeypatch.setattr(module, "parse_pdf_document", original)
    assert client.post(f"/api/material-imports/{record['id']}/retry").status_code == 200
    assert wait_import(client, record)["status"] == "completed"


def test_cancel_unknown_or_finished_import_is_safe(client):
    assert client.post("/api/material-imports/unknown/cancel").status_code == 404
    result = wait_import(client, upload(client))
    saved = result["job"]["card_ids"]
    cancelled = client.post(f"/api/material-imports/{result['id']}/cancel").json()
    assert cancelled["status"] == "completed" and cancelled["job"]["card_ids"] == saved


def test_cancelling_queued_import_does_not_stop_the_other_document(client, monkeypatch):
    import ankisense.automatic_import as module
    original = module.parse_pdf_document
    entered, release = threading.Event(), threading.Event()
    calls = []
    def parse(content, **options):
        calls.append(content)
        entered.set()
        assert release.wait(5)
        return original(content, **options)
    monkeypatch.setattr(module, "parse_pdf_document", parse)
    first = upload(client, lecture_pdf(2), "first.pdf")
    try:
        assert entered.wait(3)
        queued = upload(client, lecture_pdf(3), "queued.pdf")
        result = client.post(f"/api/material-imports/{queued['id']}/cancel").json()
        assert result["status"] == "cancelled"
    finally:
        release.set()
    assert wait_import(client, first)["status"] == "completed"
    assert len(calls) == 1
    assert client.get(f"/api/material-imports/{queued['id']}").json()["status"] == "cancelled"
    assert len(client.app.state.store.all("auto_card_jobs")) == 1
