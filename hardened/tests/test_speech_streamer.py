import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.tts.streamer import SpeechStreamer


class FakeMic:
    def __init__(self) -> None:
        self.mute_calls = []  # ordered sequence of True/False

    def set_muted(self, muted: bool) -> None:
        self.mute_calls.append(muted)


class FakePiperTTS:
    async def synthesize_chunk_async(self, text: str) -> bytes:
        return b"\x00\x00" * 10  # short dummy PCM, content doesn't matter here


class FailingTTS:
    async def synthesize_chunk_async(self, text: str) -> bytes:
        raise RuntimeError("synthesis boom")


class FakeAudioPlayer:
    def __init__(self) -> None:
        self.played = []

    async def play_pcm16(self, pcm: bytes) -> None:
        self.played.append(pcm)


async def _one_shot(text: str) -> AsyncIterator[str]:
    yield text


async def test_speak_stream_mutes_then_unmutes_mic_on_success():
    mic = FakeMic()
    streamer = SpeechStreamer(tts=FakePiperTTS(), player=FakeAudioPlayer(), mic=mic)

    result = await streamer.speak_stream(_one_shot("Hello there."))

    assert result == "Hello there."
    assert mic.mute_calls == [True, False]


async def test_speak_stream_unmutes_mic_even_when_synthesis_raises():
    # The critical safety property: an exception mid-speech must not leave
    # the mic permanently muted, or the assistant goes silently deaf for
    # the rest of the session -- a far worse failure than the self-echo
    # problem muting exists to prevent.
    mic = FakeMic()
    streamer = SpeechStreamer(tts=FailingTTS(), player=FakeAudioPlayer(), mic=mic)

    with pytest.raises(RuntimeError):
        await streamer.speak_stream(_one_shot("This will fail badly."))

    assert mic.mute_calls == [True, False]


async def test_speak_stream_without_a_mic_does_not_crash():
    streamer = SpeechStreamer(tts=FakePiperTTS(), player=FakeAudioPlayer(), mic=None)

    result = await streamer.speak_stream(_one_shot("No mic wired up."))

    assert result == "No mic wired up."


async def test_multiple_speak_stream_calls_each_mute_and_unmute_independently():
    mic = FakeMic()
    streamer = SpeechStreamer(tts=FakePiperTTS(), player=FakeAudioPlayer(), mic=mic)

    await streamer.speak_stream(_one_shot("First."))
    await streamer.speak_stream(_one_shot("Second."))

    assert mic.mute_calls == [True, False, True, False]
