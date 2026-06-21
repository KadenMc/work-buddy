"""The in-process dispatcher — ``publish`` (append + fan-out) and ``drain``
(durable, at-least-once delivery behind the consent gate).

``publish(event)``: if the event is durable, append it (the ``UNIQUE(source,id)``
insert is the dedup claim — a duplicate is silently dropped); then *immediately*
fan out a lossy projection to the dashboard bus. Durable handler delivery is
**not** inline — it happens on ``drain()``.

``drain()``: for each registered durable consumer, read events ``since`` its
committed offset (capped at ``READ_LIMIT``), run the consent gate, run the
handler, and commit the offset *after* a successful run (at-least-once). A
handler exception is retried up to ``MAX_ATTEMPTS`` times (bounded) before the
event is dead-lettered and the offset advances, so one poison event can't wedge
a consumer.

Consumers are **registry entries, not threads**: a single
``event-drain`` thread (see ``drain.py``) iterates them. An idle consumer costs
one dict entry, not a wakeup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from work_buddy.events.envelope import Event
from work_buddy.events.policy import policy_check
from work_buddy.events.protocol import Processor, RunContext
from work_buddy.events.store import EventStore

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5     # bounded retry before dead-lettering a poison event
READ_LIMIT = 200     # per-consumer batch cap per drain tick (backpressure)


@dataclass
class DurableConsumer:
    """A registered handler the drain delivers events to.

    ``types=None`` means "all types"; otherwise the consumer only runs for
    events whose ``type`` is in the set (its offset still advances past the
    rest, so a narrow consumer never re-reads events it doesn't handle).
    """

    id: str
    processor: Processor
    consent_action: str | None = None
    consent_weight: str = "low"
    types: frozenset[str] | None = None


# --- module state ----------------------------------------------------------

_REGISTRY: dict[str, DurableConsumer] = {}
_store_singleton: EventStore | None = None
# Per-(consumer, seq) attempt counts. Transient by design: a restart resets
# them, so a poison event gets MAX_ATTEMPTS fresh tries after a restart — an
# acceptable trade vs. a DB column for purely in-flight retry state.
_attempts: dict[tuple[str, int], int] = {}


def _store() -> EventStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = EventStore()
    return _store_singleton


def set_store(store: EventStore | None) -> None:
    """Override the store singleton (tests)."""
    global _store_singleton
    _store_singleton = store


def register_consumer(consumer: DurableConsumer) -> None:
    """Register a durable consumer and pin the log from now (so retention can't
    reap ahead of a freshly-registered consumer before it delivers)."""
    _REGISTRY[consumer.id] = consumer
    _store().ensure_offset(consumer.id, 0)


def registered_durable_consumers() -> list[DurableConsumer]:
    return list(_REGISTRY.values())


def clear_consumers() -> None:
    """Clear the registry + in-flight attempt state (tests)."""
    _REGISTRY.clear()
    _attempts.clear()


# --- publish ---------------------------------------------------------------


def publish(event: Event) -> None:
    """Durable append (dedup) + immediate lossy fan-out. See module docstring."""
    if event.durable:
        seq = _store().append(event)
        if seq is None:
            return  # (source, id) duplicate — dropped
    # Immediate, best-effort projection to the dashboard bus (never raises).
    try:
        from work_buddy.dashboard.events import publish_auto

        publish_auto(event.type, event.projection_payload())
    except Exception:  # pragma: no cover — defensive
        logger.debug(
            "events.publish: dashboard fan-out failed for %r (non-fatal)",
            event.type,
            exc_info=True,
        )


# --- drain -----------------------------------------------------------------


def drain() -> dict[str, int]:
    """Deliver queued durable events to each consumer. Returns a small summary
    ``{"delivered": N, "dlq": M}``. Idempotent across ticks via offsets."""
    store = _store()
    delivered = 0
    dead = 0
    for consumer in registered_durable_consumers():
        last = store.get_offset(consumer.id)
        for seq, event in store.read_since(last, limit=READ_LIMIT):
            # Type filter: advance past events this consumer doesn't handle.
            if consumer.types is not None and event.type not in consumer.types:
                store.commit_offset(consumer.id, seq)
                continue

            ctx = RunContext(
                seq=seq, traceparent=event.traceparent, session=event.wb_session
            )
            decision = policy_check(
                consumer.consent_action, ctx, consent_weight=consumer.consent_weight
            )
            if decision == "deny":
                store.commit_offset(consumer.id, seq)
                continue
            if decision == "prompt":
                break  # leave offset; consumer waits on consent, re-tick later

            try:
                consumer.processor.run(event, ctx)
                store.commit_offset(consumer.id, seq)
                _attempts.pop((consumer.id, seq), None)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — bounded-retry + DLQ
                key = (consumer.id, seq)
                _attempts[key] = _attempts.get(key, 0) + 1
                if _attempts[key] >= MAX_ATTEMPTS:
                    store.dead_letter(seq, consumer.id, _attempts[key], repr(exc))
                    store.commit_offset(consumer.id, seq)
                    _attempts.pop(key, None)
                    dead += 1
                    logger.warning(
                        "events.drain: %s seq=%d dead-lettered after %d attempts: %s",
                        consumer.id, seq, MAX_ATTEMPTS, exc,
                    )
                else:
                    logger.warning(
                        "events.drain: %s seq=%d failed (attempt %d/%d): %s",
                        consumer.id, seq, _attempts[key], MAX_ATTEMPTS, exc,
                    )
                    break  # retry this seq next tick; do NOT advance the offset
    return {"delivered": delivered, "dlq": dead}
