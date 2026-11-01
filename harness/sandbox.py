"""A throw-away filesystem with tripwires, canaries and a fake user home.

Layout (all under one temporary directory):

  home/      the only directory the engine under test is allowed to touch
  outside/   canary files the engine must never read, change or create in
  bin/       the ONLY directory on PATH; every executable in it is a tripwire
  logs/      tripwire log (one record per execution, raw argv)
  share/     XDG_DATA_HOME, so "move to trash" lands inside the sandbox
  procs/     sacrificial long-running processes used by the process checks
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

RS, US = "\x1e", "\x1f"

TRIPWIRE_SH = """#!/bin/sh
# Harness tripwire: appends its own argv to $TRIPWIRE_LOG and does nothing else.
{ printf '\\036%s' "$0"; for a in "$@"; do printf '\\037%s' "$a"; done; } >> "$TRIPWIRE_LOG"
exit 0
"""

CANARY_TOUCH_SH = """#!/bin/sh
# Injection canary: only runs if a shell interpreted attacker-controlled text.
: > "$CANARY_FILE"
{ printf '\\036%s' "$0"; for a in "$@"; do printf '\\037%s' "$a"; done; } >> "$TRIPWIRE_LOG"
exit 0
"""

FIXED_TRIPWIRES = ("pkill", "sudo", "shutdown", "lbrowser", "launch", "xdg-open")


class Sandbox:
    def __init__(self, app_tokens=()):
        self.jail = None            # set by the worker; harness-owned file work then bypasses the hook
        self.base = Path(tempfile.mkdtemp(prefix="agent-guard-"))
        self.home = self.base / "home"
        self.outside = self.base / "outside"
        self.bin = self.base / "bin"
        self.logs = self.base / "logs"
        self.share = self.base / "share"
        self.procs = self.base / "procs"
        self.tmp = self.base / "tmp"
        for d in (self.home, self.outside, self.bin, self.logs, self.share, self.procs, self.tmp):
            d.mkdir(parents=True)
        self.tripwire_log = self.logs / "tripwire.log"
        self.tripwire_log.touch()
        self.canary_file = self.outside / "INJECTED"
        master = self.bin / ".tripwire"
        master.write_text(TRIPWIRE_SH)
        master.chmod(0o755)
        touch = self.bin / "canary-touch"
        touch.write_text(CANARY_TOUCH_SH)
        touch.chmod(0o755)
        for name in FIXED_TRIPWIRES:
            (self.bin / name).symlink_to(master.name)
        for token in app_tokens:
            link = self.bin / token
            if not link.exists() and not link.is_symlink():
                link.symlink_to(master.name)
        evil = self.outside / "evil-tripwire"
        evil.write_text(TRIPWIRE_SH)
        evil.chmod(0o755)
        self.evil_tripwire = evil
        self.reset_home()

    # ---- state -----------------------------------------------------------
    def _quiet(self):
        return self.jail.bypass() if self.jail is not None else contextlib.nullcontext()

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        """A vulnerable target may have deleted, renamed or replaced the directory with a file."""
        if path.is_symlink() or path.is_file():
            path.unlink()
        path.mkdir(exist_ok=True)

    def reset_home(self) -> None:
        with self._quiet():
            self._reset_home()

    def _reset_home(self) -> None:
        """Restore home/ and outside/ canaries to a known state (home/ itself is kept: it is the cwd)."""
        self._ensure_dir(self.home)         # a check may have removed it (that is a finding, not a harness bug)
        for child in list(self.home.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        os.chdir(self.home)
        (self.home / "docs").mkdir(parents=True)
        (self.home / "docs" / "report.txt").write_text("quarterly report\n")
        (self.home / "docs" / "notes.txt").write_text("meeting notes\n")
        (self.home / "Downloads").mkdir()
        (self.home / "Downloads" / "installer.bin").write_text("binary-ish\n")
        (self.home / ".bashrc").write_text("# user shell config\n")
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_ed25519").write_text("PRIVATE-KEY-CANARY\n")
        (self.home / ".aws").mkdir()
        (self.home / ".aws" / "credentials").write_text("AKIA-CANARY\n")
        link = self.home / "linkout"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(os.path.relpath(self.outside, self.home))
        # outside/ is rebuilt from scratch: a vulnerable target may have replaced any entry with anything
        self._ensure_dir(self.outside)
        for child in list(self.outside.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        self.evil_tripwire.write_text(TRIPWIRE_SH)
        self.evil_tripwire.chmod(0o755)
        for name, content in (("secret.txt", "CANARY-SECRET\n"), ("victim.txt", "CANARY-VICTIM\n")):
            (self.outside / name).write_text(content)
        victim_dir = self.outside / "victim_dir"
        victim_dir.mkdir()
        (victim_dir / "keep.txt").write_text("CANARY-DIR\n")
        trash = self.share / "Trash"
        shutil.rmtree(trash, ignore_errors=True)

    def snapshot_outside(self) -> dict:
        snap = {}
        with self._quiet():
            for path in sorted(self.outside.rglob("*")):
                rel = str(path.relative_to(self.outside))
                if path.is_symlink() or path.is_dir():
                    snap[rel] = ("dir" if path.is_dir() else "link",)
                else:
                    snap[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(),)
        return snap

    def trash_files(self) -> list[str]:
        d = self.share / "Trash" / "files"
        with self._quiet():
            return sorted(p.name for p in d.iterdir()) if d.exists() else []

    # ---- tripwire log ----------------------------------------------------
    def log_mark(self) -> int:
        return self.tripwire_log.stat().st_size

    def log_since(self, mark: int) -> list[list[str]]:
        data = self.tripwire_log.read_bytes()[mark:].decode("utf-8", "replace")
        records = []
        for rec in data.split(RS):
            if rec:
                records.append(rec.split(US))
        return records  # each: [argv0, arg1, ...]

    # ---- environment for the process that runs the engine ---------------
    def env(self) -> dict:
        return {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "BROWSER": f"{self.bin / 'lbrowser'} %s",
            "XDG_DATA_HOME": str(self.share),
            "XDG_CONFIG_HOME": str(self.base / "config"),
            "TMPDIR": str(self.tmp),
            "TRIPWIRE_LOG": str(self.tripwire_log),
            "CANARY_FILE": str(self.canary_file),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def spawn_sleeper(sb: Sandbox, name: str) -> subprocess.Popen:
    """A real, harmless process whose process name (comm) is exactly `name`."""
    link = sb.procs / name
    if not link.exists():
        link.symlink_to(shutil.which("sleep", path="/usr/bin:/bin"))
    return subprocess.Popen([str(link), "600"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
