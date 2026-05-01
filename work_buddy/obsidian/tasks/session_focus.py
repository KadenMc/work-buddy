"""Slice 5b: per-session ``working_on_now`` pointer.

The ``focused`` task state is persistent and Allen-style sticky;
``working_on_now`` is per-session, transient, single-task: "what is
THIS session actively working on right now?"  Two parallel agent
sessions can each have their own working_on_now without contention.

Storage: a single-row-per-session SQLite table.  No automatic
cleanup; explicit ``clear_working_on_now(session_id)`` on session
end OR overwriting via ``set_working_on_now`` for a different task.

The Today tab's "Re-run" button reads this; the
:func:`build_now_plan` ranker boosts whatever the session is
currently working on to the top so the recommendation surface
honors stated focus before pure heuristics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_working_on_now(session_id: str, task_id: str) -> dict[str, Any]:
    """Mark ``task_id`` as the session's current focus (idempotent).

    Replaces any existing focus for this session.  Returns the new
    pointer with ``started_at`` so callers can render "since HH:mm".
    """
    if not session_id or not task_id:
        raise ValueError("session_id and task_id are required")

    now = _now_iso()
    conn = store.get_connection()
    try:
        # INSERT OR REPLACE keeps the per-session uniqueness invariant
        # without requiring a separate DELETE round-trip.
        conn.execute(
            """INSERT OR REPLACE INTO session_focus
               (session_id, task_id, started_at) VALUES (?, ?, ?)""",
            (session_id, task_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "session_focus set: session=%s task=%s", session_id[:8], task_id,
    )
    return {"session_id": session_id, "task_id": task_id, "started_at": now}


def get_working_on_now(session_id: str) -> dict[str, Any] | None:
    """Return ``{task_id, started_at}`` for the session, or None."""
    if not session_id:
        return None
    conn = store.get_connection()
    try:
        row = conn.execute(
            """SELECT task_id, started_at FROM session_focus
               WHERE session_id = ?""",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def clear_working_on_now(session_id: str) -> bool:
    """Remove the session's focus.  Returns True if a row existed."""
    if not session_id:
        return False
    conn = store.get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM session_focus WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def sessions_focused_on(task_id: str) -> list[dict[str, Any]]:
    """Reverse lookup: which sessions are currently focused on this task?

    Used by the dashboard Tasks-tab to render "X agent sessions
    actively working on this" so the user can spot contention.
    """
    if not task_id:
        return []
    conn = store.get_connection()
    try:
        rows = conn.execute(
            """SELECT session_id, task_id, started_at FROM session_focus
               WHERE task_id = ?
               ORDER BY started_at""",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_active() -> list[dict[str, Any]]:
    """Return every active session_focus row.

    Used by the Today tab to honor the "boost stated focus to the top"
    recommendation rule and by diagnostic dashboards.
    """
    conn = store.get_connection()
    try:
        rows = conn.execute(
            """SELECT session_id, task_id, started_at FROM session_focus
               ORDER BY started_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
