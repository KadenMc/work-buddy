"""SQLite task metadata store — external storage for task attributes.

The markdown task line stays clean (just #todo, text, #projects/*, 🆔, and
plugin emojis). All work-buddy metadata (state, urgency, complexity,
contract link, review dates, state history) lives here, keyed by task ID.

The store is the source of truth for work-buddy metadata. The Obsidian Tasks
plugin cache is the source of truth for plugin-owned data (checkbox, dates,
priority emojis). They don't overlap.

Schema follows the messaging/models.py pattern: SQLite with WAL mode,
row_factory=sqlite3.Row, auto-create on first access.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS task_metadata (
    task_id         TEXT PRIMARY KEY,   -- e.g. 't-a3f8c1e2'
    state           TEXT NOT NULL DEFAULT 'inbox',
    urgency         TEXT NOT NULL DEFAULT 'medium',
    complexity      TEXT,               -- 'simple', 'moderate', 'complex', or NULL
    contract        TEXT,               -- contract slug this task serves, or NULL
    note_uuid       TEXT,               -- UUID of linked note file, or NULL
    snooze_until    TEXT,               -- ISO date to wake snoozed task, or NULL
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    completed_at    TEXT,               -- ISO timestamp when state became 'done'
    archived_at     TEXT                -- ISO timestamp when moved to archive
);

CREATE TABLE IF NOT EXISTS task_state_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    old_state       TEXT,
    new_state       TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    reason          TEXT,               -- optional: why the state changed
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id)
);

CREATE TABLE IF NOT EXISTS task_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES task_metadata(task_id),
    UNIQUE(task_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_task_state
    ON task_metadata(state);
CREATE INDEX IF NOT EXISTS idx_task_contract
    ON task_metadata(contract);
CREATE INDEX IF NOT EXISTS idx_task_history
    ON task_state_history(task_id, changed_at);
CREATE INDEX IF NOT EXISTS idx_task_sessions_task
    ON task_sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_task_sessions_session
    ON task_sessions(session_id);
"""

VALID_STATES = {"inbox", "mit", "focused", "snoozed", "done"}
VALID_URGENCIES = {"low", "medium", "high"}
VALID_COMPLEXITIES = {"simple", "moderate", "complex", None}


def _db_path() -> Path:
    """Resolve the task metadata database path from config."""
    cfg = load_config()
    custom = cfg.get("tasks", {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/tasks")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection() -> sqlite3.Connection:
    """Open (or create) the task metadata database with WAL mode."""
    path = _db_path()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _SentinelType:
    """Distinguishes 'not provided' from None in update() kwargs."""
    def __repr__(self) -> str:
        return "<NOT_PROVIDED>"

_SENTINEL = _SentinelType()


# ── CRUD ────────────────────────────────────────────────────────


def create(
    task_id: str,
    state: str = "inbox",
    urgency: str = "medium",
    complexity: str | None = None,
    contract: str | None = None,
    note_uuid: str | None = None,
) -> dict[str, Any]:
    """Create a metadata record for a new task.

    Called when create_task() generates a new 🆔.
    """
    if state not in VALID_STATES:
        raise ValueError(f"Invalid state {state!r}")
    if urgency not in VALID_URGENCIES:
        raise ValueError(f"Invalid urgency {urgency!r}")

    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO task_metadata
               (task_id, state, urgency, complexity, contract, note_uuid,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, state, urgency, complexity, contract, note_uuid, now, now),
        )
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, NULL, ?, ?, ?)""",
            (task_id, state, now, "created"),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Task metadata created: %s (state=%s)", task_id, state)
    return {"task_id": task_id, "state": state, "urgency": urgency}


def get(task_id: str) -> dict[str, Any] | None:
    """Get metadata for a task by ID. Returns None if not found."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update(
    task_id: str,
    *,
    state: str | None = None,
    urgency: str | None = None,
    complexity: str | None = _SENTINEL,
    contract: str | None = _SENTINEL,
    snooze_until: str | None = _SENTINEL,
    note_uuid: str | None = _SENTINEL,
    reason: str | None = None,
) -> dict[str, Any]:
    """Update metadata fields for a task. Only provided fields change.

    State changes are recorded in task_state_history with optional reason.
    """
    sets: list[str] = []
    params: list[Any] = []

    if state is not None:
        if state not in VALID_STATES:
            raise ValueError(f"Invalid state {state!r}")
        sets.append("state = ?")
        params.append(state)
        if state == "done":
            sets.append("completed_at = ?")
            params.append(_now_iso())

    if urgency is not None:
        if urgency not in VALID_URGENCIES:
            raise ValueError(f"Invalid urgency {urgency!r}")
        sets.append("urgency = ?")
        params.append(urgency)

    if complexity is not _SENTINEL:
        sets.append("complexity = ?")
        params.append(complexity)

    if contract is not _SENTINEL:
        sets.append("contract = ?")
        params.append(contract)

    if snooze_until is not _SENTINEL:
        sets.append("snooze_until = ?")
        params.append(snooze_until)

    if note_uuid is not _SENTINEL:
        sets.append("note_uuid = ?")
        params.append(note_uuid)

    if not sets:
        return {"task_id": task_id, "changed": False}

    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(task_id)

    conn = get_connection()
    try:
        # Record state change history
        if state is not None:
            old_row = conn.execute(
                "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
            ).fetchone()
            old_state = old_row["state"] if old_row else None

            if old_state != state:
                conn.execute(
                    """INSERT INTO task_state_history
                       (task_id, old_state, new_state, changed_at, reason)
                       VALUES (?, ?, ?, ?, ?)""",
                    (task_id, old_state, state, _now_iso(), reason),
                )

        conn.execute(
            f"UPDATE task_metadata SET {', '.join(sets)} WHERE task_id = ?",
            params,
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Task metadata updated: %s", task_id)
    return {"task_id": task_id, "changed": True}


def delete(task_id: str) -> bool:
    """Delete a task's metadata and state history. Returns True if found.

    Writes a tombstone row to ``task_state_history`` (new_state='deleted')
    before removing the metadata, so the deletion is visible in timelines.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        # Tombstone: record deletion in history before removing data
        now = _now_iso()
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, ?, 'deleted', ?, 'deleted')""",
            (task_id, row["state"], now),
        )
        conn.execute("DELETE FROM task_sessions WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_metadata WHERE task_id = ?", (task_id,))
        conn.commit()
        logger.info("Task metadata deleted (tombstone written): %s", task_id)
        return True
    finally:
        conn.close()


