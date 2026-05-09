"""LLM-call priority queue.

**Owned by the LLM-calling subsystem, NOT by Threads.** This is
load-bearing per DESIGN.md §9.2: the queue is general infrastructure
reusable by any client (Thread system, scheduled jobs, agents, batch
ops). Building it inside the Thread package would tie general
infrastructure to one client and force every other client to either
reinvent or reach across module boundaries.

Stage 1.6 deliverable: schema, publisher API, and minimum CRUD. The
**dispatcher loop** (the worker that pulls pending entries and
actually calls an LLM) is Stage 2 work.

Schema
------

A single ``llm_call_queue`` table. Each row represents one LLM-call
request. Status transitions:

    pending → in_flight → done       (on success)
                       → failed      (on error)
                       → cancelled   (caller cancelled before dispatch)
    pending → rejected               (e.g. budget exceeded at enqueue)

Public API
----------

- ``enqueue(...)``                 — publisher
- ``dequeue(worker_id)``           — atomic claim by a dispatcher
- ``complete(entry_id, result)``   — dispatcher reports success
- ``fail(entry_id, error_text)``   — dispatcher reports failure
- ``reject(entry_id, reason)``     — pre-dispatch rejection
- ``cancel(entry_id)``             — caller cancels a pending entry
- ``get_entry(entry_id)``          — read one entry
- ``peek_pending(limit)``          — visibility into the queue
- ``status_for_caller(caller_id)`` — counts by status

Caller IDs follow ``"<kind>:<id>"`` convention (e.g.
``"thread:th-abc123"``). The ``caller_kind`` column is also
populated explicitly for filtered queries.

DESIGN.md §9.2 (priority queue scope), §9.4 (budget enforcement
hook), §14 (sidecar workers) are the spec.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Caller-kind constants
# ---------------------------------------------------------------------------

CALLER_THREAD = "thread"
CALLER_SCHEDULED_JOB = "scheduled_job"
CALLER_AGENT = "agent"
CALLER_BATCH = "batch"
CALLER_OTHER = "other"

ALL_CALLER_KINDS: frozenset[str] = frozenset({
    CALLER_THREAD,
    CALLER_SCHEDULED_JOB,
    CALLER_AGENT,
    CALLER_BATCH,
    CALLER_OTHER,
})


# ---------------------------------------------------------------------------
# Status values
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

ALL_STATUSES: frozenset[str] = frozenset({
    STATUS_PENDING, STATUS_IN_FLIGHT, STATUS_DONE,
    STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
})

TERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED,
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class QueueRejected(RuntimeError):
    """Raised by enqueue when a pre-flight budget/admission check
    rejects the request (e.g. caller's per-Thread budget would be
    exceeded). The entry IS recorded with status='rejected' for
    audit; this exception lets the caller distinguish 'queued OK'
    from 'queue refused me'.
    """


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    """Resolve the queue DB path. Tests monkeypatch this."""
    from work_buddy.paths import resolve
    return resolve("db/llm_queue")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_call_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Who owns this request
    caller_id           TEXT NOT NULL,           -- e.g. 'thread:th-abc123'
    caller_kind         TEXT NOT NULL,           -- thread | scheduled_job | agent | batch | other

    -- What to do
    target              TEXT NOT NULL,           -- 'intent' | 'context' | 'action' | ... (free-form)
    priority            INTEGER NOT NULL DEFAULT 100,  -- LOWER = higher priority
    payload_json        TEXT NOT NULL DEFAULT '{}',
    tier_hint           TEXT,                    -- ModelTier value, or NULL
    estimated_cost_usd  REAL NOT NULL DEFAULT 0,

    -- Lifecycle
    status              TEXT NOT NULL DEFAULT 'pending',
    enqueued_at         TEXT NOT NULL,
    dequeued_at         TEXT,
    completed_at        TEXT,

    -- Worker tracking
    worker_id           TEXT,                    -- which worker claimed this entry
    result_json         TEXT,                    -- on done
    error_text          TEXT,                    -- on failed
    rejection_reason    TEXT                     -- on rejected
);

CREATE INDEX IF NOT EXISTS idx_queue_pending_priority
    ON llm_call_queue(priority, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_queue_caller
    ON llm_call_queue(caller_id);
CREATE INDEX IF NOT EXISTS idx_queue_status
    ON llm_call_queue(status);
"""


