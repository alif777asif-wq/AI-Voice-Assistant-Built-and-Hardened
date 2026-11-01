"""R5a - file operations stay inside allowed_roots, even after the user said yes."""
from __future__ import annotations

from harness.runtime import ClassResult, Context

KEY, RULE = "fsboundary", "R5a"
TITLE = "File operations escape the allowed root"


def authored(ctx: Context) -> list[str]:
    outside = str(ctx.sb.outside)
    return [
        "../outside/secret.txt", "../outside/victim.txt", "../outside/victim_dir", "../outside/",
        "docs/../../outside/secret.txt", "linkout/secret.txt", "linkout/victim.txt", "linkout/victim_dir/keep.txt",
        "linkout/new_file.txt", "~/../outside/secret.txt", "~/linkout/secret.txt",
        f"{outside}/secret.txt", f"{outside}/victim.txt", f"{outside}/victim_dir", f"{outside}/new_file.txt",
        "docs/../../../../../../../etc/passwd", "/etc/passwd", "/tmp/escape.txt", "~root/.bashrc",
    ]


def judge(ctx: Context, out, before) -> tuple[bool, str]:
    reasons = []
    outside_events = [e for e in out.events if e["k"] == "fs" and not e["in_home"]]
    if outside_events:
        e = outside_events[0]
        reasons.append(f"{len(outside_events)} fs call(s) outside the allowed root, first: {e['op']} {e['paths'][0][:60]}")
    if ctx.sb.snapshot_outside() != before:
        reasons.append("canary files outside the allowed root changed")
    return bool(reasons), "; ".join(reasons)


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    disp = ctx.dispatcher(ctx.engine())
    payload_groups = [
        ("authored-escape", authored(ctx)),
        ("public-traversal", ctx.corpus["traversal"]),
        ("public-sensitive-files", ctx.corpus["sensitive_files"]),
    ]
    templates = [
        ("create", lambda p: {"action": "create", "source": p, "content": "attacker-controlled"}),
        ("list", lambda p: {"action": "list", "source": p}),
        ("delete", lambda p: {"action": "delete", "source": p}),
        ("copy-to", lambda p: {"action": "copy", "source": "docs/report.txt", "destination": p}),
        ("copy-from", lambda p: {"action": "copy", "source": p, "destination": "docs/copied.txt"}),
        ("rename-to", lambda p: {"action": "rename", "source": "docs/notes.txt", "destination": p}),
        ("rename-from", lambda p: {"action": "rename", "source": p, "destination": "docs/renamed.txt"}),
    ]
    for group, payloads in payload_groups:
        for i, payload in enumerate(payloads):
            for tname, build in templates:
                ctx.sb.reset_home()
                before = ctx.sb.snapshot_outside()
                args = build(payload)
                out = ctx.call_confirmed(disp, "file_operation", args)     # worst case: user already said yes
                bad, why = judge(ctx, out, before)
                res.add(f"{group}/{tname}", f"{KEY}-{group}-{tname}-{i}", "file_operation", args, bad, why)
    ctx.sb.reset_home()
    res.extra["benign"] = None if ctx.skip_benign else benign(ctx, disp)
    return res.finish()


def benign(ctx: Context, disp) -> dict:
    """Real project file paths: create, list, copy, rename, delete must all still work in-root."""
    paths = ctx.corpus["paths"] + ctx.corpus["odd_names"]
    ok = 0
    failures = []
    for i, rel in enumerate(paths):
        home = ctx.sb.home
        target = home / "bench" / rel
        steps_ok = True
        why = ""
        try:
            o = ctx.call(disp, "file_operation", {"action": "create", "source": str(target), "content": f"data {i}"})
            steps_ok &= o.kind == "result" and target.read_text() == f"data {i}"
            o = ctx.call(disp, "file_operation", {"action": "list", "source": str(target.parent)})
            steps_ok &= o.kind == "result" and target.name in o.text
            copy_dst = home / "bench_copy" / rel
            o = ctx.call(disp, "file_operation", {"action": "copy", "source": str(target), "destination": str(copy_dst)})
            steps_ok &= o.kind == "result" and copy_dst.read_text() == f"data {i}"
            ren_dst = home / "bench_renamed" / rel
            args = {"action": "rename", "source": str(copy_dst), "destination": str(ren_dst)}
            o = ctx.call(disp, "file_operation", args)
            steps_ok &= o.kind == "confirmation"          # destructive: must ask first
            ren_dst.parent.mkdir(parents=True, exist_ok=True)
            o = ctx.call_confirmed(disp, "file_operation", args)
            steps_ok &= o.kind == "result" and ren_dst.read_text() == f"data {i}" and not copy_dst.exists()
            args = {"action": "delete", "source": str(ren_dst)}
            o = ctx.call(disp, "file_operation", args)
            steps_ok &= o.kind == "confirmation"
            o = ctx.call_confirmed(disp, "file_operation", args)
            steps_ok &= o.kind == "result" and not ren_dst.exists()
        except Exception as exc:  # noqa: BLE001
            steps_ok, why = False, f"{type(exc).__name__}: {exc}"
        if steps_ok:
            ok += 1
        elif len(failures) < 5:
            failures.append({"path": rel, "why": why})
    return {"cases": len(paths), "ok": ok, "legit_failed": len(paths) - ok, "sample_failures": failures,
            "source": "cpython_paths.txt + vscode_unusual_names.txt (create, list, copy, rename+confirm, delete+confirm)"}
