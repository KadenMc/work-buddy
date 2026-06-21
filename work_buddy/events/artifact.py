"""Artifact registration for the Events backbone log (offset-aware retention).

Registers the ``events`` table as an artifact so the twice-daily
``artifact_cleanup`` sweep bounds it — mirroring messaging
(``work_buddy/messaging/models.py::_register_messages_artifact``). Imported by
``artifacts.registry.ensure_consumers_loaded`` (this module is listed in
``_CONSUMER_MODULES``) so registration happens wherever the sweep runs.

The retention predicate is the safety constraint: a row is **KEPT** (never
reaped despite its TTL) when it is undelivered to some consumer, sitting in the
DLQ, or — for *external* events only — still inside the provider replay window.
Everything else reaps on its per-row ``expires_at`` (computed at append from a
per-type TTL; see ``store._expires_at_for``). The external-only replay window is
why a high-volume internal type (``schedule.tick``) self-reaps by its short TTL
instead of being pinned for the full replay window.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from work_buddy.events.store import EventStore, _db_path

logger = logging.getLogger(__name__)

_DEDUP_WINDOW_DAYS = 7.0
_SNAPSHOT_TTL_S = 30.0  # memoize min_live_offset + DLQ across a single sweep


def _older_than_days(iso: str | None, days: float) -> bool:
    if not iso:
        return True
    try:
        ts = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) > timedelta(days=days)


def _make_events_keep(store: EventStore) -> Callable[[dict], bool]:
    """Build the retention predicate (**True = keep**) over a live ``EventStore``.

    Snapshots ``min_live_offset`` + DLQ seqs and memoizes them for a few seconds
    so a sweep over many rows stays O(1) in queries, not O(rows).
    """
    snap: dict[str, object] = {"t": None, "min_live": 0, "dlq": set()}

    def _refresh() -> None:
        now = time.monotonic()
        last = snap["t"]
        if last is None or (now - float(last)) > _SNAPSHOT_TTL_S:
            snap["min_live"] = store.min_live_offset()
            snap["dlq"] = store.dlq_seqs()
            snap["t"] = now

    def _events_keep(record: dict) -> bool:
        _refresh()
        try:
            seq = int(record.get("seq", 0))
        except (TypeError, ValueError):
            return True  # unparseable → keep (fail safe)
        if seq > int(snap["min_live"]):  # type: ignore[arg-type]
            return True  # undelivered — TTL must never outrun a consumer
        if seq in snap["dlq"]:  # type: ignore[operator]
            return True  # un-triaged poison
        # Replay window applies to EXTERNAL events only; internal types
        # (e.g. schedule.tick) self-reap by their short TTL once delivered.
        if record.get("modality") in ("push", "pull"):
            if not _older_than_days(record.get("received_at"), _DEDUP_WINDOW_DAYS):
                return True
        return False

    return _events_keep


def _register_events_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            Lifecycle,
            PerRecordTtl,
            register_artifact,
            SqliteRowsStorage,
        )

        store = EventStore()
        register_artifact(Artifact(
            name="events",
            storage=SqliteRowsStorage(
                db_path=_db_path(),
                table="events",
                id_column="seq",
                vacuum_on_delete=True,
            ),
            lifecycle=Lifecycle(
                trigger=PerRecordTtl(ttl_field="expires_at"),  # absolute per-row expiry
                action=Delete(),
                retention_predicate=_make_events_keep(store),
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register events artifact: %s", exc)


_register_events_artifact()
