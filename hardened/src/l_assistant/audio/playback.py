"""
Speech + earcon playback via sounddevice, mirroring the capture layer: one
persistent OutputStream, with blocking writes issued from a worker thread
(`asyncio.to_thread`) so the event loop is never blocked on audio I/O.

Using sounddevice for playback -- rather than shelling out to a
platform-specific CLI player like `aplay`/`paplay` on Linux or nothing at
all on Windows -- means this file has zero OS branches. It is the same
code path on both target platforms.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioPlayer:
    def __init__(
        self,
        sample_rate: int,
        channels: int = 1,
        device: Optional[str] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._device = device
        self._stream: Optional[sd.OutputStream] = None

    async def __aenter__(self) -> "AudioPlayer":
        # No `extra_settings=sd.WasapiSettings(exclusive=True)` --
        # deliberately, and this is the side that actually matters:
        # exclusive-mode WASAPI playback claims the output device for
        # itself, which on Windows means everything else trying to play
        # audio (Spotify, a browser tab, a game) gets silently muted or
        # fails outright for as long as this stream is open. Shared mode
        # -- sounddevice/PortAudio's default absent this setting -- mixes
        # with whatever else is already playing instead, which is the
        # actual requirement behind "never interrupts ongoing media
        # playback": not some detection logic deciding when to duck
        # other audio, just never taking exclusive ownership of the
        # device in the first place. Don't add WasapiSettings(exclusive=
        # True) here without deliberately deciding to break that.
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            device=self._device,
        )
        self._stream.start()
        logger.info(
            "Audio output opened (device=%s, %d Hz)",
            self._device or "system default",
            self.sample_rate,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def play_pcm16(self, pcm_bytes: bytes) -> None:
        """Blocking write of raw 16-bit PCM, off the event loop thread."""
        if not pcm_bytes or self._stream is None:
            return
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, self.channels)
        await asyncio.to_thread(self._stream.write, audio)

    def stop_immediately(self) -> None:
        """
        Hard-stop mid-playback. Not wired into the default half-duplex
        pipeline (see orchestrator.py docstring on barge-in), but exposed
        here as the extension point a future full-duplex mode would use.
        """
        if self._stream is not None:
            self._stream.abort()
            self._stream.start()


def generate_earcon(
    sample_rate: int,
    frequency: float = 880.0,
    duration_s: float = 0.12,
    volume: float = 0.25,
) -> bytes:
    """
    A short synthesized chime played the instant a wake word is detected,
    so the user gets immediate feedback without a bundled audio asset
    file or a round trip through the TTS engine (which would add
    hundreds of milliseconds of perceived latency for zero benefit here).
    """
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    envelope = np.sin(np.pi * t / duration_s)  # raised-sine in/out, no click
    tone = np.sin(2 * np.pi * frequency * t) * envelope * volume
    pcm = (tone * 32767).astype(np.int16)
    return pcm.tobytes()
