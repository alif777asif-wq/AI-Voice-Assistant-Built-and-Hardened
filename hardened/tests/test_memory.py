import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.memory.conversation_memory import ConversationMemory


@pytest.fixture
def memory(tmp_path: Path) -> ConversationMemory:
    mem = ConversationMemory(
        db_path=tmp_path / "test_memory.db",
        max_turns_in_context=4,
        summarize_after_turns=6,
        session_id="test-session",
    )
    yield mem
    mem.close()


async def test_add_and_retrieve_messages(memory: ConversationMemory):
    await memory.add_message("user", "Hello")
    await memory.add_message("assistant", "Hi there")
    context = await memory.get_context_messages()
    assert context == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


async def test_blank_messages_are_ignored(memory: ConversationMemory):
    await memory.add_message("user", "   ")
    await memory.add_message("user", "")
    context = await memory.get_context_messages()
    assert context == []


async def test_context_window_is_bounded(memory: ConversationMemory):
    # max_turns_in_context=4 in the fixture above.
    for i in range(10):
        await memory.add_message("user", f"turn {i}")
    context = await memory.get_context_messages()
    assert len(context) == 4
    assert [m["content"] for m in context] == ["turn 6", "turn 7", "turn 8", "turn 9"]


async def test_compaction_triggers_after_threshold(memory: ConversationMemory):
    calls = []

    async def fake_summarize(transcript: str) -> str:
        calls.append(transcript)
        return "a compact summary of earlier turns"

    # summarize_after_turns=6 in the fixture; 8 turns should trip it once.
    for i in range(8):
        await memory.add_message("user", f"msg {i}")
        await memory.maybe_compact(fake_summarize)

    assert len(calls) == 1
    context = await memory.get_context_messages()
    assert context[0]["role"] == "system"
    assert "a compact summary of earlier turns" in context[0]["content"]


async def test_failed_summarization_does_not_lose_history(memory: ConversationMemory):
    async def failing_summarize(transcript: str) -> str:
        return ""  # simulates an unreachable LLM

    for i in range(8):
        await memory.add_message("user", f"msg {i}")
    await memory.maybe_compact(failing_summarize)

    context = await memory.get_context_messages()
    # No summary was stored, but the raw context window is still intact.
    assert all(m["role"] != "system" for m in context)
    assert len(context) == 4


async def test_sessions_are_isolated(tmp_path: Path):
    db_path = tmp_path / "shared.db"
    mem_a = ConversationMemory(db_path=db_path, session_id="session-a")
    mem_b = ConversationMemory(db_path=db_path, session_id="session-b")
    try:
        await mem_a.add_message("user", "from A")
        await mem_b.add_message("user", "from B")
        ctx_a = await mem_a.get_context_messages()
        ctx_b = await mem_b.get_context_messages()
        assert ctx_a == [{"role": "user", "content": "from A"}]
        assert ctx_b == [{"role": "user", "content": "from B"}]
    finally:
        mem_a.close()
        mem_b.close()
