"""Direct, bounded loopback fallback to Osaurus' explicitly local engine.

No request normalization happens here: native-only preferences, multimodal parts,
tools and structured-output contracts stay with the engine that understands them.
"""
import http.client
import json
import math
import os
import socket
import threading
import time
from urllib.parse import urlsplit

from .idle_policy import IdlePolicyError, normalize_endpoint

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024


class ProxyError(Exception):
    """Only fixed, public messages; never retain upstream bodies or credentials."""
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)

    def as_dict(self):
        return {"error": {"message": self.message, "type": "osaurus_proxy_error", "code": self.code}}


def _fail(code, message, status=502):
    raise ProxyError(status, code, message)


def _json(raw):
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError()
        return result
    try:
        return json.loads(raw.decode("utf-8"), parse_float=finite_float,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        _fail("invalid_json", "Osaurus returned invalid JSON.")


class _Response:
    """A direct connection with a total deadline, including slow HTTP headers."""
    def __init__(self, endpoint, method, path, payload, timeout, *, stream=False):
        parsed = urlsplit(endpoint)
        self.connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=min(1.0, timeout))
        self.response = self.transport = self.timer = None
        self.expired = threading.Event()
        self.deadline = time.monotonic() + timeout
        self.count = 0
        self.expected = None
        try:
            self.connection.connect()
            self.transport = self.connection.sock
            self.timer = threading.Timer(max(.001, self.deadline - time.monotonic()), self._expire)
            self.timer.daemon = True
            self.timer.start()
            headers = {"Accept": "text/event-stream" if stream else "application/json", "Connection": "close"}
            key = os.environ.get("OSAURUS_API_KEY")
            if key:
                headers["Authorization"] = "Bearer " + key
            if payload is not None:
                headers["Content-Type"] = "application/json"
            self._remaining()
            self.connection.request(method, path, body=payload, headers=headers)
            self._remaining()
            self.response = http.client.HTTPResponse(self.transport, method=method)
            self.response.begin()
            self._remaining()
            status = self.response.status
            if 300 <= status < 400:
                _fail("redirect_refused", "Osaurus redirects are not followed.")
            if status != 200:
                _fail("upstream_http_error", "Osaurus rejected the local-engine request.",
                      status if 400 <= status <= 599 else 502)
            length = self.response.getheader("Content-Length")
            if length is not None:
                if not length.isascii() or not length.isdigit():
                    _fail("invalid_response", "Osaurus returned invalid response framing.")
                self.expected = int(length)
                if self.expected > MAX_RESPONSE_BYTES:
                    _fail("response_too_large", "Osaurus response exceeded the size limit.")
            if self.response.getheader("Content-Encoding", "identity").lower() != "identity":
                _fail("invalid_response", "Osaurus returned unsupported response encoding.")
        except (OSError, http.client.HTTPException):
            self.close()
            if self.expired.is_set():
                _fail("upstream_timeout", "Osaurus local-engine request timed out.", 504)
            raise
        except BaseException:
            self.close()
            raise

    def _expire(self):
        self.expired.set()
        try:
            self.transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.expired.is_set() or remaining <= 0:
            _fail("upstream_timeout", "Osaurus local-engine request timed out.", 504)
        self.transport.settimeout(remaining)

    def chunks(self):
        while True:
            self._remaining()
            chunk = self.response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - self.count))
            self._remaining()
            if not chunk:
                if self.expected is not None and not self.response.chunked and self.count != self.expected:
                    _fail("invalid_response", "Osaurus returned an incomplete response.")
                return
            self.count += len(chunk)
            if self.count > MAX_RESPONSE_BYTES:
                _fail("response_too_large", "Osaurus response exceeded the size limit.")
            yield chunk

    def close(self):
        if self.timer is not None:
            self.timer.cancel()
        if self.response is not None:
            self.response.close()
        self.connection.close()


def _events(response):
    """Parse SSE as strict UTF-8, with bounded lines/events and exact DONE."""
    pending, data, event_size = b"", [], 0
    for chunk in response.chunks():
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            event_size += len(line) + 1
            if event_size > MAX_EVENT_BYTES:
                _fail("event_too_large", "Osaurus stream event exceeded the size limit.")
            try:
                decoded = line.decode("utf-8")
            except UnicodeError:
                _fail("invalid_utf8", "Osaurus stream was not valid UTF-8.")
            if not line:
                if data:
                    value = "\n".join(data)
                    if value == "[DONE]":
                        yield None
                        return
                    value = _json(value.encode("utf-8"))
                    if not isinstance(value, dict) or "error" in value:
                        _fail("invalid_stream", "Osaurus returned an invalid stream event.")
                    yield value
                data, event_size = [], 0
            elif decoded.startswith("data:"):
                data.append(decoded[5:].removeprefix(" "))
        if event_size + len(pending) > MAX_EVENT_BYTES:
            _fail("event_too_large", "Osaurus stream event exceeded the size limit.")
    _fail("missing_done", "Osaurus stream ended without its completion marker.")


