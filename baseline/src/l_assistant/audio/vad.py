"""
Voice activity detection built on WebRTC's VAD, via the `webrtcvad-wheels`
package -- a maintained fork of `webrtcvad` that ships prebuilt binary
wheels for Linux, Windows and macOS, so no C compiler or Visual Studio
Build Tools are ever required to install it (the original `webrtcvad`
package has no Windows wheels on some Python versions and falls back to a
source build, which is exactly the kind of "workaround left to the user"
this project avoids).

WebRTC VAD only accepts 10/20/30 ms frames of 16-bit mono PCM at 8/16/32/48
kHz. The capture layer hands us 80 ms blocks (1280 samples @ 16 kHz), so
`FrameVad.is_speech` slices each block into four 20 ms sub-frames and
applies a majority vote -- accurate enough for endpointing and cheap
enough to run on every single block.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import webrtcvad


class FrameVad:
    """
    Wraps WebRTC VAD's per-20ms-subframe classification with a majority
    vote across an 80ms block, plus an OPTIONAL amplitude floor checked
    BEFORE WebRTC VAD even runs.

    Why the floor exists: WebRTC VAD's classifier is spectral, not purely
    loudness-based -- it's looking for frequency-domain shape that
    resembles voiced speech, not just "is this loud." Broadband
    mechanical noise (a PC fan, HVAC) can have enough speech-like
    spectral structure to pass that classifier fairly often even though
    it's much quieter than a person actually talking into the mic. That
    shows up as EndpointDetector never seeing 700ms of "clean" silence --
    every silence_timeout_ms window gets punctuated by another
    fan-shaped subframe getting voted "speech" -- so an utterance runs
    all the way to max_utterance_seconds instead of ending naturally a
    fraction of a second after the user actually stops talking. Raising
    vote_threshold/aggressiveness (both already at or near their
    ceiling: aggressiveness has no setting past 3) reduces how often any
    single subframe gets misclassified, but doesn't help if the noise is
    LOUD enough, relative to speech, to pass classification consistently
    -- at that point what's missing isn't stricter spectral analysis,
    it's a plain "is this even loud enough to be someone talking"
    pre-check, which is what min_rms is.

    Off by default (min_rms=0.0) because the right value is specific to
    a given room/mic/gain, not something with a universally-correct
    default -- see scripts/vad_calibrate.py for measuring yours.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 3,
        subframe_ms: int = 20,
        vote_threshold: float = 0.75,
        min_rms: float = 0.0,
    ) -> None:
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate
        self._subframe_samples = int(sample_rate * subframe_ms / 1000)
        self._vote_threshold = vote_threshold
        self._min_rms = min_rms

    def is_speech(self, frame: np.ndarray) -> bool:
        """`frame`: int16 mono array whose length is a multiple of the
        sub-frame size (guaranteed by AudioConfig.frame_samples validation)."""
        if self._min_rms > 0.0 and _rms(frame) < self._min_rms:
            return False

        n = self._subframe_samples
        votes = 0
        total = 0
        for start in range(0, len(frame) - n + 1, n):
            sub = frame[start : start + n]
            if self._vad.is_speech(sub.tobytes(), self._sample_rate):
                votes += 1
            total += 1
        if total == 0:
            return False
        return votes >= (total * self._vote_threshold)


def _rms(frame: np.ndarray) -> float:
    """Root-mean-square amplitude of an int16 PCM frame, on the same
    0-32768ish scale as the samples themselves -- see
    scripts/vad_calibrate.py, which reports numbers on this exact scale
    specifically so they're pastable straight into vad.min_rms."""
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))


@dataclass
class EndpointResult:
    should_stop: bool
    reason: str  # "silence" | "max_duration" | "no_speech_started" | ""


class EndpointDetector:
    """
    Stateful end-of-utterance detector used while recording after a wake
    word fires. Tracks accumulated speech and trailing silence in
    wall-clock time (not frame counts), so behavior is identical
    regardless of the configured block size.

    `max_wait_for_speech_seconds`, if given, is a SEPARATE, earlier
    ceiling that only applies before any speech has been seen at all:
    give up (reason="no_speech_started") if nobody starts talking within
    this many seconds, rather than waiting all the way out to
    max_utterance_seconds. Once real speech begins, this ceiling stops
    applying and max_utterance_seconds governs the rest of the capture
    exactly as before -- the two are independent on purpose. This is
    what lets Orchestrator's follow-up-window listening use a short
    leash for "is anyone still talking to me" while still giving an
    actual follow-up command the same generous runway a wake-triggered
    one gets once it starts. Omit it (the default, None) to get the
    original single-ceiling behavior, unchanged -- every caller that
    existed before follow-up mode does this.
    """

    def __init__(
        self,
        frame_vad: FrameVad,
        silence_timeout_ms: int,
        min_utterance_ms: int,
        max_utterance_seconds: float,
        max_wait_for_speech_seconds: Optional[float] = None,
    ) -> None:
        self._vad = frame_vad
        self._silence_timeout_s = silence_timeout_ms / 1000.0
        self._min_utterance_s = min_utterance_ms / 1000.0
        self._max_utterance_s = max_utterance_seconds
        self._max_wait_for_speech_s = max_wait_for_speech_seconds

        self._start_time: Optional[float] = None
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._had_speech = False

    def reset(self) -> None:
        self._start_time = None
        self._speech_seconds = 0.0
        self._silence_seconds = 0.0
        self._had_speech = False

    @property
    def had_speech(self) -> bool:
        """True once at least one frame has been classified as speech."""
        return self._had_speech

    def push(self, frame: np.ndarray, frame_duration_s: float) -> EndpointResult:
        now = time.monotonic()
        if self._start_time is None:
            self._start_time = now
        elapsed = now - self._start_time

        # Classify this frame BEFORE checking either ceiling below --
        # specifically, before checking max_wait_for_speech_s, so the
        # exact frame where speech first starts is the one that flips
        # _had_speech True, rather than losing a race against "gave up
        # waiting" on that same boundary frame.
        if self._vad.is_speech(frame):
            self._speech_seconds += frame_duration_s
            self._silence_seconds = 0.0
            self._had_speech = True
        else:
            self._silence_seconds += frame_duration_s

        if (
            not self._had_speech
            and self._max_wait_for_speech_s is not None
            and elapsed >= self._max_wait_for_speech_s
        ):
            return EndpointResult(True, "no_speech_started")

        if elapsed >= self._max_utterance_s:
            return EndpointResult(True, "max_duration")

        enough_speech = self._speech_seconds >= self._min_utterance_s
        trailing_silence = self._silence_seconds >= self._silence_timeout_s

        if self._had_speech and enough_speech and trailing_silence:
            return EndpointResult(True, "silence")

        return EndpointResult(False, "")
