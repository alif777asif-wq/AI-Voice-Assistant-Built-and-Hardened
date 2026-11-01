import sys
import time
import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.config import AppConfig
from l_assistant.llm.base import ChatWithToolsResult, ToolCall
from l_assistant.pipeline.orchestrator import Orchestrator
from l_assistant.pipeline.state import AgentState, PendingConfirmation
from l_assistant.tools.registry import ToolDispatcher
from l_assistant.tools.system_tools import SystemToolsEngine, ToolError


class FakeSpeechStreamer:
    """Records what it was asked to speak; returns the joined text like the real one."""

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


class FakeMic:
    """Tracks drain() calls (and their order relative to other events) without real audio."""

    def __init__(self) -> None:
        self.drain_call_count = 0
        self.event_log: List[str] = []

    def drain(self) -> None:
        self.drain_call_count += 1
        self.event_log.append("drain")


class ScriptedLlm:
    """Fake LlmClient whose chat_with_tools() returns a pre-scripted sequence of results."""

    def __init__(self, script: List[ChatWithToolsResult]) -> None:
        self._script = list(script)
        self.calls: List[List[Dict[str, Any]]] = []

    async def ensure_ready(self) -> None:
        pass

    def stream_chat(self, messages):
        raise NotImplementedError("not exercised by tool-calling tests")

    async def chat_with_tools(self, messages, tools) -> ChatWithToolsResult:
        self.calls.append([dict(m) for m in messages])
        return self._script.pop(0)

    async def summarize(self, text: str, max_words: int = 120) -> str:
        return ""


class AlwaysSucceedsDispatcher:
    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        return "ok"


def _make_orchestrator(llm, speech_streamer, tool_dispatcher, config=None) -> Orchestrator:
    config = config or AppConfig(tools={"enabled": True, "max_tool_iterations": 4})
    # mic/player/wake_word/frame_vad/stt/tts/memory are never touched by
    # _run_tool_loop / _resolve_pending_confirmation / _speak_complete_text
    # (the only methods these tests call directly), so plain None stand-ins
    # are enough -- no audio hardware or model loading needed for this
    # orchestration-logic test suite.
    return Orchestrator(
        config=config,
        mic=None,
        player=None,
        wake_word=None,
        frame_vad=None,
        stt=None,
        llm=llm,
        tts=None,
        speech_streamer=speech_streamer,
        memory=None,
        tool_dispatcher=tool_dispatcher,
        tool_schemas=[{"type": "function", "function": {"name": "dummy"}}],
    )


async def test_plain_response_no_tool_call_speaks_directly():
    llm = ScriptedLlm(
        [ChatWithToolsResult(content="Hello there!", tool_calls=[], raw_assistant_message={})]
    )
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(llm, streamer, tool_dispatcher=AlwaysSucceedsDispatcher())

    result = await orch._run_tool_loop([{"role": "user", "content": "hi"}])

    assert result == "Hello there!"
    assert streamer.spoken == ["Hello there!"]
    assert orch.state == AgentState.SPEAKING
    assert len(llm.calls) == 1


