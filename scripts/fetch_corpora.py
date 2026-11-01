#!/usr/bin/env python3
"""Rebuild data/ from the public upstream repositories at pinned commits.

Only needed to audit or refresh the vendored data; the normal reproduce
command (see README) uses the files already committed under data/.

Every upstream is fetched with a partial clone (no blobs except those we
check out), at an exact commit SHA, and every produced file is hashed into
data/MANIFEST.json together with source, licence and retrieval time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

PATT_FILES = [
    "Command Injection/Intruder/command-execution-unix.txt",
    "Command Injection/Intruder/command_exec.txt",
    "Directory Traversal/Intruder/directory_traversal.txt",
    "Directory Traversal/Intruder/deep_traversal.txt",
    "Directory Traversal/Intruder/traversals-8-deep-exotic-encoding.txt",
    "File Inclusion/Intruders/Linux-files.txt",
    "File Inclusion/Intruders/Windows-files.txt",
    "Open Redirect/Intruder/Open-Redirect-payloads.txt",
    "Open Redirect/Intruder/openredirects.txt",
    "Open Redirect/Intruder/open_redirect_wordlist.txt",
]

SOURCES = {
    "payloads": {
        "repo": "https://github.com/swisskyrepo/PayloadsAllTheThings",
        "commit": "3ac27901c711bdf3f5b65a7b1d1820a1f65bd09a",
        "license": "MIT",
    },
    "cask": {
        "repo": "https://github.com/Homebrew/homebrew-cask",
        "commit": "31c8bdaf55f2af91fb7832592a548afcbba6696e",
        "license": "BSD-2-Clause",
    },
    "cpython": {
        "repo": "https://github.com/python/cpython",
        "commit": "d9825d504ad738a5160d3c3ecd8cd575a1713e98",
        "license": "PSF-2.0 (only file paths are used)",
    },
    "vscode": {
        "repo": "https://github.com/microsoft/vscode",
        "commit": None,  # resolved from the remote HEAD at retrieval time and recorded
        "license": "MIT (only file names are used)",
    },
}


def run(args, cwd=None, text=True):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=text).stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def clone_at(repo: str, commit: str | None, dest: Path, sparse: list[str] | None):
    dest.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=dest)
    run(["git", "remote", "add", "origin", repo], cwd=dest)
    if commit is None:
        commit = run(["git", "ls-remote", repo, "HEAD"]).split()[0]
    run(["git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit], cwd=dest)
    if sparse is not None:
        run(["git", "sparse-checkout", "init", "--no-cone"], cwd=dest)
        run(["git", "sparse-checkout", "set", *sparse], cwd=dest)
    run(["git", "-c", "advice.detachedHead=false", "checkout", "-q", commit], cwd=dest)
    return commit


def ls_tree_z(repo_dir: Path) -> list[str]:
    out = run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"], cwd=repo_dir, text=False)
    return [p.decode("utf-8", "strict") for p in out.split(b"\0") if p]


def main() -> int:
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {"retrieved_utc": retrieved, "sources": {}, "files": []}
    tmp = Path(tempfile.mkdtemp(prefix="corpora-"))
    try:
        # ---- adversarial payloads (MIT) ----------------------------------
        src = SOURCES["payloads"]
        d = tmp / "payloads"
        commit = clone_at(src["repo"], src["commit"], d, ["LICENSE", *PATT_FILES])
        out = DATA / "payloads"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        shutil.copyfile(d / "LICENSE", out / "LICENSE-PayloadsAllTheThings.txt")
        for rel in PATT_FILES:
            target = out / Path(rel).name
            shutil.copyfile(d / rel, target)
            manifest["files"].append({
                "path": str(target.relative_to(ROOT)), "source": f"{src['repo']}/blob/{commit}/{rel.replace(' ', '%20')}",
                "licence": src["license"], "sha256": sha256(target),
                "lines": len(target.read_text("utf-8", "replace").splitlines()), "role": "adversarial",
            })
        manifest["sources"]["payloads"] = {**src, "commit": commit}

        # ---- benign: real application tokens + homepages (BSD-2-Clause) ----
        src = SOURCES["cask"]
        d = tmp / "cask"
        commit = clone_at(src["repo"], src["commit"], d, ["LICENSE", "Casks/"])
        out = DATA / "benign"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        shutil.copyfile(d / "LICENSE", out / "LICENSE-homebrew-cask.txt")
        rows = []
        for dirpath, _dirs, files in os.walk(d / "Casks"):
            for fn in files:
                if not fn.endswith(".rb"):
                    continue
                text = (Path(dirpath) / fn).read_text("utf-8", "replace")
                m = re.search(r'^\s*homepage\s+"([^"]+)"', text, re.M)
                if m and "#{" not in m.group(1):
                    rows.append((fn[:-3], m.group(1)))
        rows = sorted(set(rows))
        target = out / "cask_apps.tsv"
        target.write_text("token\thomepage\n" + "".join(f"{t}\t{h}\n" for t, h in rows), "utf-8")
        manifest["files"].append({
            "path": str(target.relative_to(ROOT)),
            "source": f"{src['repo']}/tree/{commit}/Casks (derived: token = file name, homepage = first `homepage` string)",
            "licence": src["license"], "sha256": sha256(target), "lines": len(rows), "role": "benign",
        })
        manifest["sources"]["cask"] = {**src, "commit": commit}

        # ---- benign: real file paths (CPython) -----------------------------
        src = SOURCES["cpython"]
        d = tmp / "cpython"
        commit = clone_at(src["repo"], src["commit"], d, [".gitignore"])
        paths = ls_tree_z(d)
        target = out / "cpython_paths.txt"
        target.write_text("".join(p + "\n" for p in paths), "utf-8")
        manifest["files"].append({
            "path": str(target.relative_to(ROOT)), "source": f"{src['repo']}/tree/{commit} (derived: git ls-tree -r)",
            "licence": src["license"], "sha256": sha256(target), "lines": len(paths), "role": "benign",
        })
        manifest["sources"]["cpython"] = {**src, "commit": commit}

        # ---- benign: unusual real file names (VS Code tree) ----------------
        src = SOURCES["vscode"]
        d = tmp / "vscode"
        commit = clone_at(src["repo"], src["commit"], d, [".gitignore"])
        names = sorted({
            p.rsplit("/", 1)[-1] for p in ls_tree_z(d)
            if (not p.rsplit("/", 1)[-1].isascii()) or " " in p.rsplit("/", 1)[-1]
            or any(c in p.rsplit("/", 1)[-1] for c in "()'#&")
        })
        target = out / "vscode_unusual_names.txt"
        target.write_text("".join(n + "\n" for n in names), "utf-8")
        manifest["files"].append({
            "path": str(target.relative_to(ROOT)), "source": f"{src['repo']}/tree/{commit} (derived: unusual file names from git ls-tree -r)",
            "licence": src["license"], "sha256": sha256(target), "lines": len(names), "role": "benign",
        })
        manifest["sources"]["vscode"] = {**src, "commit": commit}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    (DATA / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"wrote {len(manifest['files'])} files; manifest at data/MANIFEST.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
