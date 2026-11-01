"""
Finite-state machine states, plus the small pieces of cross-turn state
the orchestrator needs to track a pending destructive-action confirmation.
Kept as plain dataclasses/enums (rather than scattering fields directly
on Orchestrator) so what persists between turns is visible in one place.

    IDLE               -- listening only for the wake word (or, if the
                           wake word is disabled, immediately falls
                           through to LISTENING every cycle -- see
                           Orchestrator).
    LISTENING          -- wake word fired (or a follow-up already
                           started talking; see AWAITING_FOLLOW_UP);
                           recording the user's utterance until
                           WebRTC-VAD detects trailing silence.
    THINKING           -- utterance captured; running STT, then waiting
                           on the LLM (streamed, or a non-streaming
                           tool-calling loop -- see Orchestrator._run_turn).
    SPEAKING           -- streaming (or already-complete) text going
                           through sentence-chunked, overlapped TTS
                           playback.
    AWAITING_FOLLOW_UP -- L just finished speaking, conversation.
                           follow_up_enabled is on, and the wake word is
                           NOT required for the next utterance --
                           listening in silence for up to
                           conversation.follow_up_window_seconds to see
                           if the user keeps talking, before reverting to
                           IDLE. Flips to LISTENING the moment real
                           speech is actually detected -- see
                           Orchestrator._listen_for_utterance.
    ERROR_RECOVERY     -- something in the turn raised; a short spoken
                           apology is played and the loop returns to IDLE.
"""

import enum
from dataclasses import dataclass
from typing import Any, Dict


class AgentState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    AWAITING_FOLLOW_UP = "awaiting_follow_up"
    ERROR_RECOVERY = "error_recovery"


@dataclass
class PendingConfirmation:
    """
    Set when a tool call raises `ConfirmationRequired` (see
    `l_assistant.tools.system_tools`). The orchestrator speaks `question`, then
    treats the *next* utterance as a yes/no answer to it rather than a
    new request -- see `Orchestrator._resolve_pending_confirmation`.
    """

    tool_name: str
    arguments: Dict[str, Any]
    question: str