def get_connection() -> sqlite3.Connection:
    """Open the queue DB with WAL mode and ensure schema."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_json(value: Any) -> str:
    if value is None:
        return "{}"
    return json.dumps(value, default=str)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pre-enqueue admission hook (Stage 1.8 budget hook lives here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    admit: bool
    reason: Optional[str] = None  # populated when admit=False


# Hook signature: ``(caller_id, caller_kind, target, payload, tier_hint, estimated_cost_usd) -> AdmissionDecision``
#
# Registered by ``register_admission_hook``. Multiple hooks compose:
# any False decision rejects. The hook list is intentionally global
# state — this is process-wide infrastructure.
_ADMISSION_HOOKS: list[Any] = []


def register_admission_hook(hook: Any) -> None:
    """Register a callable to run before each enqueue.

    Hook returns :class:`AdmissionDecision`. Multiple hooks compose;
    the first ``admit=False`` decision wins. Used by the per-Thread
    budget enforcement (Stage 1.8): when wired, the budget hook
    queries the Thread's cumulative cost and rejects if the new
    enqueue would exceed ``budget_usd``.
    """
    _ADMISSION_HOOKS.append(hook)


def clear_admission_hooks() -> None:
    """Test/utility: drop all registered hooks."""
    _ADMISSION_HOOKS.clear()


def _run_admission_checks(
    caller_id: str,
    caller_kind: str,
    target: str,
    payload: dict[str, Any],
    tier_hint: Optional[str],
    estimated_cost_usd: float,
) -> AdmissionDecision:
    for hook in _ADMISSION_HOOKS:
        decision = hook(
            caller_id=caller_id,
            caller_kind=caller_kind,
            target=target,
            payload=payload,
            tier_hint=tier_hint,
            estimated_cost_usd=estimated_cost_usd,
        )
        if not decision.admit:
            return decision
    return AdmissionDecision(admit=True)


# ---------------------------------------------------------------------------
# QueueEntry
# ---------------------------------------------------------------------------


@dataclass
class QueueEntry:
    id: int
    caller_id: str
    caller_kind: str
    target: str
    priority: int
    payload: dict[str, Any]
    tier_hint: Optional[str]
    estimated_cost_usd: float
    status: str
    enqueued_at: str
    dequeued_at: Optional[str]
    completed_at: Optional[str]
    worker_id: Optional[str]
    result: Optional[dict[str, Any]]
    error_text: Optional[str]
    rejection_reason: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "caller_id": self.caller_id,
            "caller_kind": self.caller_kind,
            "target": self.target,
            "priority": self.priority,
            "payload": self.payload,
            "tier_hint": self.tier_hint,
            "estimated_cost_usd": self.estimated_cost_usd,
            "status": self.status,
            "enqueued_at": self.enqueued_at,
            "dequeued_at": self.dequeued_at,
            "completed_at": self.completed_at,
            "worker_id": self.worker_id,
            "result": self.result,
            "error_text": self.error_text,
            "rejection_reason": self.rejection_reason,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> QueueEntry:
        return cls(
            id=row["id"],
            caller_id=row["caller_id"],
            caller_kind=row["caller_kind"],
            target=row["target"],
            priority=row["priority"],
            payload=_load_json(row.get("payload_json"), {}),
            tier_hint=row.get("tier_hint"),
            estimated_cost_usd=row.get("estimated_cost_usd") or 0.0,
            status=row["status"],
            enqueued_at=row["enqueued_at"],
            dequeued_at=row.get("dequeued_at"),
            completed_at=row.get("completed_at"),
            worker_id=row.get("worker_id"),
            result=_load_json(row.get("result_json"), None),
            error_text=row.get("error_text"),
            rejection_reason=row.get("rejection_reason"),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(
    *,
    caller_id: str,
    caller_kind: str,
    target: str,
    priority: int = 100,
    payload: Optional[dict[str, Any]] = None,
    tier_hint: Optional[str] = None,
    estimated_cost_usd: float = 0.0,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Enqueue an LLM-call request. Returns the new entry's id.

    Runs registered admission hooks before insert. If any hook
    returns ``admit=False``, the entry IS recorded (with
    status='rejected' and ``rejection_reason`` populated) and a
    :class:`QueueRejected` exception is raised. This gives callers
    a typed signal AND keeps a durable audit trail.
    """
    if caller_kind not in ALL_CALLER_KINDS:
        raise ValueError(
            f"Unknown caller_kind {caller_kind!r}; must be one of "
            f"{sorted(ALL_CALLER_KINDS)}",
        )
    payload = payload or {}

    decision = _run_admission_checks(
        caller_id=caller_id,
        caller_kind=caller_kind,
        target=target,
        payload=payload,
        tier_hint=tier_hint,
        estimated_cost_usd=estimated_cost_usd,
    )

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        if not decision.admit:
            cur = conn.execute(
                """INSERT INTO llm_call_queue
                   (caller_id, caller_kind, target, priority, payload_json,
                    tier_hint, estimated_cost_usd, status, enqueued_at,
                    rejection_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    caller_id, caller_kind, target, priority,
                    _dump_json(payload), tier_hint, estimated_cost_usd,
                    STATUS_REJECTED, _now_iso(), decision.reason or "rejected",
                ),
            )
            conn.commit()
            entry_id = cur.lastrowid
            raise QueueRejected(
                f"enqueue rejected for {caller_id} (entry id={entry_id}): "
                f"{decision.reason or '<no reason>'}",
            )

        cur = conn.execute(
            """INSERT INTO llm_call_queue
               (caller_id, caller_kind, target, priority, payload_json,
                tier_hint, estimated_cost_usd, status, enqueued_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                caller_id, caller_kind, target, priority,
                _dump_json(payload), tier_hint, estimated_cost_usd,
                STATUS_PENDING, _now_iso(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if own_conn:
            conn.close()


def dequeue(
    worker_id: str,
    *,
    caller_kind: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[QueueEntry]:
    """Atomically claim the highest-priority pending entry.

    Uses an immediate transaction so two concurrent workers never
    claim the same row. Returns None if the queue is empty.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        clauses = ["status = ?"]
        params: list[Any] = [STATUS_PENDING]
        if caller_kind is not None:
            clauses.append("caller_kind = ?")
            params.append(caller_kind)
        sql = (
            f"SELECT * FROM llm_call_queue WHERE {' AND '.join(clauses)} "
            f"ORDER BY priority ASC, id ASC LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        now = _now_iso()
        conn.execute(
            "UPDATE llm_call_queue SET status = ?, dequeued_at = ?, "
            "worker_id = ? WHERE id = ?",
            (STATUS_IN_FLIGHT, now, worker_id, row["id"]),
        )
        conn.execute("COMMIT")
        # Re-fetch to get the updated row
        updated = conn.execute(
            "SELECT * FROM llm_call_queue WHERE id = ?", (row["id"],)
        ).fetchone()
        return QueueEntry.from_row(dict(updated))
    finally:
        if own_conn:
            conn.close()


def complete(
    entry_id: int,
    result: dict[str, Any],
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE llm_call_queue SET status = ?, completed_at = ?, "
            "result_json = ? WHERE id = ? AND status = ?",
            (STATUS_DONE, _now_iso(), _dump_json(result), entry_id,
             STATUS_IN_FLIGHT),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def fail(
    entry_id: int,
    error_text: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE llm_call_queue SET status = ?, completed_at = ?, "
            "error_text = ? WHERE id = ? AND status = ?",
            (STATUS_FAILED, _now_iso(), error_text, entry_id,
             STATUS_IN_FLIGHT),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def cancel(
    entry_id: int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Cancel a pending entry. No-op for already-claimed/terminal entries.

    Returns True if a cancellation actually happened.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE llm_call_queue SET status = ?, completed_at = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_CANCELLED, _now_iso(), entry_id, STATUS_PENDING),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            conn.close()


def get_entry(
    entry_id: int,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[QueueEntry]:
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM llm_call_queue WHERE id = ?", (entry_id,)
        ).fetchone()
        return QueueEntry.from_row(dict(row)) if row else None
    finally:
        if own_conn:
            conn.close()


def peek_pending(
    *,
    limit: int = 10,
    caller_kind: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[QueueEntry]:
    """Return the next ``limit`` pending entries in dispatch order.

    Read-only; does NOT claim anything. Useful for visibility.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        clauses = ["status = ?"]
        params: list[Any] = [STATUS_PENDING]
        if caller_kind is not None:
            clauses.append("caller_kind = ?")
            params.append(caller_kind)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM llm_call_queue WHERE {' AND '.join(clauses)} "
            f"ORDER BY priority ASC, id ASC LIMIT ?",
            params,
        ).fetchall()
        return [QueueEntry.from_row(dict(r)) for r in rows]
    finally:
        if own_conn:
            conn.close()


def status_for_caller(
    caller_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, int]:
    """Return ``{status: count}`` for entries owned by ``caller_id``."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM llm_call_queue "
            "WHERE caller_id = ? GROUP BY status",
            (caller_id,),
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Auto-init schema on first import (best-effort)
# ---------------------------------------------------------------------------


def _init_schema_safe() -> None:
    try:
        conn = get_connection()
        conn.close()
    except Exception as e:
        logger.warning("LLM-call queue schema init skipped: %s", e)


_init_schema_safe()


# ---------------------------------------------------------------------------
# Lifecycle registration — llm-queue artifact
# ---------------------------------------------------------------------------
#
# The queue previously had NO pruner — every terminal-status row stuck
# around forever (UPDATE on completion, no DELETE). Audit confirmed
# rows accumulate indefinitely, with each row carrying the full prompt
# JSON + LLM response (~10-100 KB / row). This registration brings the
# queue under the unified cleanup tick.
#
# Storage: SqliteRowsStorage(db/llm_queue, table=llm_call_queue, id=id)
# Lifecycle: PerRecordTtl(completed_at, default_ttl_days=30) + Delete +
#   retention_predicate(keep pending/in_flight rows regardless of age)

def _llm_queue_keep_live(record: dict) -> bool:
    """Return True (keep) for queue rows still in flight.

    pending and in_flight rows must NEVER be deleted — they're load-
    bearing for the dispatcher. Only terminal statuses (done, failed,
    cancelled, rejected) are eligible for deletion.
    """
    return record.get("status") in ("pending", "in_flight")


def _register_llm_queue_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            Lifecycle,
            PerRecordTtl,
            register_artifact,
            SqliteRowsStorage,
        )
        from work_buddy.paths import resolve

        register_artifact(Artifact(
            name="llm-queue",
            storage=SqliteRowsStorage(
                db_path=resolve("db/llm_queue"),
                table="llm_call_queue",
                id_column="id",
                vacuum_on_delete=True,
            ),
            lifecycle=Lifecycle(
                trigger=PerRecordTtl(
                    ttl_field="completed_at",
                    default_ttl_days=30,
                ),
                action=Delete(),
                retention_predicate=_llm_queue_keep_live,
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to register llm-queue artifact: %s", exc)


_register_llm_queue_artifact()
