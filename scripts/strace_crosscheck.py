#!/usr/bin/env python3
"""Independent cross-check of the harness's process accounting against the kernel's own execve trace.

The checks count "a program was started" from two in-process sources (a PEP 578 audit hook and the
tripwire scripts' own log). This script runs the same worker under `strace -f -C -e trace=execve` and
compares those counts with what the kernel reports, separately for the baseline and the hardened tree.

The headline number is strace's own syscall summary (calls minus errors for execve), which does not depend
on parsing individual trace lines. The trace lines are parsed as a second, diagnostic view (which programs).

  python scripts/strace_crosscheck.py baseline|hardened OUT.json
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r'^(?:\[pid\s+)?(?P<pid>\d+)\]?\s+execve\("(?P<path>(?:[^"\\]|\\.)*)",\s*\[(?P<argv>.*?)\],.*\)\s+=\s+(?P<ret>-?\d+|\?)')
UNFINISHED = re.compile(r'^(?:\[pid\s+)?(?P<pid>\d+)\]?\s+execve\("(?P<path>(?:[^"\\]|\\.)*)".*<unfinished \.\.\.>')
RESUMED = re.compile(r'^(?:\[pid\s+)?(?P<pid>\d+)\]?\s+<\.\.\. execve resumed>.*\)\s+=\s+(?P<ret>-?\d+|\?)')
SUMMARY = re.compile(r"^\s*[\d.]+\s+[\d.]+\s+\d+\s+(?P<calls>\d+)\s+(?P<errors>\d+)?\s*execve\s*$", re.M)
SHELL_NAMES = {"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish"}


def is_helper(path: str) -> bool:
    """Programs the harness itself starts: the worker's own interpreter and the ldconfig probe python runs."""
    name = os.path.basename(path)
    return name.startswith("python") or name in ("ldconfig", "ldconfig.real") or name == "pgrep"


def main(target: str, out_path: str) -> int:
    if not shutil.which("strace"):
        Path(out_path).write_text(json.dumps({"target": target, "skipped": "strace not installed"}) + "\n")
        return 0
    work = Path(tempfile.mkdtemp(prefix="strace-xcheck-"))
    try:
        trace, raw = work / "execve.trace", work / "worker.json"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        cmd = ["strace", "-f", "-qq", "-C", "-e", "trace=execve", "-s", "200", "-o", str(trace),
               sys.executable, "-m", "harness.worker", "--target", target, "--checks", "inject,pathexec",
               "--out", str(raw)]
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(f"strace/worker failed: {proc.stderr[-500:]}")
        worker = json.loads(raw.read_text("utf-8"))
        text = trace.read_text("utf-8", errors="replace")
        if os.environ.get("STRACE_KEEP_TRACE"):
            shutil.copy(trace, os.environ["STRACE_KEEP_TRACE"])

        # ---- view 1 (headline): strace's own syscall summary ------------------------------------------
        m = SUMMARY.search(text)
        if not m:
            raise RuntimeError("strace printed no execve summary row")
        calls_total, errors_total = int(m["calls"]), int(m["errors"] or 0)
        successful_total = calls_total - errors_total

        # ---- view 2 (diagnostic): which programs, from the individual trace lines ----------------------
        pending: dict[int, str] = {}
        successful_lines: list[tuple[int, str]] = []
        not_understood = 0
        for line in text.splitlines():
            mm = LINE.match(line)
            if mm:
                pid, path, ret = int(mm["pid"]), mm["path"], mm["ret"]
            else:
                u = UNFINISHED.match(line)
                if u:
                    pending[int(u["pid"])] = u["path"]
                    continue
                r = RESUMED.match(line)
                if r and int(r["pid"]) in pending:
                    pid, path, ret = int(r["pid"]), pending.pop(int(r["pid"])), r["ret"]
                elif "execve(" in line or "execve resumed" in line:
                    not_understood += 1
                    continue
                else:
                    continue
            if ret == "0":
                successful_lines.append((pid, path))
        helper_lines = [p for _pid, p in successful_lines if is_helper(p)]
        other = [p for _pid, p in successful_lines if not is_helper(p)]
        in_sandbox = [p for p in other if "agent-guard-" in p or not p.startswith("/")
                      or os.path.basename(p) == "evil-tripwire"]
        real_programs = [p for p in other if p not in in_sandbox]
        shell_execs = [p for p in real_programs if os.path.basename(p) in SHELL_NAMES]

        totals = worker["totals"]
        helper_execs = len(helper_lines)
        kernel_tripwire_execs = successful_total - helper_execs
        result = {
            "target": target, "checks": "inject,pathexec (full corpora, benign half included)",
            "kernel_execve_calls": calls_total, "kernel_execve_errors": errors_total,
            "kernel_successful_execs_total": successful_total,
            "harness_helper_execs_excluded": helper_execs,
            "kernel_successful_execs_of_sandbox_tripwires": kernel_tripwire_execs,
            "tripwire_log_records": totals["tripwire_log_records"],
            "audit_hook_allowed_tripwire_execs": totals["audit_exec_allowed_tripwire"],
            "audit_hook_vetoed_real_program_execs": totals["audit_exec_vetoed_real_programs"],
            "audit_hook_vetoed_process_calls": totals["audit_process_calls_vetoed"],
            "kernel_failed_execve_attempts": errors_total,
            # second view, from the individual lines
            "trace_lines_successful_execs_in_sandbox": len(in_sandbox),
            "trace_lines_not_understood": not_understood,
            "kernel_execs_of_real_programs_outside_sandbox": real_programs,
            "kernel_shell_executions": shell_execs,
            "violations_reported": {k: v["violations"] for k, v in worker["checks"].items()},
        }
        result["counts_agree"] = (kernel_tripwire_execs == result["tripwire_log_records"]
                                  == result["audit_hook_allowed_tripwire_execs"])
        # every successful kernel exec is either a harness helper or a sandbox tripwire: nothing else ran
        result["kernel_unexplained_successful_execs"] = successful_total - helper_execs - result["tripwire_log_records"]
        result["line_view_agrees"] = len(in_sandbox) == kernel_tripwire_execs and not not_understood
        Path(out_path).write_text(json.dumps(result, indent=1) + "\n", "utf-8")
        print(json.dumps(result, indent=1))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
