"""Structured logging for work-buddy agents and workflows.

Sets up per-session logging under agents/<session>/logs/ with both
file and console output. All work_buddy modules use the standard
Python logging module — this just configures handlers and formatters.

Usage:
    from work_buddy.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Starting workflow", extra={"workflow": "analyze-contracts"})
"""

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from work_buddy.agent_session import get_session_dir


_configured = False


def _get_log_dir() -> Path:
    """Get the logs directory for the current session."""
    log_dir = get_session_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    return log_dir


def setup_logging(level: int = logging.INFO) -> None:
    """Configure logging for the current agent session.

    Sets up:
    - File handler: agents/<session>/logs/work_buddy.log (all levels)
    - Console handler: stderr (INFO and above)

    Safe to call multiple times — only configures once per process.
    """
    global _configured
    if _configured:
        return
    _configured = True

    # Force std streams to UTF-8 with backslashreplace so non-ASCII log
    # output (e.g. Obsidian task descriptions, vault content) never
    # raises UnicodeEncodeError on Windows where the default codec is
    # cp1252. Layer 1 (PYTHONUTF8=1 in sidecar child env) covers the
    # production path; this is the standalone-launch / test fallback.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass

    log_dir = _get_log_dir()
    log_file = log_dir / "work_buddy.log"

    root_logger = logging.getLogger("work_buddy")
    root_logger.setLevel(level)

    # File handler — detailed, all levels
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # RotatingFileHandler so long-lived sessions (e.g. the sidecar's
    # synthetic self-session) can't grow this file unboundedly. Cap at
    # 16 MiB × 4 rotations → 80 MB ceiling per session.
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=16 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Console handler — concise with timestamp, INFO+, stdout (not stderr)
    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for the given module name.

    Automatically sets up logging on first call.
    """
    setup_logging()
    return logging.getLogger(name)
