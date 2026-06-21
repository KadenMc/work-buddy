"""work-buddy Events backbone — the durable, fire-and-forget delivery spine.

A first-class system for *event-shaped* facts: a thing happened ("X happened")
— observed from outside (webhook, poll) or declared by work-buddy itself —
published to 0..N consumers that may react asynchronously. The producer does
not know or care who reacts.

This is the **inclusion-rule** boundary: the spine carries only fire-and-forget
facts. Request/response (RPC), observability surfaces, and
process supervision are excluded by category and stay on their existing rails.

Public surface (kept deliberately small):

- ``Event`` / ``new_event`` — the CloudEvents-superset envelope
- ``EventStore`` — the durable SQLite log (inbox-dedup + offsets + DLQ)
- ``publish`` — append (if durable) + immediate lossy fan-out to the dashboard
- ``register_consumer`` / ``DurableConsumer`` — register a durable handler
- protocols: ``Source`` / ``Processor`` / ``Condition`` (interfaces; concrete
  sources/conditions are not yet implemented)

See the ``events`` knowledge unit for the architecture.
"""

from __future__ import annotations

from work_buddy.events.envelope import Event, new_event
from work_buddy.events.store import EventStore

__all__ = [
    "Event",
    "new_event",
    "EventStore",
    # Lazily exposed below to keep import light + avoid cycles.
    "publish",
    "drain",
    "register_consumer",
    "DurableConsumer",
]


def __getattr__(name: str):
    """Lazily expose the dispatcher surface so importing the package stays
    cheap and free of the dashboard/consent import cost until actually used."""
    if name in ("publish", "drain", "register_consumer", "registered_durable_consumers"):
        from work_buddy.events import dispatcher
        return getattr(dispatcher, name)
    if name == "DurableConsumer":
        from work_buddy.events.dispatcher import DurableConsumer
        return DurableConsumer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
