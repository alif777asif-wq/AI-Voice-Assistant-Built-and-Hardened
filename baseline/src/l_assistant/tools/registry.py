"""
OpenAI-format tool schemas (compatible with both Ollama's and Groq's
tool-calling APIs -- see `l_assistant/llm/base.py`) plus the dispatcher that
turns a model's parsed `ToolCall` into an actual `SystemToolsEngine`
method call.

`CORE_TOOL_SCHEMAS`, `POWER_MANAGEMENT_TOOL_SCHEMAS`, and
`COMMAND_EXECUTION_TOOL_SCHEMAS` are the single source of truth for what
the LLM is told it can do -- `main.py` assembles the actual list handed
to the model from these three based on config.tools.*_enabled, since the
latter two are each opt-in and off by default (see config.py's
ToolsConfig and system_tools.py sections 6/7). `TOOL_SCHEMAS` below is
just their concatenation, kept for anything that wants "all of them"
without the gating. `ToolDispatcher` is the single source of truth for
what actually runs. A tool's name in any of these always corresponds to
a method of the same name on `SystemToolsEngine` -- `ToolDispatcher.execute`
uses `getattr` to enforce that they can't silently drift apart.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from l_assistant.tools.system_tools import ConfirmationRequired, SystemToolsEngine, ToolError

logger = logging.getLogger(__name__)


CORE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Launch a local application or program by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to open, e.g. 'notepad', 'firefox', 'code'.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_application",
            "description": "Close/kill a running application by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the running application to close.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_volume",
            "description": "Adjust the system's master output volume.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["up", "down", "mute"]},
                    "percent": {
                        "type": "integer",
                        "description": "How much to change volume by, 1-100. Ignored for 'mute'.",
                        "default": 10,
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "minimize_all_windows",
            "description": "Minimize every open desktop window (show desktop). Works on Windows and Linux; not implemented on macOS.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_window",
            "description": (
                "Minimize, maximize, restore, or focus ONE specific window "
                "-- unlike minimize_all_windows, which affects every "
                "window. Use this for 'maximize Brave', 'switch to VS "
                "Code', 'minimize this', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["minimize", "maximize", "restore", "focus"],
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Substring of the window's title, matched "
                            "case-insensitively (e.g. 'brave', 'code', "
                            "'reddit'). Leave empty to act on whichever "
                            "window currently has focus."
                        ),
                        "default": "",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_website",
            "description": (
                "Open a website directly -- 'open Facebook', 'open "
                "WhatsApp', 'open example.com', or any other site by "
                "name or domain. Use this whenever the user wants to GO "
                "TO a site, with no search query involved. Use "
                "search_web instead when they want to search FOR "
                "something (optionally on/via a specific site)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Site name ('facebook', 'claude ai') or domain ('example.com').",
                    },
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web and open the results in the default browser. "
                "If the user names a specific site to search on (e.g. "
                "'search for X on YouTube', 'look up Y on Amazon'), ALWAYS "
                "put that site name in 'site', never inside 'query'. For "
                "example, for 'search fifa world cup 2026 on youtube': call "
                "this with query='fifa world cup 2026', site='youtube' -- "
                "NOT query='fifa world cup 2026 youtube' with a plain Google "
                "search. Only omit 'site' (or use 'google') for a general "
                "web search with no named target site."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for. Never include a site name here -- use 'site' instead.",
                    },
                    "site": {
                        "type": "string",
                        "description": (
                            "Which site to search on: 'google' (default, general "
                            "web search), 'youtube' (searches YouTube directly), "
                            "or any other site name for a site-scoped Google search."
                        ),
                        "default": "google",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_browser_tabs",
            "description": (
                "Open a new tab, close the current tab, or switch to the "
                "next/previous tab in the focused browser window. Works on "
                "Windows and Linux; on macOS only 'new' and 'close' work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["new", "close", "next", "previous"]}
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_operation",
            "description": (
                "Create, copy, rename, delete, or list a local file or "
                "folder. 'delete', 'rename', and anything that would "
                "overwrite an existing file always ask the user to "
                "confirm first -- call this tool normally; if "
                "confirmation is needed you'll get a question to relay "
                "to the user instead of an error. Never set 'confirmed' "
                "yourself -- the orchestrator sets it after the user "
                "actually confirms. If the user references a target "
                "without naming it exactly ('delete the folder on my "
                "desktop', 'that file') use 'list' on the relevant "
                "directory FIRST to find the real name, rather than "
                "guessing OR immediately asking the user to spell out "
                "an exact name -- ask only if 'list' still leaves it "
                "ambiguous (more than one plausible match)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "copy", "rename", "delete", "list"],
                    },
                    "source": {
                        "type": "string",
                        "description": "Path to the file/folder to act on, or the directory to list.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Target path. Required for 'copy' and 'rename'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write. Only used by 'create'.",
                    },
                },
                "required": ["action", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_user_fact",
            "description": (
                "Durably remember something about the user across "
                "sessions -- their name, a preference, anything they "
                "ask you to remember. Unlike ordinary conversation, "
                "this survives indefinitely, not just until context "
                "gets summarized away. Use whenever the user states a "
                "fact about themselves worth keeping ('my name is...', "
                "'I prefer...', 'remember that...')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short label for the fact, e.g. 'name', 'temperature_unit', 'favorite_team'.",
                    },
                    "value": {
                        "type": "string",
                        "description": "The fact itself, e.g. 'Alif', 'Celsius', 'Real Madrid'.",
                    },
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_user_fact",
            "description": "Remove a previously remembered fact about the user -- 'forget my name', 'forget my timezone'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "The fact's label, as used with remember_user_fact -- e.g. 'name'.",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_youtube_search",
            "description": (
                "Search YouTube and immediately play the first result "
                "-- use for requests like 'play the first video on "
                "YouTube after searching for X' or 'play X on YouTube'. "
                "Resolves and opens the actual video directly, unlike "
                "search_web(site='youtube'), which only opens a search "
                "results page the user would still have to click "
                "through."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for, e.g. 'lofi hip hop radio', 'python asyncio tutorial'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# Only included when config.tools.power_management_enabled is True --
# see main.py's tool_schemas assembly and system_tools.py section 6.
POWER_MANAGEMENT_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "power_control",
            "description": (
                "Shut down, restart, or cancel a scheduled shutdown/"
                "restart of THIS computer. 'shutdown'/'reboot' always ask "
                "the user to confirm first -- call this tool normally, "
                "you'll get a question to relay if confirmation is "
                "needed. Never set 'confirmed' yourself. 'cancel' needs "
                "no confirmation and works whether or not anything is "
                "currently scheduled."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["shutdown", "reboot", "cancel"]},
                    "delay_seconds": {
                        "type": "integer",
                        "description": (
                            "Seconds to wait before shutting down/restarting "
                            "(gives the user a window to say 'cancel'). "
                            "Ignored for action='cancel'. A configured "
                            "minimum is enforced even if you pass less."
                        ),
                        "default": 60,
                    },
                },
                "required": ["action"],
            },
        },
    },
]

# Only included when config.tools.command_execution_enabled is True --
# see main.py's tool_schemas assembly and system_tools.py section 7.
COMMAND_EXECUTION_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run an arbitrary shell command (PowerShell on Windows, "
                "bash on Linux) as a last resort for something no other "
                "tool covers -- prefer open_application, file_operation, "
                "control_window, etc. when one of them already does what's "
                "needed. ALWAYS asks the user to confirm the exact command "
                "before running it -- call this tool normally, you'll get "
                "a question to relay if confirmation is needed. Never set "
                "'confirmed' yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The exact command to run.",
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "Directory to run it in. Must be inside an allowed root. Omit to use the default.",
                        "default": "",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One short phrase on why, spoken to the user as part of the confirmation.",
                        "default": "",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "How long to allow the command to run before giving up on it.",
                        "default": 60,
                    },
                },
                "required": ["command"],
            },
        },
    },
]

# Flat export for anything that still imports the old name directly --
# main.py itself builds the list conditionally (see its tool_schemas
# assembly) so command_execution/power_management can be off by default
# even when tools.enabled is on.
TOOL_SCHEMAS: List[Dict[str, Any]] = (
    CORE_TOOL_SCHEMAS + POWER_MANAGEMENT_TOOL_SCHEMAS + COMMAND_EXECUTION_TOOL_SCHEMAS
)


class ToolDispatcher:
    """Executes a parsed ToolCall against a SystemToolsEngine instance."""

    def __init__(self, engine: SystemToolsEngine) -> None:
        self._engine = engine

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Returns the tool's result as a string (every OpenAI-compatible
        `tool` message role expects string content). Only
        `ConfirmationRequired` escapes this method -- it needs to be
        handled structurally by the orchestrator's confirmation flow, not
        turned into a string the model would otherwise have to parse back
        out. Everything else (`ToolError`, bad arguments, unexpected
        exceptions) is caught here and turned into an `"Error: ..."`
        result string, because letting any of those propagate would crash
        the whole turn instead of letting the model explain the failure
        to the user in its own words.
        """
        method = getattr(self._engine, tool_name, None)
        if method is None or not callable(method) or tool_name.startswith("_"):
            logger.warning("Model requested unknown tool '%s'", tool_name)
            return f"Error: tool '{tool_name}' does not exist."

        try:
            return method(**arguments)
        except ConfirmationRequired:
            raise
        except ToolError as exc:
            logger.info("Tool '%s' refused: %s", tool_name, exc)
            return f"Error: {exc}"
        except TypeError as exc:
            logger.warning("Tool '%s' called with bad arguments %r: %s", tool_name, arguments, exc)
            return f"Error: invalid arguments for '{tool_name}': {exc}"
        except Exception as exc:  # noqa: BLE001 -- a tool must never crash a turn
            logger.exception("Tool '%s' raised an unexpected error", tool_name)
            return f"Error: '{tool_name}' failed unexpectedly: {exc}"
