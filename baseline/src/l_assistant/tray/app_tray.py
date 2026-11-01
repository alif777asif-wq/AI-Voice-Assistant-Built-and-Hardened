"""
System tray integration + single-instance lock, for running L as a
background process instead of a foreground terminal app.

pystray's event loop (`icon.run()`) needs to own the thread it's called
from on most backends (a GTK/Win32/Cocoa constraint, not an L one),
while L's actual voice pipeline is an asyncio event loop. `run_in_tray`
resolves this the same way the source blueprint's threaded design did:
the asyncio pipeline runs on a background thread (its own event loop, via
`asyncio.run`), and pystray's `icon.run()` blocks the calling (main)
thread until "Exit" is chosen or the process is killed.

pystray itself supports Linux and macOS (via AppIndicator3/GTK or Cocoa)
as well as Windows -- this module doesn't hard-code a Windows-only
assumption, even though the packaging in `packaging/` targets Windows
specifically to match this feature's origin spec. On a headless Linux
box with no display server, constructing `pystray.Icon` will fail; that
failure surfaces as a normal exception from `run_in_tray`, not a silent
no-op.

`pystray`, `PIL`, and `tendo` are imported lazily inside the functions
that need them rather than at module load time, so `import l_assistant.tray`
(and therefore `import l_assistant.main`) doesn't require any of them to be
installed unless `tray.enabled: true` is actually used.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """Raised when another instance of L already holds the single-instance lock."""


class SingleInstanceGuard:
    """
    Thin wrapper around `tendo.singleton.SingleInstance` so the rest of
    the codebase depends on one small interface instead of importing
    `tendo` directly. Must be kept alive (assign it to a variable, don't
    let it get garbage-collected) for as long as the lock should be held
    -- its lifetime *is* the lock.
    """

    def __init__(self, lockfile_name: str = "l_voice_assistant") -> None:
        from tendo import singleton

        try:
            self._lock = singleton.SingleInstance(flavor_id=lockfile_name)
        except singleton.SingleInstanceException as exc:
            raise AlreadyRunningError(
                "Another instance of L is already running."
            ) from exc


def _create_icon_image(color_hex: str):
    from l_assistant.tray.icon_art import draw_icon

    return draw_icon(64, accent_hex=color_hex)


def run_in_tray(
    app_name: str,
    icon_color: str,
    build_and_run_orchestrator: Callable[[], "asyncio.Future"],
    request_pause: Callable[[], None],
    request_resume: Callable[[], None],
) -> None:
    """
    Runs L's voice pipeline on a background thread and blocks the
    calling thread on the tray icon until "Exit" is chosen.

    `build_and_run_orchestrator`: a zero-argument callable returning the
    orchestrator's `run()` coroutine. Called on the background thread
    (not here), since the orchestrator and everything it owns (the mic
    stream, the asyncio.Event used for pause/resume) must be constructed
    on the same thread that will run their event loop.

    `request_pause` / `request_resume`: thread-safe callables (see
    `Orchestrator.request_pause`/`request_resume`) wired to the tray's
    Pause/Resume menu item. Unlike the source blueprint's version of this
    menu item -- which was a non-functional `lambda: print("Toggled")` --
    these genuinely pause and resume wake-word listening, via
    `asyncio.Event` + `call_soon_threadsafe` so it's safe to call from the
    tray icon's own thread.

    Shutdown: choosing "Exit" stops the tray icon and returns from this
    function; main.py exits the process immediately after. The pipeline
    thread is a daemon thread, so it's torn down by the OS along with the
    process rather than being cleanly cancelled first -- for a
    voice-command turn in progress at that exact moment, this means "the
    process ends", not "the turn finishes first". Acceptable for an Exit
    action; documented rather than silently glossed over.
    """
    import pystray
    from pystray import MenuItem as Item

    from l_assistant import autostart

    paused = threading.Event()  # local UI state only, mirrors the orchestrator's

    def _on_toggle_pause(icon, item) -> None:
        if paused.is_set():
            paused.clear()
            request_resume()
        else:
            paused.set()
            request_pause()
        icon.update_menu()

    def _pause_item_text(item) -> str:
        return "Resume" if paused.is_set() else "Pause"

    def _autostart_enabled(item) -> bool:
        # Checked lazily, every time the menu is opened, rather than
        # cached at startup -- reflects reality even if the registry
        # value was changed some other way (a fresh install running
        # enable() again, manual regedit, etc.) between menu opens.
        try:
            return autostart.is_available() and autostart.is_enabled()
        except Exception:
            logger.exception("Couldn't read autostart state")
            return False

    def _on_toggle_autostart(icon, item) -> None:
        if not autostart.is_available():
            return
        try:
            new_state = autostart.toggle()
            logger.info("Autostart %s", "enabled" if new_state else "disabled")
        except Exception:
            logger.exception("Couldn't change autostart setting")
            try:
                icon.notify("Couldn't change the Start with Windows setting -- see logs.", title=app_name)
            except Exception:
                pass  # notifications aren't supported on every backend/OS
        icon.update_menu()

    def _on_exit(icon, item) -> None:
        logger.info("Tray: Exit selected. The process will end (daemon pipeline thread).")
        icon.stop()

    menu = pystray.Menu(
        Item(f"{app_name} -- running", lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        Item(_pause_item_text, _on_toggle_pause),
        Item(
            "Start with Windows",
            _on_toggle_autostart,
            checked=_autostart_enabled,
            enabled=autostart.is_available(),
        ),
        Item("Exit", _on_exit),
    )
    icon = pystray.Icon(app_name, _create_icon_image(icon_color), app_name, menu)

    def _thread_main() -> None:
        async def _runner() -> None:
            await build_and_run_orchestrator()

        try:
            asyncio.run(_runner())
            # A clean return (not an exception) means the orchestrator's
            # run() loop exited on its own, which it never does by
            # design (it's an infinite loop) -- still worth surfacing
            # rather than leaving a tray icon open over a dead pipeline.
            logger.warning("Voice pipeline exited unexpectedly (no error raised).")
        except BaseException as exc:  # noqa: BLE001 -- includes SystemExit from
            # ensure_ready()'s sys.exit(1) on a fatal startup failure
            # (LLM unreachable, etc). Catching only Exception here would
            # let that propagate silently into a dead thread while the
            # tray icon kept showing "-- running" with no indication
            # anything was wrong -- worse than an unhandled-exception
            # traceback in the log, which at least a user checking logs
            # can act on.
            logger.exception("Voice pipeline thread failed to start or crashed")
            try:
                icon.notify(f"L stopped: {exc}", title=app_name)
            except Exception:
                pass  # notifications aren't supported on every backend/OS
            icon.stop()

    pipeline_thread = threading.Thread(target=_thread_main, daemon=True, name="l-pipeline")
    pipeline_thread.start()
    icon.run()  # blocks until _on_exit calls icon.stop(), or the pipeline thread fails
