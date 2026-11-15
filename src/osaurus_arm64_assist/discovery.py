"""Ask Osaurus to refresh its provider list after advertised models change.

This independent watcher never runs under inference or catalog locks. Local
inventory comes exclusively from /admin/models; /v1/models is notification only.
"""
import json
import threading

from .idle_policy import discover_endpoint
from .osaurus_sync import read_json


class DiscoveryRefresh:
    def __init__(self, get_config, *, reader=read_json, interval=2.0, autostart=True):
        self.get_config, self.reader, self.interval = get_config, reader, interval
        self.closed, self.thread, self.signature = threading.Event(), None, None
        if autostart:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def poll_once(self):
        try:
            config = self.get_config()
            endpoint = discover_endpoint(config)
            advertised = [(p["id"], p.get("context", 2048), p["mode"])
                          for p in config.get("models", []) if p.get("enabled", True)]
            signature = (endpoint, json.dumps(sorted(advertised), sort_keys=True))
            if signature != self.signature:
                self.reader(endpoint, "/v1/models?refresh=1", timeout=2.0)
                self.signature = signature
        except Exception:
            # A failed notification leaves the signature pending for retry.
            # Inventory and generation remain available independently.
            return

    def _run(self):
        while not self.closed.wait(self.interval):
            self.poll_once()

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=3)
