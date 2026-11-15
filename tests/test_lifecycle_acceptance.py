import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("acceptance_tool", Path(__file__).resolve().parents[1]/"tools/lifecycle_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class LifecycleReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.model = self.root/"private-model.gguf"
        self.model.write_bytes(b"metadata-only test fixture")
        self.osaurus = {"version": 1, "providers": [{"name": "PRIVATE_PROVIDER_NAME", "provider": "custom", "enabled": True}],
                        "agents": [{"name": "PRIVATE_AGENT_NAME", "model": "PRIVATE_MODEL_ID", "system_prompt": "PRIVATE_PROMPT_CANARY"}],
                        "default_agent": {"model": "PRIVATE_DEFAULT_MODEL"}, "models": ["PRIVATE_REPOSITORY"]}
        self.config = {"schema_version": 1, "api_key": "PRIVATE_CREDENTIAL_OLD", "runtime_dir": str(self.root/"runtime-before"),
                       "port": 8082, "idle_seconds": 300, "models": [{"id": "PRIVATE_PROFILE_ID", "mode": "cpu", "path": str(self.model)}]}
        self.metadata = acceptance.capture_models(self.config)

    def test_reinstall_receipt_is_sanitized_and_checks_rotation(self):
        after = copy.deepcopy(self.osaurus)
        after["providers"].append({"name": "Osaurus ARM64 Assist", "provider": "openaicompatible", "enabled": True,
                                   "base_url": "http://127.0.0.1:8082/v1"})
        fresh = copy.deepcopy(self.config); fresh["api_key"] = "PRIVATE_CREDENTIAL_NEW"
        fresh["runtime_dir"] = str(self.root/"runtime-after")
        result = acceptance.compare(self.osaurus, after, expected_provider="present", companion_before=self.config,
                                    companion_after=fresh, model_metadata=self.metadata, expect_key_rotation=True)
        self.assertTrue(result["passed"])
        self.assertTrue(result["installation_api_key_rotated"])
        self.assertTrue(result["checks"]["model_profiles_preserved"])
        self.assertEqual(result["unique_model_file_count"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("PRIVATE_", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.model), serialized)

    def test_uninstall_can_compare_tokenless_backup_and_absent_root(self):
        backup = copy.deepcopy(self.config); backup.pop("api_key")
        result = acceptance.compare(self.osaurus, self.osaurus, expected_provider="absent", companion_before=self.config,
                                    companion_after=backup, model_metadata=self.metadata, expect_tokenless_after=True,
                                    install_root=self.root/"removed-install", expected_install="absent")
        self.assertTrue(result["passed"])
        self.assertIsNone(result["installation_api_key_rotated"])
        self.assertFalse(result["after_companion_config_contains_api_key"])

    def test_unrelated_configuration_edit_is_detected_without_exposing_it(self):
        after = copy.deepcopy(self.osaurus); after["agents"][0]["system_prompt"] = "PRIVATE_MODIFIED_PROMPT"
        result = acceptance.compare(self.osaurus, after)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["agents_unchanged"])
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_size_change_detected_without_reading_model_contents(self):
        timestamp = self.model.stat().st_mtime_ns
        self.model.write_bytes(b"new, differently sized fixture")
        os.utime(self.model, ns=(timestamp, timestamp))
        result = acceptance.compare(self.osaurus, self.osaurus, model_metadata=self.metadata)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["model_file_sizes_unchanged"])
        self.assertFalse(result["model_content_hashing_performed"])

    def test_wrong_owned_endpoint_is_detected(self):
        after = copy.deepcopy(self.osaurus)
        after["providers"].append({"name": "Osaurus ARM64 Assist", "provider": "custom", "enabled": True,
                                   "base_url": "https://example.invalid/v1"})
        result = acceptance.compare(self.osaurus, after, expected_provider="present")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["owned_provider_endpoint_matches"])

    def test_missing_model_is_detected(self):
        self.model.unlink()
        result = acceptance.compare(self.osaurus, self.osaurus, model_metadata=self.metadata)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["model_files_still_exist"])

    def test_input_cannot_be_overwritten(self):
        source = self.root/"snapshot.json"; source.write_text("{}")
        with self.assertRaises(ValueError):
            acceptance.write_output(source, {}, [source])
        self.assertEqual(source.read_text(), "{}")

    def test_private_capture_refuses_public_source_tree(self):
        public = Path(__file__).resolve().parents[1]/"evidence/private-fixture.json"
        with self.assertRaises(ValueError):
            acceptance.write_output(public, self.metadata, [], private=True)
        self.assertFalse(public.exists())

    def test_invalid_json_error_omits_path_and_source_excerpt(self):
        invalid = self.root/"private-input.json"; invalid.write_text('{"PRIVATE_SECRET_CANARY": invalid}')
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = acceptance.main(["capture-models", "--config", str(invalid), "--output", str(self.root/"private-metadata.json")])
        self.assertEqual(result, 2)
        self.assertNotIn("PRIVATE_SECRET_CANARY", stream.getvalue())
        self.assertNotIn(str(self.root), stream.getvalue())


if __name__ == "__main__":
    unittest.main()
