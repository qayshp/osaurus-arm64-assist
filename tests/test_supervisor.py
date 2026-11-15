"""Automatic lifecycle decisions with fake apps/controllers and local fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from osaurus_arm64_assist import supervisor

OWNER = "11111111-1111-4111-8111-111111111111"
INSTANCE = "22222222-2222-4222-8222-222222222222"


def sample(state="present", pid=7, created=133000000000000007):
    return {"state": state, "processes": [{"pid": pid, "creation_filetime": created}] if state == "present" else []}


class FakeMonitor:
    def __init__(self, *samples):
        self.samples = list(samples)
        self.last = sample("absent")
        self.calls = self.closed = 0

    def sample(self):
        self.calls += 1
        if self.samples: self.last = self.samples.pop(0)
        return copy.deepcopy(self.last)

    def close(self):
        self.closed += 1


class FakeController:
    def __init__(self, running=False, starts=(True,), stops=(True,)):
        self.is_running = running
        self.starts, self.stops = list(starts), list(stops)
        self.start_count = self.stop_count = self.closed = 0
        self.refreshes = []

    def running(self):
        return self.is_running

    def start(self):
        self.start_count += 1
        result = self.starts.pop(0) if self.starts else True
        if result: self.is_running = True
        return result

    def stop(self):
        self.stop_count += 1
        result = self.stops.pop(0) if self.stops else True
        if result: self.is_running = False
        return result

    def refresh_discovery(self, config, now):
        self.refreshes.append((dict(config), now))

    def close(self):
        self.closed += 1


class LifecycleStateMachineTests(unittest.TestCase):
    def loop(self, *samples, controller=None):
        monitor, controller = FakeMonitor(*samples), controller or FakeController()
        return supervisor.LifecycleSupervisor(monitor, controller), monitor, controller

    def test_verified_presence_starts_once_and_refreshes_without_loading_a_model(self):
        loop, _, controller = self.loop(sample())
        self.assertEqual(loop.step(0, {})["status"], "running")
        self.assertEqual(loop.step(2, {})["status"], "running")
        self.assertEqual(controller.start_count, 1)
        self.assertEqual(controller.refreshes, [({}, 0), ({}, 2)])
        self.assertEqual(controller.stop_count, 0)

    def test_absence_never_starts_and_needs_grace_plus_fresh_confirmation_to_stop(self):
        loop, monitor, controller = self.loop(sample("absent"), controller=FakeController(running=True))
        self.assertEqual(loop.step(0, {})["status"], "exit_grace")
        self.assertEqual(loop.step(2, {})["status"], "exit_grace")
        self.assertEqual(controller.stop_count, 0)
        self.assertEqual(loop.step(3, {})["status"], "waiting")
        self.assertEqual(monitor.calls, 4, "a separate reconciliation sample is required before stop")
        self.assertEqual(controller.stop_count, 1)
        self.assertEqual(controller.start_count, 0)

    def test_unknown_preserves_running_server_and_restarts_absence_grace(self):
        loop, _, controller = self.loop(sample("absent"), sample("unknown"), sample("absent"),
            controller=FakeController(running=True))
        loop.step(0, {})
        self.assertEqual(loop.step(2, {})["status"], "detection_unknown")
        self.assertEqual(loop.step(3, {})["status"], "exit_grace")
        self.assertTrue(controller.is_running)
        self.assertEqual(controller.start_count + controller.stop_count, 0)

    def test_relaunch_during_grace_does_not_stop_a_running_companion(self):
        loop, _, controller = self.loop(sample(), sample("absent"), sample(created=133000000000000008),
            controller=FakeController(running=True))
        loop.step(0, {}); loop.step(2, {})
        self.assertEqual(loop.step(4, {})["status"], "running")
        self.assertEqual(controller.stop_count, 0)

    def test_present_or_unknown_confirmation_prevents_stop(self):
        for confirmation in (sample(pid=8), sample("unknown")):
            with self.subTest(confirmation=confirmation):
                loop, _, controller = self.loop(sample("absent"), sample("absent"), confirmation,
                    controller=FakeController(running=True))
                loop.step(0, {})
                self.assertEqual(loop.step(3, {})["status"], "rechecking")
                self.assertEqual(controller.stop_count, 0)

    def test_failed_start_has_bounded_backoff_and_retries(self):
        controller = FakeController(starts=(False, True))
        loop, _, _ = self.loop(sample(), controller=controller)
        self.assertEqual(loop.step(0, {})["last_error"], "server_start_failed_or_not_owned")
        self.assertEqual(loop.step(14, {})["status"], "retry_wait")
        self.assertEqual(controller.start_count, 1)
        self.assertEqual(loop.step(15, {})["status"], "running")
        self.assertEqual(controller.start_count, 2)
        self.assertIsNone(loop.last_error)

    def test_reopen_during_exit_grace_resets_previous_lifetime_backoff(self):
        controller = FakeController(starts=(False, True))
        loop, _, _ = self.loop(sample(), sample("absent"), sample(created=133000000000000099), controller=controller)
        loop.step(0, {}); loop.step(1, {})
        self.assertEqual(loop.step(2, {})["status"], "running")
        self.assertEqual(controller.start_count, 2)

    def test_pid_reuse_without_an_observed_absent_sweep_starts_new_lifetime(self):
        controller = FakeController(starts=(False, True))
        loop, _, _ = self.loop(sample(), sample(created=133000000000000099), controller=controller)
        loop.step(0, {})
        self.assertEqual(loop.step(2, {})["status"], "running")
        self.assertEqual(controller.start_count, 2)

    def test_unknown_then_same_app_does_not_bypass_retry_backoff(self):
        controller = FakeController(starts=(False, True))
        loop, _, _ = self.loop(sample(), sample("unknown"), sample(), controller=controller)
        loop.step(0, {}); loop.step(1, {})
        self.assertEqual(loop.step(2, {})["status"], "retry_wait")
        self.assertEqual(controller.start_count, 1)

    def test_pending_stop_is_reconciled_again_before_retry(self):
        controller = FakeController(running=True, stops=(False, True))
        loop, monitor, _ = self.loop(sample("absent"), controller=controller)
        loop.step(0, {})
        self.assertEqual(loop.step(3, {})["status"], "stopping")
        self.assertEqual(loop.step(5, {})["status"], "waiting")
        self.assertEqual(controller.stop_count, 2)
        self.assertEqual(monitor.calls, 5)


class ControllerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/"logs").mkdir(); (self.root/"state").mkdir()
        self.controller = supervisor.ServerController(self.root, OWNER)
        # Close the fixture log directly: cleanup must never run a real script.
        self.addCleanup(self.controller.log.close)
        run_patch = mock.patch.object(self.controller, "_run", return_value=True)
        self.run = run_patch.start(); self.addCleanup(run_patch.stop)
        get_patch = mock.patch.object(self.controller, "_get", return_value=self.health())
        self.get = get_patch.start(); self.addCleanup(get_patch.stop)

    def health(self, **changes):
        return {"service": "osaurus-arm64-assist", "instance_id": INSTANCE, "pid": 7, **changes}

    def write_state(self, **changes):
        value = {"automatic_owner_id": OWNER, "root": str(self.root), "port": 8082,
            "instance_id": INSTANCE, "identity": {"pid": 7}, **changes}
        (self.root/"state/server.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_only_matching_owner_uuid_pid_and_health_identify_running_server(self):
        self.write_state()
        self.assertTrue(self.controller.running())
        self.get.assert_called_once_with("http://127.0.0.1:8082", "/health")
        for value in (None, {}, self.health(service="other"), self.health(instance_id=OWNER), self.health(pid=8)):
            with self.subTest(health=value):
                self.get.return_value = value
                self.assertFalse(self.controller.running())

    def test_boolean_health_pid_is_not_an_integer_identity(self):
        self.write_state(identity={"pid": 1}); self.get.return_value = self.health(pid=True)
        self.assertFalse(self.controller.running())

    def test_manual_foreign_and_malformed_state_are_not_adopted_or_stopped(self):
        malformed = [{"automatic_owner_id": None}, {"automatic_owner_id": "foreign-install"},
            {"root": str(self.root/"other-install")}, {"identity": {}}, {"identity": {"pid": True}},
            {"identity": {"pid": 0}}, {"identity": None}, {"instance_id": None}, {"instance_id": ""},
            {"instance_id": "not-a-uuid"}]
        for changes in malformed:
            with self.subTest(changes=changes):
                self.write_state(**changes)
                self.assertFalse(self.controller.running())
                self.assertTrue(self.controller.stop())
        self.get.assert_not_called(); self.run.assert_not_called()

    def test_matching_owned_stop_passes_owner_and_server_only_flags(self):
        self.write_state(); self.controller.refresh_pending = True
        self.assertTrue(self.controller.stop())
        self.run.assert_called_once_with("Stop.ps1", ["-ServerOnly", "-AutomaticOwnerId", OWNER])
        self.assertFalse(self.controller.refresh_pending)

    def test_start_never_adopts_an_existing_manual_server(self):
        self.write_state(automatic_owner_id=None)
        self.assertFalse(self.controller.start())
        self.run.assert_called_once_with("Start.ps1", ["-AutomaticOwnerId", OWNER])
        self.assertFalse(self.controller.refresh_pending)
        self.assertIsNone(supervisor.read_json(self.root/"state/server.json")["automatic_owner_id"])

    def test_start_schedules_discovery_only_after_owned_listener_is_verified(self):
        def start(name, arguments):
            self.write_state(); return True
        self.run.side_effect = start
        self.assertTrue(self.controller.start())
        self.assertTrue(self.controller.refresh_pending)
        self.run.assert_called_once_with("Start.ps1", ["-AutomaticOwnerId", OWNER])

    def test_pending_starter_prevents_stop_script_racing_its_start(self):
        self.write_state(); self.controller.pending = mock.Mock()
        self.controller.pending.poll.return_value = None
        self.assertFalse(self.controller.stop())
        self.run.assert_not_called()

    def test_windows_powershell_child_isolates_module_paths_and_preserves_other_environment(self):
        environment = {"WINDIR": str(self.root/"Windows"),
            "PSModulePath": "synthetic-untrusted-pwsh-modules",
            "pSmOdUlEpAtH": "synthetic-second-module-path",
            "psModuleAnalysisCachePath": "synthetic-untrusted-module-cache",
            "OSAURUS_API_KEY": "synthetic-test-only-environment-key", "OTHER_SETTING": "preserved"}
        before = dict(environment)
        child = mock.Mock(); child.wait.return_value = 0
        with mock.patch.object(supervisor, "os", SimpleNamespace(environ=environment)), \
                mock.patch.object(supervisor.subprocess, "Popen", return_value=child) as launch, \
                mock.patch.object(supervisor.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            # Bypass this fixture's _run mock; Popen itself remains entirely fake.
            self.assertTrue(supervisor.ServerController._run(self.controller, "Start.ps1", []))
        options = launch.call_args.kwargs
        supplied = options["env"]
        shell = Path(environment["WINDIR"])/"System32/WindowsPowerShell/v1.0/powershell.exe"
        self.assertEqual(launch.call_args.args[0][0], str(shell))
        self.assertEqual(supplied["PSModulePath"], str(shell.parent/"Modules"))
        self.assertEqual([key for key in supplied if key.casefold() == "psmodulepath"], ["PSModulePath"])
        self.assertNotIn("psmoduleanalysiscachepath", {key.casefold() for key in supplied})
        self.assertEqual(supplied["OSAURUS_API_KEY"], environment["OSAURUS_API_KEY"])
        self.assertEqual(supplied["OTHER_SETTING"], "preserved")
        self.assertEqual(environment, before, "module-path isolation must affect only the child")
        self.assertEqual(options["creationflags"], 0x08000000)
        child.wait.assert_called_once_with(timeout=45)

    def test_discovery_retries_on_its_own_interval_and_never_calls_inference(self):
        self.controller.refresh_pending = True; self.get.side_effect = [None, True]
        with mock.patch.dict(os.environ, {"OSAURUS_API_KEY": "synthetic-osaurus-key"}):
            self.controller.refresh_discovery({"osaurus_url": "http://127.0.0.1:1444"}, 0)
            self.controller.refresh_discovery({"osaurus_url": "http://127.0.0.1:1444"}, 4.9)
            self.assertEqual(self.get.call_count, 1)
            self.controller.refresh_discovery({"osaurus_url": "http://127.0.0.1:1444"}, 5)
            self.controller.refresh_discovery({"osaurus_url": "http://127.0.0.1:1444"}, 10)
        self.assertEqual(self.get.call_args_list, [mock.call("http://127.0.0.1:1444", "/v1/models?refresh=1",
            "synthetic-osaurus-key", read_body=False)] * 2)
        self.assertFalse(self.controller.refresh_pending)
        self.run.assert_not_called()


class StateAndWatchTests(unittest.TestCase):
    def test_read_json_accepts_bounded_objects_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"state.json"
            self.assertIsNone(supervisor.read_json(path))
            for data in (b'[]', b'null', b'{invalid', b'\xff', b'['*1200+b']'*1200):
                path.write_bytes(data); self.assertIsNone(supervisor.read_json(path))
            value = b'{"safe":true}'
            path.write_bytes(value+b' '*(1024*1024-len(value)))
            self.assertEqual(supervisor.read_json(path), {"safe": True})
            path.write_bytes(value+b' '*(1024*1024+1-len(value)))
            self.assertIsNone(supervisor.read_json(path))

    def test_disabled_configuration_closes_watcher_resources_without_sampling_or_starting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config_path = root/"config.json"
            (root/".osaurus-arm64-assist-install.json").write_text(json.dumps({
                "product": supervisor.PRODUCT, "root": str(root), "installation_id": OWNER}), encoding="utf-8")
            monitor, controller, mutex = FakeMonitor(sample()), FakeController(), mock.Mock()
            mutex.acquire.return_value = True
            fake_os = SimpleNamespace(name="nt", path=os.path, devnull=os.devnull)
            with mock.patch.object(supervisor, "os", fake_os), \
                    mock.patch.object(supervisor, "own_identity", return_value={"pid": 123, "creation_filetime": 456, "session_id": 1}), \
                    mock.patch.object(supervisor, "SingleInstance", return_value=mutex), \
                    mock.patch.object(supervisor, "AppMonitor", return_value=monitor), \
                    mock.patch.object(supervisor, "ServerController", return_value=controller), \
                    mock.patch.object(supervisor.configuration, "load", return_value={"auto_start": False}), \
                    mock.patch.object(supervisor.time, "sleep") as sleep:
                supervisor.watch(config_path)
            self.assertEqual(monitor.calls, 0)
            self.assertEqual(controller.start_count, 0)
            self.assertEqual((monitor.closed, controller.closed), (1, 1))
            mutex.close.assert_called_once(); sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
