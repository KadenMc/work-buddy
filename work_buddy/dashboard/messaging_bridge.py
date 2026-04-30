"""Cross-process event bridge.

Consumes ``bus.event`` messages from the messaging service (port 5123)
and republishes them onto the dashboard's in-process event bus
(``work_buddy.dashboard.events``). Sidecar-process publishers (cron
jobs, IR rebuilds, email triage, service-health detector) reach the
dashboard via this bridge.

Why a poll, not a push: the messaging service is SQLite + REST and has
no fan-out primitive. ~500 ms cadence is below the user's perception
threshold for any cross-process event we publish (slowest is hourly
cron output) and well within the messaging service's read budget for a
single-user deployment.

Replay semantics: the bridge drains every ``bus.event`` message
currently in the inbox on each poll, including events that arrived
while the dashboard was down. The dashboard's browser-side
``EventSource`` only sees events from the in-process bus *after* it
reconnects, so cross-process events that fire before the page is open
are still lost from the user's perspective — but never lost on the
server side. Acceptable for a dashboard that primarily reflects current
state.
"""

from __future__ import annotations

import json
import logging
import threading

from work_buddy.dashboard.events import EventBus, get_bus

logger = logging.getLogger(__name__)

DASHBOARD_RECIPIENT = "dashboard"
BUS_EVENT_TYPE = "bus.event"
DEFAULT_POLL_INTERVAL = 0.5  # seconds
DEFAULT_FETCH_LIMIT = 50  # max messages drained per poll cycle


def _drain_once(bus: EventBus) -> int:
    """Drain every pending bus.event message and republish on ``bus``.

    Returns the number of events successfully republished (for tests
    and diagnostics).
    """
    from work_buddy.messaging.client import (
        query_messages,
        read_message,
        update_status,
    )

    try:
        msgs = query_messages(
            recipient=DASHBOARD_RECIPIENT,
            status="unread",
            limit=DEFAULT_FETCH_LIMIT,
        )
    except Exception:
        logger.exception("messaging bridge: query_messages failed")
        return 0

    delivered = 0
    for msg in msgs:
        if msg.get("type") != BUS_EVENT_TYPE:
            # Other messages addressed to "dashboard" are handled by
            # other consumers (or none) — leave them alone.
            continue

        msg_id = msg.get("id")
        if not msg_id:
            continue

        # query_messages strips the body; fetch it via read_message.
        full = read_message(msg_id)
        if full is None:
            continue

        body = full.get("body") or ""
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            logger.warning(
                "messaging bridge: bus.event %s body not JSON; marking resolved",
                msg_id,
            )
            update_status(msg_id, "resolved")
            continue

        event_type = envelope.get("event_type")
        if not isinstance(event_type, str):
            logger.warning(
                "messaging bridge: bus.event %s missing event_type; marking resolved",
                msg_id,
            )
            update_status(msg_id, "resolved")
            continue

        try:
            bus.publish(event_type, envelope.get("payload"))
        except Exception:
            logger.exception(
                "messaging bridge: republish failed for %r (msg %s)",
                event_type,
                msg_id,
            )
            # Don't mark resolved on publish failure — allow next cycle to retry.
            continue

        update_status(msg_id, "resolved")
        delivered += 1

    return delivered


def start_messaging_bridge(
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    bus: EventBus | None = None,
) -> threading.Event:
    """Start a daemon thread that drains ``bus.event`` messages.

    Returns a ``threading.Event``; ``set()`` stops the bridge thread
    within ``poll_interval`` seconds.
    """
    target_bus = bus if bus is not None else get_bus()
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                _drain_once(target_bus)
            except Exception:
                # _drain_once already logs on its own failures, but
                # defend the thread against any unexpected escape.
                logger.exception("messaging bridge poll loop crashed; continuing")
            stop.wait(poll_interval)

    t = threading.Thread(
        target=_loop, daemon=True, name="event-bus-messaging-bridge"
    )
    t.start()
    logger.info(
        "Messaging bridge started (poll_interval=%.2fs, recipient=%s, type=%s)",
        poll_interval,
        DASHBOARD_RECIPIENT,
        BUS_EVENT_TYPE,
    )
    return stop
