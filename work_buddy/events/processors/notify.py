"""The ``notify`` action — surface a notification when a source's condition fires.

Mirrors `send_notification` (notifications_ops.py): `create_notification` only
*persists*; surface delivery (Telegram / dashboard / Obsidian) is a separate push
via `SurfaceDispatcher.deliver`, kept **best-effort** — a blocking surface POST
runs on the drain thread, and a delivery failure must never poison the consumer
(→ DLQ). The notification is durably stored regardless.
"""

from __future__ import annotations

from work_buddy.events.envelope import Event
from work_buddy.events.protocol import ProcessorResult, RunContext
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def notify_action(event: Event, source, ctx: RunContext) -> ProcessorResult:
    """Notify the user that ``source`` changed. ``source`` is an EventSourceDef
    (passed in because the action composes its message from `action_params`)."""
    from work_buddy.notifications.models import Notification, SourceType
    from work_buddy.notifications.store import create_notification, mark_delivered

    data = event.data or {}
    params = source.action_params or {}
    title = params.get("title") or f"Watch: {source.name}"
    body = params.get("body") or _default_body(source, data)

    notif = create_notification(
        Notification(
            title=title,
            body=body,
            source=f"events:source:{source.name}",
            source_type=SourceType.PROGRAMMATIC.value,
            tags=["events", "source", source.name],
        )
    )

    delivered: dict = {}
    try:
        from work_buddy.notifications.dispatcher import SurfaceDispatcher

        delivered = SurfaceDispatcher.from_config().deliver(
            notif, mark_delivered_fn=mark_delivered
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "notify action: surface delivery failed (non-fatal) for source %s", source.name
        )

    return ProcessorResult(
        text="notified",
        structured={
            "source": source.name,
            "notification_id": notif.notification_id,
            "delivered": [k for k, v in delivered.items() if v],
        },
    )


def _default_body(source, data: dict) -> str:
    # Keep the reverse-DNS event *type* OUT of the body — a dotted string gets
    # auto-linkified on Telegram. Just the readable diff.
    current = data.get("current")
    prev = data.get("prev")
    return f"{source.name} changed.\n\nnow: {current}\nwas: {prev}"
