"""Maintainer helper: rebuild dependency hashes and pinned native ABI manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {"Jinja2": "3.1.6", "MarkupSafe": "3.0.4", "fastjsonschema": "2.22.3",
            "setuptools": "84.2.1", "wheel": "0.48.0", "packaging": "26.3"}
ARCHIVE_SHA256 = "d22f41ccc3d2ce76998f412f79d57e460eb3668192a42bd872ae1611da688443"
CORE = "93cf736a8d1d46aa922e27eb64aa712bcd5ebc30"


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--runtime-archive", type=Path, required=True)
    args = parser.parse_args()
    with args.runtime_archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise SystemExit("Runtime archive does not match the official pinned release")
    lines = ["# Exact wheel hashes from PyPI JSON. Install with --require-hashes --only-binary=:all:."]
    for name, version in VERSIONS.items():
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            metadata = json.load(response)
        hashes = sorted({x["digests"]["sha256"] for x in metadata["urls"] if x["filename"].endswith(".whl")})
        lines.append(name + "==" + version + " \\")
        lines.extend("    --hash=sha256:" + value + (" \\" if index < len(hashes)-1 else "") for index,value in enumerate(hashes))
    (ROOT/"requirements.lock").write_text("\n".join(lines)+"\n", encoding="utf-8")
    with zipfile.ZipFile(args.runtime_archive) as archive:
        prefix = "geniex-bench-windows-arm64-v0.11.0/lib/llama_cpp/"
        files = {name[len(prefix):]: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()
                 if name.startswith(prefix) and not name.endswith("/")}
    manifest = {"geniex_version": "0.11.0", "llama_cpp_commit": CORE,
                "archive_sha256": ARCHIVE_SHA256, "files": files}
    (ROOT/"src/osaurus_arm64_assist/native.lock.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    print("Wrote exact Python wheel hashes and", len(files), "native file hashes")


if __name__ == "__main__":
    main()
