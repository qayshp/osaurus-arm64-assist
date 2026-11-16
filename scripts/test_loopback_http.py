"""Disposable target/proxy fixture; contains no real API credentials."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

parser = argparse.ArgumentParser()
parser.add_argument("--ready", type=Path, required=True)
args = parser.parse_args()
state = {"proxy_hits": 0}


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Proxy(QuietHandler):
    def do_GET(self):
        state["proxy_hits"] += 1
        self.reply({"proxy": True})

    do_POST = do_GET
    do_PUT = do_GET


class Target(QuietHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{proxy.server_port}/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/stats":
            self.reply(state)
        else:
            self.reply({"service": "fixture", "direct": True})

    def do_POST(self):
        self.reply({"direct": True, "authorized": self.headers.get("Authorization") == "Bearer loopback-test-key"})

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/redirect":
            self.do_GET()
            return
        data = json.loads(body.decode("utf-8"))
        self.reply({"direct": True, "authorized": self.headers.get("Authorization") == "Bearer loopback-test-key",
                    "content_type": self.headers.get("Content-Type"), "body": data})


proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
for server in (proxy, target):
    threading.Thread(target=server.serve_forever, daemon=True).start()
temporary = args.ready.with_suffix(".tmp")
temporary.write_text(json.dumps({"proxy_port": proxy.server_port, "target_port": target.server_port}), encoding="utf-8")
temporary.replace(args.ready)
threading.Event().wait(60)
