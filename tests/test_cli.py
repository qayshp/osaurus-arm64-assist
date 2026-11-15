"""Registry and backup CLI checks; no native library or real model required."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from osaurus_arm64_assist import cli
from osaurus_arm64_assist import config


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = self.root/"install"/"settings.json"
        self.runtime = self.root/"runtime"
        self.models = self.root/"osaurus"/"models"
        self.run_cli(["init", "--config", str(self.settings), "--runtime-dir", str(self.runtime)])

    def run_cli(self, arguments, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                cli.main(arguments)
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, expected, f"stdout={stdout.getvalue()} stderr={stderr.getvalue()}")
        return (json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None), stderr.getvalue()

    def model(self, relative="publisher/模型/Raptor-Q6_K.gguf", content=b"GGUF synthetic unit-test bytes, not an inference model"):
        path = self.models/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def add(self, path, identifier="raptor/cpu", mode="cpu", extra=()):
        return self.run_cli(["models", "add", "--config", str(self.settings), "--id", identifier,
            "--mode", mode, "--path", str(path), *extra])

    def test_discovery_uses_osaurus_files_without_copying_them(self):
        path = self.model()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        results, error = self.run_cli(["models", "discover", "--models-dir", str(self.models)])
        self.assertEqual(error, "")
        self.assertEqual(results, [{"osaurus_model": "Raptor-Q6_K", "path": str(path.resolve()), "bytes": path.stat().st_size}])
        registered, _ = self.run_cli(["models", "add", "--config", str(self.settings), "--id", "raptor/hybrid",
            "--mode", "hybrid", "--osaurus-model", "raptor-q6_k", "--models-dir", str(self.models)])
        self.assertIs(registered["copied_model"], False)
        self.assertIs(registered["execution_verified"], False)
        saved = config.load(self.settings)["models"][0]
        self.assertTrue(os.path.samefile(saved["path"], path))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(list(self.settings.parent.rglob("*.gguf")), [])

    def test_default_discovery_is_scoped_to_local_osaurus_models(self):
        local = self.root/"local-app-data"
        path = local/"osaurus"/"models"/"publisher"/"Example.GGUF"
        path.parent.mkdir(parents=True); path.write_bytes(b"synthetic")
        (path.parent/"readme.txt").write_text("not a model", encoding="utf-8")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            result, _ = self.run_cli(["models", "discover"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], str(path.resolve()))

    def test_ambiguous_osaurus_names_are_rejected_until_path_is_explicit(self):
        first = self.model("first/Raptor-Q6_K.gguf")
        self.model("second/Raptor-Q6_K.gguf", b"different synthetic bytes")
        before = self.settings.read_bytes()
        output, error = self.run_cli(["models", "add", "--config", str(self.settings), "--id", "raptor/hybrid",
            "--mode", "hybrid", "--osaurus-model", "Raptor-Q6_K", "--models-dir", str(self.models)], expected=2)
        self.assertIsNone(output)
        self.assertIn("exactly one", json.loads(error)["error"])
        self.assertEqual(self.settings.read_bytes(), before)
        self.add(first)
        self.assertEqual(config.load(self.settings)["models"][0]["path"], str(first.resolve()))

    def test_two_profiles_can_reference_one_file_and_removal_preserves_it(self):
        path = self.model(); before = path.read_bytes()
        self.add(path, "raptor/cpu", "cpu")
        self.add(path, "raptor/hybrid", "hybrid")
        output, _ = self.run_cli(["models", "remove", "--config", str(self.settings), "--id", "raptor/hybrid"])
        self.assertIs(output["deleted_model"], False)
        self.assertEqual(path.read_bytes(), before)
        listed, _ = self.run_cli(["models", "list", "--config", str(self.settings)])
        self.assertEqual([value["id"] for value in listed], ["raptor/cpu"])
        self.assertIs(listed[0]["exists"], True)

    def test_replacing_a_profile_is_explicit_and_preserves_both_model_files(self):
        first = self.model("one.gguf", b"one"); second = self.model("two.gguf", b"two")
        self.add(first)
        before = self.settings.read_bytes()
        self.run_cli(["models", "add", "--config", str(self.settings), "--id", "raptor/cpu",
            "--mode", "cpu", "--path", str(second)], expected=2)
        self.assertEqual(self.settings.read_bytes(), before)
        self.add(second, extra=("--replace",))
        self.assertEqual(config.load(self.settings)["models"][0]["path"], str(second.resolve()))
        self.assertEqual(first.read_bytes(), b"one"); self.assertEqual(second.read_bytes(), b"two")

    def test_init_never_clobbers_an_existing_key_or_profile(self):
        path = self.model(); self.add(path)
        before = self.settings.read_bytes(); key = config.load(self.settings)["api_key"]
        output, error = self.run_cli(["init", "--config", str(self.settings), "--runtime-dir", str(self.root/"another-runtime")], expected=2)
        self.assertIsNone(output)
        self.assertIn("already exists", error)
        self.assertNotIn(key, error)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_export_cannot_replace_the_live_configuration(self):
        self.add(self.model())
        before = self.settings.read_bytes()
        output, error = self.run_cli(["config", "export", "--config", str(self.settings),
            "--output", str(self.settings.parent/"."/"config-alias"/".."/self.settings.name)], expected=2)
        self.assertIsNone(output)
        self.assertIn("must not overwrite", json.loads(error)["error"])
        self.assertEqual(self.settings.read_bytes(), before)

    def test_local_api_bypasses_environment_proxy_configuration(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.StringIO('{"unloaded":true}')
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid:8888",
                "HTTPS_PROXY": "http://proxy.invalid:8888", "NO_PROXY": ""}), \
                patch.object(cli.urllib.request, "build_opener", return_value=opener) as build:
            result = cli.api(config.load(self.settings), "/admin/unload", {})
        self.assertEqual(result, {"unloaded": True})
        self.assertEqual(len(build.call_args.args), 1)
        self.assertIsInstance(build.call_args.args[0], cli.urllib.request.ProxyHandler)
        self.assertEqual(build.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8082/admin/unload")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer "+config.load(self.settings)["api_key"])

    def test_tokenless_backup_preserves_a_new_installation_key_and_runtime(self):
        path = self.model(); self.add(path, "raptor/hybrid", "hybrid")
        source = config.load(self.settings); source["port"] = 8092; source["idle_seconds"] = 60
        source.pop("_config_directory"); config.atomic_json(self.settings, source)
        backup_path = self.root/"backup.json"
        exported, _ = self.run_cli(["config", "export", "--config", str(self.settings), "--output", str(backup_path)])
        backup = json.loads(backup_path.read_text())
        self.assertIs(exported["contains_api_key"], False)
        self.assertNotIn("api_key", backup)
        self.assertNotIn(source["api_key"], backup_path.read_text())
        self.assertNotIn("_config_directory", backup)
        destination = self.root/"new-install"/"settings.json"
        new_runtime = self.root/"new-runtime"
        self.run_cli(["init", "--config", str(destination), "--runtime-dir", str(new_runtime)])
        fresh = config.load(destination)
        # A backup cannot replace the installation-local token, runtime or bind policy.
        backup.update(api_key="untrusted-backup-key-should-not-win", runtime_dir="untrusted-runtime", host="0.0.0.0")
        backup_path.write_text(json.dumps(backup), encoding="utf-8")
        imported, _ = self.run_cli(["config", "import", "--config", str(destination), "--input", str(backup_path)])
        current = config.load(destination)
        self.assertIs(imported["new_installation_key_and_runtime_preserved"], True)
        self.assertEqual(current["api_key"], fresh["api_key"])
        self.assertNotEqual(current["api_key"], source["api_key"])
        self.assertEqual(current["runtime_dir"], str(new_runtime.resolve()))
        self.assertEqual(current["host"], "127.0.0.1")
        self.assertEqual(current["models"], config.load(self.settings)["models"])
        self.assertEqual(current["port"], 8092)
        self.assertEqual(current["idle_seconds"], 60)
        self.assertEqual(path.read_bytes(), b"GGUF synthetic unit-test bytes, not an inference model")

    def test_import_refuses_models_inside_the_owned_installation(self):
        (self.settings.parent/".osaurus-arm64-assist-install.json").write_text("{}", encoding="utf-8")
        before = self.settings.read_bytes()
        backup = self.root/"bad-backup.json"
        backup.write_text(json.dumps({"models": [{"id": "unsafe", "path": str(self.settings.parent/"weights.gguf"), "mode": "cpu"}]}), encoding="utf-8")
        self.run_cli(["config", "import", "--config", str(self.settings), "--input", str(backup)], expected=2)
        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
