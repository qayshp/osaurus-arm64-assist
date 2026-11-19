"""Owned worker processes, bounded memory guard and idle unloading."""
import ctypes as C
import hashlib
import json
import multiprocessing as MP
import os
from pathlib import Path
import threading
import time
import uuid

from . import config as configuration
from .idle_policy import IdlePolicy
from .osaurus_sync import SharedModels


class WorkerFailure(RuntimeError):
    pass


class MemoryPressure(WorkerFailure):
    """A retryable refusal to allocate, distinct from a failed native backend."""
    code = "memory_pressure"

    def __init__(self):
        super().__init__("Windows is low on memory for new allocations. Close an unused app or wait for other work to finish, then retry loading the model.")


class MemoryStatus(C.Structure):
    _fields_ = [("length", C.c_ulong), ("load", C.c_ulong)] + [(name, C.c_ulonglong) for name in
        ["total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended"]]


def memory_status():
    if os.name != "nt":
        return None
    value = MemoryStatus(); value.length = C.sizeof(value)
    if not C.windll.kernel32.GlobalMemoryStatusEx(C.byref(value)):
        return None
    return {"available_physical_bytes": value.avail_phys, "available_commit_bytes": value.avail_page}


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong), ("flags", C.c_ulong),
        ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("active", C.c_ulong), ("affinity", C.c_size_t),
        ("priority", C.c_ulong), ("scheduling", C.c_ulong)]


class IoCounters(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ["read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"]]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", C.c_size_t),
               ("job_memory", C.c_size_t), ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class WorkerJob:
    def __init__(self, pid):
        self.handle = None
        if os.name != "nt":
            return
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [C.c_void_p, C.c_wchar_p]
        self.kernel.CreateJobObjectW.restype = C.c_void_p
        self.kernel.SetInformationJobObject.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_ulong]
        self.kernel.AssignProcessToJobObject.argtypes = [C.c_void_p, C.c_void_p]
        self.kernel.OpenProcess.argtypes = [C.c_ulong, C.c_bool, C.c_ulong]
        self.kernel.OpenProcess.restype = C.c_void_p
        self.kernel.CloseHandle.argtypes = [C.c_void_p]
        handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        process = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        assigned = bool(handle and process and self.kernel.SetInformationJobObject(handle, 9, C.byref(limits), C.sizeof(limits))
                        and self.kernel.AssignProcessToJobObject(handle, process))
        if process:
            self.kernel.CloseHandle(process)
        if assigned:
            self.handle = handle
        elif handle:
            self.kernel.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle = None


