"""
SentenceChunker turns a stream of small text deltas (as they arrive token
by token from the LLM) into complete, speakable chunks as early as
possible. This is the piece that makes overlapped "speak while still
generating" playback possible: the moment the model finishes a sentence,
that sentence is handed to TTS while generation of the next one continues
in the background.

The splitting logic is deliberately conservative: it only cuts at a
sentence-ending punctuation mark that is *already* followed by whitespace
in the buffer (so we never guess whether "3." is the end of a sentence or
the start of a decimal / list item -- we simply wait one more character
to find out), and it skips over a small list of common abbreviations so
"Dr. Smith is here." isn't split after "Dr.".
"""

from __future__ import annotations

import difflib
import re
from typing import List

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "approx", "no", "fig", "vol", "gen", "col", "capt",
}

# A confirmed boundary: sentence punctuation immediately followed by
# whitespace (so the character *after* the punctuation is already known),
# or a newline, which is always a hard boundary.
_BOUNDARY_RE = re.compile(r"([.!?])(\s+)|(\n+)")

# Used to look at the word immediately before a candidate boundary, to
# check it against the abbreviation list.
_LAST_WORD_RE = re.compile(r"([A-Za-z][A-Za-z.]*)$")


class SentenceChunker:
    def __init__(self, max_buffer_chars: int = 400) -> None:
        self._buffer = ""
        self._max_buffer_chars = max_buffer_chars

    def feed(self, delta: str) -> List[str]:
        """Add a text delta; return zero or more completed chunks."""
        if not delta:
            return []
        self._buffer += delta
        return self._extract_ready_chunks()

    def flush(self) -> List[str]:
        """Call once the source stream has ended; emits any remainder."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []

    def _extract_ready_chunks(self) -> List[str]:
        chunks: List[str] = []

        while True:
            split_at = self._find_first_confirmed_boundary(self._buffer)

            if split_at is None:
                if len(self._buffer) >= self._max_buffer_chars:
                    # Runaway sentence with no punctuation (rare, but
                    # unbounded latency is worse than an imperfect cut).
                    # Cut at the last whitespace so we don't break a word.
                    cut = self._buffer.rfind(" ", 0, self._max_buffer_chars)
                    if cut <= 0:
                        cut = self._max_buffer_chars
                    chunk = self._buffer[:cut].strip()
                    self._buffer = self._buffer[cut:].lstrip()
                    if chunk:
                        chunks.append(chunk)
                    continue
                break

            chunk = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:].lstrip()
            if chunk:
                chunks.append(chunk)
            # Keep scanning in case multiple short sentences already
            # arrived in one delta (e.g. "Sure. On it.").

        return chunks

    def _find_first_confirmed_boundary(self, text: str) -> "int | None":
        """
        Returns the end index of the *first* confirmed sentence boundary
        in `text`, or None if there isn't one yet. Finding the first
        (rather than scanning for the last) is what lets multiple
        complete sentences that arrived in a single delta -- e.g. "Sure.
        On it." -- be peeled off and spoken one at a time instead of
        being queued as one merged chunk.
        """
        for match in _BOUNDARY_RE.finditer(text):
            if match.group(3):  # newline group: always a hard boundary
                return match.end()

            punct_end = match.end(1)
            preceding = text[:punct_end]
            word_match = _LAST_WORD_RE.search(preceding[:-1])  # drop punct
            word = (word_match.group(1) if word_match else "").lower()
            if word in _ABBREVIATIONS:
                continue
            return match.end()

        return None


_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_for_comparison(text: str) -> str:
    """
    Lowercase, strip punctuation, collapse whitespace -- for comparing two
    utterances by their words alone, ignoring casing/punctuation
    differences that don't reflect a real difference in what was said
    (e.g. "Yes, boss." vs "yes boss").
    """
    return " ".join(_WORD_RE.findall(text.lower()))


def text_similarity(a: str, b: str) -> float:
    """
    A 0-1 similarity score between two strings, robust to punctuation and
    casing differences. Used by `Orchestrator._looks_like_self_echo` to
    detect when a freshly transcribed utterance is actually a garbled
    re-hearing of something L just said, not a genuine new request.
    Not a real NLP similarity metric -- difflib's ratio() on normalized
    text is sufficient for "is this close to one known string", and
    pulling in a proper string-similarity or embedding library would be a
    heavy, unnecessary dependency for that narrow a job.
    """
    norm_a, norm_b = normalize_for_comparison(a), normalize_for_comparison(b)
    if not norm_a or not norm_b:
        return 0.0
    return difflib.SequenceMatcher(None, norm_a, norm_b).ratio()

