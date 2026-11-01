import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.audio.vad import EndpointDetector, FrameVad

_SAMPLE_RATE = 16000
_FRAME_SAMPLES = 1280  # 80ms


def _silence_frame() -> np.ndarray:
    return np.zeros(_FRAME_SAMPLES, dtype=np.int16)


def test_pure_silence_never_sets_had_speech(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("l_assistant.audio.vad.time.monotonic", lambda: fake_now[0])

    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=2)
    detector = EndpointDetector(
        vad, silence_timeout_ms=700, min_utterance_ms=250, max_utterance_seconds=0.5
    )
    frame_duration_s = _FRAME_SAMPLES / _SAMPLE_RATE

    result = None
    for _ in range(20):  # 20 * 80ms = 1.6s of simulated time, well past 0.5s
        fake_now[0] += frame_duration_s
        result = detector.push(_silence_frame(), frame_duration_s)
        if result.should_stop:
            break

    assert result is not None
    assert result.should_stop is True
    assert result.reason == "max_duration"
    assert detector.had_speech is False


def test_endpoint_does_not_stop_on_silence_alone_without_prior_speech():
    """
    Trailing-silence endpointing should never fire before any speech was
    detected -- otherwise the very first frame of an utterance (which is
    silence, before the user starts talking) could end it immediately.
    """
    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=2)
    detector = EndpointDetector(
        vad, silence_timeout_ms=100, min_utterance_ms=250, max_utterance_seconds=10.0
    )
    frame_duration_s = _FRAME_SAMPLES / _SAMPLE_RATE

    # Several silent frames, well past silence_timeout_ms, but no speech
    # was ever detected -- should not report "silence" as a stop reason.
    for _ in range(10):
        result = detector.push(_silence_frame(), frame_duration_s)
        assert result.should_stop is False


def test_reset_clears_had_speech():
    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=2)
    detector = EndpointDetector(
        vad, silence_timeout_ms=700, min_utterance_ms=250, max_utterance_seconds=5.0
    )
    detector._had_speech = True  # simulate mid-utterance state
    detector.reset()
    assert detector.had_speech is False


def test_frame_vad_classifies_silence_as_not_speech():
    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=2)
    assert vad.is_speech(_silence_frame()) is False


def _tone_frame(amplitude: int, samples: int = _FRAME_SAMPLES) -> np.ndarray:
    """A pure tone, not real speech -- used only to drive RMS well above
    or below a min_rms floor. Not meant to reliably pass WebRTC VAD's
    OWN spectral classification (a sine wave doesn't look like voiced
    speech to it), which is exactly why the min_rms tests below use
    aggressiveness=0 (WebRTC VAD's most permissive setting) so the tone
    passes the spectral half of is_speech() unmodified, isolating min_rms
    as the only thing under test.
    """
    t = np.arange(samples)
    return (amplitude * np.sin(2 * np.pi * 220 * t / _SAMPLE_RATE)).astype(np.int16)


def test_min_rms_zero_preserves_original_behavior():
    # Default -- a loud tone must be classified purely by WebRTC VAD,
    # unaffected by any amplitude floor.
    vad_off = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=0, min_rms=0.0)
    loud = _tone_frame(amplitude=20000)
    assert vad_off.is_speech(loud) is True


def test_min_rms_rejects_quiet_frame_regardless_of_spectral_shape():
    quiet = _tone_frame(amplitude=50)  # RMS well under a 500 floor
    vad_with_floor = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=0, min_rms=500.0)
    vad_without_floor = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=0, min_rms=0.0)
    # Same frame, same spectral classifier -- only the floor differs.
    assert vad_without_floor.is_speech(quiet) is True
    assert vad_with_floor.is_speech(quiet) is False


def test_min_rms_still_accepts_loud_frame_above_floor():
    loud = _tone_frame(amplitude=20000)  # RMS well over a 500 floor
    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=0, min_rms=500.0)
    assert vad.is_speech(loud) is True


class _ScriptedVad:
    """Not FrameVad -- a minimal is_speech() stub so the onset-timeout
    tests below can control exactly which pushes register as speech,
    independent of real WebRTC VAD's actual audio classification
    (already covered by test_frame_vad_classifies_silence_as_not_speech)."""

    def __init__(self, speech_flags):
        self._flags = list(speech_flags)

    def is_speech(self, frame: np.ndarray) -> bool:
        return self._flags.pop(0) if self._flags else False


def test_no_speech_started_fires_when_nobody_talks(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("l_assistant.audio.vad.time.monotonic", lambda: fake_now[0])

    vad = _ScriptedVad([False] * 20)
    detector = EndpointDetector(
        vad,
        silence_timeout_ms=700,
        min_utterance_ms=250,
        max_utterance_seconds=20.0,  # deliberately generous -- proves the
        max_wait_for_speech_seconds=1.0,  # SHORTER ceiling is what actually fires
    )
    frame_duration_s = _FRAME_SAMPLES / _SAMPLE_RATE

    result = None
    for _ in range(20):
        fake_now[0] += frame_duration_s
        result = detector.push(_silence_frame(), frame_duration_s)
        if result.should_stop:
            break

    assert result is not None
    assert result.should_stop is True
    assert result.reason == "no_speech_started"
    assert detector.had_speech is False


def test_onset_timeout_does_not_apply_once_speech_has_started(monkeypatch):
    """Once real speech has begun, max_wait_for_speech_seconds must stop
    applying entirely -- max_utterance_seconds governs the rest of the
    capture, exactly as if the parameter had never been set. This is the
    core guarantee follow-up mode depends on: a slow-starting follow-up
    still gets a normal amount of time to finish once it actually
    starts."""
    fake_now = [1000.0]
    monkeypatch.setattr("l_assistant.audio.vad.time.monotonic", lambda: fake_now[0])

    # 5 speech frames (5*80ms=400ms >= min_utterance_ms) then silence --
    # by the time enough trailing silence accumulates to end the
    # utterance naturally, real elapsed time will already be well past
    # max_wait_for_speech_seconds=1.0s, which is exactly the point.
    vad = _ScriptedVad([True] * 5 + [False] * 20)
    detector = EndpointDetector(
        vad,
        silence_timeout_ms=700,
        min_utterance_ms=250,
        max_utterance_seconds=20.0,
        max_wait_for_speech_seconds=1.0,
    )
    frame_duration_s = _FRAME_SAMPLES / _SAMPLE_RATE

    result = None
    for _ in range(30):
        fake_now[0] += frame_duration_s
        result = detector.push(_silence_frame(), frame_duration_s)
        if result.should_stop:
            break

    assert result is not None
    assert result.should_stop is True
    assert result.reason == "silence"  # NOT "no_speech_started"
    assert detector.had_speech is True


def test_max_wait_for_speech_omitted_keeps_original_single_ceiling_behavior():
    """No max_wait_for_speech_seconds passed (the default, and every
    caller before follow-up mode existed) -- must never produce
    "no_speech_started" regardless of how long silence runs, only the
    original "max_duration"/"silence" reasons."""
    vad = FrameVad(sample_rate=_SAMPLE_RATE, aggressiveness=2)
    detector = EndpointDetector(
        vad, silence_timeout_ms=100, min_utterance_ms=250, max_utterance_seconds=0.5
    )
    frame_duration_s = _FRAME_SAMPLES / _SAMPLE_RATE

    result = None
    for _ in range(20):
        result = detector.push(_silence_frame(), frame_duration_s)
        if result.should_stop:
            break

    assert result is not None
    assert result.reason != "no_speech_started"
