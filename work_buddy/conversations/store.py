"""SQLite-backed conversation store.

Storage for conversations and their messages. Lightweight, thread-safe
via Python's sqlite3 module. Database lives at
``agents/conversations.db``.

All public functions accept an optional ``conn`` parameter for callers
that want to manage their own connections (e.g., transactions). When
omitted, a fresh connection is created and auto-closed.

Renamed from ``work_buddy.threads``; that namespace is reserved for
the universal-entity primitive (:mod:`work_buddy.threads`). On first import,
this module will auto-migrate any existing ``threads.db`` (with tables
``threads``/``messages``) to ``conversations.db`` (tables
``conversations``/``messages`` with column ``conversation_id``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.conversations.models import Conversation, ConversationMessage

logger = logging.getLogger(__name__)

from work_buddy.paths import data_dir

_DB_PATH = data_dir("agents") / "conversations.db"
_LEGACY_DB_PATH = data_dir("agents") / "threads.db"


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
    # Detect legacy schema (post-rename of file but pre-rename of tables, or
    # for any future caller that creates the legacy shape).
    legacy_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone()
    if legacy_table is not None:
        # Rename legacy ``threads`` table to ``conversations`` and rename
        # the ``thread_id`` columns to ``conversation_id`` in both tables.
        # SQLite's ALTER TABLE supports table + column renames since 3.25.
        try:
            conn.executescript(
                """
                ALTER TABLE threads RENAME TO conversations;
                ALTER TABLE conversations RENAME COLUMN thread_id TO conversation_id;
                ALTER TABLE messages RENAME COLUMN thread_id TO conversation_id;
                """
            )
            conn.commit()
            logger.info(
                "Migrated legacy threads/messages schema to "
                "conversations/messages with conversation_id."
            )
        except sqlite3.OperationalError as e:
            # Older SQLite without RENAME COLUMN support — fall back to
            # rebuild. Volume is small; safe to do at startup.
            logger.warning(
                "RENAME COLUMN unsupported (%s); rebuilding tables.", e,
            )
            conn.executescript(
                """
                CREATE TABLE conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title           TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'open',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    source          TEXT NOT NULL DEFAULT '',
                    metadata        TEXT NOT NULL DEFAULT '{}'
                );
                INSERT INTO conversations
                    (conversation_id, title, status, created_at, updated_at, source, metadata)
                  SELECT thread_id, title, status, created_at, updated_at, source, metadata
                    FROM threads;
                DROP TABLE threads;

                CREATE TABLE messages_new (
                    message_id      TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'agent',
                    content         TEXT NOT NULL DEFAULT '',
                    created_at      TEXT NOT NULL,
                    message_type    TEXT NOT NULL DEFAULT 'text',
                    response_type   TEXT NOT NULL DEFAULT 'none',
                    choices         TEXT,
                    response        TEXT,
                    status          TEXT NOT NULL DEFAULT 'sent',
                    FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
                );
                INSERT INTO messages_new
                    (message_id, conversation_id, role, content, created_at,
                     message_type, response_type, choices, response, status)
                  SELECT message_id, thread_id, role, content, created_at,
                         message_type, response_type, choices, response, status
                    FROM messages;
                DROP TABLE messages;
                ALTER TABLE messages_new RENAME TO messages;
                """
            )
            conn.commit()

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            title           TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'open',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            source          TEXT NOT NULL DEFAULT '',
            metadata        TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id      TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'agent',
            content         TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            message_type    TEXT NOT NULL DEFAULT 'text',
            response_type   TEXT NOT NULL DEFAULT 'none',
            choices         TEXT,
            response        TEXT,
            status          TEXT NOT NULL DEFAULT 'sent',
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_conversations_status
            ON conversations(status);
        """
    )


def _maybe_migrate_legacy_db() -> None:
    """If a legacy ``threads.db`` exists and ``conversations.db`` doesn't,
    rename the file in place. The schema migration in ``_ensure_schema``
    handles the table/column renames on first open.
    """
    if _LEGACY_DB_PATH.exists() and not _DB_PATH.exists():
        try:
            _LEGACY_DB_PATH.rename(_DB_PATH)
            # WAL/SHM sidecars (best-effort).
            for suffix in ("-wal", "-shm"):
                legacy = _LEGACY_DB_PATH.with_name(
                    _LEGACY_DB_PATH.name + suffix
                )
                if legacy.exists():
                    legacy.rename(_DB_PATH.with_name(_DB_PATH.name + suffix))
            logger.info("Renamed legacy threads.db → conversations.db")
        except OSError as e:
            logger.warning("Could not rename legacy DB: %s", e)


# Auto-init on first import
try:
    _maybe_migrate_legacy_db()
    _conn = get_connection()
    _ensure_schema(_conn)
    _conn.close()