async def test_tool_call_then_final_answer(tmp_path, monkeypatch):
    engine = SystemToolsEngine(allowed_roots=[str(tmp_path)])
    dispatcher = ToolDispatcher(engine)

    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))

    call = ToolCall(id="call_1", name="search_web", arguments={"query": "openWakeWord"})
    llm = ScriptedLlm(
        [
            ChatWithToolsResult(
                content="",
                tool_calls=[call],
                raw_assistant_message={"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            ),
            ChatWithToolsResult(content="Done searching!", tool_calls=[], raw_assistant_message={}),
        ]
    )
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(llm, streamer, dispatcher)

    messages = [{"role": "user", "content": "search for openWakeWord"}]
    result = await orch._run_tool_loop(messages)

    assert result == "Done searching!"
    assert opened["url"].endswith("openWakeWord")
    assert len(llm.calls) == 2
    second_call_messages = llm.calls[1]
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in second_call_messages
    )


async def test_confirmation_required_sets_pending_and_speaks_question(tmp_path):
    engine = SystemToolsEngine(allowed_roots=[str(tmp_path)])
    dispatcher = ToolDispatcher(engine)
    target = tmp_path / "important.txt"
    target.write_text("keep me")

    call = ToolCall(
        id="call_1", name="file_operation", arguments={"action": "delete", "source": str(target)}
    )
    llm = ScriptedLlm(
        [
            ChatWithToolsResult(
                content="",
                tool_calls=[call],
                raw_assistant_message={"role": "assistant", "tool_calls": [{"id": "call_1"}]},
            )
        ]
    )
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(llm, streamer, dispatcher)

    result = await orch._run_tool_loop([{"role": "user", "content": "delete important.txt"}])

    assert "Delete" in result
    assert orch._pending_confirmation is not None
    assert orch._pending_confirmation.tool_name == "file_operation"
    assert target.exists()  # not actually deleted yet -- only asked about


async def test_max_iterations_bounds_a_model_that_never_stops_calling_tools():
    call = ToolCall(id="call_1", name="search_web", arguments={"query": "x"})
    script = [
        ChatWithToolsResult(
            content="",
            tool_calls=[call],
            raw_assistant_message={"role": "assistant", "tool_calls": [{"id": "call_1"}]},
        )
        for _ in range(10)
    ]
    llm = ScriptedLlm(script)
    streamer = FakeSpeechStreamer()
    config = AppConfig(tools={"enabled": True, "max_tool_iterations": 3})
    orch = _make_orchestrator(llm, streamer, AlwaysSucceedsDispatcher(), config=config)

    result = await orch._run_tool_loop([{"role": "user", "content": "loop forever"}])

    assert len(llm.calls) == 3  # bounded -- did not hang or loop past the limit
    assert "wasn't able to finish" in result.lower()


async def test_resolve_pending_confirmation_yes_executes_tool(tmp_path):
    engine = SystemToolsEngine(allowed_roots=[str(tmp_path)])
    dispatcher = ToolDispatcher(engine)
    target = tmp_path / "bye.txt"
    target.write_text("gone soon")

    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, dispatcher)
    orch._pending_confirmation = PendingConfirmation(
        tool_name="file_operation",
        arguments={"action": "delete", "source": str(target)},
        question="Delete bye.txt?",
    )

    await orch._resolve_pending_confirmation("yes")

    assert not target.exists()
    assert orch._pending_confirmation is None
    assert "trash" in streamer.spoken[-1].lower()


async def test_resolve_pending_confirmation_no_cancels_and_does_not_delete(tmp_path):
    engine = SystemToolsEngine(allowed_roots=[str(tmp_path)])
    dispatcher = ToolDispatcher(engine)
    target = tmp_path / "stay.txt"
    target.write_text("still here")

    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, dispatcher)
    orch._pending_confirmation = PendingConfirmation(
        tool_name="file_operation",
        arguments={"action": "delete", "source": str(target)},
        question="Delete stay.txt?",
    )

    await orch._resolve_pending_confirmation("no thanks")

    assert target.exists()
    assert streamer.spoken[-1] == "Okay, cancelled."
    assert orch._pending_confirmation is None


async def test_resolve_pending_confirmation_ambiguous_reply_treated_as_decline(tmp_path):
    engine = SystemToolsEngine(allowed_roots=[str(tmp_path)])
    dispatcher = ToolDispatcher(engine)
    target = tmp_path / "ambiguous.txt"
    target.write_text("data")

    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, dispatcher)
    orch._pending_confirmation = PendingConfirmation(
        tool_name="file_operation",
        arguments={"action": "delete", "source": str(target)},
        question="Delete ambiguous.txt?",
    )

    # Neither a clear "yes" nor "no" -- must fail safe (not delete).
    await orch._resolve_pending_confirmation("what do you mean")

    assert target.exists()
    assert streamer.spoken[-1] == "Okay, cancelled."


