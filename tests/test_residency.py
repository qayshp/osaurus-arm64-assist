import copy
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from osaurus_arm64_assist.lifecycle import Manager
from osaurus_arm64_assist import residency
from tests import test_server_sync as server_fixture


def cold(**changes):
    return {"state": "cold", "heldBytes": 0, "managed": True, "busy": False,
            "generating": False, "keepLoaded": False, **changes}


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"id": "automatic/test", "mode": "hybrid", "synced": True, "settings_source": "osaurus",
                        "_osaurus": {"endpoint": "http://127.0.0.1:1337", "model_id": "publisher/model name"}}
        self.config = {"models": [self.profile]}

    def test_cold_and_unloaded_models_allow_allocation(self):
        for state in ("cold", "unloaded"):
            with self.subTest(state=state):
                reader = Mock(return_value=cold(state=state))
                self.assertIsNone(residency.native_admission(self.profile, self.config, reader=reader))
                self.assertEqual(reader.call_args.kwargs, {"timeout": 1.0})
                self.assertEqual(parse_qs(urlsplit(reader.call_args.args[1]).query)["model"], ["publisher/model name"])

    def test_resident_or_active_or_pinned_source_delegates_without_unload(self):
        for changes in ({"state": "loaded"}, {"state": "loading"}, {"state": "unloading"}, {"state": "busy"},
                        {"heldBytes": 1}, {"busy": True}, {"generating": True}, {"keepLoaded": True}):
            with self.subTest(changes=changes):
                reader = Mock(return_value=cold(**changes))
                self.assertEqual(residency.native_admission(self.profile, self.config, reader=reader), residency.POLICY)
                self.assertTrue(all(call.args[1].startswith("/admin/models/residency?model=") for call in reader.call_args_list))

    def test_unique_source_ids_only_for_selected_endpoint(self):
        other = copy.deepcopy(self.profile); other["_osaurus"]["model_id"] = "another/model"
        duplicate = copy.deepcopy(self.profile); duplicate["id"] = "old/alias"
        remote = copy.deepcopy(other); remote["_osaurus"]["endpoint"] = "http://127.0.0.1:1444"
        self.config["models"] += [other, duplicate, remote, {"id": "fixed", "mode": "cpu"}]
        reader = Mock(return_value=cold())
        self.assertIsNone(residency.native_admission(self.profile, self.config, reader=reader))
        self.assertEqual(reader.call_count, 2)
        self.assertTrue(all(call.args[0] == "http://127.0.0.1:1337" for call in reader.call_args_list))

    def test_missing_invalid_or_unavailable_source_delegates_with_safe_reason(self):
        for value in (None, [], {}, cold(state="unknown"), cold(heldBytes=True), cold(heldBytes=-1),
                      cold(busy="false"), cold(keepLoaded=None), cold(generating="no")):
            with self.subTest(value=value):
                self.assertEqual(residency.native_admission(self.profile, self.config, reader=Mock(return_value=value)), residency.UNAVAILABLE)
        reader = Mock(side_effect=TimeoutError("PRIVATE_TOKEN C:\\private\\model"))
        self.assertEqual(residency.native_admission(self.profile, self.config, reader=reader), residency.UNAVAILABLE)

    def test_fixed_and_unsynced_profiles_never_query_osaurus(self):
        reader = Mock(side_effect=AssertionError("No HTTP"))
        for changes in ({"synced": False}, {"settings_source": "fixed"}, {"mode": "osaurus"}):
            self.assertIsNone(residency.native_admission({**self.profile, **changes}, self.config, reader=reader))
        reader.assert_not_called()

    def test_only_a_reusable_ready_worker_bypasses_source_residency(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "synthetic.gguf"; model.write_bytes(b"never loaded")
            profile = {**self.profile, "path": str(model), "context": 8192, "repack": False}
            manager = Manager.__new__(Manager)
            manager.config = {"runtime_dir": "synthetic-runtime"}
            manager.lock = threading.Lock()
            manager.child = SimpleNamespace(is_alive=lambda: True)
            manager.ready = {"type": "ready"}
            manager.fingerprint = manager._load_fingerprint(profile)
            with manager.lock, patch("osaurus_arm64_assist.residency.native_admission", return_value=residency.POLICY) as check:
                # Sampling/default refreshes and aliases do not allocate again.
                for candidate in (profile, {**profile, "id": "another-alias"},
                                  {**profile, "sampling_defaults": {"temperature": .25}}):
                    self.assertIsNone(manager.native_admission(candidate, self.config))
                check.assert_not_called()
                for changes in ({"mode": "cpu"}, {"context": 16384}, {"repack": True}, {"threads": 2}):
                    with self.subTest(changes=changes):
                        candidate = {**profile, **changes}
                        self.assertEqual(manager.native_admission(candidate, self.config), residency.POLICY)
                        check.assert_called_with(candidate, self.config)
                check.reset_mock()
                manager.config["runtime_dir"] = "different-runtime"
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                check.assert_called_once_with(profile, self.config)

    def test_dead_unready_changed_or_inaccessible_model_rechecks_residency(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "synthetic.gguf"; model.write_bytes(b"never loaded")
            profile = {**self.profile, "path": str(model)}
            manager = Manager.__new__(Manager)
            manager.config = {"runtime_dir": "synthetic-runtime"}
            manager.child = SimpleNamespace(is_alive=lambda: True)
            manager.ready = {"type": "ready"}
            manager.fingerprint = manager._load_fingerprint(profile)
            with patch("osaurus_arm64_assist.residency.native_admission", return_value=residency.POLICY) as check:
                manager.ready = None
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                manager.ready = {"type": "ready"}
                manager.child = SimpleNamespace(is_alive=lambda: False)
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                manager.child = SimpleNamespace(is_alive=Mock(side_effect=ValueError("closed process")))
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                manager.child = SimpleNamespace(is_alive=lambda: True)
                model.write_bytes(b"changed model metadata, no inference")
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                model.unlink()
                self.assertEqual(manager.native_admission(profile, self.config), residency.POLICY)
                self.assertEqual(check.call_count, 5)


class ResidencyHttpIntegrationTests(unittest.TestCase):
    """Reuse the disposable HTTP fixture, never a real Osaurus app or model."""
    setUp = server_fixture.ServerSyncTests.setUp
    tearDown = server_fixture.ServerSyncTests.tearDown
    connection = server_fixture.ServerSyncTests.connection
    get = server_fixture.ServerSyncTests.get
    post = server_fixture.ServerSyncTests.post
    proxy = server_fixture.ServerSyncTests.proxy

    def test_residency_fallback_uses_original_body_under_admission_lock(self):
        def admission(profile, config):
            self.assertTrue(self.manager.lock.locked())
            self.assertEqual(profile["id"], "shared/Raptor")
            return residency.POLICY
        self.manager.native_admission = admission
        response, raw = self.post({"temperature": 0})
        self.assertEqual(response.status, 200, raw)
        profile, body = self.proxied[0]
        self.assertEqual(profile["_fallback_reason"], residency.POLICY)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("min_p", body)
        self.assertFalse(self.manager.requests)

    def test_unsupported_request_reason_precedes_residency_check(self):
        self.manager.native_admission = Mock(side_effect=AssertionError("No residency query for proxy request"))
        response, raw = self.post({"response_format": {"type": "json_schema"}})
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(self.proxied[0][0]["_fallback_reason"], "unsupported_request_features")
        self.manager.native_admission.assert_not_called()
