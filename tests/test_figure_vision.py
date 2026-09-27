"""Image checks are explicit, source-bound and never silently approve cards."""
import copy
import io
import json
import subprocess
import types
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest
from PIL import Image

from ankisense import figure_vision as vision
from ankisense.llm import Settings
from test_figure_recommendations import context, completed


GOOD = {"classification": "diagram", "description": "图中展示栈的插入删除操作。", "relation": "supported",
        "completeness": "complete", "legibility": "clear", "answer_leakage": "no", "reasons": ["栈顶箭头与操作标注可见。"]}


@pytest.fixture
def checked_context(context, monkeypatch):
    client, card, adapter, storage = context
    calls = []

    def fake(settings, crop, page, prompt):
        calls.append({"crop": crop, "page": page, "prompt": prompt})
        return {"result": copy.deepcopy(GOOD), "usage": {"total_tokens": 42}}

    monkeypatch.setattr(vision, "run_enrichment", fake)
    monkeypatch.setattr(vision, "_python", lambda: Path(__file__))
    service = client.app.state.image_checks
    service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1", model="fixture-vision", api_key="never-print-test-key")
    job = completed(client, card)
    body = {"job_id": job["id"], "candidate_id": job["candidates"][0]["id"], "card_version": 1, "side": "answer"}
    yield client, card, body, service, calls


def check(c, card, body, **changes):
    return c.post(f"/api/cards/{card['id']}/image-recommendations/check", json={**body, **changes})


def test_explicit_check_runs_docling_adapter_and_cache_never_mutates_cards(checked_context):
    c, card, body, service, calls = checked_context
    assert calls == []
    assert c.get("/api/image-check/status").json()["native_classifier"] is False
    result = check(c, card, body)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["status"] == "checked" and data["verdict"] == "pass" and data["cached"] is False
    assert data["classification"]["source"] == "docling-picture-description-api"
    assert "never-print" not in result.text
    assert len(calls) == 1
    assert calls[0]["crop"] != calls[0]["page"]
    assert card["answer"] in calls[0]["prompt"] and "source_blocks" in calls[0]["prompt"]
    assert check(c, card, body).json()["cached"] is True and len(calls) == 1
    assert c.app.state.store.get("cards", card["id"]) == card
    assert c.app.state.store.all("media") == []


def test_cache_binds_side_crop_answer_rubric_model_and_original_page(checked_context):
    c, card, body, service, calls = checked_context
    first = check(c, card, body).json()["id"]
    assert check(c, card, body, side="question").json()["id"] != first
    assert check(c, card, body, bbox=[.1, .1, .7, .7]).json()["id"] != first
    changed = {**card, "answer": "后进入的元素先出来。"}
    c.app.state.store.put("cards", changed)
    assert check(c, card, body).json()["id"] != first
    changed["rubric"] = [{"point_id": "stack_lifo", "text": "后进先出，表述一致即可"}]
    c.app.state.store.put("cards", changed)
    assert check(c, card, body).json()["id"] != first
    service.settings = Settings(mode="cloud", base_url="https://model.invalid/v1", model="other-vision", api_key="key")
    assert check(c, card, body).json()["id"] != first
    job = service.recommendations._job(body["job_id"])
    path = service.recommendations._page_path(job, 2)
    with Image.open(path) as image:
        image.putpixel((30, 30), (4, 5, 6))
        image.save(path)
    assert check(c, card, body).json()["id"] != first
    assert len(calls) == 7


def test_stale_missing_cross_card_and_invalid_crop_never_call_model(checked_context):
    c, card, body, service, calls = checked_context
    assert check(c, card, body, card_version=2).status_code == 409
    assert check(c, {"id": "other-card"}, body).status_code == 409
    assert check(c, card, body, bbox=[0, 0, .0001, .1]).status_code == 422
    assert check(c, card, body, bbox=[-1, 0, 1, 1]).status_code == 422
    assert check(c, card, body, candidate_id="../secret").status_code == 404
    assert check(c, card, body, side="hidden").status_code == 422
    c.app.state.store.put("cards", {**card, "question": "题目被直接改动。"})
    assert check(c, card, body).status_code == 409
    assert calls == []


@pytest.mark.parametrize("change,side,expected", [
    ({"relation": "unrelated"}, "answer", "attention"),
    ({"completeness": "incomplete"}, "answer", "attention"),
    ({"legibility": "unclear"}, "answer", "attention"),
    ({"answer_leakage": "yes"}, "question", "attention"),
    ({"answer_leakage": "yes"}, "answer", "pass"),
    ({"completeness": "uncertain"}, "answer", "uncertain"),
    ({"answer_leakage": "uncertain"}, "question", "uncertain"),
    ({"classification": "unknown"}, "answer", "uncertain"),
])
def test_verdict_is_derived_not_supplied_by_model(change, side, expected):
    assert vision.verdict({**GOOD, **change}, side) == expected


