"""
The single place OS differences in filesystem layout are resolved.

Every other module asks *this* module for a directory instead of building
paths itself. `platformdirs` gives us the correct, idiomatic location on
both Linux (XDG base directories, e.g. ~/.local/share/l-assistant) and
Windows (e.g. C:\\Users\\<user>\\AppData\\Local\\l-assistant-project\\l-assistant)
without any `if platform.system() == "Windows"` branching anywhere else
in the codebase.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Optional

from platformdirs import PlatformDirs

# Changing appname/appauthor here changes the resolved data directory --
# see README's "Data locations" section: existing conversation memory,
# downloaded models, and voices live under the OLD elin/elin-project
# path and won't be found here automatically. Move that folder's
# contents over, or just let scripts/download_models.py repopulate this
# one from scratch.
_DIRS = PlatformDirs(appname="l-assistant", appauthor="l-assistant-project", roaming=False)


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_linux() -> bool:
    return platform.system() == "Linux"


def get_data_dir(override: Optional[str] = None) -> Path:
    """Persistent app data: SQLite memory DB, downloaded models, voices."""
    path = Path(override) if override else Path(_DIRS.user_data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_dir() -> Path:
    """Scratch space for model downloads that can be safely wiped."""
    path = Path(_DIRS.user_cache_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_dir(data_dir_override: Optional[str] = None) -> Path:
    path = get_data_dir(data_dir_override) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_models_dir(data_dir_override: Optional[str] = None) -> Path:
    """Wake-word .onnx models live here."""
    path = get_data_dir(data_dir_override) / "models" / "wakeword"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_voices_dir(data_dir_override: Optional[str] = None) -> Path:
    """Piper .onnx + .onnx.json voice pairs live here."""
    path = get_data_dir(data_dir_override) / "models" / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path(db_filename: str, data_dir_override: Optional[str] = None) -> Path:
    return get_data_dir(data_dir_override) / db_filename


def get_profile_path(data_dir_override: Optional[str] = None) -> Path:
    """UserProfileStore's JSON file -- durable user facts, distinct from
    the SQLite conversation memory get_db_path points at."""
    return get_data_dir(data_dir_override) / "user_profile.json"


def python_executable() -> str:
    """
    Absolute path to the interpreter currently running L. Used any time
    we need to shell out to `python -m some_module` (e.g. piper's voice
    downloader) so we always invoke the same venv on both Linux and Windows
    instead of guessing between `python` / `python3` / `py`.
    """
    return sys.executable
