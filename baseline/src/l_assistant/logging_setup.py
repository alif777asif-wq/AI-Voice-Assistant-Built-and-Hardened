"""
Central logging configuration. Console output is terse and human-facing;
the rotating file log is verbose and meant for debugging voice-pipeline
timing issues after the fact.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

from rich.logging import RichHandler

_FILE_FMT = (
    "%(asctime)s %(levelname)-7s %(name)-22s "
    "[%(filename)s:%(lineno)d] %(message)s"
)
_DATE_FMT = "%H:%M:%S"


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers below filter individually
    root.handlers.clear()

    # RichHandler renders identically in any ANSI-capable terminal on
    # either OS (Windows Terminal, PowerShell 7+, and legacy cmd.exe all
    # support it); it degrades to plain text automatically when output is
    # redirected to a file or pipe.
    console = RichHandler(
        show_path=False,
        show_time=True,
        markup=False,
        rich_tracebacks=True,
    )
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(console)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "l_assistant.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
    root.addHandler(file_handler)

    # Quiet down chatty third-party libraries at the console; keep them in
    # the file log for when something actually goes wrong.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
