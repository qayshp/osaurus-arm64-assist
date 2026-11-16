"""Lifecycle and native-integrity contracts without loading a model or DLL."""
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from osaurus_arm64_assist import lifecycle, native


class FakeProcess:
    def __init__(self, pid=4242, alive=True, join_stops=False):
        self.pid, self.alive = pid, alive
        self.join_stops = join_stops
        self.terminated = self.killed = self.closed = False
        self.started = False

    def start(self):
        self.started = self.alive = True

    def is_alive(self):
        if self.closed:
            raise ValueError("process object is closed")
        return self.alive

    def join(self, timeout=None):
        if self.join_stops:
            self.alive = False

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, process=None, events=()):
        self.process = process
        self.events = list(events)
        self.sent, self.closed = [], False

    def send(self, message):
        self.sent.append(message)
        if message["type"] == "unload" and self.process:
            self.process.alive = False

    def poll(self, timeout):
        return bool(self.events)

    def recv(self):
        return self.events.pop(0)

    def close(self):
        self.closed = True


class FakeJob:
    def __init__(self, pid=None):
        self.pid, self.closed = pid, False

    def close(self):
        self.closed = True


def manager_without_thread():
    """Build test state without starting a real background thread/process."""
    value = lifecycle.Manager.__new__(lifecycle.Manager)
    value.config = {"runtime_dir": "unused", "idle_seconds": 300, "memory_floor_mib": 512}
    value.lock = threading.Lock()
    value.child = value.connection = value.job = None
    value.ready = value.active_profile = value.fingerprint = None
    value.failure = value.last_metrics = None
    value.last_used = 0
    return value


