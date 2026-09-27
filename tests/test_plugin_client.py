"""Reference-client contracts against a loopback HTTP server, without Anki/cloud."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from examples import anki_plugin_client as client_module
from examples.anki_plugin_client import PluginClient, PluginError


TOKEN = "mock_plugin_connection_token_" + "x" * 24
CLIENT_ID = "local-profile-one"
SERVER_ID = "as-" + "1" * 32
PREFIX = "/api/plugin/v1"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1kAAAAASUVORK5CYII=")


def status_payload(**changes):
    return {"application": "AnkiSense", "protocol_version": 1, "server_id": SERVER_ID, **changes}


@pytest.fixture
def server():
    state = SimpleNamespace(requests=[], response={"body": status_payload()})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.requests.append({"method": self.command, "path": self.path,
                                   "headers": {key.lower(): value for key, value in self.headers.items()},
                                   "body": body})
            response = dict(state.response)
            content = response.get("body", {})
            if not isinstance(content, bytes):
                content = json.dumps(content, ensure_ascii=False).encode("utf-8")
            if response.get("delay"):
                time.sleep(response["delay"])
            try:
                self.send_response(response.get("status", 200))
                self.send_header("Content-Type", response.get("content_type", "application/json"))
                if "Transfer-Encoding" not in response.get("headers", {}):
                    self.send_header("Content-Length", str(len(content)))
                for name, value in response.get("headers", {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(content)
            except OSError:
                # Timeout tests deliberately disconnect before the response.
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{httpd.server_port}"
    worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def client(server, **kwargs):
    return PluginClient(server.url, TOKEN, CLIENT_ID, **kwargs)


def assert_error(code, operation, *, status=None):
    with pytest.raises(PluginError) as failed:
        operation()
    assert failed.value.code == code
    assert failed.value.status == status
    assert TOKEN not in str(failed.value)
    return failed.value


def test_authentication_headers_and_status_ignore_environment_proxy(server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    result = client(server, server_id=SERVER_ID).status()
    assert result == status_payload()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == PREFIX + "/status"
    assert request["headers"]["authorization"] == "Bearer " + TOKEN
    assert request["headers"]["x-ankisense-client"] == CLIENT_ID
    assert request["headers"]["accept"] == "application/json"
    assert request["body"] == b""


def test_answer_and_retry_json_preserve_original_text_and_require_explicit_cloud_flag(server):
    api = client(server)
    server.response = {"body": {"id": "attempt-one", "status": "completed"}}
    answer = "  原始回答：后进先出\n保留空格。  "
    assert api.submit_answer("card-one", 3, answer, "request-one")["id"] == "attempt-one"
    first = server.requests[0]
    assert first["method"] == "POST"
    assert first["path"] == PREFIX + "/attempts"
    assert first["headers"]["content-type"] == "application/json"
    assert json.loads(first["body"]) == {"card_id": "card-one", "card_version": 3,
                                          "answer": answer, "request_id": "request-one", "allow_cloud": False}
    api.retry_attempt("attempt-one", expected_retry_count=0, allow_cloud=True)
    assert server.requests[1]["path"] == PREFIX + "/attempts/attempt-one/retry"
    assert json.loads(server.requests[1]["body"]) == {"expected_retry_count": 0, "allow_cloud": True}
    api.attempt("attempt-one")
    assert server.requests[2]["method"] == "GET"
    assert server.requests[2]["path"] == PREFIX + "/attempts/attempt-one"


@pytest.mark.parametrize("body", [b"not JSON", b'{"incomplete":', b"\xff", [], "string", None])
def test_non_object_or_invalid_json_is_rejected(server, body):
    server.response = {"body": body}
    assert_error("invalid_response", client(server).status)
    assert len(server.requests) == 1


@pytest.mark.parametrize("protocol", [None, 0, 2, "1", True, 1.0])
def test_status_rejects_unsupported_or_noninteger_protocol(server, protocol):
    server.response = {"body": status_payload(protocol_version=protocol)}
    assert_error("incompatible_protocol", client(server).status)


def test_status_rejects_another_application(server):
    server.response = {"body": status_payload(application="OtherApp")}
    assert_error("incompatible_protocol", client(server).status)


@pytest.mark.parametrize("method", ["status", "catalog"])
def test_server_identity_change_is_rejected_before_sync(server, method):
    server.response = {"body": status_payload(server_id="as-" + "2" * 32, complete=True, cards=[])}
    assert_error("different_server", getattr(client(server, server_id=SERVER_ID), method))


@pytest.mark.parametrize("changes", [{"complete": False}, {"complete": 1}, {"cards": {}},
                                    {"protocol_version": 2}, {"protocol_version": True},
                                    {"protocol_version": 1.0}])
def test_catalog_never_accepts_an_incomplete_or_incompatible_manifest(server, changes):
    server.response = {"body": {"protocol_version": 1, "complete": True, "cards": [], **changes}}
    assert_error("incomplete_catalog", client(server).catalog)


def test_catalog_and_card_hash_contract(server):
    api = client(server, server_id=SERVER_ID)
    manifest = {"protocol_version": 1, "complete": True, "server_id": SERVER_ID,
                "cards": [{"id": "card-one", "content_hash": "1" * 64}]}
    server.response = {"body": manifest}
    assert api.catalog() == manifest
    card = {"id": "card-one", "content_hash": "1" * 64, "question": "问题"}
    server.response = {"body": card}
    assert api.card("card-one", expected_hash="1" * 64) == card
    assert server.requests[-1]["path"] == PREFIX + "/cards/card-one?expected_hash=" + "1" * 64
    server.response = {"body": {**card, "content_hash": "2" * 64}}
    assert_error("card_changed", lambda: api.card("card-one", expected_hash="1" * 64))
    server.response = {"body": {**card, "id": "another-card"}}
    assert_error("card_changed", lambda: api.card("card-one"))


@pytest.mark.parametrize("status", [401, 409, 422, 429, 500, 503])
def test_http_errors_preserve_safe_code_and_never_automatically_retry(server, status):
    server.response = {"status": status, "body": {"detail": {"code": "request_rejected", "message": "请检查本机配置。"}}}
    failed = assert_error("request_rejected",
                          lambda: client(server).submit_answer("card-one", 1, "答案", "request-one"), status=status)
    assert str(failed) == "请检查本机配置。"
    assert len(server.requests) == 1


@pytest.mark.parametrize("detail", [{"message": "Bad token: " + TOKEN, "code": "unsafe-code-with-dashes"},
                                   "Bad token: " + TOKEN,
                                   {"message": "x" * 480 + TOKEN, "code": "request_failed"}])
def test_error_token_is_redacted_even_when_it_crosses_message_truncation(server, detail):
    server.response = {"status": 401, "body": {"detail": detail}}
    failed = assert_error("request_failed", client(server).status, status=401)
    assert TOKEN[:16] not in str(failed)
    assert len(str(failed)) <= 500


@pytest.mark.parametrize("body", [b"<html>service failed</html>", [], {"detail": {"message": [], "code": []}}])
def test_malformed_error_body_uses_safe_default(server, body):
    server.response = {"status": 500, "body": body}
    failed = assert_error("request_failed", client(server).status, status=500)
    assert str(failed) == "AnkiSense could not complete this request."


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_blocked_without_a_second_request_or_credential_forwarding(server, status):
    server.response = {"status": status, "headers": {"Location": server.url + "/second-hop"}, "body": {}}
    assert_error("redirect_blocked",
                 lambda: client(server).submit_answer("card-one", 1, "答案", "request-one"), status=status)
    assert len(server.requests) == 1
    assert server.requests[0]["path"] == PREFIX + "/attempts"


def test_media_validates_sha_and_png_signature_before_returning_bytes(server):
    api = client(server)
    media_id = "img-" + "1" * 32
    digest = hashlib.sha256(PNG).hexdigest()
    server.response = {"body": PNG, "content_type": "image/png"}
    assert api.media(media_id, digest) == PNG
    assert server.requests[-1]["path"] == PREFIX + f"/media/{media_id}?sha256={digest}"
    assert server.requests[-1]["headers"]["accept"] == "image/png"
    assert_error("media_mismatch", lambda: api.media(media_id, "0" * 64))
    text = b"not a PNG image"
    server.response = {"body": text}
    assert_error("media_mismatch", lambda: api.media(media_id, hashlib.sha256(text).hexdigest()))


def test_none_media_identifier_is_rejected_locally_as_value_error(server):
    with pytest.raises(ValueError, match="Invalid media identifier"):
        client(server).media(None, "0" * 64)
    assert server.requests == []


def test_truncated_chunked_http_response_is_reported_as_connection_failure(server):
    # Declare a ten-byte chunk, write only three bytes, then close. urllib's
    # HTTP parser raises IncompleteRead before JSON decoding can begin.
    server.response = {"headers": {"Transfer-Encoding": "chunked"}, "body": b"A\r\nabc"}
    assert_error("connection_failed", client(server).status)
    assert len(server.requests) == 1


@pytest.mark.parametrize("kind", ["json", "media"])
def test_response_size_limits_are_enforced(server, monkeypatch, kind):
    monkeypatch.setattr(client_module, "MAX_JSON_BYTES", 64)
    monkeypatch.setattr(client_module, "MAX_MEDIA_BYTES", 64)
    server.response = {"body": b"x" * 65}
    operation = client(server).status if kind == "json" else lambda: client(server).media("img-" + "1" * 32, "0" * 64)
    assert_error("response_too_large", operation)
    assert len(server.requests) == 1


def test_timeout_has_safe_error_and_does_not_resubmit_answer(server):
    server.response = {"delay": 1.2, "body": {"status": "completed"}}
    failed = assert_error("connection_failed",
                          lambda: client(server, timeout=1).submit_answer("card-one", 1, "答案", "request-one"))
    assert "same request ID" in str(failed)
    assert len(server.requests) == 1


def test_retry_timeout_requires_querying_status_before_another_retry(server):
    server.response = {"delay": 1.2, "body": {"status": "failed", "retry_count": 1}}
    failed = assert_error("connection_failed",
                          lambda: client(server, timeout=1).retry_attempt("attempt-one", expected_retry_count=0))
    assert "Query this attempt's status" in str(failed)
    assert "same request ID" not in str(failed)
    assert len(server.requests) == 1
    assert json.loads(server.requests[0]["body"]) == {"expected_retry_count": 0, "allow_cloud": False}


@pytest.mark.parametrize("count", [-1, 3, True, 0.0, "0", None])
def test_invalid_retry_count_is_rejected_before_sending_request(server, count):
    with pytest.raises(ValueError):
        client(server).retry_attempt("attempt-one", expected_retry_count=count)
    assert server.requests == []


def test_connection_refused_has_safe_error():
    # Keep the port reserved but not listening, so no external destination is
    # contacted and another local process cannot take the test's port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        api = PluginClient(f"http://127.0.0.1:{reserved.getsockname()[1]}", TOKEN, CLIENT_ID, timeout=1)
        assert_error("connection_failed", api.status)


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.1:8765", "http://127.0.0.1.example.com",
                                "http://user:password@127.0.0.1:8765", "http://user@localhost",
                                "http://127.0.0.1:8765/api", "http://127.0.0.1?token=x",
                                "http://127.0.0.1#fragment", "http://127.0.0.1:0", "http://127.0.0.1:99999",
                                "file:///tmp/plugin", "http://127.0.0.1\\@example.com", " http://127.0.0.1"])
def test_remote_or_credential_bearing_connection_urls_are_rejected(url):
    with pytest.raises(ValueError, match="Only a local"):
        PluginClient(url, TOKEN, CLIENT_ID)


@pytest.mark.parametrize("changes", [{"card_version": True}, {"card_version": 0}, {"card_version": 1.0},
                                    {"answer": "x" * 4001}, {"answer": None}, {"request_id": "short"},
                                    {"request_id": "injected\r\nheader"}, {"allow_cloud": 1}, {"card_id": "../card"}])
def test_invalid_answer_request_is_rejected_locally(server, changes):
    payload = {"card_id": "card-one", "card_version": 1, "answer": "答案", "request_id": "request-one", **changes}
    with pytest.raises(ValueError):
        client(server).submit_answer(**payload)
    assert server.requests == []


def test_connection_file_pins_server_identity_and_loads_enabled_v1(server, tmp_path):
    path = tmp_path / "plugin-connection.json"
    path.write_text(json.dumps({"protocol_version": 1, "enabled": True, "server_id": SERVER_ID,
                               "base_url": server.url, "token": TOKEN}), encoding="utf-8")
    api = PluginClient.from_connection_file(path, CLIENT_ID)
    assert api.server_id == SERVER_ID
    assert api.status() == status_payload()
    server.response = {"body": status_payload(server_id="as-" + "2" * 32)}
    assert_error("different_server", api.status)


@pytest.mark.parametrize("changes", [{"enabled": False}, {"enabled": 1}, {"protocol_version": 2},
                                    {"protocol_version": True}, {"protocol_version": 1.0},
                                    {"server_id": None}, {"server_id": ""}, {"server_id": "unknown-server"},
                                    {"token": "short"}, {"base_url": "https://example.com"}])
def test_invalid_or_disabled_connection_file_is_rejected(server, tmp_path, changes):
    path = tmp_path / "plugin-connection.json"
    value = {"protocol_version": 1, "enabled": True, "server_id": SERVER_ID,
             "base_url": server.url, "token": TOKEN, **changes}
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or disabled") as failed:
        PluginClient.from_connection_file(path, CLIENT_ID)
    assert TOKEN not in str(failed.value)
    assert server.requests == []


@pytest.mark.parametrize("content", [None, "not json", "[]", "{}", "x" * 17000])
def test_missing_or_malformed_connection_file_is_rejected(tmp_path, content):
    path = tmp_path / "plugin-connection.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or disabled"):
        PluginClient.from_connection_file(path, CLIENT_ID)
