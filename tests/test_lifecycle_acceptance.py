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
        self.assertTrue(result["passed"], result["checks"])
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
        self.assertTrue(result["passed"], result["checks"])
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

    def test_model_metadata_matches_a_canonicalized_path_alias(self):
        if os.name == "nt":
            # Windows runners can expose TEMP through a DOS short name such as
            # RUNNER~1. Path.resolve() expands it when capture records the model.
            import ctypes
            from ctypes import wintypes
            get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            get_short_path.restype = wintypes.DWORD
            needed = get_short_path(str(self.model), None, 0)
            if not needed:
                self.skipTest("Filesystem does not supply DOS short paths")
            buffer = ctypes.create_unicode_buffer(needed)
            written = get_short_path(str(self.model), buffer, needed)
            if not written or written >= needed:
                self.skipTest("Filesystem does not supply a stable DOS short path")
            alias = Path(buffer.value)
        else:
            alias_directory = self.root / "model-directory-alias"
            alias_directory.symlink_to(self.root, target_is_directory=True)
            alias = alias_directory / self.model.name
        if os.path.normcase(os.path.abspath(alias)) == os.path.normcase(os.path.abspath(self.model)):
            self.skipTest("Filesystem has no distinct path alias for this fixture")
        config = copy.deepcopy(self.config)
        config["models"][0]["path"] = str(alias)
        metadata = acceptance.capture_models(config)
        result = acceptance.compare(self.osaurus, self.osaurus, companion_before=config,
                                    companion_after=config, model_metadata=metadata)
        self.assertTrue(result["passed"], result["checks"])
        self.assertTrue(result["checks"]["metadata_covers_model_profiles"])
        self.assertTrue(result["checks"]["model_file_identities_unchanged"])

    def test_installed_root_has_no_weight_copy_and_references_external_model(self):
        install = self.root/"install"; install.mkdir()
        (install/".osaurus-arm64-assist-install.json").write_text(json.dumps({"product": "org.osaurus-community.arm64-assist", "root": str(install)}))
        result = acceptance.compare(self.osaurus, self.osaurus, companion_before=self.config, companion_after=self.config,
                                    install_root=install, expected_install="present")
        self.assertTrue(result["passed"], result["checks"])
        self.assertTrue(result["checks"]["installation_contains_no_gguf_weights"])
        self.assertTrue(result["checks"]["model_references_are_external_to_installation"])
        (install/"unwanted-copy.gguf").write_bytes(b"duplicate fixture")
        result = acceptance.compare(self.osaurus, self.osaurus, install_root=install, expected_install="present")
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["installation_contains_no_gguf_weights"])

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
