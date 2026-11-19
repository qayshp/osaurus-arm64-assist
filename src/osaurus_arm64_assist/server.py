"""Loopback OpenAI-compatible chat provider with explicit execution profiles."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit
import uuid

from . import __version__
from .lifecycle import Manager, MemoryPressure, WorkerFailure, memory_status
from .osaurus_proxy import ProxyError, proxy_events
from .protocol import normalize_request


def strict_json(data):
    def invalid(value):
        raise ValueError(f"Non-finite JSON number is not allowed: {value}")
    return json.loads(data, parse_constant=invalid)


def merge_preferences(profile, body):
    """Explicit request fields win over inherited model preferences, including 0."""
    defaults = profile.get("sampling_defaults") or {}
    merged = {**defaults, **body}
    if "max_tokens" in body and "max_completion_tokens" not in body:
        merged.pop("max_completion_tokens", None)
    template = profile.get("template_kwargs") or {}
    if isinstance(template, dict):
        if "chat_template_kwargs" not in body:
            if template:
                merged["chat_template_kwargs"] = dict(template)
        elif isinstance(body["chat_template_kwargs"], dict):
            merged["chat_template_kwargs"] = {**template, **body["chat_template_kwargs"]}
    return merged


def unsupported_request(body):
    """Unknown native request controls stay with Osaurus rather than being lost."""
    supported = {"model", "messages", "tools", "tool_choice", "response_format", "stop", "stream", "stream_options",
                 "max_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "min_p", "seed",
                 "repeat_penalty", "repeat_last_n", "frequency_penalty", "presence_penalty", "n",
                 "logprobs", "top_logprobs", "enable_thinking", "chat_template_kwargs"}
    if set(body) - supported:
        return True
    if isinstance(body.get("stream_options"), dict) and set(body["stream_options"]) - {"include_usage"}:
        return True
    # Remaining unsupported values (schema output, vision, thinking, n>1, etc.)
    # are detected by normalize_request without invoking a model or runtime.
    return False


def sync_snapshot(manager):
    source = getattr(manager, "shared_models", None)
    return source.snapshot() if source else None


def public_profile(model):
    return {"id": model["id"], "mode": model["mode"], "context": model.get("context", 2048),
            "synced": model.get("synced", False), "settings_source": model.get("settings_source", "fixed"),
            "fallback_reason": model.get("_fallback_reason"), "sync": model.get("sync")}


class AssistServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, manager, instance_id=None):
        self.manager = manager
        self.instance_id = instance_id or str(uuid.uuid4())
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OsaurusArm64Assist/" + __version__

    def setup(self):
        super().setup()
        self.connection.settimeout(60)
        self.response_started = False

    def handle_one_request(self):
        self.response_started = False
        return super().handle_one_request()

    def log_message(self, format_string, *args):
        # No request bodies, tokens, Authorization values or query strings are logged.
        print(time.strftime("%Y-%m-%dT%H:%M:%S"), self.command, urlsplit(self.path).path,
              str(args[1]) if len(args) > 1 else "", flush=True)

    def origin(self):
        origin = self.headers.get("Origin")
        allowed = self.server.manager.config.get("origins", ["http://127.0.0.1:1337", "http://localhost:1337"])
        if origin and origin not in allowed:
            raise PermissionError("Browser origin is not allowed")
        return origin

    def headers_for(self, status, content_type, length=None):
        origin = self.origin()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if self.close_connection:
            self.send_header("Connection", "close")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers(); self.response_started = True

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.headers_for(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def authorized(self):
        expected = "Bearer " + self.server.manager.config["api_key"]
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def error(self, status, message, kind="invalid_request_error", code=None):
        self.close_connection = True
        error = {"message": message, "type": kind}
        if code is not None:
            error["code"] = code
        if self.response_started:
            self.sse({"error": error})
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        else:
            self.send_json(status, {"error": error})

    def do_OPTIONS(self):
        try:
            self.origin()
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0"); self.end_headers()
        except PermissionError:
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        try:
            self.origin()
            manager = self.server.manager
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path in ("/", "/health"):
                child = manager.child
                try:
                    loaded = bool(child and child.is_alive())
                except (ValueError, OSError):
                    loaded = False
                self.send_json(200, {"service": "osaurus-arm64-assist", "status": "ok", "version": __version__,
                    "pid": os.getpid(), "instance_id": self.server.instance_id,
                    "model_loaded": loaded, "active_model": manager.active_profile if loaded else None,
                    "worker_job_attached": bool(manager.job and manager.job.handle),
                    "idle_policy": manager.idle_policy.snapshot() if getattr(manager, "idle_policy", None) else None,
                    "model_sync": sync_snapshot(manager),
                    "memory": memory_status()})
                return
            if not self.authorized():
                self.error(401, "A valid companion API key is required", "authentication_error"); return
            config = manager.refresh()
            if path == "/v1/models":
                self.send_json(200, {"object": "list", "data": [
                    {"id": model["id"], "object": "model", "created": 0, "owned_by": "osaurus-arm64-assist",
                     "context_length": model.get("context", 2048), "max_model_len": model.get("context", 2048),
                     "assist": {"mode": model["mode"], "experimental": True, "modalities": ["text"],
                                "synced": model.get("synced", False), "settings_source": model.get("settings_source", "fixed")}}
                    for model in config["models"] if model.get("enabled", True)]})
            elif path == "/sync":
                self.send_json(200, {"model_sync": sync_snapshot(manager), "models": [public_profile(model)
                    for model in config["models"] if model.get("enabled", True)]})
            elif path in ("/metrics", "/v1/metrics"):
                self.send_json(200, {"last_request": manager.last_metrics, "active_model": manager.active_profile})
            else:
                self.error(404, "Unknown endpoint")
        except PermissionError:
            self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers()
        except (ValueError, TypeError, KeyError, OSError) as error:
            self.error(400, str(error))

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not supported; send Content-Length")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length") from error
        if not 0 <= size <= 4*1024*1024:
            raise ValueError("Request body must not exceed 4 MiB")
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("Incomplete request body")
        return strict_json(data.decode("utf-8")) if data else {}

    def do_POST(self):
        manager, acquired, events = self.server.manager, False, None
        try:
            self.origin()
            if not self.authorized():
                self.error(401, "A valid companion API key is required", "authentication_error"); return
            path = urlsplit(self.path).path.rstrip("/")
            body = self.read_body()
            if path == "/admin/shutdown":
                self.send_json(200, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start(); return
            if path == "/admin/unload":
                acquired = manager.lock.acquire(blocking=False)
                if not acquired:
                    self.error(409, "An inference request is active; cancel it before unloading"); return
                manager._stop(); self.send_json(200, {"status": "unloaded"}); return
            if path not in ("/v1/chat/completions", "/admin/benchmark"):
                self.error(404, "Unknown endpoint"); return
            config = manager.refresh()
            if path == "/admin/benchmark":
                from .benchmark import normalize_benchmark
                # Validate primitive types before selecting/loading any worker.
                request = normalize_benchmark(body, 131072)
                profile = next((model for model in config["models"] if model["id"] == request["model"] and model.get("enabled", True)), None)
                if not profile:
                    self.error(404, "Unknown or disabled model profile"); return
                if profile.get("mode") == "osaurus":
                    self.error(400, "Fixed-token benchmarks require a companion native execution profile", code="native_benchmark_required"); return
                normalize_benchmark(request, profile.get("context", 2048))
                acquired = manager.lock.acquire(blocking=False)
                if not acquired:
                    self.error(429, "One model request is already running; retry after it completes", "rate_limit_error"); return
                api_started = time.perf_counter()
                report = manager.benchmark(profile, request)
                report["api_seconds"] = time.perf_counter()-api_started
                manager.last_metrics = {"model": profile["id"], "benchmark": True,
                                        "summary": report["summary"], "api_seconds": report["api_seconds"]}
                self.send_json(200, report); return
            if not isinstance(body, dict):
                raise ValueError("Request must be a JSON object")
            profile = next((model for model in config["models"] if model["id"] == body.get("model") and model.get("enabled", True)), None)
            if not profile:
                self.error(404, "Unknown or disabled model profile"); return
            proxy = profile.get("mode") == "osaurus"
            if not proxy:
                merged = merge_preferences(profile, body)
                if profile.get("synced") and unsupported_request(merged):
                    proxy = True
                else:
                    try:
                        request = normalize_request(merged)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        if not profile.get("synced"):
                            raise
                        proxy = True
                if proxy:
                    profile = {**profile, "_fallback_reason": "unsupported_request_features"}
                elif profile.get("synced") and request["max_tokens"] >= profile.get("context", 2048):
                    proxy = True
                    profile = {**profile, "_fallback_reason": "requested_output_exceeds_assist_context"}
            acquired = manager.lock.acquire(blocking=False)
            if not acquired:
                self.error(429, "One model request is already running; retry after it completes", "rate_limit_error"); return
            api_started = time.perf_counter()
            if not proxy and hasattr(manager, "native_admission"):
                residency_reason = manager.native_admission(profile, config)
                if residency_reason:
                    proxy = True
                    profile = {**profile, "_fallback_reason": residency_reason}
            if not proxy:
                events = manager.generate(profile, request)
                try:
                    first_event = next(events)
                except ValueError as error:
                    # Only retry a known capacity mismatch, before any client
                    # output. Other model/output errors retain their contract.
                    if not profile.get("synced") or not re.fullmatch(
                            r"Prompt \([0-9]+ tokens\) plus requested output \([0-9]+\) exceeds the configured [0-9]+-token context", str(error)):
                        raise
                    events.close()
                    events = None
                    proxy = True
                    profile = {**profile, "_fallback_reason": "prompt_exceeds_assist_context"}
            if proxy:
                # One admission slot covers both engines; release native memory
                # before asking Osaurus to allocate its own local model.
                manager._stop()
                events = proxy_events(profile, body)
                first_event = next(events)
                if body.get("stream"):
                    self.close_connection = True
                    self.headers_for(200, "text/event-stream; charset=utf-8")
                    event = first_event
                    while True:
                        if event["type"] == "end":
                            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                            return
                        if event["type"] != "sse":
                            raise ProxyError(502, "invalid_stream", "Osaurus returned an invalid stream event.")
                        value = event["value"]
                        if "assist" in value:
                            value["assist"]["api_seconds"] = time.perf_counter()-api_started
                            manager.last_metrics = {"model": profile["id"], **value["assist"]}
                        self.sse(value)
                        event = next(events)
                if first_event["type"] != "json":
                    raise ProxyError(502, "invalid_response", "Osaurus returned an invalid completion response.")
                value = first_event["value"]
                value["assist"]["api_seconds"] = time.perf_counter()-api_started
                manager.last_metrics = {"model": profile["id"], **value["assist"]}
                self.send_json(200, value)
                return
            buffered = bool(request["tools"] and request["tool_choice"] != "none") or request["response_format"]["type"] != "text"
            identifier, created = "chatcmpl-"+uuid.uuid4().hex, int(time.time())
            base = {"id": identifier, "created": created, "model": profile["id"]}
            if request.get("stream"):
                if buffered:
                    finished = first_event if first_event["type"] == "done" else None
                    for event in events:
                        if event["type"] == "done": finished = event
                    if not finished:
                        raise WorkerFailure("No completed response was received")
                    self.start_stream(base)
                    delta = dict(finished["message"]); delta.pop("role", None)
                    if delta.get("tool_calls"):
                        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
                    self.chunk(base, delta)
                else:
                    self.start_stream(base)
                    finished = None
                    def accept(event):
                        if event["type"] == "token": self.chunk(base, {"content": event["text"]})
                    if first_event["type"] == "done": finished = first_event
                    else: accept(first_event)
                    for event in events:
                        if event["type"] == "done": finished = event
                        else: accept(event)
                    if not finished:
                        raise WorkerFailure("No completed response was received")
                finished["assist"]["api_seconds"] = time.perf_counter()-api_started
                manager.last_metrics = {"model": profile["id"], **finished["assist"]}
                self.chunk(base, {}, finished["finish_reason"], assist=finished["assist"])
                options = request.get("stream_options") or {}
                if isinstance(options, dict) and options.get("include_usage"):
                    self.sse({**base, "object": "chat.completion.chunk", "choices": [], "usage": finished["usage"]})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
                self.close_connection = True
            else:
                finished = first_event if first_event["type"] == "done" else None
                for event in events:
                    if event["type"] == "done": finished = event
                if not finished:
                    raise WorkerFailure("No completed response was received")
                finished["assist"]["api_seconds"] = time.perf_counter()-api_started
                manager.last_metrics = {"model": profile["id"], **finished["assist"]}
                self.send_json(200, {**base, "object": "chat.completion", "choices": [{"index": 0,
                    "message": finished["message"], "finish_reason": finished["finish_reason"]}],
                    "usage": finished["usage"], "assist": finished["assist"]})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if acquired:
                manager._stop()
        except TimeoutError:
            if acquired:
                manager._stop()
            try:
                self.error(504, "The companion request timed out before a complete response was produced.",
                           "timeout_error", "request_timeout")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                # A write timeout can also mean the client socket is no longer
                # writable. The owned worker was already stopped above.
                self.close_connection = True
        except PermissionError:
            self.close_connection = True
            self.send_response(403); self.send_header("Connection", "close")
            self.send_header("Content-Length", "0"); self.end_headers()
        except (ValueError, TypeError, KeyError, AttributeError, StopIteration) as error:
            self.error(400, str(error))
        except MemoryPressure as error:
            self.error(503, str(error), "native_worker_error", error.code)
        except WorkerFailure as error:
            self.error(503, str(error), "native_worker_error")
        except ProxyError as error:
            self.error(error.status, error.message, "osaurus_proxy_error", error.code)
        except Exception:
            self.error(500, "Companion request failed; inspect the local service log", "server_error")
        finally:
            if events:
                events.close()
            if acquired:
                manager.lock.release()

    def start_stream(self, base):
        self.headers_for(200, "text/event-stream; charset=utf-8")
        self.chunk(base, {"role": "assistant"})

    def sse(self, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.wfile.write(b"data: "+data+b"\n\n"); self.wfile.flush()

    def chunk(self, base, delta, finish=None, assist=None):
        value = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if assist is not None:
            value["assist"] = assist
        self.sse(value)


def serve(config_path, host=None, port=None, instance_id=None):
    manager = Manager(config_path)
    host = host or manager.config.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        manager.close(); raise ValueError("Only loopback 127.0.0.1 is supported")
    port = port or manager.config.get("port", 8082)
    server = None
    try:
        server = AssistServer((host, port), manager, instance_id)
        from .discovery import DiscoveryRefresh
        manager.discovery = DiscoveryRefresh(manager.refresh)
        print(f"Osaurus ARM64 Assist {__version__} listening on http://{host}:{port}", flush=True)
        server.serve_forever(poll_interval=.2)
    finally:
        if server:
            server.server_close()
        manager.close()
