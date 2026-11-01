"""
Async client for a local Ollama server -- the LLM backend for this
project.

Why Ollama instead of a paid cloud API, and instead of embedding an
inference engine like llama-cpp-python directly in this codebase: Ollama
ships official one-line installers for both Linux and Windows that
include a prebuilt, hardware-appropriate inference engine (CPU or CUDA)
with zero compilation step. Embedding llama-cpp-python directly would
mean either requiring a compiler toolchain on Windows (Visual Studio
Build Tools) or gambling on a prebuilt wheel existing for the user's
exact Python/CUDA combination -- precisely the kind of platform-specific
workaround this project is built to avoid. Talking to Ollama over its
local HTTP API keeps this codebase 100% pure Python on both platforms;
the only OS-specific step anywhere in the LLM stack is a documented
installer download (see README.md).

It also runs with zero usage limits: once a model is pulled, inference is
bounded only by local hardware, never by an API rate limit or quota.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, List

import httpx

from l_assistant.llm.base import ChatWithToolsResult, LlmUnavailableError, parse_openai_tool_response

logger = logging.getLogger(__name__)


class OllamaUnavailableError(LlmUnavailableError):
    pass


class OllamaModelMissingError(LlmUnavailableError):
    pass


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2:3b",
        temperature: float = 0.6,
        num_ctx: int = 4096,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._num_ctx = num_ctx
        self._timeout = httpx.Timeout(
            connect=5.0, read=request_timeout_seconds, write=10.0, pool=5.0
        )

    async def ensure_ready(self) -> None:
        """
        Fail fast, at startup, with an actionable error message -- rather
        than discovering mid-conversation that Ollama isn't running or
        that the configured model was never pulled.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"Could not reach Ollama at {self._base_url} ({exc}).\n"
                f"Start it first:\n"
                f"  Linux:   'ollama serve' (or it's already running as a service)\n"
                f"  Windows: launch the Ollama app, or run 'ollama serve'\n"
                f"Install instructions: https://ollama.com/download"
            ) from exc

        installed = {m.get("name", "") for m in data.get("models", [])}
        # Ollama's /api/tags names include the tag (e.g. "llama3.2:3b");
        # accept either an exact match or a bare-name match so
        # "llama3.2:3b" and "llama3.2" both resolve sensibly.
        base_name = self._model.split(":")[0]
        exact_or_base_match = self._model in installed or any(
            name.split(":")[0] == base_name for name in installed
        )
        if not exact_or_base_match:
            raise OllamaModelMissingError(
                f"Model '{self._model}' is not pulled in Ollama yet.\n"
                f"Run:\n  ollama pull {self._model}\n"
                f"Installed models: {sorted(installed) or '(none)'}"
            )

    async def stream_chat(self, messages: List[Dict[str, str]]) -> AsyncIterator[str]:
        """
        Stream a chat completion, token-delta by token-delta. Each yielded
        string is the incremental text produced since the last yield
        (matching Ollama's `message.content` delta semantics), so the
        caller can feed them straight into SentenceChunker for overlapped
        TTS playback.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._num_ctx,
            },
        }
        url = f"{self._base_url}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        if obj.get("error"):
                            raise RuntimeError(f"Ollama error: {obj['error']}")
                        delta = obj.get("message", {}).get("content", "")
                        if delta:
                            yield delta
                        if obj.get("done"):
                            break
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"Lost connection to Ollama at {self._base_url} mid-request: {exc}"
            ) from exc

    async def chat_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> ChatWithToolsResult:
        """
        One non-streaming completion with tool definitions attached, via
        Ollama's OpenAI-*compatible* endpoint (`/v1/chat/completions`),
        not its native `/api/chat`. See this module's package docstring
        (`l_assistant/llm/base.py`) for why: the native endpoint returns already-
        parsed dict arguments while every OpenAI-compatible endpoint
        returns a JSON string, and standardizing on one wire format means
        one shared, tested parser instead of two.
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "temperature": self._temperature,
        }
        url = f"{self._base_url}/v1/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return parse_openai_tool_response(resp.json())
        except httpx.HTTPStatusError as exc:
            raise OllamaUnavailableError(
                f"Ollama returned {exc.response.status_code} for a tool-calling "
                f"request ({exc}). If this model doesn't support tool calling, "
                f"disable tools.enabled or switch llm.model to one that does "
                f"(e.g. qwen2.5:7b) -- see README's System control section."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailableError(
                f"Lost connection to Ollama at {self._base_url} mid-request: {exc}"
            ) from exc

    async def summarize(self, text: str, max_words: int = 120) -> str:
        """
        One-shot (non-streaming, non-chat-history) summarization call used
        by ConversationMemory to compact old turns. Deliberately bypasses
        `stream_chat` -- this is a single throwaway request, not a
        conversational turn, and must never itself be added to history.
        """
        payload = {
            "model": self._model,
            "prompt": (
                "Summarize the following conversation between a user and a "
                f"voice assistant in at most {max_words} words. Preserve "
                "concrete facts, decisions, names, and stated preferences. "
                "Write flat prose, third person, no preamble.\n\n"
                f"{text}"
            ),
            "stream": False,
            "options": {"temperature": 0.2},
        }
        url = f"{self._base_url}/api/generate"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json().get("response", "").strip()
        except httpx.HTTPError as exc:
            logger.warning("Memory summarization failed, skipping compaction: %s", exc)
            return ""
