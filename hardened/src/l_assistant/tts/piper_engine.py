"""
Text-to-speech via Piper (the actively maintained OHF-Voice/piper1-gpl
fork, distributed on PyPI as `piper-tts`), a small local neural TTS engine
that embeds espeak-ng for phonemization directly inside its wheel. That
means `pip install piper-tts` produces a fully self-contained install on
both Linux and Windows -- no separate espeak-ng system package, no
LD_LIBRARY_PATH tweaking, no DLL hunting, identical behavior on both.

Licensing note: as of the v1.3.0 rewrite, Piper's maintained fork changed
license from MIT to GPL-3.0 (the original MIT-licensed rhasspy/piper repo
was archived read-only in October 2025). This has zero effect on running
this assistant for personal or internal use, but matters if you
redistribute a build of this project to others -- see README.md.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from piper import PiperVoice, SynthesisConfig

logger = logging.getLogger(__name__)


class PiperTTS:
    def __init__(
        self,
        voice: str,
        voices_dir: Path,
        length_scale: float = 1.0,
        volume: float = 1.0,
        use_cuda: bool = False,
    ) -> None:
        self._syn_config = SynthesisConfig(length_scale=length_scale, volume=volume)

        model_path = self._ensure_voice_available(voice, voices_dir)
        logger.info("Loading Piper voice: %s", model_path)
        t0 = time.monotonic()
        self._voice = PiperVoice.load(str(model_path), use_cuda=use_cuda)
        logger.info("Piper voice loaded in %.1fs", time.monotonic() - t0)

    @property
    def sample_rate(self) -> int:
        return self._voice.config.sample_rate

    @staticmethod
    def _ensure_voice_available(voice: str, voices_dir: Path) -> Path:
        model_path = voices_dir / f"{voice}.onnx"
        config_path = voices_dir / f"{voice}.onnx.json"
        if model_path.is_file() and config_path.is_file():
            return model_path

        voices_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Voice '%s' not found in %s -- downloading from Hugging Face "
            "(rhasspy/piper-voices)...",
            voice,
            voices_dir,
        )
        # `sys.executable` guarantees we invoke the *same* interpreter (and
        # therefore the same venv/piper install) that is currently running,
        # regardless of whether that's `python`, `python3`, or `py` on the
        # user's PATH.
        result = subprocess.run(
            [sys.executable, "-m", "piper.download_voices", voice],
            cwd=str(voices_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not model_path.is_file():
            raise RuntimeError(
                f"Failed to download Piper voice '{voice}' into {voices_dir}.\n"
                f"--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}\n"
                f"Try manually:\n"
                f"  {sys.executable} -m piper.download_voices {voice}\n"
                f"Browse available voices:\n"
                f"  https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md"
            )
        logger.info("Voice '%s' downloaded successfully.", voice)
        return model_path

    def warmup(self) -> None:
        """Absorb first-call ONNX session overhead before the user is waiting."""
        list(self._voice.synthesize("Ready.", syn_config=self._syn_config))

    def synthesize_chunk(self, text: str) -> bytes:
        """Blocking synthesis of one text chunk into raw 16-bit PCM bytes."""
        pcm = bytearray()
        for audio_chunk in self._voice.synthesize(text, syn_config=self._syn_config):
            pcm.extend(audio_chunk.audio_int16_bytes)
        return bytes(pcm)

    async def synthesize_chunk_async(self, text: str) -> bytes:
        return await asyncio.to_thread(self.synthesize_chunk, text)
