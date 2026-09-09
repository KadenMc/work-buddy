"""Task outbox event contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class TaskOutboxStore(Protocol):
    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def mark_outbox_published(self, event_id: str, *, published_at: str) -> bool: ...

    def record_outbox_failure(self, event_id: str, *, error: str) -> bool: ...


class CollectionOutboxStore(Protocol):
    def pending_outbox_invalidation(self) -> dict[str, int] | None: ...

    def record_outbox_invalidation_delivery(
        self, through_revision: int, *, published_at: str | None = None, error: str | None = None
    ) -> int: ...


def invalidation_payload(
    *,
    task_id: str,
    mutation: str,
    collection_revision: int,
) -> dict[str, Any]:
    """Build the dashboard-compatible invalidation payload."""
    return {
        "app_id": "wb.tasks",
        "view_ids": ["wb.tasks.workspace"],
        "revision": collection_revision,
        "task_id": task_id,
        "mutation": mutation,
    }


def _dashboard_delivery(event_type: str, payload: dict[str, Any]) -> bool:
    """Deliver to the dashboard SSE bus without weakening task durability."""

    try:
        from work_buddy.dashboard import events

        if getattr(events, "_is_dashboard_process", False):
            events.publish(event_type, payload)
            return True
        return bool(events.publish_cross_process(event_type, payload))
    except Exception:
        return False


def publish_pending(
    store: TaskOutboxStore,
    *,
    limit: int = 100,
    delivery: Callable[[str, dict[str, Any]], bool] = _dashboard_delivery,
) -> dict[str, int]:
    """Drain committed invalidations; leave failed deliveries retryable."""

    delivered_ids: list[str] = []
    failed_ids: dict[str, list[str]] = {}
    for event in store.pending_outbox(limit=limit):
        event_id = str(event["event_id"])
        payload = dict(event["payload"])
        try:
            delivered = bool(delivery("task.changed", payload))
        except Exception as exc:  # pragma: no cover - defensive callback seam
            delivered = False
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = "dashboard event delivery unavailable"
        if delivered:
            delivered_ids.append(event_id)
        else:
            failed_ids.setdefault(error, []).append(event_id)
    published_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    batch_published = getattr(store, "mark_outbox_batch_published", None)
    if callable(batch_published):
        batch_published(delivered_ids, published_at=published_at)
    else:
        for event_id in delivered_ids:
            store.mark_outbox_published(event_id, published_at=published_at)
    batch_failed = getattr(store, "record_outbox_batch_failure", None)
    for error, event_ids in failed_ids.items():
        if callable(batch_failed):
            batch_failed(event_ids, error=error)
        else:
            for event_id in event_ids:
                store.record_outbox_failure(event_id, error=error)
    return {"published": len(delivered_ids), "failed": sum(map(len, failed_ids.values()))}


def publish_pending_coalesced(
    store: CollectionOutboxStore,
    *,
    delivery: Callable[[str, dict[str, Any]], bool] = _dashboard_delivery,
) -> dict[str, int | None]:
    """Invalidate the task view once for the complete committed backlog.

    This is a collection notification, not a replacement per-task event:
    consumers that need individual task.changed payloads keep publish_pending.
    A failed delivery (or acknowledgement) leaves durable rows retryable, and
    events committed during delivery remain outside the captured boundary.
    A failed snapshot read reports an unknown failed count (None).
    """
    try:
        batch = store.pending_outbox_invalidation()
    except Exception:
        logger.exception("Task collection invalidation remains pending after snapshot failure")
        return {"published": 0, "failed": None}
    if batch is None:
        return {"published": 0, "failed": 0}
    payload = {
        "app_id": "wb.tasks", "view_ids": ["wb.tasks.workspace"],
        "revision": batch["revision"], "scope": "collection", "event_count": batch["event_count"],
    }
    error = "dashboard event delivery unavailable"
    try:
        delivered = bool(delivery("task.collection_changed", payload))
    except Exception as exc:
        delivered = False
        error = f"{type(exc).__name__}: {exc}"
    try:
        if delivered:
            published = store.record_outbox_invalidation_delivery(
                batch["revision"],
                published_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            )
            return {"published": published, "failed": 0}
        failed = store.record_outbox_invalidation_delivery(batch["revision"], error=error)
        return {"published": 0, "failed": failed}
    except Exception:
        # Notification bookkeeping follows an already committed mutation. A
        # transient acknowledgement failure must not turn its HTTP response
        # into a false mutation failure; at-least-once retry is safe here.
        logger.exception("Task collection invalidation remains pending after acknowledgement failure")
        return {"published": 0, "failed": batch["event_count"]}


def publish_pending_async(store: TaskOutboxStore, *, limit: int = 100) -> None:
    """Schedule a non-blocking cross-process outbox drain."""

    threading.Thread(
        target=publish_pending,
        kwargs={"store": store, "limit": limit},
        daemon=True,
        name="task-outbox-publisher",
    ).start()


__all__ = ["invalidation_payload", "publish_pending", "publish_pending_coalesced", "publish_pending_async"]
