"""Measure long-context quality and stages against an explicitly fixed profile.

The client never loads DLLs or edits configuration. Only its operator runs model
requests. Random-token capacity/performance cases are separate from text quality.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import threading
import time

from hardware_acceptance import APIError, Client, ROOT, SECRETS, content, numeric_metrics, sanitized, strict_json, usage_valid


def positive_number(value, *, integer=False):
    return (type(value) is int if integer else type(value) in (int, float)) and math.isfinite(value) and value > 0


def workloads(records, seed):
    """All data, histories and answers are synthetic and reproducible."""
    rng = random.Random(seed)
    codes = {name: str(rng.randrange(10000000, 100000000)) for name in ("early", "middle", "late")}
    lines = [f"Line {index:05d}: routine shipment checked and filed." for index in range(records)]
    for name, position in (("early", 0), ("middle", records // 2), ("late", records - 1)):
        lines[position] = f"The {name.upper()} shipment approval code is {codes[name]}."
    question = 'Return only a JSON object with string fields early, middle, late containing the three approval codes.'
    document = [{"role": "system", "content": "Extract facts exactly from the supplied synthetic shipping document."},
                {"role": "user", "content": "BEGIN DOCUMENT\n" + "\n".join(lines) + "\nEND DOCUMENT\n" + question}]
    # A source fact in an early turn must survive many later conversation turns.
    history = [{"role": "system", "content": "Preserve the user's shipment approval codes throughout this conversation."},
               {"role": "user", "content": lines[0]},
               {"role": "assistant", "content": "I have recorded the early approval code."}]
    middle = records // 2
    for offset in range(1, records - 1, 32):
        history += [{"role": "user", "content": "\n".join(lines[offset:min(offset + 32, records - 1)])},
                    {"role": "assistant", "content": "The shipment records are noted."}]
    history += [{"role": "user", "content": lines[-1] + "\n" + question}]
    return [{"id": "document_three_positions", "messages": document, "expected": codes,
             "needle_positions": {"early": 0, "middle": middle, "late": records - 1}},
            {"id": "conversation_three_positions", "messages": history, "expected": codes,
             "history_messages": len(history)}]


class MemorySampler:
    """Observe global memory via cheap health; no process paths or identities."""
    def __init__(self, base):
        self.client = Client(base, timeout=1)
        self.stop = threading.Event()
        self.samples = []
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set() and len(self.samples) < 8192:
            try:
                value, _ = self.client.request("/health")
                memory = value.get("memory") or {}
                row = {"elapsed_seconds": time.perf_counter() - self.started}
                for key in ("available_physical_bytes", "available_commit_bytes"):
                    if type(memory.get(key)) is int:
                        row[key] = memory[key]
                if len(row) > 1:
                    self.samples.append(row)
            except (APIError, OSError, ValueError, KeyError):
                pass
            self.stop.wait(.25)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(1.5)

    def report(self):
        return {"scope": "global available physical RAM and commit, not per-process allocation or power",
                "samples": self.samples,
                "minimum_available": {key: min(row[key] for row in self.samples if key in row)
                    for key in ("available_physical_bytes", "available_commit_bytes")
                    if any(key in row for row in self.samples)}}


def exception_result(error):
    if isinstance(error, APIError):
        return {"failure_category": "api", "http_status": error.status, "error_type": error.kind}
    return {"failure_category": "transport_or_protocol", "error_type": type(error).__name__}


def run_quality(client, base, profile, case, context, minimum, repetitions, output_tokens, expect_cold):
    rows = []
    request = {"model": profile["id"], "messages": case["messages"], "temperature": 0,
               "seed": 42, "max_tokens": output_tokens, "response_format": {"type": "json_object"},
               "stream": True, "stream_options": {"include_usage": True}}
    for repeat in range(repetitions):
        row = {"case": case["id"], "model": profile["id"], "repeat": repeat,
               "warmth": "cold_model" if expect_cold and repeat == 0 else "resident_model_full_history_reprefill",
               "expected": case["expected"], "request": request, "passed": False}
        with MemorySampler(base) as monitor:
            try:
                result, timing = client.request("/v1/chat/completions", request)
                assist, usage = result.get("assist") or {}, result.get("usage") or {}
                stages = assist.get("metrics") or {}
                answer = content(result)
                try:
                    quality = strict_json(answer) == case["expected"]
                except (ValueError, TypeError):
                    quality = False
                checks = {"usage_consistent": usage_valid(result), "strict_sse_completed": result.get("sse_done") is True,
                          "long_input_verified": type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] >= minimum,
                          "within_context": type(usage.get("total_tokens")) is int and usage["total_tokens"] <= context,
                          "actual_native_mode": assist.get("actual_mode") == profile["mode"],
                          "actual_context": assist.get("context_length") == context,
                          "no_stock_engine_fallback": not assist.get("fallback_reason"),
                          "continuation_exercised": usage.get("completion_tokens", 0) > 1}
                if profile["mode"] == "hybrid":
                    checks["state_transferred"] = positive_number(stages.get("state_bytes"), integer=True)
                    checks["handoff_timed"] = positive_number(stages.get("state_transfer_seconds"))
                if expect_cold and repeat == 0:
                    checks["cold_model_observed"] = assist.get("cold_load") is True
                elif repeat > 0:
                    checks["resident_model_observed"] = assist.get("cold_load") is False
                row.update(transport_ok=True, quality_ok=quality, contract_checks=checks,
                           passed=quality and all(checks.values()), answer=answer, usage=usage, client_timing=timing,
                           assist={key: assist.get(key) for key in ("requested_mode", "actual_mode", "cold_load",
                               "load_seconds_this_request", "architecture", "context_length", "fallback_reason")},
                           stages=numeric_metrics(stages), load_stages=numeric_metrics(assist.get("load_stages")))
                row["failure_category"] = None if row["passed"] else "quality" if not quality else "contract"
            except (APIError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
                row.update(exception_result(error))
        row["memory"] = monitor.report()
        rows.append(row)
    return rows


def run_benchmark(client, base, profile, prompt_tokens, generated_tokens):
    payload = {"model": profile["id"], "prompt_tokens": prompt_tokens, "generated_tokens": generated_tokens,
               "repetitions": 2, "warmup": 0, "seed": 42}
    row = {"request": payload, "purpose": "exact random-token capacity/performance only; not answer quality", "passed": False}
    with MemorySampler(base) as monitor:
        try:
            result, timing = client.request("/admin/benchmark", payload)
            ids, runs = result.get("input_ids", []), result.get("runs", [])
            rng = random.Random(42)
            checks = {"exact_input": ids == [rng.randrange(32000) for _ in range(prompt_tokens)], "two_runs": len(runs) == 2,
                "settings_match": all(result.get("settings", {}).get(key) == value for key, value in payload.items()),
                "no_warmups": result.get("warmup_runs") == [] and all(run.get("warmup") is False for run in runs),
                "exact_prompt_usage": all(usage_valid(run) and run["usage"]["prompt_tokens"] == prompt_tokens for run in runs),
                "exact_output": all(run.get("usage", {}).get("completion_tokens") == generated_tokens and
                    len(run.get("generated_ids", [])) == generated_tokens for run in runs),
                "actual_mode": all(run.get("assist", {}).get("actual_mode") == profile["mode"] for run in runs),
                "actual_context": all(run.get("assist", {}).get("context_length") == profile["context"] for run in runs)}
            if profile["mode"] == "hybrid":
                checks["state_transferred"] = all(positive_number(run.get("metrics", {}).get("state_bytes"), integer=True) for run in runs)
            row.update(passed=all(checks.values()), checks=checks, client_timing=timing,
                       input_ids_sha256=hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest(),
                       runs=[{"usage": run.get("usage"), "generated_ids": run.get("generated_ids"),
                              "stages": numeric_metrics(run.get("metrics")),
                              "assist": {key: run.get("assist", {}).get(key) for key in
                                  ("requested_mode", "actual_mode", "context_length", "cold_load", "load_seconds_this_request")}}
                             for run in runs], summary=numeric_metrics(result.get("summary")))
        except (APIError, OSError, ValueError, KeyError, TypeError) as error:
            row.update(exception_result(error))
    row["memory"] = monitor.report()
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--compare-model", help="Optional fixed CPU profile of the same model and context")
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--records", type=int, help="Synthetic filler records; actual tokenizer length is checked afterward")
    parser.add_argument("--minimum-prompt-tokens", type=int)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument("--benchmark-prompts", type=int, nargs="*", default=[])
    parser.add_argument("--cold", action="store_true", help="Unload the companion's own worker before each profile")
    parser.add_argument("--dry-run", action="store_true", help="Write synthetic workloads without HTTP or loading models")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if (args.output.resolve() == args.config.resolve() or not 4096 <= args.context <= 131072
            or not 1 <= args.repetitions <= 5 or not 16 <= args.output_tokens <= 2048 or args.timeout <= 0):
        parser.error("Use a distinct output, context 4K..128K, repetitions 1..5, output tokens 16..2048, and a positive timeout")
    records = args.records if args.records is not None else args.context // 20
    minimum = args.minimum_prompt_tokens if args.minimum_prompt_tokens is not None else max(2049, args.context // 2)
    if not 8 <= records <= 10000 or not 2049 <= minimum < args.context - args.output_tokens:
        parser.error("Use 8..10000 records and a minimum verified prompt length above 2K with output room")
    if any(not 1 <= n <= args.context - 32 for n in args.benchmark_prompts):
        parser.error("Each exact benchmark prompt must leave 32 output tokens within the configured context")
    config = strict_json(args.config.read_text(encoding="utf-8-sig"))
    profiles = []
    for identifier in [args.model] + ([args.compare_model] if args.compare_model else []):
        profile = next(item for item in config["models"] if item["id"] == identifier)
        if profile.get("mode") == "osaurus" or profile.get("context", 2048) != args.context or (
                config.get("models_source", "osaurus") != "fixed" and profile.get("settings_source") != "fixed"):
            parser.error("Every tested profile must explicitly use fixed settings and the requested native context")
        profiles.append(profile)
    if args.compare_model and (profiles[1]["mode"] != "cpu" or Path(profiles[0]["path"]).resolve() != Path(profiles[1]["path"]).resolve()):
        parser.error("The comparison must be CPU using the same model file")
    cases = workloads(records, 42)
    receipt = {"schema_version": 1, "kind": "long-context-acceptance", "context": args.context, "records": records,
               "expected_native_core": strict_json((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))["llama_cpp_commit"],
               "minimum_actual_prompt_tokens": minimum, "seed": 42, "dry_run": args.dry_run,
               "scope": "Synthetic text quality plus exact random-token performance; no power or physical NPU counter sampling",
               "warmth_note": "Resident weights/contexts; the current engine clears KV and prefills supplied history on every request",
               "workloads": cases, "profiles": [], "cases": [], "benchmarks": []}
    if not args.dry_run:
        SECRETS.append(config["api_key"])
        base = f"http://127.0.0.1:{int(config.get('port', 8082))}"
        client = Client(base, config["api_key"], args.timeout)
        listing, _ = client.request("/v1/models")
        for profile in profiles:
            advertised = next(item for item in listing["data"] if item["id"] == profile["id"])
            if advertised.get("context_length") != args.context:
                raise ValueError("The service did not advertise the requested fixed context")
            settings = ("mode", "context", "batch", "microbatch", "threads", "batch_threads", "power_mode", "repack",
                        "gpu_layers", "flash_attention", "kv_unified", "kv_offload", "swa_full", "op_offload", "settings_source")
            receipt["profiles"].append({"id": profile["id"], "mode": profile["mode"], "context": args.context,
                "model_basename": Path(profile["path"]).name,
                "configuration": {key: profile[key] for key in settings if key in profile}})
            if args.cold:
                client.request("/admin/unload", {})
            for index, case in enumerate(cases):
                receipt["cases"] += run_quality(client, base, profile, case, args.context, minimum,
                    args.repetitions, args.output_tokens, args.cold and index == 0)
            for prompt in args.benchmark_prompts:
                receipt["benchmarks"].append(run_benchmark(client, base, profile, prompt, 32))
    receipt["passed"] = None if args.dry_run else all(row["passed"] for row in receipt["cases"] + receipt["benchmarks"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sanitized(receipt), ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dry_run": args.dry_run, "passed": receipt["passed"], "cases": len(receipt["cases"]),
                      "quality_failures": sum(row.get("quality_ok") is False for row in receipt["cases"]),
                      "contract_failures": sum(row.get("failure_category") == "contract" for row in receipt["cases"])}))
    return 0 if args.dry_run or receipt["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (APIError, OSError, ValueError, KeyError, StopIteration) as error:
        print(json.dumps({"fatal_error": type(error).__name__, "message": "Context acceptance setup or API failed."}), file=sys.stderr)
        raise SystemExit(2)
