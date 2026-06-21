"""Thin ``CronAdapter`` — emits ``ai.workbuddy.schedule.tick`` onto the spine.

**Additive** — the scheduler is left unmodified (this is not a replacement for
it); the daemon calls :func:`emit_schedule_tick` right after
``scheduler.tick()``. The adapter only *reads* post-tick state and publishes a
durable event. Throttled to a low-volume heartbeat (``_MIN_INTERVAL_S``), and
its short per-type TTL (``store._TTL_DAYS_BY_TYPE``) self-reaps it — so it
proves "an internal mechanism feeds the spine" without churn.
"""

from __future__ import annotations

import time
from typing import Any

from work_buddy.events import dispatcher
from work_buddy.events.envelope import new_event
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

SOURCE = "/wb/scheduler"
TYPE = "ai.workbuddy.schedule.tick"
_MIN_INTERVAL_S = 300.0  # at most one tick event every ~5 min

_last_emit: float = 0.0


def emit_schedule_tick(scheduler: Any = None, *, force: bool = False) -> bool:
    """Publish a ``schedule.tick`` event, throttled. Returns True if emitted."""
    global _last_emit
    now = time.monotonic()
    if not force and (now - _last_emit) < _MIN_INTERVAL_S:
        return False
    _last_emit = now

    data: dict[str, Any] = {}
    jobs = getattr(scheduler, "jobs", None)
    if jobs is not None:
        try:
            data["job_count"] = len(jobs)
        except Exception:  # pragma: no cover — defensive
            pass

    try:
        dispatcher.publish(new_event(SOURCE, TYPE, data, modality="internal"))
        return True
    except Exception:  # pragma: no cover — defensive; producers never raise up
        logger.debug("cron adapter: emit failed (non-fatal)", exc_info=True)
        return False