# ---- _settle_after_speech (the self-hearing / echo fix) --------------------
#
# Regression coverage for a real bug: the mic was only ever drained after a
# *complete* turn, never between "L finishes speaking" and "L starts
# listening" -- so the tail of L's own TTS output (the wake ack, or a
# confirmation question) got captured and transcribed as if it were the
# user's command. See VadConfig.post_speech_settle_ms and this method's
# docstring in orchestrator.py.


async def test_settle_after_speech_drains_before_and_after_the_delay(monkeypatch):
    mic = FakeMic()

    async def fake_sleep(seconds: float) -> None:
        mic.event_log.append(f"sleep({seconds})")

    monkeypatch.setattr("l_assistant.pipeline.orchestrator.asyncio.sleep", fake_sleep)

    config = AppConfig(vad={"post_speech_settle_ms": 250})
    orch = _make_orchestrator(ScriptedLlm([]), FakeSpeechStreamer(), AlwaysSucceedsDispatcher(), config=config)
    orch._mic = mic

    await orch._settle_after_speech()

    assert mic.drain_call_count == 2
    # Order matters: drain the backlog from *during* playback, wait for the
    # output-buffer/room-reverb tail, then drain whatever arrived *during*
    # that wait too -- not drain-then-drain, and not sleep-then-single-drain.
    assert mic.event_log == ["drain", "sleep(0.25)", "drain"]


async def test_settle_after_speech_skips_sleep_when_configured_to_zero(monkeypatch):
    mic = FakeMic()
    sleep_was_called = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleep_was_called
        sleep_was_called = True

    monkeypatch.setattr("l_assistant.pipeline.orchestrator.asyncio.sleep", fake_sleep)

    config = AppConfig(vad={"post_speech_settle_ms": 0})
    orch = _make_orchestrator(ScriptedLlm([]), FakeSpeechStreamer(), AlwaysSucceedsDispatcher(), config=config)
    orch._mic = mic

    await orch._settle_after_speech()

    assert mic.drain_call_count == 2  # still drains both sides, just no wait
    assert sleep_was_called is False


# ---- _looks_like_self_echo (the third defense layer) -----------------------
#
# Muting the mic during playback (SpeechStreamer) and the settle-drain
# (_settle_after_speech) are the primary defenses; this is the last-resort
# check for anything that still leaks through. Grounded in the actual
# text_similarity() scores for these phrase pairs, not assumed numbers --
# see the module docstring in utils/text.py for why difflib is enough here.


def test_self_echo_detected_for_near_identical_transcript():
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher())
    streamer.last_spoken_text = "Yes, boss."
    streamer.last_spoken_at = time.monotonic()  # just now

    # Punctuation/casing differ (a realistic STT rendering), wording doesn't.
    assert orch._looks_like_self_echo("yes boss") is True


def test_self_echo_not_flagged_for_genuinely_different_text():
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher())
    streamer.last_spoken_text = "Yes, boss."
    streamer.last_spoken_at = time.monotonic()

    # A real, unrelated command spoken shortly after the ack must not be
    # discarded -- this is the false-positive risk of any similarity check.
    assert orch._looks_like_self_echo("open notepad") is False


def test_self_echo_confirmation_question_not_confused_with_a_real_answer():
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher())
    streamer.last_spoken_text = "Delete the file important.txt? This can't be undone."
    streamer.last_spoken_at = time.monotonic()

    # A real "yes, delete it" answer to L's own question must not be
    # mistaken for an echo of that question, even though L just said
    # something related moments before.
    assert orch._looks_like_self_echo("yes delete it") is False


def test_self_echo_expires_after_refractory_window(monkeypatch):
    streamer = FakeSpeechStreamer()
    config = AppConfig(vad={"self_echo_refractory_seconds": 4.0})
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher(), config=config)
    streamer.last_spoken_text = "Yes, boss."

    fake_now = [1000.0]
    monkeypatch.setattr("l_assistant.pipeline.orchestrator.time.monotonic", lambda: fake_now[0])
    streamer.last_spoken_at = fake_now[0]

    fake_now[0] += 5.0  # past the 4s refractory window
    # Identical text, but too much time has passed -- must not be flagged.
    assert orch._looks_like_self_echo("Yes, boss.") is False


