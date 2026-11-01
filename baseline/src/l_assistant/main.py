"""
L voice assistant entry point.

Usage:
    python -m l_assistant
    python -m l_assistant --config config/default.yaml
    python -m l_assistant --list-devices
    python -m l_assistant --no-wake-word              # open-mic mode
    python -m l_assistant --config config/groq.yaml    # cloud LLM, no local Ollama needed
    python -m l_assistant --tools                      # enable OS control (see README)
    python -m l_assistant --tray                       # run in the system tray

This module is the only place the whole dependency graph is assembled:
config -> (mic, player) + (wake word, VAD, STT, LLM client, TTS, memory,
tools) -> Orchestrator, either run directly (foreground) or handed to the
system tray's background thread. Every component above is independently
testable; this file's only job is wiring, startup ordering, and
top-level error handling.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from rich.console import Console
from rich.panel import Panel

from l_assistant import platform_utils
from l_assistant.audio.capture import MicrophoneStream
from l_assistant.audio.playback import AudioPlayer
from l_assistant.audio.vad import FrameVad
from l_assistant.config import AppConfig, load_config
from l_assistant.llm.base import LlmClient, LlmUnavailableError
from l_assistant.llm.factory import build_llm_client
from l_assistant.logging_setup import get_logger, setup_logging
from l_assistant.memory.conversation_memory import ConversationMemory
from l_assistant.memory.user_profile import UserProfileStore
from l_assistant.pipeline.orchestrator import Orchestrator
from l_assistant.stt.factory import SttEngine, build_stt_engine
from l_assistant.tools.registry import (
    COMMAND_EXECUTION_TOOL_SCHEMAS,
    CORE_TOOL_SCHEMAS,
    POWER_MANAGEMENT_TOOL_SCHEMAS,
    ToolDispatcher,
)
from l_assistant.tools.system_tools import SystemToolsEngine
from l_assistant.tts.piper_engine import PiperTTS
from l_assistant.tts.streamer import SpeechStreamer
from l_assistant.wake.wake_word import WakeWordDetector

logger = get_logger("l_assistant.main")


@dataclass
class ComponentBundle:
    """Everything Orchestrator needs, built once by `_build_components`."""

    mic: MicrophoneStream
    player: AudioPlayer
    wake_word: Optional[WakeWordDetector]
    frame_vad: FrameVad
    stt: SttEngine
    llm: LlmClient
    tts: PiperTTS
    speech_streamer: SpeechStreamer
    memory: ConversationMemory
    tool_dispatcher: Optional[ToolDispatcher]
    tool_schemas: List[Dict[str, Any]]
    user_profile: UserProfileStore


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="l", description="L local voice assistant")
    parser.add_argument("--config", type=Path, default=None, help="Path to a config YAML file")
    parser.add_argument("--log-level", type=str, default=None, help="Override configured log level")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument("--input-device", type=str, default=None, help="Override input device name/index")
    parser.add_argument("--output-device", type=str, default=None, help="Override output device name/index")
    parser.add_argument(
        "--no-wake-word",
        action="store_true",
        help="Disable wake word detection and listen continuously (open-mic mode)",
    )
    tools_group = parser.add_mutually_exclusive_group()
    tools_group.add_argument(
        "--tools", action="store_true", help="Enable OS control tools (overrides config)"
    )
    tools_group.add_argument(
        "--no-tools", action="store_true", help="Disable OS control tools (overrides config)"
    )
    tray_group = parser.add_mutually_exclusive_group()
    tray_group.add_argument(
        "--tray", action="store_true", help="Run in the system tray (overrides config)"
    )
    tray_group.add_argument(
        "--no-tray", action="store_true", help="Run in the foreground terminal (overrides config)"
    )
    autostart_group = parser.add_mutually_exclusive_group()
    autostart_group.add_argument(
        "--enable-autostart",
        action="store_true",
        help="Add L to Windows startup (registry Run key) and exit",
    )
    autostart_group.add_argument(
        "--disable-autostart",
        action="store_true",
        help="Remove L from Windows startup and exit",
    )
    return parser.parse_args(argv)


def _list_devices() -> None:
    # Imported lazily: this is the only place sounddevice's device query is
    # needed, and importing it before argparse would slow down `--help`.
    import sounddevice as sd

    print(sd.query_devices())


def _print_banner(config: AppConfig, data_dir: Path) -> None:
    lines = [
        f"[bold]wake word[/]   {config.wake_word.model if config.wake_word.enabled else '(disabled -- open mic)'}",
        f"[bold]stt[/]         {config.stt.provider}"
        + (
            f" / {config.stt.model_size} ({config.stt.device}, {config.stt.compute_type})"
            if config.stt.provider == "local"
            else f" / {config.stt.groq_model}"
        ),
        f"[bold]llm[/]         {config.llm.provider} / {config.llm.model}"
        + (f" @ {config.llm.base_url}" if config.llm.base_url else ""),
        f"[bold]voice[/]       piper / {config.tts.voice}",
        f"[bold]tools[/]       {'enabled' if config.tools.enabled else 'disabled'}",
        f"[bold]follow-up[/]   {'enabled (' + str(int(config.conversation.follow_up_window_seconds)) + 's window)' if config.conversation.follow_up_enabled else 'disabled'}",
        f"[bold]tray[/]        {'enabled' if config.tray.enabled else 'disabled'}",
        f"[bold]data dir[/]    {data_dir}",
    ]
    Console().print(Panel("\n".join(lines), title=f"{config.name} starting up", expand=False))


def _build_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if args.log_level:
        config.log_level = args.log_level
    if args.input_device:
        config.audio.input_device = args.input_device
    if args.output_device:
        config.audio.output_device = args.output_device
    if args.no_wake_word:
        config.wake_word.enabled = False
    if args.tools:
        config.tools.enabled = True
    if args.no_tools:
        config.tools.enabled = False
    if args.tray:
        config.tray.enabled = True
    if args.no_tray:
        config.tray.enabled = False
    return config


async def _build_components(config: AppConfig) -> ComponentBundle:
    data_dir = platform_utils.get_data_dir(config.data_dir)
    voices_dir = (
        Path(config.tts.voice_dir)
        if config.tts.voice_dir
        else platform_utils.get_voices_dir(config.data_dir)
    )
    db_path = platform_utils.get_db_path(config.memory.db_filename, config.data_dir)
    logger.info("%s data directory: %s", config.name, data_dir)
    _print_banner(config, data_dir)

    # The LLM is checked first, before any heavy local model is loaded --
    # failing fast here means the user isn't left waiting through a 30s
    # Whisper + Piper + wake-word load only to be told the LLM was never
    # reachable in the first place. Works identically whether the
    # configured provider is a local Ollama server or Groq's cloud API.
    llm = build_llm_client(config.llm)
    try:
        await llm.ensure_ready()
    except LlmUnavailableError as exc:
        logger.error(str(exc))
        sys.exit(1)
    logger.info("LLM ready: provider=%s, model=%s", config.llm.provider, config.llm.model)

    # Heavy, blocking model loads are pushed onto worker threads. They run
    # sequentially here (each `await` waits for the previous to finish) so
    # log output stays readable and peak RAM during startup stays lower
    # than loading everything at once would.
    stt = await asyncio.to_thread(build_stt_engine, config.stt)
    await asyncio.to_thread(stt.warmup)

    tts = await asyncio.to_thread(
        PiperTTS,
        voice=config.tts.voice,
        voices_dir=voices_dir,
        length_scale=config.tts.length_scale,
        volume=config.tts.volume,
        use_cuda=config.tts.use_cuda,
    )
    await asyncio.to_thread(tts.warmup)

    wake_word: Optional[WakeWordDetector] = None
    if config.wake_word.enabled:
        wake_word = await asyncio.to_thread(
            WakeWordDetector,
            model=config.wake_word.model,
            threshold=config.wake_word.threshold,
            vad_threshold=config.wake_word.vad_threshold,
            inference_framework=config.wake_word.inference_framework,
            cooldown_seconds=config.wake_word.cooldown_seconds,
        )

    frame_vad = FrameVad(
        sample_rate=config.audio.sample_rate,
        aggressiveness=config.vad.aggressiveness,
        vote_threshold=config.vad.speech_frame_vote_threshold,
        min_rms=config.vad.min_rms,
    )
    mic = MicrophoneStream(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        frame_samples=config.audio.frame_samples,
        device=config.audio.input_device,
    )
    # Output stream runs at the TTS voice's native sample rate (Piper
    # voices are commonly 22050 Hz, not the 16 kHz the mic/STT/wake-word
    # side uses) so there is no resampling step -- one less place for
    # latency or quality loss to creep in.
    player = AudioPlayer(
        sample_rate=tts.sample_rate,
        channels=1,
        device=config.audio.output_device,
    )
    speech_streamer = SpeechStreamer(
        tts=tts,
        player=player,
        min_chunk_chars=config.tts.min_chunk_chars,
        mic=mic,
    )
    memory = ConversationMemory(
        db_path=db_path,
        max_turns_in_context=config.memory.max_turns_in_context,
        summarize_after_turns=config.memory.summarize_after_turns,
    )
    user_profile = UserProfileStore(platform_utils.get_profile_path(config.data_dir))

    tool_dispatcher: Optional[ToolDispatcher] = None
    tool_schemas: List[Dict[str, Any]] = []
    if config.tools.enabled:
        engine = SystemToolsEngine(
            allowed_roots=config.tools.allowed_roots or None,
            require_confirmation=config.tools.require_confirmation_for_destructive,
            command_execution_enabled=config.tools.command_execution_enabled,
            power_management_enabled=config.tools.power_management_enabled,
            power_min_delay_seconds=config.tools.power_min_delay_seconds,
            user_profile=user_profile,
        )
        tool_dispatcher = ToolDispatcher(engine)
        # CORE_TOOL_SCHEMAS always comes along with tools.enabled (same as
        # before); the other two are additive and each gated by their own
        # flag, so turning tools.enabled on does not, by itself, expose
        # power_control or run_command -- see config.py's ToolsConfig and
        # system_tools.py sections 6/7 for why those two are treated as a
        # separate risk class from everything in CORE_TOOL_SCHEMAS.
        tool_schemas = list(CORE_TOOL_SCHEMAS)
        if config.tools.power_management_enabled:
            tool_schemas += POWER_MANAGEMENT_TOOL_SCHEMAS
        if config.tools.command_execution_enabled:
            tool_schemas += COMMAND_EXECUTION_TOOL_SCHEMAS
        logger.info(
            "System control enabled: %d tools (power_management=%s, "
            "command_execution=%s), confirmation_required=%s, allowed_roots=%s",
            len(tool_schemas),
            config.tools.power_management_enabled,
            config.tools.command_execution_enabled,
            config.tools.require_confirmation_for_destructive,
            config.tools.allowed_roots or [str(Path.home())],
        )

    return ComponentBundle(
        mic=mic,
        player=player,
        wake_word=wake_word,
        frame_vad=frame_vad,
        stt=stt,
        llm=llm,
        tts=tts,
        speech_streamer=speech_streamer,
        memory=memory,
        tool_dispatcher=tool_dispatcher,
        tool_schemas=tool_schemas,
        user_profile=user_profile,
    )


def _make_orchestrator(config: AppConfig, bundle: ComponentBundle) -> Orchestrator:
    return Orchestrator(
        config=config,
        mic=bundle.mic,
        player=bundle.player,
        wake_word=bundle.wake_word,
        frame_vad=bundle.frame_vad,
        stt=bundle.stt,
        llm=bundle.llm,
        tts=bundle.tts,
        speech_streamer=bundle.speech_streamer,
        memory=bundle.memory,
        tool_dispatcher=bundle.tool_dispatcher,
        tool_schemas=bundle.tool_schemas,
        user_profile=bundle.user_profile,
    )


async def _run_foreground(config: AppConfig) -> None:
    bundle = await _build_components(config)
    orchestrator = _make_orchestrator(config, bundle)
    try:
        await orchestrator.run()
    finally:
        bundle.memory.close()


def _run_tray_mode(config: AppConfig) -> None:
    """
    Blocking. Acquires the single-instance lock on the calling (main)
    thread, then hands off to `l_assistant.tray.app_tray.run_in_tray`, which
    builds the orchestrator on a background thread and blocks this thread
    on the tray icon's own event loop until Exit is chosen.
    """
    from l_assistant.tray.app_tray import AlreadyRunningError, SingleInstanceGuard, run_in_tray

    try:
        # Must be kept alive for the lock to hold -- assigning it (even
        # though it's otherwise unused) is what matters here, not reading
        # it back.
        _guard = SingleInstanceGuard()  # noqa: F841
    except AlreadyRunningError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except ImportError as exc:
        logger.error(
            "Tray mode needs extra dependencies that aren't installed: %s\n"
            "Install them with:  pip install pystray tendo pillow",
            exc,
        )
        sys.exit(1)

    # A plain dict as a mutable holder: the orchestrator doesn't exist
    # until `_build_and_run` runs on the background thread, but the tray's
    # Pause/Resume menu (constructed before that thread starts) needs a
    # way to reach it once it does.
    holder: Dict[str, Optional[Orchestrator]] = {"orchestrator": None}

    async def _build_and_run() -> None:
        bundle = await _build_components(config)
        orchestrator = _make_orchestrator(config, bundle)
        holder["orchestrator"] = orchestrator
        try:
            await orchestrator.run()
        finally:
            bundle.memory.close()

    def _request_pause() -> None:
        orchestrator = holder["orchestrator"]
        if orchestrator is not None:
            orchestrator.request_pause()

    def _request_resume() -> None:
        orchestrator = holder["orchestrator"]
        if orchestrator is not None:
            orchestrator.request_resume()

    try:
        run_in_tray(
            app_name=config.name,
            icon_color=config.tray.icon_color,
            build_and_run_orchestrator=_build_and_run,
            request_pause=_request_pause,
            request_resume=_request_resume,
        )
    except ImportError as exc:
        logger.error(
            "Tray mode needs extra dependencies that aren't installed: %s\n"
            "Install them with:  pip install pystray tendo pillow",
            exc,
        )
        sys.exit(1)


def _show_fatal_error(title: str, message: str) -> None:
    """
    Last-resort error surfacing for a console=False packaged build --
    see the 2026-08-23 note in the module docstring on why this exists
    at all: a plain `logger.exception(...)` is not enough on its own,
    because nothing guarantees the person hitting a startup failure
    thinks to go find and open the log file. On Windows, this pops a
    real message box (ctypes, no new dependency) so a crash is
    something you SEE, not something you have to know to go looking
    for. Falls back to stderr elsewhere (also what happens if the
    message box call itself fails for some reason -- this function is
    itself inside a last-resort path and must not be able to raise).
    """
    full_message = f"{message}\n\nDetails were written to the log file (see README's Troubleshooting section)."
    if sys.platform == "win32":
        try:
            import ctypes

            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(None, full_message, title, MB_OK | MB_ICONERROR)
            return
        except Exception:
            pass  # fall through to stderr below -- this function cannot itself raise
    print(f"{title}: {full_message}", file=sys.stderr)


def cli_main(argv: Optional[Sequence[str]] = None) -> None:
    args = _parse_args(argv)

    if args.list_devices:
        _list_devices()
        return

    if args.enable_autostart or args.disable_autostart:
        from l_assistant import autostart

        if not autostart.is_available():
            print("Autostart is Windows-only.", file=sys.stderr)
            sys.exit(1)
        try:
            if args.enable_autostart:
                autostart.enable()
                print("L will now start automatically when you log in.")
            else:
                autostart.disable()
                print("L will no longer start automatically.")
        except OSError as exc:
            print(f"ERROR: couldn't update the registry: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    try:
        config = _build_config(args)
    except FileNotFoundError as exc:
        # Logging isn't set up yet at this point (it needs config.data_dir
        # to know where to put the log file) -- plain stderr here, not
        # logger.error, is deliberate, not an oversight.
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- genuinely last-resort, see _show_fatal_error
        _show_fatal_error("L failed to start", f"Couldn't load configuration: {exc}")
        sys.exit(1)

    log_dir = platform_utils.get_log_dir(config.data_dir)
    setup_logging(log_dir, level=config.log_level)

    logger.info("Starting %s...", config.name)
    try:
        if config.tray.enabled:
            _run_tray_mode(config)  # blocking, synchronous
        else:
            asyncio.run(_run_foreground(config))
    except KeyboardInterrupt:
        # Plain KeyboardInterrupt handling (rather than manual
        # loop.add_signal_handler for SIGINT/SIGTERM) is deliberate: it is
        # the one shutdown mechanism that behaves identically under
        # asyncio.run() on both Linux and Windows without any branching.
        logger.info("Interrupted, shutting down.")
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        # Expected configuration/startup problems (missing custom wake
        # word model, invalid config value, etc) get a clean one-line
        # error instead of a full traceback -- anything *not* in this
        # tuple, or the tuple below, is unexpected and handled there.
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        # Genuinely last-resort: something neither this function's own
        # more specific handlers NOR _run_tray_mode's (ImportError,
        # AlreadyRunningError) caught. Before this existed, an exception
        # here in a console=False packaged build (see l_agent.spec) had
        # nowhere to go -- no console to print a traceback to, so the
        # process just silently vanished with zero visible indication
        # anything went wrong: no tray icon, no error, nothing. Proven,
        # not theoretical -- `import pystray` itself can raise something
        # other than ImportError depending on backend/environment state
        # (confirmed directly: an X11 backend init failure on Linux
        # raises Xlib.error.DisplayNameError, not ImportError; Windows'
        # win32 backend is a different code path with its own possible
        # failure modes this hasn't been able to enumerate against a
        # real Windows machine). Whatever the specific cause, it must
        # never again be invisible.
        logger.exception("Unhandled error during startup")
        _show_fatal_error(f"{config.name} failed to start", str(exc) or type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    cli_main()
