"""
Async client for Groq's cloud LLM API, via its OpenAI-compatible chat
completions endpoint. This exists for anyone who explicitly doesn't want
to run a local LLM at all -- swap `llm.provider: groq` in config and this
replaces `OllamaClient` with zero changes anywhere else in the pipeline
(see `l_assistant.llm.base.LlmClient` and `l_assistant.llm.factory.build_llm_client`).

Read this before switching:

Trade-off vs. the default Ollama backend -- Groq's free tier is
rate-limited (roughly 30 requests/minute and a per-model tokens/minute
cap, as of mid-2026), which is exactly the kind of usage-limited tier
`OllamaClient`'s module docstring explains this project defaults away
from. For one person talking to their own assistant at normal
conversational pace, 30 RPM is very unlikely to ever be the bottleneck --
it's flagged here so it's a decision you're making on purpose, with
`GroqUnavailableError` giving you the `retry-after` value if you ever do
hit it, not a surprise discovered mid-conversation.

Model choice matters and changes over time on a hosted catalog like this
one -- Groq deprecated its `llama-3.1-8b-instant` and
`llama-3.3-70b-versatile` chat models in 2026 (the former's shutdown date
is 2026-08-16). Do not configure either. `openai/gpt-oss-20b` is the
current fast/cheap default this project ships with; `openai/gpt-oss-120b`
is slower and more capable. Check
https://console.groq.com/docs/models before picking something else --
`ensure_ready()` below also warns (not fails) if the configured model
isn't in Groq's live catalog at startup.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Dict, List

import httpx

from l_assistant.llm.base import ChatWithToolsResult, LlmUnavailableError, parse_openai_tool_response

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GroqUnavailableError(LlmUnavailableError):
    pass


class GroqClient:
    def __init__(
        self,
        model: str,
        api_key_env_var: str = "GROQ_API_KEY",
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.6,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._api_key_env_var = api_key_env_var
        self._api_key = os.environ.get(api_key_env_var)
        self._timeout = httpx.Timeout(
            connect=5.0, read=request_timeout_seconds, write=10.0, pool=5.0
        )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def ensure_ready(self) -> None:
        """
        Fail fast, at startup, with an actionable error -- same contract
        as `OllamaClient.ensure_ready()`, so main.py doesn't need a
        provider-specific code path.
        """
        if not self._api_key:
            raise GroqUnavailableError(
                f"${self._api_key_env_var} is not set. Get a free key at "
                f"https://console.groq.com/keys, then:\n"
                f"  Linux:   export {self._api_key_env_var}=gsk_...\n"
                f"  Windows: setx {self._api_key_env_var} gsk_...   "
                f"(new terminal windows only)"
            )

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/models", headers=self._headers()
                )
                if resp.status_code in (401, 403):
                    raise GroqUnavailableError(
                        f"Groq rejected {self._api_key_env_var} "
                        f"({resp.status_code}). Check/regenerate the key at "
                        f"https://console.groq.com/keys"
                    )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise GroqUnavailableError(f"Could not reach Groq's API: {exc}") from exc

        available = {m.get("id", "") for m in data.get("data", [])}
        if self._model not in available:
            logger.warning(
                "Configured model '%s' was not in Groq's live model list "
                "(%d models returned). It may still work if the list is "
                "incomplete or the model is very new -- but this is also "
                "exactly what happens when a model gets deprecated out from "
                "under you. Double-check https://console.groq.com/docs/models.",
                self._model,
                len(available),
            )

    async def stream_chat(self, messages: List[Dict[str, str]]) -> AsyncIterator[str]:
        """
        Stream a chat completion, token-delta by token-delta, from Groq's
        OpenAI-compatible `/chat/completions` SSE stream (`data: {...}`
        lines terminated by a `data: [DONE]` sentinel).
        """
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "stream": True,
        }
        url = f"{self._base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as response:
                    if response.status_code == 429:
                        retry_after = response.headers.get("retry-after", "a few")
                        raise GroqUnavailableError(
                            f"Groq free-tier rate limit hit; retry after "
                            f"{retry_after}s. This is the trade-off documented "
                            f"at the top of groq_client.py -- switch "
                            f"llm.provider back to 'ollama' if this happens often."
                        )
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            break
                        obj = json.loads(data_str)
                        choices = obj.get("choices") or [{}]
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield delta
        except httpx.HTTPStatusError as exc:
            raise GroqUnavailableError(
                f"Groq returned {exc.response.status_code} for this request "
                f"({exc}). If this is 401/403, check {self._api_key_env_var}; "
                f"if 404, double check the model name '{self._model}'."
            ) from exc
        except httpx.HTTPError as exc:
            raise GroqUnavailableError(
                f"Lost connection to Groq mid-request: {exc}"
            ) from exc

    async def chat_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> ChatWithToolsResult:
        """One non-streaming completion with tool definitions attached."""
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": self._temperature,
            "stream": False,
        }
        url = f"{self._base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                return parse_openai_tool_response(resp.json())
        except httpx.HTTPStatusError as exc:
            raise GroqUnavailableError(
                f"Groq returned {exc.response.status_code} for a tool-calling "
                f"request ({exc}). If this model doesn't support tool calling, "
                f"switch llm.model -- openai/gpt-oss-20b and openai/gpt-oss-120b "
                f"both do."
            ) from exc
        except httpx.HTTPError as exc:
            raise GroqUnavailableError(
                f"Lost connection to Groq mid-request: {exc}"
            ) from exc

    async def summarize(self, text: str, max_words: int = 120) -> str:
        """
        One-shot, non-conversational summarization used by
        `ConversationMemory.maybe_compact`. Uses the same chat endpoint
        (Groq has no separate completion-only endpoint) but as a single
        message with no history, and is never itself added to memory.
        """
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Summarize the following conversation between a user "
                        f"and a voice assistant in at most {max_words} words. "
                        "Preserve concrete facts, decisions, names, and stated "
                        "preferences. Write flat prose, third person, no "
                        f"preamble.\n\n{text}"
                    ),
                }
            ],
            "temperature": 0.2,
            "stream": False,
        }
        url = f"{self._base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            logger.warning("Memory summarization failed, skipping compaction: %s", exc)
            return ""
