"""Follow the verified Osaurus app lifetime without loading inference libraries."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from . import config as configuration
from .idle_policy import discover_endpoint
from .windows_process import AppMonitor, SingleInstance, own_identity

PRODUCT = "org.osaurus-community.arm64-assist"


def read_json(path):
    try:
        with Path(path).open("rb") as source:
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return None
        value = json.loads(data.decode("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


class ServerController:
    """Reuse the PowerShell process/installation ownership boundaries."""
    def __init__(self, root, installation_id):
        self.root, self.owner = Path(root), installation_id
        self.config_path = self.root / "config.json"
        self.pending = None
        self.refresh_pending = False
        self.observed_instance = None
        self.last_refresh = float("-inf")
        self.log = (self.root / "logs" / "supervisor-commands.log").open("a", encoding="utf-8")

    def _state(self):
        value = read_json(self.root / "state" / "server.json")
        if not value or value.get("automatic_owner_id") != self.owner:
            return None
        identity = value.get("identity")
        identifier = value.get("instance_id")
        if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or identity["pid"] <= 0
                or not isinstance(identifier, str) or not identifier or not isinstance(value.get("root"), str)):
            return None
        try:
            uuid.UUID(identifier)
        except ValueError:
            return None
        if os.path.normcase(str(Path(value["root"]).resolve())) != os.path.normcase(str(self.root.resolve())):
            return None
        return value

    @staticmethod
    def _get(origin, path, api_key=None, read_body=True):
        parsed = urlsplit(origin)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
        headers = {"Connection": "close"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                return None
            if not read_body:
                return True
            data = response.read(65537)
            if len(data) > 65536:
                return None
            return json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    def running(self):
        value = self._state()
        if not value:
            return False
        port = value.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            return False
        health = self._get(f"http://127.0.0.1:{port}", "/health")
        running = bool(isinstance(health, dict) and health.get("service") == "osaurus-arm64-assist"
                    and health.get("instance_id") == value.get("instance_id")
                    and type(health.get("pid")) is int
                    and health.get("pid") == value.get("identity", {}).get("pid"))
        if running and self.observed_instance != value["instance_id"]:
            self.observed_instance = value["instance_id"]
            self.refresh_pending = True
        return running

    def _run(self, name, arguments):
        if self.pending is not None:
            if self.pending.poll() is None:
                return False
            self.pending = None
        shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                   str(self.root / "app" / "scripts" / name), "-RootPath", str(self.root), *arguments]
        self.pending = subprocess.Popen(command, cwd=self.root, stdout=self.log, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            result = self.pending.wait(timeout=45)
        except subprocess.TimeoutExpired:
            # Keep the starter attached to this process. Stop-AutoStart can
            # verify and remove the whole owned tree during maintenance.
            return False
        self.pending = None
        return result == 0

    def start(self):
        if self.running():
            return True
        result = self._run("Start.ps1", ["-AutomaticOwnerId", self.owner])
        if result and self.running():
            self.refresh_pending = True
            return True
        return False

    def stop(self):
        if self.pending is not None and self.pending.poll() is None:
            return False
        if not self._state():
            return True  # A manual or foreign server is never adopted/stopped.
        self.refresh_pending = False
        return self._run("Stop.ps1", ["-ServerOnly", "-AutomaticOwnerId", self.owner])

    def refresh_discovery(self, config, now):
        if not self.refresh_pending or now - self.last_refresh < 5:
            return
        self.last_refresh = now
        endpoint = discover_endpoint(config)
        # Osaurus can start querying providers before this listener is ready.
        # Refresh its ordinary model discovery once its own API is available.
        if self._get(endpoint, "/v1/models?refresh=1", os.environ.get("OSAURUS_API_KEY"), read_body=False):
            self.refresh_pending = False

    def close(self):
        try:
            if self.pending is not None:
                try:
                    self.pending.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    return
            self.stop()
        finally:
            self.log.close()


class LifecycleSupervisor:
    """Small state machine; unknown detection preserves the existing server."""
    def __init__(self, monitor, controller, *, grace_seconds=3, retry_seconds=15):
        self.monitor, self.controller = monitor, controller
        self.grace_seconds, self.retry_seconds = grace_seconds, retry_seconds
        self.absent_since = None
        self.last_attempt = float("-inf")
        self.last_error = None
        self.present_signature = None

    def step(self, now, config):
        observed = self.monitor.sample()
        state = observed["state"]
        result = {"app_state": state, "app_pids": [item["pid"] for item in observed["processes"]],
                  "status": "waiting", "last_error": None}
        if state == "unknown":
            self.absent_since = None
            return {**result, "status": "detection_unknown", "last_error": "app_detection_unavailable"}
        if state == "present":
            signature = tuple(sorted((item["pid"], item["creation_filetime"]) for item in observed["processes"]))
            if signature != self.present_signature:
                self.last_attempt = float("-inf")
                self.present_signature = signature
            self.absent_since = None
            if self.controller.running():
                self.controller.refresh_discovery(config, now)
                return {**result, "status": "running"}
            if now - self.last_attempt < self.retry_seconds:
                return {**result, "status": "retry_wait", "last_error": self.last_error}
            self.last_attempt = now
            if self.controller.start():
                self.last_error = None
                self.controller.refresh_discovery(config, now)
                return {**result, "status": "running"}
            self.last_error = "server_start_failed_or_not_owned"
            return {**result, "status": "retry_wait", "last_error": self.last_error}
        self.present_signature = None
        if self.absent_since is None:
            self.absent_since = now
        if now - self.absent_since < self.grace_seconds:
            return {**result, "status": "exit_grace"}
        # Reconcile immediately before stopping, so a relaunch during the grace
        # period or a transient snapshot failure cannot cause a false shutdown.
        confirmation = self.monitor.sample()
        if confirmation["state"] != "absent":
            self.absent_since = None
            return {**result, "status": "rechecking", "app_state": confirmation["state"],
                    "app_pids": [item["pid"] for item in confirmation["processes"]]}
        if self.controller.stop():
            self.last_attempt = float("-inf")
            return result
        return {**result, "status": "stopping", "last_error": "server_stop_pending"}


def watch(config_path):
    if os.name != "nt":
        raise ValueError("Automatic app lifecycle requires Windows")
    # pythonw can have no standard streams, including for an early error.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    config_path = Path(config_path).resolve()
    root = config_path.parent
    marker = read_json(root / ".osaurus-arm64-assist-install.json")
    if (not marker or marker.get("product") != PRODUCT
            or os.path.normcase(str(Path(marker.get("root", "")).resolve())) != os.path.normcase(str(root))):
        raise ValueError("Automatic lifecycle requires a matching owned installation")
    installation_id = str(uuid.UUID(marker["installation_id"]))
    identity = own_identity()
    instance = str(uuid.uuid4())
    mutex = SingleInstance(f"Local\\OsaurusArm64Assist.Supervisor.{installation_id}.{identity['session_id']}")
    if not mutex.acquire():
        return
    (root / "logs").mkdir(exist_ok=True)
    log = (root / "logs" / "supervisor.log").open("a", encoding="utf-8", buffering=1)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = log
    monitor = controller = None
    state_path = root / "state" / "supervisor.json"
    stop_path = root / "state" / "supervisor-stop.json"
    record = {"product": PRODUCT, "installation_id": installation_id, "instance_id": instance,
              "config_path": str(config_path), **identity}
    previous = None
    try:
        configuration.atomic_json(state_path, {**record, "status": "starting", "app_state": "unknown",
                                               "app_pids": [], "last_error": None})
        (root / "logs").mkdir(exist_ok=True)
        monitor = AppMonitor()
        controller = ServerController(root, installation_id)
        loop = LifecycleSupervisor(monitor, controller)
        while True:
            request = read_json(stop_path)
            if request and request.get("instance_id") == instance:
                break
            config = configuration.load(config_path)
            if not config.get("auto_start", True):
                break
            try:
                update = loop.step(time.monotonic(), config)
            except Exception:
                update = {"status": "error", "app_state": "unknown", "app_pids": [],
                          "last_error": "supervision_failed"}
            if update != previous:
                configuration.atomic_json(state_path, {**record, **update})
                previous = update
            time.sleep(2)
    finally:
        if controller:
            controller.close()
        if monitor:
            monitor.close()
        current = read_json(state_path)
        if current and current.get("instance_id") == instance:
            configuration.atomic_json(state_path, {**record, "status": "stopped", "app_state": "unknown",
                                                   "app_pids": [], "last_error": None})
        mutex.close()
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log.close()