def test_self_echo_none_when_nothing_spoken_yet():
    streamer = FakeSpeechStreamer()  # last_spoken_text/at both None, fresh session
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher())

    assert orch._looks_like_self_echo("anything at all") is False


async def test_run_turn_style_discard_does_not_touch_pending_confirmation():
    # If a garbled echo of a confirmation *question* leaks through, the
    # pending confirmation itself must survive the discard so the next
    # real utterance still resolves it, rather than the state being lost.
    streamer = FakeSpeechStreamer()
    orch = _make_orchestrator(ScriptedLlm([]), streamer, AlwaysSucceedsDispatcher())
    orch._pending_confirmation = PendingConfirmation(
        tool_name="file_operation",
        arguments={"action": "delete", "source": "/home/user/x.txt"},
        question="Delete the file x.txt? This can't be undone.",
    )
    streamer.last_spoken_text = "Delete the file x.txt? This can't be undone."
    streamer.last_spoken_at = time.monotonic()

    assert orch._looks_like_self_echo("delete the file cant be undone") is True
    # The check itself doesn't clear state -- _run_turn's early return does,
    # by simply not calling _resolve_pending_confirmation this cycle.
    assert orch._pending_confirmation is not None


async def test_slow_tool_call_does_not_block_the_event_loop():
    """Proves the run_in_executor wrapping in _run_tool_loop actually
    does something, not just that nothing broke: a tool whose execute()
    does a REAL blocking sleep (time.sleep, not asyncio.sleep -- the
    exact shape of run_command's subprocess.run or open_website's
    yt-dlp network call) must not prevent an unrelated concurrent task
    from making progress. Before the executor wrap, this test would
    show background_ticks staying empty until the tool call fully
    returned; time.sleep on the event-loop thread blocks everything
    equally, no matter how many concurrent asyncio tasks are scheduled."""
    import time as time_module

    class SlowDispatcher:
        def execute(self, tool_name: str, arguments: dict) -> str:
            time_module.sleep(0.2)
            return "done"

    llm = ScriptedLlm(
        [
            ChatWithToolsResult(
                content="",
                tool_calls=[ToolCall(id="call_1", name="slow_tool", arguments={})],
                raw_assistant_message={"role": "assistant", "tool_calls": []},
            ),
            ChatWithToolsResult(content="Finished.", tool_calls=[], raw_assistant_message={}),
        ]
    )
    orch = _make_orchestrator(llm, FakeSpeechStreamer(), SlowDispatcher())

    background_ticks: list = []

    async def ticker() -> None:
        # Deliberately more ticks than can complete within the 0.2s
        # blocking window (20 * 0.02s = 0.4s) -- a partial count checked
        # DURING the blocking call is informative; if the ticker could
        # finish all its ticks regardless of timing, checking after
        # completion (the bug in this test's first draft) can't tell
        # "ran concurrently" apart from "ran entirely afterward", since
        # both end up at the same final count.
        for i in range(20):
            await asyncio.sleep(0.02)
            background_ticks.append(i)

    ticker_task = asyncio.create_task(ticker())
    result = await orch._run_tool_loop([{"role": "user", "content": "do the slow thing"}])
    # Snapshot taken THE INSTANT _run_tool_loop returns, before the
    # ticker is given any further chance to run -- this is what actually
    # distinguishes "the event loop was free for those ~0.2s" (many
    # ticks already landed) from "the event loop was frozen for those
    # ~0.2s" (zero ticks could have landed, since the ticker task never
    # got scheduled at all until the blocking call released the thread).
    ticks_during_blocking_call = len(background_ticks)
    ticker_task.cancel()

    assert result == "Finished."
    assert ticks_during_blocking_call >= 5, (
        f"expected several ticker iterations to interleave with the "
        f"~0.2s blocking tool call, got {ticks_during_blocking_call} -- "
        f"the event loop was blocked instead of staying free"
    )


# ============================================================= user profile


class _FakeMemoryForProfileTest:
    """Just enough of ConversationMemory's interface for _run_turn to
    complete without touching real SQLite -- this test is about what
    system message reaches the LLM, not about memory persistence."""

    async def add_message(self, role: str, text: str) -> None:
        pass

    async def get_context_messages(self):
        return []

    async def maybe_compact(self, summarize_fn) -> None:
        pass


