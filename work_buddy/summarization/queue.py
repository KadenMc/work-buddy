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
    last_error_kind TEXT,
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
    last_error_kind TEXT,
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
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(summarization_queue)")
        }
        if "last_error_kind" not in columns:
            conn.execute(
                "ALTER TABLE summarization_queue ADD COLUMN last_error_kind TEXT"
            )
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
            "  enqueued_at=excluded.enqueued_at, "
            "  attempts=0, last_error=NULL, last_error_kind=NULL",
            (namespace, item_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def dequeue_eligible(
    namespace: str | None = None,
    cooldown_minutes: int = 30,
    limit: int | None = None,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Return queue entries eligible for processing — strictly FIFO over the
    eligible subset (cooldown-passed).

    A row is "eligible" iff:
    - `now - last_summarized_at >= cooldown_minutes` (using `summary_items.generated_at`)
    - OR no row in `summary_items` (never summarized)

    Doesn't remove rows from the queue — the worker calls `remove(...)`
    after successful processing, or `record_failure(...)` on failure.

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
            "       q.last_error, q.last_error_kind, "
            "       s.generated_at AS last_summarized_at "
            "FROM summarization_queue q "
            "LEFT JOIN summary_items s "
            "  ON q.namespace = s.namespace AND q.item_id = s.item_id "
        )
        where = ["q.attempts < ?"]
        params.append(max_attempts)
        if namespace:
            where.append("q.namespace = ?")
            params.append(namespace)
        sql += "WHERE " + " AND ".join(where) + " "
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


def record_failure(
    namespace: str,
    item_id: str,
    error: str | None,
    *,
    error_kind: str | None,
    count_attempt: bool,
) -> None:
    """Record and rotate a failure so the next queue item gets a turn.

    Environmental failures remain retryable without consuming the intrinsic
    attempt budget.  Intrinsic failures increment ``attempts`` and become a
    visible dead letter once they reach the worker's configured cap.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE summarization_queue SET "
            "  attempts = attempts + ?, "
            "  last_error = ?, last_error_kind = ?, enqueued_at = ? "
            "WHERE namespace = ? AND item_id = ?",
            (
                1 if count_attempt else 0,
                error,
                error_kind,
                now,
                namespace,
                item_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_attempt(namespace: str, item_id: str, error: str | None) -> None:
    """Backward-compatible intrinsic/unknown failure recorder."""
    record_failure(
        namespace,
        item_id,
        error,
        error_kind="unknown",
        count_attempt=True,
    )


def queue_depth(
    namespace: str | None = None,
    *,
    include_dead_letters: bool = True,
    max_attempts: int = 3,
) -> int:
    """Return the count of queued items. Cheap; for dashboard/observability."""
    conn = get_connection()
    try:
        ensure_queue_table(conn)
        where: list[str] = []
        params: list[Any] = []
        if namespace:
            where.append("namespace = ?")
            params.append(namespace)
        if not include_dead_letters:
            where.append("attempts < ?")
            params.append(max_attempts)
        sql = "SELECT COUNT(*) AS n FROM summarization_queue"
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = conn.execute(sql, params).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def queue_snapshot(
    namespace: str | None = None,
    *,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Return a snapshot of all queued items for dashboard rendering."""
    conn = get_connection()
    try:
        ensure_queue_table(conn)
        params: list[Any] = []
        sql = (
            "SELECT q.namespace, q.item_id, q.enqueued_at, q.attempts, "
            "       q.last_error, q.last_error_kind, "
            "       s.generated_at AS last_summarized_at "
            "FROM summarization_queue q "
            "LEFT JOIN summary_items s "
            "  ON q.namespace = s.namespace AND q.item_id = s.item_id "
        )
        if namespace:
            sql += "WHERE q.namespace = ? "
            params.append(namespace)
        sql += "ORDER BY q.enqueued_at ASC"
        result = [dict(row) for row in conn.execute(sql, params)]
        for item in result:
            item["dead_lettered"] = item["attempts"] >= max_attempts
        return result
    finally:
        conn.close()


def queue_stats(
    namespace: str | None = None,
    *,
    max_attempts: int = 3,
) -> dict[str, int]:
    """Return active/dead-letter counts without hiding either population."""
    snapshot = queue_snapshot(namespace, max_attempts=max_attempts)
    dead = sum(1 for item in snapshot if item["dead_lettered"])
    return {"active": len(snapshot) - dead, "dead_lettered": dead, "total": len(snapshot)}
