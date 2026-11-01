"""R3 - closing an application must signal only processes of exactly that name."""
from __future__ import annotations

import os
import re
import subprocess
import shutil

from harness.runtime import ClassResult, Context
from harness.sandbox import spawn_sleeper

KEY, RULE = "overmatch", "R3"
TITLE = "close_application signals more processes than the one named"

_REGEX_OPERATORS = re.compile(r"[.+()\[\]*?^${}|\\]")

BYSTANDERS = ["lcanary_alpha", "lcanary_beta", "lcanary_gamma", "lcanary_delta"]

# Authored: pattern characters that are legal in a program name list but are regular-expression
# operators for pkill. (Shell operators such as | & ; are excluded: the engine already refuses them.)
AUTHORED = [
    "lcanary_alpha",            # control: an exact name, must signal exactly one process
    "lcanary_.*", ".*", ".+", "[a-z]*", "lcanary_[ab].*", "lcanary_a.pha", "lcanary_alph.",
    "^lcanary_alpha", "lcanary_(alpha)", "lc.*", "l.*", "(.*)", ".", "..", "a*", "lcanary_alpha.*",
]


def _procs_named(ctx: Context, name: str) -> set[int]:
    """Independent ground truth: processes whose kernel process name equals `name` exactly."""
    hits = set()
    with ctx.jail.bypass():
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    with open(f"/proc/{entry}/comm", "rb") as fh:
                        if fh.read().rstrip(b"\n").decode("utf-8", "replace") == name:
                            hits.add(int(entry))
                except OSError:
                    pass
    return hits


def would_signal(ctx: Context, pkill_argv: list[str]) -> tuple[set[int], int]:
    """Ask the real `pgrep` (same matcher as pkill, but read-only) who a pkill call would hit."""
    pgrep = shutil.which("pgrep", path=ctx.real_path)
    argv = [pgrep] + pkill_argv[1:]
    with ctx.jail.bypass():
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    pids = {int(x) for x in proc.stdout.split() if x.isdigit()}
    return pids, proc.returncode


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    engine = ctx.engine()
    disp = ctx.dispatcher(engine)
    with ctx.jail.bypass():
        procs = {name: spawn_sleeper(ctx.sb, name) for name in BYSTANDERS}
    try:
        bystander_pids = {p.pid for p in procs.values()}
        payloads = [("authored-pattern", AUTHORED), ("public-command-injection", ctx.corpus["cmd"])]
        for group, items in payloads:
            for i, name in enumerate(items):
                out = ctx.call(disp, "close_application", {"app_name": name})
                pk = [t for t in out.tripwires if os.path.basename(t[0]) == "pkill"]
                if not pk:      # refused before any process call: nothing could be signalled
                    res.add(group, f"{KEY}-{group}-{i}", "close_application", {"app_name": name}, False, "refused")
                    continue
                observed, rc = would_signal(ctx, pk[0])
                expected = _procs_named(ctx, name)
                extra = observed - expected
                hit = len(extra & bystander_pids)
                bad = bool(extra)
                res.add(group, f"{KEY}-{group}-{i}", "close_application", {"app_name": name}, bad,
                        f"pkill argv={pk[0][1:]} would signal {len(observed)} process(es), "
                        f"{hit}/{len(bystander_pids)} bystanders hit, {len(extra)} not named '{name}'" if bad else "",
                        keep_pass=(group == "authored-pattern"), would_signal_total=len(observed), bystanders_hit=hit)
        # benign: every 4th real application token that fits a process name (<= 15 chars), plus EVERY real
        # token that contains a regular-expression operator (pkill -x still treats those as a pattern)
        fits = [t for t, _ in ctx.corpus["apps"] if len(t) <= 15]
        tokens = list(dict.fromkeys(fits[::4] + [t for t in fits if _REGEX_OPERATORS.search(t)]))
        if ctx.skip_benign:
            tokens = []
        ok = 0
        for token in tokens:
            with ctx.jail.bypass():
                sleeper = spawn_sleeper(ctx.sb, token)
            try:
                out = ctx.call(disp, "close_application", {"app_name": token})
                pk = [t for t in out.tripwires if os.path.basename(t[0]) == "pkill"]
                if pk:
                    observed, _ = would_signal(ctx, pk[0])
                    if observed == _procs_named(ctx, token) and sleeper.pid in observed:
                        ok += 1
            finally:
                with ctx.jail.bypass():
                    sleeper.kill()
                    sleeper.wait()
        res.extra["benign"] = None if ctx.skip_benign else {"cases": len(tokens), "ok": ok, "legit_failed": len(tokens) - ok,
                               "source": "cask_apps.tsv tokens (<=15 chars): every 4th, plus every token containing a regex operator"}
    finally:
        with ctx.jail.bypass():
            for p in procs.values():
                p.kill()
                p.wait()
    return res.finish()
