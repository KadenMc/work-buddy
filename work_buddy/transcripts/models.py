"""Canonical transcript models shared by all harness providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def mtime_floor(days: int | None, since: datetime | None) -> float:
    """Coarse mtime lower bound (epoch seconds) for provider discovery.

    A transcript file whose mtime is below this cannot contain activity at or
    after the window start, so a provider may skip it without opening it.
    ``since`` (an aware datetime) is authoritative; ``days`` is day-granular
    sugar (``now - days``); neither — or ``days <= 0`` — means "all history"
    (floor ``0``).

    The *upper* bound is deliberately not a provider concern: a resumed session
    can carry a recent mtime yet old turns, so callers apply the precise
    conversation-time window (including ``until``) to the discovered candidates
    themselves.
    """
    if since is not None:
        return since.timestamp()
    if days is not None and days > 0:
        import time

        return time.time() - days * 86400.0
    return 0.0


@dataclass(frozen=True)
class TranscriptSession:
    provider_id: str
    harness_id: str
    session_id: str
    native_session_id: str
    path: Path
    mtime: float
    project_slug: str = ""
    project_name: str = ""
    cwd: str = ""
    originator: str = ""


@dataclass(frozen=True)
class TranscriptTurn:
    role: str
    text: str
    tools: tuple[str, ...] = ()
    timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "tools": list(self.tools),
            "timestamp": self.timestamp,
        }


@dataclass
class TranscriptToolCall:
    call_id: str
    name: str
    arguments: Any
    timestamp: str | None
    message_index: int
    output: Any = None
    is_error: bool = False

    @property
    def arguments_text(self) -> str:
        return _as_text(self.arguments)

    @property
    def output_text(self) -> str:
        return _as_text(self.output)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
