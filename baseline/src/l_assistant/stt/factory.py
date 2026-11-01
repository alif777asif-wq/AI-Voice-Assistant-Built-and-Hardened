"""
Builds whichever STT engine `config.stt.provider` selects: local
faster-whisper (offline, default) or Groq's cloud Whisper API. Mirrors
`l_assistant.llm.factory.build_llm_client` -- this is the one place that needs
to know both concrete classes exist; everything downstream just calls
`.transcribe(pcm_bytes, sample_rate) -> str`.
"""

from __future__ import annotations

from typing import Protocol

from l_assistant.config import SttConfig
from l_assistant.stt.groq_cloud_whisper import DEFAULT_BASE_URL as _GROQ_STT_DEFAULT_BASE_URL
from l_assistant.stt.groq_cloud_whisper import GroqCloudSTT
from l_assistant.stt.whisper_engine import WhisperSTT


class SttEngine(Protocol):
    async def transcribe(self, pcm16_bytes: bytes, sample_rate: int = 16000) -> str: ...

    def warmup(self) -> None: ...


def build_stt_engine(config: SttConfig) -> SttEngine:
    """
    Blocking by design when `provider == "local"` (loading a
    faster-whisper model is a real, multi-second CPU operation) -- call
    this via `asyncio.to_thread` from async code, exactly as `main.py`
    already does for every other heavy model load.
    """
    if config.provider == "local":
        return WhisperSTT(
            model_size=config.model_size,
            device=config.device,
            compute_type=config.compute_type,
            beam_size=config.beam_size,
            language=config.language,
            no_speech_prob_threshold=config.no_speech_prob_threshold,
            avg_logprob_reject_threshold=config.avg_logprob_reject_threshold,
        )

    if config.provider == "groq":
        engine = GroqCloudSTT(
            model=config.groq_model,
            api_key_env_var=config.api_key_env_var,
            base_url=config.base_url or _GROQ_STT_DEFAULT_BASE_URL,
            language=config.language,
            request_timeout_seconds=config.request_timeout_seconds,
            no_speech_prob_threshold=config.no_speech_prob_threshold,
            avg_logprob_reject_threshold=config.avg_logprob_reject_threshold,
        )
        engine.ensure_ready()
        return engine

    # Unreachable in practice -- SttConfig's own field validator already
    # rejects unknown providers before this function ever sees them.
    raise ValueError(f"Unknown stt.provider '{config.provider}'")
