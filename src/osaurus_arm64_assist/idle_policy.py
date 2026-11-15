"""Follow Osaurus' global idle timer without blocking inference supervision.

Only the idleUnloadSeconds value is retained from the runtime-settings response.
The watcher never writes Osaurus settings. Its own cache contains no credentials
or other runtime settings and is bound to a validated loopback endpoint.
"""

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit


SETTING = "idleUnloadSeconds"
MAX_SECONDS = 2**31 - 1
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_ENDPOINT = "http://127.0.0.1:1337"
DEFAULT_SECONDS = 300


class IdlePolicyError(Exception):
    """A safe, fixed error code; never carry response bodies or URLs."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _valid_seconds(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SECONDS


def normalize_endpoint(value):
    """Accept only a plain HTTP loopback origin, with no user information."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise IdlePolicyError("invalid_endpoint")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else 80
        if (parsed.scheme != "http" or host not in ("127.0.0.1", "localhost", "::1")
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or not 1 <= port <= 65535):
            raise IdlePolicyError("invalid_endpoint")
    except (ValueError, TypeError):
        raise IdlePolicyError("invalid_endpoint") from None
    # Avoid DNS, including a modified localhost mapping. The IPv6 loopback stays
    # available when the Osaurus server explicitly listens on ::1.
    host = "127.0.0.1" if host == "localhost" else host
    authority = "[::1]" if host == "::1" else host
    return f"http://{authority}:{port}"


def _load_small_json(path, limit):
    with Path(path).open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("oversized_json")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except RecursionError:
        raise ValueError("invalid_json") from None


def discover_endpoint(config):
    """Use only the port from Osaurus metadata; never trust a metadata host."""
    requested = config.get("osaurus_url", "auto")
    if requested != "auto":
        return normalize_endpoint(requested)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        try:
            metadata = _load_small_json(Path(local_app_data) / "osaurus" / "server.json", 4096)
            port = metadata.get("port") if isinstance(metadata, dict) else None
            if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
                return f"http://127.0.0.1:{port}"
        except (OSError, ValueError, TypeError):
            pass
    return DEFAULT_ENDPOINT


