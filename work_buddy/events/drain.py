"""The single ``event-drain`` daemon thread — the backbone's only added thread.

Decoupled from the shared health-check loop, on its own cadence
(``interval_s``, default 45s, **no jitter** — a single local drain gains
nothing from jitter). Mirrors the existing ``health-monitor`` /
``inference-poller`` threads in ``sidecar/daemon.py``. One thread iterates ALL
registered consumers, so registering a consumer never adds a
thread — the backbone stays a **+1-thread** system regardless of consumer count.
"""

from __future__ import annotations

import threading

from work_buddy.events import dispatcher
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_S = 45.0


class EventDrain:
    """Owns the background drain loop. ``start()`` / ``stop()`` mirror
    ``JobsWatcher`` / ``HealthMonitor``."""

    def __init__(self, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="event-drain", daemon=True
        )
        self._thread.start()
        logger.info("event-drain started (interval %.0fs)", self._interval)

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                dispatcher.drain()
            except Exception:  # pragma: no cover — defensive; a tick must not die
                logger.exception("event-drain tick failed (non-fatal)")
            # Wake immediately on stop; otherwise sleep the interval (no jitter).
            self._stop.wait(self._interval)
