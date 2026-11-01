"""
Durable, cross-session user facts (name, preferences) -- distinct from
ConversationMemory (l_assistant.memory.conversation_memory), which is a
RECENT-dialogue rolling window that gets compacted/summarized as it grows and is not a reliable place to keep a specific
fact indefinitely. This is the opposite: small, flat, and never
auto-pruned -- if the user says "remember my name is Alif," it's still
there next month, whether or not the conversation that mentioned it has
long since scrolled out of context and been summarized away.

One JSON file per data directory (see platform_utils.py, already
per-user/per-machine). That's what actually satisfies "an isolated
memory per machine" when l_assistant.exe is copied elsewhere: not
anything in THIS module, but simply that platformdirs resolves a
different %LOCALAPPDATA% path for every distinct Windows user account --
a fresh copy of the exe on a different machine, or run by a different
user on the same machine, starts with an empty profile rather than
inheriting someone else's.

JSON, not SQLite: the access pattern here is "read a small dict once at
startup, occasionally add/remove one key" -- SQLite's transactional/
relational machinery adds real complexity for zero benefit at this
scale, and a JSON file is trivially something the user could open and
read themselves to see exactly what's been remembered about them.
ConversationMemory uses SQLite instead because its access pattern (an
append-only, growing log queried by recency) actually benefits from it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


class UserProfileStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._facts: Dict[str, str] = self._load()

    def _load(self) -> Dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("profile file does not contain a JSON object")
            return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # A corrupted/unreadable profile must never crash startup --
            # log it, start empty. The next remember() call rebuilds a
            # fresh, valid file; nothing else in this class assumes the
            # file was ever well-formed.
            logger.warning("Couldn't read user profile at %s (%s); starting empty.", self._path, exc)
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename over the real one -- rename
        # is atomic on both Windows and POSIX, so a crash or power loss
        # mid-write can never leave a half-written, corrupted profile
        # file in place; worst case, the temp file is orphaned and the
        # previous good version is still what's read next time.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._facts, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    @staticmethod
    def _normalize_key(key: str) -> str:
        return key.strip().lower().replace(" ", "_")

    def remember(self, key: str, value: str) -> None:
        key = self._normalize_key(key)
        if not key:
            raise ValueError("fact key can't be empty")
        value = value.strip()
        if not value:
            raise ValueError("fact value can't be empty -- use forget() to remove a fact")
        with self._lock:
            self._facts[key] = value
            self._save()

    def forget(self, key: str) -> bool:
        """Returns whether there was actually anything to forget."""
        key = self._normalize_key(key)
        with self._lock:
            if key in self._facts:
                del self._facts[key]
                self._save()
                return True
            return False

    def forget_all(self) -> None:
        with self._lock:
            self._facts = {}
            self._save()

    def all_facts(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._facts)

    def as_prompt_fragment(self) -> str:
        """Rendered for injection into the system prompt. Empty string
        for a brand-new/empty profile, so callers can unconditionally
        append this without producing a dangling "Known facts:" header
        when nothing's been learned yet."""
        facts = self.all_facts()
        if not facts:
            return ""
        lines = [f"- {k.replace('_', ' ')}: {v}" for k, v in sorted(facts.items())]
        return "Known facts about this user (remembered from earlier sessions):\n" + "\n".join(lines)
