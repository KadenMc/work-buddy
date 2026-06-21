"""Events-backbone ops (agent-facing capabilities).

Each op here is referenced by a ``kind: capability`` knowledge-store unit
carrying a matching ``op`` field (``knowledge/store/events/``).
"""

from __future__ import annotations

from typing import Any

from work_buddy.mcp_server.op_registry import register_op


def event_publish(
    type: str,  # noqa: A002 — mirrors the CloudEvents attribute name
    data: dict[str, Any] | None = None,
    source: str = "/wb/agent",
    durable: bool = True,
    subject: str | None = None,
) -> dict[str, Any]:
    """Publish one event onto the Events backbone (fire-and-forget).

    Durable events are logged (deduped on ``(source, id)``) and delivered
    at-least-once to registered consumers; ``durable=False`` is a lossy
    UI-only fan-out that never hits the log.
    """
    from work_buddy.events.dispatcher import publish
    from work_buddy.events.envelope import new_event

    evt = new_event(
        source, type, data or {}, durable=durable, subject=subject, modality="internal"
    )
    publish(evt)
    return {
        "ok": True,
        "id": evt.id,
        "type": evt.type,
        "source": evt.source,
        "durable": durable,
    }


def _register() -> None:
    register_op("op.wb.event_publish", event_publish)


_register()
