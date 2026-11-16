"""Long-context receipt validation with synthetic clients, never native models."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("context_acceptance_tool", TOOLS / "context_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(TOOLS), *sys.path]):
    SPEC.loader.exec_module(acceptance)


class NoNetworkMonitor:
    def __init__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def report(self):
        return {"samples": [{"elapsed_seconds": .25, "available_physical_bytes": 123456789}]}


class FakeClient:
    def __init__(self, expected):
        self.expected = expected
        self.requests = []
        self.modify = lambda result, number: None

    def request(self, path, body):
        self.requests.append((path, copy.deepcopy(body)))
        if path == "/admin/benchmark":
            rng = random.Random(body["seed"])
            runs = [{"warmup": False,
                     "usage": {"prompt_tokens": body["prompt_tokens"], "completion_tokens": body["generated_tokens"],
                               "total_tokens": body["prompt_tokens"] + body["generated_tokens"]},
                     "generated_ids": list(range(body["generated_tokens"])),
                     "metrics": {"state_bytes": 20000000, "state_transfer_seconds": .1},
                     "assist": {"actual_mode": "hybrid", "context_length": 8192, "cold_load": index == 0}}
                    for index in range(body["repetitions"])]
            result = {"settings": dict(body), "warmup_runs": [], "runs": runs,
                      "input_ids": [rng.randrange(32000) for _ in range(body["prompt_tokens"])]}
        else:
            result = {"choices": [{"message": {"content": json.dumps(self.expected)}}], "sse_done": True,
                      "usage": {"prompt_tokens": 5000, "completion_tokens": 40, "total_tokens": 5040},
                      "assist": {"actual_mode": "hybrid", "context_length": 8192,
                                 "cold_load": len(self.requests) == 1, "load_seconds_this_request": .5,
                                 "metrics": {"state_bytes": 20000000, "state_transfer_seconds": .1,
                                             "prompt_seconds": .4, "first_token_seconds": .41}}}
        self.modify(result, len(self.requests))
        return result, {"headers_seconds": .01, "first_text_seconds": .5, "complete_seconds": .51}


class ContextAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.cases = acceptance.workloads(256, 42)
        self.profile = {"id": "synthetic-hybrid", "mode": "hybrid", "context": 8192}
        self.client = FakeClient(self.cases[0]["expected"])
        self.monitor = patch.object(acceptance, "MemorySampler", NoNetworkMonitor)
        self.monitor.start()
        self.addCleanup(self.monitor.stop)

    def quality(self, *, repetitions=1, minimum=4097, cold=False):
        return acceptance.run_quality(self.client, "http://127.0.0.1:1", self.profile,
                                      self.cases[0], 8192, minimum, repetitions, 128, cold)

    def test_all_facts_occur_once_in_document_and_history_in_order(self):
        expected = self.cases[0]["expected"]
        self.assertEqual(expected, self.cases[1]["expected"])
        self.assertEqual(self.cases, acceptance.workloads(256, 42))
        self.assertNotEqual(expected, acceptance.workloads(256, 43)[0]["expected"])
        for count in (8, 33, 64, 65, 256, 819):
            for case in acceptance.workloads(count, 42):
                text = "\n".join(message["content"] for message in case["messages"])
                positions = []
                for name in ("early", "middle", "late"):
                    code = case["expected"][name]
                    self.assertEqual(text.count(code), 1, (count, case["id"], name))
                    positions.append(text.index(code))
                self.assertEqual(positions, sorted(positions))
        history = self.cases[1]["messages"]
        self.assertIn(expected["early"], history[1]["content"])
        self.assertNotIn(expected["early"], history[-1]["content"])
        self.assertIn(expected["late"], history[-1]["content"])
        self.assertGreater(len(history), 10)

    def test_exact_json_strict_sse_actual_long_input_handoff_and_cold_warm_pass(self):
        rows = self.quality(repetitions=2, cold=True)
        self.assertTrue(all(row["passed"] for row in rows), rows)
        self.assertTrue(rows[0]["contract_checks"]["cold_model_observed"])
        self.assertTrue(rows[1]["contract_checks"]["resident_model_observed"])
        self.assertEqual(rows[0]["stages"]["state_bytes"], 20000000)
        self.assertEqual(rows[0]["usage"]["prompt_tokens"], 5000)
        self.assertEqual(rows[0]["memory"]["samples"][0]["available_physical_bytes"], 123456789)
        request = self.client.requests[0][1]
        self.assertEqual(request["temperature"], 0)
        self.assertTrue(request["stream"])
        self.assertEqual(request["response_format"], {"type": "json_object"})

    def test_correct_short_answer_does_not_establish_long_context_support(self):
        def shorten(result, number):
            result["usage"].update(prompt_tokens=2048, total_tokens=2088)
        self.client.modify = shorten
        row = self.quality()[0]
        self.assertFalse(row["passed"])
        self.assertTrue(row["transport_ok"])
        self.assertTrue(row["quality_ok"])
        self.assertEqual(row["failure_category"], "contract")
        self.assertFalse(row["contract_checks"]["long_input_verified"])

    def test_wrong_retrieval_is_quality_failure_with_valid_transport_and_contract(self):
        self.client.expected = {**self.cases[0]["expected"], "middle": "00000000"}
        row = self.quality()[0]
        self.assertFalse(row["passed"])
        self.assertTrue(row["transport_ok"])
        self.assertFalse(row["quality_ok"])
        self.assertTrue(all(row["contract_checks"].values()))
        self.assertEqual(row["failure_category"], "quality")

    def test_context_mode_sse_continuation_and_transfer_are_required(self):
        changes = [
            ("actual_context", lambda r: r["assist"].update(context_length=2048)),
            ("actual_native_mode", lambda r: r["assist"].update(actual_mode="osaurus-native")),
            ("no_stock_engine_fallback", lambda r: r["assist"].update(fallback_reason="stock")),
            ("strict_sse_completed", lambda r: r.update(sse_done=False)),
            ("continuation_exercised", lambda r: r["usage"].update(completion_tokens=1, total_tokens=5001)),
            ("state_transferred", lambda r: r["assist"]["metrics"].update(state_bytes=0)),
            ("state_transferred", lambda r: r["assist"]["metrics"].update(state_bytes=True)),
            ("handoff_timed", lambda r: r["assist"]["metrics"].update(state_transfer_seconds=True)),
        ]
        for name, change in changes:
            with self.subTest(check=name, change=change):
                self.client.modify = lambda result, number: change(result)
                row = self.quality()[0]
                self.assertFalse(row["passed"])
                self.assertTrue(row["quality_ok"])
                self.assertFalse(row["contract_checks"][name])
                self.assertEqual(row["failure_category"], "contract")

    def test_cold_load_requires_real_boolean_and_resident_repeat_is_checked(self):
        self.client.modify = lambda result, number: result["assist"].update(cold_load=1)
        self.assertFalse(self.quality(cold=True)[0]["contract_checks"]["cold_model_observed"])
        self.client.modify = lambda result, number: result["assist"].update(cold_load=True)
        rows = self.quality(repetitions=2, cold=True)
        self.assertTrue(rows[0]["passed"])
        self.assertFalse(rows[1]["contract_checks"]["resident_model_observed"])

    def test_api_errors_have_safe_separate_failure_category(self):
        with patch.object(self.client, "request", side_effect=acceptance.APIError(504, "native_timeout")):
            row = self.quality()[0]
        self.assertFalse(row["passed"])
        self.assertEqual(row["failure_category"], "api")
        self.assertEqual(row["http_status"], 504)
        self.assertNotIn("answer", row)

    def test_random_benchmark_requires_exact_32_outputs_and_two_measured_runs(self):
        row = acceptance.run_benchmark(self.client, "http://127.0.0.1:1", self.profile, 6144, 32)
        self.assertTrue(row["passed"], row)
        request = self.client.requests[0][1]
        self.assertEqual((request["generated_tokens"], request["repetitions"], request["warmup"]), (32, 2, 0))
        self.assertEqual(len(row["runs"][0]["generated_ids"]), 32)
        self.assertIn("not answer quality", row["purpose"])
        def truncate(result, number):
            result["runs"][0]["generated_ids"].pop()
        self.client.modify = truncate
        self.assertFalse(acceptance.run_benchmark(self.client, "http://127.0.0.1:1", self.profile, 6144, 32)["passed"])

    def test_random_benchmark_cannot_pass_with_missing_runs_wrong_context_or_boolean_state(self):
        changes = [lambda r: r.update(runs=[]),
                   lambda r: r["runs"][0]["assist"].update(context_length=2048),
                   lambda r: r["runs"][0]["metrics"].update(state_bytes=True),
                   lambda r: r["settings"].update(generated_tokens=128),
                   lambda r: r["runs"][0].update(warmup=True)]
        for change in changes:
            with self.subTest(change=change):
                self.client.modify = lambda result, number: change(result)
                row = acceptance.run_benchmark(self.client, "http://127.0.0.1:1", self.profile, 6144, 32)
                self.assertFalse(row["passed"])

    def test_dry_run_never_constructs_http_clients_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory(prefix="context-acceptance-test-") as folder:
            root = Path(folder).resolve()
            config, output = root / "config.json", root / "receipt.json"
            original = json.dumps({"api_key": "PRIVATE_TEST_CREDENTIAL", "models_source": "fixed",
                "models": [{**self.profile, "path": str(root / "not-a-real-model.gguf")}],
                "unrelated_private_setting": "PRIVATE_TEST_SETTING"})
            config.write_text(original, encoding="utf-8")
            argv = ["--config", str(config), "--model", self.profile["id"], "--context", "8192",
                    "--output", str(output), "--dry-run", "--cold", "--benchmark-prompts", "6144"]
            with patch.object(acceptance, "Client", side_effect=AssertionError("No HTTP in dry run")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(argv), 0)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            raw = output.read_text(encoding="utf-8")
            result = json.loads(raw)
            self.assertIsNone(result["passed"])
            self.assertEqual(len(result["workloads"]), 2)
            self.assertEqual(result["cases"], [])
            self.assertNotIn("PRIVATE_TEST_", raw)
            self.assertNotIn(str(root), raw)


if __name__ == "__main__":
    unittest.main()
