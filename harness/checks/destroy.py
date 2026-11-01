"""R5c/R5d - no action may remove the allowed root itself or silently destroy an existing file."""
from __future__ import annotations

from harness.runtime import ClassResult, Context

KEY, RULE = "destroy", "R5c"
TITLE = "Root deletion and silent overwrite on rename"


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    disp = ctx.dispatcher(ctx.engine())
    home = ctx.sb.home

    # R5c: the allowed root itself (user already confirmed - worst case)
    for label, source in (("root-by-tilde", "~"), ("root-by-dot", "."), ("root-by-path", str(home))):
        ctx.sb.reset_home()
        out = ctx.call_confirmed(disp, "file_operation", {"action": "delete", "source": source})
        gone = not home.exists() or not (home / "docs").exists()
        res.add("delete-root", f"{KEY}-{label}", "file_operation", {"action": "delete", "source": source}, gone,
                "the whole allowed root was moved to the trash" if gone else "", keep_pass=True)
    for label, dest in (("rename-root-away", str(ctx.sb.base / "home_moved")),):
        ctx.sb.reset_home()
        out = ctx.call_confirmed(disp, "file_operation", {"action": "rename", "source": str(home), "destination": dest})
        gone = not (home / "docs").exists()
        res.add("rename-root", f"{KEY}-{label}", "file_operation", {"action": "rename", "source": str(home)}, gone,
                "the allowed root itself was renamed away" if gone else "", keep_pass=True)
        import shutil
        shutil.rmtree(dest, ignore_errors=True)

    # R5d: rename onto an existing file/folder
    for label, kind in (("file-over-file", "file"), ("dir-over-empty-dir", "dir")):
        ctx.sb.reset_home()
        if kind == "file":
            src, dst = home / "docs" / "notes.txt", home / "docs" / "report.txt"
            old = "quarterly report\n"
        else:
            src, dst = home / "Downloads", home / "emptydir"
            dst.mkdir()
            old = None
        args = {"action": "rename", "source": str(src), "destination": str(dst)}
        asked = ctx.call(disp, "file_operation", args)                    # what is the user told?
        warns = asked.kind == "confirmation" and any(w in asked.text.lower() for w in ("already exists", "replace", "overwrit"))
        ctx.call_confirmed(disp, "file_operation", args)                  # then the user says yes
        recoverable = bool(ctx.sb.trash_files()) if old is None else any(
            (ctx.sb.share / "Trash" / "files" / n).is_file()
            and (ctx.sb.share / "Trash" / "files" / n).read_text() == old for n in ctx.sb.trash_files())
        if kind == "dir":
            recoverable = bool(ctx.sb.trash_files())
        bad = not (warns and recoverable)
        res.add("rename-over-existing", f"{KEY}-{label}", "file_operation", args, bad,
                f"prompt mentions overwrite: {warns}; replaced item recoverable from trash: {recoverable}" if bad else "",
                keep_pass=True, prompt_warns=warns, recoverable=recoverable)
    ctx.sb.reset_home()
    return res.finish()
