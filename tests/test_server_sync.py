"""HTTP integration of immutable shared snapshots and native-engine fallback."""
import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from osaurus_arm64_assist.osaurus_proxy import ProxyError
from osaurus_arm64_assist.server import AssistServer
from tests.test_server import FakeManager


class SharedSnapshot:
    def snapshot(self):
        return {"source": "osaurus", "status": "live", "inventory_models": 2, "native_profiles": 1, "proxy_profiles": 1}


class SharedManager(FakeManager):
    def __init__(self):
        super().__init__()
        self.config["models"] = []  # refresh returns the effective immutable view.
        self.shared_models = SharedSnapshot()
        base = {"id": "shared/Raptor", "mode": "hybrid", "context": 2048, "synced": True,
                "path": "C:\\private-fixture\\weights.gguf", "settings_source": "osaurus",
                "_osaurus": {"endpoint": "http://127.0.0.1:1337", "model_id": "Raptor"},
                "_requested_mode": "auto", "_fallback_reason": None,
                "sync": {"applied": ["temperature"], "adapted": ["contextSize"], "unsupported": []},
                "sampling_defaults": {"temperature": .8, "top_p": .95, "min_p": .05, "top_k": 40,
                                      "seed": 123, "max_completion_tokens": 300},
                "template_kwargs": {"enable_thinking": False, "preserve_thinking": True}}
        self.models = [base, {**copy.deepcopy(base), "id": "shared/Other", "mode": "osaurus",
                             "_fallback_reason": "native_compatibility_unverified"}]
        self.refreshes = 0
        self.context_failure = False
        self.after_output_failure = False

    def refresh(self):
        self.refreshes += 1
        return {**self.config, "models": copy.deepcopy(self.models)}

    def generate(self, profile, request):
        if self.context_failure:
            raise ValueError("Prompt (3000 tokens) plus requested output (32) exceeds the configured 2048-token context")
        if self.after_output_failure:
            yield {"type": "token", "text": "partial"}
            raise ValueError("Prompt (3000 tokens) plus requested output (32) exceeds the configured 2048-token context")
        yield from super().generate(profile, request)


