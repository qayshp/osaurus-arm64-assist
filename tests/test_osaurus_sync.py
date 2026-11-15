import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from osaurus_arm64_assist import osaurus_sync as sync


def gguf(path, *, architecture="spark2_5", chat=True, file_type=18, split_count=1, split_no=0, general_type=None):
    values = {"general.architecture": architecture, "general.file_type": file_type,
              "split.count": split_count, "split.no": split_no}
    if chat:
        values["tokenizer.chat_template"] = "synthetic template, never inference"
    if general_type is not None:
        values["general.type"] = general_type
    def string(value):
        encoded = value.encode(); return struct.pack("<Q", len(encoded)) + encoded
    data = b"GGUF" + struct.pack("<IQQ", 3, 0, len(values))
    for key, value in values.items():
        data += string(key)
        data += struct.pack("<I", 8 if isinstance(value, str) else 4)
        data += string(value) if isinstance(value, str) else struct.pack("<I", value)
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    return path


class Source:
    def __init__(self, directory):
        self.runtime = {"localModelsDir": str(directory), "localGpuLayers": None, "localContextSize": None,
                        "localAccelerator": "auto", "globalProxyUrl": "PRIVATE_PROXY_TOKEN"}
        self.entries, self.settings, self.resolved = [], {}, {}
        self.downloads = {"downloads": []}
        self.calls = []
        self.failure = None

    def add(self, path, *, identifier=None, **changes):
        identifier = identifier or path.stem
        relative = str(path.relative_to(Path(self.runtime["localModelsDir"])))
        entry = {"id": identifier, "fileName": path.name, "relativePath": relative, "sizeBytes": path.stat().st_size,
                 "kind": "model", "external": False, "architecture": "spark2_5", "quantization": "Q6_K", **changes}
        self.entries.append(entry)
        self.settings[identifier] = {"modelId": identifier, "override": None,
            "defaults": {"batchSize": 2048, "uBatchSize": 512, "useMmap": True, "kvOffload": True}}
        self.resolved[identifier] = {"rows": [], "argv": ["PRIVATE_ARGV_CANARY"]}
        return entry

    def read(self, endpoint, path, **kwargs):
        self.calls.append((endpoint, path))
        if self.failure:
            raise self.failure
        if path == "/admin/runtime-settings": return copy.deepcopy(self.runtime)
        if path == "/admin/models": return {"models": copy.deepcopy(self.entries)}
        if path == "/admin/models/downloads": return copy.deepcopy(self.downloads)
        for identifier in self.settings:
            base = "/admin/models/" + quote(identifier, safe="") + "/settings"
            if path == base: return copy.deepcopy(self.settings[identifier])
            if path == base + "/resolved": return copy.deepcopy(self.resolved[identifier])
        raise sync.SyncError("http_404")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.models = self.root / "models"
        self.install = self.root / "install"; self.install.mkdir()
        self.config_path = self.install / "config.json"
        self.config = {"osaurus_url": "http://127.0.0.1:1337", "models": []}
        self.source = Source(self.models)
        self.patch = mock.patch.object(sync, "read_json", side_effect=self.source.read)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.policy = sync.SharedModels(self.config_path, lambda: self.config, autostart=False)
        self.addCleanup(self.policy.close)

    def add(self, name="Raptor.gguf", **changes):
        return self.source.add(gguf(self.models / name), **changes)

    def profiles(self):
        return self.policy.profiles(self.config["models"])

    def poll(self):
        state = self.policy.poll_once()
        self.assertEqual(state["status"], "live", state)
        return state

    def test_completed_chat_inventory_native_and_unknown_proxy_without_weight_copy(self):
        self.add()
        self.source.add(gguf(self.models / "new-chat.gguf", architecture="new_architecture"), architecture="new_architecture", identifier="new/chat")
        self.source.add(gguf(self.models / "embedding.gguf"), kind="embedding")
        self.source.add(gguf(self.models / "speech.gguf", architecture="whisper", chat=False), architecture="whisper")
        bad = self.models / "bad.gguf"; bad.write_bytes(b"not GGUF")
        self.source.add(bad)
        self.poll(); profiles = self.profiles()
        self.assertEqual(len(profiles), 2)
        self.assertEqual({profile["mode"] for profile in profiles}, {"hybrid", "osaurus"})
        self.assertTrue(all(profile["synced"] for profile in profiles))
        self.assertTrue(all(profile["_osaurus"]["endpoint"] == self.config["osaurus_url"] for profile in profiles))
        self.assertEqual(list(self.install.rglob("*.gguf")), [])
        self.assertFalse(any(path.startswith("/v1/") for _, path in self.source.calls))

    def test_generated_ids_are_stable_and_collision_resistant(self):
        self.add(identifier="org/model name")
        self.add("second.gguf", identifier="org-model-name")
        self.poll(); first = [profile["id"] for profile in self.profiles()]
        self.poll(); self.assertEqual([profile["id"] for profile in self.profiles()], first)
        self.assertEqual(len(set(first)), 2)
        self.assertTrue(all(value.startswith("osaurus/") and len(value) <= 128 for value in first))

    def test_profiles_are_pure_and_coherent_and_return_deep_copies(self):
        entry = self.add()
        path = str(self.models / entry["relativePath"])
        self.config["models"] = [{"id": "legacy/cpu", "path": path, "mode": "cpu", "context": 512}]
        self.poll()
        with mock.patch.object(Path, "open", side_effect=AssertionError("No file reads")), \
                mock.patch.object(Path, "stat", side_effect=AssertionError("No file stats")), \
                mock.patch.object(sync, "read_json", side_effect=AssertionError("No HTTP")):
            profiles = self.profiles()
            alias = next(profile for profile in profiles if profile["id"] == "legacy/cpu")
            self.assertEqual(alias["mode"], "cpu")
            self.assertEqual(alias["context"], 2048)
            profiles[0]["_osaurus"]["model_id"] = "changed"
            self.assertNotEqual(self.profiles()[0]["_osaurus"]["model_id"], "changed")

    def test_fixed_manual_alias_stays_exact_and_missing_manual_files_are_hidden(self):
        entry = self.add(); path = str(self.models / entry["relativePath"])
        fixed = {"id": "legacy/exact", "mode": "cpu", "path": path, "context": 1024, "settings_source": "fixed"}
        missing = {"id": "missing", "mode": "cpu", "path": str(self.models / "gone.gguf")}
        self.config["models"] = [fixed, missing]
        self.poll()
        self.assertEqual(next(profile for profile in self.profiles() if profile["id"] == fixed["id"]), fixed)
        self.assertFalse(any(profile["id"] == "missing" for profile in self.profiles()))

    def test_manual_id_collision_wins_and_fixed_source_never_uses_http(self):
        entry = self.add(); path = str(self.models / entry["relativePath"])
        fixed = {"id": sync.automatic_id(entry["id"]), "mode": "cpu", "path": path, "settings_source": "fixed"}
        self.config["models"] = [fixed]; self.poll()
        self.assertEqual(self.profiles(), [fixed])
        self.config["models_source"] = "fixed"; self.source.calls.clear()
        self.assertEqual(self.policy.poll_once()["status"], "fixed")
        self.assertEqual(self.profiles(), [fixed]); self.assertEqual(self.source.calls, [])

    def test_settings_refresh_inherits_aliases_without_resolved_or_header_churn(self):
        entry = self.add(); identifier = entry["id"]
        self.config["models"] = [{"id": "old/cpu", "mode": "cpu", "path": str(self.models / entry["relativePath"])}]
        with mock.patch.object(sync.model_settings, "inspect_gguf", wraps=sync.model_settings.inspect_gguf) as inspect:
            self.poll(); self.poll()
            self.assertEqual(inspect.call_count, 1)
            self.assertEqual(sum(path.endswith("/resolved") for _, path in self.source.calls), 1)
            revision = self.policy.revision
            self.source.settings[identifier]["override"] = {"temperature": .2, "threads": 8}
            self.poll()
            alias = next(profile for profile in self.profiles() if profile["id"] == "old/cpu")
            self.assertEqual(alias["threads"], 8)
            self.assertEqual(alias["sampling_defaults"]["temperature"], .2)
            self.assertEqual(self.policy.revision, revision)
            self.source.runtime["localContextSize"] = 4096
            self.poll()
            self.assertGreater(self.policy.revision, revision)
            self.assertEqual(sum(path.endswith("/resolved") for _, path in self.source.calls), 3)
            self.assertEqual(inspect.call_count, 1)

    def test_successful_removal_hides_automatic_and_known_aliases_but_keeps_config_and_files(self):
        entry = self.add(); path = self.models / entry["relativePath"]
        self.config["models"] = [{"id": "alias", "mode": "cpu", "path": str(path)}]
        before = copy.deepcopy(self.config); self.poll()
        self.assertEqual(len(self.profiles()), 2)
        self.source.entries = []; self.poll()
        self.assertEqual(self.profiles(), [])
        self.assertEqual(self.config, before); self.assertTrue(path.is_file())

    def test_downloads_exclude_pending_and_unknown_shapes_admit_no_new_files(self):
        old = self.add(); self.poll()
        self.add("new.gguf", identifier="new")
        self.source.downloads = {"downloads": [{"opaqueUnknownSchema": "private"}]}
        self.assertEqual(self.poll()["error"], "unsupported_download_state")
        self.assertEqual(len(self.profiles()), 1)
        self.source.downloads = {"downloads": [{"modelId": old["id"], "status": "downloading"}]}
        self.poll(); self.assertEqual(len(self.profiles()), 1)
        self.assertEqual(self.profiles()[0]["_osaurus"]["model_id"], "new")
        self.source.downloads = {"downloads": []}; self.poll()
        self.assertEqual(len(self.profiles()), 2)

    def test_paths_are_contained_and_external_records_require_absolute_paths(self):
        entry = self.add()
        for relative in ("../Raptor.gguf", "a/../../Raptor.gguf", "C:\\foreign\\model.gguf", str(self.models / "Raptor.gguf")):
            candidate = {**entry, "relativePath": relative}
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                sync.resolve_model_path(candidate, self.source.runtime)
        external = gguf(self.root / "external.gguf")
        self.source.entries = [{**entry, "external": True, "relativePath": str(external), "fileName": external.name, "sizeBytes": external.stat().st_size}]
        self.poll(); self.assertEqual(len(self.profiles()), 1)
        self.source.entries[0]["relativePath"] = "external.gguf"
        self.poll(); self.assertEqual(self.profiles(), [])

    def test_size_architecture_and_split_sets_must_match(self):
        entry = self.add(); entry["sizeBytes"] += 1
        self.poll(); self.assertEqual(self.profiles(), [])
        entry["sizeBytes"] -= 1; entry["architecture"] = "wrong_architecture"
        self.poll(); self.assertEqual(self.profiles(), [])
        self.source.entries = []
        first = gguf(self.models / "split-00001-of-00002.gguf", split_count=2)
        self.source.add(first, identifier="split")
        self.poll(); self.assertEqual(self.profiles(), [])
        second = gguf(self.models / "split-00002-of-00002.gguf", split_count=2, split_no=1)
        self.poll(); self.assertEqual(len(self.profiles()), 1)
        gguf(second, split_count=2, split_no=1, architecture="wrong")
        self.poll(); self.assertEqual(self.profiles(), [])

    def test_errors_keep_last_good_and_cache_is_private_allowlist_and_source_bound(self):
        entry = self.add(); identifier = entry["id"]
        self.source.settings[identifier]["override"] = {"extraArgs": ["--secret", "PRIVATE_SETTINGS_VALUE"]}
        self.source.resolved[identifier]["rows"] = [{"key": "n_ctx", "value": "2048", "source": "plan", "reason": "PRIVATE_REASON"}]
        self.poll(); before = self.profiles()
        cache_path = self.install / "state" / "osaurus-models.json"
        self.assertNotIn("PRIVATE", cache_path.read_text())
        self.assertNotIn("globalProxyUrl", cache_path.read_text())
        self.source.failure = sync.SyncError("http_401")
        self.assertEqual(self.policy.poll_once()["status"], "cached")
        self.assertEqual(self.profiles(), before)
        restarted = sync.SharedModels(self.config_path, lambda: self.config, autostart=False)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.snapshot()["status"], "cached")
        self.assertEqual(restarted.profiles([]), before)
        self.config["osaurus_url"] = "http://127.0.0.1:1444"
        self.policy.poll_once(); self.assertEqual(self.profiles(), [])
        diagnostic = json.dumps(self.policy.snapshot())
        self.assertNotIn(str(self.root), diagnostic); self.assertNotIn("127.0.0.1", diagnostic)

    def test_malformed_settings_or_duplicate_inventory_keeps_last_good(self):
        entry = self.add(); self.poll(); before = self.profiles()
        self.source.settings[entry["id"]] = {"override": ["PRIVATE_BAD_VALUE"], "defaults": {}}
        self.policy.poll_once(); self.assertEqual(self.profiles(), before)
        self.assertEqual(self.policy.snapshot()["error"], "invalid_model_settings")
        self.source.entries.append(copy.deepcopy(entry))
        self.policy.poll_once(); self.assertEqual(self.profiles(), before)
        self.assertEqual(self.policy.snapshot()["error"], "duplicate_source_model_id")

    def test_managed_path_symlink_cannot_escape_models_directory(self):
        self.models.mkdir()
        outside = gguf(self.root / "outside" / "escape.gguf")
        link = self.models / "alias"
        try:
            link.symlink_to(outside.parent, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # A junction needs no symlink privilege. Both paths are inside this
            # disposable test directory; removal below removes only the link.
            command = str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe")
            result = subprocess.run([command, "/d", "/c", "mklink", "/J", str(link), str(outside.parent)],
                                    capture_output=True, timeout=5, check=False)
            if result.returncode:
                self.skipTest("This filesystem supports neither directory symlinks nor junctions")
            self.addCleanup(os.rmdir, link)
        else:
            self.addCleanup(link.unlink)
        self.source.entries = [{"id": "escape", "fileName": outside.name, "relativePath": "alias/escape.gguf",
                                "sizeBytes": outside.stat().st_size, "kind": "model", "external": False}]
        self.poll(); self.assertEqual(self.profiles(), [])
        self.assertTrue(outside.is_file())

    def test_background_poll_does_not_block_profiles_and_close_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def slow_read(*args, **kwargs):
            entered.set(); release.wait(timeout=2)
            return copy.deepcopy(self.source.runtime)
        with mock.patch.object(sync, "read_json", side_effect=slow_read):
            policy = sync.SharedModels(self.config_path, lambda: self.config, poll_seconds=.01)
            self.addCleanup(policy.close); self.addCleanup(release.set)
            self.assertTrue(entered.wait(timeout=1))
            started = time.monotonic()
            self.assertEqual(policy.profiles([]), [])
            self.assertEqual(policy.snapshot()["status"], "unavailable")
            self.assertLess(time.monotonic() - started, .5)
            started = time.monotonic(); policy.close()
            self.assertLess(time.monotonic() - started, 1.7)
            release.set(); policy.close()
            self.assertFalse(policy._thread.is_alive())


class HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        reply = self.server.reply
        try:
            if reply.get("delay"):
                time.sleep(reply["delay"])
            self.send_response(reply.get("status", 200))
            body = reply.get("body", b'{"models":[]}')
            self.send_header("Content-Length", str(len(body)))
            if "location" in reply: self.send_header("Location", reply["location"])
            self.end_headers(); self.wfile.write(body)
        except (OSError, BrokenPipeError): pass
    def log_message(self, *_): pass


class HttpTests(unittest.TestCase):
    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
        server.daemon_threads = True; server.reply = {}; server.requests = []
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=1)))
        return server, f"http://127.0.0.1:{server.server_port}"

    def test_direct_http_bypasses_proxy_and_uses_only_osaurus_key(self):
        server, endpoint = self.server(); proxy, proxy_endpoint = self.server()
        with mock.patch.dict(os.environ, {"OSAURUS_API_KEY": "PRIVATE_AUTH", "HTTP_PROXY": proxy_endpoint,
                                          "http_proxy": proxy_endpoint, "ALL_PROXY": proxy_endpoint,
                                          "all_proxy": proxy_endpoint, "NO_PROXY": "", "no_proxy": ""}):
            self.assertEqual(sync.read_json(endpoint, "/admin/models"), {"models": []})
        self.assertEqual(proxy.requests, [])
        self.assertEqual(server.requests, [("/admin/models", "Bearer PRIVATE_AUTH")])

    def test_redirect_and_oversized_or_bad_json_and_auth_failures_are_safe(self):
        server, endpoint = self.server(); target, target_endpoint = self.server()
        for reply, code in (({"status": 302, "location": target_endpoint}, "redirect_refused"),
                            ({"body": b"PRIVATE_INVALID_JSON"}, "invalid_json"),
                            ({"status": 401, "body": b"PRIVATE_AUTH_FAILURE"}, "http_401"),
                            ({"body": b"x" * (sync.MAX_BYTES + 1)}, "invalid_response_size")):
            server.reply = reply
            with self.assertRaises(sync.SyncError) as raised:
                sync.read_json(endpoint, "/admin/models")
            self.assertEqual(str(raised.exception), code)
        self.assertEqual(target.requests, [])

    def test_socket_deadline_limits_unresponsive_local_api(self):
        server, endpoint = self.server()
        server.reply = {"delay": .3}
        started = time.monotonic()
        with self.assertRaises(sync.SyncError) as raised:
            sync.read_json(endpoint, "/admin/models", timeout=.1)
        self.assertEqual(str(raised.exception), "network_timeout")
        self.assertLess(time.monotonic() - started, .5)


if __name__ == "__main__":
    unittest.main()
