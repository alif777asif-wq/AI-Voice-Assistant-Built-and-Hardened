#!/usr/bin/env python3
"""Live demonstration (needs root + setpriv): ONE close_application(".*") call with the REAL pkill.

Everything runs as a throw-away unprivileged uid so nothing else on the machine can be affected.
The harness's pgrep-based oracle predicts who a pkill call would hit; this script checks the prediction
against reality by letting the call run unmodified and counting which processes are actually gone.

  python scripts/live_pkill_demo.py baseline|hardened   -> prints one JSON object
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UID = 59001


def drop(cmd: list[str]) -> list[str]:
    return ["setpriv", f"--reuid={UID}", f"--regid={UID}", "--clear-groups", "--no-new-privs", "--", *cmd]


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] not in ("Z", "X")
    except OSError:
        return False


def main(target: str) -> int:
    if os.geteuid() != 0 or not shutil.which("setpriv"):
        print(json.dumps({"target": target, "skipped": "needs root and setpriv (throw-away uid isolation)"}))
        return 0
    # Everything the throw-away uid must reach lives in a world-readable folder under /tmp: the repository itself may
    # sit in a private directory (for example a 0700 home folder) that this uid cannot enter.
    work = Path(tempfile.mkdtemp(prefix="pkill-demo-", dir="/tmp" if os.path.isdir("/tmp") else None))
    work.chmod(0o755)
    staged = work / "code"
    shutil.copytree(ROOT / target / "src", staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for dirpath, dirnames, filenames in os.walk(staged):
        os.chmod(dirpath, 0o755)
        for fn in filenames:
            os.chmod(os.path.join(dirpath, fn), 0o644)
    interpreter = os.path.realpath(sys.executable)      # resolves a venv's symlink to the interpreter itself
    names = ["lcanary_alpha", "lcanary_beta", "lcanary_gamma", "lcanary_delta"]
    sleep = shutil.which("sleep", path="/usr/bin:/bin")
    procs = {}
    for n in names:
        (work / n).symlink_to(sleep)
        procs[n] = subprocess.Popen(drop([str(work / n), "600"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    src = staged
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from l_assistant.tools.system_tools import SystemToolsEngine;"
            "from l_assistant.tools.registry import ToolDispatcher;"
            "print(ToolDispatcher(SystemToolsEngine()).execute('close_application', {'app_name': sys.argv[2]}), flush=True)")
    out = {"target": target, "uid": UID, "calls": []}
    try:
        probe = subprocess.run(drop([interpreter, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                                     "import l_assistant.tools.registry; print('importable')", str(src)]),
                               capture_output=True, text=True, timeout=60)
        if probe.stdout.strip() != "importable":
            print(json.dumps({"target": target, "skipped": "the throw-away uid cannot run the interpreter or import "
                              "the audited code: " + (probe.stderr.strip()[-200:] or "no output")}))
            return 0
        for name in (".*", "lcanary_alpha"):
            for n in names:                      # make sure every bystander is alive before each call
                if not alive(procs[n].pid):
                    procs[n] = subprocess.Popen(drop([str(work / n), "600"]), stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
            time.sleep(0.3)
            before = {n: alive(p.pid) for n, p in procs.items()}
            caller = subprocess.run(drop([interpreter, "-I", "-B", "-c", code, str(src), name]),
                                    capture_output=True, text=True, timeout=60)
            time.sleep(0.6)
            after = {n: alive(p.pid) for n, p in procs.items()}
            out["calls"].append({
                "app_name": name, "tool_result": caller.stdout.strip()[:200],
                "caller_exit_code": caller.returncode,
                "caller_killed_by_signal": caller.returncode < 0 and -caller.returncode,
                "bystanders_alive_before": sum(before.values()), "bystanders_alive_after": sum(after.values()),
                "killed": [n for n in names if before[n] and not after[n]],
            })
    finally:
        for p in procs.values():
            p.kill()
            p.wait()
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
