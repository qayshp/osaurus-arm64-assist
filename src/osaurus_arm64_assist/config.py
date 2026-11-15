"""Local configuration, with no machine-specific paths in the project."""
import json
import os
from pathlib import Path
import re
import secrets

MODES = ("cpu", "npu", "hybrid", "cpu-experts", "gpu", "gpu-experts")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def new_config(runtime_dir):
    return {"schema_version": 1, "runtime_dir": str(Path(runtime_dir).resolve()),
            "api_key": secrets.token_urlsafe(32), "host": "127.0.0.1", "port": 8082,
            "idle_seconds": 300, "memory_floor_mib": 512, "models": []}


def integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be an integer between {low} and {high}")
    return value


def validate(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    if isinstance(config.get("schema_version"), bool) or config.get("schema_version") != 1:
        raise ValueError("Unsupported configuration schema; expected schema_version 1")
    if config.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("This companion only serves on 127.0.0.1")
    integer(config.get("port", 8082), "port", 1024, 65535)
    integer(config.get("idle_seconds", 300), "idle_seconds", 0, 86400)
    integer(config.get("memory_floor_mib", 512), "memory_floor_mib", 256, 65536)
    if not isinstance(config.get("api_key"), str) or len(config["api_key"]) < 24:
        raise ValueError("Configuration needs an installation-specific API key of at least 24 characters")
    if not isinstance(config.get("runtime_dir"), str) or not config["runtime_dir"]:
        raise ValueError("runtime_dir is required")
    models = config.get("models", [])
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    seen = set()
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("Each model profile must be an object")
        identifier = model.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", identifier):
            raise ValueError("Each model needs a short id containing letters, numbers, dot, colon, slash or dash")
        if identifier in seen:
            raise ValueError(f"Duplicate model id: {identifier}")
        seen.add(identifier)
        if model.get("mode") not in MODES:
            raise ValueError(f"Unknown mode for {identifier}; choose from {', '.join(MODES)}")
        if not isinstance(model.get("path"), str) or not model["path"].lower().endswith(".gguf"):
            raise ValueError(f"{identifier} needs a GGUF file path")
        model_path = Path(model["path"])
        if not model_path.is_absolute():
            model["path"] = str((Path(config.get("_config_directory", "."))/model_path).resolve())
        for field, default, low, high in [("context", 2048, 128, 131072), ("threads", 6, 1, 128),
                ("batch_threads", 10, 1, 128), ("batch", 512, 1, 4096), ("microbatch", 512, 1, 4096)]:
            integer(model.get(field, default), f"{identifier}.{field}", low, high)
        if model.get("microbatch", 512) > model.get("batch", 512):
            raise ValueError(f"{identifier}: microbatch must not exceed batch")
        for field in ("repack", "allow_experimental_architecture", "enabled"):
            if field in model and not isinstance(model[field], bool):
                raise ValueError(f"{identifier}.{field} must be a boolean")
        if model.get("power_mode", "balanced") not in ("default", "balanced", "burst", "low_power_saver", "power_saver",
                "high_power_saver", "low_balanced", "high_performance", "sustained_high_performance"):
            raise ValueError(f"{identifier}.power_mode is not a supported Qualcomm power mode")
        for field, default in (("request_timeout_seconds", 900), ("load_timeout_seconds", 300)):
            integer(model.get(field, default), f"{identifier}.{field}", 1, 3600)
        integer(model.get("gpu_layers", -1), f"{identifier}.gpu_layers", -1, 1024)
        if model.get("fallback_mode") not in (None, "cpu"):
            raise ValueError("Only an explicit CPU fallback is supported")
        owned_root = Path(config.get("_config_directory", "."))
        if (owned_root/".osaurus-arm64-assist-install.json").is_file():
            if Path(model["path"]).resolve().is_relative_to(owned_root.resolve()):
                raise ValueError("Keep model files outside the companion installation so uninstall can preserve them")
    return config


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data["_config_directory"] = str(Path(path).resolve().parent)
    return validate(data)


def public_config(config):
    exported = {key: value for key, value in config.items() if key != "api_key" and not key.startswith("_")}
    exported["backup_kind"] = "osaurus-arm64-assist-settings-without-secret"
    return exported
