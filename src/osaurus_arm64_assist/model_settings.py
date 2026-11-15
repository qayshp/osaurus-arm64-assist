"""Pure mapping of allowlisted Osaurus model settings into execution profiles.

This module never executes argv, reads weights, opens a socket, or loads a DLL.
Catalog callers may inject cached inspect_gguf output as entry['_gguf'].
"""
import hashlib
import json
import math
import re

from .gguf_metadata import inspect_gguf

SAMPLING = {
    "temperature": ("temperature", .8, 0, 2, False),
    "topK": ("top_k", 40, 0, 1000000, True),
    "topP": ("top_p", .95, 0, 1, False),
    "minP": ("min_p", .05, 0, 1, False),
    "repeatPenalty": ("repeat_penalty", 1., .01, 10, False),
    "presencePenalty": ("presence_penalty", 0., -2, 2, False),
    "frequencyPenalty": ("frequency_penalty", 0., -2, 2, False),
    "repeatLastN": ("repeat_last_n", 64, -1, 131072, True),
    "seed": ("seed", -1, -1, 0xFFFFFFFF, True),
}
SUPPORTED = set(SAMPLING) | {
    "contextSize", "gpuLayerCount", "batchSize", "uBatchSize", "threads", "batchThreads",
    "flashAttention", "cacheTypeK", "cacheTypeV", "repack", "useMmap", "loadMode",
    "kvOffload", "opOffload", "kvUnified", "swaFull", "chatTemplateKwargs", "reasoningPreserve",
    "reasoning", "cpuMoe",
}
RESOLVED_KEYS = {item[0] for item in SAMPLING.values()} | {
    "context_size", "gpu_layers", "batch_size", "ubatch_size", "threads", "batch_threads",
    "flash_attention", "cache_type_k", "cache_type_v", "kv_offload", "op_offload", "kv_unified",
    "swa_full", "repack", "chat_template_kwargs", "load_mode",
}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_SAFE_WORDS = {"auto", "f16", "fp16", "mmap", "dio", "true", "false", "same as Threads", "from the model"}
_BOOL_FIELDS = {"flashAttention", "repack", "useMmap", "kvOffload", "opOffload", "kvUnified", "swaFull", "reasoningPreserve", "cpuMoe"}


def _safe_names(value):
    return {name for name in value if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name)} if isinstance(value, list) else set()


def _number(value, *, integer=False, display=False):
    if display and isinstance(value, str):
        expression = _INTEGER if integer else _NUMBER
        if len(value) > 64 or not expression.fullmatch(value):
            raise ValueError("Invalid resolved number")
        value = int(value) if integer else float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("A finite typed number is required")
    if integer and type(value) is not int:
        raise ValueError("An integer is required")
    return value


def _kwargs(value):
    if isinstance(value, str):
        if len(value) > 8192:
            raise ValueError("Template options are too large")
        value = json.loads(value)
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("Template options must be a small object")
    # These are the template controls implemented and exercised by this alpha.
    # Other custom options stay with the stock engine; raw templates/secrets
    # are not copied into the catalog's settings cache.
    if any(key not in ("enable_thinking", "preserve_thinking") or type(item) is not bool for key, item in value.items()):
        raise ValueError("Unsupported template options")
    return dict(value)


