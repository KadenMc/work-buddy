"""Task outbox event contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import threading
from typing import Any, Callable, Protocol


class TaskOutboxStore(Protocol):
    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def mark_outbox_published(self, event_id: str, *, published_at: str) -> bool: ...

    def record_outbox_failure(self, event_id: str, *, error: str) -> bool: ...


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

    published = failed = 0
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
            store.mark_outbox_published(
                event_id,
                published_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            )
            published += 1
        else:
            store.record_outbox_failure(event_id, error=error)
            failed += 1
    return {"published": published, "failed": failed}


def publish_pending_async(store: TaskOutboxStore, *, limit: int = 100) -> None:
    """Schedule a non-blocking cross-process outbox drain."""

    threading.Thread(
        target=publish_pending,
        kwargs={"store": store, "limit": limit},
        daemon=True,
        name="task-outbox-publisher",
    ).start()


__all__ = ["invalidation_payload", "publish_pending", "publish_pending_async"]
