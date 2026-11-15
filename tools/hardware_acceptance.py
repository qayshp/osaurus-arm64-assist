"""Independent synthetic acceptance client. Never loads models or changes settings."""
import argparse
import http.client
import json
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SECRETS = []


def strict_json(data):
    def reject(value):
        raise ValueError("Non-finite JSON")
    return json.loads(data, parse_constant=reject)


def sanitized(value):
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()
                if key.lower() not in {"api_key", "authorization", "hostname", "path", "config_path", "runtime_dir"}}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, str):
        for secret in SECRETS:
            value = value.replace(secret, "[secret]")
        return re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s\"'<>|]*", "[local-path]", value)
    return value


class APIError(Exception):
    def __init__(self, status, kind):
        self.status, self.kind = status, kind


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class Client:
    def __init__(self, base, token=None, timeout=1800):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Only loopback HTTP URLs without credentials/query are allowed")
        self.base = base.rstrip("/").removesuffix("/v1")
        self.token, self.timeout = token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, body=None):
        started = time.perf_counter()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base + path, None if body is None else
            json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                timing = {"headers_seconds": time.perf_counter() - started}
                if body and body.get("stream"):
                    if "text/event-stream" not in response.headers.get("Content-Type", ""):
                        raise ValueError("Expected SSE content type")
                    result = self.sse(response, started, timing)
                else:
                    raw = response.read(4*1024*1024 + 1)
                    if len(raw) > 4*1024*1024:
                        raise ValueError("Response exceeds bounded client buffer")
                    result = strict_json(raw.decode("utf-8", errors="strict"))
                timing["complete_seconds"] = time.perf_counter() - started
                return result, timing
        except urllib.error.HTTPError as error:
            kind = "http_error"
            try:
                candidate = strict_json(error.read(65536).decode("utf-8"))["error"]["type"]
                if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,64}", candidate):
                    kind = candidate
            except (ValueError, KeyError, TypeError):
                pass
            raise APIError(error.code, kind) from None

    @staticmethod
    def sse(response, started, timing):
        text, fields, usage, assist, finish, done, total = [], [], None, None, None, False, 0
        while True:
            raw = response.readline(1024*1024 + 1)
            if not raw:
                break
            total += len(raw)
            if len(raw) > 1024*1024 or total > 8*1024*1024:
                raise ValueError("SSE exceeds bounded client buffer")
            timing.setdefault("first_wire_line_seconds", time.perf_counter() - started)
            line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
            if line.startswith("data:"):
                fields.append(line[5:].lstrip(" "))
            elif not line and fields:
                data, fields = "\n".join(fields), []
                if data == "[DONE]":
                    done = True
                    break
                event = strict_json(data)
                if "error" in event:
                    raise APIError(200, "sse_error")
                usage = event.get("usage") or usage
                assist = event.get("assist") or assist
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        if not isinstance(delta, str):
                            raise ValueError("SSE text delta must be a string")
                        timing.setdefault("first_text_seconds", time.perf_counter() - started)
                        text.append(delta)
                    finish = choice.get("finish_reason") or finish
        if not done or not finish:
            raise ValueError("SSE did not complete with finish_reason and [DONE]")
        return {"choices": [{"message": {"role": "assistant", "content": "".join(text)},
                "finish_reason": finish}], "usage": usage, "assist": assist, "sse_done": True}


def chat(model, messages, **options):
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    return {"model": model, "messages": messages, "temperature": 0, "max_tokens": 48, **options}


def content(result):
    return result["choices"][0]["message"].get("content") or ""


def numeric_metrics(value):
    return {key: numeric_metrics(item) if isinstance(item, dict) else item for key, item in (value or {}).items()
            if isinstance(item, (int, float, bool, dict)) or item is None}


def usage_valid(result):
    usage = result.get("usage") or {}
    values = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    return all(type(value) is int and value >= 0 for value in values) and values[0] > 0 and sum(values[:2]) == values[2]


