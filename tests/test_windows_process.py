"""App identity/lifetime decisions with fake handles; no app or DLL touched."""
import copy
import unittest
from unittest import mock

from osaurus_arm64_assist import windows_process as process


def identity(pid=7, created=133000000000000007, **changes):
    return {"pid": pid, "creation_filetime": created, "basename": process.APP_BASENAME,
            "package_family": process.PACKAGE_FAMILY, "same_user": True, "session_id": 1, **changes}


class FakeBackend:
    session_id = 1

    def __init__(self):
        self.rows = [(7, process.APP_BASENAME)]
        self.snapshot_error = None
        self.infos = {"handle-7": identity()}
        self.opened = []
        self.inspected = []
        self.closed_handles = []
        self.close_count = 0
        self.handles = {7: "handle-7"}
        self.open_errors = {}
        self.inspect_errors = {}
        self.liveness = {}

    def snapshot(self):
        if self.snapshot_error:
            raise self.snapshot_error
        return list(self.rows)

    def open(self, pid):
        self.opened.append(pid)
        if pid in self.open_errors:
            raise self.open_errors[pid]
        return self.handles[pid]

    def inspect(self, handle):
        self.inspected.append(handle)
        if handle in self.inspect_errors:
            raise self.inspect_errors[handle]
        return dict(self.infos[handle])

    def alive(self, handle):
        result = self.liveness.get(handle, True)
        if isinstance(result, Exception):
            raise result
        return result

    def close_process(self, handle):
        self.closed_handles.append(handle)

    def close(self):
        self.close_count += 1

    def own_identity(self):
        return {"pid": 123, "creation_filetime": 133123456789012345, "session_id": 1}


class AppMonitorTests(unittest.TestCase):
    def monitor(self, backend=None):
        backend = backend or FakeBackend()
        monitor = process.AppMonitor(backend=backend)
        self.addCleanup(monitor.close)
        return monitor, backend

    def test_verified_handle_is_cached_and_only_safe_identity_is_returned(self):
        monitor, backend = self.monitor()
        backend.infos["handle-7"].update(private_sid="must-not-publish", image_path="must-not-publish")
        expected = {"state": "present", "processes": [{"pid": 7, "creation_filetime": 133000000000000007}]}
        self.assertEqual(monitor.sample(), expected)
        self.assertEqual(monitor.sample(), expected)
        self.assertEqual(backend.opened, [7])
        self.assertEqual(backend.inspected, ["handle-7"])
        self.assertEqual(backend.closed_handles, [])
        # Caller mutation cannot alter the retained identity.
        value = monitor.sample(); value["processes"][0]["pid"] = 99
        self.assertEqual(monitor.sample(), expected)

    def test_retained_live_handle_proves_presence_despite_snapshot_failure(self):
        monitor, backend = self.monitor()
        monitor.sample()
        backend.snapshot_error = OSError("snapshot unavailable")
        self.assertEqual(monitor.sample()["state"], "present")
        self.assertEqual(backend.closed_handles, [])

    def test_snapshot_failure_without_live_proof_is_unknown_not_absent(self):
        monitor, backend = self.monitor()
        backend.snapshot_error = OSError("snapshot unavailable")
        self.assertEqual(monitor.sample(), {"state": "unknown", "processes": []})
        self.assertEqual(backend.opened, [])

    def test_cli_and_helper_names_are_ignored_before_opening(self):
        monitor, backend = self.monitor()
        backend.rows = [(7, "osaurus.exe"), (8, "Osaurus.Helper.exe"), (9, "Osaurus.App.exe.fake")]
        self.assertEqual(monitor.sample(), {"state": "absent", "processes": []})
        self.assertEqual(backend.opened, [])

    def test_known_foreign_or_unpackaged_candidates_are_ignored_and_closed(self):
        variants = [identity(package_family="Other.Publisher_1234567890123"), identity(package_family=None),
                    identity(same_user=False), identity(session_id=2), identity(basename="osaurus.exe")]
        for info in variants:
            with self.subTest(info=info):
                backend = FakeBackend(); backend.infos["handle-7"] = info
                monitor, _ = self.monitor(backend)
                self.assertEqual(monitor.sample(), {"state": "absent", "processes": []})
                self.assertEqual(backend.closed_handles, ["handle-7"])

    def test_image_and_family_names_compare_case_insensitively(self):
        monitor, backend = self.monitor()
        backend.rows = [(7, process.APP_BASENAME.upper())]
        backend.infos["handle-7"].update(basename=process.APP_BASENAME.lower(), package_family=process.PACKAGE_FAMILY.upper())
        self.assertEqual(monitor.sample()["state"], "present")

    def test_inaccessible_candidate_is_unknown_and_any_open_handle_is_closed(self):
        for phase in ("open", "inspect"):
            with self.subTest(phase=phase):
                backend = FakeBackend()
                if phase == "open": backend.open_errors[7] = PermissionError("access denied")
                else: backend.inspect_errors["handle-7"] = PermissionError("token query denied")
                monitor, _ = self.monitor(backend)
                self.assertEqual(monitor.sample(), {"state": "unknown", "processes": []})
                self.assertEqual(backend.closed_handles, [] if phase == "open" else ["handle-7"])

    def test_disappearing_candidate_is_absent_and_does_not_leak_handle(self):
        monitor, backend = self.monitor()
        backend.inspect_errors["handle-7"] = OSError("process exited during query")
        backend.liveness["handle-7"] = False
        self.assertEqual(monitor.sample(), {"state": "absent", "processes": []})
        self.assertEqual(backend.closed_handles, ["handle-7"])
        backend.open_errors[7] = process.ProcessGone("already gone")
        self.assertEqual(monitor.sample()["state"], "absent")

    def test_exit_after_inspection_is_not_cached_as_a_live_app(self):
        monitor, backend = self.monitor()
        backend.liveness["handle-7"] = False
        self.assertEqual(monitor.sample()["state"], "absent")
        self.assertEqual(backend.closed_handles, ["handle-7"])

    def test_pid_reuse_closes_old_object_and_records_new_creation_time(self):
        monitor, backend = self.monitor()
        first = monitor.sample()
        backend.liveness["handle-7"] = False
        backend.handles[7] = "replacement-7"
        backend.infos["replacement-7"] = identity(created=133000000000000099)
        second = monitor.sample()
        self.assertEqual(first["processes"][0]["creation_filetime"], 133000000000000007)
        self.assertEqual(second["processes"], [{"pid": 7, "creation_filetime": 133000000000000099}])
        self.assertEqual(backend.closed_handles, ["handle-7"])
        monitor.close()
        self.assertEqual(backend.closed_handles, ["handle-7", "replacement-7"])

    def test_failed_wait_never_becomes_absence_or_retains_a_broken_handle(self):
        monitor, backend = self.monitor()
        monitor.sample()
        backend.liveness["handle-7"] = OSError("wait failed")
        backend.snapshot_error = OSError("snapshot failed")
        self.assertEqual(monitor.sample(), {"state": "unknown", "processes": []})
        self.assertEqual(backend.closed_handles, ["handle-7"])

    def test_multiple_apps_keep_presence_and_every_handle_is_closed_once(self):
        monitor, backend = self.monitor()
        backend.rows = [(8, process.APP_BASENAME), (7, process.APP_BASENAME)]
        backend.handles[8] = "handle-8"; backend.infos["handle-8"] = identity(pid=8)
        self.assertEqual([r["pid"] for r in monitor.sample()["processes"]], [7, 8])
        backend.liveness["handle-7"] = False; backend.rows = [(8, process.APP_BASENAME)]
        self.assertEqual(monitor.sample()["state"], "present")
        monitor.close(); monitor.close()
        self.assertCountEqual(backend.closed_handles, ["handle-7", "handle-8"])
        self.assertEqual(backend.close_count, 1)
        with self.assertRaises(RuntimeError): monitor.sample()

    def test_incomplete_creation_identity_is_unknown(self):
        monitor, backend = self.monitor()
        backend.infos["handle-7"]["creation_filetime"] = 0
        self.assertEqual(monitor.sample()["state"], "unknown")
        self.assertEqual(backend.closed_handles, ["handle-7"])

    def test_own_identity_keeps_exact_integer_timestamp_and_closes_backend(self):
        backend = FakeBackend()
        result = process.own_identity(backend=backend)
        self.assertEqual(result, {"pid": 123, "creation_filetime": 133123456789012345, "session_id": 1})
        self.assertIs(type(result["creation_filetime"]), int)
        self.assertEqual(backend.close_count, 1)