def proxy_events(profile, body):
    """Yield json/sse events and an end event; closing the generator closes HTTP.

    `_osaurus.model_id` is the original inventory ID, never a provider routing ID.
    Exact local ownership is rechecked before POST to prevent provider recursion.
    """
    response = None
    try:
        if not isinstance(profile, dict) or not isinstance(profile.get("_osaurus"), dict):
            _fail("invalid_local_model", "A confirmed Osaurus local model ID is required.", 400)
        source = profile["_osaurus"]
        identifier = source.get("model_id")
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 1024
                or identifier.startswith("prov-") or any(ord(c) < 32 or ord(c) == 127 for c in identifier)):
            _fail("invalid_local_model", "A confirmed Osaurus local model ID is required.", 400)
        try:
            endpoint = normalize_endpoint(source.get("endpoint"))
        except IdlePolicyError:
            _fail("invalid_endpoint", "An Osaurus loopback endpoint is required.", 400)
        timeout = profile.get("request_timeout_seconds", 900)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            _fail("invalid_timeout", "The local-engine timeout must be positive.", 400)
        timeout = min(float(timeout), 900.0)
        if not isinstance(body, dict) or not isinstance(body.get("stream", False), bool):
            _fail("invalid_request", "The local-engine request must be a JSON object with a boolean stream field.", 400)
        forwarded = dict(body)
        forwarded["model"] = "local/" + identifier
        if forwarded.get("stream"):
            options = forwarded.get("stream_options")
            if options is None:
                forwarded["stream_options"] = {"include_usage": True}
            elif isinstance(options, dict):
                forwarded["stream_options"] = dict(options)
                forwarded["stream_options"].setdefault("include_usage", True)
        try:
            payload = json.dumps(forwarded, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail("invalid_request", "The local-engine request must contain valid JSON values.", 400)
        if len(payload) > MAX_REQUEST_BYTES:
            _fail("request_too_large", "The local-engine request exceeded the size limit.", 413)
        response = _Response(endpoint, "GET", "/v1/models", None, min(timeout, 5.0))
        inventory = _json(b"".join(response.chunks()))
        response.close()
        response = None
        entries = inventory.get("data") if isinstance(inventory, dict) else None
        if not isinstance(entries, list) or not any(isinstance(m, dict) and m.get("id") == identifier
                and m.get("owned_by") == "local" for m in entries):
            _fail("unconfirmed_local_model", "Osaurus did not confirm this model belongs to its local engine.", 404)
        reason = profile.get("_fallback_reason", "preferences_require_native_engine")
        if not isinstance(reason, str) or len(reason) > 256 or any(c in reason for c in "\\/\r\n\x00"):
            reason = "preferences_require_native_engine"
        assist = {"requested_mode": profile.get("_requested_mode", profile.get("mode", "auto")),
                  "actual_mode": "osaurus-native", "fallback_reason": reason, "source_model": identifier}
        response = _Response(endpoint, "POST", "/v1/chat/completions", payload, timeout,
                             stream=forwarded.get("stream", False))
        if forwarded.get("stream"):
            if response.response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                _fail("invalid_stream", "Osaurus did not return an event stream.")
            previous = None
            for event in _events(response):
                if event is None:
                    if previous is None:
                        _fail("invalid_stream", "Osaurus returned an empty event stream.")
                    previous["assist"] = assist
                    yield {"type": "sse", "value": previous}
                    yield {"type": "end"}
                    return
                if previous is not None:
                    yield {"type": "sse", "value": previous}
                previous = event
        else:
            value = _json(b"".join(response.chunks()))
            if not isinstance(value, dict) or "error" in value:
                _fail("invalid_response", "Osaurus returned an invalid completion response.")
            value["assist"] = assist
            yield {"type": "json", "value": value}
            yield {"type": "end"}
    except ProxyError:
        raise
    except (socket.timeout, TimeoutError):
        _fail("upstream_timeout", "Osaurus local-engine request timed out.", 504)
    except (OSError, http.client.HTTPException, ValueError):
        if response is not None and response.expired.is_set():
            _fail("upstream_timeout", "Osaurus local-engine request timed out.", 504)
        _fail("upstream_unavailable", "Osaurus local engine is unavailable.", 503)
    finally:
        if response is not None:
            response.close()
