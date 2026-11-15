"""Read-only lifecycle comparison; publish receipts, never the private inputs.

capture-models writes a PRIVATE path/size/mtime/file-ID snapshot before cleanup.
compare emits counts and booleans only. It does not start, stop, install, connect,
run inference, change Osaurus, or hash large model files.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys


def load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Lifecycle input must be a JSON object")
    return value


def normalized_path(value):
    return os.path.normcase(str(Path(value).resolve()))


def model_stat(profile):
    path = Path(profile["path"])
    record = {"id": profile["id"], "path": str(path.resolve()), "exists": path.is_file()}
    if record["exists"]:
        info = path.stat()
        record.update(bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                      device_id=info.st_dev, file_id=info.st_ino)
    return record


def capture_models(config):
    profiles = config.get("models", [])
    if not isinstance(profiles, list):
        raise ValueError("Model profiles must be an array")
    return {"private_snapshot": True, "schema_version": 1,
            "model_profile_count": len(profiles), "models": [model_stat(p) for p in profiles]}


def other_config(value, provider_name):
    result = copy.deepcopy(value)
    providers = result.get("providers", [])
    if not isinstance(providers, list):
        raise ValueError("Osaurus provider list must be an array")
    result["providers"] = sorted(
        (p for p in providers if p["name"].casefold() != provider_name.casefold()),
        key=lambda p: p["name"].casefold())
    return result


def compare(osaurus_before, osaurus_after, *, provider_name="Osaurus ARM64 Assist",
            expected_provider="either", companion_before=None, companion_after=None,
            model_metadata=None, expect_key_rotation=False, expect_tokenless_after=False,
            install_root=None, expected_install="either"):
    old_other = other_config(osaurus_before, provider_name)
    new_other = other_config(osaurus_after, provider_name)
    owned_after = [p for p in osaurus_after.get("providers", [])
                   if p["name"].casefold() == provider_name.casefold()]
    checks = {
        "unrelated_osaurus_configuration_unchanged": old_other == new_other,
        "unrelated_providers_unchanged": old_other["providers"] == new_other["providers"],
        "agents_unchanged": osaurus_before.get("agents") == osaurus_after.get("agents"),
        "default_agent_unchanged": osaurus_before.get("default_agent") == osaurus_after.get("default_agent"),
        "owned_provider_name_unambiguous": len(owned_after) <= 1,
    }
    if expected_provider != "either":
        checks["expected_provider_state"] = len(owned_after) == (1 if expected_provider == "present" else 0)
    receipt = {"schema_version": 1, "private_paths_and_credentials_included": False,
               "osaurus_provider_count_before": len(osaurus_before.get("providers", [])),
               "osaurus_provider_count_after": len(osaurus_after.get("providers", [])),
               "osaurus_agent_count_before": len(osaurus_before.get("agents", [])),
               "osaurus_agent_count_after": len(osaurus_after.get("agents", [])),
               "owned_provider_present_after": len(owned_after) == 1,
               "model_content_hashing_performed": False, "checks": checks}
    if companion_before is not None and companion_after is not None:
        before_profiles = companion_before.get("models", [])
        after_profiles = companion_after.get("models", [])
        checks["model_profiles_preserved"] = sorted(before_profiles, key=lambda p: p["id"]) == sorted(after_profiles, key=lambda p: p["id"])
        checks["nonsecret_companion_settings_preserved"] = all(
            companion_before.get(k) == companion_after.get(k)
            for k in ("idle_seconds", "memory_floor_mib", "port", "origins")) and all(
            companion_before.get(k, default) == companion_after.get(k, default)
            for k, default in (("idle_source", "osaurus"), ("osaurus_url", "auto"), ("auto_start", True)))
        receipt["model_profile_count_before"] = len(before_profiles)
        receipt["model_profile_count_after"] = len(after_profiles)
        checks["registered_models_still_exist"] = all(Path(p["path"]).is_file() for p in after_profiles)
        old_key = companion_before.get("api_key")
        new_key = companion_after.get("api_key")
        receipt["after_companion_config_contains_api_key"] = "api_key" in companion_after
        receipt["installation_api_key_rotated"] = bool(old_key != new_key) if old_key and new_key else None
        if expect_key_rotation:
            checks["fresh_installation_api_key_rotated"] = bool(old_key and new_key and old_key != new_key)
        if expect_tokenless_after:
            checks["backup_omits_api_key"] = "api_key" not in companion_after
    elif expect_key_rotation or expect_tokenless_after:
        raise ValueError("Both companion configuration inputs are required for the selected credential check")
    if owned_after:
        desired_port = companion_after.get("port", 8082) if companion_after is not None else 8082
        desired_url = f"http://127.0.0.1:{desired_port}/v1"
        checks["owned_provider_endpoint_matches"] = all(p.get("base_url", "").rstrip("/") == desired_url for p in owned_after)
        checks["owned_provider_type_and_enabled_match"] = all(
            p.get("provider", "").casefold() in ("custom", "openaicompatible") and p.get("enabled") is True
            for p in owned_after)
    if model_metadata is not None:
        models = model_metadata.get("models", [])
        if not isinstance(models, list) or not models:
            raise ValueError("A nonempty private model metadata snapshot is required")
        current = [model_stat(p) for p in models]
        checks["model_files_existed_before"] = all(p.get("exists") for p in models)
        checks["model_files_still_exist"] = all(p["exists"] for p in current)
        checks["model_file_sizes_unchanged"] = all(a.get("bytes") == b.get("bytes") for a, b in zip(models, current))
        checks["model_file_modification_times_unchanged"] = all(a.get("mtime_ns") == b.get("mtime_ns") for a, b in zip(models, current))
        meaningful_ids = all(p.get("file_id") for p in models)
        if meaningful_ids:
            checks["model_file_identities_unchanged"] = all(
                (a.get("device_id"), a.get("file_id")) == (b.get("device_id"), b.get("file_id"))
                for a, b in zip(models, current))
        if companion_before is not None:
            expected = {(p["id"], normalized_path(p["path"])) for p in companion_before.get("models", [])}
            captured = {(p["id"], normalized_path(p["path"])) for p in models}
            checks["metadata_covers_model_profiles"] = expected == captured
        receipt["model_metadata_profile_count"] = len(models)
        receipt["unique_model_file_count"] = len({normalized_path(p["path"]) for p in models})
    if install_root is not None:
        root = Path(install_root)
        exists = root.is_dir()
        receipt["installation_directory_present_after"] = exists
        if expected_install != "either":
            checks["expected_installation_state"] = exists == (expected_install == "present")
        if exists:
            marker_path = root / ".osaurus-arm64-assist-install.json"
            marker = load(marker_path) if marker_path.is_file() else {}
            checks["owned_installation_marker_matches"] = (
                marker.get("product") == "org.osaurus-community.arm64-assist"
                and normalized_path(marker.get("root", "")) == normalized_path(root))
            resolved_root = root.resolve()
            checks["installation_contains_no_gguf_weights"] = next(root.rglob("*.gguf"), None) is None
            if companion_after is not None:
                checks["model_references_are_external_to_installation"] = all(
                    resolved_root != Path(p["path"]).resolve() and resolved_root not in Path(p["path"]).resolve().parents
                    for p in companion_after.get("models", []))
    receipt["passed"] = all(checks.values())
    return receipt


def write_output(path, value, inputs, *, private=False):
    output = Path(path).resolve()
    if output in {Path(p).resolve() for p in inputs if p}:
        raise ValueError("Output must not overwrite a lifecycle input")
    if output.exists():
        raise ValueError("Output already exists; choose a new receipt or snapshot filename")
    if private:
        project = Path(__file__).resolve().parents[1]
        if output == project or project in output.parents:
            raise ValueError("Private model metadata must be saved outside the public source checkout")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture-models", help="Save PRIVATE model path/size/mtime/file-ID metadata")
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("compare", help="Compare private snapshots and publish a sanitized receipt")
    check.add_argument("--osaurus-before", type=Path, required=True)
    check.add_argument("--osaurus-after", type=Path, required=True)
    check.add_argument("--provider-name", default="Osaurus ARM64 Assist")
    check.add_argument("--expected-provider", choices=("present", "absent", "either"), default="either")
    check.add_argument("--companion-before", type=Path)
    check.add_argument("--companion-after", type=Path)
    check.add_argument("--model-metadata-before", type=Path)
    check.add_argument("--expect-key-rotation", action="store_true")
    check.add_argument("--expect-tokenless-after", action="store_true")
    check.add_argument("--install-root", type=Path)
    check.add_argument("--expected-install", choices=("present", "absent", "either"), default="either")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture-models":
            snapshot = capture_models(load(args.config))
            write_output(args.output, snapshot, [args.config], private=True)
            print(json.dumps({"private_model_snapshot_saved": True, "model_profile_count": snapshot["model_profile_count"]}))
            return 0
        if bool(args.companion_before) != bool(args.companion_after):
            raise ValueError("Provide both companion configurations or neither")
        receipt = compare(load(args.osaurus_before), load(args.osaurus_after), provider_name=args.provider_name,
                          expected_provider=args.expected_provider,
                          companion_before=load(args.companion_before) if args.companion_before else None,
                          companion_after=load(args.companion_after) if args.companion_after else None,
                          model_metadata=load(args.model_metadata_before) if args.model_metadata_before else None,
                          expect_key_rotation=args.expect_key_rotation, expect_tokenless_after=args.expect_tokenless_after,
                          install_root=args.install_root, expected_install=args.expected_install)
        inputs = [args.osaurus_before, args.osaurus_after, args.companion_before,
                  args.companion_after, args.model_metadata_before]
        write_output(args.output, receipt, inputs)
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError):
        # Paths, JSON source excerpts and credential-bearing values stay private.
        print(json.dumps({"passed": False, "error": "Lifecycle input/output validation failed; inspect the supplied private paths locally."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
