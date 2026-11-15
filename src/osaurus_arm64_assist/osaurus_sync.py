"""Read-only, cached local Osaurus inventory and per-model settings sharing."""
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
import re
import socket
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit

from .idle_policy import discover_endpoint, normalize_endpoint
from . import model_settings


MAX_MODELS = 4096
MAX_BYTES = 4 * 1024 * 1024
ENTRY_KEYS = ("id", "fileName", "sizeBytes", "architecture", "parameterSize", "quantization",
              "contextLength", "relativePath", "repoId", "kind", "external", "externalSource")
RUNTIME_KEYS = ("localModelsDir", "localGpuLayers", "localContextSize", "localAccelerator", "maxResidentModels")
HEADER_KEYS = ("architecture", "general_type", "has_chat_template", "split_count", "split_no",
               "file_type", "quantization", "tensor_count", "context_length", "version")


class SyncError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def read_json(endpoint, path, *, timeout=1.0):
    """A bounded direct loopback GET. Never use proxies or follow redirects."""
    origin = urlsplit(normalize_endpoint(endpoint))
    connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    timer = None
    expired = threading.Event()
    try:
        connection.connect()
        transport = connection.sock

        def expire():
            expired.set()
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0 or expired.is_set():
                raise SyncError("network_timeout")
            transport.settimeout(value)

        remaining()
        timer = threading.Timer(deadline - time.monotonic(), expire)
        timer.daemon = True; timer.start()
        headers = {"Accept": "application/json", "Connection": "close"}
        key = os.environ.get("OSAURUS_API_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
        connection.request("GET", path, headers=headers)
        remaining()
        with http.client.HTTPResponse(transport, method="GET") as response:
            response.begin()
            remaining()
            if 300 <= response.status < 400:
                raise SyncError("redirect_refused")
            if response.status != 200:
                raise SyncError(f"http_{response.status}")
            length = response.getheader("Content-Length")
            expected = int(length) if length is not None else None
            if expected is not None and (expected < 0 or expected > MAX_BYTES):
                raise SyncError("invalid_response_size")
            parts, count = [], 0
            while True:
                remaining()
                chunk = response.read1(min(8192, MAX_BYTES + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_BYTES:
                    raise SyncError("oversized_response")
                parts.append(chunk)
            if expected is not None and not response.chunked and count != expected:
                raise SyncError("invalid_response_size")
        if expired.is_set():
            raise SyncError("network_timeout")
        return json.loads(b"".join(parts).decode("utf-8-sig"))
    except SyncError:
        raise
    except (TimeoutError, socket.timeout):
        raise SyncError("network_timeout") from None
    except (OSError, http.client.HTTPException):
        raise SyncError("network_timeout" if expired.is_set() else "network_unavailable") from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SyncError("invalid_json") from None
    finally:
        if timer:
            timer.cancel(); timer.join(timeout=.05)
        connection.close()


def _lexical(path):
    # No filesystem access: request-time aliases use precomputed canonical paths.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def automatic_id(source_id):
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_id).strip("-.")[:80] or "model"
    return "osaurus/" + slug + "-" + hashlib.sha256(source_id.encode()).hexdigest()[:16]


def _valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and "://" not in value and all(ord(c) >= 32 for c in value)


def resolve_model_path(entry, runtime):
    relative = entry.get("relativePath")
    if not isinstance(relative, str) or not relative or "\x00" in relative or "://" in relative:
        raise ValueError("invalid_model_path")
    path = Path(relative)
    if entry.get("external") is True:
        if not path.is_absolute():
            raise ValueError("external_path_must_be_absolute")
        return path.resolve()
    if entry.get("external", False) is not False:
        raise ValueError("invalid_external_flag")
    base = runtime.get("localModelsDir")
    if not isinstance(base, str) or not Path(base).is_absolute():
        raise ValueError("missing_models_directory")
    portable = relative.replace("\\", "/")
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or ".." in portable.split("/"):
        raise ValueError("model_path_traversal")
    root = Path(base).resolve()
    path = (root / portable).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("model_path_traversal")
    return path


def _safe_header(value):
    return {key: value[key] for key in HEADER_KEYS if key in value and isinstance(value[key], (str, int, bool, type(None)))}


def _safe_runtime(value):
    if not isinstance(value, dict):
        raise SyncError("invalid_runtime_settings")
    return {key: value[key] for key in RUNTIME_KEYS if key in value and isinstance(value[key], (str, int, bool, type(None)))}


class SharedModels:
    def __init__(self, config_path, get_config, *, poll_seconds=5, autostart=True):
        self._get_config = get_config
        self._cache_path = Path(config_path).resolve().parent / "state" / "osaurus-models.json"
        self._root = Path(config_path).resolve().parent
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._thread = None
        self._poll_seconds = max(.01, float(poll_seconds))
        self._records = []
        self._runtime = {}
        self._known_paths = set()
        self._files = {}
        self._headers = {}
        self._resolved = {}
        self._endpoint = None
        self._source = "osaurus"
        self._status = "unavailable"
        self._error = None
        self._excluded = 0
        self._pending = 0
        self._revision = 0
        self._advertised_signature = None
        self._counts = {"automatic_profiles": 0, "manual_profiles": 0, "native_profiles": 0, "proxy_profiles": 0}
        try:
            config = dict(get_config())
            self._source = config.get("models_source", "osaurus")
            self._scan_manual(config.get("models", []))
            if self._source == "fixed":
                self._status = "fixed"
            else:
                self._switch_endpoint(discover_endpoint(config))
        except Exception:
            self._error = "configuration_unavailable"
        if autostart:
            self._thread = threading.Thread(target=self._watch, name="osaurus-model-settings", daemon=True)
            self._thread.start()

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def snapshot(self):
        with self._lock:
            return {"source": self._source, "status": self._status, "error": self._error,
                    "inventory_models": len(self._records), "excluded_models": self._excluded,
                    "pending_downloads": self._pending, "revision": self._revision, **self._counts}

    def _scan_manual(self, profiles):
        files = {}
        for profile in profiles:
            try:
                raw = _lexical(profile["path"])
                path = Path(profile["path"]).resolve()
                files[raw] = {"canonical": _lexical(path), "exists": path.is_file()}
                files[_lexical(path)] = files[raw]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        with self._lock:
            self._files = files

    def _header(self, path):
        stat = path.stat()
        key = (_lexical(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._headers:
            try:
                self._headers[key] = _safe_header(model_settings.inspect_gguf(path))
            except ValueError:
                self._headers[key] = None
            if len(self._headers) > MAX_MODELS * 2:
                self._headers.pop(next(iter(self._headers)))
        if self._headers.get(key) is None:
            raise ValueError("invalid_gguf_header")
        return self._headers[key], stat

    def _chat_file(self, entry, runtime):
        if entry.get("kind", "model") not in ("model", "chat", "llm"):
            raise ValueError("not_chat")
        path = resolve_model_path(entry, runtime)
        if path.suffix.lower() != ".gguf" or not path.is_file() or path.is_relative_to(self._root):
            raise ValueError("not_external_gguf")
        size = entry.get("sizeBytes")
        if type(size) is not int or size <= 0:
            raise ValueError("invalid_expected_size")
        header, stat = self._header(path)
        if stat.st_size != size or header.get("has_chat_template") is not True:
            raise ValueError("incomplete_or_nonchat")
        if header.get("general_type") is not None and str(header["general_type"]).casefold() not in ("model", ""):
            raise ValueError("not_chat")
        if entry.get("architecture") and entry["architecture"] != header.get("architecture"):
            raise ValueError("mismatched_architecture")
        if entry.get("fileName") and path.name.casefold() != str(entry["fileName"]).casefold():
            raise ValueError("mismatched_filename")
        count, index = header.get("split_count", 1), header.get("split_no", 0)
        if type(count) is not int or not 1 <= count <= 256 or type(index) is not int or index != 0:
            raise ValueError("invalid_split")
        if count > 1:
            match = re.fullmatch(r"(.+)-00001-of-(\d{5})\.gguf", path.name, flags=re.IGNORECASE)
            if not match or int(match[2]) != count:
                raise ValueError("invalid_split_name")
            for index in range(1, count):
                if self._stop.is_set():
                    raise SyncError("stopped")
                piece = path.with_name(f"{match[1]}-{index+1:05d}-of-{count:05d}.gguf")
                piece_header, piece_stat = self._header(piece)
                if (piece_stat.st_size <= 0 or piece_header.get("split_count") != count
                        or piece_header.get("split_no") != index
                        or piece_header.get("architecture") != header.get("architecture")):
                    raise ValueError("mismatched_split")
        return path, header, stat

    def _switch_endpoint(self, endpoint):
        if endpoint == self._endpoint:
            return
        with self._lock:
            self._endpoint = endpoint
            self._records, self._runtime, self._known_paths = [], {}, set()
            self._status, self._error = "unavailable", None
        self._resolved = {}
        try:
            with self._cache_path.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return
            cached = json.loads(raw.decode("utf-8-sig"))
            if (not isinstance(cached, dict) or type(cached.get("schema_version")) is not int
                    or cached["schema_version"] != 1 or cached.get("endpoint") != endpoint
                    or not isinstance(cached.get("records"), list) or len(cached["records"]) > MAX_MODELS):
                return
            timestamp = datetime.fromisoformat(cached["updated_utc"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
                return
            runtime = _safe_runtime(cached.get("runtime"))
            records = []
            for record in cached["records"]:
                entry = {key: record["entry"][key] for key in ENTRY_KEYS if key in record["entry"]}
                if not _valid_id(entry.get("id")):
                    continue
                path = resolve_model_path(entry, runtime)
                stat = path.stat()
                if stat.st_size != record["bytes"] or stat.st_mtime_ns != record["mtime_ns"]:
                    continue
                header = _safe_header(record["header"])
                self._headers[(_lexical(path), stat.st_size, stat.st_mtime_ns)] = header
                checked_path, header, stat = self._chat_file(entry, runtime)
                records.append({"entry": entry, "path": str(checked_path), "header": header,
                    "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "settings": model_settings.sanitize_settings(record["settings"]),
                    "resolved": model_settings.sanitize_resolved(record["resolved"])})
            known_paths = {_lexical(record["path"]) for record in records}
            known_paths.update(_lexical(value) for value in cached.get("known_paths", []) if isinstance(value, str))
            with self._lock:
                self._records, self._runtime = records, runtime
                self._known_paths = known_paths
                self._status = "cached"
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            pass

    def _save(self):
        value = {"schema_version": 1, "endpoint": self._endpoint,
                 "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "runtime": self._runtime, "records": self._records, "known_paths": sorted(self._known_paths)}
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(encoded) > MAX_BYTES:
            raise ValueError("cache_exceeds_bound")
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".osaurus-models-", suffix=".tmp", dir=self._cache_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
            os.replace(name, self._cache_path)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _downloads(value, entries):
        if not isinstance(value, dict) or not isinstance(value.get("downloads"), list):
            raise SyncError("invalid_download_inventory")
        pending, unknown = set(), False
        for item in value["downloads"]:
            if not isinstance(item, dict):
                unknown = True; continue
            status = item.get("status", item.get("state", ""))
            if isinstance(status, str) and status.casefold() in ("completed", "complete", "finished", "done"):
                continue
            identifiers = [item.get(key) for key in ("modelId", "id", "fileName", "relativePath", "path")]
            matches = {entry["id"] for entry in entries if any(
                isinstance(value, str) and value in (entry.get("id"), entry.get("fileName"), entry.get("relativePath"))
                for value in identifiers)}
            if matches:
                pending.update(matches)
            else:
                unknown = True
        return pending, unknown, len(value["downloads"])

    def poll_once(self):
        if self._stop.is_set():
            return self.snapshot()
        with self._poll_lock:
            try:
                config = dict(self._get_config())
                source = config.get("models_source", "osaurus")
                if source not in ("osaurus", "fixed"):
                    raise SyncError("invalid_configuration")
                self._scan_manual(config.get("models", []))
                if source == "fixed":
                    with self._lock:
                        self._source, self._status, self._error = source, "fixed", None
                    self.profiles(config.get("models", []))
                    return self.snapshot()
                endpoint = discover_endpoint(config)
                with self._lock:
                    self._source = source
                # Endpoint switching/cache reading is confined to this watcher;
                # request paths only take a coherent copy below.
                self._switch_endpoint(endpoint)
                def get(path):
                    if self._stop.is_set():
                        raise SyncError("stopped")
                    return read_json(endpoint, path)
                runtime = _safe_runtime(get("/admin/runtime-settings"))
                inventory = get("/admin/models")
                if not isinstance(inventory, dict) or not isinstance(inventory.get("models"), list) or len(inventory["models"]) > MAX_MODELS:
                    raise SyncError("invalid_model_inventory")
                entries = inventory["models"]
                if any(not isinstance(entry, dict) or not _valid_id(entry.get("id")) for entry in entries):
                    raise SyncError("invalid_model_inventory")
                if len({entry["id"] for entry in entries}) != len(entries):
                    raise SyncError("duplicate_source_model_id")
                pending, unknown, pending_count = self._downloads(get("/admin/models/downloads"), entries)
                previous = {(record["entry"]["id"], record["path"], record["bytes"], record["mtime_ns"]) for record in self._records}
                records, excluded = [], 0
                for raw_entry in entries:
                    if self._stop.is_set():
                        raise SyncError("stopped")
                    entry = {key: raw_entry[key] for key in ENTRY_KEYS if key in raw_entry}
                    if entry["id"] in pending:
                        excluded += 1; continue
                    try:
                        path, header, stat = self._chat_file(entry, runtime)
                    except (OSError, ValueError, KeyError, TypeError):
                        excluded += 1; continue
                    if unknown and (entry["id"], str(path), stat.st_size, stat.st_mtime_ns) not in previous:
                        excluded += 1; continue
                    model_path = "/admin/models/" + quote(entry["id"], safe="") + "/settings"
                    settings = get(model_path)
                    if not isinstance(settings, dict) or not isinstance(settings.get("defaults"), dict) or settings.get("override") is not None and not isinstance(settings.get("override"), dict):
                        raise SyncError("invalid_model_settings")
                    signature = _digest([entry, settings, runtime])
                    cached_resolved = self._resolved.get(entry["id"])
                    if cached_resolved is None or cached_resolved[0] != signature:
                        resolved = model_settings.sanitize_resolved(get(model_path + "/resolved"))
                        self._resolved[entry["id"]] = (signature, resolved)
                    else:
                        resolved = cached_resolved[1]
                    records.append({"entry": entry, "path": str(path), "header": header, "bytes": stat.st_size,
                                    "mtime_ns": stat.st_mtime_ns, "settings": model_settings.sanitize_settings(settings),
                                    "resolved": resolved})
                with self._lock:
                    self._records, self._runtime = records, runtime
                    self._known_paths.update(_lexical(record["path"]) for record in records)
                    for record in records:
                        self._files[_lexical(record["path"])] = {"canonical": _lexical(record["path"]), "exists": True}
                    self._status, self._error = "live", "unsupported_download_state" if unknown else None
                    self._excluded, self._pending = excluded, pending_count
                try:
                    self._save()
                except (OSError, ValueError, TypeError):
                    with self._lock:
                        self._error = "cache_write_failed"
            except Exception as error:
                if self._stop.is_set():
                    return self.snapshot()
                with self._lock:
                    self._status = "cached" if self._records or self._status in ("live", "cached") else "unavailable"
                    self._error = error.code if isinstance(error, SyncError) else "source_unavailable"
            self.profiles(config.get("models", []) if "config" in locals() else [])
            return self.snapshot()

    def profiles(self, manual_profiles):
        with self._lock:
            records, runtime, files = copy.deepcopy((self._records, self._runtime, self._files))
            known, source, endpoint = set(self._known_paths), self._source, self._endpoint
        by_path = {_lexical(record["path"]): record for record in records}
        result = {}
        if source == "osaurus":
            for record in records:
                entry = {**record["entry"], "_gguf": record["header"]}
                profile = model_settings.build_profile(entry, record["settings"], record["resolved"], runtime,
                    record["path"], identifier=automatic_id(entry["id"]))
                profile.setdefault("_osaurus", {}).update(endpoint=endpoint, model_id=entry["id"])
                profile.update(synced=True, settings_source="osaurus")
                result[profile["id"]] = profile
        auto_count = len(result)
        manual_count = 0
        for manual in manual_profiles:
            if not isinstance(manual, dict) or not manual.get("enabled", True):
                continue
            file = files.get(_lexical(manual.get("path", "")))
            if not file or not file["exists"]:
                continue
            path = file["canonical"]
            record = by_path.get(path) if source == "osaurus" else None
            if source == "osaurus" and path in known and record is None:
                continue
            if record and manual.get("settings_source") != "fixed":
                entry = {**record["entry"], "_gguf": record["header"]}
                profile = model_settings.build_profile(entry, record["settings"], record["resolved"], runtime,
                    record["path"], identifier=manual["id"], mode=manual["mode"])
                profile.setdefault("_osaurus", {}).update(endpoint=endpoint, model_id=entry["id"])
                profile.update(synced=True, settings_source="osaurus")
            else:
                profile = copy.deepcopy(manual)
            result[profile["id"]] = profile
            manual_count += 1
        profiles = sorted(result.values(), key=lambda profile: profile["id"])
        signature = _digest([{key: profile.get(key) for key in ("id", "mode", "context", "synced", "_fallback_reason")}
                             for profile in profiles])
        with self._lock:
            if signature != self._advertised_signature:
                self._advertised_signature = signature
                self._revision += 1
            self._counts = {"automatic_profiles": auto_count, "manual_profiles": manual_count,
                            "native_profiles": sum(profile.get("mode") != "osaurus" for profile in profiles),
                            "proxy_profiles": sum(profile.get("mode") == "osaurus" for profile in profiles)}
        return copy.deepcopy(profiles)

    def _watch(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.25)
