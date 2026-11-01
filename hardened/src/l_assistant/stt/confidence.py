"""
Shared policy for deciding whether a transcript is likely a Whisper
hallucination on near-silent/non-speech audio, rather than a real
utterance -- used by both `whisper_engine.WhisperSTT` (which always has
these signals, straight from faster-whisper's `Segment` objects) and
`groq_cloud_whisper.GroqCloudSTT` (which may or may not, depending on
what Groq's API actually returns -- see that module's docstring).

Why this exists (root cause, not a guess -- see the accompanying commit
notes): a wake-word false-trigger on background noise starts a
recording; WebRTC VAD occasionally misclassifies a noise burst as
"speech" for just long enough to clear `vad.min_utterance_ms`, so a
short, mostly-silent clip gets sent to STT anyway. Whisper is well
documented to hallucinate short, plausible, generic phrases on exactly
this kind of input rather than returning an empty string. The fix isn't
"don't record" (a real, if short, command like "cancel" or "yes" must
still work) -- it's "don't trust text back from audio the model itself
is signaling it didn't think was speech."

Kept as a pure function (no model, no I/O) specifically so it's unit
testable without faster-whisper, torch, or a network call -- see
tests/test_stt_confidence.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SegmentConfidence:
    """The subset of a Whisper segment's own self-reported confidence
    signals this policy uses. Both fields are optional because Groq's
    cloud API may not expose them (unconfirmed at the time this was
    written -- see groq_cloud_whisper.py); faster-whisper always does."""

    no_speech_prob: Optional[float] = None
    avg_logprob: Optional[float] = None


def is_likely_hallucination(
    segments: list[SegmentConfidence],
    no_speech_prob_threshold: float = 0.6,
    avg_logprob_reject_threshold: float = -1.0,
) -> bool:
    """
    True if the available confidence signals agree this transcript
    probably isn't real speech.

    Deliberately conservative -- requires *both* signals to point the
    same way when both are available, rather than either one alone:
    `no_speech_prob` on its own flags plenty of genuine quiet/mumbled
    speech as "probably silence" (it's a coarse, whole-segment estimate),
    and `avg_logprob` on its own flags genuine-but-hard-to-recognize
    words (accents, unusual phrasing, a name) the same way a
    hallucination reads -- low-confidence-but-real. Together they're a
    much stronger signal: the model was both fairly sure there was no
    speech *and* unconfident in the tokens it emitted anyway.

    If a clip has zero segments (empty transcript) or every segment is
    missing both fields (provider doesn't expose them), this returns
    False -- "not enough information to reject" defers to the existing
    length/self-echo checks downstream rather than silently discarding
    audio on a signal that was never actually available. An empty
    `segments` list is exactly what a genuinely-empty transcript
    produces, and that's already handled by
    `stt.min_transcript_chars` in the orchestrator -- this function
    only needs to catch the case where Whisper filled the silence with
    *something*.
    """
    if not segments:
        return False

    usable = [
        s for s in segments if s.no_speech_prob is not None and s.avg_logprob is not None
    ]
    if not usable:
        return False

    avg_no_speech = sum(s.no_speech_prob for s in usable) / len(usable)  # type: ignore[misc]
    avg_logprob = sum(s.avg_logprob for s in usable) / len(usable)  # type: ignore[misc]

    return avg_no_speech >= no_speech_prob_threshold and avg_logprob < avg_logprob_reject_threshold
