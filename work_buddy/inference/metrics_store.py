"""Persistent store for ``LocalInferenceBroker`` call metrics.

The broker keeps only an in-memory 1000-entry ring, wiped on every process
restart (a sidecar reset restarts the embedding-service child). This module
persists COMPLETED calls to a small SQLite table so the dashboard's Inference
panel survives restarts instead of blanking until the ring refills.

Design constraints:

* **The broker stays pure.** Persistence is written out-of-band by a flusher
  daemon in the embedding service (``service.py:_broker_metrics_persist_loop``),
  which passes ``broker.snapshot_metrics()`` rows in. This module never imports
  the broker — so listing it in ``artifacts.registry._CONSUMER_MODULES`` (so the
  cleanup tick discovers the artifact) stays cheap and side-effect-free.
* **Wall-clock at persist time.** ``SlotMetrics`` timestamps are
  ``time.monotonic()`` (process-relative). They are converted to epoch
  wall-clock here using a ``(mono_now, wall_now)`` reference pair captured by the
  caller, and ``completed_at`` is stored as a UTC ISO string — the format the
  ``broker-metrics`` artifact's ``PerRecordTtl`` parses (epoch would silently
  never expire).
* **Terminal rows only.** A terminal ``SlotMetrics`` is immutable once stamped,
  so ``INSERT OR IGNORE`` keyed on ``id`` is correct and idempotent across
  flushes. In-flight rows (``queued`` / ``running``) are skipped — persisting one
  would freeze a stale snapshot under OR-IGNORE.

Aged out by the ``broker-metrics`` artifact (7-day per-record TTL) on the
unified cleanup tick.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.paths import resolve

logger = logging.getLogger(__name__)

# Immutable, persist-eligible statuses. (queued / running are in-flight.)
_TERMINAL_STATUSES = frozenset(
    {"ok", "error", "queue_full", "queue_wait_timeout", "inference_timeout"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_metrics (
    id                TEXT PRIMARY KEY,
    profile           TEXT NOT NULL,
    priority          TEXT,
    status            TEXT NOT NULL,
    error_kind        TEXT,
    error_detail      TEXT,
    queued_at_wall    REAL,
    finished_at_wall  REAL,
    queue_wait_ms     REAL,
    service_time_ms   REAL,
    total_latency_ms  REAL,
    completed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_broker_metrics_finished
    ON broker_metrics(finished_at_wall);
"""

# DB paths whose schema is ensured this process; keyed on resolved path so a
# test pointing at a fresh DB still creates the table exactly once.
_schema_ready: set[str] = set()


def _db_path() -> Path:
    return resolve("db/broker-metrics")


def get_connection() -> sqlite3.Connection:
    """Open (creating + migrating once per path) the broker-metrics DB."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    key = str(path)
    if key not in _schema_ready:
        conn.executescript(_SCHEMA)
        _schema_ready.add(key)
    return conn


def _to_wall(ts: float | None, mono_now: float, wall_now: float) -> float | None:
    """Convert a process-relative ``time.monotonic()`` value to epoch wall-clock."""
    if ts is None:
        return None
    return wall_now - (mono_now - ts)


def persist_terminal_rows(
    rows: list[dict[str, Any]], mono_now: float, wall_now: float
) -> int:
    """Insert completed broker calls into the store; return the count newly written.

    ``rows`` is ``broker.snapshot_metrics()`` output (monotonic timestamps). Only
    terminal-status, finished rows are persisted. Idempotent across flushes via
    ``INSERT OR IGNORE`` on the immutable ``id``. Never raises — persistence is
    best-effort observability.
    """
    terminal = [
        r for r in rows
        if r.get("status") in _TERMINAL_STATUSES and r.get("finished_at") is not None
    ]
    if not terminal:
        return 0
    try:
        conn = get_connection()
        try:
            written = 0
            for r in terminal:
                finished_wall = _to_wall(r.get("finished_at"), mono_now, wall_now)
                completed_at = datetime.fromtimestamp(
                    finished_wall, tz=timezone.utc
                ).isoformat()
                cur = conn.execute(
                    "INSERT OR IGNORE INTO broker_metrics "
                    "(id, profile, priority, status, error_kind, error_detail, "
                    " queued_at_wall, finished_at_wall, queue_wait_ms, "
                    " service_time_ms, total_latency_ms, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r.get("id"),
                        r.get("profile"),
                        r.get("priority"),
                        r.get("status"),
                        r.get("error_kind"),
                        r.get("error_detail"),
                        _to_wall(r.get("queued_at"), mono_now, wall_now),
                        finished_wall,
                        r.get("queue_wait_ms"),
                        r.get("service_time_ms"),
                        r.get("total_latency_ms"),
                        completed_at,
                    ),
                )
                written += cur.rowcount or 0
            conn.commit()
            return written
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("broker-metrics persist failed: %s", exc)
        return 0


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Most-recent persisted calls, newest-first (ordered by the indexed column)."""
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM broker_metrics "
                "ORDER BY finished_at_wall DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("broker-metrics read failed: %s", exc)
        return []


def _init_schema_safe() -> None:
    try:
        get_connection().close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("broker-metrics schema init skipped: %s", exc)


def _register_broker_metrics_artifact() -> None:
    """Register the entry-TTL artifact so the cleanup tick prunes old rows.

    7-day per-record TTL keyed on ``completed_at`` (UTC ISO). No retention
    predicate — every persisted row is terminal, hence TTL-eligible.
    ``vacuum_on_delete=False``: the table is tiny and pruned frequently, so a
    VACUUM (which locks the DB) on every tick is not worth it.
    """
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            Lifecycle,
            PerRecordTtl,
            register_artifact,
            SqliteRowsStorage,
        )

        register_artifact(Artifact(
            name="broker-metrics",
            storage=SqliteRowsStorage(
                db_path=_db_path(),
                table="broker_metrics",
                id_column="id",
                vacuum_on_delete=False,
            ),
            lifecycle=Lifecycle(
                trigger=PerRecordTtl(
                    ttl_field="completed_at",
                    default_ttl_days=7,
                ),
                action=Delete(),
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register broker-metrics artifact: %s", exc)


_init_schema_safe()
_register_broker_metrics_artifact()
