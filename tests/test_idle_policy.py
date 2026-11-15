from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from osaurus_arm64_assist import idle_policy


class ReplyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        reply = dict(self.server.reply)
        body = reply.get("body", b'{"idleUnloadSeconds":300}')
        try:
            if reply.get("header_drip"):
                self.close_connection = True
                for byte in b"HTTP/1.1 200 OK\r\nX-Slow-Header: abcdefghijklmnopqrstuvwxyz\r\n\r\n":
                    self.wfile.write(bytes([byte])); self.wfile.flush()
                    time.sleep(reply["header_drip"])
                return
            if reply.get("delay"):
                time.sleep(reply["delay"])
            self.protocol_version = reply.get("protocol", "HTTP/1.0")
            self.send_response(reply.get("status", 200))
            self.send_header("Content-Type", "application/json")
            if not reply.get("omit_length"):
                self.send_header("Content-Length", str(reply.get("length", len(body))))
            for name, value in reply.get("headers", {}).items():
                self.send_header(name, value)
            self.end_headers()
            if reply.get("drip"):
                for byte in body:
                    self.wfile.write(bytes([byte])); self.wfile.flush()
                    time.sleep(reply["drip"])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def log_message(self, *_):
        pass


class IdlePolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "config.json"
        self.cache_path = self.root / "state" / "idle-policy.json"
        self.environment = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local-data"),
                                                       "OSAURUS_API_KEY": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ReplyHandler)
        server.daemon_threads = True
        server.reply = {}
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def close():
            server.shutdown(); server.server_close(); thread.join(timeout=1)

        self.addCleanup(close)
        return server, f"http://127.0.0.1:{server.server_port}"

    def policy(self, config=None, **kwargs):
        config = {} if config is None else config
        policy = idle_policy.IdlePolicy(self.config_path, lambda: config, autostart=False, **kwargs)
        self.addCleanup(policy.close)
        return policy

    def assert_error(self, code, callback):
        with self.assertRaises(idle_policy.IdlePolicyError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def test_live_timer_changes_300_to_15_to_never_and_cache_is_minimal(self):
        server, endpoint = self.server()
        policy = self.policy({"osaurus_url": endpoint, "api_key": "COMPANION_TOKEN_MUST_NOT_LEAK"})
        for seconds in (300, 15, 0):
            server.reply = {"body": json.dumps({"idleUnloadSeconds": seconds, "secret": "PRIVATE_SETTINGS_CANARY"}).encode()}
            self.assertEqual(policy.poll_once(), {"mode": "osaurus", "seconds": seconds, "status": "live",
                                                   "error": None, "setting": "idleUnloadSeconds"})
            self.assertEqual(policy.effective_seconds(), seconds)
        self.assertEqual([r["path"] for r in server.requests], ["/admin/runtime-settings"] * 3)
        self.assertTrue(all(r["authorization"] is None for r in server.requests))
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual(set(cache), {"schema_version", "endpoint", "seconds", "updated_utc"})
        self.assertEqual(cache["seconds"], 0)
        self.assertEqual(cache["endpoint"], endpoint)
        self.assertTrue(cache["updated_utc"].endswith("Z"))
        self.assertNotIn("PRIVATE", self.cache_path.read_text())
        self.assertEqual(list(self.cache_path.parent.glob("*.tmp")), [])

    def test_fixed_mode_never_reads_network_metadata_or_cache(self):
        config = {"idle_source": "fixed", "idle_seconds": 0, "osaurus_url": "https://external.invalid"}
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds") as read, \
                mock.patch.object(idle_policy, "_load_small_json") as load:
            policy = self.policy(config)
            self.assertEqual(policy.poll_once(), {"mode": "fixed", "seconds": 0, "status": "fixed",
                                                   "error": None, "setting": "idleUnloadSeconds"})
            config["idle_seconds"] = 17
            self.assertEqual(policy.poll_once()["seconds"], 17)
            read.assert_not_called(); load.assert_not_called()
        self.assertFalse(self.cache_path.exists())

    def test_fast_accessors_do_no_io_and_return_a_copy(self):
        policy = self.policy({"idle_source": "fixed", "idle_seconds": 12})
        with mock.patch.object(policy, "_get_config", side_effect=AssertionError("No config read")), \
                mock.patch.object(idle_policy, "read_osaurus_idle_seconds", side_effect=AssertionError("No network")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("No files")):
            self.assertEqual(policy.seconds, 12)
            self.assertEqual(policy.effective_seconds(), 12)
            snapshot = policy.snapshot(); snapshot["seconds"] = 99
            self.assertEqual(policy.snapshot()["seconds"], 12)

    def test_cached_zero_survives_restart_and_offline_and_source_change_isolated(self):
        _, endpoint = self.server()
        initial = self.policy({"osaurus_url": endpoint})
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", return_value=0):
            self.assertEqual(initial.poll_once()["status"], "live")
        restarted = self.policy({"osaurus_url": endpoint, "idle_seconds": 123})
        self.assertEqual(restarted.snapshot()["status"], "cached")
        self.assertEqual(restarted.seconds, 0)
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", side_effect=idle_policy.IdlePolicyError("network_unavailable")):
            self.assertEqual(restarted.poll_once()["seconds"], 0)
            self.assertEqual(restarted.snapshot()["status"], "cached")
            self.assertEqual(restarted.snapshot()["error"], "network_unavailable")
            changed = restarted.poll_once({"osaurus_url": "http://127.0.0.1:1", "idle_seconds": 123})
            self.assertEqual(changed["seconds"], 123)
            self.assertEqual(changed["status"], "fallback")

    def test_live_value_is_kept_on_failure_without_rewriting_cache(self):
        server, endpoint = self.server()
        policy = self.policy({"osaurus_url": endpoint, "idle_seconds": 19})
        server.reply = {"body": b'{"idleUnloadSeconds":7}'}
        policy.poll_once()
        cache = self.cache_path.read_bytes()
        server.reply = {"status": 401, "body": b"PRIVATE_AUTH_BODY"}
        state = policy.poll_once()
        self.assertEqual(state["seconds"], 7)
        self.assertEqual(state["status"], "cached")
        self.assertEqual(state["error"], "http_401")
        self.assertEqual(self.cache_path.read_bytes(), cache)
        self.assertNotIn("PRIVATE", json.dumps(state))

    def test_network_failures_auth_and_not_found_are_safe_and_observable(self):
        server, endpoint = self.server()
        policy = self.policy({"osaurus_url": endpoint, "idle_seconds": 21})
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                server.reply = {"status": status, "body": b"PRIVATE_RUNTIME_RESPONSE"}
                state = policy.poll_once()
                self.assertEqual(state["status"], "fallback")
                self.assertEqual(state["seconds"], 21)
                self.assertEqual(state["error"], f"http_{status}")
                self.assertNotIn("PRIVATE", json.dumps(state))
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            unavailable = f"http://127.0.0.1:{unused.getsockname()[1]}"
        with self.assertRaises(idle_policy.IdlePolicyError) as raised:
            idle_policy.read_osaurus_idle_seconds(unavailable)
        self.assertIn(raised.exception.code, ("network_unavailable", "network_timeout"))

    def test_bad_setting_types_and_bounds_are_rejected(self):
        server, endpoint = self.server()
        for value in (True, False, "15", 15.0, -1, None, [], {}, 2**31):
            with self.subTest(value=value):
                server.reply = {"body": json.dumps({"idleUnloadSeconds": value}).encode()}
                self.assert_error("invalid_setting", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))
        for body in (b"{}", b"[]", b"null", b'{"otherSetting":15}'):
            with self.subTest(body=body):
                server.reply = {"body": body}
                self.assert_error("invalid_setting", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))
        server.reply = {"body": json.dumps({"idleUnloadSeconds": 2**31 - 1}).encode()}
        self.assertEqual(idle_policy.read_osaurus_idle_seconds(endpoint), 2**31 - 1)

    def test_malformed_and_oversized_responses_are_bounded(self):
        server, endpoint = self.server()
        for body in (b"PRIVATE_NOT_JSON", b"\xff", b'{"idleUnloadSeconds":'):
            server.reply = {"body": body}
            self.assert_error("invalid_json", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))
        server.reply = {"body": b"[" * 2000 + b"]" * 2000}
        with self.assertRaises(idle_policy.IdlePolicyError) as raised:
            idle_policy.read_osaurus_idle_seconds(endpoint)
        # JSON decoder recursion limits differ across supported Python builds;
        # either a safe parse failure or rejection of the array shape is valid.
        self.assertIn(raised.exception.code, ("invalid_json", "invalid_setting"))
        server.reply = {"body": b"x" * (idle_policy.MAX_RESPONSE_BYTES + 1)}
        self.assert_error("oversized_response", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))
        server.reply["omit_length"] = True
        self.assert_error("oversized_response", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))

    def test_http11_chunked_response_is_read_without_waiting_for_connection_close(self):
        server, endpoint = self.server()
        body = b'{"idleUnloadSeconds":15}'
        chunks = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        server.reply = {"body": chunks, "omit_length": True, "protocol": "HTTP/1.1",
                        "headers": {"Transfer-Encoding": "chunked"}}
        self.assertEqual(idle_policy.read_osaurus_idle_seconds(endpoint), 15)

    def test_invalid_or_truncated_content_length_is_not_a_live_setting(self):
        server, endpoint = self.server()
        body = b'{"idleUnloadSeconds":15}'
        for length in (-1, "invalid", len(body) + 5):
            with self.subTest(length=length):
                server.reply = {"body": body, "length": length}
                self.assert_error("invalid_response", lambda: idle_policy.read_osaurus_idle_seconds(endpoint))

    def test_socket_deadline_bounds_headers_and_slow_drip_body(self):
        server, endpoint = self.server()
        for reply in ({"delay": 0.25}, {"header_drip": 0.03},
                      {"body": b'{"idleUnloadSeconds":15}', "drip": 0.03}):
            server.reply = reply
            started = time.monotonic()
            self.assert_error("network_timeout", lambda: idle_policy.read_osaurus_idle_seconds(endpoint, timeout=0.10))
            self.assertLess(time.monotonic() - started, 0.5)

    def test_redirect_is_not_followed_and_bearer_is_not_forwarded(self):
        redirect, endpoint = self.server()
        destination, destination_endpoint = self.server()
        redirect.reply = {"status": 302, "headers": {"Location": destination_endpoint + "/capture"}}
        self.assert_error("redirect_refused", lambda: idle_policy.read_osaurus_idle_seconds(endpoint, api_key="PRIVATE_OSAURUS_KEY"))
        self.assertEqual(len(redirect.requests), 1)
        self.assertEqual(redirect.requests[0]["authorization"], "Bearer PRIVATE_OSAURUS_KEY")
        self.assertEqual(destination.requests, [])

    def test_proxy_environment_is_bypassed_and_only_osaurus_key_is_used(self):
        server, endpoint = self.server()
        proxy, proxy_endpoint = self.server()
        config = {"osaurus_url": endpoint, "api_key": "PRIVATE_COMPANION_KEY"}
        policy = self.policy(config)
        with mock.patch.dict(os.environ, {"HTTP_PROXY": proxy_endpoint, "HTTPS_PROXY": proxy_endpoint,
                                          "ALL_PROXY": proxy_endpoint, "NO_PROXY": "",
                                          "http_proxy": proxy_endpoint, "https_proxy": proxy_endpoint,
                                          "all_proxy": proxy_endpoint, "no_proxy": "",
                                          "OSAURUS_API_KEY": "PRIVATE_OSAURUS_KEY"}):
            self.assertEqual(policy.poll_once()["status"], "live")
        self.assertEqual(proxy.requests, [])
        self.assertEqual(server.requests[0]["authorization"], "Bearer PRIVATE_OSAURUS_KEY")
        self.assertNotIn("PRIVATE", self.cache_path.read_text())

    def test_auto_discovery_uses_only_the_port_and_follows_a_port_change(self):
        first, first_endpoint = self.server()
        second, second_endpoint = self.server()
        metadata = self.root / "local-data" / "osaurus" / "server.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"port": first.server_port, "host": "external.invalid",
                                        "url": "https://external.invalid", "secret": "PRIVATE_METADATA"}))
        policy = self.policy()
        self.assertEqual(idle_policy.discover_endpoint({}), first_endpoint)
        self.assertEqual(policy.poll_once()["status"], "live")
        metadata.write_text(json.dumps({"port": second.server_port}))
        second.reply = {"body": b'{"idleUnloadSeconds":0}'}
        self.assertEqual(policy.poll_once()["seconds"], 0)
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(json.loads(self.cache_path.read_text())["endpoint"], second_endpoint)

    def test_invalid_or_missing_metadata_falls_back_to_default_loopback(self):
        metadata = self.root / "local-data" / "osaurus" / "server.json"
        metadata.parent.mkdir(parents=True)
        self.assertEqual(idle_policy.discover_endpoint({}), "http://127.0.0.1:1337")
        for body in ("not json", "[]", '{"port":true}', '{"port":0}', '{"port":"1337"}',
                     '{"port":65536}', "x" * 4097):
            metadata.write_text(body)
            self.assertEqual(idle_policy.discover_endpoint({}), "http://127.0.0.1:1337")

    def test_explicit_urls_are_strict_loopback_origins(self):
        self.assertEqual(idle_policy.normalize_endpoint("http://localhost:1337/"), "http://127.0.0.1:1337")
        self.assertEqual(idle_policy.normalize_endpoint("http://[::1]:1337"), "http://[::1]:1337")
        for endpoint in ("https://127.0.0.1:1337", "http://external.invalid:1337", "http://127.0.0.2:1337",
                         "http://127.1:1337", "http://user:PRIVATE_KEY@127.0.0.1:1337", "http://127.0.0.1:1337/v1",
                         "http://127.0.0.1:1337/?token=PRIVATE_KEY", "http://127.0.0.1:1337/#fragment",
                         "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://[::1", "http://localhost\n:1337",
                         "http://127.0.0.1.evil.invalid:1337", "http://[::ffff:127.0.0.1]:1337", None):
            with self.subTest(endpoint=endpoint):
                self.assert_error("invalid_endpoint", lambda: idle_policy.normalize_endpoint(endpoint))

    def test_invalid_cache_never_overrides_fallback(self):
        self.cache_path.parent.mkdir()
        good = {"schema_version": 1, "endpoint": "http://127.0.0.1:1337", "seconds": 0,
                "updated_utc": "2026-01-01T00:00:00Z"}
        changes = ({"schema_version": True}, {"seconds": True}, {"seconds": -1}, {"seconds": 2**31},
                   {"endpoint": "http://127.0.0.1:1"}, {"updated_utc": "invalid"},
                   {"updated_utc": "2026-01-01T00:00:00"}, {"extra": "PRIVATE_SETTING"})
        for change in changes:
            with self.subTest(change=change):
                cache = dict(good); cache.update(change)
                self.cache_path.write_text(json.dumps(cache))
                policy = self.policy({"idle_seconds": 27})
                self.assertEqual(policy.snapshot()["status"], "fallback")
                self.assertEqual(policy.seconds, 27)
        for raw in ("PRIVATE_INVALID_JSON", "[]", "x" * 4097):
            self.cache_path.write_text(raw)
            self.assertEqual(self.policy({"idle_seconds": 27}).seconds, 27)

    def test_cache_write_error_does_not_discard_live_value(self):
        policy = self.policy()
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", return_value=0), \
                mock.patch.object(policy, "_save_cache", side_effect=OSError("PRIVATE_PATH")):
            self.assertEqual(policy.poll_once(), {"mode": "osaurus", "seconds": 0, "status": "live",
                                                   "error": "cache_write_failed", "setting": "idleUnloadSeconds"})
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", side_effect=idle_policy.IdlePolicyError("network_timeout")):
            self.assertEqual(policy.poll_once()["seconds"], 0)
            self.assertEqual(policy.snapshot()["status"], "cached")

    def test_latest_configuration_switches_between_follow_and_fixed(self):
        config = {"idle_source": "fixed", "idle_seconds": 8}
        policy = self.policy(config)
        config["idle_source"] = "osaurus"
        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", return_value=15) as read:
            self.assertEqual(policy.poll_once()["seconds"], 15)
            config["idle_source"] = "fixed"; config["idle_seconds"] = 0
            self.assertEqual(policy.poll_once()["status"], "fixed")
            self.assertEqual(policy.seconds, 0)
            self.assertEqual(read.call_count, 1)

    def test_invalid_configuration_or_getter_failure_is_safe(self):
        for config in ({"idle_source": "PRIVATE_INVALID"}, {"idle_seconds": True}, {"idle_seconds": -1},
                       {"osaurus_url": "https://PRIVATE_EXTERNAL.invalid", "idle_seconds": 14}):
            policy = self.policy(config)
            state = policy.poll_once()
            self.assertEqual(state["status"], "fallback")
            self.assertNotIn("PRIVATE", json.dumps(state))
        policy = self.policy({"idle_source": "fixed", "idle_seconds": 10})
        with mock.patch.object(policy, "_get_config", side_effect=OSError("PRIVATE_PATH")):
            self.assertEqual(policy.poll_once(), {"mode": "fixed", "seconds": 10, "status": "fixed",
                                                   "error": "configuration_unavailable", "setting": "idleUnloadSeconds"})

    def test_watcher_is_separate_fast_and_close_is_bounded(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_read(*_, **__):
            entered.set(); release.wait(timeout=2)
            return 15

        with mock.patch.object(idle_policy, "read_osaurus_idle_seconds", side_effect=slow_read):
            policy = idle_policy.IdlePolicy(self.config_path, lambda: {"idle_seconds": 300}, poll_seconds=0.01)
            self.addCleanup(policy.close)
            self.addCleanup(release.set)
            self.assertTrue(entered.wait(timeout=1))
            started = time.monotonic()
            self.assertEqual(policy.seconds, 300)
            self.assertEqual(policy.snapshot()["status"], "fallback")
            self.assertLess(time.monotonic() - started, 0.5)
            started = time.monotonic(); policy.close()
            self.assertLess(time.monotonic() - started, 1.6)
            release.set(); policy.close()
            self.assertFalse(policy._thread.is_alive())
            self.assertEqual(policy.poll_once(), policy.snapshot())


if __name__ == "__main__":
    unittest.main()
