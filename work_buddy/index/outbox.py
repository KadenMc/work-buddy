"""Shared transactional-search-outbox delivery hooks.

Database-backed partitions expose two optional, deliberately small methods::

    pending_search_events(limit=...) -> Sequence[event]
    acknowledge_search_events(events) -> None

The consolidated index owns the ordering around those hooks: snapshot pending
events, complete the locked partition build, then acknowledge exactly that
snapshot.  A crash before acknowledgement leaves the durable domain events
pending and the next build safely replays them.  A forced partition build is
the backfill path; it uses the same acknowledgement boundary.

The hooks are optional so filesystem and legacy IR partitions remain ordinary
discover/diff/build sources without depending on domain outbox concepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from work_buddy.index.partition import get_change_key


DEFAULT_DELIVERY_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class SearchDeliveryBatch:
    """One immutable pre-build snapshot of pending domain events."""

    events: tuple[Any, ...]

    @property
    def count(self) -> int:
        return len(self.events)


def snapshot_pending_events(
    partition: Any,
    *,
    limit: int = DEFAULT_DELIVERY_LIMIT,
) -> SearchDeliveryBatch | None:
    """Read a bounded pending batch when ``partition`` owns an outbox.

    ``None`` means the partition has no transactional outbox contract.  An
    empty batch is still meaningful: it identifies a DB-backed partition whose
    regular incremental build may be its first backfill.
    """

    reader = getattr(partition, "pending_search_events", None)
    if reader is None:
        return None
    events: Sequence[Any] = reader(limit=max(1, int(limit))) or ()
    return SearchDeliveryBatch(tuple(events))


def acknowledge_events(partition: Any, batch: SearchDeliveryBatch | None) -> int:
    """Acknowledge a pre-build batch after the index commit completed.

    The domain method must be idempotent.  It receives full immutable event
    records so a domain may apply its own digest/CAS check while recording
    delivery.  No acknowledgement is attempted for non-outbox partitions or
    empty batches.
    """

    if batch is None or not batch.events:
        return 0
    writer = getattr(partition, "acknowledge_search_events", None)
    if writer is None:
        raise RuntimeError(
            f"partition {getattr(partition, 'name', '<unknown>')!r} exposes "
            "pending search events without an acknowledgement hook"
        )
    writer(batch.events)
    return batch.count


def pending_event_count(partition: Any) -> int | None:
    """Return the immediately visible remaining lag, when cheaply available."""

    counter = getattr(partition, "pending_search_event_count", None)
    if counter is None:
        return None
    return max(0, int(counter()))


def reconciliation_evidence(partition: Any, store: Any) -> dict[str, int]:
    """Compare the current DB source ledger with the just-built index ledger.

    This runs before acknowledgement for outbox partitions. A read failure thus
    leaves events pending. A concurrent post-build mutation appears as a
    non-zero mismatch and a still-pending newer event; the cutover gate can
    retry instead of claiming parity.
    """

    refs = list(partition.discover())
    indexed = store.get_indexed_items(partition.name)
    source = {ref.item_id: ref for ref in refs}
    mismatches = len(set(source) ^ set(indexed))
    change_key = get_change_key(partition)
    for item_id in set(source) & set(indexed):
        ref = source[item_id]
        prior_mtime, prior_hash = indexed[item_id]
        if change_key == "mtime":
            mismatches += int(abs(float(prior_mtime) - float(ref.mtime)) > 1e-6)
        else:
            mismatches += int((prior_hash or "") != (ref.content_hash or ""))
    return {
        "source_items": len(source),
        "indexed_items": len(indexed),
        "parity_mismatches": mismatches,
    }


__all__ = [
    "DEFAULT_DELIVERY_LIMIT",
    "SearchDeliveryBatch",
    "acknowledge_events",
    "pending_event_count",
    "reconciliation_evidence",
    "snapshot_pending_events",
]
