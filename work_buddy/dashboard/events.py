"""Dashboard event bus — in-process pub/sub.

Server-side mutators (clarify pool, tasks, health, preferences, cost
ledger) call ``publish(event_type, payload)`` after a successful write.
The SSE endpoint (``GET /api/events`` in ``service.py``) creates a
subscriber and streams events to one EventSource client.

Cross-process events flow through the messaging service on port 5123
and are bridged into this bus by ``dashboard/messaging_bridge.py``;
publishers there use a sibling helper rather than ``publish``. From the
subscriber's perspective the two sources are indistinguishable.

Single-process, single-user. The bus is process-local — if the
dashboard is ever deployed with multiple workers, replace this with an
external broker (Redis pub/sub, NATS, ...). The ``architecture/event-bus``
knowledge unit tracks the design and the event taxonomy.

## Concurrency model

- One ``EventBus`` instance with N subscribers.
- ``publish`` snapshots the subscriber list under a short lock, then
  pushes to each subscriber's queue outside the lock — so subscriber
  push() never blocks publishers on unrelated subscribers.
- Each subscriber owns a bounded ``deque`` and a ``threading.Condition``.
- A slow / stalled subscriber drops oldest events rather than blocking
  the publisher. SSE clients reconnect on disconnect, so a backed-up
  subscriber is a sign the consumer is gone or dead, not a correctness
  emergency. The drop count is exposed for diagnostics.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Per-subscriber queue cap. Subscribers that fall behind drop oldest
# events rather than blocking publishers. Single-user, mostly idle —
# 1000 is a generous cushion that covers any plausible burst (e.g. an
# IR rebuild publishing many ``cron.job_completed`` siblings) without
# making slow-leak bugs invisible.
DEFAULT_QUEUE_MAX = 1000


class _Subscriber:
    """One queue + condition pair, owned by exactly one consumer."""

    __slots__ = ("_queue", "_cond", "_closed", "_dropped")

    def __init__(self, maxlen: int) -> None:
        self._queue: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._closed = False
        self._dropped = 0

    def push(self, event: dict[str, Any]) -> None:
        with self._cond:
            if self._closed:
                return
            # deque(maxlen=N) silently drops the leftmost element when
            # full; account for it explicitly so we can report the
            # count via ``dropped``.
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                self._dropped += 1
            self._queue.append(event)
            self._cond.notify()

    def pop(self, timeout: float | None) -> dict[str, Any] | None:
        """Pop next event or return None on timeout / close.

        ``None`` means "no event available within ``timeout`` seconds";
        the caller (SSE endpoint) uses this to send keepalive comments
        without conflating timeout with a real event.
        """
        with self._cond:
            if not self._queue and not self._closed:
                self._cond.wait(timeout=timeout)
            if self._queue:
                return self._queue.popleft()
            return None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def dropped(self) -> int:
        return self._dropped


class EventBus:
    """Thread-safe in-process pub/sub.

    Multiple producers publish concurrently; each subscriber gets its
    own queue. Slow subscribers drop oldest events rather than blocking
    producers (bounded ``deque``).
    """

    def __init__(self, queue_max: int = DEFAULT_QUEUE_MAX) -> None:
        self._queue_max = queue_max
        self._subscribers: list[_Subscriber] = []
        # Protects mutations to ``_subscribers``. Held only while
        # adding/removing/snapshotting; never held across a subscriber's
        # push() (which acquires its own per-subscriber condition).
        self._lock = threading.Lock()

    def publish(self, event_type: str, payload: Any = None) -> None:
        """Broadcast an event to every current subscriber.

        Returns immediately. A misbehaving subscriber's push() failure
        does not propagate — publishers are isolated from subscribers.
        """
        event = {
            "event_type": event_type,
            "payload": payload,
            "ts": time.time(),
        }
        with self._lock:
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub.push(event)
            except Exception:
                logger.exception(
                    "EventBus subscriber push failed for %r — dropping event for that subscriber",
                    event_type,
                )

    def subscribe(
        self,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any] | None]:
        """Yield events as they arrive. Generator.

        The subscriber is registered on first iteration and unregistered
        when the generator is closed (consumer breaks out, GC collects,
        or ``.close()`` is called explicitly via try/finally).

        ``timeout`` controls how long ``pop`` blocks before yielding
        ``None``. The SSE endpoint uses a short timeout so it can send
        keepalive comments between real events.
        """
        sub = _Subscriber(self._queue_max)
        with self._lock:
            self._subscribers.append(sub)
        try:
            while True:
                yield sub.pop(timeout)
        finally:
            sub.close()
            with self._lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# ---------------------------------------------------------------------------
# Process-global singleton
# ---------------------------------------------------------------------------

_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_bus() -> EventBus:
    """Return the process-global ``EventBus``, creating it on first use."""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = EventBus()
    return _bus


def publish(event_type: str, payload: Any = None) -> None:
    """Convenience: publish to the process-global bus.

    This is the call site for in-process publishers (clarify pool,
    tasks, health, preferences, cost ledger). Cross-process publishers
    go through ``publish_cross_process`` instead.
    """
    get_bus().publish(event_type, payload)


_is_dashboard_process: bool = False


def mark_dashboard_process() -> None:
    """Declare this process as the dashboard's Flask process.

    After this is called, ``publish_auto`` routes to the in-process
    bus directly. Other processes (sidecar) leave the flag False and
    ``publish_auto`` falls through to ``publish_cross_process``.

    Idempotent. Called once from ``dashboard.service.main()``.
    """
    global _is_dashboard_process
    _is_dashboard_process = True


def publish_auto(event_type: str, payload: Any = None) -> None:
    """Publish on whichever channel is right for this process.

    Use this from cross-cutting mutators (e.g. ``ClarifyPool``) that
    can be called from either the dashboard process or the sidecar
    process. The dashboard process publishes in-process (zero IPC);
    every other process routes through the messaging-service bridge.

    Never raises — like the underlying ``publish_cross_process``,
    failures here must not propagate into a primary-work failure.
    """
    if _is_dashboard_process:
        try:
            publish(event_type, payload)
        except Exception:
            logger.exception("publish_auto: in-process publish failed for %r", event_type)
        return
    publish_cross_process(event_type, payload)


def publish_cross_process(event_type: str, payload: Any = None) -> bool:
    """Publish from a process other than the dashboard's.

    Routes the event through the messaging service so the dashboard's
    bridge consumer (``dashboard/messaging_bridge.py``) can pick it up
    and re-publish on the in-process bus.

    Returns ``True`` on apparent delivery to the messaging service,
    ``False`` if the messaging client couldn't reach the service.
    Failures are logged but do not raise — publishers must not fail
    their primary work because of a missed cross-process event.

    Use this from sidecar-process callers (cron jobs, IR rebuilds,
    email triage, service-health monitor). In-process callers should
    use ``publish`` instead — same event taxonomy, no IPC overhead.
    """
    import json as _json

    try:
        from work_buddy.messaging.client import send_message
    except Exception:
        logger.exception("publish_cross_process: messaging client import failed")
        return False

    envelope = _json.dumps({"event_type": event_type, "payload": payload})
    try:
        result = send_message(
            sender="sidecar",
            recipient="dashboard",
            type="bus.event",
            subject=event_type,
            body=envelope,
        )
    except Exception:
        logger.exception(
            "publish_cross_process: send_message failed for %r", event_type
        )
        return False

    return result is not None


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def start_heartbeat(
    interval: float = 10.0,
    bus: EventBus | None = None,
) -> threading.Event:
    """Start a daemon thread that publishes ``bus.heartbeat`` every ``interval`` seconds.

    Returns a ``threading.Event``; ``set()`` stops the heartbeat
    thread within ``interval`` seconds.

    Dual-purpose:
        - Keeps SSE connections alive (some intermediaries idle-close
          long-lived HTTP responses; the SSE keepalive comment helps,
          a real event helps more).
        - Gives the browser a liveness signal independent of any
          domain-specific event traffic.
    """
    target_bus = bus if bus is not None else get_bus()
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            target_bus.publish("bus.heartbeat", {"interval": interval})
            stop.wait(interval)

    t = threading.Thread(target=_loop, daemon=True, name="event-bus-heartbeat")
    t.start()
    logger.info("Event bus heartbeat started (interval=%.1fs)", interval)
    return stop
