"""Lifecycle and native-integrity contracts without loading a model or DLL."""
import hashlib
from pathlib import Path
import tempfile
import threading
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