class MutexBackend:
    def __init__(self, wait=process.WAIT_OBJECT_0, handle=42):
        self.wait, self.handle = wait, handle
        self.created, self.closed, self.released, self.waits = [], [], [], []

    def CreateMutexW(self, security, initial_owner, name):
        self.created.append((security, initial_owner, name)); return self.handle

    def WaitForSingleObject(self, handle, milliseconds):
        self.waits.append((handle, milliseconds)); return self.wait

    def ReleaseMutex(self, handle):
        self.released.append(handle); return True

    def CloseHandle(self, handle):
        self.closed.append(handle); return True

    def error(self, operation):
        return OSError(operation)


class SingleInstanceTests(unittest.TestCase):
    def test_normal_and_abandoned_mutex_ownership_is_released_once(self):
        for wait in (process.WAIT_OBJECT_0, process.WAIT_ABANDONED):
            with self.subTest(wait=wait):
                backend = MutexBackend(wait)
                mutex = process.SingleInstance("Local\\synthetic-install-session", backend=backend)
                self.assertTrue(mutex.acquire()); self.assertTrue(mutex.acquire())
                self.assertEqual(backend.created, [(None, False, "Local\\synthetic-install-session")])
                self.assertEqual(backend.waits, [(42, 0)])
                mutex.close(); mutex.close()
                self.assertEqual(backend.released, [42]); self.assertEqual(backend.closed, [42])

    def test_competing_owner_closes_only_its_unacquired_handle(self):
        backend = MutexBackend(process.WAIT_TIMEOUT)
        mutex = process.SingleInstance("Local\\test", backend=backend)
        self.assertFalse(mutex.acquire()); mutex.close()
        self.assertEqual(backend.released, []); self.assertEqual(backend.closed, [42])

    def test_wait_failure_closes_handle_and_create_failure_does_not_release(self):
        backend = MutexBackend(process.WAIT_FAILED)
        with self.assertRaises(OSError): process.SingleInstance("Local\\test", backend=backend).acquire()
        self.assertEqual(backend.closed, [42]); self.assertEqual(backend.released, [])
        backend = MutexBackend(handle=None)
        with self.assertRaises(OSError): process.SingleInstance("Local\\test", backend=backend).acquire()
        self.assertEqual(backend.closed, []); self.assertEqual(backend.waits, [])

    def test_mutex_cannot_be_released_from_a_different_thread(self):
        backend = MutexBackend(); mutex = process.SingleInstance("Local\\test", backend=backend)
        self.assertTrue(mutex.acquire())
        with mock.patch.object(process.threading, "get_ident", return_value=-1):
            with self.assertRaises(RuntimeError): mutex.close()
        self.assertEqual(backend.released, [])
        mutex.close(); self.assertEqual(backend.released, [42])


if __name__ == "__main__":
    unittest.main()
