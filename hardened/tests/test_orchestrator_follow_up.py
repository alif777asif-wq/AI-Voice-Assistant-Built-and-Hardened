"""
Exercises Orchestrator.run() itself -- the actual async while-loop, not
just its pieces in isolation -- via a fully faked mic/wake-word/VAD/STT/
LLM/speech-streamer stack. This is the only place the interaction
between skip_wake_word_once, in_follow_up_window, and the pending-
confirmation flow gets tested as a whole; test_orchestrator_tools.py
deliberately calls individual methods directly instead (see its own
comment on why) and doesn't exercise run() at all.

Frames are tagged floats, not real audio -- FakeWakeWord/FakeFrameVad
below key off the tag value instead of doing real signal processing.
Each fake frames generator raises _StopTest once its scripted sequence
is exhausted, which is what lets a test cleanly end run()'s otherwise-
infinite loop and then assert on everything the fakes recorded.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.config import AppConfig
from l_assistant.llm.base import ChatWithToolsResult
from l_assistant.pipeline.orchestrator import Orchestrator

WAKE = 1.0
SPEECH = 2.0
SILENCE = 0.0

# 4 SPEECH frames (4 * 80ms = 320ms >= default min_utterance_ms=250) then
# 9 SILENCE frames (9 * 80ms = 720ms >= default silence_timeout_ms=700)
# is the minimal tag sequence that ends via EndpointDetector's "silence"
# reason with default VadConfig timings and the default 80ms frame
# (1280 samples @ 16kHz) -- see vad.py if either default changes.
UTTERANCE = [SPEECH] * 4 + [SILENCE] * 9


class _StopTest(BaseException):
    """NOT an Exception subclass on purpose: Orchestrator.run()'s own
    `except Exception:` recovery handler would otherwise swallow this,
    log an apology, and loop forever against an already-exhausted fake
    frames generator instead of letting the test end. asyncio.
    CancelledError uses the same BaseException trick for the same
    reason."""


def _frame(tag: float, samples: int = 1280) -> np.ndarray:
    # Real frames are 1280-sample int16 mono blocks (AudioConfig
    # defaults) -- matching that size matters here, not just dtype: the
    # post-endpoint-detection "was this actually long enough" check in
    # _listen_for_utterance derives duration from len(pcm_bytes), so a
    # too-small fake frame reads as a near-zero-length utterance and
    # gets discarded regardless of how many logical frames were pushed.
    # Only frame[0] is ever inspected (by FakeWakeWord/FakeFrameVad
    # below); the tag is broadcast across the whole block simply so
    # there's nothing meaningful-looking left uninitialized.
    return np.full(samples, int(tag), dtype=np.int16)


async def _frames_from(tags: List[float], delay_s: float = 0.0) -> AsyncIterator[np.ndarray]:
    for tag in tags:
        if delay_s:
            await asyncio.sleep(delay_s)
        yield _frame(tag)
    raise _StopTest()


class FakeWakeWord:
    def __init__(self) -> None:
        self.reset_count = 0
        self.detect_count = 0

    def detect(self, frame: np.ndarray) -> Optional[str]:
        if frame[0] == WAKE:
            self.detect_count += 1
            return "hey_jarvis"
        return None

    def reset(self) -> None:
        self.reset_count += 1


class FakeFrameVad:
    def is_speech(self, frame: np.ndarray) -> bool:
        return frame[0] == SPEECH


class FakeMic:
    def __init__(self, tags: List[float], delay_s: float = 0.0) -> None:
        self._tags = tags
        self._delay_s = delay_s
        self.drain_count = 0

    async def __aenter__(self) -> "FakeMic":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def frames(self) -> AsyncIterator[np.ndarray]:
        return _frames_from(self._tags, self._delay_s)

    def drain(self) -> None:
        self.drain_count += 1


class FakePlayer:
    sample_rate = 22050

    async def __aenter__(self) -> "FakePlayer":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def play_pcm16(self, pcm: bytes) -> None:
        return None


class FakeStt:
    def __init__(self, script: List[str]) -> None:
        self._script = list(script)
        self.call_count = 0

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        self.call_count += 1
        return self._script.pop(0) if self._script else ""


class FakeMemory:
    def __init__(self) -> None:
        self.messages: List[tuple] = []

    async def add_message(self, role: str, text: str) -> None:
        self.messages.append((role, text))

    async def get_context_messages(self) -> List[Dict[str, Any]]:
        return []

    async def maybe_compact(self, summarize_fn: Any) -> None:
        return None


class FakeSpeechStreamer:
    def __init__(self) -> None:
        self.spoken: List[str] = []
        self.last_spoken_text: Optional[str] = None
        self.last_spoken_at: Optional[float] = None

    async def speak_stream(self, text_stream: AsyncIterator[str]) -> str:
        parts = [chunk async for chunk in text_stream]
        full = "".join(parts)
        self.spoken.append(full)
        self.last_spoken_text = full
        self.last_spoken_at = time.monotonic()
        return full


class ScriptedLlm:
    def __init__(self, script: List[ChatWithToolsResult]) -> None:
        self._script = list(script)

    async def ensure_ready(self) -> None:
        return None

    def stream_chat(self, messages: Any) -> Any:
        raise NotImplementedError("not exercised by these tests")

    async def chat_with_tools(self, messages: Any, tools: Any) -> ChatWithToolsResult:
        return self._script.pop(0)

    async def summarize(self, text: str, max_words: int = 120) -> str:
        return ""


class AlwaysSucceedsDispatcher:
    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        return "ok"


def _plain_reply(text: str) -> ChatWithToolsResult:
    return ChatWithToolsResult(content=text, tool_calls=[], raw_assistant_message={"role": "assistant", "content": text})


def _make_orchestrator(
    tags: List[float],
    transcripts: List[str],
    llm_script: List[ChatWithToolsResult],
    conversation_overrides: Optional[Dict[str, Any]] = None,
    frame_delay_s: float = 0.0,
):
    conv = {
        "follow_up_enabled": True,
        "follow_up_window_seconds": 5.0,
        "follow_up_max_utterance_seconds": 20.0,
        "max_consecutive_follow_ups": 6,
    }
    conv.update(conversation_overrides or {})

    config = AppConfig(
        tools={"enabled": True},
        conversation=conv,
        vad={"post_speech_settle_ms": 1},  # skip the real settle sleep in tests
    )
    mic = FakeMic(tags, delay_s=frame_delay_s)
    wake_word = FakeWakeWord()
    stt = FakeStt(transcripts)
    speech_streamer = FakeSpeechStreamer()
    memory = FakeMemory()
    llm = ScriptedLlm(llm_script)

    orch = Orchestrator(
        config=config,
        mic=mic,
        player=FakePlayer(),
        wake_word=wake_word,
        frame_vad=FakeFrameVad(),
        stt=stt,
        llm=llm,
        tts=None,
        speech_streamer=speech_streamer,
        memory=memory,
        tool_dispatcher=AlwaysSucceedsDispatcher(),
        tool_schemas=[{"type": "function", "function": {"name": "dummy"}}],
    )
    return orch, wake_word, stt, speech_streamer


# =========================================================================


@pytest.mark.asyncio
async def test_follow_up_disabled_matches_original_behavior():
    """Critical regression check: with the feature off (the default), a
    second utterance with no wake word in front of it must be IGNORED
    entirely, exactly as before follow-up mode existed -- not queued,
    not treated as a follow-up, nothing. Only 1 STT call should ever
    happen even though a full second utterance is right there in the
    frame stream."""
    tags = [WAKE] + UTTERANCE + UTTERANCE  # second utterance, no WAKE in front of it
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags, transcripts=["open notepad", "should never be reached"],
        llm_script=[_plain_reply("Opening notepad.")],
        conversation_overrides={"follow_up_enabled": False},
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 1
    assert speech_streamer.spoken == ["Yes?", "Opening notepad."]


@pytest.mark.asyncio
async def test_follow_up_chains_second_turn_without_wake_word():
    tags = [WAKE] + UTTERANCE + UTTERANCE  # note: no WAKE between the two utterances
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags,
        transcripts=["what's the weather", "and set a timer"],
        llm_script=[_plain_reply("Sunny today."), _plain_reply("Timer set.")],
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 2
    assert speech_streamer.spoken == ["Yes?", "Sunny today.", "Timer set."]
    assert wake_word.detect_count == 1  # only ever triggered once, for the whole exchange


@pytest.mark.asyncio
async def test_follow_up_window_times_out_and_reverts_to_idle():
    tags = (
        [WAKE] + UTTERANCE
        + [SILENCE] * 30  # real per-frame delay below makes this exceed the window
        + [WAKE] + UTTERANCE
    )
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags,
        transcripts=["turn one", "turn three"],
        llm_script=[_plain_reply("Response one."), _plain_reply("Response three.")],
        conversation_overrides={"follow_up_window_seconds": 0.05},
        frame_delay_s=0.003,
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 2  # the silent stretch never reached STT at all
    assert speech_streamer.spoken == ["Yes?", "Response one.", "Yes?", "Response three."]
    assert wake_word.detect_count == 2  # had to be re-triggered for "turn three"


@pytest.mark.asyncio
async def test_explicit_end_phrase_ends_follow_up_immediately():
    tags = [WAKE] + UTTERANCE + UTTERANCE + [WAKE] + UTTERANCE
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags,
        transcripts=["turn one", "goodbye", "turn three"],
        llm_script=[_plain_reply("Response one."), _plain_reply("Response three.")],
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 3
    assert speech_streamer.spoken == [
        "Yes?",
        "Response one.",
        "Okay, let me know if you need anything else.",
        "Yes?",
        "Response three.",
    ]
    assert wake_word.detect_count == 2  # ended by "goodbye", re-triggered for "turn three"


@pytest.mark.asyncio
async def test_ignored_transcript_during_follow_up_keeps_window_open():
    tags = [WAKE] + UTTERANCE + UTTERANCE + UTTERANCE  # no WAKE between any of these
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags,
        transcripts=["turn one", "", "turn three"],  # "" -> too short, discarded
        llm_script=[_plain_reply("Response one."), _plain_reply("Response three.")],
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 3
    assert speech_streamer.spoken == ["Yes?", "Response one.", "Response three."]  # the "" produced no reply
    assert wake_word.detect_count == 1  # window survived the discarded transcript


@pytest.mark.asyncio
async def test_max_consecutive_follow_ups_forces_rearm():
    tags = [WAKE] + UTTERANCE + UTTERANCE + [WAKE] + UTTERANCE
    orch, wake_word, stt, speech_streamer = _make_orchestrator(
        tags,
        transcripts=["turn one", "turn two", "turn three"],
        llm_script=[_plain_reply("Response one."), _plain_reply("Response two."), _plain_reply("Response three.")],
        conversation_overrides={"max_consecutive_follow_ups": 1},
    )
    with pytest.raises(_StopTest):
        await orch.run()

    assert stt.call_count == 3
    # turn two consumed the single allotted follow-up; turn three needed
    # a fresh wake word, which the script only supplies if the cap
    # actually took effect after exactly one follow-up.
    assert wake_word.detect_count == 2
    assert speech_streamer.spoken == ["Yes?", "Response one.", "Response two.", "Yes?", "Response three."]
