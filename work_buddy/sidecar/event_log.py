"""Structured event log — in-memory ring buffer for sidecar events.

Events are emitted by the daemon, scheduler, and service supervisor.
Each event has a concise ``summary`` (for dashboard display) and an
optional verbose ``detail`` (forwarded to Python stdlib logging for
console and file output).

The ring buffer holds up to ``max_size`` events in memory. A subset
(typically 50) is serialized into ``sidecar_state.json`` on every
daemon tick for dashboard consumption.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SidecarEvent:
    """A single structured event from the sidecar."""

    ts: float  # epoch seconds
    kind: str  # event type (see EventLog.emit docstring)
    source: str  # job name, service name, or "daemon"
    summary: str  # concise one-liner for dashboard display
    detail: str = ""  # verbose text for console/file log (may be empty)
    level: str = "info"  # info | warn | error


class EventLog:
    """Thread-safe ring buffer of SidecarEvents.

    Usage::

        log = EventLog(max_size=200)
        log.emit("daemon_start", "daemon", "Sidecar started")
        log.emit("job_completed", "my-job", "ok — 3 keys",
                 detail="Full result dict: {...}", level="info")

        # For state file serialization (bounded):
        state.events = log.recent(50)
    """

    def __init__(self, max_size: int = 200) -> None:
        self._buffer: deque[SidecarEvent] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def emit(
        self,
        kind: str,
        source: str,
        summary: str,
        detail: str = "",
        level: str = "info",
    ) -> None:
        """Add an event and forward to Python logging.

        Args:
            kind: Event type. One of: ``job_fired``, ``job_completed``,
                ``job_failed``, ``service_restart``, ``service_healthy``,
                ``service_crashed``, ``daemon_start``, ``daemon_stop``,
                ``hot_reload``.
            source: Origin — job name, service name, or ``"daemon"``.
            summary: Concise one-liner shown in the dashboard.
            detail: Verbose text for console/file log. If empty,
                ``summary`` is used for the log message instead.
            level: ``"info"``, ``"warn"``, or ``"error"``.
        """
        event = SidecarEvent(
            ts=time.time(),
            kind=kind,
            source=source,
            summary=summary,
            detail=detail,
            level=level,
        )
        with self._lock:
            self._buffer.append(event)

        # Forward to Python logging (verbose channel)
        log_msg = f"[{kind}] {source}: {detail or summary}"
        if level == "error":
            logger.error(log_msg)
        elif level == "warn":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last *n* events as serializable dicts.

        Designed for embedding in ``sidecar_state.json``. The
        ``detail`` field is omitted to keep the state file concise.
        """
        with self._lock:
            # Slice the last n entries
            items = list(self._buffer)[-n:]
        return [
            {
                "ts": e.ts,
                "kind": e.kind,
                "source": e.source,
                "summary": e.summary,
                "level": e.level,
            }
            for e in items
        ]

    def since(self, after_ts: float) -> list[dict[str, Any]]:
        """Return events with ``ts > after_ts`` (for incremental polling)."""
        with self._lock:
            items = [e for e in self._buffer if e.ts > after_ts]
        return [
            {
                "ts": e.ts,
                "kind": e.kind,
                "source": e.source,
                "summary": e.summary,
                "level": e.level,
            }
            for e in items
        ]