def benchmark_result(result, payload):
    settings, runs = result.get("settings", {}), result.get("runs", [])
    rng = random.Random(payload["seed"])
    expected_ids = [rng.randrange(32000) for _ in range(payload["prompt_tokens"])]
    checks = {"settings_match": all(settings.get(key) == value for key, value in payload.items() if key != "model"),
        "deterministic_input_ids": result.get("input_ids") == expected_ids,
        "measured_runs_exact": len(runs) == payload["repetitions"], "warmup_runs_exact": len(result.get("warmup_runs", [])) == payload["warmup"],
        "exact_token_counts": all(usage_valid(run) and run["usage"]["prompt_tokens"] == payload["prompt_tokens"] and
            run["usage"]["completion_tokens"] == payload["generated_tokens"] and
            len(run.get("generated_ids", [])) == payload["generated_tokens"] for run in runs)}
    retained = [{"usage": {key: run.get("usage", {}).get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "generated_ids": run.get("generated_ids"), "metrics": numeric_metrics(run.get("metrics")),
        "assist": {key: run.get("assist", {}).get(key) for key in ("requested_mode", "actual_mode", "context_length")}} for run in runs]
    return {"passed": all(checks.values()), "checks": checks, "settings": {key: settings.get(key) for key in payload if key != "model"},
        "input_ids": result.get("input_ids"), "runs": retained, "summary": numeric_metrics(result.get("summary"))}


def exercise(client, target, profile, quick=False):
    rows, results = [], {}

    def run(name, request, expected, checker):
        row = {"case": name, "target": target, "request": request, "expected": expected}
        rows.append(row)
        try:
            result, timing = client.request("/v1/chat/completions", request)
            results[name] = result
            try:
                quality = bool(checker(result))
            except (ValueError, KeyError, TypeError, IndexError):
                quality = False
            checks = {"usage_consistent": usage_valid(result)}
            if name == "harbor_extract":
                checks["long_prompt_over_512_tokens"] = (result.get("usage") or {}).get("prompt_tokens", 0) > 512
            assist = result.get("assist") or {}
            if target == "companion":
                checks.update(requested_mode=assist.get("requested_mode") == profile["mode"],
                    actual_mode=assist.get("actual_mode") == profile["mode"], context_2048=assist.get("context_length") == 2048)
                if profile["mode"] == "hybrid":
                    checks["state_transferred"] = (assist.get("metrics") or {}).get("state_bytes", 0) > 0
            row.update(transport_ok=True, quality_ok=quality, contract_checks=checks,
                passed=quality and all(checks.values()), answer=result["choices"][0], usage=result.get("usage"),
                client_timing=timing, assist={key: assist.get(key) for key in
                    ("requested_mode", "actual_mode", "fallback_reason", "cold_load", "load_seconds_this_request", "architecture", "context_length")},
                stages=numeric_metrics(assist.get("metrics")), load_stages=numeric_metrics(assist.get("load_stages")))
            row["failure_category"] = None if row["passed"] else "quality" if not quality else "contract"
        except APIError as error:
            row.update(passed=False, transport_ok=True, failure_category="api", http_status=error.status, error_type=error.kind)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as error:
            row.update(passed=False, transport_ok=False, failure_category="transport", error_type=type(error).__name__)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            row.update(passed=False, transport_ok=False, failure_category="protocol", error_type=type(error).__name__)

    model = profile["id"]
    run("arithmetic", chat(model, "Reply with only the answer: what is 17 + 25?", max_tokens=24), "42", lambda x: content(x).strip() in ("42", "42."))
    run("unicode_sse", chat(model, "Reply with exactly this text, without quotes: café 你好", max_tokens=24,
        stream=True, stream_options={"include_usage": True}), "café 你好", lambda x: content(x).strip() == "café 你好" and x.get("sse_done"))
    if quick:
        return rows
    history = [{"role": "system", "content": "Follow the conversation and preserve the user's code."},
        {"role": "user", "content": "The code for this conversation is ORCHID42. Remember it."},
        {"role": "assistant", "content": "I will remember the code."},
        {"role": "user", "content": "What is the code? Reply with only the code."}]
    run("conversation_history", chat(model, history), "ORCHID42", lambda x: content(x).strip() == "ORCHID42")
    if target != "companion":
        return rows
    workload = strict_json((ROOT/"evidence/workloads.json").read_text(encoding="utf-8"))
    harbor = next(item for item in workload["cases"] if item["id"] == "long_extract")
    run("harbor_extract", chat(model, workload["request_prefix"].format(repeat=1)+harbor["prompt"], max_tokens=harbor["max_tokens"]),
        {"answer": workload["extraction_expected"], "minimum_prompt_tokens_exclusive": 512},
        lambda x: content(x).strip() == workload["extraction_expected"])
    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
              "required": ["a", "b"], "additionalProperties": False}
    tools = [{"type": "function", "function": {"name": "add_numbers", "description": "Add two integers.", "parameters": schema}}]
    forced = chat(model, "Use add_numbers to add 17 and 25.", max_tokens=128, tools=tools,
                  tool_choice={"type": "function", "function": {"name": "add_numbers"}})
    def tool_correct(result):
        calls = result["choices"][0]["message"].get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != "add_numbers":
            return False
        args = strict_json(calls[0]["function"]["arguments"])
        return set(args) == {"a", "b"} and all(type(x) is int for x in args.values()) and sorted(args.values()) == [17, 25]
    run("forced_tool", forced, {"function": "add_numbers", "arguments": {"a": 17, "b": 25}}, tool_correct)
    if rows[-1].get("quality_ok"):
        message = results["forced_tool"]["choices"][0]["message"]
        messages = forced["messages"] + [message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": "42"},
            {"role": "user", "content": "Reply with only the numeric result returned by the tool."}]
        run("tool_roundtrip", chat(model, messages, tools=tools, tool_choice="none"), "42", lambda x: content(x).strip() in ("42", "42."))
    else:
        rows.append({"case": "tool_roundtrip", "target": target, "passed": False, "failure_category": "dependency", "reason": "No valid forced tool call"})
    run("json_object", chat(model, "Return a JSON object with an integer field answer equal to 42.",
        max_tokens=64, response_format={"type": "json_object"}), {"answer": 42},
        lambda x: strict_json(content(x)) == {"answer": 42})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True); parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True); parser.add_argument("--quick", action="store_true")
    parser.add_argument("--osaurus-url"); parser.add_argument("--routing-id"); parser.add_argument("--benchmark-smoke", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if args.output.resolve() == args.config.resolve() or args.timeout <= 0 or bool(args.osaurus_url) != bool(args.routing_id):
        parser.error("Use a distinct receipt output, positive timeout, and both Osaurus routing options together")
    config = strict_json(args.config.read_text(encoding="utf-8-sig"))
    profile = next(item for item in config["models"] if item["id"] == args.model)
    SECRETS.append(config["api_key"])
    client = Client(f"http://127.0.0.1:{int(config.get('port', 8082))}", config["api_key"], args.timeout)
    core = strict_json((ROOT/"runtime.lock.json").read_text(encoding="utf-8")).get("llama_cpp_commit")
    receipt = {"schema_version": 1, "kind": "synthetic-hardware-acceptance", "quick": args.quick,
        "source": {"model_basename": Path(profile["path"]).name, "mode": profile["mode"], "context": profile.get("context", 2048), "expected_native_core": core},
        "scope": "API observations; this client does not sample physical NPU counters", "cases": []}
    try:
        listing, _ = client.request("/v1/models")
        entry = next(item for item in listing["data"] if item["id"] == args.model)
        receipt["advertised_context_2048"] = entry.get("context_length", entry.get("max_model_len")) == 2048
    except (APIError, OSError, ValueError, KeyError, StopIteration):
        receipt["advertised_context_2048"] = False
    receipt["cases"] = exercise(client, "companion", profile, args.quick)
    if args.osaurus_url:
        receipt["cases"] += exercise(Client(args.osaurus_url, timeout=args.timeout), "osaurus", {**profile, "id": args.routing_id}, args.quick)
    if args.benchmark_smoke:
        payload = {"model": args.model, "prompt_tokens": 32, "generated_tokens": 8, "repetitions": 1, "warmup": 0, "seed": 42}
        try:
            result, timing = client.request("/admin/benchmark", payload)
            receipt["benchmark_smoke"] = {"request": payload, **benchmark_result(result, payload), "client_timing": timing}
        except (APIError, OSError, ValueError, KeyError, TypeError) as error:
            receipt["benchmark_smoke"] = {"request": payload, "passed": False, "error_type": type(error).__name__}
    receipt["passed"] = receipt["advertised_context_2048"] and all(item.get("passed") for item in receipt["cases"]) and receipt.get("benchmark_smoke", {"passed": True})["passed"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(sanitized(receipt), ensure_ascii=False, allow_nan=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"passed": receipt["passed"], "cases": len(receipt["cases"]), "failed_cases": [item["case"] for item in receipt["cases"] if not item.get("passed")], "benchmark_smoke_passed": receipt.get("benchmark_smoke", {}).get("passed")}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, StopIteration) as error:
        print(json.dumps({"fatal_error": type(error).__name__, "message": "Acceptance setup failed; check configuration and model selection."}), file=sys.stderr)
        raise SystemExit(2)
