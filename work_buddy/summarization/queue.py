"""Summarization queue — event-driven trigger for v2 refresh.

PRD §6 O1-O11. Replaces v1's 2h-cron with a queue drained by a worker that
piggybacks on the existing 5-minute `conversation-observability-refresh`
cadence.

The queue lives in `summarization.db` alongside `summary_items` /
`summary_nodes`. One row per pending `(namespace, item_id)`; re-enqueueing
an already-queued session updates `enqueued_at` rather than duplicating.

Cooldown is enforced at *dequeue* time — a session in cooldown stays in
the queue but the worker skips it on its tick. Daily-budget circuit
breaker is also a dequeue-time check.

Schema:

```sql
CREATE TABLE summarization_queue (
    namespace   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    PRIMARY KEY (namespace, item_id)
);
```
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from work_buddy.summarization.db import get_connection

logger = logging.getLogger(__name__)


_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS summarization_queue (
    namespace   TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    PRIMARY KEY (namespace, item_id)
);
CREATE INDEX IF NOT EXISTS idx_summarization_queue_enqueued_at
    ON summarization_queue(enqueued_at);
"""


def ensure_queue_table(conn=None) -> None:
    """Idempotent queue table creation. Called by enqueue/dequeue paths."""
    if conn is None:
        conn = get_connection()
        owned = True
    else:
        owned = False
    try:
        conn.executescript(_QUEUE_SCHEMA)
        conn.commit()
    finally:
        if owned:
            conn.close()


def enqueue(namespace: str, item_id: str) -> None:
    """Add `(namespace, item_id)` to the queue, or refresh its `enqueued_at`
    if already present.

    Idempotent: re-enqueuing a session updates `enqueued_at` (so an
    actively-changing session doesn't starve other queued work — its
    position resets but it doesn't pre-empt; PRD §6 O10).
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        ensure_queue_table(conn)
        conn.execute(
            "INSERT INTO summarization_queue "
            "(namespace, item_id, enqueued_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(namespace, item_id) DO UPDATE SET "
            "  enqueued_at=excluded.enqueued_at",
            (namespace, item_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def dequeue_eligible(
    namespace: str | None = None,
    cooldown_minutes: int = 30,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return queue entries eligible for processing — strictly FIFO over the
    eligible subset (cooldown-passed).

    A row is "eligible" iff:
    - `now - last_summarized_at >= cooldown_minutes` (using `summary_items.generated_at`)
    - OR no row in `summary_items` (never summarized)

    Doesn't remove rows from the queue — the worker calls `remove(...)`
    after successful processing, or `record_attempt(...)` on failure.

    Returns dicts with `namespace`, `item_id`, `enqueued_at`, `attempts`,
    `last_error`, `last_summarized_at` (None if never).
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - cooldown_minutes * 60

    conn = get_connection()
    try:
        ensure_queue_table(conn)
        params: list[Any] = []
        sql = (
            "SELECT q.namespace, q.item_id, q.enqueued_at, q.attempts, "
            "       q.last_error, s.generated_at AS last_summarized_at "
            "FROM summarization_queue q "
            "LEFT JOIN summary_items s "
            "  ON q.namespace = s.namespace AND q.item_id = s.item_id "
        )
        if namespace:
            sql += "WHERE q.namespace = ? "
            params.append(namespace)
        sql += "ORDER BY q.enqueued_at ASC"
        rows = list(conn.execute(sql, params))
    finally:
        conn.close()

    eligible: list[dict[str, Any]] = []
    for row in rows:
        last = row["last_summarized_at"]
        if last is not None:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt.timestamp() > cutoff:
                    continue  # in cooldown
            except ValueError:
                # Bad timestamp — treat as eligible.
                pass
        eligible.append(dict(row))
        if limit is not None and len(eligible) >= limit:
            break
    return eligible


def remove(namespace: str, item_id: str) -> None:
    """Remove a queue entry after successful processing."""
    conn = get_connection()
    try:
        conn.execute(
            "DELETE FROM summarization_queue "
            "WHERE namespace = ? AND item_id = ?",
            (namespace, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def record_attempt(namespace: str, item_id: str, error: str | None) -> None:
    """Record an attempt — increment attempts; stash last_error if any."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE summarization_queue SET "
            "  attempts = attempts + 1, "
            "  last_error = ? "
            "WHERE namespace = ? AND item_id = ?",
            (error, namespace, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def queue_depth(namespace: str | None = None) -> int:
    """Return the count of queued items. Cheap; for dashboard/observability."""
    conn = get_connection()
    try:
        ensure_queue_table(conn)
        if namespace:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM summarization_queue "
                "WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM summarization_queue"
            ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def queue_snapshot(namespace: str | None = None) -> list[dict[str, Any]]:
    """Return a snapshot of all queued items for dashboard rendering."""
    conn = get_connection()
    try:
        ensure_queue_table(conn)
        params: list[Any] = []
        sql = (
            "SELECT q.namespace, q.item_id, q.enqueued_at, q.attempts, "
            "       q.last_error, s.generated_at AS last_summarized_at "
            "FROM summarization_queue q "
            "LEFT JOIN summary_items s "
            "  ON q.namespace = s.namespace AND q.item_id = s.item_id "
        )
        if namespace:
            sql += "WHERE q.namespace = ? "
            params.append(namespace)
        sql += "ORDER BY q.enqueued_at ASC"
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()