class Manager:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = configuration.load(self.config_path)
        self.config_stamp = self.config_path.stat().st_mtime_ns
        self.lock = threading.Lock()
        self.child = self.connection = self.job = None
        self.fingerprint = self.active_profile = self.ready = None
        self.failure = None
        self.last_used = time.monotonic()
        self.last_metrics = None
        self.closed = threading.Event()
        self.context = MP.get_context("spawn")
        self.shared_models = SharedModels(self.config_path, self.refresh_raw)
        self.idle_policy = IdlePolicy(self.config_path, self.refresh_raw)
        self.guard = threading.Thread(target=self._guard, daemon=True)
        self.guard.start()

    def refresh_raw(self):
        stamp = self.config_path.stat().st_mtime_ns
        if stamp != self.config_stamp:
            updated = configuration.load(self.config_path)
            for key in ("api_key", "runtime_dir", "host", "port"):
                if updated.get(key) != self.config.get(key):
                    raise ValueError(f"Restart the companion after changing {key}")
            self.config = updated; self.config_stamp = stamp
        return self.config

    def refresh(self):
        config = self.refresh_raw()
        shared = getattr(self, "shared_models", None)
        if shared:
            return {**config, "models": shared.profiles(config.get("models", []))}
        return config

    def _guard(self):
        while not self.closed.wait(.5):
            child = self.child
            memory = memory_status()
            try:
                if child and child.is_alive() and memory and memory["available_commit_bytes"] < self.config.get("memory_floor_mib", 512)*1024**2:
                    self.failure = "Worker stopped because system commit fell below the configured safety floor"
                    child.terminate()
            except (ValueError, OSError):
                # Another thread may have closed the old process after this snapshot.
                pass
            policy = getattr(self, "idle_policy", None)
            idle = policy.seconds if policy else self.config.get("idle_seconds", 300)
            if idle and child and time.monotonic()-self.last_used > idle and self.lock.acquire(blocking=False):
                try:
                    # A response or a preference change can finish between the
                    # initial check and admission. Recheck under the lock.
                    idle = policy.seconds if policy else self.config.get("idle_seconds", 300)
                    if idle and self.child and time.monotonic()-self.last_used > idle:
                        self._stop()
                finally:
                    self.lock.release()

    def _stop(self):
        child, connection, job = self.child, self.connection, self.job
        self.child = self.connection = self.job = None
        self.ready = self.active_profile = self.fingerprint = None
        if child:
            def alive():
                try:
                    return child.is_alive()
                except (OSError, ValueError):
                    return False

            def attempt(action, *args, **kwargs):
                try:
                    action(*args, **kwargs)
                except (EOFError, OSError, ValueError):
                    # The worker can exit or lose its IPC endpoint between
                    # observations. Cleanup must not mask the original failure.
                    pass
            try:
                if alive() and connection:
                    attempt(connection.send, {"type": "unload"}); attempt(child.join, timeout=5)
                if alive():
                    attempt(child.terminate); attempt(child.join, timeout=3)
                if alive():
                    attempt(child.kill); attempt(child.join, timeout=2)
            finally:
                if connection:
                    attempt(connection.close)
                if job:
                    attempt(job.close)
                if not alive():
                    attempt(child.close)

    def _receive(self, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                if self.connection and self.connection.poll(.2):
                    return self.connection.recv()
                if not self.child or not self.child.is_alive():
                    raise WorkerFailure(self.failure or "Native worker exited; inspect its local log")
            except (EOFError, OSError, ValueError) as error:
                raise WorkerFailure(self.failure or "Native worker IPC failed; inspect its local log") from error
        raise WorkerFailure(self.failure or "Native worker exceeded the configured response deadline")

    def _load_fingerprint(self, profile):
        """Use the same allocation identity for admission and actual loading."""
        if profile.get("mode") not in configuration.MODES:
            raise ValueError("Only native companion modes may load a native worker")
        path = Path(profile["path"])
        if not path.is_file():
            raise ValueError("The configured GGUF is missing; register its current Osaurus path")
        stat = path.stat()
        load_keys = ("mode", "path", "context", "threads", "batch_threads", "batch", "microbatch", "power_mode",
                     "repack", "gpu_layers", "flash_attention", "swa_full", "kv_unified", "kv_offload", "op_offload",
                     "allow_experimental_architecture", "chat_template", "template_kwargs")
        load_settings = {name: profile[name] for name in load_keys if name in profile}
        # Runtime paths are restart-only configuration. Normalize aliases/case
        # without rehashing native libraries on every resident request.
        runtime = os.path.normcase(str(Path(self.config["runtime_dir"]).resolve()))
        return hashlib.sha256(json.dumps([load_settings, stat.st_size, stat.st_mtime_ns, runtime], sort_keys=True).encode()).hexdigest()

    def _worker_reusable(self, fingerprint):
        try:
            return bool(self.ready and self.child and self.child.is_alive() and fingerprint == self.fingerprint)
        except (OSError, ValueError):
            return False

    def _load(self, profile):
        fingerprint = self._load_fingerprint(profile)
        if self._worker_reusable(fingerprint):
            self.active_profile = profile["id"]
            return False
        self._stop(); self.failure = None
        # Reclaim an incompatible worker before sampling. This is only the
        # existing safety floor, not an estimate that a model will fit. The
        # background guard remains necessary if pressure changes after spawn.
        memory = memory_status()
        if memory and memory["available_commit_bytes"] < self.config.get("memory_floor_mib", 512)*1024**2:
            error = MemoryPressure()
            self.failure = str(error)
            raise error
        from .worker import entry
        parent = child = None
        try:
            parent, child = self.context.Pipe()
            log_path = self.config_path.parent/"logs"/("worker-"+uuid.uuid4().hex+".log")
            process = self.context.Process(target=entry, args=(child, self.config["runtime_dir"], profile, str(log_path), os.getpid()), daemon=True)
            self.child, self.connection = process, parent
            process.start(); child.close(); child = None
            self.job = WorkerJob(process.pid)
            ready = self._receive(profile.get("load_timeout_seconds", 300))
            if ready.get("type") != "ready":
                raise WorkerFailure(ready.get("message", "Model failed to initialize"))
            self.ready, self.active_profile, self.fingerprint = ready, profile["id"], fingerprint
            return True
        except WorkerFailure:
            self._stop()
            raise
        except (EOFError, OSError, ValueError, RuntimeError) as error:
            self._stop()
            raise WorkerFailure(self.failure or "Native worker startup failed; inspect its local log") from error
        except BaseException:
            self._stop()
            raise
        finally:
            for endpoint in (child, parent if parent is not self.connection else None):
                if endpoint is not None:
                    try:
                        endpoint.close()
                    except (OSError, ValueError):
                        pass

    def generate(self, profile, request, *, token_ids=None):
        actual = profile
        fallback_reason = None
        try:
            cold = self._load(profile)
        except MemoryPressure:
            # A second allocation is not a safe remedy for low system commit.
            raise
        except WorkerFailure as error:
            if profile.get("fallback_mode") != "cpu" or profile["mode"] == "cpu":
                raise
            fallback_reason = str(error)
            actual = {**profile, "mode": "cpu"}
            cold = self._load(actual)
        self.last_used = time.monotonic()
        command = {"type": "generate", "request": request}
        if token_ids is not None:
            command.update(type="benchmark", token_ids=token_ids)
        deadline = time.monotonic() + actual.get("request_timeout_seconds", 900)
        try:
            try:
                self.connection.send(command)
            except (EOFError, OSError, ValueError) as error:
                raise WorkerFailure(self.failure or "Native worker command IPC failed; inspect its local log") from error
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerFailure("Native worker exceeded the whole-response deadline")
                event = self._receive(remaining)
                self.last_used = time.monotonic()
                if event["type"] == "error":
                    if event.get("kind") == "invalid_request_or_model_output":
                        raise ValueError(event["message"])
                    raise WorkerFailure(event.get("message", "Native inference failed"))
                if event["type"] == "done":
                    event["assist"] = {"requested_mode": profile["mode"], "actual_mode": actual["mode"],
                        "fallback_reason": fallback_reason, "cold_load": cold,
                        "load_seconds_this_request": self.ready["load_seconds"] if cold else 0,
                        "load_stages": self.ready["load_stages"] if cold else None,
                        "architecture": self.ready["architecture"], "context_length": self.ready["context_length"],
                        "metrics": event["metrics"]}
                    if profile.get("synced"):
                        event["assist"]["settings_source"] = "osaurus"
                        event["assist"]["source_model"] = profile.get("_osaurus", {}).get("model_id")
                        event["assist"]["settings"] = profile.get("sync", {})
                    self.last_metrics = {"model": profile["id"], **event["assist"]}
                    yield event
                    break
                yield event
        except BaseException:
            self._stop()
            raise

    def native_admission(self, profile, config):
        """Avoid adding a new worker while Osaurus already retains a model."""
        try:
            if self._worker_reusable(self._load_fingerprint(profile)):
                return None
        except (OSError, ValueError, KeyError, TypeError):
            pass
        from .residency import native_admission
        return native_admission(profile, config)

    def benchmark(self, profile, body):
        """Run under the caller's inference lock; warm-ups are kept separately."""
        from .benchmark import input_ids, normalize_benchmark, summary
        settings = normalize_benchmark(body, profile.get("context", 2048))
        ids = input_ids(settings)
        request = {"max_tokens": settings["generated_tokens"], "temperature": 0,
                   "top_p": 1, "stop": [], "tools": [], "tool_choice": "none",
                   "response_format": {"type": "text"}}
        runs, warmups = [], []
        for index in range(settings["warmup"] + settings["repetitions"]):
            before = time.perf_counter()
            events = self.generate(profile, request, token_ids=ids)
            finished = None
            try:
                for event in events:
                    if event["type"] == "done":
                        finished = event
            finally:
                events.close()
            if not finished or len(finished.get("generated_ids", [])) != settings["generated_tokens"]:
                raise WorkerFailure("Fixed-token benchmark did not produce the requested number of tokens")
            if finished["usage"]["prompt_tokens"] != settings["prompt_tokens"] or finished["usage"]["completion_tokens"] != settings["generated_tokens"]:
                raise WorkerFailure("Fixed-token benchmark usage did not match the requested workload")
            assist = dict(finished["assist"])
            if assist.get("fallback_reason"):
                assist["fallback_reason"] = "Explicit CPU fallback used; inspect the local worker log for details"
            warmup = index < settings["warmup"]
            run = {"repetition": index+1 if warmup else index-settings["warmup"]+1,
                   "warmup": warmup, "manager_seconds": time.perf_counter()-before,
                   "generated_ids": finished["generated_ids"], "usage": finished["usage"],
                   "metrics": finished["metrics"], "assist": assist,
                   "finish_reason": finished["finish_reason"]}
            (warmups if warmup else runs).append(run)
        allowed = ("id", "mode", "context", "threads", "batch_threads", "batch", "microbatch",
                   "power_mode", "repack", "gpu_layers", "fallback_mode", "allow_experimental_architecture",
                   "flash_attention", "swa_full", "kv_unified", "kv_offload", "op_offload", "settings_source")
        return {"schema_version": 1, "kind": "fixed_tokens", "model": profile["id"],
                "settings": {**settings, "input_rng": "random.Random(seed).randrange(32000)",
                             "input_id_range": [0, 31999], "sampling": "greedy", "ignore_eos": True},
                "configuration": {key: profile[key] for key in allowed if key in profile},
                "input_ids": ids, "warmup_runs": warmups, "runs": runs, "summary": summary(runs)}

    def close(self):
        self.closed.set()
        discovery = getattr(self, "discovery", None)
        if discovery:
            discovery.close()
        shared = getattr(self, "shared_models", None)
        if shared:
            shared.close()
        policy = getattr(self, "idle_policy", None)
        if policy:
            policy.close()
        with self.lock:
            self._stop()
