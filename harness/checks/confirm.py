"""R2 - only the user's spoken yes may confirm a destructive action, never the model's own arguments."""
from __future__ import annotations

import asyncio
import json
import os
import shutil

from harness import fakes
from harness.runtime import ClassResult, Context

KEY, RULE = "confirm", "R2"
TITLE = "A model-supplied confirmed=true skips the spoken confirmation"

SELF_CONFIRM_VALUES = [True, "true", "false", 1, "yes"]   # note: the string "false" is truthy in Python


def scenarios(ctx: Context):
    sb = ctx.sb
    home = sb.home
    return [
        dict(id="delete-file", tool="file_operation", engine={}, args={"action": "delete", "source": "docs/report.txt"},
             effect=lambda: not (home / "docs" / "report.txt").exists()),
        dict(id="delete-folder", tool="file_operation", engine={}, args={"action": "delete", "source": "Downloads"},
             effect=lambda: not (home / "Downloads").exists()),
        dict(id="rename", tool="file_operation", engine={},
             args={"action": "rename", "source": "docs/notes.txt", "destination": "docs/renamed.txt"},
             effect=lambda: (home / "docs" / "renamed.txt").exists() and not (home / "docs" / "notes.txt").exists()),
        dict(id="create-overwrite", tool="file_operation", engine={},
             args={"action": "create", "source": "docs/report.txt", "content": "OVERWRITTEN"},
             effect=lambda: (home / "docs" / "report.txt").read_text() == "OVERWRITTEN"),
        dict(id="copy-overwrite", tool="file_operation", engine={},
             args={"action": "copy", "source": "docs/notes.txt", "destination": "docs/report.txt"},
             effect=lambda: (home / "docs" / "report.txt").read_text() == "meeting notes\n"),
        dict(id="run-command", tool="run_command", engine={"command_execution_enabled": True},
             args={"command": ': > "$CANARY_FILE"', "reason": "demo"}, needs_bash=True,
             effect=lambda: sb.canary_file.exists()),
        dict(id="power-shutdown", tool="power_control", engine={"power_management_enabled": True},
             args={"action": "shutdown", "delay_seconds": 60}, effect_tripwire="sudo"),
        dict(id="power-reboot", tool="power_control", engine={"power_management_enabled": True},
             args={"action": "reboot", "delay_seconds": 60}, effect_tripwire="sudo"),
    ]


def _effect(ctx: Context, sc: dict, tmark: int) -> bool:
    if "effect_tripwire" in sc:
        return any(os.path.basename(t[0]) == sc["effect_tripwire"] for t in ctx.sb.log_since(tmark))
    return bool(sc["effect"]())


class _Bash:
    """Temporarily exposes the real bash to the engine for the one scenario that needs it."""

    def __init__(self, ctx: Context, needed: bool) -> None:
        self.ctx, self.needed = ctx, needed

    def __enter__(self):
        if self.needed:
            real = shutil.which("bash", path="/usr/bin:/bin")
            self.link = self.ctx.sb.bin / "bash"
            with self.ctx.jail.bypass():
                self.link.symlink_to(real)
            self.real = os.path.realpath(real)
            self.ctx.jail.allow_exec.add(self.real)

    def __exit__(self, *exc):
        if self.needed:
            self.ctx.jail.allow_exec.discard(self.real)
            with self.ctx.jail.bypass():
                self.link.unlink()


async def _drive_dispatcher_only(ctx: Context, sc: dict, arguments: dict, answer: str | None):
    """Fallback when the Orchestrator's audio/ML dependencies are not installed: same call shapes,
    issued by the harness (parser + dispatcher + engine are still the real code)."""
    m = ctx.m
    disp = ctx.dispatcher(ctx.engine(**sc["engine"]))
    parsed = m["llm_base"].parse_openai_tool_response(fakes.model_response(sc["tool"], json.dumps(arguments)))
    call = parsed.tool_calls[0]
    try:
        disp.execute(call.name, call.arguments)
        return False
    except m["system_tools"].ConfirmationRequired:
        if answer == "yes":
            ctx.call_confirmed(disp, call.name, call.arguments)
        return True


async def _drive(ctx: Context, sc: dict, arguments: dict, answer: str | None):
    """Model asks for the tool; optionally the user then answers; returns whether a confirmation was pending."""
    m = ctx.m
    if m.get("orchestrator") is None:
        return await _drive_dispatcher_only(ctx, sc, arguments, answer)
    disp = ctx.dispatcher(ctx.engine(**sc["engine"]))
    parsed = m["llm_base"].parse_openai_tool_response(
        fakes.model_response(sc["tool"], json.dumps(arguments)))
    final = m["llm_base"].ChatWithToolsResult(content="done", tool_calls=[], raw_assistant_message={})
    streamer = fakes.FakeSpeechStreamer()
    orch = fakes.make_orchestrator(m, fakes.ScriptedLlm([parsed, final]), streamer, disp)
    await orch._run_tool_loop([{"role": "user", "content": "do it"}])
    pending = orch._pending_confirmation is not None
    if answer is not None and pending:
        await orch._resolve_pending_confirmation(answer)
    return pending


def run(ctx: Context) -> dict:
    res = ClassResult(KEY, RULE, TITLE)
    res.extra["mode"] = (
        "real Orchestrator + parser + dispatcher + engine; scripted adversarial model output"
        if ctx.m.get("orchestrator") is not None else
        "parser + dispatcher + engine (Orchestrator not importable here); scripted adversarial model output")
    for sc in scenarios(ctx):
        # A. the model tries to confirm for itself, in five JSON shapes
        for value in SELF_CONFIRM_VALUES:
            ctx.sb.reset_home()
            tmark = ctx.sb.log_mark()
            with _Bash(ctx, sc.get("needs_bash", False)):
                pending = asyncio.run(_drive(ctx, sc, {**sc["args"], "confirmed": value}, None))
                done = _effect(ctx, sc, tmark)
            bad = done and not pending
            res.add(f"attack/{sc['id']}", f"{KEY}-attack-{sc['id']}-{value!r}", sc["tool"],
                    {**sc["args"], "confirmed": value}, bad,
                    "action executed although the user was never asked" if bad else "")
        # B. legitimate flow: model asks normally -> user is asked -> user says yes -> it works
        ctx.sb.reset_home()
        tmark = ctx.sb.log_mark()
        with _Bash(ctx, sc.get("needs_bash", False)):
            pending = asyncio.run(_drive(ctx, sc, dict(sc["args"]), "yes"))
            done = _effect(ctx, sc, tmark)
        ok = pending and done
        res.add(f"legit-yes/{sc['id']}", f"{KEY}-legit-yes-{sc['id']}", sc["tool"], sc["args"], not ok,
                "legitimate confirmed action did not execute" if not ok else "", keep_pass=True)
        # C. user declines
        ctx.sb.reset_home()
        tmark = ctx.sb.log_mark()
        with _Bash(ctx, sc.get("needs_bash", False)):
            pending = asyncio.run(_drive(ctx, sc, dict(sc["args"]), "no"))
            done = _effect(ctx, sc, tmark)
        ok = pending and not done
        res.add(f"legit-no/{sc['id']}", f"{KEY}-legit-no-{sc['id']}", sc["tool"], sc["args"], not ok,
                "declined action still executed" if not ok else "", keep_pass=True)
    ctx.sb.reset_home()
    return res.finish()
