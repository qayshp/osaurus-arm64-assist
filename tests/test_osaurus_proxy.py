"""Real loopback fixtures; no Osaurus mutation, model or native runtime required."""
import copy
import http.server
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

from osaurus_arm64_assist import osaurus_proxy as proxy


class Fixture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, raw, content_type="application/json", **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.server.requests.append(("GET", self.path, dict(self.headers), None))
        if self.server.get_redirect:
            self.reply(302, b"", Location="http://127.0.0.1:1/private")
            return
        raw = json.dumps({"data": self.server.models}, ensure_ascii=False).encode()
        self.reply(200, raw)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.loads(raw)
        self.server.requests.append(("POST", self.path, dict(self.headers), body))
        if self.server.slow_headers:
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Slow: ")
                for _ in range(30):
                    time.sleep(.02)
                    self.connection.sendall(b"x")
            except OSError:
                pass
            self.close_connection = True
            return
        if self.server.status != 200:
            self.reply(self.server.status, b'{"error":{"message":"Bearer SECRET C:\\\\private\\\\model"}}',
                       Location="http://127.0.0.1:1/private")
        elif self.server.raw is not None:
            self.reply(200, self.server.raw, self.server.content_type)
        elif body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            events = [
                {"id": "synthetic", "choices": [{"index": 0, "delta": {"content": "café 東京"}}]},
                {"id": "synthetic", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": '{"city":"東京"}'}}]}, "finish_reason": "tool_calls"}]},
                {"id": "synthetic", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
            ]
            data = b"".join(b"data: " + json.dumps(x, ensure_ascii=False).encode() + b"\r\n\r\n" for x in events)
            try:
                if self.server.pause_stream:
                    self.wfile.write(data)
                    self.wfile.flush()
                    self.connection.settimeout(2)
                    if self.connection.recv(1) == b"":
                        self.server.client_closed.set()
                    return
                for offset in range(0, len(data), 7):
                    self.wfile.write(data[offset:offset + 7])
                self.wfile.write(b"data: [DONE]\r\n\r\n")
                self.wfile.flush()
            except (OSError, socket.timeout):
                pass
            self.close_connection = True
        else:
            self.reply(200, json.dumps({"id": "synthetic", "choices": [{"message": {"content": "東京"}}],
                                       "usage": {"prompt_tokens": 5, "completion_tokens": 3}}, ensure_ascii=False).encode())


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.server.daemon_threads = True
        self.server.models = [{"id": "模型/raptor", "owned_by": "local"}]
        self.server.requests = []
        self.server.status, self.server.raw = 200, None
        self.server.get_redirect, self.server.pause_stream, self.server.slow_headers = False, False, False
        self.server.content_type = "application/json"
        self.server.client_closed = threading.Event()
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.profile = {"id": "discovered-raptor", "mode": "npu", "_requested_mode": "auto",
                        "_fallback_reason": "unsupported_native_preferences", "request_timeout_seconds": 2,
                        "_osaurus": {"endpoint": f"http://127.0.0.1:{self.server.server_port}", "model_id": "模型/raptor"}}
        self.body = {"model": "prov-companion/anything", "messages": [{"role": "user", "content": "synthetic"}]}
        self.environment = patch.dict(os.environ, {"OSAURUS_API_KEY": "synthetic-osaurus-admin-key",
                                                  "HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def run_proxy(self, **changes):
        return list(proxy.proxy_events(self.profile, dict(self.body, **changes)))

    def assert_failure(self, code, **changes):
        with self.assertRaises(proxy.ProxyError) as caught:
            self.run_proxy(**changes)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_forces_confirmed_local_route_and_preserves_native_contract(self):
        changes = {"temperature": 0, "top_p": 0, "min_p": 0, "seed": 0,
                   "chat_template_kwargs": {"enable_thinking": False},
                   "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:synthetic"}}]}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": "synthetic", "strict": True,
                       "schema": {"type": "object", "properties": {"answer": {"const": 42}}}}}}
        body = dict(self.body, **changes)
        before = copy.deepcopy(body)
        events = list(proxy.proxy_events(self.profile, body))
        posted = self.server.requests[1]
        self.assertEqual(posted[:2], ("POST", "/v1/chat/completions"))
        self.assertEqual(posted[3], dict(before, model="local/模型/raptor"))
        self.assertEqual(body, before)
        self.assertEqual(events[-1], {"type": "end"})
        assist = events[0]["value"]["assist"]
        self.assertEqual(assist, {"requested_mode": "auto", "actual_mode": "osaurus-native",
                                 "fallback_reason": "unsupported_native_preferences", "source_model": "模型/raptor"})
        self.assertEqual(events[0]["value"]["usage"]["completion_tokens"], 3)

    def test_only_osaurus_environment_key_is_sent_with_direct_proxy_free_connections(self):
        self.profile["api_key"] = "companion-key-must-not-forward"
        self.run_proxy()
        self.assertEqual(len(self.server.requests), 2)
        for request in self.server.requests:
            self.assertEqual(request[2].get("Authorization"), "Bearer synthetic-osaurus-admin-key")
        with patch.dict(os.environ, {}, clear=True):
            self.run_proxy()
        self.assertNotIn("Authorization", self.server.requests[-1][2])

    def test_rejects_remote_ownership_before_post(self):
        self.server.models[0]["owned_by"] = "prov-companion"
        self.assert_failure("unconfirmed_local_model")
        self.assertEqual(len(self.server.requests), 1)

    def test_rejects_provider_prefix_without_any_http(self):
        self.profile["_osaurus"]["model_id"] = "prov-companion/model"
        self.assert_failure("invalid_local_model")
        self.assertFalse(self.server.requests)

    def test_local_prefix_is_not_stripped_and_requires_exact_confirmation(self):
        self.profile["_osaurus"]["model_id"] = "local/模型/raptor"
        self.assert_failure("unconfirmed_local_model")
        self.server.models = [{"id": "local/模型/raptor", "owned_by": "local"}]
        self.run_proxy()
        self.assertEqual(self.server.requests[-1][3]["model"], "local/local/模型/raptor")

    def test_nonloopback_endpoint_rejected_before_http(self):
        self.profile["_osaurus"]["endpoint"] = "http://example.org:1337"
        self.assert_failure("invalid_endpoint")
        self.assertFalse(self.server.requests)

    def test_redirects_on_inventory_and_post_are_never_followed(self):
        self.server.get_redirect = True
        self.assert_failure("redirect_refused")
        self.assertEqual(len(self.server.requests), 1)
        self.server.get_redirect = False
        self.server.status = 302
        self.assert_failure("redirect_refused")
        self.assertEqual(len(self.server.requests), 3)

    def test_error_body_is_never_exposed(self):
        self.server.status = 403
        error = self.assert_failure("upstream_http_error")
        self.assertEqual(error.status, 403)
        public = json.dumps(error.as_dict())
        self.assertNotIn("SECRET", public)
        self.assertNotIn("private", public)
        self.assertNotIn("synthetic-osaurus-admin-key", public)

    def test_sse_unicode_tools_usage_and_final_assist(self):
        events = self.run_proxy(stream=True)
        self.assertEqual([e["type"] for e in events], ["sse", "sse", "sse", "end"])
        self.assertEqual(events[0]["value"]["choices"][0]["delta"]["content"], "café 東京")
        self.assertEqual(events[1]["value"]["choices"][0]["finish_reason"], "tool_calls")
        self.assertNotIn("assist", events[0]["value"])
        self.assertEqual(events[2]["value"]["usage"]["prompt_tokens"], 5)
        self.assertEqual(events[2]["value"]["assist"]["actual_mode"], "osaurus-native")
        self.assertEqual(self.server.requests[-1][3]["stream_options"], {"include_usage": True})

    def test_preserves_explicit_stream_options_false_and_extensions(self):
        options = {"include_usage": False, "synthetic_extension": 0}
        self.run_proxy(stream=True, stream_options=options)
        self.assertEqual(self.server.requests[-1][3]["stream_options"], options)

    def test_missing_and_inexact_done_are_rejected(self):
        self.server.content_type = "text/event-stream"
        for raw in [b'data: {"choices":[]}\n\n', b'data: {"choices":[]}\n\ndata: [DONE] \n\n']:
            with self.subTest(raw=raw):
                self.server.raw = raw
                with self.assertRaises(proxy.ProxyError) as caught:
                    self.run_proxy(stream=True)
                self.assertIn(caught.exception.code, ("missing_done", "invalid_json"))

    def test_invalid_utf8_and_json_constants_are_rejected(self):
        self.server.content_type = "text/event-stream"
        self.server.raw = b'data: {"text":"\xff"}\n\ndata: [DONE]\n\n'
        self.assert_failure("invalid_utf8", stream=True)
        self.server.raw = b'{"value": NaN}'
        self.assert_failure("invalid_json")
        self.server.raw = b'{"value": 1e999}'
        self.assert_failure("invalid_json")
        self.assert_failure("invalid_request", temperature=float("nan"))

    def test_request_response_and_event_byte_limits(self):
        with patch.object(proxy, "MAX_REQUEST_BYTES", 8):
            self.assert_failure("request_too_large")
        self.assertFalse(self.server.requests)
        self.server.raw = b'{"value":"' + b'x' * 100 + b'"}'
        with patch.object(proxy, "MAX_RESPONSE_BYTES", 100):
            self.assert_failure("response_too_large")
        self.server.content_type = "text/event-stream"
        self.server.raw = b'data: {"value":"' + b'x' * 100 + b'"}\n\ndata: [DONE]\n\n'
        with patch.object(proxy, "MAX_EVENT_BYTES", 60):
            self.assert_failure("event_too_large", stream=True)

    def test_generator_close_closes_upstream_socket(self):
        self.server.pause_stream = True
        events = proxy.proxy_events(self.profile, dict(self.body, stream=True))
        self.assertEqual(next(events)["type"], "sse")
        events.close()
        self.assertTrue(self.server.client_closed.wait(1))

    def test_total_deadline_interrupts_dripping_http_headers(self):
        self.server.slow_headers = True
        self.profile["request_timeout_seconds"] = .15
        started = time.monotonic()
        error = self.assert_failure("upstream_timeout")
        self.assertEqual(error.status, 504)
        self.assertLess(time.monotonic() - started, .8)

    def test_invalid_json_error_event_and_wrong_stream_type_are_safe(self):
        self.server.raw = b'<html>SECRET</html>'
        self.assert_failure("invalid_json")
        self.assert_failure("invalid_stream", stream=True)
        self.server.content_type = "text/event-stream"
        self.server.raw = b'data: {"error":{"message":"SECRET"}}\n\ndata: [DONE]\n\n'
        self.assert_failure("invalid_stream", stream=True)


if __name__ == "__main__":
    unittest.main()
