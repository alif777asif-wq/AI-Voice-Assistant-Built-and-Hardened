"""
Ties SentenceChunker + PiperTTS + AudioPlayer together into overlapped
"speak while still generating" playback.

This is the single biggest latency win in the whole pipeline: without it,
the assistant would wait for the *entire* LLM response to finish
generating before synthesizing any audio, then wait for the *entire*
response to finish synthesizing before playing any of it. With it, the
user starts hearing the first sentence roughly as soon as the LLM has
produced it, while later sentences are still being generated and
synthesized in the background.

Also the single place mic input gating happens: `speak_stream` mutes the
mic (see `l_assistant.audio.capture.MicrophoneStream.set_muted`) for its entire
duration whenever a `mic` is supplied, and always unmutes in a `finally`
block -- every spoken output in the whole app (streaming LLM responses,
wake acknowledgments, confirmation questions, apologies) funnels through
this one method, so gating it here means no future code path that speaks
something can forget to gate the mic while doing it. The `finally` is
load-bearing: an exception mid-speech that skipped unmuting would leave
the assistant permanently deaf, which is a far worse failure than the
echo problem this exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, List, Optional

from l_assistant.audio.capture import MicrophoneStream
from l_assistant.audio.playback import AudioPlayer
from l_assistant.tts.piper_engine import PiperTTS
from l_assistant.utils.text import SentenceChunker

logger = logging.getLogger(__name__)


class SpeechStreamer:
    def __init__(
        self,
        tts: PiperTTS,
        player: AudioPlayer,
        min_chunk_chars: int = 6,
        synth_queue_size: int = 4,
        mic: Optional[MicrophoneStream] = None,
    ) -> None:
        self._tts = tts
        self._player = player
        self._min_chunk_chars = min_chunk_chars
        self._synth_queue_size = synth_queue_size
        self._mic = mic
        # Every spoken output in the app funnels through speak_stream (see
        # module docstring), so tracking "what did L last say, and
        # when" here -- rather than in whichever caller happens to invoke
        # it -- is what lets Orchestrator._looks_like_self_echo cover
        # every code path automatically, including the plain streaming
        # conversational responses that call speak_stream directly rather
        # than through _speak_complete_text.
        self.last_spoken_text: Optional[str] = None
        self.last_spoken_at: Optional[float] = None

    async def speak_stream(self, text_deltas: AsyncIterator[str]) -> str:
        """
        Consume an async stream of text deltas (as produced by the LLM
        client), synthesize + play completed sentences as soon as they're
        ready, and return the full accumulated response text once
        everything has been spoken -- so the caller can store the exact
        reply in conversation memory.
        """
        if self._mic is not None:
            self._mic.set_muted(True)
        try:
            full_text = await self._speak_stream_unmuted(text_deltas)
            self.last_spoken_text = full_text
            self.last_spoken_at = time.monotonic()
            return full_text
        finally:
            if self._mic is not None:
                self._mic.set_muted(False)

    async def _speak_stream_unmuted(self, text_deltas: AsyncIterator[str]) -> str:
        chunker = SentenceChunker()
        full_text_parts: List[str] = []
        synth_queue: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(
            maxsize=self._synth_queue_size
        )

        async def synthesize_and_enqueue(chunk_text: str) -> None:
            if len(chunk_text) < self._min_chunk_chars:
                # Too short to be worth a separate synthesis round trip
                # (e.g. a stray "Ok" before the real sentence) -- but we
                # still keep it in full_text_parts via the raw delta feed,
                # so nothing is lost from memory, only from audio.
                return
            pcm = await self._tts.synthesize_chunk_async(chunk_text)
            await synth_queue.put(pcm)

        async def producer() -> None:
            try:
                async for delta in text_deltas:
                    if not delta:
                        continue
                    full_text_parts.append(delta)
                    for chunk_text in chunker.feed(delta):
                        await synthesize_and_enqueue(chunk_text)
                for chunk_text in chunker.flush():
                    await synthesize_and_enqueue(chunk_text)
            finally:
                await synth_queue.put(None)  # sentinel: generation finished

        async def consumer() -> None:
            while True:
                pcm = await synth_queue.get()
                if pcm is None:
                    break
                await self._player.play_pcm16(pcm)

        await asyncio.gather(producer(), consumer())

        full_text = "".join(full_text_parts).strip()
        logger.debug("Spoke full response (%d chars)", len(full_text))
        return full_text
