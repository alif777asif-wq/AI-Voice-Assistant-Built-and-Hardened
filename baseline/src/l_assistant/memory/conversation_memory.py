"""
SQLite-backed conversation memory with a rolling context window plus
periodic LLM-driven compaction -- so the context sent to the LLM stays
small and fast even in a long-running session, while the complete
transcript is always preserved on disk.

sqlite3 is part of the Python standard library on both Linux and Windows,
so this module adds zero new dependencies and zero platform branches. The
database itself lives under the app data directory resolved by
`l_assistant.platform_utils`.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'summary')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at);
"""


class ConversationMemory:
    def __init__(
        self,
        db_path: Path,
        max_turns_in_context: int = 12,
        summarize_after_turns: int = 20,
        session_id: str | None = None,
    ) -> None:
        self._db_path = db_path
        self._max_turns_in_context = max_turns_in_context
        self._summarize_after_turns = summarize_after_turns
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._turns_since_summary = 0

        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe here because every call into this
        # connection is routed through asyncio.to_thread and awaited before
        # the next one starts -- there is never truly concurrent access,
        # just access from different worker threads over time.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("Conversation memory ready at %s (session=%s)", db_path, self.session_id)

    async def add_message(self, role: str, content: str) -> None:
        if not content or not content.strip():
            return
        await asyncio.to_thread(self._add_message_sync, role, content.strip())
        if role in ("user", "assistant"):
            self._turns_since_summary += 1

    def _add_message_sync(self, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (self.session_id, role, content, time.time()),
        )
        self._conn.commit()

    async def get_context_messages(self) -> List[Dict[str, str]]:
        """
        Returns messages in `{"role": ..., "content": ...}` chat format:
        the most recent standing summary (if any) first, followed by the
        last `max_turns_in_context` raw user/assistant turns, oldest
        first.
        """
        return await asyncio.to_thread(self._get_context_messages_sync)

    def _get_context_messages_sync(self) -> List[Dict[str, str]]:
        cur = self._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'summary' "
            "ORDER BY created_at DESC LIMIT 1",
            (self.session_id,),
        )
        summary_row = cur.fetchone()

        cur = self._conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (self.session_id, self._max_turns_in_context),
        )
        recent = list(reversed(cur.fetchall()))

        messages: List[Dict[str, str]] = []
        if summary_row:
            messages.append(
                {
                    "role": "system",
                    "content": f"Summary of earlier conversation: {summary_row[0]}",
                }
            )
        messages.extend({"role": role, "content": content} for role, content in recent)
        return messages

    async def maybe_compact(self, summarize_fn: Callable[[str], Awaitable[str]]) -> None:
        """
        Every `summarize_after_turns` completed turns, collapse everything
        older than the active context window into a single running
        summary via `summarize_fn` (normally `OllamaClient.summarize`).
        Intended to be called by the orchestrator once per finished turn;
        it is a fast no-op unless the threshold has actually been crossed.
        """
        if self._turns_since_summary < self._summarize_after_turns:
            return

        to_summarize = await asyncio.to_thread(self._fetch_turns_outside_window)
        if not to_summarize:
            self._turns_since_summary = 0
            return

        transcript = "\n".join(f"{role}: {content}" for role, content in to_summarize)
        summary_text = await summarize_fn(transcript)
        if not summary_text:
            logger.warning("Summarization returned nothing; will retry next turn")
            return

        await asyncio.to_thread(self._store_summary_sync, summary_text)
        self._turns_since_summary = 0
        logger.info("Compacted %d older turns into a running summary", len(to_summarize))

    def _fetch_turns_outside_window(self) -> List[Tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT role, content FROM messages "
            "WHERE session_id = ? AND role IN ('user','assistant') "
            "ORDER BY created_at ASC",
            (self.session_id,),
        )
        rows = cur.fetchall()
        cutoff = max(0, len(rows) - self._max_turns_in_context)
        return rows[:cutoff]

    def _store_summary_sync(self, summary_text: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, 'summary', ?, ?)",
            (self.session_id, summary_text, time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
