"""Standard-library client reusable by an Anki desktop add-on.

Network calls belong in an Anki background operation, not its Qt UI thread.
This module never imports aqt, edits a collection, or retries a paid request.
"""
from __future__ import annotations

import hashlib
from http.client import HTTPException as HTTPProtocolError
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PREFIX = "/api/plugin/v1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_MEDIA_BYTES = 5 * 1024 * 1024


class PluginError(RuntimeError):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise ValueError("Invalid record identifier")
    return quote(value, safe="")


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA256 digest")
    return value


class PluginClient:
    def __init__(self, base_url, token, client_id, *, timeout=210, server_id=None):
        try:
            url = urlsplit(base_url)
            if (url.scheme not in {"http", "https"} or url.hostname not in {"127.0.0.1", "localhost"}
                    or url.username is not None or url.password is not None or url.query or url.fragment
                    or url.path not in {"", "/"} or url.port == 0 or url.netloc.endswith(":")
                    or re.search(r"[\s\x00-\x20\x7f\\]", base_url)):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Only a local AnkiSense URL is supported") from None
        if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{40,128}", token):
            raise ValueError("Invalid local connection token")
        if not isinstance(client_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", client_id):
            raise ValueError("Use a stable ID for this Anki profile")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            raise ValueError("Timeout must be between 1 and 300 seconds")
        self.base_url = f"{url.scheme}://{url.hostname}" + (f":{url.port}" if url.port is not None else "")
        self.client_id = client_id
        self._token, self.timeout, self.server_id = token, timeout, server_id
        # No environment proxy or redirects can forward the local credential.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_connection_file(cls, path, client_id, **kwargs):
        try:
            with Path(path).open(encoding="utf-8") as stream:
                value = json.loads(stream.read(16385))
            if (type(value["protocol_version"]) is not int or value["protocol_version"] != 1 or value["enabled"] is not True
                    or not isinstance(value["server_id"], str) or not re.fullmatch(r"as-[0-9a-f]{32}", value["server_id"])):
                raise ValueError
            return cls(value["base_url"], value["token"], client_id, server_id=value["server_id"], **kwargs)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("The local plugin connection file is invalid or disabled") from None

    def _request(self, path, *, body=None, binary=False):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._token, "X-AnkiSense-Client": self.client_id,
                   "Accept": "image/png" if binary else "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + PREFIX + path, data=payload, headers=headers,
                          method="GET" if payload is None else "POST")
        maximum = MAX_MEDIA_BYTES if binary else MAX_JSON_BYTES
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content = response.read(maximum + 1)
        except HTTPError as exception:
            if 300 <= exception.code < 400:
                exception.close()
                raise PluginError(exception.code, "redirect_blocked", "AnkiSense redirected the request; check the local address.") from None
            message, code = "AnkiSense could not complete this request.", "request_failed"
            try:
                detail = json.loads(exception.read(8192)).get("detail")
                if isinstance(detail, dict):
                    if isinstance(detail.get("message"), str):
                        message = detail["message"].replace(self._token, "[connection token]")[:500]
                    if isinstance(detail.get("code"), str) and re.fullmatch(r"[a-z_]{1,80}", detail["code"]):
                        code = detail["code"]
                elif isinstance(detail, str):
                    message = detail.replace(self._token, "[connection token]")[:500]
            except (ValueError, TypeError, AttributeError):
                pass
            finally:
                exception.close()
            raise PluginError(exception.code, code, message) from None
        except (URLError, TimeoutError, OSError, HTTPProtocolError):
            if path.endswith("/retry"):
                raise PluginError(None, "connection_failed", "Cannot reach local AnkiSense. Query this attempt's status before requesting another retry.") from None
            raise PluginError(None, "connection_failed", "Cannot reach local AnkiSense. Keep your answer and retry with the same request ID.") from None
        if len(content) > maximum:
            raise PluginError(None, "response_too_large", "The response exceeds this client's limit.")
        if binary:
            return content
        try:
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise PluginError(None, "invalid_response", "AnkiSense returned an invalid response.") from None
        return result

    def status(self):
        result = self._request("/status")
        if result.get("application") != "AnkiSense" or type(result.get("protocol_version")) is not int or result["protocol_version"] != 1:
            raise PluginError(None, "incompatible_protocol", "This server does not support AnkiSense plugin protocol v1.")
        if self.server_id is not None and result.get("server_id") != self.server_id:
            raise PluginError(None, "different_server", "The connected AnkiSense data directory has changed. Reconnect before syncing.")
        return result

    def catalog(self, document_id=None):
        suffix = "?" + urlencode({"document_id": document_id}) if document_id else ""
        result = self._request("/cards" + suffix)
        if type(result.get("protocol_version")) is not int or result["protocol_version"] != 1 or result.get("complete") is not True or not isinstance(result.get("cards"), list):
            raise PluginError(None, "incomplete_catalog", "No complete sync manifest was received; keep existing Anki notes unchanged.")
        if self.server_id is not None and result.get("server_id") != self.server_id:
            raise PluginError(None, "different_server", "The connected AnkiSense data directory has changed.")
        return result

    def card(self, card_id, *, expected_hash=None):
        path = "/cards/" + _identifier(card_id)
        if expected_hash is not None:
            path += "?" + urlencode({"expected_hash": _digest(expected_hash)})
        result = self._request(path)
        if result.get("id") != card_id or (expected_hash is not None and result.get("content_hash") != expected_hash):
            raise PluginError(None, "card_changed", "The returned card differs from the sync manifest.")
        return result

    def media(self, media_id, sha256):
        if not isinstance(media_id, str) or not re.fullmatch(r"img-[0-9a-f]{32}", media_id):
            raise ValueError("Invalid media identifier")
        expected = _digest(sha256)
        content = self._request("/media/" + media_id + "?" + urlencode({"sha256": expected}), binary=True)
        if not content.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(content).hexdigest() != expected:
            raise PluginError(None, "media_mismatch", "The downloaded image failed validation; existing Anki media was not changed.")
        return content

    def submit_answer(self, card_id, card_version, answer, request_id, *, allow_cloud=False):
        _identifier(card_id)
        if type(card_version) is not int or card_version < 1 or not isinstance(answer, str) or len(answer) > 4000:
            raise ValueError("Invalid card version or answer")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", request_id) or type(allow_cloud) is not bool:
            raise ValueError("Invalid request ID or cloud confirmation")
        return self._request("/attempts", body={"card_id": card_id, "card_version": card_version,
                             "answer": answer, "request_id": request_id, "allow_cloud": allow_cloud})

    def attempt(self, attempt_id):
        return self._request("/attempts/" + _identifier(attempt_id))

    def connection_event(self, kind, result=None, *, plugin_version="0.3.2"):
        if kind not in {"heartbeat", "sync"}:
            raise ValueError("Invalid connection event")
        result = result or {}
        counts = {key: result.get(key, 0) for key in ("added", "updated", "unchanged", "conflicts", "excluded")}
        if any(type(value) is not int or value < 0 or value > 10000 for value in counts.values()):
            raise ValueError("Invalid sync counts")
        return self._request("/events", body={"kind": kind, "plugin_version": plugin_version, **counts})

    def retry_attempt(self, attempt_id, expected_retry_count, *, allow_cloud=False):
        if type(allow_cloud) is not bool or type(expected_retry_count) is not int or not 0 <= expected_retry_count <= 2:
            raise ValueError("Use the latest retry count and a boolean cloud confirmation")
        return self._request("/attempts/" + _identifier(attempt_id) + "/retry",
                             body={"expected_retry_count": expected_retry_count, "allow_cloud": allow_cloud})