class _FakePlayerForProfileTest:
    sample_rate = 22050

    async def play_pcm16(self, pcm: bytes) -> None:
        pass


class _FakeSttForProfileTest:
    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return self._transcript


async def test_run_turn_injects_known_user_facts_into_system_prompt(tmp_path):
    from l_assistant.memory.user_profile import UserProfileStore

    profile = UserProfileStore(tmp_path / "profile.json")
    profile.remember("name", "Alif")

    llm = ScriptedLlm([ChatWithToolsResult(content="Hi Alif.", tool_calls=[], raw_assistant_message={})])
    orch = _make_orchestrator(llm, FakeSpeechStreamer(), AlwaysSucceedsDispatcher())
    orch._memory = _FakeMemoryForProfileTest()
    orch._player = _FakePlayerForProfileTest()
    orch._stt = _FakeSttForProfileTest("hello")
    orch._user_profile = profile

    await orch._run_turn(b"\x00\x00" * 8000)

    assert len(llm.calls) == 1
    system_message = llm.calls[0][0]
    assert system_message["role"] == "system"
    assert "name: Alif" in system_message["content"]


async def test_run_turn_with_empty_profile_adds_no_fragment(tmp_path):
    from l_assistant.memory.user_profile import UserProfileStore

    profile = UserProfileStore(tmp_path / "profile.json")  # never remember() called

    llm = ScriptedLlm([ChatWithToolsResult(content="Hi.", tool_calls=[], raw_assistant_message={})])
    orch = _make_orchestrator(llm, FakeSpeechStreamer(), AlwaysSucceedsDispatcher())
    orch._memory = _FakeMemoryForProfileTest()
    orch._player = _FakePlayerForProfileTest()
    orch._stt = _FakeSttForProfileTest("hello")
    orch._user_profile = profile

    await orch._run_turn(b"\x00\x00" * 8000)

    system_message = llm.calls[0][0]
    assert system_message["content"] == orch._config.llm.system_prompt  # unchanged, no fragment appended


async def test_run_turn_without_a_profile_configured_works_unchanged(tmp_path):
    # user_profile=None (the default) -- must not error, must not alter
    # the system prompt at all.
    llm = ScriptedLlm([ChatWithToolsResult(content="Hi.", tool_calls=[], raw_assistant_message={})])
    orch = _make_orchestrator(llm, FakeSpeechStreamer(), AlwaysSucceedsDispatcher())
    orch._memory = _FakeMemoryForProfileTest()
    orch._player = _FakePlayerForProfileTest()
    orch._stt = _FakeSttForProfileTest("hello")
    assert orch._user_profile is None

    await orch._run_turn(b"\x00\x00" * 8000)

    assert llm.calls[0][0]["content"] == orch._config.llm.system_prompt


def test_remember_and_forget_user_fact_tools(tmp_path):
    from l_assistant.memory.user_profile import UserProfileStore

    profile = UserProfileStore(tmp_path / "profile.json")
    engine = SystemToolsEngine(user_profile=profile)

    result = engine.remember_user_fact(key="name", value="Alif")
    assert "Alif" in result
    assert profile.all_facts() == {"name": "Alif"}

    result = engine.forget_user_fact(key="name")
    assert "forgot" in result.lower()
    assert profile.all_facts() == {}


def test_forget_user_fact_nothing_remembered_is_not_an_error(tmp_path):
    from l_assistant.memory.user_profile import UserProfileStore

    profile = UserProfileStore(tmp_path / "profile.json")
    engine = SystemToolsEngine(user_profile=profile)
    result = engine.forget_user_fact(key="nonexistent")
    assert "didn't have anything" in result.lower()


def test_remember_user_fact_without_profile_configured_raises():
    engine = SystemToolsEngine()  # user_profile=None, the default
    with pytest.raises(ToolError, match="isn't configured"):
        engine.remember_user_fact(key="name", value="Alif")


