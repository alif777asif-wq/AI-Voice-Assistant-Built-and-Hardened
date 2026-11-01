"""R4 - open_application launches a bare program name, never a path or an option."""
from __future__ import annotations

import os

from harness.runtime import ClassResult, Context

KEY, RULE = "pathexec", "R4"
TITLE = "open_application executes explicit paths and options"


def authored(ctx: Context) -> list[str]:
    evil = str(ctx.sb.evil_tripwire)
    return [
        evil,                                   # absolute path to an executable OUTSIDE the allowed root
        "../outside/evil-tripwire",             # relative traversal to the same file
        "./../outside/evil-tripwire",
        "~/../outside/evil-tripwire",
        "/bin/sh", "/usr/bin/env", "./run.sh", "~/.local/bin/tool", "-h", "--help",
        "--gpu-launcher=x", "..", ".", "C:\\Windows\\System32\\cmd.exe", "\\\\attacker\\share\\x.exe",
        "http://example.com/x.exe", "file:///etc/passwd", "ms-msdt:/id", "/etc/passwd",
    ]


def judge(out, ctx) -> tuple[bool, str]:
    reasons = []
    for ev in out.events:
        if ev["k"] == "exec" and ev["argv"]:
            a0 = ev["argv"][0]
            if "/" in a0 or "\\" in a0 or a0.startswith("-") or a0 in (".", ".."):
                reasons.append(f"exec attempt of non-bare name {a0[:60]!r}")
    for t in out.tripwires:
        if os.path.realpath(t[0]) == os.path.realpath(ctx.sb.evil_tripwire):
            reasons.append("program outside the allowed root really executed")
    return bool(reasons), "; ".join(reasons)


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    disp = ctx.dispatcher(ctx.engine())
    groups = [
        ("authored-path-and-option", authored(ctx)),
        ("public-sensitive-file-paths", ctx.corpus["sensitive_files"]),
        ("public-traversal-paths", ctx.corpus["traversal"]),
    ]
    for group, payloads in groups:
        for i, payload in enumerate(payloads):
            out = ctx.call(disp, "open_application", {"app_name": payload})
            bad, why = judge(out, ctx)
            res.add(group, f"{KEY}-{group}-{i}", "open_application", {"app_name": payload}, bad, why)
    # benign: every real application token must still launch (the tripwire of that name must run)
    ok = 0
    failures = []
    apps = [] if ctx.skip_benign else ctx.corpus["apps"]
    for token, _home in apps:
        out = ctx.call(disp, "open_application", {"app_name": token})
        if any(os.path.basename(t[0]) == token for t in out.tripwires) and out.kind == "result":
            ok += 1
        elif len(failures) < 10:
            failures.append({"token": token, "kind": out.kind, "text": out.text[:120],
                             "exec_events": [{"argv": e["argv"][:2], "resolved": bool(e["resolved"]), "allowed": e["allowed"]}
                                             for e in out.events if e["k"] == "exec"],
                             "tripwire_records": len(out.tripwires)})
    res.extra["benign"] = None if ctx.skip_benign else {"cases": len(apps), "ok": ok, "legit_failed": len(apps) - ok,
                           "sample_failures": failures, "source": "cask_apps.tsv tokens (all)"}
    return res.finish()