class ServerSyncTests(unittest.TestCase):
    def setUp(self):
        self.manager = SharedManager()
        self.server = AssistServer(("127.0.0.1", 0), self.manager, "synthetic-shared-instance")
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.connections, self.proxied = [], []
        self.entered, self.release, self.closed = threading.Event(), threading.Event(), threading.Event()
        self.pause = False
        self.proxy_error = None
        self.patcher = patch("osaurus_arm64_assist.server.proxy_events", side_effect=self.proxy)
        self.patcher.start()

    def tearDown(self):
        self.release.set()
        for connection in self.connections:
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.patcher.stop()

    def connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.connections.append(connection)
        return connection

    def get(self, path, authenticated=True):
        connection = self.connection()
        headers = {"Authorization": "Bearer " + self.manager.config["api_key"]} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def post(self, body=None, path="/v1/chat/completions"):
        value = {} if path == "/admin/benchmark" else {
            "model": "shared/Raptor", "messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 32}
        value.update(body or {})
        connection = self.connection()
        connection.request("POST", path, body=json.dumps(value, ensure_ascii=False).encode(),
                           headers={"Authorization": "Bearer " + self.manager.config["api_key"], "Content-Type": "application/json"})
        response = connection.getresponse()
        return response, response.read()

    def proxy(self, profile, body):
        self.assertTrue(self.manager.lock.locked())
        self.assertTrue(self.manager.unloaded)
        self.proxied.append((copy.deepcopy(profile), copy.deepcopy(body)))
        self.entered.set()
        try:
            if self.pause:
                self.release.wait(2)
            if self.proxy_error:
                raise self.proxy_error
            assist = {"actual_mode": "osaurus-native", "requested_mode": "auto",
                      "source_model": "Raptor", "fallback_reason": profile.get("_fallback_reason")}
            if body.get("stream"):
                yield {"type": "sse", "value": {"id": "upstream", "choices": [{"delta": {"content": "東京"}}]}}
                yield {"type": "sse", "value": {"id": "upstream", "choices": [], "usage": {"total_tokens": 9}, "assist": assist}}
            else:
                yield {"type": "json", "value": {"id": "upstream", "choices": [{"message": {"content": "東京"}}],
                                                  "usage": {"total_tokens": 9}, "assist": assist}}
            yield {"type": "end"}
        finally:
            self.closed.set()

    def test_model_list_uses_effective_config_and_sync_status_has_no_paths(self):
        status, models = self.get("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in models["data"]], ["shared/Raptor", "shared/Other"])
        self.assertEqual(models["data"][0]["assist"]["settings_source"], "osaurus")
        self.assertEqual(self.get("/sync", False)[0], 401)
        status, sync = self.get("/sync")
        self.assertEqual(status, 200)
        self.assertEqual(sync["model_sync"]["status"], "live")
        self.assertEqual(sync["models"][0]["mode"], "hybrid")
        raw = json.dumps(sync)
        for private in ("private-fixture", "weights.gguf", "127.0.0.1", self.manager.config["api_key"]):
            self.assertNotIn(private, raw)

    def test_health_reads_only_cheap_snapshot(self):
        status, health = self.get("/health", False)
        self.assertEqual(status, 200)
        self.assertEqual(health["model_sync"]["inventory_models"], 2)
        self.assertEqual(self.manager.refreshes, 0)

    def test_shared_native_merges_sampling_and_template_defaults_preserving_zero(self):
        response, raw = self.post({"temperature": 0, "min_p": 0, "top_k": 0, "seed": 0,
                                   "chat_template_kwargs": {"preserve_thinking": False}})
        self.assertEqual(response.status, 200, raw)
        request = self.manager.requests[0][1]
        self.assertEqual((request["temperature"], request["min_p"], request["top_k"], request["seed"]), (0, 0, 0, 0))
        self.assertEqual(request["top_p"], .95)
        self.assertEqual(request["max_tokens"], 32)
        self.assertNotIn("max_completion_tokens", request)
        self.assertEqual(request["chat_template_kwargs"], {"enable_thinking": False, "preserve_thinking": False})
        self.assertFalse(self.proxied)
        self.assertFalse(self.manager.unloaded)

    def test_explicit_max_completion_tokens_wins_over_both_other_values(self):
        response, raw = self.post({"max_tokens": 12, "max_completion_tokens": 44})
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(self.manager.requests[0][1]["max_tokens"], 44)

    def test_proxy_profile_forwards_raw_body_without_normalizing(self):
        response, raw = self.post({"model": "shared/Other", "temperature": 0,
                                  "chat_template_kwargs": {"custom_native_option": "synthetic"},
                                  "response_format": {"type": "json_schema", "json_schema": {"name": "fixture"}}})
        self.assertEqual(response.status, 200, raw)
        posted = self.proxied[0][1]
        self.assertNotIn("min_p", posted)
        self.assertEqual(posted["temperature"], 0)
        self.assertEqual(posted["chat_template_kwargs"], {"custom_native_option": "synthetic"})
        self.assertEqual(json.loads(raw)["assist"]["actual_mode"], "osaurus-native")
        self.assertEqual(self.manager.last_metrics["actual_mode"], "osaurus-native")
        self.assertTrue(self.closed.wait(1))

    def test_synced_unsupported_request_features_all_proxy_without_dropping_fields(self):
        cases = [{"reasoning_effort": "high"}, {"chat_template_kwargs": {"enable_thinking": True}},
                 {"response_format": {"type": "json_schema", "json_schema": {"name": "fixture"}}},
                 {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:fixture"}}]}]},
                 {"n": 2}, {"logprobs": True}, {"stream_options": {"native_option": True}}]
        for change in cases:
            with self.subTest(change=change):
                response, raw = self.post(change)
                self.assertEqual(response.status, 200, raw)
                profile, posted = self.proxied[-1]
                self.assertEqual(profile["_fallback_reason"], "unsupported_request_features")
                for key, value in change.items():
                    self.assertEqual(posted[key], value)
        self.assertFalse(self.manager.requests)

    def test_fixed_manual_profile_keeps_existing_strict_rejection(self):
        self.manager.models[0].update(synced=False, settings_source="fixed")
        response, raw = self.post({"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(response.status, 400, raw)
        self.assertFalse(self.proxied)

    def test_proxy_stream_preserves_chunks_usage_and_writes_done_once(self):
        response, raw = self.post({"model": "shared/Other", "stream": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(raw.count(b"data: [DONE]"), 1)
        chunks = [json.loads(line[6:]) for line in raw.decode().splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        self.assertEqual(len(chunks), 2)  # No fabricated native assistant-role chunk.
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "東京")
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 9)
        self.assertEqual(chunks[-1]["assist"]["actual_mode"], "osaurus-native")
        self.assertTrue(self.closed.is_set())

    def test_proxy_error_status_code_and_generator_cleanup(self):
        self.proxy_error = ProxyError(504, "upstream_timeout", "Synthetic safe timeout.")
        response, raw = self.post({"model": "shared/Other", "stream": True})
        self.assertEqual(response.status, 504)
        self.assertEqual(json.loads(raw)["error"]["code"], "upstream_timeout")
        self.assertIn("application/json", response.getheader("Content-Type"))
        self.assertTrue(self.closed.wait(1))
        self.assertTrue(self.manager.lock.acquire(timeout=1))
        self.manager.lock.release()

    def test_proxy_holds_admission_while_health_remains_available(self):
        self.pause = True
        result = []
        caller = threading.Thread(target=lambda: result.append(self.post({"model": "shared/Other"})[0].status), daemon=True)
        caller.start()
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(self.get("/health", False)[0], 200)
        response, raw = self.post()
        self.assertEqual(response.status, 429, raw)
        self.release.set()
        caller.join(2)
        self.assertEqual(result, [200])

    def test_proxy_benchmark_is_rejected_before_any_worker_or_proxy(self):
        response, raw = self.post({"model": "shared/Other", "prompt_tokens": 32, "generated_tokens": 8}, "/admin/benchmark")
        self.assertEqual(response.status, 400, raw)
        self.assertEqual(json.loads(raw)["error"]["code"], "native_benchmark_required")
        self.assertFalse(self.manager.requests or self.proxied)

    def test_unknown_model_is_selected_before_native_normalization(self):
        response, raw = self.post({"model": "absent", "messages": []})
        self.assertEqual(response.status, 404, raw)

    def test_context_failure_before_first_event_falls_back_with_original_request(self):
        self.manager.context_failure = True
        response, raw = self.post({"temperature": 0, "stream": True})
        self.assertEqual(response.status, 200, raw)
        profile, body = self.proxied[0]
        self.assertEqual(profile["_fallback_reason"], "prompt_exceeds_assist_context")
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("min_p", body)
        self.assertEqual(raw.count(b"data: [DONE]"), 1)

    def test_context_failure_after_output_is_not_retried(self):
        self.manager.after_output_failure = True
        response, raw = self.post({"stream": True})
        self.assertEqual(response.status, 200)
        self.assertIn(b"partial", raw)
        self.assertIn(b'"error"', raw)
        self.assertEqual(raw.count(b"data: [DONE]"), 1)
        self.assertFalse(self.proxied)

    def test_requested_output_beyond_native_capacity_preflights_to_proxy(self):
        response, raw = self.post({"max_tokens": 4096})
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(self.proxied[0][0]["_fallback_reason"], "requested_output_exceeds_assist_context")
        self.assertFalse(self.manager.requests)

    def test_client_disconnect_closes_proxy_generator_and_releases_admission(self):
        def producing(profile, body):
            try:
                yield {"type": "sse", "value": {"choices": [{"delta": {"content": "first"}}]}}
                self.release.wait(2)
                for _ in range(512):
                    yield {"type": "sse", "value": {"choices": [{"delta": {"content": "x" * 8192}}]}}
            finally:
                self.closed.set()
        with patch("osaurus_arm64_assist.server.proxy_events", side_effect=producing):
            connection = self.connection()
            connection.request("POST", "/v1/chat/completions", body=json.dumps({"model": "shared/Other", "stream": True,
                "messages": [{"role": "user", "content": "synthetic"}]}),
                headers={"Authorization": "Bearer " + self.manager.config["api_key"], "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read(1)
            response.close()
            connection.close()
            self.release.set()
            self.assertTrue(self.closed.wait(2))
            self.assertTrue(self.manager.lock.acquire(timeout=1))
            self.manager.lock.release()


if __name__ == "__main__":
    unittest.main()