class GuardTests(unittest.TestCase):
    def test_shared_never_policy_overrides_an_expired_fixed_fallback(self):
        manager = manager_without_thread()
        manager.config["idle_seconds"] = 1
        manager.idle_policy = SimpleNamespace(seconds=0)
        process = manager.child = FakeProcess()
        manager.connection = FakeConnection(process)
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                mock.patch.object(lifecycle.time, "monotonic", return_value=1000):
            manager._guard()
        self.assertTrue(process.alive)
        self.assertFalse(process.terminated)
        self.assertEqual(manager.connection.sent, [])

    def test_shorter_shared_policy_uses_existing_idle_elapsed_time_next_sweep(self):
        manager = manager_without_thread()
        manager.idle_policy = SimpleNamespace(seconds=300)
        process = manager.child = FakeProcess()
        connection = manager.connection = FakeConnection(process)
        manager.closed = mock.Mock()
        def sweep(timeout):
            count = manager.closed.wait.call_count
            if count == 2:
                self.assertTrue(process.alive, "the first sweep must respect the longer timer")
                manager.idle_policy.seconds = 60
            return count >= 3
        manager.closed.wait.side_effect = sweep
        with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                mock.patch.object(lifecycle.time, "monotonic", return_value=100):
            manager._guard()
        self.assertEqual(manager.last_used, 0, "a preference change must not restart elapsed idle time")
        self.assertEqual(connection.sent, [{"type": "unload"}])
        self.assertTrue(process.closed)

    def test_shared_policy_cannot_unload_an_active_inference_request(self):
        manager = manager_without_thread()
        manager.idle_policy = SimpleNamespace(seconds=1)
        manager.child = FakeProcess()
        manager._stop = mock.Mock()
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        with manager.lock:
            with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                    mock.patch.object(lifecycle.time, "monotonic", return_value=1000):
                manager._guard()
        manager._stop.assert_not_called()
        self.assertTrue(manager.child.alive)

    def test_response_finishing_before_guard_admission_prevents_stale_unload(self):
        manager = manager_without_thread()
        manager.idle_policy = SimpleNamespace(seconds=60)
        manager.child = FakeProcess()
        manager._stop = mock.Mock()
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        def admit(*args, **kwargs):
            # A response updates last_used after the initial elapsed-time test.
            manager.last_used = 1000
            return True
        manager.lock = mock.Mock()
        manager.lock.acquire.side_effect = admit
        with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                mock.patch.object(lifecycle.time, "monotonic", return_value=1000):
            manager._guard()
        manager._stop.assert_not_called()
        manager.lock.release.assert_called_once()

    def test_never_or_longer_policy_chosen_during_admission_prevents_unload(self):
        for seconds in (0, 2000):
            with self.subTest(seconds=seconds):
                manager = manager_without_thread()
                manager.idle_policy = SimpleNamespace(seconds=60)
                manager.child = FakeProcess()
                manager._stop = mock.Mock()
                manager.closed = mock.Mock()
                manager.closed.wait.side_effect = [False, True]
                def admit(*args, **kwargs):
                    manager.idle_policy.seconds = seconds
                    return True
                manager.lock = mock.Mock()
                manager.lock.acquire.side_effect = admit
                with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                        mock.patch.object(lifecycle.time, "monotonic", return_value=1000):
                    manager._guard()
                manager._stop.assert_not_called()
                manager.lock.release.assert_called_once()

    def test_close_stops_shared_policy_watcher_before_owned_worker_cleanup(self):
        manager = manager_without_thread()
        manager.closed = threading.Event()
        manager.idle_policy = mock.Mock()
        order = []
        manager.idle_policy.close.side_effect = lambda: order.append("policy")
        manager._stop = mock.Mock(side_effect=lambda: order.append("worker"))
        manager.close()
        self.assertTrue(manager.closed.is_set())
        self.assertEqual(order, ["policy", "worker"])
        manager.idle_policy.close.assert_called_once_with()

    def test_closed_snapshot_does_not_disable_later_memory_protection(self):
        for race_error in (ValueError("closed process"), OSError("handle closed")):
            with self.subTest(error=type(race_error).__name__):
                manager = manager_without_thread()
                manager.config["idle_seconds"] = 0
                stale = mock.Mock()
                stale.is_alive.side_effect = race_error
                current = FakeProcess()
                manager.child = stale
                manager.closed = mock.Mock()

                def next_iteration(timeout):
                    count = manager.closed.wait.call_count
                    if count == 2:
                        manager.child = current
                    return count >= 3

                manager.closed.wait.side_effect = next_iteration
                with mock.patch.object(lifecycle, "memory_status", return_value={"available_commit_bytes": 1}):
                    manager._guard()
                self.assertTrue(current.terminated, "a stale process race must not end the memory guard")
                self.assertIn("commit", manager.failure)

    def test_idle_unload_releases_worker_channel_job_and_context_metadata(self):
        manager = manager_without_thread()
        process = manager.child = FakeProcess()
        connection = manager.connection = FakeConnection(process)
        job = manager.job = FakeJob(process.pid)
        manager.ready, manager.active_profile, manager.fingerprint = {"loaded": True}, "example", "fingerprint"
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                mock.patch.object(lifecycle.time, "monotonic", return_value=301):
            manager._guard()
        self.assertEqual(connection.sent, [{"type": "unload"}])
        self.assertTrue(connection.closed and job.closed and process.closed)
        self.assertIsNone(manager.child)
        self.assertIsNone(manager.ready)
        self.assertIsNone(manager.active_profile)
        self.assertIsNone(manager.fingerprint)
        manager._stop()  # Repeated removal must be harmless.

    def test_idle_guard_preserves_worker_during_an_admitted_request(self):
        manager = manager_without_thread()
        process = manager.child = FakeProcess()
        connection = manager.connection = FakeConnection(process)
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        manager.lock.acquire()
        try:
            with mock.patch.object(lifecycle, "memory_status", return_value=None), \
                    mock.patch.object(lifecycle.time, "monotonic", return_value=1000):
                manager._guard()
        finally:
            manager.lock.release()
        self.assertTrue(process.alive)
        self.assertEqual(connection.sent, [])