def read_osaurus_idle_seconds(endpoint, api_key=None, timeout=1.0):
    """Read one setting with a total socket deadline and a 64 KiB body limit.

    http.client connects directly, so system/environment proxies do not apply.
    Redirect responses are refused before their Location is considered.
    """
    endpoint = normalize_endpoint(endpoint)
    parsed = urlsplit(endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    expired = threading.Event()
    deadline_timer = None
    headers = {"Accept": "application/json", "Connection": "close"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    def remaining(sock):
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise IdlePolicyError("network_timeout")
        sock.settimeout(seconds)

    try:
        connection.connect()
        transport = connection.sock

        def interrupt_at_deadline():
            expired.set()
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining(transport)
        # HTTPResponse.begin() reads header lines internally. Interrupt the
        # socket at the total deadline so a slow header drip cannot keep this
        # watcher blocked by repeatedly satisfying a per-read timeout.
        deadline_timer = threading.Timer(deadline - time.monotonic(), interrupt_at_deadline)
        deadline_timer.daemon = True
        deadline_timer.start()
        connection.request("GET", "/admin/runtime-settings", headers=headers)
        remaining(transport)
        # Retain the original socket until the body is consumed. HTTPConnection
        # otherwise closes its socket object as soon as a HTTP/1.0 or
        # Connection: close response begins, preventing deadline updates.
        with http.client.HTTPResponse(transport, method="GET") as response:
            response.begin()
            if expired.is_set():
                raise IdlePolicyError("network_timeout")
            if 300 <= response.status <= 399:
                raise IdlePolicyError("redirect_refused")
            if response.status != 200:
                raise IdlePolicyError(f"http_{response.status}")
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_RESPONSE_BYTES:
                        raise IdlePolicyError("oversized_response")
                except ValueError:
                    raise IdlePolicyError("invalid_response") from None
            parts = []
            count = 0
            while True:
                remaining(transport)
                chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_RESPONSE_BYTES:
                    raise IdlePolicyError("oversized_response")
                parts.append(chunk)
        if expired.is_set():
            raise IdlePolicyError("network_timeout")
        try:
            value = json.loads(b"".join(parts).decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            raise IdlePolicyError("invalid_json") from None
        seconds = value.get(SETTING) if isinstance(value, dict) else None
        if not _valid_seconds(seconds):
            raise IdlePolicyError("invalid_setting")
        return seconds
    except IdlePolicyError:
        raise
    except (TimeoutError, socket.timeout):
        raise IdlePolicyError("network_timeout") from None
    except (OSError, http.client.HTTPException):
        raise IdlePolicyError("network_timeout" if expired.is_set() else "network_unavailable") from None
    except (ValueError, TypeError):
        raise IdlePolicyError("invalid_request") from None
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()
            deadline_timer.join(timeout=0.05)
        connection.close()


class IdlePolicy:
    """Maintain a fast, observable effective timer on a separate watcher thread."""

    def __init__(self, config_path, get_config, *, poll_seconds=5.0, autostart=True):
        self._get_config = get_config
        self._cache_path = Path(config_path).resolve().parent / "state" / "idle-policy.json"
        self._poll_seconds = max(0.01, float(poll_seconds))
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._endpoint = None
        self._last_good = None
        self._persisted = None
        self._state = {"mode": "osaurus", "seconds": DEFAULT_SECONDS,
                       "status": "fallback", "error": None, "setting": SETTING}
        # Initial cache reads are small and local. Network work begins only on
        # the watcher (or an explicit poll_once call).
        try:
            self._initialize(dict(get_config()))
        except Exception:
            self._publish("osaurus", DEFAULT_SECONDS, "fallback", "configuration_unavailable")
        if autostart:
            self._thread = threading.Thread(target=self._watch, name="osaurus-idle-policy", daemon=True)
            self._thread.start()

    @property
    def seconds(self):
        with self._lock:
            return self._state["seconds"]

    def effective_seconds(self):
        return self.seconds

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def _publish(self, mode, seconds, status, error=None):
        with self._lock:
            self._state = {"mode": mode, "seconds": seconds, "status": status,
                           "error": error, "setting": SETTING}

    @staticmethod
    def _options(config):
        mode = config.get("idle_source", "osaurus")
        fallback = config.get("idle_seconds", DEFAULT_SECONDS)
        if mode not in ("osaurus", "fixed") or not _valid_seconds(fallback):
            raise IdlePolicyError("invalid_configuration")
        return mode, fallback

    def _cache_for_endpoint(self, endpoint):
        if endpoint == self._endpoint:
            return
        self._endpoint = endpoint
        self._last_good = None
        self._persisted = None
        try:
            cached = _load_small_json(self._cache_path, 4096)
            if (not isinstance(cached, dict)
                    or set(cached) != {"schema_version", "endpoint", "seconds", "updated_utc"}
                    or type(cached.get("schema_version")) is not int or cached["schema_version"] != 1
                    or cached.get("endpoint") != endpoint or not _valid_seconds(cached.get("seconds"))
                    or not isinstance(cached.get("updated_utc"), str)):
                return
            timestamp = datetime.fromisoformat(cached["updated_utc"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
                return
            self._last_good = cached["seconds"]
            self._persisted = (endpoint, cached["seconds"])
        except (OSError, ValueError, TypeError):
            pass

    def _initialize(self, config):
        try:
            mode, fallback = self._options(config)
            if mode == "fixed":
                self._publish(mode, fallback, "fixed")
                return
            endpoint = discover_endpoint(config)
            self._cache_for_endpoint(endpoint)
            self._publish(mode, self._last_good if self._last_good is not None else fallback,
                          "cached" if self._last_good is not None else "fallback")
        except IdlePolicyError as error:
            self._publish("osaurus", DEFAULT_SECONDS if not _valid_seconds(config.get("idle_seconds")) else config["idle_seconds"],
                          "fallback", error.code)

    def _save_cache(self, endpoint, seconds):
        if self._persisted == (endpoint, seconds):
            return
        value = {"schema_version": 1, "endpoint": endpoint, "seconds": seconds,
                 "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".idle-policy-", suffix=".tmp", dir=self._cache_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, allow_nan=False, separators=(",", ":"))
                output.write("\n")
            os.replace(temporary_name, self._cache_path)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        self._persisted = (endpoint, seconds)

    def poll_once(self, config=None):
        if self._stop.is_set():
            return self.snapshot()
        with self._poll_lock:
            try:
                current = dict(self._get_config() if config is None else config)
            except Exception:
                previous = self.snapshot()
                status = "fixed" if previous["mode"] == "fixed" else "cached" if self._last_good is not None else "fallback"
                self._publish(previous["mode"], previous["seconds"], status, "configuration_unavailable")
                return self.snapshot()
            try:
                mode, fallback = self._options(current)
                if mode == "fixed":
                    self._publish(mode, fallback, "fixed")
                    return self.snapshot()
                endpoint = discover_endpoint(current)
            except IdlePolicyError as error:
                fallback = current.get("idle_seconds", DEFAULT_SECONDS)
                self._publish("osaurus", fallback if _valid_seconds(fallback) else DEFAULT_SECONDS,
                              "fallback", error.code)
                return self.snapshot()
            self._cache_for_endpoint(endpoint)
            try:
                seconds = read_osaurus_idle_seconds(endpoint, api_key=os.environ.get("OSAURUS_API_KEY"), timeout=1.0)
            except IdlePolicyError as error:
                self._publish(mode, self._last_good if self._last_good is not None else fallback,
                              "cached" if self._last_good is not None else "fallback", error.code)
            else:
                self._last_good = seconds
                cache_error = None
                try:
                    self._save_cache(endpoint, seconds)
                except (OSError, ValueError, TypeError):
                    cache_error = "cache_write_failed"
                self._publish(mode, seconds, "live", cache_error)
            return self.snapshot()

    def _watch(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.25)
