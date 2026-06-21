"""Demo consumer: turn ``ai.workbuddy.demo.ping`` events into a user
notification — the visible end-to-end proof that the spine *delivers*.

Low-risk: ``consent_action=None`` (no gate). It subscribes to a single type, so
its offset advances past everything else (e.g. ``schedule.tick``) without acting.
"""

from __future__ import annotations

from work_buddy.events.dispatcher import DurableConsumer, register_consumer
from work_buddy.events.protocol import ProcessorManifest, ProcessorResult
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

CONSUMER_ID = "events.notify-demo"
PING_TYPE = "ai.workbuddy.demo.ping"


class NotifyDemoProcessor:
    """Writes a notification for each delivered ``demo.ping`` event."""

    manifest = ProcessorManifest(
        name="notify-demo",
        description="Demo: notify the user on ai.workbuddy.demo.ping",
        consent_action=None,
        consent_weight="low",
    )

    def run(self, event, ctx):
        from work_buddy.notifications.models import Notification, SourceType
        from work_buddy.notifications.store import create_notification, mark_delivered

        data = event.data or {}
        message = data.get("message") or "An event reached the Events backbone."
        # Keep the reverse-DNS event *type* (e.g. ai.workbuddy.demo.ping) OUT of
        # the body: it looks like a hostname, so Telegram auto-linkifies it. A
        # short hex id + seq don't linkify; the full type rides the structured
        # result for diagnostics.
        notif = create_notification(Notification(
            title="Events backbone",
            body=f"{message}\n\n(spine event {event.id[:8]}, seq {ctx.seq})",
            source="events:notify-demo",
            source_type=SourceType.PROGRAMMATIC.value,
            tags=["events", "demo"],
        ))

        # create_notification only *persists*; surface delivery (Telegram /
        # dashboard / Obsidian) is a separate push via the dispatcher. Keep it
        # **best-effort**: a blocking surface POST runs on the drain thread, and
        # a delivery failure must never poison the consumer (→ DLQ) — the
        # notification is already durably stored regardless.
        delivered: dict = {}
        try:
            from work_buddy.notifications.dispatcher import SurfaceDispatcher

            delivered = SurfaceDispatcher.from_config().deliver(
                notif, mark_delivered_fn=mark_delivered
            )
        except Exception:  # pragma: no cover — defensive
            logger.exception("notify-demo: surface delivery failed (non-fatal)")

        return ProcessorResult(
            text="notified",
            structured={
                "event_id": event.id,
                "event_type": event.type,
                "notification_id": notif.notification_id,
                "delivered": [k for k, v in delivered.items() if v],
            },
        )


def register_notify_demo() -> None:
    """Register the demo consumer with the dispatcher (called at sidecar boot)."""
    register_consumer(DurableConsumer(
        id=CONSUMER_ID,
        processor=NotifyDemoProcessor(),
        consent_action=None,
        types=frozenset({PING_TYPE}),
    ))
    logger.info("events: registered notify-demo consumer (%s)", PING_TYPE)