def test_failure_is_safe_and_not_cached(checked_context, monkeypatch):
    c, card, body, service, calls = checked_context
    def fail(*args):
        raise vision.VisionError("图像检查超时，请稍后重试。")
    monkeypatch.setattr(vision, "run_enrichment", fail)
    response = check(c, card, body)
    assert response.status_code == 502 and "超时" in response.text
    assert response.json()["detail"]["message"] == "图像检查超时，请稍后重试。"
    assert not service.cache.exists()


def test_disabled_service_does_not_call_model(checked_context):
    c, card, body, service, calls = checked_context
    service.settings = Settings(mode="demo")
    assert c.get("/api/image-check/status").json()["available"] is False
    assert check(c, card, body).status_code == 503 and calls == []


def test_corrupted_cached_result_is_rechecked(checked_context):
    c, card, body, service, calls = checked_context
    first = check(c, card, body).json()
    path = service.cache / (first["id"].removeprefix("figcheck-") + ".json")
    path.write_text(json.dumps({**first, "verdict": "auto-approved"}), encoding="utf-8")
    assert check(c, card, body).json()["cached"] is False
    path.write_text("[]", encoding="utf-8")
    assert check(c, card, body).json()["cached"] is False
    assert len(calls) == 3


@pytest.mark.parametrize("output", ["not JSON secret-provider-token", "[]", json.dumps({"result": {**GOOD, "relation": "probably"}})])
def test_worker_invalid_output_never_exposes_raw_response(output, monkeypatch):
    monkeypatch.setattr(vision, "_python", lambda: Path(__file__))
    monkeypatch.setattr(vision.subprocess, "run", lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(vision.VisionError) as error:
        vision.run_enrichment(Settings(mode="cloud", base_url="https://provider.invalid", model="fixture", api_key="private-key"), b"crop", b"page", "prompt")
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_worker_timeout_uses_finite_deadline_and_stdin_credentials(monkeypatch):
    monkeypatch.setattr(vision, "_python", lambda: Path(__file__))
    settings = Settings(mode="cloud", base_url="https://provider.invalid", model="fixture", api_key="private-key", timeout_seconds=10)
    def timeout(argv, **kwargs):
        assert "private-key" not in str(argv)
        assert json.loads(kwargs["input"])["api_key"] == "private-key"
        assert 10 <= kwargs["timeout"] <= 55
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(vision.subprocess, "run", timeout)
    with pytest.raises(vision.VisionError, match="超时"):
        vision.run_enrichment(settings, b"crop", b"page", "prompt")


@pytest.mark.parametrize("url", ["http://cloud.invalid", "https://user:password@cloud.invalid", "https://cloud.invalid?key=secret", "file:///tmp/model"])
def test_invalid_cloud_endpoint_rejected_without_model_call(url):
    assert vision.capability(Settings(mode="cloud", base_url=url, model="fixture", api_key="key"))["available"] is False


def test_changed_card_during_request_is_rejected_without_cache(checked_context, monkeypatch):
    c, card, body, service, calls = checked_context
    entered, release = Event(), Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"result": copy.deepcopy(GOOD), "usage": None}
    monkeypatch.setattr(vision, "run_enrichment", slow)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(check, c, card, body)
        assert entered.wait(5)
        c.app.state.store.put("cards", {**card, "answer": "答案更新了。"})
        release.set()
        assert future.result().status_code == 409
    assert not service.cache.exists()


def test_duplicate_concurrent_check_reuses_one_request(checked_context, monkeypatch):
    c, card, body, service, calls = checked_context
    entered, release = Event(), Event()
    def slow(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"result": copy.deepcopy(GOOD), "usage": None}
    monkeypatch.setattr(vision, "run_enrichment", slow)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(check, c, card, body)
        assert entered.wait(5)
        second = pool.submit(check, c, card, body)
        release.set()
        assert first.result().status_code == second.result().status_code == 200
    assert len(calls) == 1


