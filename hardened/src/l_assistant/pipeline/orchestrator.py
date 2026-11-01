"""
The orchestrator is the async finite-state machine that owns one shared
microphone stream and drives it through:

    IDLE (wake word) -> LISTENING (VAD-gated recording) -> THINKING (STT
    + LLM) -> SPEAKING (sentence-chunked TTS) -> IDLE

Concurrency model: this whole loop runs on a single asyncio task. Audio
capture keeps running in the background via sounddevice's own callback
thread regardless of what state we're in (see `l_assistant.audio.capture`), but
we only *consume* frames from it during IDLE and LISTENING. During
THINKING, frames arrive and queue up; the moment we return to IDLE we
call `mic.drain()` to discard that backlog before listening for the wake
word again. During SPEAKING, `SpeechStreamer` mutes the mic for the
entire duration of playback (see its module docstring) -- frames are
dropped at the source in the PortAudio callback itself, not captured and
discarded afterward.

Two features layer on top of that base FSM:

**Tool calling** (only when a `ToolDispatcher` is supplied, i.e.
`tools.enabled: true` in config): `_run_tool_loop` runs a bounded
call-model -> maybe-run-a-tool -> call-model-again cycle. This is
necessarily non-streaming (see `elin/llm/base.py`'s module docstring for
why), so a tool-invoking turn doesn't get the "speak while still
generating" latency win a plain conversational turn does -- the final
answer is still spoken through the same sentence-chunked TTS pipeline
once it's ready, which still overlaps synthesis of sentence N+1 with
playback of sentence N.

**Spoken confirmation for destructive actions**: when a tool call raises
`ConfirmationRequired`, the orchestrator speaks the question and sets
`self._pending_confirmation`; the *next* utterance is checked for a
yes/no answer instead of being sent to the LLM as a new request (see
`_resolve_pending_confirmation`), and is captured without re-requiring
the wake word (see the `skip_wake_word_once` flag in `run()`).

**Follow-up conversation mode** (`conversation.follow_up_enabled`, off
by default): reuses that exact same `skip_wake_word_once` mechanism for
general commands, not just confirmation answers. After a normal turn
completes, `run()` listens again immediately -- via `_listen_for_utterance`
with `awaiting_follow_up=True`, which uses `EndpointDetector`'s
`max_wait_for_speech_seconds` to give up quickly (reverting to
wake-word-gated IDLE) if nobody keeps talking, rather than waiting out
the full utterance ceiling in silence. `_run_turn`'s `in_follow_up_window`
parameter additionally checks the transcript against
`_FOLLOW_UP_END_PHRASES` ("that's all", "goodbye", ...) so the user can
end the window explicitly instead of waiting for it to time out, and
`conversation.max_consecutive_follow_ups` bounds how many turns can
chain before a wake-word re-arm is forced regardless of the timeout --
caps a sustained false-positive run to a fixed number of turns rather
than an unbounded one. This is the one listen in the whole file that
ISN'T gated by the wake word first, which is exactly why it defaults
off: see the "self-hearing" defenses below and `elin/stt/confidence.py`
-- STT-side hallucination rejection is this mode's primary defense
against ambient noise, not a backup behind wake-word confidence the way
it is everywhere else.

**Self-hearing (L transcribing its own TTS output as a user command)
is handled in three layers**, in order of how much work each one does:
1. `SpeechStreamer` mutes the mic for the full duration of every spoken
   utterance -- audio played while muted is never captured at all.
2. `_settle_after_speech` drains, pauses `vad.post_speech_settle_ms`,
   drains again after unmuting -- covers output-buffer latency and room
   reverb tail that continue briefly after playback electrically stops.
3. `_looks_like_self_echo` discards a transcript that's both very recent
   and textually close to what L just said, as a last resort for
   anything that still leaks through layers 1-2.
Real acoustic echo cancellation (feeding a reference signal into an
adaptive filter, as WebRTC/Speex do) was evaluated and deliberately not
integrated: the available Python bindings (`webrtc_audio_processing`,
`speexdsp-python`) ship no prebuilt wheels for a modern x86_64 target on
either OS -- both require compiling against system C libraries, which
violates this project's "no compiler required on either platform" bar
that every other dependency choice in this codebase has held to. If that
trade-off is ever revisited, layers 2-3 above are exactly the seams
where a real AEC-cleaned signal would slot in.

Barge-in (interrupting the assistant mid-sentence by speaking over it) is
a related but distinct feature -- it's not implemented, for the same
underlying reason (no clean cross-platform AEC path). Muting during
playback is *more* correct for barge-in specifically, not less: since the
mic can't hear anything while muted, this codebase has no way to
recognize "the user is trying to interrupt" at all right now.
`AudioPlayer.stop_immediately()` remains the extension point if that's
ever built.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import numpy as np

from l_assistant.audio.capture import MicrophoneStream
from l_assistant.audio.playback import AudioPlayer, generate_earcon
from l_assistant.audio.vad import EndpointDetector, FrameVad
from l_assistant.config import AppConfig
from l_assistant.llm.base import LlmClient
from l_assistant.memory.conversation_memory import ConversationMemory
from l_assistant.memory.user_profile import UserProfileStore
from l_assistant.pipeline.state import AgentState, PendingConfirmation
from l_assistant.stt.factory import SttEngine
from l_assistant.tools.registry import ToolDispatcher
from l_assistant.tools.system_tools import ConfirmationRequired
from l_assistant.tts.piper_engine import PiperTTS
from l_assistant.tts.streamer import SpeechStreamer
from l_assistant.utils.text import text_similarity
from l_assistant.wake.wake_word import WakeWordDetector

logger = logging.getLogger(__name__)

_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "confirm", "confirmed", "sure",
    "do it", "go ahead", "correct", "affirmative", "please do",
}
_NEGATIVE = {
    "no", "nope", "nah", "cancel", "stop", "don't", "do not",
    "negative", "never mind", "nevermind", "abort",
}
# Only checked during an active follow-up window (conversation.
# follow_up_enabled), never against a normal wake-triggered command --
# see _matches_end_phrase. Deliberately overlaps with _NEGATIVE
# ("stop", "never mind", ...) where a phrase naturally reads as both
# "no" to a pending confirmation and "I'm done" to end a follow-up
# window; the two sets are checked in disjoint code paths, so the
# overlap doesn't create ambiguity in practice, and it's still a
# separate set rather than reusing _NEGATIVE outright because the two
# mean different things and it should stay easy to tune one without the
# other.
_FOLLOW_UP_END_PHRASES = {
    "stop", "cancel", "never mind", "nevermind",
    "that's all", "thats all", "that will be all", "that'll be all",
    "that's it", "thats it", "we're done", "were done", "i'm done", "im done",
    "goodbye", "bye", "good night",
    "go to sleep", "stop listening",
    "thanks that's all", "thank you that's all", "thanks that'll be it",
}


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        mic: MicrophoneStream,
        player: AudioPlayer,
        wake_word: Optional[WakeWordDetector],
        frame_vad: FrameVad,
        stt: SttEngine,
        llm: LlmClient,
        tts: PiperTTS,
        speech_streamer: SpeechStreamer,
        memory: ConversationMemory,
        tool_dispatcher: Optional[ToolDispatcher] = None,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        user_profile: Optional[UserProfileStore] = None,
    ) -> None:
        self._config = config
        self._mic = mic
        self._player = player
        self._wake_word = wake_word
        self._frame_vad = frame_vad
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._speech_streamer = speech_streamer
        self._memory = memory
        self._user_profile = user_profile

        self._tool_dispatcher = tool_dispatcher
        self._tool_schemas = tool_schemas or []
        self._tools_enabled = tool_dispatcher is not None
        self._max_tool_iterations = config.tools.max_tool_iterations

        self.state = AgentState.IDLE
        self._frame_duration_s = config.audio.frame_samples / config.audio.sample_rate
        self._pending_confirmation: Optional[PendingConfirmation] = None

        # Set once run() starts; used by request_pause/request_resume to
        # safely toggle `_paused` from another thread (the tray icon's
        # thread -- see elin/tray/app_tray.py), via call_soon_threadsafe
        # rather than touching asyncio primitives cross-thread directly.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._paused: Optional[asyncio.Event] = None

    # -- external control (thread-safe) ------------------------------------

    def request_pause(self) -> None:
        """Safe to call from any thread, e.g. a system tray menu handler."""
        if self._loop is not None and self._paused is not None:
            self._loop.call_soon_threadsafe(self._paused.clear)
            logger.info("Pause requested; will take effect before the next wake-word wait.")

    def request_resume(self) -> None:
        """Safe to call from any thread."""
        if self._loop is not None and self._paused is not None:
            self._loop.call_soon_threadsafe(self._paused.set)
            logger.info("Resume requested.")

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._paused = asyncio.Event()
        self._paused.set()  # start unpaused

        async with self._mic, self._player:
            frames = self._mic.frames()
            if self._config.wake_word.enabled:
                logger.info(
                    "%s is listening for the wake word '%s'...",
                    self._config.name,
                    self._config.wake_word.model,
                )
            else:
                logger.info("Wake word disabled -- open-mic mode, listening continuously.")
            if self._tools_enabled:
                logger.info(
                    "System control tools enabled (%d tools, max %d iterations/turn).",
                    len(self._tool_schemas),
                    self._max_tool_iterations,
                )
            if self._config.conversation.follow_up_enabled:
                logger.info(
                    "Follow-up conversation mode enabled (window=%.0fs, "
                    "max %d consecutive turns without the wake word).",
                    self._config.conversation.follow_up_window_seconds,
                    self._config.conversation.max_consecutive_follow_ups,
                )

            skip_wake_word_once = False
            in_follow_up_window = False
            consecutive_follow_ups = 0
            while True:
                await self._paused.wait()
                try:
                    if not skip_wake_word_once:
                        await self._idle_until_wake_word(frames)
                        await self._speak_wake_ack()
                        in_follow_up_window = False
                        consecutive_follow_ups = 0
                    skip_wake_word_once = False

                    await self._settle_after_speech()

                    if self._pending_confirmation is not None:
                        utterance_pcm = await self._listen_for_utterance(
                            frames,
                            max_utterance_seconds=self._config.tools.confirmation_timeout_seconds,
                        )
                    elif in_follow_up_window:
                        utterance_pcm = await self._listen_for_utterance(
                            frames,
                            max_utterance_seconds=self._config.conversation.follow_up_max_utterance_seconds,
                            max_wait_for_speech_seconds=self._config.conversation.follow_up_window_seconds,
                            awaiting_follow_up=True,
                        )
                    else:
                        utterance_pcm = await self._listen_for_utterance(frames)

                    if utterance_pcm is None:
                        if self._pending_confirmation is not None:
                            # Silence after asking "are you sure?" -- don't
                            # leave a destructive action dangling forever.
                            logger.info("No reply to confirmation prompt; treating as declined.")
                            self._pending_confirmation = None
                        elif in_follow_up_window:
                            logger.info(
                                "No follow-up within %.0fs; back to wake-word listening.",
                                self._config.conversation.follow_up_window_seconds,
                            )
                        in_follow_up_window = False
                        continue

                    outcome = await self._run_turn(utterance_pcm, in_follow_up_window=in_follow_up_window)

                    if outcome == "ignored":
                        # Too short, or a likely self-echo -- nothing
                        # actually happened. Re-listen in the SAME mode we
                        # were already in (a pending confirmation or an
                        # active follow-up window keep going exactly as
                        # they were; a normal wake-triggered turn goes
                        # back to full wake-word listening, same as
                        # always) rather than letting a discarded
                        # transcript silently grant or revoke
                        # wake-word-free listening on its own.
                        skip_wake_word_once = (
                            self._pending_confirmation is not None or in_follow_up_window
                        )
                        continue

                    if outcome == "ended":
                        # Explicit end phrase during a follow-up window --
                        # skip_wake_word_once is already False here, so
                        # the next iteration re-arms the wake word.
                        in_follow_up_window = False
                        continue

                    # outcome == "handled"
                    if self._pending_confirmation is not None:
                        # The next utterance answers the confirmation
                        # question we just asked -- don't make the user
                        # repeat the wake word to answer "yes". Takes
                        # priority over starting/continuing a follow-up
                        # window; that resumes normally once the
                        # confirmation itself resolves.
                        skip_wake_word_once = True
                    elif (
                        self._config.conversation.follow_up_enabled
                        and consecutive_follow_ups < self._config.conversation.max_consecutive_follow_ups
                    ):
                        skip_wake_word_once = True
                        in_follow_up_window = True
                        consecutive_follow_ups += 1
                    else:
                        if in_follow_up_window:
                            logger.info(
                                "Hit max_consecutive_follow_ups (%d); "
                                "re-arming the wake word.",
                                self._config.conversation.max_consecutive_follow_ups,
                            )
                        in_follow_up_window = False

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Unhandled error mid-turn; recovering to idle state")
                    self.state = AgentState.ERROR_RECOVERY
                    self._pending_confirmation = None
                    skip_wake_word_once = False
                    in_follow_up_window = False
                    await self._speak_apology()
                finally:
                    self._mic.drain()
                    if self._wake_word is not None and not skip_wake_word_once:
                        self._wake_word.reset()
                    self.state = AgentState.IDLE

    # -- state handlers ---------------------------------------------------

    async def _settle_after_speech(self) -> None:
        """
        Called after any TTS playback finishes (the wake acknowledgment,
        or a confirmation question) and before listening for the user's
        next utterance.

        Without an acoustic echo cancellation path (see this module's
        docstring on barge-in), the mic reliably picks up L's own
        speech through the speakers and room acoustics. Left unhandled,
        that gets transcribed as if it were the user's command -- which
        is exactly what happened before this method existed: a wake
        acknowledgment of "Yes, boss" was picked up by the mic and
        transcribed verbatim as the user's first utterance, and every
        turn after that transcribed a garbled mix of L's own previous
        response instead of what the user actually said.

        Drains whatever the mic already queued during playback, waits
        `vad.post_speech_settle_ms` for any output-buffer/room-reverb
        tail to finish, then drains once more to catch frames that
        arrived during that wait.
        """
        self._mic.drain()
        settle_s = self._config.vad.post_speech_settle_ms / 1000.0
        if settle_s > 0:
            await asyncio.sleep(settle_s)
        self._mic.drain()

    async def _idle_until_wake_word(self, frames: AsyncIterator[np.ndarray]) -> None:
        self.state = AgentState.IDLE
        if self._wake_word is None:
            return  # open-mic mode: fall straight through to LISTENING
        async for frame in frames:
            if self._wake_word.detect(frame) is not None:
                return

    async def _listen_for_utterance(
        self,
        frames: AsyncIterator[np.ndarray],
        max_utterance_seconds: Optional[float] = None,
        max_wait_for_speech_seconds: Optional[float] = None,
        awaiting_follow_up: bool = False,
    ) -> Optional[bytes]:
        self.state = AgentState.AWAITING_FOLLOW_UP if awaiting_follow_up else AgentState.LISTENING
        endpoint = EndpointDetector(
            self._frame_vad,
            silence_timeout_ms=self._config.vad.silence_timeout_ms,
            min_utterance_ms=self._config.vad.min_utterance_ms,
            max_utterance_seconds=max_utterance_seconds or self._config.vad.max_utterance_seconds,
            max_wait_for_speech_seconds=max_wait_for_speech_seconds,
        )

        pcm_chunks: List[bytes] = []
        stop_reason = ""
        still_awaiting = awaiting_follow_up
        async for frame in frames:
            pcm_chunks.append(frame.tobytes())
            result = endpoint.push(frame, self._frame_duration_s)
            if still_awaiting and endpoint.had_speech:
                # Follow-up window: nothing said yet, then it started --
                # flip from "waiting to see if you'll say anything" to
                # "actively listening" the moment real speech begins.
                self.state = AgentState.LISTENING
                still_awaiting = False
            if result.should_stop:
                stop_reason = result.reason
                break

        if stop_reason in ("max_duration", "no_speech_started") and not endpoint.had_speech:
            # Hit the time limit (or, in a follow-up window, the earlier
            # no_speech_started ceiling) without a single speech frame --
            # pure silence, not a short/quiet utterance. Distinguishing
            # this from "too short" below matters most for the
            # confirmation flow and follow-up windows: silence after
            # "are you sure?" or after L finishes answering needs to
            # resolve to "no response" rather than feeding a long silent
            # clip to STT and then getting stuck deciding what an empty
            # transcript means.
            logger.info(
                "No speech detected within %.1fs (%s), treating as no response",
                max_wait_for_speech_seconds or max_utterance_seconds or self._config.vad.max_utterance_seconds,
                stop_reason,
            )
            return None

        if stop_reason == "max_duration" and endpoint.had_speech:
            # Speech *was* detected, but 700ms of continuous trailing
            # silence never accumulated -- every utterance running to the
            # full ceiling instead of ending naturally after the user
            # stops talking is the signature of background noise (fan,
            # HVAC, room echo) intermittently misclassified as speech,
            # which keeps resetting the silence counter to zero. Surfaced
            # at WARNING (not DEBUG) because it's always worth knowing
            # about -- a one-off is fine, every utterance hitting the
            # ceiling is a real, fixable configuration problem, not
            # something to leave invisible in the default log level.
            logger.warning(
                "Utterance ran the full %.0fs without a natural pause -- "
                "speech was detected but never followed by %dms of clean "
                "silence. Usually background noise (fan, HVAC, room echo) "
                "getting misclassified as speech. Try raising "
                "vad.aggressiveness (currently %d) or "
                "vad.speech_frame_vote_threshold (currently %.2f), or check "
                "mic gain/placement.",
                max_utterance_seconds or self._config.vad.max_utterance_seconds,
                self._config.vad.silence_timeout_ms,
                self._config.vad.aggressiveness,
                self._config.vad.speech_frame_vote_threshold,
            )
        elif stop_reason:
            logger.debug("Utterance ended (%s)", stop_reason)

        pcm = b"".join(pcm_chunks)
        duration_ms = 1000 * (len(pcm) / 2) / self._config.audio.sample_rate
        if duration_ms < self._config.vad.min_utterance_ms:
            logger.info("Utterance too short (%.0f ms), discarding as a false trigger", duration_ms)
            return None
        return pcm

    async def _run_turn(self, utterance_pcm: bytes, in_follow_up_window: bool = False) -> str:
        """
        Returns "ignored" (nothing usable was said -- too short, or a
        likely self-echo; the caller should keep listening in whatever
        mode it was already in, rather than treating a discarded
        transcript as either a completed turn or a reason to change
        mode), "handled" (a real command was processed, or a pending
        confirmation was resolved), or "ended" (an explicit end phrase
        was heard during a follow-up window -- the caller should return
        to wake-word-gated idle immediately rather than waiting out the
        rest of the follow-up window).
        """
        t0 = time.monotonic()
        self.state = AgentState.THINKING

        # Instant feedback the moment recording ends, before STT/LLM --
        # the two slowest steps in a turn -- even start. Without this,
        # the only signal a command was captured was the eventual full
        # response, sometimes several seconds later on a tool-calling
        # turn; in practice that led to people repeating the wake word
        # mid-turn out of uncertainty, which then polluted the very
        # recording it was worried hadn't started. A synthesized tone
        # costs nothing to generate (a sine wave, not a model call).
        logger.info("Captured -- transcribing...")
        await self._player.play_pcm16(
            generate_earcon(self._player.sample_rate, frequency=1200.0, duration_s=0.08)
        )

        transcript = await self._stt.transcribe(utterance_pcm, self._config.audio.sample_rate)

        if len(transcript.strip()) < self._config.stt.min_transcript_chars:
            logger.info("Empty/too-short transcript (%r), ignoring turn", transcript)
            return "ignored"

        if self._looks_like_self_echo(transcript):
            logger.warning(
                "Discarding likely self-echo transcript (closely matches "
                "what L just said): %r",
                transcript,
            )
            return "ignored"

        if self._pending_confirmation is not None:
            await self._resolve_pending_confirmation(transcript)
            return "handled"

        if in_follow_up_window and self._matches_end_phrase(transcript):
            logger.info("Follow-up window ended by user: %r", transcript)
            await self._memory.add_message("user", transcript)
            self.state = AgentState.SPEAKING
            closing = "Okay, let me know if you need anything else."
            await self._speak_complete_text(closing)
            await self._memory.add_message("assistant", closing)
            return "ended"

        logger.info("User: %s", transcript)
        await self._memory.add_message("user", transcript)
        context = await self._memory.get_context_messages()
        system_content = self._config.llm.system_prompt
        if self._user_profile is not None:
            # Proactively in context every turn, not behind a "recall"
            # tool the model has to think to call -- a real assistant
            # doesn't need to be asked to remember your name once you've
            # told it. remember_user_fact/forget_user_fact (system_tools.py
            # section 8) are for LEARNING and CORRECTING facts; this is
            # what makes facts already known actually get used.
            profile_fragment = self._user_profile.as_prompt_fragment()
            if profile_fragment:
                system_content = f"{system_content}\n\n{profile_fragment}"
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_content},
            *context,
        ]

        if self._tools_enabled:
            spoken_text = await self._run_tool_loop(messages)
            t_end = time.monotonic()
            logger.info("Turn timing: total=%.0fms (tool-calling, non-streamed)", (t_end - t0) * 1000)
        else:
            timing_marks: Dict[str, float] = {}

            async def instrumented_stream() -> AsyncIterator[str]:
                async for delta in self._llm.stream_chat(messages):
                    if "first_token" not in timing_marks:
                        timing_marks["first_token"] = time.monotonic()
                    yield delta

            self.state = AgentState.SPEAKING
            spoken_text = await self._speech_streamer.speak_stream(instrumented_stream())
            t_end = time.monotonic()
            logger.info(
                "Turn timing: time-to-first-token=%.0fms  total=%.0fms",
                (timing_marks.get("first_token", t0) - t0) * 1000,
                (t_end - t0) * 1000,
            )

        if spoken_text:
            logger.info("L: %s", spoken_text)
            await self._memory.add_message("assistant", spoken_text)
            await self._memory.maybe_compact(self._llm.summarize)

        return "handled"

    async def _run_tool_loop(self, messages: List[Dict[str, Any]]) -> str:
        """
        Bounded call-model -> maybe-run-tool -> call-model-again cycle.
        Returns the spoken text of the final answer (possibly a
        confirmation question, if a destructive tool call needs one).
        """
        assert self._tool_dispatcher is not None

        for iteration in range(self._max_tool_iterations):
            result = await self._llm.chat_with_tools(messages, self._tool_schemas)

            if not result.tool_calls:
                self.state = AgentState.SPEAKING
                return await self._speak_complete_text(result.content)

            messages.append(result.raw_assistant_message)

            for call in result.tool_calls:
                logger.info("Tool call (iteration %d): %s(%r)", iteration + 1, call.name, call.arguments)
                try:
                    # run_in_executor, not a direct call: ToolDispatcher.
                    # execute is synchronous, and some tools genuinely
                    # block for real time (run_command's subprocess,
                    # open_website's YouTube-search resolution via
                    # yt-dlp) -- calling it directly on this coroutine
                    # would freeze the ENTIRE event loop for that whole
                    # duration: audio capture, TTS playback, wake-word
                    # detection, all of it, not just this one turn.
                    tool_result = await asyncio.get_running_loop().run_in_executor(
                        None, self._tool_dispatcher.execute, call.name, call.arguments
                    )
                except ConfirmationRequired as exc:
                    self._pending_confirmation = PendingConfirmation(
                        tool_name=call.name,
                        arguments=call.arguments,
                        question=str(exc),
                    )
                    self.state = AgentState.SPEAKING
                    return await self._speak_complete_text(str(exc))

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": tool_result,
                    }
                )

        logger.warning(
            "Hit tools.max_tool_iterations (%d) without a final answer", self._max_tool_iterations
        )
        self.state = AgentState.SPEAKING
        return await self._speak_complete_text(
            "I wasn't able to finish that after a few tries. Want me to try something else?"
        )

    async def _resolve_pending_confirmation(self, transcript: str) -> None:
        """
        Handles the utterance immediately following a spoken "are you
        sure?" question. Not added to conversation memory -- this is a
        short out-of-band yes/no exchange, not something later turns need
        in their context window.
        """
        assert self._pending_confirmation is not None
        assert self._tool_dispatcher is not None
        pending = self._pending_confirmation
        self._pending_confirmation = None

        normalized = transcript.strip().lower().strip(".!? ")
        is_yes = normalized in _AFFIRMATIVE or any(
            normalized.startswith(word) for word in _AFFIRMATIVE
        )
        is_no = normalized in _NEGATIVE or any(normalized.startswith(word) for word in _NEGATIVE)

        self.state = AgentState.SPEAKING
        if is_yes and not is_no:
            logger.info("Confirmation accepted: %s(%r)", pending.tool_name, pending.arguments)
            # Confirmation travels out-of-band (keyword argument), never inside the
            # model-controlled argument dict -- see ToolDispatcher.execute.
            result_text = await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(
                    self._tool_dispatcher.execute, pending.tool_name, pending.arguments, confirmed=True
                ),
            )
            await self._speak_complete_text(result_text)
        else:
            logger.info(
                "Confirmation declined or unclear (%r) for: %s(%r)",
                transcript, pending.tool_name, pending.arguments,
            )
            await self._speak_complete_text("Okay, cancelled.")

    def _looks_like_self_echo(self, transcript: str) -> bool:
        """
        Last-resort defense against acting on L hearing itself: True if
        `transcript` arrived within `vad.self_echo_refractory_seconds` of
        L finishing its last utterance AND is textually similar enough
        to that utterance to plausibly be a garbled re-hearing of it
        rather than a real, coincidentally-similar request.

        This is a safety net, not the primary defense -- `SpeechStreamer`
        muting the mic for the full duration of every spoken response
        (see its module docstring) is what actually prevents most
        self-hearing. This exists for the residual gap between "playback
        electrically stopped" and "the room has actually gone quiet"
        that `vad.post_speech_settle_ms` narrows but can't fully close
        without real acoustic echo cancellation (see this module's
        docstring on why that isn't integrated here).
        """
        last_text = self._speech_streamer.last_spoken_text
        last_at = self._speech_streamer.last_spoken_at
        if last_text is None or last_at is None:
            return False

        elapsed = time.monotonic() - last_at
        if elapsed > self._config.vad.self_echo_refractory_seconds:
            return False

        similarity = text_similarity(transcript, last_text)
        if similarity >= self._config.vad.self_echo_similarity_threshold:
            logger.debug(
                "Self-echo check: transcript=%r vs last_spoken=%r -> "
                "similarity=%.2f (elapsed=%.1fs)",
                transcript,
                last_text,
                similarity,
                elapsed,
            )
            return True
        return False

    def _matches_end_phrase(self, transcript: str) -> bool:
        """Only ever checked during an active follow-up window -- see
        _run_turn and _FOLLOW_UP_END_PHRASES' module-level comment."""
        normalized = transcript.strip().lower().strip(".!? ")
        return normalized in _FOLLOW_UP_END_PHRASES or any(
            normalized.startswith(phrase) for phrase in _FOLLOW_UP_END_PHRASES
        )

    async def _speak_complete_text(self, text: str) -> str:
        """
        Feeds an already-complete string through the same SentenceChunker
        + overlapped-synthesis pipeline `SpeechStreamer` uses for live LLM
        token streams, via a one-shot async generator. Sentence 1 still
        starts playing while sentence 2 synthesizes, even though
        generation itself already finished before this was called.
        """
        async def _one_shot() -> AsyncIterator[str]:
            yield text

        return await self._speech_streamer.speak_stream(_one_shot())

    async def _speak_wake_ack(self) -> None:
        phrase = self._config.wake_word.ack_phrase
        if not phrase:
            return
        await self._speak_complete_text(phrase)

    async def _speak_apology(self) -> None:
        try:
            await self._speak_complete_text(
                "Sorry, something went wrong there. Go ahead and try again."
            )
        except Exception:
            logger.exception("Failed to speak the error apology (non-fatal, continuing)")
