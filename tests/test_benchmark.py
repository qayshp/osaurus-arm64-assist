"""Fixed-token reproducibility, explicit IPC, HTTP admission and safe local CLI."""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from osaurus_arm64_assist import benchmark, cli, config
from osaurus_arm64_assist.engine import Engine
from osaurus_arm64_assist.lifecycle import Manager, WorkerFailure
from osaurus_arm64_assist.server import AssistServer


def settings(**changes):
    return {"model": "sample", "prompt_tokens": 32, "generated_tokens": 8,
            "repetitions": 3, "warmup": 1, "seed": 42, **changes}


def result(number=1, count=8, prompts=32):
    return {"type": "done", "finish_reason": "length", "generated_ids": list(range(count)),
            "usage": {"prompt_tokens": prompts, "completion_tokens": count, "total_tokens": prompts+count},
            "metrics": {"request_seconds": number, "first_text_seconds": None, "state_bytes": 0,
                        "token_count_convention": "fixed token test"},
            "assist": {"requested_mode": "cpu", "actual_mode": "cpu", "cold_load": False,
                       "load_seconds_this_request": 0, "fallback_reason": None}}


class BenchmarkWorkloadTests(unittest.TestCase):
    def test_historical_default_rng_is_exact_and_repeatable(self):
        value = benchmark.normalize_benchmark({"model": "sample"}, 2048)
        self.assertEqual(value, settings(prompt_tokens=512, generated_tokens=128))
        ids = benchmark.input_ids(value)
        self.assertEqual(ids[:5], [20952, 3648, 819, 24299, 9012])
        self.assertEqual(len(ids), 512)
        self.assertTrue(all(0 <= token <= 31999 for token in ids))
        self.assertEqual(ids, benchmark.input_ids(value))
        self.assertNotEqual(ids, benchmark.input_ids({**value, "seed": 43}))

    def test_limits_and_primitive_types_are_checked_before_work(self):
        bad = [None, [], {"model": None}, settings(prompt_tokens=True), settings(seed=-1),
            settings(repetitions=0), settings(repetitions=11), settings(warmup=4),
            settings(warmup=False), settings(generated_tokens=0), settings(seed=2**32),
            settings(prompt_tokens=2048), settings(token_ids=[1, 2])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark.normalize_benchmark(value, 2048)
        self.assertEqual(benchmark.normalize_benchmark(settings(prompt_tokens=2040), 2048)["prompt_tokens"], 2040)

    def test_warmup_is_separate_and_summary_excludes_it_and_private_config(self):
        manager = Manager.__new__(Manager)
        seen = []
        def generate(profile, request, *, token_ids):
            seen.append((dict(request), list(token_ids)))
            yield result([99, 1, 3, 2][len(seen)-1])
        manager.generate = generate
        profile = {"id": "sample", "mode": "cpu", "context": 2048, "path": "private-model-location",
                   "api_key": "private-key", "runtime_dir": "private-runtime", "threads": 6}
        report = manager.benchmark(profile, settings())
        self.assertEqual(len(report["warmup_runs"]), 1)
        self.assertEqual(len(report["runs"]), 3)
        self.assertEqual(report["summary"]["median_metrics"]["request_seconds"], 2)
        self.assertNotIn("first_text_seconds", report["summary"]["median_metrics"])
        self.assertNotIn("private-", json.dumps(report))
        self.assertEqual(report["configuration"], {"id": "sample", "mode": "cpu", "context": 2048, "threads": 6})
        self.assertTrue(all(item[1] == report["input_ids"] for item in seen))
        self.assertTrue(all(item[0]["temperature"] == 0 and not item[0]["tools"] for item in seen))
        self.assertEqual([run["repetition"] for run in report["runs"]], [1, 2, 3])

    def test_incomplete_output_is_failure_not_a_speed_result(self):
        manager = Manager.__new__(Manager)
        def generate(*args, **kwargs):
            yield result(count=7)
        manager.generate = generate
        with self.assertRaises(WorkerFailure):
            manager.benchmark({"id": "sample", "mode": "cpu", "context": 2048}, settings(warmup=0))

    def test_manager_uses_explicit_benchmark_ipc_without_changing_chat(self):
        manager = Manager.__new__(Manager)
        manager._load = mock.Mock(return_value=False)
        manager._stop = mock.Mock()
        manager.connection = mock.Mock()
        manager.ready = {"load_seconds": 0, "load_stages": {}, "architecture": "synthetic", "context_length": 2048}
        manager._receive = mock.Mock(side_effect=[result(), result()])
        profile = {"id": "sample", "mode": "cpu"}
        list(manager.generate(profile, {}, token_ids=[1, 2]))
        self.assertEqual(manager.connection.send.call_args.args[0], {"type": "benchmark", "request": {}, "token_ids": [1, 2]})
        list(manager.generate(profile, {"token_ids": [99], "ignore_eos": True}))
        self.assertEqual(manager.connection.send.call_args.args[0]["type"], "generate")
        self.assertNotIn("token_ids", manager.connection.send.call_args.args[0])


class DirectTokenEngineTests(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine.profile, engine.context_limit = {"batch": 2}, 32
        engine.primary = SimpleNamespace(vocab="vocab", context="context")
        engine.secondary = None
        engine.template = mock.Mock()
        engine.sampler = mock.Mock(return_value="sampler")
        engine.api = mock.Mock()
        engine.api.llama_vocab_n_tokens.return_value = 10
        engine.api.llama_sampler_sample.side_effect = [2, 7, 8]
        engine.api.llama_vocab_is_eog.side_effect = lambda vocab, token: token == 2
        engine.api.piece.side_effect = lambda vocab, token: b"" if token == 2 else b"x"
        engine.api.llama_perf_context.return_value = SimpleNamespace(n_eval=2, t_eval_ms=20)
        return engine

    def test_direct_ids_bypass_template_and_ignore_eog_for_exact_count(self):
        engine = self.engine()
        events = list(engine.generate({"max_tokens": 3, "stop": []}, token_ids=[1, 2, 3], ignore_eos=True))
        done = events[-1]
        engine.template.render.assert_not_called()
        engine.api.tokenize.assert_not_called()
        engine.api.llama_vocab_is_eog.assert_not_called()
        self.assertEqual(done["generated_ids"], [2, 7, 8])
        self.assertEqual(done["usage"], {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6})
        self.assertEqual(done["metrics"]["format_seconds"], 0)
        self.assertEqual(done["metrics"]["tokenize_seconds"], 0)
        self.assertIsNotNone(done["metrics"]["first_token_seconds"])
        self.assertEqual(engine.api.decode.call_args_list, [mock.call("context", [1, 2]), mock.call("context", [3]),
            mock.call("context", [2]), mock.call("context", [7])])
        engine.api.llama_sampler_free.assert_called_once_with("sampler")

    def test_normal_eog_stopping_and_invalid_ids_are_preserved(self):
        engine = self.engine()
        done = list(engine.generate({"max_tokens": 3, "stop": []}, token_ids=[1]))[-1]
        self.assertEqual(done["generated_ids"], [])
        self.assertEqual(done["finish_reason"], "stop")
        for ids in ([True], [-1], [10], [1.5], [], list(range(33))):
            engine = self.engine()
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                list(engine.generate({"max_tokens": 3, "stop": []}, token_ids=ids, ignore_eos=True))
            engine.api.decode.assert_not_called()


class HttpManager:
    def __init__(self):
        self.config = {"api_key": "synthetic-test-key", "models": [
            {"id": "sample", "mode": "cpu", "context": 128},
            {"id": "disabled", "mode": "npu", "context": 128, "enabled": False}]}
        self.lock = threading.Lock()
        self.child = self.job = self.active_profile = self.last_metrics = None
        self.calls = []

    def refresh(self):
        return self.config

    def benchmark(self, profile, request):
        if not self.lock.locked():
            raise AssertionError("Benchmark must hold inference lock")
        self.calls.append(request)
        return {"model": profile["id"], "input_ids": [1]*request["prompt_tokens"], "runs": [],
                "summary": {"measured_repetitions": request["repetitions"]}}


class BenchmarkHttpTests(unittest.TestCase):
    def setUp(self):
        self.manager = HttpManager()
        self.server = AssistServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def post(self, body, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if auth: headers["Authorization"] = "Bearer " + self.manager.config["api_key"]
        try:
            connection.request("POST", "/admin/benchmark", json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authenticated_benchmark_records_same_api_timing_in_metrics(self):
        status, report = self.post(settings())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.manager.calls), 1)
        self.assertEqual(report["api_seconds"], self.manager.last_metrics["api_seconds"])
        self.assertFalse(self.manager.lock.locked())

    def test_auth_unknown_models_limits_and_busy_requests_do_not_run(self):
        self.assertEqual(self.post(settings(), auth=False)[0], 401)
        for body, expected in [(settings(model="missing"), 404), (settings(model="disabled"), 404),
                (settings(prompt_tokens=128), 400), (settings(repetitions=11), 400), (settings(seed=True), 400)]:
            with self.subTest(body=body): self.assertEqual(self.post(body)[0], expected)
        self.manager.lock.acquire()
        try:
            self.assertEqual(self.post(settings())[0], 429)
        finally:
            self.manager.lock.release()
        self.assertEqual(self.manager.calls, [])


class BenchmarkCliTests(unittest.TestCase):
    def test_cli_uses_admin_endpoint_and_writes_machine_readable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path, destination = Path(directory)/"config.json", Path(directory)/"result.json"
            value = config.new_config("unused-runtime")
            value["models"] = [{"id": "sample", "mode": "cpu", "path": str(Path(directory)/"synthetic.gguf"), "context": 2048}]
            config.atomic_json(path, value)
            report = {"kind": "fixed_tokens", "runs": []}
            with mock.patch.object(cli, "api", return_value=report) as api, contextlib.redirect_stdout(io.StringIO()):
                cli.main(["benchmark", "--config", str(path), "--model", "sample", "--output", str(destination)])
            self.assertEqual(api.call_args.args[1], "/admin/benchmark")
            self.assertEqual(api.call_args.args[2], settings(prompt_tokens=512, generated_tokens=128))
            self.assertGreater(api.call_args.kwargs["timeout"], 360)
            self.assertEqual(json.loads(destination.read_text()), report)

    def test_local_client_never_follows_redirect_with_bearer_key(self):
        requests = []
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True); thread.start()
        try:
            with self.assertRaises(ValueError):
                cli.api({"port": server.server_port, "api_key": "synthetic-test-key"}, "/admin/benchmark", {})
            self.assertEqual(requests, ["/admin/benchmark"])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__":
    unittest.main()
