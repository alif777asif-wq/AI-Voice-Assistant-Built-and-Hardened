"""
Microphone capture bridged into asyncio.

sounddevice runs its callback on PortAudio's own OS thread, not on the
asyncio event loop. `MicrophoneStream` bridges that callback into an
asyncio.Queue via `loop.call_soon_threadsafe`, so the rest of the pipeline
can simply `async for frame in mic.frames(): ...` without ever touching
threads directly.

Frames are fixed-size int16 mono PCM blocks (`frame_samples`, 80 ms by
default). The same block feeds openWakeWord directly and is sliced into
20 ms sub-frames for WebRTC VAD (see `l_assistant.audio.vad`), so there is exactly
one capture stream open for the whole app -- no separate "wake word mic"
and "dictation mic".

This module is identical on Linux and Windows: sounddevice's PortAudio
backend is bundled automatically for both via the pip wheel (Linux still
needs the system `libportaudio2` shared library present -- see README).

Input gating: `set_muted(True)` makes the callback drop incoming frames
at the source instead of queueing them, and `set_muted(False)` reverses
it. This is the primary defense against L hearing its own TTS output
through the speakers (see `l_assistant.tts.streamer.SpeechStreamer`, which mutes
for the full duration of every spoken response) -- muting at the source
means audio played while muted is never captured at all, not captured
and then discarded after the fact. `drain()` remains the second layer,
covering the brief acoustic tail after unmuting (output buffer latency,
room reverb) that muting alone can't reach since it ends exactly when
playback does, not when the room goes quiet.

Toggling `_muted` from the asyncio side while it's read on the PortAudio
callback thread is a plain bool, not a lock -- safe here because it's a
single attribute read/write (atomic under the GIL) and the callback only
ever reads it, never read-modifies-writes it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class MicrophoneStream:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_samples: int = 1280,
        device: Optional[str] = None,
        queue_maxsize: int = 32,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_samples = frame_samples
        self._device = device
        self._stream: Optional[sd.InputStream] = None
        self._queue: "asyncio.Queue[np.ndarray]" = asyncio.Queue(maxsize=queue_maxsize)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._dropped_frames = 0
        self._muted_frames_dropped = 0
        self._muted = False

    def set_muted(self, muted: bool) -> None:
        """
        Gate capture at the source. While muted, the PortAudio callback
        drops frames before they're ever queued -- audio played during a
        muted window is not captured, not "captured then discarded".
        """
        self._muted = muted

    @property
    def muted(self) -> bool:
        return self._muted

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            # Overflows/underflows land here. Log only -- never raise
            # inside the PortAudio callback thread, that would kill audio.
            logger.debug("sounddevice input status: %s", status)
        if self._muted:
            self._muted_frames_dropped += 1
            return
        frame = indata[:, 0].copy()  # mono int16, decoupled from PA's buffer
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._enqueue_nowait, frame)

    def _enqueue_nowait(self, frame: np.ndarray) -> None:
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Pipeline is momentarily behind (e.g. a GC pause or a slow
            # STT/LLM call blocking the loop). Drop the oldest frame rather
            # than let capture latency creep up indefinitely.
            self._dropped_frames += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except asyncio.QueueEmpty:
                pass

    async def __aenter__(self) -> "MicrophoneStream":
        self._loop = asyncio.get_running_loop()
        # No `extra_settings=sd.WasapiSettings(exclusive=True)` here --
        # deliberately. Exclusive mode would let this stream claim sole
        # ownership of the input device, which for a MICROPHONE is
        # mostly moot (nothing else is usually recording from it
        # simultaneously) but is the wrong default to reach for on
        # general principle: this app should never be the reason
        # another one can't also use the audio subsystem. WASAPI shared
        # mode -- sounddevice/PortAudio's default absent this setting --
        # is what actually matters on the PLAYBACK side (see
        # playback.py); documented here too since it's easy to assume
        # "exclusive" is the safer/more "native" choice when it's
        # actually the one that breaks coexistence with other apps.
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.frame_samples,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "Microphone opened (device=%s, %d Hz, %d-sample blocks / %.0f ms)",
            self._device or "system default",
            self.sample_rate,
            self.frame_samples,
            1000 * self.frame_samples / self.sample_rate,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._dropped_frames:
            logger.warning(
                "Dropped %d microphone frames due to pipeline backpressure",
                self._dropped_frames,
            )
        if self._muted_frames_dropped:
            logger.debug(
                "Gated %d microphone frames while muted during TTS playback",
                self._muted_frames_dropped,
            )

    def drain(self) -> None:
        """Discard any buffered frames (used when re-arming after a turn)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def frames(self) -> AsyncIterator[np.ndarray]:
        """Yield fixed-size int16 mono frames until cancelled or closed."""
        while self._stream is not None:
            frame = await self._queue.get()
            yield frame