except Exception as e:
    logger.warning("Conversation store schema init failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------

def create_conversation(
    title: str,
    source: str = "",
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> Conversation:
    """Create a new conversation. Returns the Conversation object."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        now = _now()
        conv = Conversation(
            conversation_id=_new_id(),
            title=title,
            status="open",
            created_at=now,
            updated_at=now,
            source=source,
            metadata=metadata or {},
        )
        conn.execute(
            """INSERT INTO conversations
               (conversation_id, title, status, created_at, updated_at, source, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                conv.conversation_id,
                conv.title,
                conv.status,
                conv.created_at,
                conv.updated_at,
                conv.source,
                json.dumps(conv.metadata),
            ),
        )
        conn.commit()
        logger.info("Created conversation %s: %s", conv.conversation_id, title)
        return conv
    finally:
        if own_conn:
            conn.close()


def get_conversation(
    conversation_id: str, conn: sqlite3.Connection | None = None
) -> Conversation | None:
    """Get a conversation by ID (without messages)."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return Conversation.from_row(dict(row))
    finally:
        if own_conn:
            conn.close()


def get_conversation_with_messages(
    conversation_id: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """Get a conversation with all messages in chronological order.

    Returns ``{"conversation": Conversation.to_dict(), "messages": [msg.to_dict(), ...]}``
    or None if conversation not found.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conv = get_conversation(conversation_id, conn=conn)
        if conv is None:
            return None
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
        messages = [ConversationMessage.from_row(dict(r)) for r in rows]
        return {
            "conversation": conv.to_dict(),
            "messages": [m.to_dict() for m in messages],
        }
    finally:
        if own_conn:
            conn.close()


def list_conversations(
    status: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """List conversations with last-message preview.

    Returns list of dicts with conversation fields + ``message_count`` and
    ``last_message_preview``.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                """SELECT c.*,
                          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count,
                          (SELECT m.content FROM messages m WHERE m.conversation_id = c.conversation_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_preview,
                          (SELECT m.status FROM messages m WHERE m.conversation_id = c.conversation_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_status
                   FROM conversations c
                   WHERE c.status = ?
                   ORDER BY c.updated_at DESC
                   LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT c.*,
                          (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) AS message_count,
                          (SELECT m.content FROM messages m WHERE m.conversation_id = c.conversation_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_preview,
                          (SELECT m.status FROM messages m WHERE m.conversation_id = c.conversation_id
                           ORDER BY m.created_at DESC LIMIT 1) AS last_message_status
                   FROM conversations c
                   ORDER BY c.updated_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            d = Conversation.from_row(dict(row)).to_dict()
            d["message_count"] = row["message_count"] or 0
            preview = row["last_message_preview"] or ""
            d["last_message_preview"] = preview[:120] + ("..." if len(preview) > 120 else "")
            d["has_pending"] = row["last_message_status"] == "pending"
            results.append(d)
        return results
    finally:
        if own_conn:
            conn.close()


def close_conversation(
    conversation_id: str, conn: sqlite3.Connection | None = None
) -> bool:
    """Close a conversation. Marks all pending messages as 'sent'.
    Returns False if conversation not found."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conv = get_conversation(conversation_id, conn=conn)
        if conv is None:
            return False
        now = _now()
        conn.execute(
            "UPDATE conversations SET status = 'closed', updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        conn.execute(
            "UPDATE messages SET status = 'sent' WHERE conversation_id = ? AND status = 'pending'",
            (conversation_id,),
        )
        conn.commit()
        logger.info("Closed conversation %s", conversation_id)
        return True
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Message CRUD
# ---------------------------------------------------------------------------

def add_message(
    conversation_id: str,
    role: str,
    content: str,
    message_type: str = "text",
    response_type: str = "none",
    choices: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
) -> ConversationMessage | None:
    """Add a message to a conversation. Returns the message, or None if
    conversation not found.

    For questions (message_type="question"), set response_type and choices.
    The message status is "pending" for questions, "sent" otherwise.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        conv = get_conversation(conversation_id, conn=conn)
        if conv is None:
            return None
        if conv.status == "closed":
            logger.warning(
                "Cannot add message to closed conversation %s", conversation_id,
            )
            return None

        now = _now()
        status = "pending" if message_type == "question" else "sent"
        msg = ConversationMessage(
            message_id=_new_id(),
            conversation_id=conversation_id,
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
               (message_id, conversation_id, role, content, created_at,
                message_type, response_type, choices, response, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id,
                msg.conversation_id,
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
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        conn.commit()
        return msg
    finally:
        if own_conn:
            conn.close()


def get_pending_question(
    conversation_id: str, conn: sqlite3.Connection | None = None
) -> ConversationMessage | None:
    """Get the latest unanswered question in a conversation."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            """SELECT * FROM messages
               WHERE conversation_id = ? AND status = 'pending'
               ORDER BY created_at DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return ConversationMessage.from_row(dict(row))
    finally:
        if own_conn:
            conn.close()


def respond_to_message(
    message_id: str,
    response: str,
    conn: sqlite3.Connection | None = None,
) -> ConversationMessage | None:
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
        msg = ConversationMessage.from_row(dict(row))
        if msg.status != "pending":
            logger.warning(
                "Message %s is not pending (status=%s)", message_id, msg.status,
            )
            return None

        now = _now()
        conn.execute(
            "UPDATE messages SET response = ?, status = 'answered' WHERE message_id = ?",
            (response, message_id),
        )
        # Add a user message for display purposes
        user_msg = ConversationMessage(
            message_id=_new_id(),
            conversation_id=msg.conversation_id,
            role="user",
            content=response,
            created_at=now,
            message_type="text",
            status="sent",
        )
        conn.execute(
            """INSERT INTO messages
               (message_id, conversation_id, role, content, created_at,
                message_type, response_type, choices, response, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_msg.message_id,
                user_msg.conversation_id,
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
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (now, msg.conversation_id),
        )
        conn.commit()

        msg.response = response
        msg.status = "answered"
        return msg
    finally:
        if own_conn:
            conn.close()


def respond_to_conversation(
    conversation_id: str,
    response: str,
    conn: sqlite3.Connection | None = None,
) -> ConversationMessage | None:
    """Respond to the latest pending question in a conversation.

    Convenience wrapper: finds the pending question and responds to it.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        pending = get_pending_question(conversation_id, conn=conn)
        if pending is None:
            return None
        return respond_to_message(pending.message_id, response, conn=conn)
    finally:
        if own_conn:
            conn.close()
