"""Minimal stand-ins for the parts of the assistant that need audio hardware or a live model.

The model is the UNTRUSTED input source in this audit, so it is replaced by a scripted adversary that
emits exactly the raw JSON a compromised or prompt-injected model could emit. Everything between that
JSON and the operating system (parser, orchestrator, dispatcher, engine) is the real code.
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator, Dict, List, Optional


class FakeSpeechStreamer:
    def __init__(self) -> None:
        self.spoken: List[str] = []
        self.last_spoken_text: Optional[str] = None
        self.last_spoken_at: Optional[float] = None

    async def speak_stream(self, text_stream: AsyncIterator[str]) -> str:
        parts = [chunk async for chunk in text_stream]
        full = "".join(parts)
        self.spoken.append(full)
        self.last_spoken_text = full
        self.last_spoken_at = time.monotonic()
        return full


class ScriptedLlm:
    def __init__(self, script: list) -> None:
        self._script = list(script)

    async def ensure_ready(self) -> None:
        pass

    def stream_chat(self, messages):
        raise NotImplementedError

    async def chat_with_tools(self, messages: Any, tools: Any):
        return self._script.pop(0)

    async def summarize(self, text: str, max_words: int = 120) -> str:
        return ""


def make_orchestrator(modules: dict, llm, streamer, dispatcher):
    cfg = modules["config"].AppConfig(tools={"enabled": True, "max_tool_iterations": 4})
    return modules["orchestrator"].Orchestrator(
        config=cfg, mic=None, player=None, wake_word=None, frame_vad=None, stt=None, llm=llm, tts=None,
        speech_streamer=streamer, memory=None, tool_dispatcher=dispatcher,
        tool_schemas=[{"type": "function", "function": {"name": "dummy"}}],
    )


def model_response(tool: str, arguments_json: str, call_id: str = "call_1") -> Dict[str, Any]:
    """A raw OpenAI-format chat/completions body in which the model asks for one tool call."""
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": tool, "arguments": arguments_json}}]}}]}
