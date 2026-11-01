"""
Speech-to-text via faster-whisper, a CTranslate2 reimplementation of
OpenAI's Whisper. Chosen over vanilla `openai-whisper` because it is
several times faster on CPU with the same accuracy, and over a cloud STT
API because it is fully offline and has no rate limit to degrade under
real use.

CTranslate2 ships prebuilt wheels for both Linux and Windows (x86_64), so
`pip install faster-whisper` is genuinely the same one-line install on
both target platforms -- no separate CUDA toolkit or build step required
for the CPU path this project defaults to.

Model weights are downloaded once (via huggingface_hub, itself
cross-platform) into the standard Hugging Face cache directory on first
use and reused after that.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel

from l_assistant.stt.confidence import SegmentConfidence, is_likely_hallucination

logger = logging.getLogger(__name__)


class WhisperSTT:
    def __init__(
        self,
        model_size: str = "small.en",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 1,
        language: Optional[str] = "en",
        no_speech_prob_threshold: float = 0.6,
        avg_logprob_reject_threshold: float = -1.0,
    ) -> None:
        self._beam_size = beam_size
        self._language = language
        self._no_speech_prob_threshold = no_speech_prob_threshold
        self._avg_logprob_reject_threshold = avg_logprob_reject_threshold

        logger.info(
            "Loading faster-whisper model '%s' (device=%s, compute_type=%s)...",
            model_size,
            device,
            compute_type,
        )
        t0 = time.monotonic()
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("faster-whisper model loaded in %.1fs", time.monotonic() - t0)

    def warmup(self) -> None:
        """
        Run one throwaway inference on silence so the first real utterance
        doesn't pay for lazy kernel/memory initialization. Safe to call
        from a background thread during startup.
        """
        silence = np.zeros(16000, dtype=np.float32)  # 1s @ 16kHz
        t0 = time.monotonic()
        list(self._model.transcribe(silence, language=self._language)[0])
        logger.info("faster-whisper warmup pass complete in %.2fs", time.monotonic() - t0)

    async def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str:
        """
        Transcribe raw 16-bit mono PCM captured by MicrophoneStream.
        Runs the blocking CTranslate2 call in a worker thread so the
        asyncio event loop keeps servicing audio I/O while it runs.
        """
        if not pcm16_bytes:
            return ""
        audio = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)

    def _transcribe_sync(self, audio: np.ndarray, sample_rate: int) -> str:
        t0 = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam_size,
            # Our own WebRTC-VAD endpointing already trimmed the clip to
            # roughly one utterance, and each independent utterance has no
            # useful "previous text" -- so both are turned off to save
            # latency and avoid the model hallucinating continuations.
            vad_filter=False,
            condition_on_previous_text=False,
        )
        # `segments` is a lazy generator over CTranslate2 output -- materialize
        # it once so both the transcript text and the confidence check below
        # read the same pass instead of running inference twice.
        segments = list(segments)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        duration_s = len(audio) / sample_rate
        logger.info(
            "STT: %.2fs audio -> %.2fs inference, lang=%s (p=%.2f): %r",
            duration_s,
            time.monotonic() - t0,
            info.language,
            info.language_probability,
            text,
        )

        # faster-whisper always reports these two fields per segment (unlike
        # Groq's cloud API -- see groq_cloud_whisper.py), so this backend can
        # reliably reject the classic hallucination-on-near-silence pattern:
        # a wake-word false-trigger on background noise produces a short,
        # mostly non-speech clip, and rather than returning "" Whisper often
        # fills it with a short, plausible, generic phrase. Discarding those
        # here -- instead of relying only on `min_transcript_chars` and the
        # self-echo check downstream -- is what actually breaks a
        # false-trigger loop instead of just describing it after the fact.
        confidences = [
            SegmentConfidence(no_speech_prob=seg.no_speech_prob, avg_logprob=seg.avg_logprob)
            for seg in segments
        ]
        if text and is_likely_hallucination(
            confidences,
            no_speech_prob_threshold=self._no_speech_prob_threshold,
            avg_logprob_reject_threshold=self._avg_logprob_reject_threshold,
        ):
            avg_nsp = sum(c.no_speech_prob for c in confidences) / len(confidences)
            avg_lp = sum(c.avg_logprob for c in confidences) / len(confidences)
            logger.info(
                "STT: discarding %r as a likely hallucination on non-speech audio "
                "(%.2fs clip, avg no_speech_prob=%.2f, avg avg_logprob=%.2f)",
                text,
                duration_s,
                avg_nsp,
                avg_lp,
            )
            return ""

        return text
