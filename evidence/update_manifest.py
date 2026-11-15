"""Refresh hashes of the intentionally public evidence files after a reviewed edit."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
files = [
    {"name": str(path.relative_to(root)).replace("\\", "/"),
     "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    for path in sorted(root.rglob("*"))
    if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts
]
(root / "manifest.json").write_text(json.dumps({"scope": "sanitized historical evidence and its publication helpers", "files": files}, indent=2) + "\n", encoding="utf-8")
print(f"Recorded {len(files)} public evidence files")
