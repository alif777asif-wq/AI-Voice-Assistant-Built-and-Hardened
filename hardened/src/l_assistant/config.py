"""
Configuration schema and loader for L.

All tunables live in one validated tree so every other module receives a
single, typed `AppConfig` instance instead of reading files or environment
variables itself. This is the only module that touches YAML or env vars.

Cross-platform note: no path in this file is ever hard-coded with a
platform-specific separator. Anything that needs a real filesystem
location is resolved lazily through `l_assistant.platform_utils`, which is the
single place OS differences are handled.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

_ENV_PREFIX = "L_"


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    # None => let sounddevice pick the platform default input/output device.
    input_device: Optional[str] = None
    output_device: Optional[str] = None
    # Capture block size in samples. 1280 @ 16 kHz == 80 ms, which is the
    # minimum openWakeWord wants and divides evenly into 20 ms VAD frames.
    frame_samples: int = 1280

    @field_validator("frame_samples")
    @classmethod
    def _frame_must_divide_into_20ms_subframes(cls, v: int) -> int:
        subframe = 320  # 20 ms @ 16 kHz
        if v % subframe != 0:
            raise ValueError(
                f"audio.frame_samples ({v}) must be a multiple of {subframe} "
                "so it can be split into whole 20ms WebRTC-VAD subframes."
            )
        return v


class WakeWordConfig(BaseModel):
    enabled: bool = True
    # Bare pretrained model name ("hey_jarvis", "alexa", "hey_mycroft") or an
    # absolute path to a custom-trained .onnx model.
    model: str = "hey_jarvis"
    threshold: float = 0.5
    inference_framework: str = "onnx"  # forced identical on Linux + Windows
    # Built-in Silero VAD gate inside openWakeWord; cuts false triggers from
    # non-speech noise (fans, keyboard clicks, TV dialogue, etc).
    vad_threshold: float = 0.5
    cooldown_seconds: float = 1.5
    # Spoken the instant the wake word fires, before recording starts --
    # see orchestrator.py. Keep it short; it's synthesized fresh each
    # time, not pre-recorded, so latency scales with its length.
    ack_phrase: str = "Yes?"


class VadConfig(BaseModel):
    # WebRTC VAD aggressiveness, 0 (permissive) - 3 (strict). Raised from
    # WebRTC's own "moderate" default (2) to 3 ("very aggressive") because
    # in practice, at 2, ordinary room/fan/computer noise was frequently
    # misclassified as speech -- which meant `silence_timeout_ms` of
    # trailing silence essentially never accumulated, and every utterance
    # ran to the full max_utterance_seconds ceiling instead of ending
    # naturally a moment after the user stopped talking. If real speech
    # starts getting cut off or missed, lower this back toward 2.
    aggressiveness: int = 3
    silence_timeout_ms: int = 700
    min_utterance_ms: int = 250
    max_utterance_seconds: float = 20.0
    # After L finishes speaking (the wake acknowledgment, or a
    # confirmation question) and before it starts listening, the mic is
    # drained, then this pause is inserted, then drained again. Without
    # it, the tail of L's own TTS playback -- picked up by the mic
    # through room/speaker acoustics, since there's no acoustic echo
    # cancellation (see orchestrator.py's docstring) -- gets transcribed
    # as if it were the user's command. 250ms covers typical output
    # buffer + room reverb tail; raise it if echo is still getting
    # picked up on hardware with more reverb or a slower audio backend.
    post_speech_settle_ms: int = 250
    # Fraction of an 80ms capture block's four 20ms WebRTC-VAD sub-frames
    # that must agree "speech" before the whole block counts as speech
    # (see FrameVad.is_speech). Raised from a bare majority (0.5, i.e.
    # 2 of 4) to 0.75 (3 of 4) for the same reason aggressiveness was
    # raised: real, continuous speech reliably fills most or all of an
    # 80ms window, while a brief noise transient (a fan cycling, a key
    # click) typically only lands in one or two of the four sub-frames --
    # so this reduces false "speech" classifications from noise without
    # meaningfully affecting detection of genuine speech.
    speech_frame_vote_threshold: float = 0.75
    # Amplitude (RMS) floor, on the raw int16 PCM scale, checked BEFORE
    # WebRTC VAD's spectral classification -- 0.0 (off) by default. See
    # FrameVad's docstring for why this exists (aggressiveness and
    # speech_frame_vote_threshold above target misclassified NOISE
    # SHAPE; this targets misclassified noise that's simply too quiet to
    # be someone talking, which raising those two doesn't fix if the
    # noise is loud enough, relative to speech, to pass classification
    # consistently anyway). Room/mic/gain-specific -- there's no
    # universal correct value, so it stays off until you've actually
    # measured yours with scripts/vad_calibrate.py.
    min_rms: float = 0.0
    # Last-resort defense: if a freshly transcribed utterance is textually
    # very similar to what L just finished saying, and arrived within
    # this many seconds of finishing, it's treated as leaked self-echo
    # (an acoustic tail muting + the settle pause above didn't fully
    # catch) and discarded rather than acted on -- see
    # Orchestrator._looks_like_self_echo. Muting during playback (see
    # SpeechStreamer) is the primary defense and should make this rare;
    # this exists for the residual gap between "playback electrically
    # stopped" and "the room has actually gone quiet".
    self_echo_refractory_seconds: float = 4.0
    # difflib.SequenceMatcher ratio (0-1) above which a transcript counts
    # as matching L's last utterance closely enough to be self-echo
    # rather than a real, coincidentally-similar user request.
    self_echo_similarity_threshold: float = 0.6

    @field_validator("aggressiveness")
    @classmethod
    def _range(cls, v: int) -> int:
        if not 0 <= v <= 3:
            raise ValueError("vad.aggressiveness must be between 0 and 3")
        return v

    @field_validator("speech_frame_vote_threshold")
    @classmethod
    def _vote_threshold_range(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("vad.speech_frame_vote_threshold must be in (0.0, 1.0]")
        return v


class SttConfig(BaseModel):
    provider: str = "local"  # "local" (faster-whisper, offline) or "groq" (cloud Whisper API)
    # -- local (faster-whisper) --
    model_size: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    language: Optional[str] = "en"
    min_transcript_chars: int = 2
    # -- groq (cloud) --
    groq_model: str = "whisper-large-v3-turbo"
    api_key_env_var: str = "GROQ_API_KEY"
    base_url: Optional[str] = None  # None => https://api.groq.com/openai/v1
    request_timeout_seconds: float = 15.0
    # -- hallucination rejection (both backends; see l_assistant.stt.confidence) --
    # A transcript is discarded (treated as "") only when BOTH signals agree
    # it's probably not real speech. Raise no_speech_prob_threshold toward
    # 1.0 or avg_logprob_reject_threshold toward 0.0 to reject more
    # aggressively; lower either to reject less. The Groq backend only
    # applies this when its API actually returns the fields -- see
    # l_assistant.stt.groq_cloud_whisper's module docstring.
    no_speech_prob_threshold: float = 0.6
    avg_logprob_reject_threshold: float = -1.0

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        allowed = {"local", "groq"}
        if v not in allowed:
            raise ValueError(f"stt.provider must be one of {sorted(allowed)}, got {v!r}")
        return v


class TtsConfig(BaseModel):
    voice: str = "en_US-lessac-medium"
    # None => resolved via platform_utils.get_voices_dir()
    voice_dir: Optional[str] = None
    length_scale: float = 1.0
    volume: float = 1.0
    sentence_chunking: bool = True
    min_chunk_chars: int = 6
    use_cuda: bool = False


class LlmConfig(BaseModel):
    provider: str = "ollama"  # "ollama" (local, no rate limit) or "groq" (cloud, rate-limited free tier)
    # None => provider-specific default (Ollama: localhost:11434, Groq:
    # api.groq.com/openai/v1). Only override this for a self-hosted
    # Ollama on another machine, or an OpenAI-compatible proxy in front
    # of Groq.
    base_url: Optional[str] = None
    model: str = "llama3.2:3b"
    # Only consulted when provider == "groq". The key itself is read from
    # this environment variable at startup, never stored in config.yaml.
    api_key_env_var: str = "GROQ_API_KEY"
    temperature: float = 0.6
    num_ctx: int = 4096  # Ollama-only; ignored by the Groq client
    request_timeout_seconds: float = 30.0
    system_prompt: str = (
        "You are L, a concise, friendly local voice assistant. "
        "Keep replies short and conversational since they will be spoken "
        "aloud. Avoid lists, markdown, and long monologues unless the user "
        "explicitly asks for detail."
    )

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        allowed = {"ollama", "groq"}
        if v not in allowed:
            raise ValueError(f"llm.provider must be one of {sorted(allowed)}, got {v!r}")
        return v


class MemoryConfig(BaseModel):
    db_filename: str = "l_memory.db"
    max_turns_in_context: int = 12
    summarize_after_turns: int = 20


class ToolsConfig(BaseModel):
    # OFF by default: this hands the LLM the ability to launch/kill
    # processes and touch the filesystem. Turning it on is a deliberate
    # choice, not something that happens by installing the package -- see
    # config/system-control.yaml and README's "System control" section.
    enabled: bool = False
    # "delete" and "rename" always require a spoken "yes"/"confirm" before
    # executing, regardless of this flag turning other safety off is not
    # possible from config -- see tools/system_tools.py.
    require_confirmation_for_destructive: bool = True
    confirmation_timeout_seconds: float = 12.0
    # Absolute paths file_operation is allowed to touch (create/copy/
    # rename/delete), each checked via is_relative_to() after resolving
    # symlinks -- operations outside all of these are refused outright,
    # before anything destructive runs. Empty list => resolved to
    # [the user's home directory] at runtime by tools/system_tools.py.
    allowed_roots: List[str] = Field(default_factory=list)
    # Bounds the tool-calling loop (call model -> run tool -> call model
    # again...) so a model that keeps requesting tools can't hang a turn
    # forever.
    max_tool_iterations: int = 4

    # -- higher-power capabilities, each gated separately from `enabled`
    # above and from each other -- see system_tools.py sections 6/7 for
    # the reasoning behind treating these as a different risk class from
    # open_application/file_operation/etc. rather than folding them into
    # the same switch. Both OFF by default; turning tools.enabled on does
    # NOT turn these on.
    command_execution_enabled: bool = False
    power_management_enabled: bool = False
    # Floor for power_control's delay_seconds, enforced in system_tools.py
    # regardless of what's requested -- always leaves a real "say cancel"
    # window even if a low/zero delay was asked for (by voice or by the
    # model).
    power_min_delay_seconds: int = 15


class TrayConfig(BaseModel):
    # System tray / background-process mode (pystray + single-instance
    # lock). Independent of `tools.enabled` -- you can run L in the
    # tray as a pure conversational assistant, or run it in a normal
    # terminal with system control on. See tray/app_tray.py.
    enabled: bool = False
    icon_color: str = "#00C8FF"
    start_minimized: bool = True


class ConversationConfig(BaseModel):
    # Off by default -- unlike everything else in vad.py/orchestrator.py,
    # a follow-up listen is NOT gated by the wake word first, so it
    # leans entirely on stt.no_speech_prob_threshold/avg_logprob_reject_
    # threshold (l_assistant/stt/confidence.py) as primary defense against
    # ambient noise instead of secondary backup. Confirm those are
    # actually doing something for your STT backend (see the
    # GroqCloudSTT docstring on why that's not guaranteed for the cloud
    # path) before leaning on this for long stretches unattended.
    follow_up_enabled: bool = False
    # How long, in silence, to wait for a follow-up to START after L
    # finishes speaking, before giving up and reverting to normal
    # wake-word-gated listening.
    follow_up_window_seconds: float = 8.0
    # Once a follow-up actually starts, how long IT is allowed to run --
    # deliberately separate from follow_up_window_seconds (see
    # EndpointDetector's max_wait_for_speech_seconds) so a slow starter
    # doesn't eat into how long they get to finish talking once they do.
    follow_up_max_utterance_seconds: float = 20.0
    # Hard ceiling on consecutive turns without re-hearing the wake word,
    # independent of the timeout above -- bounds the worst case of a
    # sustained false-positive run (a TV or podcast left on in the room)
    # to a fixed number of turns rather than an unbounded one; hitting it
    # forces a wake-word re-arm even if the follow-up window itself
    # hasn't timed out.
    max_consecutive_follow_ups: int = 6


class AppConfig(BaseModel):
    name: str = "L"
    log_level: str = "INFO"
    # None => resolved via platform_utils.get_data_dir()
    data_dir: Optional[str] = None

    audio: AudioConfig = Field(default_factory=AudioConfig)
    wake_word: WakeWordConfig = Field(default_factory=WakeWordConfig)
    vad: VadConfig = Field(default_factory=VadConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    tray: TrayConfig = Field(default_factory=TrayConfig)


def _apply_env_overrides(raw: dict) -> dict:
    """
    Allow a handful of high-value overrides via environment variables, e.g.
    L_LLM_BASE_URL, L_LLM_MODEL, L_LOG_LEVEL. This is intentionally
    limited (not a generic env->dict flattener) so behavior stays obvious.
    """
    overrides = {
        "L_LOG_LEVEL": ("log_level",),
        "L_DATA_DIR": ("data_dir",),
        "L_LLM_BASE_URL": ("llm", "base_url"),
        "L_LLM_MODEL": ("llm", "model"),
        "L_STT_MODEL_SIZE": ("stt", "model_size"),
        "L_TTS_VOICE": ("tts", "voice"),
        "L_WAKE_WORD_MODEL": ("wake_word", "model"),
        "L_TOOLS_ENABLED": ("tools", "enabled"),
        "L_TRAY_ENABLED": ("tray", "enabled"),
        "L_STT_PROVIDER": ("stt", "provider"),
    }
    for env_name, path in overrides.items():
        val = os.environ.get(env_name)
        if val is None:
            continue
        node = raw
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = val
    return raw


def load_config(path: Optional[Path] = None) -> AppConfig:
    """
    Load configuration from a YAML file and validate it through Pydantic,
    applying environment variable overrides on top. `path=None` means
    "no config file requested" and silently uses built-in defaults --
    but if a path *was* given and it doesn't exist, that's an error, not
    a hint to fall back to defaults silently. The latter used to be
    unconditional (any missing path, requested or not, meant "use
    defaults"), which meant a mistyped or mangled `--config` path failed
    silently into an unrelated profile instead of a clear error -- e.g. a
    Windows-style `config\\system-control.yaml` pasted into a Linux shell,
    where the backslash gets eaten as an escape character rather than
    read as a path separator, silently ran the plain default profile
    instead of raising "file not found".
    """
    raw: dict = {}
    if path is not None:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"If you're on Linux or macOS, check for a stray backslash from "
                f"a copied Windows command -- paths here use forward slashes: "
                f"config/system-control.yaml, not config\\system-control.yaml"
            )
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
            if loaded:
                raw = loaded

    raw = _apply_env_overrides(raw)
    return AppConfig.model_validate(raw)
