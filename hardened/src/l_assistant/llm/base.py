"""
The interface both LLM backends (`OllamaClient`, `GroqClient`) implement,
so `Orchestrator`, `ConversationMemory.maybe_compact`, and `main.py` never
need to know or care which one is actually configured -- they're built
once by `l_assistant.llm.factory.build_llm_client` and used identically after
that.

Also defines the shared tool-calling data shapes and response parser both
backends use. Tool-calling is deliberately routed through each backend's
OpenAI-*compatible* chat completions endpoint rather than Ollama's native
`/api/chat` -- Ollama's native endpoint returns `tool_calls[].function.
arguments` as an already-parsed dict, while every OpenAI-compatible
endpoint (Groq, and Ollama's own `/v1/chat/completions`) returns it as a
JSON string that needs `json.loads`. Standardizing on the OpenAI-compat
wire format for both means there is exactly one parser
(`parse_openai_tool_response` below) instead of two, which matters more
than it sounds like it should: a tool-calling turn can delete files, so
"one code path, reused and tested" beats "two code paths that happen to
agree today."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Protocol

logger = logging.getLogger(__name__)


class LlmUnavailableError(RuntimeError):
    """
    Raised by either backend's `ensure_ready()` when the configured LLM
    can't currently be used -- server unreachable, API key missing/
    rejected, or configured model not found. Callers (main.py) catch this
    one type regardless of which provider is active.
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatWithToolsResult:
    # Non-empty exactly when tool_calls is empty, and vice versa, for any
    # well-formed response (a model can't simultaneously "answer" and
    # "call a tool" in one OpenAI-format message).
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    # The raw {"role": "assistant", ...} message as returned by the API,
    # appended verbatim to conversation history so the next request has
    # the exact tool_calls structure the model itself produced.
    raw_assistant_message: Dict[str, Any] = field(default_factory=dict)


def parse_openai_tool_response(data: Dict[str, Any]) -> ChatWithToolsResult:
    """
    Parse a non-streaming OpenAI-format `/chat/completions` response body
    into a `ChatWithToolsResult`. Shared by OllamaClient and GroqClient.
    """
    choices = data.get("choices") or [{}]
    message = choices[0].get("message") or {}

    tool_calls: List[ToolCall] = []
    for raw_call in message.get("tool_calls") or []:
        func = raw_call.get("function") or {}
        raw_args = func.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments did not decode to a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Model returned malformed arguments for tool '%s': %r (%s). "
                "Treating as no arguments.",
                func.get("name", "?"),
                raw_args,
                exc,
            )
            args = {}
        tool_calls.append(
            ToolCall(id=raw_call.get("id", ""), name=func.get("name", ""), arguments=args)
        )

    return ChatWithToolsResult(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        raw_assistant_message=message,
    )


class LlmClient(Protocol):
    async def ensure_ready(self) -> None:
        """Fail fast with an actionable LlmUnavailableError, or return normally."""
        ...

    def stream_chat(self, messages: List[Dict[str, str]]) -> AsyncIterator[str]:
        """Yield incremental text deltas for one chat completion (no tools)."""
        ...

    async def chat_with_tools(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> ChatWithToolsResult:
        """One non-streaming completion with tool definitions attached."""
        ...

    async def summarize(self, text: str, max_words: int = 120) -> str:
        """One-shot, non-conversational summarization used by ConversationMemory."""
        ...