class WorkerLifecycleTests(unittest.TestCase):
    def test_reusing_profile_avoids_reload_and_switching_closes_old_worker(self):
        manager = manager_without_thread()
        children, channels, jobs = [], [], []
        ready = {"type": "ready", "load_seconds": .1, "load_stages": {},
                 "architecture": "test", "context_length": 2048}

        def pipe():
            parent, child = FakeConnection(events=[dict(ready)]), FakeConnection()
            channels.append((parent, child))
            return parent, child

        def process_factory(**kwargs):
            process = FakeProcess(pid=4242 + len(children), alive=False)
            channels[-1][0].process = process
            children.append(process)
            return process

        def job_factory(pid):
            job = FakeJob(pid)
            jobs.append(job)
            return job

        manager.context = mock.Mock()
        manager.context.Pipe.side_effect = pipe
        manager.context.Process.side_effect = process_factory
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(lifecycle, "WorkerJob", side_effect=job_factory):
            manager.config_path = Path(folder) / "config.json"
            model = Path(folder) / "synthetic.gguf"
            model.write_bytes(b"synthetic test fixture; never loaded")
            profile = {"id": "sample-cpu", "mode": "cpu", "path": str(model)}
            self.assertTrue(manager._load(profile))
            self.assertFalse(manager._load(profile))
            self.assertFalse(manager._load({**profile, "sampling_defaults": {"temperature": 0.0},
                                           "sync": {"status": "updated"}}))
            self.assertEqual(len(children), 1)
            self.assertTrue(manager._load({**profile, "id": "sample-npu", "mode": "npu"}))
            self.assertTrue(children[0].closed and channels[0][0].closed and jobs[0].closed)
            self.assertEqual(channels[0][0].sent, [{"type": "unload"}])
            self.assertEqual(manager.active_profile, "sample-npu")
            self.assertTrue(children[1].alive)
            self.assertTrue(all(child.closed for _, child in channels), "parent must release child IPC endpoints")
            manager._stop()
            self.assertTrue(children[1].closed and jobs[1].closed)

    def test_tokens_do_not_restart_whole_response_deadline(self):
        manager = manager_without_thread()
        manager._load = mock.Mock(return_value=False)
        manager._stop = mock.Mock()
        manager.connection = FakeConnection()
        clock = [0.0]
        waits = []

        def receive(timeout):
            waits.append(timeout)
            if timeout < .4:
                clock[0] += timeout
                raise lifecycle.WorkerFailure("deadline")
            clock[0] += .4
            return {"type": "token", "text": "x"}

        manager._receive = receive
        profile = {"id": "sample", "mode": "cpu", "request_timeout_seconds": 1}
        with mock.patch.object(lifecycle.time, "monotonic", side_effect=lambda: clock[0]):
            stream = manager.generate(profile, {})
            self.assertEqual(next(stream)["text"], "x")
            self.assertEqual(next(stream)["text"], "x")
            with self.assertRaises(lifecycle.WorkerFailure):
                next(stream)
        self.assertAlmostEqual(waits[0], 1)
        self.assertAlmostEqual(waits[1], .6)
        self.assertAlmostEqual(waits[2], .2)
        manager._stop.assert_called_once()

    def test_abandoned_generator_unloads_owned_worker(self):
        manager = manager_without_thread()
        manager._load = mock.Mock(return_value=False)
        manager._stop = mock.Mock()
        manager.connection = FakeConnection()
        manager._receive = mock.Mock(return_value={"type": "token", "text": "partial"})
        stream = manager.generate({"id": "sample", "mode": "cpu"}, {})
        self.assertEqual(next(stream)["text"], "partial")
        stream.close()
        manager._stop.assert_called_once()


