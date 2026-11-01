import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.stt.confidence import SegmentConfidence, is_likely_hallucination


def test_empty_segments_not_rejected():
    assert is_likely_hallucination([]) is False


def test_missing_fields_not_rejected():
    # Provider doesn't expose the signals at all (e.g. Groq, unconfirmed) --
    # must not reject blind, or every transcript from that provider would
    # silently vanish.
    assert is_likely_hallucination([SegmentConfidence(None, None)]) is False


def test_confident_real_speech_not_rejected():
    # A clear, high-confidence utterance: low no_speech_prob, healthy avg_logprob.
    seg = SegmentConfidence(no_speech_prob=0.05, avg_logprob=-0.2)
    assert is_likely_hallucination([seg]) is False


def test_classic_hallucination_pattern_rejected():
    # What the log's repeating "Pause." almost certainly looked like internally:
    # the model is fairly sure there's no speech, and unconfident in the
    # tokens it emitted anyway.
    seg = SegmentConfidence(no_speech_prob=0.85, avg_logprob=-1.4)
    assert is_likely_hallucination([seg]) is True


def test_quiet_but_real_speech_not_rejected():
    # High no_speech_prob alone (soft-spoken, some room noise) but the
    # model was still confident in what it heard -- must NOT reject, this
    # is exactly the "don't break short real commands" case.
    seg = SegmentConfidence(no_speech_prob=0.7, avg_logprob=-0.3)
    assert is_likely_hallucination([seg]) is False


def test_low_confidence_alone_not_rejected():
    # Unfamiliar word/name/accent: low avg_logprob but the model was sure
    # someone was talking. Must NOT reject -- this is real speech that's
    # just hard to recognize, not silence.
    seg = SegmentConfidence(no_speech_prob=0.1, avg_logprob=-1.6)
    assert is_likely_hallucination([seg]) is False


def test_multi_segment_average():
    segs = [
        SegmentConfidence(no_speech_prob=0.9, avg_logprob=-1.5),
        SegmentConfidence(no_speech_prob=0.9, avg_logprob=-1.5),
    ]
    assert is_likely_hallucination(segs) is True


def test_partial_missing_fields_averages_only_usable_segments():
    segs = [
        SegmentConfidence(no_speech_prob=0.95, avg_logprob=-1.8),
        SegmentConfidence(None, None),  # ignored, not averaged in as 0
    ]
    assert is_likely_hallucination(segs) is True


def test_thresholds_are_tunable():
    seg = SegmentConfidence(no_speech_prob=0.55, avg_logprob=-0.9)
    assert is_likely_hallucination([seg]) is False  # under default thresholds
    assert (
        is_likely_hallucination(
            [seg], no_speech_prob_threshold=0.5, avg_logprob_reject_threshold=-0.8
        )
        is True
    )
