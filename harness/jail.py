"""Interpreter-level audit hook (PEP 578) used as a safety net and as an oracle.

While armed it (1) records every process launch, shell call, filesystem
write/list and browser hand-off the engine attempts, and (2) vetoes anything
that would touch the real machine: executing a program that is not one of our
tripwires, or writing outside the sandbox.  The attempt is recorded BEFORE the
veto, so a vulnerable engine is still caught red-handed even though its action
never runs.  A hook cannot be removed once installed, so the code under test
cannot switch it off.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_FS_EVENTS = {
    "os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.symlink", "os.link",
    "os.chmod", "os.truncate", "os.utime", "os.listdir", "os.scandir",
    "shutil.copyfile", "shutil.copytree", "shutil.rmtree", "shutil.move",
}
_PROCESS_EVENTS = {"os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "os.startfile"}


class Jail:
    def __init__(self, sandbox) -> None:
        self.sb = sandbox
        self.sb_root = os.path.realpath(sandbox.base)
        self.bin_root = os.path.realpath(sandbox.bin)
        self.home_root = os.path.realpath(sandbox.home)
        self.allow_exec: set[str] = {os.path.realpath(sandbox.evil_tripwire)}
        self.armed = False
        self.allow_system = False     # positive-control mode only: lets os.system really run (PATH holds only tripwires)
        self.events: list[dict] = []
        self._tl = threading.local()
        self._lock = threading.Lock()

    # ---- control ---------------------------------------------------------
    def install(self) -> None:
        sys.addaudithook(self._hook)

    def bypass(self):
        """Context manager: harness-owned operations are neither logged nor vetoed."""
        jail = self

        class _Bypass:
            def __enter__(self_inner):
                jail._tl.depth = getattr(jail._tl, "depth", 0) + 1

            def __exit__(self_inner, *exc):
                jail._tl.depth -= 1

        return _Bypass()

    def mark(self) -> int:
        return len(self.events)

    def since(self, mark: int) -> list[dict]:
        return self.events[mark:]

    def is_tripwire(self, resolved: str | None) -> bool:
        return resolved is not None and (
            self._inside(resolved, self.bin_root) or resolved == os.path.realpath(self.sb.evil_tripwire))

    # ---- helpers ---------------------------------------------------------
    def _inside(self, path: str, root: str) -> bool:
        return path == root or path.startswith(root + os.sep)

    def _real(self, p) -> str | None:
        if isinstance(p, int) or p is None:
            return None
        try:
            return os.path.realpath(os.fsdecode(p))
        except Exception:
            return None

    def _resolve_exec(self, exe: str, env, cwd) -> str | None:
        if "/" in exe:
            p = exe if os.path.isabs(exe) else os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), exe)
            return os.path.realpath(p) if os.path.exists(p) else None
        path_env = None
        if env:
            try:
                path_env = {os.fsdecode(k): os.fsdecode(v) for k, v in dict(env).items()}.get("PATH")
            except Exception:
                path_env = None
        found = shutil.which(exe, path=path_env if path_env is not None else os.environ.get("PATH", ""))
        return os.path.realpath(found) if found else None

    def _record(self, rec: dict) -> None:
        with self._lock:
            self.events.append(rec)

    # ---- the hook --------------------------------------------------------
    def _hook(self, event: str, args: tuple) -> None:
        if not self.armed or getattr(self._tl, "depth", 0):
            return
        if event == "subprocess.Popen":
            executable, argv, cwd, env = args
            exe = os.fsdecode(executable) if executable is not None else ""
            argv_s = [os.fsdecode(a) for a in (argv or [])]
            resolved = self._resolve_exec(exe, env, cwd) if exe else None
            allowed = resolved is not None and (
                self._inside(resolved, self.bin_root) or resolved in self.allow_exec)
            self._record({"k": "exec", "exe": exe, "argv": argv_s, "resolved": resolved, "allowed": allowed})
            if resolved is not None and not allowed:
                raise PermissionError(13, "harness jail: refusing to execute a real program", exe)
        elif event in _PROCESS_EVENTS:
            self._record({"k": "proc", "op": event, "args": [str(a)[:200] for a in args]})
            if not (self.allow_system and event == "os.system"):
                raise PermissionError(13, "harness jail: process creation vetoed", event)
        elif event == "webbrowser.open":
            self._record({"k": "url", "url": str(args[0])})
        elif event == "open":
            path, mode, flags = args
            if isinstance(flags, int) and flags & _WRITE_FLAGS:
                real = self._real(path)
                if real is not None:
                    self._fs("open", [real])
        elif event in _FS_EVENTS:
            paths = [self._real(a) for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))]
            paths = [p for p in paths if p is not None]
            if paths:
                self._fs(event, paths)
        elif event == "socket.connect":
            self._record({"k": "net", "addr": str(args[1])[:100]})
            raise PermissionError(13, "harness jail: network vetoed")

    _HARMLESS = {"/dev/null"}      # opened for writing by subprocess.DEVNULL on every launch

    def _fs(self, op: str, paths: list[str]) -> None:
        if all(p in self._HARMLESS for p in paths):
            return
        in_home = all(self._inside(p, self.home_root) for p in paths)
        in_sb = all(self._inside(p, self.sb_root) for p in paths)
        self._record({"k": "fs", "op": op, "paths": [p.replace(self.sb_root, "$SB") for p in paths],
                      "in_home": in_home, "in_sandbox": in_sb})
        if not in_sb:
            raise PermissionError(13, "harness jail: write/list outside the sandbox vetoed", paths[0])
