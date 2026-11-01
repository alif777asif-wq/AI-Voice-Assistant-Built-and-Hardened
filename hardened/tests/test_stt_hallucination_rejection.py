"""
Integration tests for the two STT backends' use of
l_assistant.stt.confidence.is_likely_hallucination -- these mock the model /
HTTP layer (no real faster-whisper load, no real network call) but
otherwise exercise the actual transcribe() code path, so a mistake in
*how* the policy function gets wired up (wrong field names, wrong
list vs. dict, swallowing the wrong exception) would show up here even
though tests/test_stt_confidence.py already proves the policy itself is
correct in isolation.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.stt.groq_cloud_whisper import GroqCloudSTT
from l_assistant.stt.whisper_engine import WhisperSTT

PCM = b"\x00\x01" * 8000  # 1s of fake 16-bit mono PCM @ 16kHz, content irrelevant


# ---------------------------------------------------------------- Whisper --


def _make_whisper_stt(segments, language_probability=0.9):
    """Build a WhisperSTT with a stub model, skipping the real (slow, network-
    fetching) model load in __init__."""
    stt = WhisperSTT.__new__(WhisperSTT)
    stt._beam_size = 1
    stt._language = "en"
    stt._no_speech_prob_threshold = 0.6
    stt._avg_logprob_reject_threshold = -1.0
    info = SimpleNamespace(language="en", language_probability=language_probability)
    stt._model = SimpleNamespace(transcribe=lambda *a, **k: (iter(segments), info))
    return stt


def test_whisper_rejects_hallucination_pattern():
    seg = SimpleNamespace(text=" Pause. ", no_speech_prob=0.9, avg_logprob=-1.5)
    stt = _make_whisper_stt([seg])
    import numpy as np

    assert stt._transcribe_sync(np.zeros(16000, dtype=np.float32), 16000) == ""


def test_whisper_keeps_confident_real_speech():
    seg = SimpleNamespace(
        text=" Shut down my computer. ", no_speech_prob=0.05, avg_logprob=-0.2
    )
    stt = _make_whisper_stt([seg])
    import numpy as np

    assert (
        stt._transcribe_sync(np.zeros(16000, dtype=np.float32), 16000)
        == "Shut down my computer."
    )


def test_whisper_keeps_short_confident_command():
    # The exact case a cruder duration-only filter would break: a real,
    # short, deliberate "cancel" during a confirmation window.
    seg = SimpleNamespace(text=" Cancel. ", no_speech_prob=0.1, avg_logprob=-0.15)
    stt = _make_whisper_stt([seg])
    import numpy as np

    assert stt._transcribe_sync(np.zeros(4000, dtype=np.float32), 16000) == "Cancel."


# -------------------------------------------------------------- Groq cloud --


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test monkeypatches the name


def _groq_stt(monkeypatch, respond_json):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    stt = GroqCloudSTT()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=respond_json)

    transport = httpx.MockTransport(handler)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
    )
    return stt


@pytest.mark.asyncio
async def test_groq_rejects_hallucination_when_fields_present(monkeypatch):
    stt = _groq_stt(
        monkeypatch,
        {
            "text": "Let's pause.",
            "segments": [{"no_speech_prob": 0.88, "avg_logprob": -1.6}],
        },
    )
    assert await stt.transcribe(PCM) == ""


@pytest.mark.asyncio
async def test_groq_keeps_text_when_confidence_fields_absent(monkeypatch):
    # The realistic "Groq doesn't actually send these fields" case -- must
    # fall back to trusting the text rather than silently eating it.
    stt = _groq_stt(
        monkeypatch,
        {"text": "Open Firefox.", "segments": [{"id": 0, "start": 0.0, "end": 1.0}]},
    )
    assert await stt.transcribe(PCM) == "Open Firefox."


@pytest.mark.asyncio
async def test_groq_keeps_confident_text_when_fields_present(monkeypatch):
    stt = _groq_stt(
        monkeypatch,
        {
            "text": "Maximize Brave Browser.",
            "segments": [{"no_speech_prob": 0.02, "avg_logprob": -0.1}],
        },
    )
    assert await stt.transcribe(PCM) == "Maximize Brave Browser."
