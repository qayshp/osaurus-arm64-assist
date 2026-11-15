"""Validate the public historical projection without loading native libraries."""
import csv
import hashlib
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parent
project = root.parent
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for entry in manifest["files"]:
    path = (root / entry["name"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Evidence manifest path escapes its directory")
    data = path.read_bytes()
    assert len(data) == entry["bytes"], f"Size changed: {entry['name']}"
    assert hashlib.sha256(data).hexdigest() == entry["sha256"], f"Hash changed: {entry['name']}"

for directory in [root, project / "docs"]:
    for path in directory.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in {".gguf", ".dll", ".exe", ".zip", ".so", ".cat", ".inf"}:
            raise ValueError(f"Unexpected binary/model asset: {path.name}")
        text = path.read_text(encoding="utf-8-sig")
        # These patterns concern private host paths and access tokens, not public GitHub identities.
        assert not re.search(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+", text), f"User path in {path.name}"
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", text), f"Token-shaped string in {path.name}"
        if path.suffix == ".json":
            json.loads(text)

with (root / "stage-timings.csv").open(encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream))
assert len(rows) == 127
assert {"model", "mode", "workload", "input_tokens", "output_tokens", "response_wall_seconds"}.issubset(rows[0])
for row in rows:
    for key, value in row.items():
        if value and (key.endswith("_seconds") or key in {"input_tokens", "output_tokens", "state_bytes"}):
            assert float(value) >= 0, f"Negative counter: {key}"
print(f"Validated {len(manifest['files'])} public files and {len(rows)} timing rows; no native execution was performed")