class IPCFaultTests(unittest.TestCase):
    def test_poll_failures_become_safe_worker_failures(self):
        for error in (ConnectionResetError("PRIVATE_POLL_DETAIL"), ConnectionAbortedError("PRIVATE_POLL_DETAIL"),
                      TimeoutError("PRIVATE_POLL_DETAIL"), ValueError("PRIVATE_CLOSED_HANDLE")):
            with self.subTest(error=type(error).__name__):
                manager = manager_without_thread()
                manager.child = FakeProcess()
                manager.connection = mock.Mock()
                manager.connection.poll.side_effect = error
                with self.assertRaises(lifecycle.WorkerFailure) as raised:
                    manager._receive(1)
                self.assertEqual(str(raised.exception), "Native worker IPC failed; inspect its local log")
                self.assertNotIn("PRIVATE", str(raised.exception))

    def test_memory_guard_reason_survives_a_reset_during_loading(self):
        manager = manager_without_thread()
        manager.config["idle_seconds"] = 0
        manager.child = FakeProcess()
        manager.connection = mock.Mock()
        manager.closed = mock.Mock()
        manager.closed.wait.side_effect = [False, True]
        with mock.patch.object(lifecycle, "memory_status", return_value={"available_commit_bytes": 1}):
            manager._guard()
        self.assertTrue(manager.child.terminated)
        manager.connection.poll.side_effect = ConnectionResetError("PRIVATE_OS_DETAIL")
        with self.assertRaises(lifecycle.WorkerFailure) as raised:
            manager._receive(1)
        self.assertEqual(str(raised.exception), "Worker stopped because system commit fell below the configured safety floor")

    def test_recv_error_preserves_known_failure(self):
        manager = manager_without_thread()
        manager.child = FakeProcess()
        manager.connection = mock.Mock()
        manager.connection.poll.return_value = True
        manager.connection.recv.side_effect = ConnectionAbortedError("PRIVATE_RECV_DETAIL")
        manager.failure = "Worker stopped because system commit fell below the configured safety floor"
        with self.assertRaises(lifecycle.WorkerFailure) as raised:
            manager._receive(1)
        self.assertEqual(str(raised.exception), manager.failure)

    def test_process_start_failures_clean_both_pipe_ends_and_state(self):
        for error in (OSError("PRIVATE_SPAWN_PATH"), RuntimeError("PRIVATE_SPAWN_DETAIL")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                manager = manager_without_thread()
                manager.config_path = Path(folder) / "config.json"
                model = Path(folder) / "fixture.gguf"; model.write_bytes(b"never loaded")
                parent, child = FakeConnection(), FakeConnection()
                process = FakeProcess(alive=False)
                process.start = mock.Mock(side_effect=error)
                manager.context = mock.Mock()
                manager.context.Pipe.return_value = (parent, child)
                manager.context.Process.return_value = process
                with self.assertRaises(lifecycle.WorkerFailure) as raised:
                    manager._load({"id": "fixture", "path": str(model), "mode": "cpu"})
                self.assertEqual(str(raised.exception), "Native worker startup failed; inspect its local log")
                self.assertTrue(parent.closed and child.closed and process.closed)
                self.assertIsNone(manager.child)
                self.assertIsNone(manager.connection)
                self.assertIsNone(manager.job)
                self.assertIsNone(manager.ready)

    def test_process_constructor_failure_closes_unassigned_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = manager_without_thread()
            manager.config_path = Path(folder) / "config.json"
            model = Path(folder) / "fixture.gguf"; model.write_bytes(b"never loaded")
            parent, child = FakeConnection(), FakeConnection()
            manager.context = mock.Mock()
            manager.context.Pipe.return_value = (parent, child)
            manager.context.Process.side_effect = OSError("PRIVATE_CONSTRUCTOR_PATH")
            with self.assertRaises(lifecycle.WorkerFailure):
                manager._load({"id": "fixture", "path": str(model), "mode": "cpu"})
            self.assertTrue(parent.closed and child.closed)
            self.assertIsNone(manager.child)
            self.assertIsNone(manager.connection)

    def test_failed_command_and_unload_sends_still_terminate_owned_worker(self):
        manager = manager_without_thread()
        process = manager.child = FakeProcess()
        connection = manager.connection = FakeConnection(process)
        connection.send = mock.Mock(side_effect=ConnectionResetError("PRIVATE_SEND_DETAIL"))
        job = manager.job = FakeJob()
        manager._load = mock.Mock(return_value=False)
        with self.assertRaises(lifecycle.WorkerFailure) as raised:
            next(manager.generate({"id": "fixture", "mode": "cpu"}, {}))
        self.assertEqual(str(raised.exception), "Native worker command IPC failed; inspect its local log")
        self.assertTrue(process.terminated and process.closed and connection.closed and job.closed)
        self.assertIsNone(manager.child)
        self.assertIsNone(manager.connection)

    def test_poll_error_is_not_masked_by_failed_unload_send(self):
        manager = manager_without_thread()
        process = manager.child = FakeProcess()
        connection = manager.connection = FakeConnection(process)
        original_send = connection.send
        def send(message):
            if message["type"] == "unload":
                raise BrokenPipeError("PRIVATE_CLEANUP_DETAIL")
            original_send(message)
        connection.send = send
        connection.poll = mock.Mock(side_effect=ConnectionAbortedError("PRIVATE_POLL_DETAIL"))
        job = manager.job = FakeJob()
        manager._load = mock.Mock(return_value=False)
        with self.assertRaises(lifecycle.WorkerFailure) as raised:
            next(manager.generate({"id": "fixture", "mode": "cpu"}, {}))
        self.assertEqual(str(raised.exception), "Native worker IPC failed; inspect its local log")
        self.assertTrue(process.terminated and process.closed and connection.closed and job.closed)


class RuntimeIntegrityTests(unittest.TestCase):
    def test_hashes_and_native_allowlist_are_both_required(self):
        content = b"fixture bytes, never a real DLL"
        manifest = {"llama_cpp_commit": "fixture", "files": {
            "llama.dll": hashlib.sha256(content).hexdigest()}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(native, "runtime_manifest", return_value=manifest):
            path = Path(folder)
            self.assertFalse(native.verify_runtime(path)["verified"], "missing expected DLL must fail")
            (path / "llama.dll").write_bytes(content)
            (path / "README.txt").write_text("A harmless license/readme is allowed.", encoding="utf-8")
            self.assertTrue(native.verify_runtime(path)["verified"])
            (path / "foreign.DLL").write_bytes(content)
            checked = native.verify_runtime(path)
            self.assertFalse(checked["verified"])
            self.assertEqual(checked["unexpected_native_files"], ["foreign.DLL"])
            with mock.patch.object(native, "native_platform", return_value=True), \
                    mock.patch.object(native.C, "CDLL") as load_library:
                with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                    native.Api(path)
                load_library.assert_not_called()
            (path / "foreign.DLL").unlink()
            (path / "llama.dll").write_bytes(b"changed")
            self.assertFalse(native.verify_runtime(path)["verified"], "same filename with changed bytes must fail")


if __name__ == "__main__":
    unittest.main()
