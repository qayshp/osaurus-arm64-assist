"""Deterministic fixed-token workload; no model, prompt template or local data."""
import math
import random
import statistics

from .config import integer


def normalize_benchmark(body, context_limit):
    if not isinstance(body, dict):
        raise ValueError("Benchmark request must be an object")
    allowed = {"model", "prompt_tokens", "generated_tokens", "repetitions", "warmup", "seed"}
    if set(body) - allowed:
        raise ValueError("Unknown benchmark settings; direct token IDs are not accepted over HTTP")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise ValueError("Benchmark model must name a configured profile")
    result = {"model": body["model"]}
    for name, default, low, high in (("prompt_tokens", 512, 1, 131071),
            ("generated_tokens", 128, 1, 131071), ("repetitions", 3, 1, 10),
            ("warmup", 1, 0, 3), ("seed", 42, 0, 0xFFFFFFFF)):
        result[name] = integer(body.get(name, default), name, low, high)
    if result["prompt_tokens"] + result["generated_tokens"] > context_limit:
        raise ValueError("Benchmark prompt plus generated tokens exceeds the configured context")
    return result


def input_ids(settings):
    rng = random.Random(settings["seed"])
    return [rng.randrange(32000) for _ in range(settings["prompt_tokens"])]


def summary(runs):
    names = {name for run in runs for name, value in run["metrics"].items()
             if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
    medians = {}
    for name in sorted(names):
        values = [run["metrics"].get(name) for run in runs]
        if all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in values):
            medians[name] = statistics.median(values)
    return {"measured_repetitions": len(runs), "median_metrics": medians,
            "median_manager_seconds": statistics.median(run["manager_seconds"] for run in runs),
            "loading_excluded_from_engine_request_seconds": True,
            "warmup_excluded": True}
