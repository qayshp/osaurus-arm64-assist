"""Real loopback HTTP contracts backed by a fake manager, never a model/DLL."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from osaurus_arm64_assist.lifecycle import WorkerFailure
from osaurus_arm64_assist.idle_policy import IdlePolicy
from osaurus_arm64_assist.server import AssistServer


class FakeManager:
    def __init__(self):
        self.config = {
            "api_key": "synthetic-test-only-key-not-a-credential",
            "models": [{"id": "synthetic-cpu", "mode": "cpu", "context": 2048},
                       {"id": "hidden", "mode": "npu", "enabled": False, "context": 4096}],
        }
        self.lock = threading.Lock()
        self.child = self.job = self.active_profile = self.last_metrics = None
        self.requests = []
        self.failure = None
        self.pause = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.unloaded = False

    def refresh(self):
        return self.config

    def _stop(self):
        self.unloaded = True

    def generate(self, profile, request):
        self.requests.append((profile, request))
        if self.failure == "native":
            raise WorkerFailure("Synthetic worker stopped")
        if self.failure == "invalid":
            raise ValueError("Synthetic invalid model output")
        if request["tools"] and request["tool_choice"] != "none":
            text = "<tool_call>raw native tool text must be buffered</tool_call>"
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_synthetic", "type": "function", "function": {
                    "name": "lookup", "arguments": json.dumps({"query": "café"}, ensure_ascii=False)}}]}
            finish = "tool_calls"
        elif request["response_format"]["type"] == "json_object":
            text = '{"city":"東京"}'
            message, finish = {"role": "assistant", "content": text}, "stop"
        else:
            text = "café 你好."
            message, finish = {"role": "assistant", "content": text}, "stop"
        self.entered.set()
        yield {"type": "token", "text": text[:3]}
        if self.pause and not self.release.wait(5):
            raise WorkerFailure("Synthetic test wait timed out")
        yield {"type": "token", "text": text[3:]}
        finished = {"type": "done", "text": text, "message": message, "finish_reason": finish,
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                    "metrics": {"request_seconds": .12},
                    "assist": {"requested_mode": profile["mode"], "actual_mode": profile["mode"],
                               "cold_load": False, "load_seconds_this_request": 0, "metrics": {}}}
        self.last_metrics = finished["assist"]
        yield finished


class ServerContractTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.server = AssistServer(("127.0.0.1", 0), self.manager, "synthetic-instance")
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.connections = []

    def tearDown(self):
        self.manager.release.set()
        for connection in self.connections:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def connection(self):
        value = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.connections.append(value)
        return value

    def headers(self, **changes):
        value = {"Authorization": "Bearer " + self.manager.config["api_key"], "Content-Type": "application/json"}
        value.update(changes)
        return value

    def body(self, **changes):
        value = {"model": "synthetic-cpu", "messages": [{"role": "user", "content": "Synthetic test."}],
                 "max_tokens": 32, "temperature": 0}
        value.update(changes)
        return value

    def post(self, body=None, connection=None, headers=None, path="/v1/chat/completions"):
        connection = connection or self.connection()
        data = json.dumps(self.body() if body is None else body, ensure_ascii=False).encode("utf-8")
        connection.request("POST", path, body=data, headers=self.headers() if headers is None else headers)
        response = connection.getresponse()
        return response, response.read()

    @staticmethod
    def events(data):
        lines = [line[6:] for line in data.decode("utf-8").splitlines() if line.startswith("data: ")]
        return [json.loads(line) for line in lines if line != "[DONE]"], lines[-1]

    def test_models_require_auth_and_advertise_actual_context_only(self):
        connection = self.connection()
        connection.request("GET", "/v1/models", headers=self.headers())
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        body = json.loads(response.read())
        self.assertEqual([entry["id"] for entry in body["data"]], ["synthetic-cpu"])
        self.assertEqual(body["data"][0]["context_length"], 2048)
        self.assertEqual(body["data"][0]["max_model_len"], 2048)
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.read())["error"]["type"], "authentication_error")

    def test_public_health_includes_only_safe_idle_policy_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            policy_config = {"idle_source": "osaurus", "idle_seconds": 300,
                "osaurus_url": "http://127.0.0.1:1444", "api_key": "private-fixture-api-key",
                "runtime_dir": "private-fixture-runtime-path"}
            policy = self.manager.idle_policy = IdlePolicy(Path(folder)/"config.json", lambda: policy_config, autostart=False)
            try:
                connection = self.connection()
                connection.request("GET", "/health")
                response = connection.getresponse()
                raw = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                metadata = json.loads(raw)["idle_policy"]
                self.assertEqual(metadata, {"mode": "osaurus", "seconds": 300, "status": "fallback",
                    "error": None, "setting": "idleUnloadSeconds"})
                for private in (policy_config["api_key"], policy_config["runtime_dir"], policy_config["osaurus_url"],
                        self.manager.config["api_key"], folder):
                    self.assertNotIn(private, raw)
                self.assertIsNone(policy._thread, "health test must not start a watcher or contact real Osaurus")
            finally:
                policy.close()

    def test_persistent_connection_success_then_error_then_success(self):
        connection = self.connection()
        connection.request("GET", "/health")
        first = connection.getresponse()
        self.assertEqual(first.status, 200)
        self.assertEqual(json.loads(first.read())["instance_id"], "synthetic-instance")
        bad, data = self.post(self.body(messages=[]), connection=connection)
        self.assertEqual(bad.status, 400)
        self.assertIn("application/json", bad.getheader("Content-Type"))
        self.assertIn("error", json.loads(data))
        good, data = self.post(connection=connection)
        self.assertEqual(good.status, 200)
        self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "café 你好.")

    def test_forbidden_origin_post_returns_403(self):
        response, _ = self.post(headers=self.headers(Origin="https://untrusted.invalid"))
        self.assertEqual(response.status, 403)
        self.assertEqual(self.manager.requests, [])

    def test_bad_auth_with_unread_body_closes_connection_and_server_recovers(self):
        connection = self.connection()
        response, data = self.post(connection=connection, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertTrue(response.will_close)
        self.assertIn("error", json.loads(data))
        connection.request("GET", "/health")
        healthy = connection.getresponse()
        self.assertEqual(healthy.status, 200)
        healthy.read()
        self.assertEqual(self.manager.requests, [])

    def test_nonstream_unicode_and_usage(self):
        response, data = self.post()
        self.assertEqual(response.status, 200)
        result = json.loads(data)
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["choices"][0]["message"], {"role": "assistant", "content": "café 你好."})
        self.assertEqual(result["usage"], {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16})
        self.assertGreaterEqual(result["assist"]["api_seconds"], 0)

    def test_stream_unicode_usage_and_terminal_marker(self):
        response, data = self.post(self.body(stream=True, stream_options={"include_usage": True}))
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        events, marker = self.events(data)
        self.assertEqual(marker, "[DONE]")
        deltas = [event["choices"][0]["delta"] for event in events if event["choices"]]
        self.assertEqual(deltas[0], {"role": "assistant"})
        self.assertEqual("".join(delta.get("content", "") for delta in deltas), "café 你好.")
        self.assertEqual(events[-1]["choices"], [])
        self.assertEqual(events[-1]["usage"]["total_tokens"], 16)
        self.assertEqual(events[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len({event["id"] for event in events}), 1)

    def tools(self):
        return [{"type": "function", "function": {"name": "lookup", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]

    def test_nonstream_tool_result_is_structured(self):
        response, data = self.post(self.body(tools=self.tools()))
        self.assertEqual(response.status, 200)
        choice = json.loads(data)["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"query": "café"})

    def test_stream_tool_result_buffers_native_markup(self):
        response, data = self.post(self.body(stream=True, tools=self.tools()))
        self.assertEqual(response.status, 200)
        events, marker = self.events(data)
        self.assertEqual(marker, "[DONE]")
        self.assertNotIn(b"raw native tool text", data)
        call_chunks = [event["choices"][0]["delta"]["tool_calls"] for event in events
                       if event["choices"] and event["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(len(call_chunks), 1)
        self.assertEqual(call_chunks[0][0]["index"], 0)
        self.assertEqual(json.loads(call_chunks[0][0]["function"]["arguments"]), {"query": "café"})
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_stream_json_object_is_emitted_as_complete_validated_text(self):
        response, data = self.post(self.body(stream=True, response_format={"type": "json_object"}))
        self.assertEqual(response.status, 200)
        events, marker = self.events(data)
        contents = [event["choices"][0]["delta"]["content"] for event in events
                    if event["choices"] and event["choices"][0]["delta"].get("content")]
        self.assertEqual(marker, "[DONE]")
        self.assertEqual(len(contents), 1)
        self.assertEqual(json.loads(contents[0]), {"city": "東京"})

    def test_busy_request_returns_429_while_health_remains_responsive(self):
        self.manager.pause = True
        result = []

        def first_request():
            try:
                connection = self.connection()
                response, data = self.post(connection=connection)
                result.append((response.status, json.loads(data)))
            except Exception as error:
                result.append(error)

        caller = threading.Thread(target=first_request, daemon=True)
        caller.start()
        self.assertTrue(self.manager.entered.wait(2))
        connection = self.connection()
        connection.request("GET", "/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        response, data = self.post()
        self.assertEqual(response.status, 429)
        self.assertEqual(json.loads(data)["error"]["type"], "rate_limit_error")
        self.manager.release.set()
        caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(result[0][0], 200)
        self.assertEqual(len(self.manager.requests), 1)

    def test_unknown_and_disabled_models_do_not_start_worker(self):
        for model in ("does-not-exist", "hidden"):
            with self.subTest(model=model):
                response, data = self.post(self.body(model=model))
                self.assertEqual(response.status, 404)
                self.assertIn("error", json.loads(data))
        self.assertEqual(self.manager.requests, [])

    def test_nan_payload_is_rejected_before_inference(self):
        response, data = self.post(self.body(temperature=float("nan")))
        self.assertEqual(response.status, 400)
        self.assertIn("Non-finite", json.loads(data)["error"]["message"])
        self.assertEqual(self.manager.requests, [])

    def test_oversized_content_length_is_rejected_without_reading_body(self):
        connection = self.connection()
        connection.request("POST", "/v1/chat/completions", body=b"", headers={
            **self.headers(), "Content-Length": str(4*1024*1024 + 1)})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertEqual(response.getheader("Connection"), "close")
        response.read()
        self.assertEqual(self.manager.requests, [])

    def test_native_failure_returns_service_unavailable(self):
        self.manager.failure = "native"
        response, data = self.post()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(data)["error"]["type"], "native_worker_error")

    def test_invalid_model_output_returns_json_error_before_stream_headers(self):
        self.manager.failure = "invalid"
        response, data = self.post(self.body(stream=True, tools=self.tools()))
        self.assertEqual(response.status, 400)
        self.assertIn("application/json", response.getheader("Content-Type"))
        self.assertIn("error", json.loads(data))


if __name__ == "__main__":
    unittest.main()
