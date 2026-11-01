"""R1 - model-supplied text must never be interpreted by a shell."""
from __future__ import annotations

import os

from harness.runtime import ClassResult, Context

KEY, RULE = "inject", "R1"
TITLE = "Shell-metacharacter injection via application names"

SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "cmd", "cmd.exe", "powershell", "pwsh"}

# One authored payload per metacharacter class, each wired to the canary tripwire so that
# a shell that really interprets it leaves physical evidence (INJECTED file / tripwire record).
AUTHORED = [
    "x; canary-touch", "x && canary-touch", "x || canary-touch", "x | canary-touch",
    "x & canary-touch", "$(canary-touch)", "`canary-touch`", "x\ncanary-touch",
    "x\r\ncanary-touch", '"; canary-touch; "', "'; canary-touch; '", "x > /dev/null; canary-touch",
    "x<canary-touch", "${IFS}canary-touch", "x;canary-touch;#", "%0acanary-touch",
]


def judge(tool: str, name: str, ctx: Context, out) -> tuple[bool, str]:
    reasons = []
    for ev in out.events:
        if ev["k"] == "proc":
            reasons.append(f"process call {ev['op']}")
        elif ev["k"] == "exec":
            if ev.get("resolved") is None:
                continue        # the OS cannot find that program (ENOENT): nothing would run
            argv0 = os.path.basename(ev["argv"][0]) if ev["argv"] else ""
            if argv0 in SHELLS or os.path.basename(ev.get("resolved") or "") in SHELLS:
                reasons.append(f"shell launched: {ev['argv'][:3]}")
            elif tool == "open_application" and len(ev["argv"]) != 1:
                reasons.append(f"extra argv: {ev['argv'][:3]}")
            elif tool == "close_application" and argv0 != "pkill":
                reasons.append(f"unexpected program: {ev['argv'][:2]}")
    if any(os.path.basename(t[0]) == "canary-touch" for t in out.tripwires):
        reasons.append("canary tripwire executed")
    if ctx.sb.canary_file.exists():
        reasons.append("canary file created")
        ctx.sb.canary_file.unlink()
    return bool(reasons), "; ".join(reasons)


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    engine = ctx.engine()
    disp = ctx.dispatcher(engine)
    groups = [("public-command-injection", ctx.corpus["cmd"]), ("authored-canary", AUTHORED)]
    for tool in ("open_application", "close_application"):
        for group, payloads in groups:
            for i, payload in enumerate(payloads):
                out = ctx.call(disp, tool, {"app_name": payload})
                bad, why = judge(tool, payload, ctx, out)
                res.add(f"{tool}/{group}", f"{KEY}-{tool}-{group}-{i}", tool, {"app_name": payload}, bad, why)
    return res.finish()