def sanitize_settings(settings):
    """Drop private/unsupported values while retaining their names for fallback."""
    if not isinstance(settings, dict):
        return {"defaults": {}, "override": {}, "_unsupported": ["settings"]}
    defaults = settings.get("defaults") or {}
    overrides = settings.get("override") or {}
    if not isinstance(defaults, dict) or not isinstance(overrides, dict):
        return {"defaults": {}, "override": {}, "_unsupported": ["settings"]}
    unsupported = _safe_names(settings.get("_unsupported", []))
    safe_defaults, safe_overrides = {}, {}
    for key in SUPPORTED:
        value = defaults.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            unsupported.add(key)
            continue
        if value is None or isinstance(value, (bool, int, float)) or key in ("cacheTypeK", "cacheTypeV", "loadMode", "reasoning") and isinstance(value, str) and value in ("f16", "F16", "fp16", "auto", "mmap", "dio", "off", "on", "none"):
            safe_defaults[key] = value
    for key, value in overrides.items():
        if value is None:
            continue
        if key not in SUPPORTED:
            # Overrides are sparse Nullable<T> fields in Osaurus. Even a
            # value equal to an engine default is an explicit preference.
            # The one known harmless loading preference matches this ABI.
            if not (key == "useMlock" and value is False):
                unsupported.add(key if isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", key) else "unknownSetting")
            continue
        try:
            if key == "chatTemplateKwargs":
                safe_overrides[key] = _kwargs(value)
            elif key in _BOOL_FIELDS:
                if type(value) is not bool: raise ValueError("Boolean required")
                safe_overrides[key] = value
            elif key in ("cacheTypeK", "cacheTypeV", "loadMode", "reasoning"):
                if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    raise ValueError("Invalid option")
                safe_overrides[key] = value
            else:
                safe_overrides[key] = _number(value, integer=key in {"contextSize", "gpuLayerCount", "batchSize", "uBatchSize", "threads", "batchThreads", "topK", "repeatLastN", "seed"})
        except (ValueError, TypeError):
            unsupported.add(key)
    return {"defaults": safe_defaults, "override": safe_overrides, "_unsupported": sorted(unsupported)}


def sanitize_resolved(resolved):
    """Retain known display values only; never argv, reasons, paths or extras."""
    if not isinstance(resolved, dict) or not isinstance(resolved.get("rows", []), list):
        return {"rows": [], "_invalid": ["resolved"]}
    rows, invalid, seen = [], _safe_names(resolved.get("_invalid", [])), set()
    for row in resolved.get("rows", []):
        if not isinstance(row, dict) or row.get("key") not in RESOLVED_KEYS:
            continue
        key = row["key"]
        if key in seen:
            invalid.add(key); continue
        seen.add(key)
        value = row.get("value")
        try:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Non-finite resolved value")
            if key == "chat_template_kwargs" and value is not None:
                value = _kwargs(value)
            elif value is not None and not (isinstance(value, (bool, int, float)) or isinstance(value, str) and len(value) <= 64 and (_NUMBER.fullmatch(value) or value in _SAFE_WORDS)):
                raise ValueError("Unsafe resolved value")
            source = row.get("source")
            rows.append({"key": key, "value": value, "source": source if source in ("engine", "model", "plan", "global", "override", "user") else "unknown", "applies": row.get("applies", True) is True})
        except (ValueError, TypeError):
            invalid.add(key)
    return {"rows": rows, "_invalid": sorted(invalid)}


def build_profile(entry, settings, resolved, runtime, path, *, identifier=None, mode=None):
    """Build a native or stock-engine proxy profile from an immutable snapshot."""
    if not isinstance(entry, dict) or not isinstance(runtime, dict) or not isinstance(entry.get("id"), str):
        raise ValueError("A model entry, runtime settings and source model ID are required")
    settings, resolved = sanitize_settings(settings), sanitize_resolved(resolved)
    defaults, overrides = settings["defaults"], settings["override"]
    rows = {row["key"]: row for row in resolved["rows"] if row["applies"]}
    header = entry.get("_gguf") if isinstance(entry.get("_gguf"), dict) else {}
    architecture = header.get("architecture") or entry.get("architecture")
    quantization = header.get("quantization") or entry.get("quantization")
    quantization = quantization.upper() if isinstance(quantization, str) else None
    known_spark = architecture == "spark2_5" and quantization == "Q6_K"
    known_moe = architecture == "qwen35moe" and quantization in ("Q4_K_M", "Q2_K")
    requested = mode or "auto"
    if requested not in ("auto", "osaurus", "cpu", "npu", "gpu", "hybrid", "cpu-experts", "gpu-experts"):
        raise ValueError("Unknown requested execution mode")
    selected = "hybrid" if known_spark else "cpu-experts" if known_moe else "osaurus"
    if requested != "auto": selected = requested
    applied, adapted, unsupported = set(), set(), set(settings["_unsupported"]) | set(resolved["_invalid"])
    reason = None
    def fallback(code, name=None):
        nonlocal reason
        if reason is None: reason = code
        if name: unsupported.add(name)
    if selected == "osaurus": fallback("stock_engine_requested" if requested == "osaurus" else "native_compatibility_unverified")
    if selected == "hybrid" and not known_spark or selected in ("cpu-experts", "gpu-experts") and not known_moe:
        fallback("native_strategy_unverified")
    if entry.get("kind") in ("embedding", "speech", "projector", "adapter") or header.get("general_type") == "adapter":
        fallback("non_chat_model")
    if header.get("has_chat_template") is False:
        fallback("embedded_template_missing")
    if unsupported: fallback("unsupported_explicit_setting")

    def integer_value(name, default, low, high):
        value = overrides.get(name, defaults.get(name))
        if value is None: value = default
        try:
            value = _number(value, integer=True)
            if not low <= value <= high: raise ValueError("Out of range")
        except ValueError:
            fallback("invalid_explicit_setting", name); return default
        if name in overrides or defaults.get(name) is not None: applied.add(name)
        return value

    training = header.get("context_length") or entry.get("contextLength")
    training = training if type(training) is int and 128 <= training <= 2**31-1 else None
    proxy_context = None
    candidates = [rows.get("context_size", {}).get("value"), entry.get("bestFitContext"), entry.get("contextLength"), header.get("context_length")]
    for value in candidates:
        try:
            value = _number(value, integer=True, display=True)
            if 128 <= value <= 1048576 and (training is None or value <= training):
                proxy_context = value; break
        except ValueError: pass
    proxy_context = proxy_context or 2048
    explicit_context = overrides.get("contextSize", runtime.get("localContextSize"))
    context = 2048
    if explicit_context is None:
        adapted.add("contextSize")
    else:
        try:
            context = _number(explicit_context, integer=True)
            if not 128 <= context <= 2048 or training is not None and context > training:
                raise ValueError("Outside validated context")
            applied.add("contextSize")
        except ValueError:
            fallback("context_outside_validated_range", "contextSize"); context = 2048

    gpu_preference = overrides.get("gpuLayerCount", runtime.get("localGpuLayers"))
    for value in (gpu_preference,):
        if value is not None:
            try:
                value = _number(value, integer=True)
                if value < -1: raise ValueError("Invalid layer count")
                if value == 0: fallback("explicit_cpu_preference", "gpuLayerCount")
                elif value > 0 and not selected.startswith("gpu"):
                    fallback("gpu_placement_unsupported", "gpuLayerCount")
                elif selected.startswith("gpu"): applied.add("gpuLayerCount")
                else: adapted.add("gpuLayerCount")
            except ValueError: fallback("invalid_explicit_setting", "gpuLayerCount")
    accelerator = runtime.get("localAccelerator")
    if accelerator is not None and (not isinstance(accelerator, str) or accelerator.casefold() not in ("auto", "")):
        fallback("explicit_accelerator_preference", "localAccelerator")
    residents = runtime.get("maxResidentModels", 1)
    if type(residents) is not int or residents != 1:
        fallback("multiple_resident_models_unsupported", "maxResidentModels")

    size = entry.get("sizeBytes", header.get("size_bytes"))
    large = type(size) is not int or size > 8 * 1024**3
    expert = selected in ("cpu-experts", "gpu-experts")
    repack = not large and not expert
    if "repack" in overrides:
        repack = overrides["repack"]; applied.add("repack")
        if repack and (large or expert): fallback("large_or_expert_repack_unsupported", "repack")
    elif defaults.get("repack") is False:
        repack = False; applied.add("repack")
    elif not repack: adapted.add("repack")
    flash = not selected.startswith("gpu")
    if "flashAttention" in overrides:
        flash = overrides["flashAttention"]; applied.add("flashAttention")
        if flash and selected.startswith("gpu"): fallback("opencl_flash_attention_unsupported", "flashAttention")
    else: adapted.add("flashAttention")
    for name in ("cacheTypeK", "cacheTypeV"):
        value = overrides.get(name, defaults.get(name))
        if value is not None:
            if not isinstance(value, str) or value.casefold() not in ("f16", "fp16"):
                fallback("cache_type_unsupported", name)
            else: applied.add(name)
    if overrides.get("useMmap", defaults.get("useMmap", True)) is False:
        fallback("loading_policy_unsupported", "useMmap")
    if "loadMode" in overrides and overrides["loadMode"].casefold() not in ("auto", "mmap"):
        fallback("loading_policy_unsupported", "loadMode")
    if rows.get("load_mode", {}).get("value") not in (None, "mmap"):
        adapted.add("loadMode")

    sampling = {}
    for name, (key, default, low, high, integral) in SAMPLING.items():
        explicit = name in overrides
        value = overrides[name] if explicit else rows.get(key, {}).get("value")
        if value is None: value = default
        try:
            value = _number(value, integer=integral, display=not explicit)
            if not low <= value <= high or key == "top_p" and value == 0: raise ValueError("Out of range")
            if key == "seed" and value == -1: value = 0xFFFFFFFF
            sampling[key] = value; applied.add(name)
        except ValueError:
            sampling[key] = 0xFFFFFFFF if key == "seed" else default
            fallback("sampling_setting_unsupported", name)
    template = rows.get("chat_template_kwargs", {}).get("value") or {}
    template = dict(template) if isinstance(template, dict) else {}
    if "chatTemplateKwargs" in overrides:
        template.update(overrides["chatTemplateKwargs"])
    if "reasoningPreserve" in overrides:
        template["preserve_thinking"] = overrides["reasoningPreserve"]
    if overrides.get("reasoning") in ("off", "none"):
        template["enable_thinking"] = False
    elif "reasoning" in overrides and overrides["reasoning"] != "auto":
        fallback("thinking_not_supported", "reasoning")
    if template.get("enable_thinking") is True:
        fallback("thinking_not_supported", "chatTemplateKwargs")
    if template: applied.add("chatTemplateKwargs")
    if "cpuMoe" in overrides and overrides["cpuMoe"] and not expert:
        fallback("expert_policy_unsupported", "cpuMoe")
    if overrides.get("cpuMoe") is False and expert:
        fallback("expert_policy_unsupported", "cpuMoe")

    identifier = identifier or entry["id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", identifier):
        identifier = "shared/" + hashlib.sha256(entry["id"].encode()).hexdigest()[:20]
    profile = {"id": identifier, "path": str(path), "mode": selected, "context": context,
        "batch": integer_value("batchSize", 2048, 1, 4096),
        "microbatch": integer_value("uBatchSize", 512, 1, 4096),
        "threads": integer_value("threads", 6, 1, 128), "batch_threads": integer_value("batchThreads", 10, 1, 128),
        "repack": repack, "flash_attention": flash, "power_mode": "balanced",
        "sampling_defaults": sampling, "template_kwargs": template,
        "_osaurus": {"model_id": entry["id"]}, "_requested_mode": requested,
        "_fallback_reason": None, "execution_verified": False}
    if profile["microbatch"] > profile["batch"]:
        fallback("invalid_batch_configuration", "uBatchSize")
    for name, target, default in (("kvOffload", "kv_offload", True), ("opOffload", "op_offload", True),
                                  ("kvUnified", "kv_unified", True), ("swaFull", "swa_full", False)):
        profile[target] = overrides.get(name, defaults.get(name))
        if profile[target] is None: profile[target] = default
        if type(profile[target]) is not bool:
            fallback("invalid_explicit_setting", name); profile[target] = default
        else: applied.add(name)
    gpu_layers = overrides.get("gpuLayerCount", runtime.get("localGpuLayers"))
    if selected.startswith("gpu") and type(gpu_layers) is int and gpu_layers != 0:
        profile["gpu_layers"] = gpu_layers
    if reason:
        profile.update(mode="osaurus", context=proxy_context, _fallback_reason=reason)
    profile["sync"] = {"applied": sorted(applied), "adapted": sorted(adapted), "unsupported": sorted(unsupported)}
    return profile