def query(
    state: str | None = None,
    urgency: str | None = None,
    contract: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Query task metadata with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if urgency is not None:
        clauses.append("urgency = ?")
        params.append(urgency)
    if contract is not None:
        clauses.append("contract = ?")
        params.append(contract)
    if not include_archived:
        clauses.append("archived_at IS NULL")

    where = " AND ".join(clauses) if clauses else "1=1"

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT * FROM task_metadata WHERE {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_history(task_id: str) -> list[dict[str, Any]]:
    """Get state change history for a task, newest first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM task_state_history
               WHERE task_id = ? ORDER BY changed_at DESC""",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_in_range(since: str, until: str) -> list[dict[str, Any]]:
    """Get all task state changes within a time range.

    Args:
        since: ISO datetime string (inclusive lower bound).
        until: ISO datetime string (exclusive upper bound).

    Returns:
        List of dicts with task_id, old_state, new_state, changed_at, reason.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT h.task_id, h.old_state, h.new_state, h.changed_at,
                      h.reason
               FROM task_state_history h
               WHERE h.changed_at >= ? AND h.changed_at < ?
               ORDER BY h.changed_at ASC""",
            (since, until),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def counts_by_state() -> dict[str, int]:
    """Get task counts grouped by state (excluding archived)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT state, COUNT(*) as count FROM task_metadata
               WHERE archived_at IS NULL GROUP BY state"""
        ).fetchall()
        return {r["state"]: r["count"] for r in rows}
    finally:
        conn.close()


def mark_archived(task_id: str) -> None:
    """Mark a task as archived (sets archived_at timestamp).

    Also writes a history row so archival is visible in timelines.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT state FROM task_metadata WHERE task_id = ?", (task_id,)
        ).fetchone()
        now = _now_iso()
        conn.execute(
            "UPDATE task_metadata SET archived_at = ?, updated_at = ? WHERE task_id = ?",
            (now, now, task_id),
        )
        if row:
            conn.execute(
                """INSERT INTO task_state_history
                   (task_id, old_state, new_state, changed_at, reason)
                   VALUES (?, ?, 'archived', ?, 'archived')""",
                (task_id, row["state"], now),
            )
        conn.commit()
    finally:
        conn.close()


# ── Session assignment ─────────────────────────────────────────


def assign_session(task_id: str, session_id: str) -> dict[str, Any]:
    """Record a session as working on a task. Idempotent (INSERT OR IGNORE)."""
    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO task_sessions
               (task_id, session_id, assigned_at)
               VALUES (?, ?, ?)""",
            (task_id, session_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info("Session %s assigned to task %s", session_id[:8], task_id)
    return {"task_id": task_id, "session_id": session_id, "assigned_at": now}


def get_sessions(task_id: str) -> list[dict[str, Any]]:
    """Get all sessions assigned to a task, ordered by assignment time."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT task_id, session_id, assigned_at
               FROM task_sessions
               WHERE task_id = ? ORDER BY assigned_at""",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
