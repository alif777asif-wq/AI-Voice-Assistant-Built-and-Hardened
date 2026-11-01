"""Positive controls: the same oracles pointed at deliberately vulnerable reference code."""
from __future__ import annotations

from harness import vulnerable
from harness.checks import fsboundary, inject
from harness.runtime import ClassResult, Context, Outcome

KEY, RULE = "control", "C1"
TITLE = "Positive controls (the oracles must fire on known-bad code)"


def _observe(ctx: Context, fn, *args) -> Outcome:
    jmark, tmark = ctx.jail.mark(), ctx.sb.log_mark()
    try:
        text, kind = fn(*args), "result"
    except BaseException as exc:  # noqa: BLE001
        text, kind = f"{type(exc).__name__}: {exc}", "exception"
    return Outcome(kind, text, ctx.jail.since(jmark), ctx.sb.log_since(tmark))


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    # 1. shell injection oracle against an os.system launcher
    ctx.jail.allow_system = True
    canary_files = 0
    try:
        for i, payload in enumerate(inject.AUTHORED):
            out = _observe(ctx, vulnerable.vulnerable_open_application, payload)
            fired_file = ctx.sb.canary_file.exists()          # physical evidence, independent of the hook
            canary_files += fired_file
            bad, why = inject.judge("open_application", payload, ctx, out)   # also unlinks the canary
            res.add("shell-injection/os.system-launcher", f"control-inject-{i}", "vulnerable_open_application",
                    {"app_name": payload}, bad, why, keep_pass=True, canary_file_created=fired_file)
    finally:
        ctx.jail.allow_system = False
    res.extra["canary_files_created_by_shell_injection"] = canary_files
    # 2. path-boundary oracle against a naive create
    for i, payload in enumerate(fsboundary.authored(ctx)):
        ctx.sb.reset_home()
        before = ctx.sb.snapshot_outside()
        out = _observe(ctx, vulnerable.vulnerable_create, str(ctx.sb.home), payload, "attacker-controlled")
        bad, why = fsboundary.judge(ctx, out, before)
        res.add("path-traversal/naive-create", f"control-boundary-{i}", "vulnerable_create",
                {"relative": payload}, bad, why, keep_pass=True)
    ctx.sb.reset_home()
    return res.finish()
