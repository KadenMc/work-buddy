"""The ``Event`` envelope — a CloudEvents v1.0 superset (no SDK).

A frozen dataclass: CloudEvents-core fields plus work-buddy extension
attributes. This is the *general form* of the two in-tree domain logs
(``thread_events`` / ``work_item_events``); the envelope is kept field-compatible
with ``work_item_events``. Routing and dedup read context attributes only; the
opaque ``data`` payload is never parsed to route.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    """One immutable event on the spine.

    Identity is ``(source, id)`` → the dedup key. ``dedup_key`` defaults to
    ``f"{source}::{id}"`` and is only overridden when a producer needs a
    coarser identity (e.g. poll-diff emitting one event per changed field).
    """

    # --- CloudEvents core ---
    id: str
    source: str                       # URI-ref, e.g. "/wb/scheduler"
    type: str                         # reverse-DNS, e.g. "ai.workbuddy.schedule.tick"
    data: dict[str, Any]              # opaque, JSON-serializable
    time: str                         # RFC3339; occurred (push) or observed (pull)
    specversion: str = "1.0"
    subject: str | None = None
    datacontenttype: str = "application/json"
    dataschema: str | None = None
    # --- work-buddy extension attributes ---
    modality: str = "internal"        # "push" | "pull" | "internal"
    durable: bool = True              # False => lossy fan-out only, never logged
    dedup_key: str | None = None      # defaults to f"{source}::{id}"
    idempotency_key: str | None = None
    traceparent: str | None = None    # W3C trace-context
    wb_session: str | None = None
    workflow_run_id: str | None = None
    ext: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dedup_key is None:
            object.__setattr__(self, "dedup_key", f"{self.source}::{self.id}")

    def projection_payload(self) -> dict[str, Any]:
        """A small JSON-serializable dict for the lossy dashboard fan-out.

        Deliberately *not* the full envelope — the projection is for live UI
        display, not reliable reaction (which reads the durable log).
        """
        return {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "subject": self.subject,
            "time": self.time,
            "modality": self.modality,
            "data": self.data,
        }


def new_event(
    source: str,
    type: str,  # noqa: A002 — mirrors the CloudEvents attribute name
    data: dict[str, Any] | None = None,
    *,
    id: str | None = None,  # noqa: A002 — CloudEvents attribute name
    time: str | None = None,
    durable: bool = True,
    modality: str = "internal",
    subject: str | None = None,
    dedup_key: str | None = None,
    idempotency_key: str | None = None,
    traceparent: str | None = None,
    wb_session: str | None = None,
    workflow_run_id: str | None = None,
    ext: dict[str, Any] | None = None,
) -> Event:
    """Construct an :class:`Event`, filling ``id`` (uuid) and ``time`` (now)
    when not supplied. The ergonomic way producers create events."""
    return Event(
        id=id or uuid.uuid4().hex,
        source=source,
        type=type,
        data=data or {},
        time=time or _now_iso(),
        durable=durable,
        modality=modality,
        subject=subject,
        dedup_key=dedup_key,
        idempotency_key=idempotency_key,
        traceparent=traceparent,
        wb_session=wb_session,
        workflow_run_id=workflow_run_id,
        ext=ext or {},
    )
