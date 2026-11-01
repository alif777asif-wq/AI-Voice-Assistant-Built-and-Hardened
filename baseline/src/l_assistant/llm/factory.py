"""
Builds whichever LLM client `config.llm.provider` selects. This is the
only place in the codebase that needs to know both `OllamaClient` and
`GroqClient` exist -- everything downstream (`Orchestrator`,
`ConversationMemory.maybe_compact`, `main.py`'s error handling) talks to
the shared `l_assistant.llm.base.LlmClient` interface instead.
"""

from __future__ import annotations

from l_assistant.config import LlmConfig
from l_assistant.llm.base import LlmClient
from l_assistant.llm.groq_client import DEFAULT_BASE_URL as _GROQ_DEFAULT_BASE_URL
from l_assistant.llm.groq_client import GroqClient
from l_assistant.llm.ollama_client import OllamaClient

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


def build_llm_client(config: LlmConfig) -> LlmClient:
    if config.provider == "ollama":
        return OllamaClient(
            base_url=config.base_url or _OLLAMA_DEFAULT_BASE_URL,
            model=config.model,
            temperature=config.temperature,
            num_ctx=config.num_ctx,
            request_timeout_seconds=config.request_timeout_seconds,
        )

    if config.provider == "groq":
        return GroqClient(
            model=config.model,
            api_key_env_var=config.api_key_env_var,
            base_url=config.base_url or _GROQ_DEFAULT_BASE_URL,
            temperature=config.temperature,
            request_timeout_seconds=config.request_timeout_seconds,
        )

    # Unreachable in practice -- LlmConfig's own field validator already
    # rejects unknown providers before this function ever sees them.
    raise ValueError(f"Unknown llm.provider '{config.provider}'")
