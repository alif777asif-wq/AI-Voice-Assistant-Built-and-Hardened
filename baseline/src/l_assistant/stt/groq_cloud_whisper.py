"""
Speech-to-text via Groq's cloud Whisper API
(`/openai/v1/audio/transcriptions`), for anyone who wants to offload STT
from local CPU entirely -- the source blueprint's stated reason for this
("eliminate CPU lag on low-spec systems") is a real trade-off: cloud STT
adds network round-trip latency and Groq's free-tier rate limit (same
caveat as `l_assistant.llm.groq_client`) in exchange for zero local CPU cost
during transcription, which matters on genuinely low-spec hardware.

This implements the same interface `l_assistant.stt.whisper_engine.WhisperSTT`
does (`transcribe(pcm16_bytes, sample_rate) -> str`), so
`l_assistant.stt.factory.build_stt_engine` can swap between them based on
`stt.provider` with zero changes anywhere else in the pipeline.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from typing import Optional

import httpx

from l_assistant.stt.confidence import SegmentConfidence, is_likely_hallucination

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

# Whether Groq's /audio/transcriptions actually returns per-segment
# no_speech_prob/avg_logprob under response_format="verbose_json", the way
# OpenAI's own Whisper API does, was NOT confirmed against a live Groq
# account while writing this -- third-party docs describing Groq's
# verbose_json schema list only id/text/start/end/words, no confidence
# fields. So this is written defensively: if the fields are present, they
# get used exactly like whisper_engine.py uses faster-whisper's; if
# they're absent, transcripts fall back to the old unfiltered behavior and
# a single one-time log line tells you which case you're actually in.
# Check for that "GroqCloudSTT: verbose_json segment schema" line after
# your next few utterances -- if it says fields are absent, the wake-word
# threshold / VAD vote-threshold tuning in config is doing the real work
# for this backend, not this function.


class GroqSttUnavailableError(RuntimeError):
    pass


class GroqCloudSTT:
    def __init__(
        self,
        model: str = "whisper-large-v3-turbo",
        api_key_env_var: str = "GROQ_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        language: Optional[str] = "en",
        request_timeout_seconds: float = 15.0,
        no_speech_prob_threshold: float = 0.6,
        avg_logprob_reject_threshold: float = -1.0,
    ) -> None:
        import os

        self._model = model
        self._base_url = base_url.rstrip("/")
        self._language = language
        self._api_key_env_var = api_key_env_var
        self._api_key = os.environ.get(api_key_env_var)
        self._timeout = httpx.Timeout(
            connect=5.0, read=request_timeout_seconds, write=15.0, pool=5.0
        )
        self._no_speech_prob_threshold = no_speech_prob_threshold
        self._avg_logprob_reject_threshold = avg_logprob_reject_threshold
        self._logged_schema_once = False

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def ensure_ready(self) -> None:
        """
        Synchronous (unlike the LLM clients) since it's called once during
        the same startup phase as the rest of main.py's model loading,
        which is already synchronous under asyncio.to_thread there. Just
        checks the key is present -- unlike /api/tags for Ollama/Groq
        chat, there's no cheap unauthenticated endpoint to hit for a real
        connectivity check without burning a real (paid-tier-limited)
        transcription request.
        """
        if not self._api_key:
            raise GroqSttUnavailableError(
                f"${self._api_key_env_var} is not set. Get a free key at "
                f"https://console.groq.com/keys, then:\n"
                f"  Linux:   export {self._api_key_env_var}=gsk_...\n"
                f"  Windows: setx {self._api_key_env_var} gsk_...   "
                f"(new terminal windows only)"
            )

    def warmup(self) -> None:
        """No-op for the cloud backend -- there is no local model to warm up."""

    async def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        if not pcm16_bytes:
            return ""

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit PCM
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16_bytes)
        wav_bytes = wav_buffer.getvalue()

        t0 = time.monotonic()
        # verbose_json instead of plain "text" -- see the module-level note
        # above on why, and on the fact that Groq returning the per-segment
        # confidence fields this unlocks is not yet confirmed.
        data = {"model": self._model, "response_format": "verbose_json"}
        if self._language:
            data["language"] = self._language
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        url = f"{self._base_url}/audio/transcriptions"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), data=data, files=files
                )
                if resp.status_code in (401, 403):
                    raise GroqSttUnavailableError(
                        f"Groq rejected {self._api_key_env_var} ({resp.status_code}) "
                        f"for a transcription request. Check/regenerate the key at "
                        f"https://console.groq.com/keys"
                    )
                if resp.status_code == 429:
                    logger.warning(
                        "Groq STT rate limit hit; dropping this utterance rather "
                        "than blocking the turn (retry-after=%s)",
                        resp.headers.get("retry-after", "?"),
                    )
                    return ""
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except ValueError:
                    # Unexpected non-JSON 200 body -- fall back to raw text
                    # rather than raising, same spirit as the HTTPError
                    # handler below: never let an STT quirk crash the turn.
                    logger.warning(
                        "Groq STT returned non-JSON body despite verbose_json; "
                        "using raw response text"
                    )
                    text = resp.text.strip()
                    segments_raw = []
                else:
                    text = str(payload.get("text", "")).strip()
                    segments_raw = payload.get("segments") or []
        except httpx.HTTPError as exc:
            logger.warning("Groq STT request failed, treating as empty transcript: %s", exc)
            return ""

        duration_s = len(pcm16_bytes) / 2 / sample_rate
        logger.info(
            "Cloud STT: %.2fs audio -> %.2fs round trip: %r",
            duration_s,
            time.monotonic() - t0,
            text,
        )

        if not self._logged_schema_once:
            self._logged_schema_once = True
            has_confidence_fields = bool(segments_raw) and any(
                "no_speech_prob" in seg for seg in segments_raw
            )
            logger.info(
                "GroqCloudSTT: verbose_json segment schema -- %s "
                "(segments=%d, confidence fields %s)",
                "usable for hallucination rejection" if has_confidence_fields
                else "no confidence fields observed, falling back to "
                "wake-word/VAD tuning as the primary defense for this backend",
                len(segments_raw),
                "present" if has_confidence_fields else "absent",
            )

        if text and segments_raw:
            confidences = [
                SegmentConfidence(
                    no_speech_prob=seg.get("no_speech_prob"),
                    avg_logprob=seg.get("avg_logprob"),
                )
                for seg in segments_raw
            ]
            if is_likely_hallucination(
                confidences,
                no_speech_prob_threshold=self._no_speech_prob_threshold,
                avg_logprob_reject_threshold=self._avg_logprob_reject_threshold,
            ):
                logger.info(
                    "Cloud STT: discarding %r as a likely hallucination on "
                    "non-speech audio (%.2fs clip)",
                    text,
                    duration_s,
                )
                return ""

        return text
