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

    log_dir = _get_log_dir()
    log_file = log_dir / "work_buddy.log"

    root_logger = logging.getLogger("work_buddy")
    root_logger.setLevel(level)

    # File handler — detailed, all levels
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
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
