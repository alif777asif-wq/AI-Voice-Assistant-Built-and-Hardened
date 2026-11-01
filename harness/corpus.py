"""Loads the vendored public corpora under data/ and verifies their checksums."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def verify_manifest() -> dict:
    """Re-hash every file listed in data/MANIFEST.json; raise on any mismatch."""
    manifest = json.loads((DATA / "MANIFEST.json").read_text("utf-8"))
    for entry in manifest["files"]:
        path = ROOT / entry["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            raise RuntimeError(f"checksum mismatch for {entry['path']}: {digest} != {entry['sha256']}")
    return manifest


def _lines(path: Path) -> list[str]:
    seen: dict[str, None] = {}
    for line in path.read_text("utf-8", errors="replace").splitlines():
        if line != "":
            seen.setdefault(line, None)
    return list(seen)


def payload_file(name: str) -> list[str]:
    return _lines(DATA / "payloads" / name)


def load() -> dict:
    """All corpora the checks use, as plain lists of strings."""
    deep = [p.replace("{FILE}", "etc/passwd") for p in payload_file("deep_traversal.txt")]
    corpus = {
        "cmd": list(dict.fromkeys(
            payload_file("command-execution-unix.txt") + payload_file("command_exec.txt"))),
        "traversal": list(dict.fromkeys(
            payload_file("directory_traversal.txt") + deep
            + payload_file("traversals-8-deep-exotic-encoding.txt"))),
        "sensitive_files": list(dict.fromkeys(
            payload_file("Linux-files.txt") + payload_file("Windows-files.txt"))),
        "redirect": list(dict.fromkeys(
            payload_file("Open-Redirect-payloads.txt") + payload_file("openredirects.txt")
            + payload_file("open_redirect_wordlist.txt"))),
    }
    rows = (DATA / "benign" / "cask_apps.tsv").read_text("utf-8").splitlines()[1:]
    corpus["apps"] = [tuple(r.split("\t", 1)) for r in rows]  # (token, homepage)
    corpus["paths"] = _lines(DATA / "benign" / "cpython_paths.txt")
    corpus["odd_names"] = _lines(DATA / "benign" / "vscode_unusual_names.txt")
    return corpus
