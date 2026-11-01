"""
Wake word detection via openWakeWord, forced onto the ONNX Runtime backend
on both Linux and Windows.

openWakeWord supports two inference backends: TensorFlow Lite and ONNX
Runtime. Its own upstream default is TFLite *on Linux* and ONNX *on
Windows* (Windows has no prebuilt modern tflite-runtime wheel) -- meaning
"the default backend" is actually two different code paths depending on
OS. That is precisely the kind of silent platform divergence this project
is built to avoid, so `inference_framework="onnx"` is pinned explicitly:
Linux and Windows run the exact same model file through the exact same
runtime, with identical detection behavior on both.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import numpy as np
import openwakeword
from openwakeword.model import Model

logger = logging.getLogger(__name__)

# The pretrained models openwakeword.utils.download_models() fetches
# out of the box. A bare name outside this set is still accepted (it may
# be a valid model added upstream after this file was written), but is
# logged as unverified.
_KNOWN_PRETRAINED_NAMES = {
    "alexa",
    "hey_mycroft",
    "hey_jarvis",
    "hey_rhasspy",
    "timer",
    "weather",
}


class WakeWordDetector:
    def __init__(
        self,
        model: str,
        threshold: float = 0.5,
        vad_threshold: float = 0.5,
        inference_framework: str = "onnx",
        cooldown_seconds: float = 1.5,
    ) -> None:
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._last_trigger_time = 0.0

        model_ref = self._resolve_model(model)

        logger.info(
            "Loading wake word model '%s' (framework=%s, vad_threshold=%.2f)",
            model_ref,
            inference_framework,
            vad_threshold,
        )
        self._model = Model(
            wakeword_models=[model_ref],
            inference_framework=inference_framework,
            vad_threshold=vad_threshold,
        )
        active_keys = list(self._model.prediction_buffer.keys())
        logger.info("Wake word model ready, active model key(s): %s", active_keys)

    @staticmethod
    def _resolve_model(model: str) -> str:
        candidate = Path(model)
        if candidate.suffix in (".onnx", ".tflite"):
            if candidate.is_file():
                logger.info("Using custom wake word model file: %s", candidate.resolve())
                return str(candidate.resolve())
            raise FileNotFoundError(
                f"wake_word.model is set to '{model}', but that file doesn't "
                f"exist. A custom wake word (e.g. \"Hey, L\") needs a model "
                f"trained specifically for that phrase -- this isn't something "
                f"that can be produced without real audio samples and a "
                f"training run, so no placeholder file is created for you. "
                f"Train one with openWakeWord's training pipeline: "
                f"https://github.com/dscripka/openWakeWord/tree/main/notebooks "
                f"then place the resulting .onnx file at this path. In the "
                f"meantime, use a built-in name instead: "
                f"{sorted(_KNOWN_PRETRAINED_NAMES)}"
            )

        name = model.strip()
        if name not in _KNOWN_PRETRAINED_NAMES:
            logger.warning(
                "Wake word model '%s' is not in the built-in pretrained set "
                "%s. Treating it as a custom model name -- if this fails to "
                "load, either point wake_word.model at an explicit .onnx "
                "file path or pick one of the built-in names.",
                name,
                sorted(_KNOWN_PRETRAINED_NAMES),
            )
        # No-op if the shared feature-extractor + pretrained models are
        # already cached locally from a previous run.
        openwakeword.utils.download_models()
        return name

    def detect(self, frame: np.ndarray) -> Optional[str]:
        """
        Feed one capture block (int16 mono, >= 80 ms) through the model.
        Returns the triggered wake word's name, or None. Internally
        debounced by `cooldown_seconds` so a single utterance of the wake
        word can't fire the pipeline twice in a row.
        """
        predictions = self._model.predict(frame)
        now = time.monotonic()
        if now - self._last_trigger_time < self._cooldown_seconds:
            return None

        for name, score in predictions.items():
            if score >= self._threshold:
                self._last_trigger_time = now
                logger.info("Wake word '%s' triggered (score=%.3f)", name, score)
                return name
        return None

    def reset(self) -> None:
        """
        Clear the model's internal prediction/audio ring buffers. Called
        after every completed turn so a phrase spoken to the LLM a moment
        ago can't "echo" into a false positive right after the assistant
        finishes responding.
        """
        self._model.reset()