def test_real_docling_stage_against_local_mock_only():
    """Exercise the installed Docling stage and transport, without a cloud call."""
    if not vision._python().is_file():
        pytest.skip("Optional Docling environment is not installed")
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "mock-vision", "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(GOOD)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    image = io.BytesIO()
    Image.new("RGB", (90, 75), "white").save(image, "PNG")
    try:
        result = vision.run_enrichment(Settings(mode="cloud", base_url=f"http://127.0.0.1:{server.server_port}", model="mock-vision", api_key="fixture-only"),
                                       image.getvalue(), image.getvalue(), "Only test this fixture.")
        assert result["result"] == GOOD and result["usage"]["total_tokens"] == 7
        assert len(requests) == 1
        assert requests[0]["max_tokens"] == 4096 and "thinking" not in requests[0]
        content = requests[0]["messages"][0]["content"]
        assert sum(item["type"] == "image_url" for item in content) == 2
        assert "A CARD CROP" in content[0]["text"] and "B SOURCE PAGE" in content[2]["text"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("status", [429, 302])
def test_real_docling_transport_single_attempt_and_no_redirect(status):
    if not vision._python().is_file():
        pytest.skip("Optional Docling environment is not installed")
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/must-not-follow")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"secret-provider-error-do-not-expose"}')
        def do_GET(self):
            requests.append("GET " + self.path)
            self.send_response(500)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, "PNG")
    try:
        with pytest.raises(vision.VisionError) as error:
            vision.run_enrichment(Settings(mode="cloud", base_url=f"http://127.0.0.1:{server.server_port}", model="mock", api_key="fixture"),
                                  image.getvalue(), image.getvalue(), "Only test this fixture.")
        assert "secret-provider" not in str(error.value)
        assert error.value.diagnostics["http_status"] == status
        assert error.value.diagnostics["error_code"] == "provider_http_error"
        assert requests == ["/chat/completions"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("endpoint,model,disabled", [
    ("https://api.deepseek.com/chat/completions", "deepseek-flash", True),
    ("https://api.deepseek.com/v1/chat/completions", "deepseek-v4-pro", True),
    ("https://api.deepseek.com:443/v1/chat/completions", "deepseek-flash", True),
    ("https://api.deepseek.com/chat/completions", "unlisted-model", False),
    ("https://api.deepseek.com.evil.invalid/chat/completions", "deepseek-flash", False),
    ("https://proxy.invalid/chat/completions", "deepseek-flash", False),
    ("http://127.0.0.1:5000/chat/completions", "deepseek-flash", False),
])
def test_thinking_extension_only_for_documented_official_deepseek(endpoint, model, disabled):
    from tools.docling_enrichment_worker import generation_params
    params = generation_params(endpoint, model)
    assert (params.get("thinking") == {"type": "disabled"}) is disabled
    assert params["max_tokens"] == 4096


def test_safe_diagnostics_drops_provider_strings_and_non_numeric_usage():
    result = vision.safe_diagnostics({"error_code": "truncated", "finish_reason": "length", "http_status": 200,
        "content": "private document content", "api_key": "private-key", "url": "https://private.invalid",
        "usage": {"prompt_tokens": 10, "total_tokens": "secret-token", "reasoning_tokens": 5, "provider_note": "private"}})
    assert result == {"error_code": "truncated", "finish_reason": "length", "http_status": 200,
                      "usage": {"prompt_tokens": 10, "reasoning_tokens": 5}}


@pytest.mark.parametrize("content,reasoning,finish,error_code", [
    (json.dumps(GOOD), "", "length", "truncated"),
    ("", json.dumps(GOOD), "stop", "empty_response"),
    ('{"classification": "diagram"', "", "stop", "invalid_json"),
    (json.dumps(GOOD), "", "content_filter", "generation_incomplete"),
])
def test_real_worker_incomplete_response_diagnostics_only(content, reasoning, finish, error_code):
    if not vision._python().is_file():
        pytest.skip("Optional Docling environment is not installed")
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(1)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "mock-vision", "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                "reasoning_content": reasoning}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2000, "total_tokens": 2003,
                          "completion_tokens_details": {"reasoning_tokens": 1500}}}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(image, "PNG")
    try:
        with pytest.raises(vision.VisionError) as error:
            vision.run_enrichment(Settings(mode="cloud", base_url=f"http://127.0.0.1:{server.server_port}", model="mock", api_key="fixture"),
                                  image.getvalue(), image.getvalue(), "Fixture only.")
        metadata = error.value.diagnostics
        assert metadata["http_status"] == 200 and metadata["finish_reason"] == finish
        assert metadata["error_code"] == error_code and metadata["content_chars"] == len(content)
        assert metadata["usage"]["reasoning_tokens"] == 1500
        assert calls == [1]
        assert GOOD["description"] not in json.dumps(metadata, ensure_ascii=False)
    finally:
        server.shutdown()
        server.server_close()
