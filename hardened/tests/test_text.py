import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.utils.text import SentenceChunker


def test_single_delta_full_sentence():
    chunker = SentenceChunker()
    chunks = chunker.feed("Hello there. ")
    assert chunks == ["Hello there."]


def test_incremental_token_deltas():
    chunker = SentenceChunker()
    out = []
    for delta in ["Hel", "lo the", "re. How ", "are you", "?"]:
        out.extend(chunker.feed(delta))
    out.extend(chunker.flush())
    assert out == ["Hello there.", "How are you?"]


def test_does_not_split_on_abbreviation():
    chunker = SentenceChunker()
    out = chunker.feed("Dr. Smith will see you now. ")
    # Should NOT split after "Dr." -- only after "now."
    assert out == ["Dr. Smith will see you now."]


def test_does_not_split_on_decimal_without_trailing_space():
    chunker = SentenceChunker()
    out = chunker.feed("Pi is roughly 3.14 and that's useful. ")
    assert out == ["Pi is roughly 3.14 and that's useful."]


def test_newline_is_a_hard_boundary():
    chunker = SentenceChunker()
    out = chunker.feed("First line\nSecond line")
    assert out == ["First line"]
    out.extend(chunker.flush())
    assert out == ["First line", "Second line"]


def test_flush_emits_remainder_without_terminal_punctuation():
    chunker = SentenceChunker()
    chunker.feed("No terminator here")
    assert chunker.flush() == ["No terminator here"]


def test_flush_on_empty_buffer_emits_nothing():
    chunker = SentenceChunker()
    chunker.feed("Complete sentence. ")
    chunker._extract_ready_chunks()  # buffer already drained by feed()
    assert chunker.flush() == []


def test_multiple_sentences_in_one_delta():
    chunker = SentenceChunker()
    out = chunker.feed("Sure. On it. Done!")
    out.extend(chunker.flush())
    assert out == ["Sure.", "On it.", "Done!"]


def test_runaway_sentence_force_splits_on_max_buffer():
    chunker = SentenceChunker(max_buffer_chars=20)
    long_text = "word " * 20  # 100 chars, no punctuation at all
    out = chunker.feed(long_text)
    assert out, "expected at least one forced split for an unbounded run-on sentence"
    assert all(len(chunk) <= 25 for chunk in out)
