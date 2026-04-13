"""SQLite-backed thread store.

Storage for threads and messages. Lightweight, thread-safe via Python's
sqlite3 module. Database lives at ``agents/threads.db``.

All public functions accept an optional ``conn`` parameter for callers
that want to manage their own connections (e.g., transactions). When
omitted, a fresh connection is created and auto-closed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.threads.models import Thread, ThreadMessage

logger = logging.getLogger(__name__)

from work_buddy.paths import data_dir

_DB_PATH = data_dir("agents") / "threads.db"


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def _get_db_path() -> Path:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    """Open a connection with row_factory set."""
    conn = sqlite3.connect(str(_get_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id   TEXT PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT '',
            metadata    TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id    TEXT PRIMARY KEY,
            thread_id     TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'agent',
            content       TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            message_type  TEXT NOT NULL DEFAULT 'text',
            response_type TEXT NOT NULL DEFAULT 'none',
            choices       TEXT,
            response      TEXT,
            status        TEXT NOT NULL DEFAULT 'sent',
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_thread
            ON messages(thread_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_threads_status
            ON threads(status);
    """)


# Auto-init on first import
try:
    _conn = get_connection()
    _ensure_schema(_conn)
    _conn.close()
except Exception as e:
    logger.warning("Thread store schema init failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Thread CRUD
# ---------------------------------------------------------------------------

def create_thread(
    title: str,
    source: str = "",
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> Thread:
    """Create a new thread. Returns the Thread object."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        now = _now()
        thread = Thread(
            thread_id=_new_id(),
            title=title,
            status="open",
            created_at=now,
            updated_at=now,
            source=source,
            metadata=metadata or {},
        )
        conn.execute(
            """INSERT INTO threads
               (thread_id, title, status, created_at, updated_at, source, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                thread.thread_id,
                thread.title,
                thread.status,
                thread.created_at,
                thread.updated_at,
                thread.source,
                json.dumps(thread.metadata),
            ),
        )
        conn.commit()
        logger.info("Created thread %s: %s", thread.thread_id, title)
        return thread
    finally:
        if own_conn:
            conn.close()


def get_thread(thread_id: str, conn: sqlite3.Connection | None = None) -> Thread | None:
    """Get a thread by ID (without messages)."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return None
        return Thread.from_row(dict(row))
    finally:
        if own_conn:
            conn.close()


def get_thread_with_messages(
    thread_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Get a thread with all messages in chronological order.

    Returns ``{"thread": Thread.to_dict(), "messages": [msg.to_dict(), ...]}``
    or None if thread not found.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        thread = get_thread(thread_id, conn=conn)
        if thread is None:
            return None
        rows = conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ).fetchall()
        messages = [ThreadMessage.from_row(dict(r)) for r in rows]
        return {
            "thread": thread.to_dict(),
            "messages": [m.to_dict() for m in messages],
        }
    finally:
        if own_conn:
            conn.close()


def list_threads(
    status: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """List threads with last message preview.

    Returns list of dicts with thread fields + ``message_count`` and
    ``last_message_preview``.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                """SELECT t.*,
                          (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.thread_id) AS message_count,
                          (SELECT m.content FROM messages m WHERE m.thread_id = t.thread_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_preview,
                          (SELECT m.status FROM messages m WHERE m.thread_id = t.thread_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_status
                   FROM threads t
                   WHERE t.status = ?
                   ORDER BY t.updated_at DESC
                   LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT t.*,
                          (SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.thread_id) AS message_count,
                          (SELECT m.content FROM messages m WHERE m.thread_id = t.thread_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_preview,
                          (SELECT m.status FROM messages m WHERE m.thread_id = t.thread_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_status
                   FROM threads t
                   ORDER BY t.updated_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            d = Thread.from_row(dict(row)).to_dict()
            d["message_count"] = row["message_count"] or 0
            preview = row["last_message_preview"] or ""
            d["last_message_preview"] = preview[:120] + ("..." if len(preview) > 120 else "")
            d["has_pending"] = row["last_message_status"] == "pending"
            results.append(d)
        return results
    finally:
        if own_conn:
            conn.close()


def close_thread(thread_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Close a thread. Marks all pending messages as 'sent' (no longer awaiting).
    Returns False if thread not found."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        thread = get_thread(thread_id, conn=conn)
        if thread is None:
            return False
        now = _now()
        conn.execute(
            "UPDATE threads SET status = 'closed', updated_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        conn.execute(
            "UPDATE messages SET status = 'sent' WHERE thread_id = ? AND status = 'pending'",
            (thread_id,),
        )
        conn.commit()
        logger.info("Closed thread %s", thread_id)
        return True
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Message CRUD
# ---------------------------------------------------------------------------

def add_message(
    thread_id: str,
    role: str,
    content: str,
    message_type: str = "text",
    response_type: str = "none",
    choices: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
) -> ThreadMessage | None:
    """Add a message to a thread. Returns the message, or None if thread not found.

    For questions (message_type="question"), set response_type and choices.
    The message status is set to "pending" for questions, "sent" otherwise.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        thread = get_thread(thread_id, conn=conn)
        if thread is None:
            return None
        if thread.status == "closed":
            logger.warning("Cannot add message to closed thread %s", thread_id)
            return None

        now = _now()
        status = "pending" if message_type == "question" else "sent"
        msg = ThreadMessage(
            message_id=_new_id(),
            thread_id=thread_id,
            role=role,
            content=content,
            created_at=now,
            message_type=message_type,
            response_type=response_type,
            choices=choices,
            status=status,
        )
        conn.execute(
            """INSERT INTO messages
               (message_id, thread_id, role, content, created_at,
                message_type, response_type, choices, response, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id,
                msg.thread_id,
                msg.role,
                msg.content,
                msg.created_at,
                msg.message_type,
                msg.response_type,
                json.dumps(choices) if choices else None,
                None,
                msg.status,
            ),
        )
        # Touch thread updated_at
        conn.execute(
            "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
            (now, thread_id),
        )
        conn.commit()
        return msg
    finally:
        if own_conn:
            conn.close()


def get_pending_question(
    thread_id: str, conn: sqlite3.Connection | None = None
) -> ThreadMessage | None:
    """Get the latest unanswered question in a thread."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM messages
               WHERE thread_id = ? AND status = 'pending'
               ORDER BY created_at DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return ThreadMessage.from_row(dict(row))
    finally:
        if own_conn:
            conn.close()


def respond_to_message(
    message_id: str,
    response: str,
    conn: sqlite3.Connection | None = None,
) -> ThreadMessage | None:
    """Record a user response to a pending question.

    Returns the updated message, or None if not found / not pending.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        msg = ThreadMessage.from_row(dict(row))
        if msg.status != "pending":
            logger.warning("Message %s is not pending (status=%s)", message_id, msg.status)
            return None

        now = _now()
        conn.execute(
            "UPDATE messages SET response = ?, status = 'answered' WHERE message_id = ?",
            (response, message_id),
        )
        # Also add a user message to the thread for display purposes
        user_msg = ThreadMessage(
            message_id=_new_id(),
            thread_id=msg.thread_id,
            role="user",
            content=response,
            created_at=now,
            message_type="text",
            status="sent",
        )
        conn.execute(
            """INSERT INTO messages
               (message_id, thread_id, role, content, created_at,
                message_type, response_type, choices, response, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_msg.message_id,
                user_msg.thread_id,
                user_msg.role,
                user_msg.content,
                user_msg.created_at,
                user_msg.message_type,
                "none",
                None,
                None,
                user_msg.status,
            ),
        )
        # Touch thread updated_at
        conn.execute(
            "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
            (now, msg.thread_id),
        )
        conn.commit()

        msg.response = response
        msg.status = "answered"
        return msg
    finally:
        if own_conn:
            conn.close()


def respond_to_thread(
    thread_id: str,
    response: str,
    conn: sqlite3.Connection | None = None,
) -> ThreadMessage | None:
    """Respond to the latest pending question in a thread.

    Convenience wrapper: finds the pending question and responds to it.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        pending = get_pending_question(thread_id, conn=conn)
        if pending is None:
            return None
        return respond_to_message(pending.message_id, response, conn=conn)
    finally:
        if own_conn:
            conn.close()
