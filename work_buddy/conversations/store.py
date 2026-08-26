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
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from work_buddy.conversations.models import Conversation, ConversationMessage

logger = logging.getLogger(__name__)

from work_buddy.paths import data_dir

_DB_PATH = data_dir("agents") / "conversations.db"
_LEGACY_DB_PATH = data_dir("agents") / "threads.db"
_AGENT_STARTING_GRACE_SECONDS = 20.0


class ConversationLeaseLost(RuntimeError):
    """The supplied consumer generation no longer owns an open conversation."""


class UserMessageIdConflictError(sqlite3.IntegrityError):
    """A caller-stable user-message id was reused for a different payload."""


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
            producer        TEXT,
            context_json    TEXT,
            ingress_json    TEXT,
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_conversations_status
            ON conversations(status);

        CREATE TABLE IF NOT EXISTS conversation_consumer_cursors (
            conversation_id TEXT NOT NULL,
            consumer        TEXT NOT NULL,
            last_created_at  TEXT NOT NULL DEFAULT '',
            last_message_id  TEXT NOT NULL DEFAULT '',
            updated_at       TEXT NOT NULL,
            PRIMARY KEY (conversation_id, consumer),
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE TABLE IF NOT EXISTS conversation_agent_leases (
            conversation_id TEXT NOT NULL,
            consumer        TEXT NOT NULL,
            generation      TEXT NOT NULL,
            status          TEXT NOT NULL,
            pid             INTEGER,
            started_at      TEXT NOT NULL,
            heartbeat_at    TEXT,
            updated_at      TEXT NOT NULL,
            error           TEXT,
            execution_json  TEXT,
            PRIMARY KEY (conversation_id, consumer),
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        );

        CREATE TABLE IF NOT EXISTS conversation_action_snapshot_receipts (
            receipt_id         TEXT PRIMARY KEY,
            conversation_id    TEXT NOT NULL,
            consumer           TEXT NOT NULL,
            generation         TEXT NOT NULL,
            user_message_id    TEXT NOT NULL,
            action_snapshot_id TEXT NOT NULL,
            reply_message_id   TEXT,
            fetched_at         TEXT NOT NULL,
            fetch_outcome      TEXT NOT NULL DEFAULT 'available',
            unavailable_code   TEXT,
            fetch_resolved_at  TEXT,
            replied_at         TEXT,
            acked_at           TEXT,
            UNIQUE (
                conversation_id,
                consumer,
                generation,
                user_message_id,
                action_snapshot_id
            ),
            FOREIGN KEY (conversation_id)
                REFERENCES conversations(conversation_id),
            FOREIGN KEY (user_message_id) REFERENCES messages(message_id),
            FOREIGN KEY (reply_message_id) REFERENCES messages(message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_action_snapshot_receipts_delivery
            ON conversation_action_snapshot_receipts(
                conversation_id, consumer, generation, user_message_id
            );
        """
    )
    _ensure_column(conn, "messages", "producer", "producer TEXT")
    _ensure_column(conn, "messages", "context_json", "context_json TEXT")
    _ensure_column(conn, "messages", "ingress_json", "ingress_json TEXT")
    # Receipts written before fetch outcomes were recorded were minted only
    # after a successful view read, so ``available`` is the truthful migration
    # default. New receipts are inserted explicitly as ``pending`` below.
    _ensure_column(
        conn,
        "conversation_action_snapshot_receipts",
        "fetch_outcome",
        "fetch_outcome TEXT NOT NULL DEFAULT 'available'",
    )
    _ensure_column(
        conn,
        "conversation_action_snapshot_receipts",
        "unavailable_code",
        "unavailable_code TEXT",
    )
    _ensure_column(
        conn,
        "conversation_action_snapshot_receipts",
        "fetch_resolved_at",
        "fetch_resolved_at TEXT",
    )
    _ensure_column(
        conn,
        "conversation_agent_leases",
        "execution_json",
        "execution_json TEXT",
    )
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """Add one optional column to an existing SQLite table, idempotently."""
    columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


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
        if own_conn:
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


def get_message(
    conversation_id: str,
    message_id: str,
    conn: sqlite3.Connection | None = None,
) -> ConversationMessage | None:
    """Read one exact message without crossing its conversation binding."""
    own_conn = conn is None
    active = get_connection() if own_conn else conn
    try:
        row = active.execute(
            "SELECT * FROM messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        return None if row is None else ConversationMessage.from_row(dict(row))
    finally:
        if own_conn:
            active.close()


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
    """Close a conversation and revoke every persisted consumer lease.

    Pending questions become ordinary transcript entries. A detached consumer
    that is currently long-polling loses its generation on the next receive or
    acknowledge call and must exit.
    """
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
        conn.execute(
            """UPDATE conversation_agent_leases
               SET status = 'stopped', updated_at = ?
               WHERE conversation_id = ? AND status != 'stopped'""",
            (now, conversation_id),
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
    message_id: str | None = None,
    producer: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
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
            message_id=message_id or _new_id(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=now,
            message_type=message_type,
            response_type=response_type,
            choices=choices,
            status=status,
            producer=dict(producer) if producer is not None else None,
            context=dict(context) if context is not None else None,
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, conversation_id, role, content, created_at,
                message_type, response_type, choices, response, status, producer,
                context_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                json.dumps(msg.producer) if msg.producer is not None else None,
                json.dumps(msg.context) if msg.context is not None else None,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (msg.message_id,)
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("message insert was ignored unexpectedly")
            existing = ConversationMessage.from_row(dict(row))
            if (
                existing.conversation_id != msg.conversation_id
                or existing.role != msg.role
                or existing.content != msg.content
                or existing.message_type != msg.message_type
                or existing.response_type != msg.response_type
                or existing.choices != msg.choices
                or existing.context != msg.context
            ):
                raise sqlite3.IntegrityError(
                    "message_id was reused for different message content"
                )
            return existing
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        if own_conn:
            conn.commit()
        return msg
    finally:
        if own_conn:
            conn.close()


def send_agent_message_idempotent(
    conversation_id: str,
    content: str,
    message_id: str,
    conn: sqlite3.Connection | None = None,
    producer: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> tuple[ConversationMessage | None, bool]:
    """Send one caller-keyed agent message with first-writer-wins semantics.

    A replay in the same conversation and agent role returns the original row
    regardless of the retry's regenerated wording. The original content is
    never overwritten. Reusing the key across a conversation or role boundary
    remains an integrity error.
    """
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message_id must be a nonempty string")
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        conversation = get_conversation(conversation_id, conn=conn)
        if conversation is None or conversation.status == "closed":
            if own_conn:
                conn.rollback()
            return None, False

        now = _now()
        candidate = ConversationMessage(
            message_id=message_id,
            conversation_id=conversation_id,
            role="agent",
            content=content,
            created_at=now,
            message_type="text",
            response_type="none",
            status="sent",
            producer=dict(producer) if producer is not None else None,
            context=dict(context) if context is not None else None,
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, conversation_id, role, content, created_at,
                message_type, response_type, choices, response, status, producer,
                context_json)
               VALUES (
                   ?, ?, 'agent', ?, ?, 'text', 'none', NULL, NULL, 'sent', ?, ?
               )""",
            (
                message_id,
                conversation_id,
                content,
                now,
                json.dumps(candidate.producer)
                if candidate.producer is not None
                else None,
                json.dumps(candidate.context)
                if candidate.context is not None
                else None,
            ),
        )
        if cursor.rowcount == 1:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
            if own_conn:
                conn.commit()
            return candidate, True

        row = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(
                "agent message insert was ignored unexpectedly"
            )
        existing = ConversationMessage.from_row(dict(row))
        if (
            existing.conversation_id != conversation_id
            or existing.role != "agent"
            or existing.context != candidate.context
        ):
            raise sqlite3.IntegrityError(
                "message_id was reused across a conversation or role boundary"
            )
        if own_conn:
            conn.commit()
        return existing, False
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _insert_user_message_locked(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    content: str,
    message_id: str | None = None,
    context: Mapping[str, Any] | None = None,
    ingress: Mapping[str, Any] | None = None,
) -> tuple[ConversationMessage, bool]:
    """Insert one display-visible user message on an active write transaction."""
    now = _now()
    user_msg = ConversationMessage(
        message_id=message_id or _new_id(),
        conversation_id=conversation_id,
        role="user",
        content=content,
        created_at=now,
        message_type="text",
        status="sent",
        context=dict(context) if context is not None else None,
        ingress=dict(ingress) if ingress is not None else None,
    )
    cursor = conn.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, conversation_id, role, content, created_at,
           message_type, response_type, choices, response, status, context_json,
           ingress_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            json.dumps(user_msg.context) if user_msg.context is not None else None,
            json.dumps(user_msg.ingress) if user_msg.ingress is not None else None,
        ),
    )
    if cursor.rowcount != 0:
        return user_msg, True

    row = conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (user_msg.message_id,)
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("user message insert was ignored unexpectedly")
    existing = ConversationMessage.from_row(dict(row))
    return _validate_user_message_replay(
        existing,
        conversation_id=user_msg.conversation_id,
        content=user_msg.content,
        context=user_msg.context,
        ingress=user_msg.ingress,
    ), False


def _validate_user_message_replay(
    existing: ConversationMessage,
    *,
    conversation_id: str,
    content: str,
    context: Mapping[str, Any] | None,
    ingress: Mapping[str, Any] | None,
) -> ConversationMessage:
    """Return an identical caller-keyed replay or raise a typed conflict."""

    expected_context = dict(context) if context is not None else None
    expected_ingress = dict(ingress) if ingress is not None else None

    def replay_identity(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        # A retry is a fresh one-use gesture and may follow a dashboard
        # session refresh. Those fields attest this transport attempt, not the
        # identity of the already-authored turn. Every authority, actor,
        # action, subject, and exact request-context field must still match.
        return {
            key: item
            for key, item in value.items()
            if key not in {"gesture_id", "session_id_sha256"}
        }
    if (
        existing.conversation_id != conversation_id
        or existing.role != "user"
        or existing.content != content
        or existing.message_type != "text"
        or existing.context != expected_context
        or replay_identity(existing.ingress) != replay_identity(expected_ingress)
    ):
        raise UserMessageIdConflictError(
            "message_id was reused for different user message content"
        )
    return existing


def respond_to_message_with_user_message(
    conversation_id: str,
    message_id: str,
    response: str,
    conn: sqlite3.Connection | None = None,
    *,
    user_message_id: str | None = None,
    context: Mapping[str, Any] | None = None,
    ingress: Mapping[str, Any] | None = None,
) -> ConversationMessage | None:
    """Answer one exact pending question and return its single user message.

    Both identifiers are part of the predicate, so a message id from another
    conversation cannot be answered accidentally. ``status = 'pending'`` is
    repeated on the update, making the operation single-winner even if two
    callers race to answer the same question.
    """
    response_context = dict(context or {})
    if response_context.get("in_reply_to", message_id) != message_id:
        raise ValueError("Answer context does not match the exact question")
    response_context["in_reply_to"] = message_id
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        conversation = get_conversation(conversation_id, conn=conn)
        if conversation is None or conversation.status == "closed":
            if own_conn:
                conn.rollback()
            return None

        # A transport retry can arrive after the original request answered the
        # question but before its HTTP acknowledgement reached the caller.
        # Recognize that durable authored turn before consulting pending state;
        # this also prevents a replay from consuming a newer question.
        if user_message_id is not None:
            replay_row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (user_message_id,),
            ).fetchone()
            if replay_row is not None:
                replay = _validate_user_message_replay(
                    ConversationMessage.from_row(dict(replay_row)),
                    conversation_id=conversation_id,
                    content=response,
                    context=response_context,
                    ingress=ingress,
                )
                if own_conn:
                    conn.commit()
                return replay
        question = conn.execute(
            """SELECT * FROM messages
               WHERE message_id = ? AND conversation_id = ? AND status = 'pending'""",
            (message_id, conversation_id),
        ).fetchone()
        if question is None:
            if own_conn:
                conn.rollback()
            return None

        user_msg, inserted = _insert_user_message_locked(
            conn,
            conversation_id=conversation_id,
            content=response,
            message_id=user_message_id,
            context=response_context,
            ingress=ingress,
        )
        if not inserted:
            # A replay of the same user-message id is already durable. It must
            # not consume a newer pending question as a side effect.
            if own_conn:
                conn.commit()
            return user_msg

        updated = conn.execute(
            """UPDATE messages
               SET response = ?, status = 'answered'
               WHERE message_id = ? AND conversation_id = ? AND status = 'pending'""",
            (response, message_id, conversation_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError(
                "pending conversation question was answered concurrently"
            )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (user_msg.created_at, conversation_id),
        )
        if own_conn:
            conn.commit()
        return user_msg
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def post_user_message(
    conversation_id: str,
    content: str,
    conn: sqlite3.Connection | None = None,
    *,
    message_id: str | None = None,
    context: Mapping[str, Any] | None = None,
    ingress: Mapping[str, Any] | None = None,
) -> ConversationMessage | None:
    """Append one ordinary user turn without consuming a pending question."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        conversation = get_conversation(conversation_id, conn=conn)
        if conversation is None or conversation.status == "closed":
            if own_conn:
                conn.rollback()
            return None

        user_msg, inserted = _insert_user_message_locked(
            conn,
            conversation_id=conversation_id,
            content=content,
            message_id=message_id,
            context=context,
            ingress=ingress,
        )
        if inserted:
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (user_msg.created_at, conversation_id),
            )
        if own_conn:
            conn.commit()
        return user_msg
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def redact_message_content(
    conversation_id: str,
    message_id: str,
    *,
    expected_content_sha256: str,
    replacement: str = "[Redacted because its source was removed.]",
) -> bool:
    """Scrub one exact persisted message without changing its stable identity.

    This narrow maintenance operation is content-addressed: a message edited or
    replaced since dependency capture is never overwritten.  Conversation and
    message IDs, timestamps, producer metadata, and context remain available
    for audit/recovery while the source-derived bytes are removed.
    """

    import hashlib

    if not all(
        isinstance(value, str) and value
        for value in (
            conversation_id,
            message_id,
            expected_content_sha256,
            replacement,
        )
    ):
        raise ValueError("conversation/message redaction fields are required")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND message_id = ?",
            (conversation_id, message_id),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        content = str(row["content"])
        current_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        replacement_sha256 = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        if current_sha256 == replacement_sha256:
            conn.commit()
            return True
        if current_sha256 != expected_content_sha256:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE messages SET content = ? WHERE conversation_id = ? AND message_id = ?",
            (replacement, conversation_id, message_id),
        )
        conn.commit()
        return True


def _timestamp_age_seconds(value: object, *, now: datetime) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def get_agent_lease(
    conversation_id: str,
    consumer: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return one persisted conversation-agent lease without mutating it."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        row = conn.execute(
            """SELECT conversation_id, consumer, generation, status, pid,
                      started_at, heartbeat_at, updated_at, error,
                      execution_json
               FROM conversation_agent_leases
               WHERE conversation_id = ? AND consumer = ?""",
            (conversation_id, consumer),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        raw_execution = result.pop("execution_json", None)
        if isinstance(raw_execution, str) and raw_execution:
            try:
                execution = json.loads(raw_execution)
            except (TypeError, ValueError):
                execution = None
        else:
            execution = None
        result["execution"] = execution if isinstance(execution, dict) else None
        return result
    finally:
        if own_conn:
            conn.close()


@contextmanager
def conversation_agent_write_guard(
    conversation_id: str,
    consumer: str,
    generation: str,
    *,
    starting_grace_seconds: float = _AGENT_STARTING_GRACE_SECONDS,
):
    """Hold the exact running lease while a dependent mutation commits.

    The immediate transaction is intentionally kept open across the caller's
    write. Lease rotation and conversation close use the same conversations DB,
    so either they win first and this guard reports ``lease_lost``, or this
    guarded mutation finishes before they can rotate the generation.
    """
    if not all(
        isinstance(value, str) and value.strip()
        for value in (conversation_id, consumer, generation)
    ):
        raise ValueError(
            "conversation_id, consumer, and generation must be nonempty strings"
        )
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT lease.status, lease.started_at
               FROM conversation_agent_leases AS lease
               JOIN conversations AS conversation
                 ON conversation.conversation_id = lease.conversation_id
               WHERE lease.conversation_id = ?
                 AND lease.consumer = ?
                 AND lease.generation = ?
                 AND lease.status IN ('starting', 'running')
                 AND conversation.status = 'open'""",
            (conversation_id, consumer, generation),
        ).fetchone()
        current_now = datetime.now(timezone.utc)
        starting_age = (
            None
            if row is None or row["status"] != "starting"
            else _timestamp_age_seconds(row["started_at"], now=current_now)
        )
        if row is None or (
            row["status"] == "starting"
            and (
                starting_age is None
                or starting_age > starting_grace_seconds
            )
        ):
            conn.rollback()
            raise ConversationLeaseLost("lease_lost")
        now = _now()
        conn.execute(
            """UPDATE conversation_agent_leases
               SET heartbeat_at = ?, updated_at = ?
               WHERE conversation_id = ? AND consumer = ?
                 AND generation = ? AND status IN ('starting', 'running')""",
            (now, now, conversation_id, consumer, generation),
        )
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def claim_agent_lease(
    conversation_id: str,
    consumer: str,
    generation: str,
    *,
    starting_grace_seconds: float = 20.0,
    heartbeat_ttl_seconds: float = 150.0,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    execution: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Atomically claim a new generation unless a recent one still owns it.

    ``claimed`` in the returned mapping tells the caller whether it owns the
    one spawn attempt. A fresh ``starting`` generation and a ``running`` lease
    with a recent heartbeat are reused across concurrent dashboard requests.
    """
    if not conversation_id or not consumer or not generation:
        raise ValueError("conversation_id, consumer, and generation are required")
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    current_now = now or datetime.now(timezone.utc)
    now_text = current_now.isoformat()
    try:
        conversation = get_conversation(conversation_id, conn=conn)
        if conversation is None or conversation.status == "closed":
            if own_conn:
                conn.rollback()
            return None
        existing = get_agent_lease(conversation_id, consumer, conn=conn)
        reusable = False
        if existing is not None and existing["status"] == "starting":
            age = _timestamp_age_seconds(existing["started_at"], now=current_now)
            reusable = age is not None and age <= starting_grace_seconds
        elif existing is not None and existing["status"] == "running":
            reference = existing["heartbeat_at"] or existing["started_at"]
            ttl = (
                heartbeat_ttl_seconds
                if existing["heartbeat_at"]
                else starting_grace_seconds
            )
            age = _timestamp_age_seconds(reference, now=current_now)
            reusable = age is not None and age <= ttl
        requested_execution = (
            dict(execution) if execution is not None else None
        )
        if (
            reusable
            and requested_execution is not None
            and existing is not None
            and existing.get("execution") != requested_execution
        ):
            reusable = False
        if reusable:
            existing["claimed"] = False
            if own_conn:
                conn.commit()
            return existing

        conn.execute(
            """INSERT INTO conversation_agent_leases
                   (conversation_id, consumer, generation, status, pid,
                    started_at, heartbeat_at, updated_at, error, execution_json)
               VALUES (?, ?, ?, 'starting', NULL, ?, NULL, ?, NULL, ?)
               ON CONFLICT(conversation_id, consumer) DO UPDATE SET
                   generation = excluded.generation,
                   status = 'starting',
                   pid = NULL,
                   started_at = excluded.started_at,
                   heartbeat_at = NULL,
                   updated_at = excluded.updated_at,
                   error = NULL,
                   execution_json = excluded.execution_json""",
            (
                conversation_id,
                consumer,
                generation,
                now_text,
                now_text,
                json.dumps(requested_execution)
                if requested_execution is not None
                else None,
            ),
        )
        if own_conn:
            conn.commit()
        return {
            "conversation_id": conversation_id,
            "consumer": consumer,
            "generation": generation,
            "status": "starting",
            "pid": None,
            "started_at": now_text,
            "heartbeat_at": None,
            "updated_at": now_text,
            "error": None,
            "execution": requested_execution,
            "claimed": True,
        }
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def activate_agent_lease(
    conversation_id: str,
    consumer: str,
    generation: str,
    pid: int,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Attach the spawned PID only if this generation still owns the lease."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        cursor = conn.execute(
            """UPDATE conversation_agent_leases
               SET status = 'running', pid = ?, heartbeat_at = ?,
                   updated_at = ?, error = NULL
               WHERE conversation_id = ? AND consumer = ?
                 AND generation = ? AND status = 'starting'""",
            (pid, now, now, conversation_id, consumer, generation),
        )
        if own_conn:
            conn.commit()
        return cursor.rowcount == 1
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def fail_agent_lease(
    conversation_id: str,
    consumer: str,
    generation: str,
    *,
    error: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Record one sanitized spawn failure for the owning generation."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        cursor = conn.execute(
            """UPDATE conversation_agent_leases
               SET status = 'spawn_failed', pid = NULL, updated_at = ?, error = ?
               WHERE conversation_id = ? AND consumer = ? AND generation = ?""",
            (now, error, conversation_id, consumer, generation),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def stop_agent_lease(
    conversation_id: str,
    consumer: str,
    generation: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Mark a dead/stale generation stopped without touching a successor."""
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        cursor = conn.execute(
            """UPDATE conversation_agent_leases
               SET status = 'stopped', updated_at = ?
               WHERE conversation_id = ? AND consumer = ? AND generation = ?""",
            (now, conversation_id, consumer, generation),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _touch_agent_lease(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    consumer: str,
    generation: str,
) -> bool:
    lease = conn.execute(
        """SELECT lease.status, lease.started_at
           FROM conversation_agent_leases AS lease
           JOIN conversations AS conversation
             ON conversation.conversation_id = lease.conversation_id
           WHERE lease.conversation_id = ? AND lease.consumer = ?
             AND lease.generation = ?
             AND lease.status IN ('starting', 'running')
             AND conversation.status = 'open'""",
        (conversation_id, consumer, generation),
    ).fetchone()
    if lease is None:
        return False
    if lease["status"] == "starting":
        age = _timestamp_age_seconds(
            lease["started_at"],
            now=datetime.now(timezone.utc),
        )
        if age is None or age > _AGENT_STARTING_GRACE_SECONDS:
            return False
    now = _now()
    cursor = conn.execute(
        """UPDATE conversation_agent_leases
           SET heartbeat_at = ?, updated_at = ?
           WHERE conversation_id = ? AND consumer = ? AND generation = ?
             AND status IN ('starting', 'running')""",
        (now, now, conversation_id, consumer, generation),
    )
    return cursor.rowcount == 1


def _next_user_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    consumer: str,
) -> sqlite3.Row | None:
    cursor = conn.execute(
        """SELECT last_created_at, last_message_id
           FROM conversation_consumer_cursors
           WHERE conversation_id = ? AND consumer = ?""",
        (conversation_id, consumer),
    ).fetchone()
    last_created_at = "" if cursor is None else str(cursor["last_created_at"])
    last_message_id = "" if cursor is None else str(cursor["last_message_id"])
    return conn.execute(
        """SELECT * FROM messages
           WHERE conversation_id = ? AND role = 'user'
             AND (
                 created_at > ?
                 OR (created_at = ? AND message_id > ?)
             )
           ORDER BY created_at ASC, message_id ASC
           LIMIT 1""",
        (
            conversation_id,
            last_created_at,
            last_created_at,
            last_message_id,
        ),
    ).fetchone()


def _action_snapshot_context(raw_context: object) -> dict[str, Any] | None:
    context: object = raw_context
    if isinstance(context, str) and context:
        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            return None
    if not isinstance(context, Mapping) or context.get("kind") != "action_snapshot":
        return None
    action_snapshot_id = context.get("action_snapshot_id")
    if not isinstance(action_snapshot_id, str) or not action_snapshot_id:
        return None
    return dict(context)


def record_action_snapshot_consumption(
    conversation_id: str,
    consumer: str,
    generation: str,
    message_id: str,
    action_snapshot_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Mint a durable receipt for fetching one delivered targeted turn.

    The receipt is bound to the active consumer generation and exact user
    message, not merely to the immutable action snapshot. Repeating the same
    fetch returns the same receipt.
    """

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not _touch_agent_lease(
            conn,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
        ):
            raise ConversationLeaseLost("lease_lost")
        existing = conn.execute(
            """SELECT * FROM conversation_action_snapshot_receipts
               WHERE conversation_id = ? AND consumer = ? AND generation = ?
                 AND user_message_id = ? AND action_snapshot_id = ?""",
            (
                conversation_id,
                consumer,
                generation,
                message_id,
                action_snapshot_id,
            ),
        ).fetchone()
        if existing is not None:
            if own_conn:
                conn.commit()
            return dict(existing)

        delivered = _next_user_message(
            conn,
            conversation_id=conversation_id,
            consumer=consumer,
        )
        if delivered is None or str(delivered["message_id"]) != message_id:
            raise ValueError(
                "action snapshot fetch must name the currently delivered user message"
            )
        context = _action_snapshot_context(delivered["context_json"])
        if (
            context is None
            or context.get("action_snapshot_id") != action_snapshot_id
        ):
            raise ValueError(
                "action snapshot fetch does not match the delivered user message"
            )
        receipt_id = _new_id()
        fetched_at = _now()
        conn.execute(
            """INSERT INTO conversation_action_snapshot_receipts
                   (receipt_id, conversation_id, consumer, generation,
                    user_message_id, action_snapshot_id, fetched_at,
                    fetch_outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                receipt_id,
                conversation_id,
                consumer,
                generation,
                message_id,
                action_snapshot_id,
                fetched_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM conversation_action_snapshot_receipts "
            "WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        assert row is not None
        if own_conn:
            conn.commit()
        return dict(row)
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def resolve_action_snapshot_consumption(
    consumption_receipt_id: str,
    fetch_outcome: str,
    *,
    unavailable_code: str | None = None,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Finalize the first exact frozen-context fetch outcome for a receipt.

    ``pending`` is never returned to an agent. The receipt and its outcome are
    committed by the same generation-fenced transaction as the capability
    response. Replays retain the first terminal outcome so the audit record
    cannot be rewritten after an unavailable explanation or successful use.
    """

    if fetch_outcome not in {"available", "unavailable"}:
        raise ValueError("fetch_outcome must be available or unavailable")
    normalized_code = (
        unavailable_code.strip()
        if isinstance(unavailable_code, str) and unavailable_code.strip()
        else None
    )
    if fetch_outcome == "unavailable" and normalized_code is None:
        raise ValueError("an unavailable fetch requires unavailable_code")
    if fetch_outcome == "available" and normalized_code is not None:
        raise ValueError("an available fetch cannot carry unavailable_code")

    resolved_at = _now()
    conn.execute(
        """UPDATE conversation_action_snapshot_receipts
           SET fetch_outcome = ?,
               unavailable_code = ?,
               fetch_resolved_at = ?
           WHERE receipt_id = ? AND fetch_outcome = 'pending'""",
        (
            fetch_outcome,
            normalized_code,
            resolved_at,
            consumption_receipt_id,
        ),
    )
    row = conn.execute(
        """SELECT * FROM conversation_action_snapshot_receipts
           WHERE receipt_id = ?""",
        (consumption_receipt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("consumption receipt is unavailable")
    if str(row["fetch_outcome"]) == "pending":
        raise ValueError("consumption receipt fetch outcome was not resolved")
    return dict(row)


def _receipt_context(raw_context: object) -> dict[str, Any] | None:
    """Return a well-formed targeted-reply consumption context."""

    context = _action_snapshot_context(raw_context)
    if context is None:
        return None
    consumption = context.get("consumption")
    if not isinstance(consumption, Mapping):
        return None
    receipt_id = consumption.get("receipt_id")
    user_message_id = consumption.get("user_message_id")
    if (
        not isinstance(receipt_id, str)
        or not receipt_id
        or not isinstance(user_message_id, str)
        or not user_message_id
    ):
        return None
    return context


def _same_targeted_reply_semantics(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> bool:
    """Compare exact target/turn semantics while ignoring receipt identity.

    A generation restart necessarily changes the receipt ID and fetch time.
    Everything authored about the target—including Co-think discussion
    context—and the exact delivered user message must remain identical.
    """

    first_consumption = first.get("consumption")
    second_consumption = second.get("consumption")
    if not isinstance(first_consumption, Mapping) or not isinstance(
        second_consumption,
        Mapping,
    ):
        return False
    if (
        first_consumption.get("user_message_id")
        != second_consumption.get("user_message_id")
    ):
        return False
    first_semantics = dict(first)
    second_semantics = dict(second)
    first_semantics.pop("consumption", None)
    second_semantics.pop("consumption", None)
    return first_semantics == second_semantics


def targeted_reply_context(
    conversation_id: str,
    consumer: str,
    generation: str,
    reply_message_id: str,
    consumption_receipt_id: str | None,
    *,
    conn: sqlite3.Connection,
) -> dict[str, Any] | None:
    """Validate a target receipt and return transcript-visible reply context."""

    receipt_id = (
        consumption_receipt_id.strip()
        if isinstance(consumption_receipt_id, str)
        else ""
    )
    delivered = _next_user_message(
        conn,
        conversation_id=conversation_id,
        consumer=consumer,
    )
    delivered_context = (
        None
        if delivered is None
        else _action_snapshot_context(delivered["context_json"])
    )
    if not receipt_id:
        if delivered_context is not None:
            raise ValueError(
                "targeted reply requires the consumption receipt returned by "
                "cowork_action_snapshot_get"
            )
        return None

    receipt = conn.execute(
        """SELECT * FROM conversation_action_snapshot_receipts
           WHERE receipt_id = ? AND conversation_id = ? AND consumer = ?
             AND generation = ?""",
        (receipt_id, conversation_id, consumer, generation),
    ).fetchone()
    if receipt is None:
        raise ValueError(
            "consumption receipt is unavailable for this conversation generation"
        )
    fetch_outcome = str(receipt["fetch_outcome"] or "available")
    if fetch_outcome not in {"available", "unavailable"}:
        raise ValueError("consumption receipt fetch outcome is not resolved")
    user_message = conn.execute(
        """SELECT context_json FROM messages
           WHERE conversation_id = ? AND message_id = ? AND role = 'user'""",
        (conversation_id, str(receipt["user_message_id"])),
    ).fetchone()
    context = (
        None
        if user_message is None
        else _action_snapshot_context(user_message["context_json"])
    )
    if (
        context is None
        or context.get("action_snapshot_id")
        != str(receipt["action_snapshot_id"])
    ):
        raise ValueError("consumption receipt no longer matches its targeted turn")
    bound_reply = receipt["reply_message_id"]
    if bound_reply is not None and str(bound_reply) != reply_message_id:
        raise ValueError("consumption receipt is already bound to another reply")
    if bound_reply is None and (
        delivered is None
        or str(delivered["message_id"]) != str(receipt["user_message_id"])
    ):
        raise ValueError("consumption receipt is not for the next delivered turn")
    consumption_context: dict[str, Any] = {
        "receipt_id": receipt_id,
        "user_message_id": str(receipt["user_message_id"]),
        "fetched_at": str(receipt["fetched_at"]),
        "fetch_outcome": fetch_outcome,
    }
    unavailable_code = receipt["unavailable_code"]
    if fetch_outcome == "unavailable" and isinstance(
        unavailable_code,
        str,
    ):
        consumption_context["unavailable_code"] = unavailable_code
    candidate_context = {
        **context,
        "consumption": consumption_context,
    }

    # A stable reply may already have committed under the predecessor
    # generation while its user-turn acknowledgement did not. Preserve the
    # first durable message context, but only after proving that its old
    # receipt and this generation's receipt name the exact same targeted turn
    # and action context. The caller will bind the new receipt to that message,
    # preserving both generations in the receipt audit.
    existing_message = conn.execute(
        """SELECT conversation_id, role, context_json
           FROM messages WHERE message_id = ?""",
        (reply_message_id,),
    ).fetchone()
    if existing_message is None:
        return candidate_context
    if (
        str(existing_message["conversation_id"]) != conversation_id
        or str(existing_message["role"]) != "agent"
    ):
        raise ValueError(
            "stable reply message_id belongs to another conversation or role"
        )
    existing_context = _receipt_context(existing_message["context_json"])
    if existing_context is None or not _same_targeted_reply_semantics(
        existing_context,
        candidate_context,
    ):
        raise ValueError(
            "stable reply message_id belongs to a different targeted turn"
        )
    existing_consumption = existing_context["consumption"]
    assert isinstance(existing_consumption, Mapping)
    existing_receipt_id = str(existing_consumption["receipt_id"])
    existing_receipt = conn.execute(
        """SELECT * FROM conversation_action_snapshot_receipts
           WHERE receipt_id = ? AND conversation_id = ? AND consumer = ?
             AND user_message_id = ? AND action_snapshot_id = ?""",
        (
            existing_receipt_id,
            conversation_id,
            consumer,
            str(receipt["user_message_id"]),
            str(receipt["action_snapshot_id"]),
        ),
    ).fetchone()
    if existing_receipt is None or (
        existing_receipt["reply_message_id"] is not None
        and str(existing_receipt["reply_message_id"]) != reply_message_id
    ):
        raise ValueError(
            "stable targeted reply has no matching durable consumption receipt"
        )
    return existing_context


def bind_action_snapshot_reply(
    consumption_receipt_id: str,
    reply_message_id: str,
    *,
    conn: sqlite3.Connection,
) -> None:
    """Bind a fetched target receipt to the exact durable assistant reply."""

    now = _now()
    cursor = conn.execute(
        """UPDATE conversation_action_snapshot_receipts
           SET reply_message_id = COALESCE(reply_message_id, ?),
               replied_at = COALESCE(replied_at, ?)
           WHERE receipt_id = ?
             AND (reply_message_id IS NULL OR reply_message_id = ?)""",
        (
            reply_message_id,
            now,
            consumption_receipt_id,
            reply_message_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("consumption receipt could not be bound to this reply")


def receive_user_message(
    conversation_id: str,
    consumer: str,
    generation: str,
    *,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    """Return the oldest unacked user turn without advancing its cursor.

    Long-polling opens short-lived connections and sleeps outside transactions.
    Every pass heartbeats the exact persisted generation, so a rotated lease
    immediately loses access and cannot consume a successor's inbox.
    """
    bounded_timeout = max(0.0, min(float(timeout_seconds), 110.0))
    deadline = time.monotonic() + bounded_timeout
    last_heartbeat = 0.0
    while True:
        conn = get_connection()
        try:
            # Couple lease validation and message selection in one short
            # transaction. A generation fence either commits first and this
            # pass returns lease_lost, or waits until this read completes; an
            # old process can never observe a turn committed after revocation.
            conn.execute("BEGIN IMMEDIATE")
            monotonic_now = time.monotonic()
            if monotonic_now - last_heartbeat >= 10.0:
                if not _touch_agent_lease(
                    conn,
                    conversation_id=conversation_id,
                    consumer=consumer,
                    generation=generation,
                ):
                    conn.rollback()
                    return {"status": "lease_lost"}
                last_heartbeat = monotonic_now
            else:
                lease = conn.execute(
                    """SELECT lease.status, lease.started_at
                       FROM conversation_agent_leases AS lease
                       JOIN conversations AS conversation
                         ON conversation.conversation_id = lease.conversation_id
                       WHERE lease.conversation_id = ? AND lease.consumer = ?
                         AND lease.generation = ?
                         AND lease.status IN ('starting', 'running')
                         AND conversation.status = 'open'""",
                    (conversation_id, consumer, generation),
                ).fetchone()
                starting_age = (
                    None
                    if lease is None or lease["status"] != "starting"
                    else _timestamp_age_seconds(
                        lease["started_at"],
                        now=datetime.now(timezone.utc),
                    )
                )
                if lease is None or (
                    lease["status"] == "starting"
                    and (
                        starting_age is None
                        or starting_age > _AGENT_STARTING_GRACE_SECONDS
                    )
                ):
                    conn.rollback()
                    return {"status": "lease_lost"}
            row = _next_user_message(
                conn,
                conversation_id=conversation_id,
                consumer=consumer,
            )
            conn.commit()
            if row is not None:
                return {
                    "status": "message",
                    "message": ConversationMessage.from_row(dict(row)).to_dict(),
                }
        finally:
            conn.close()
        if time.monotonic() >= deadline:
            return {"status": "timeout" if bounded_timeout > 0 else "empty"}
        time.sleep(
            min(
                max(0.01, poll_interval_seconds),
                max(0.0, deadline - time.monotonic()),
            )
        )


def _action_context_ack_mismatch(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    consumer: str,
    generation: str,
    message_id: str,
    raw_context: object,
    supplied_action_snapshot_id: str | None,
    consumption_receipt_id: str | None,
) -> dict[str, Any] | None:
    context = _action_snapshot_context(raw_context)
    expected = (
        None if context is None else str(context["action_snapshot_id"])
    )
    supplied = (
        supplied_action_snapshot_id.strip()
        if isinstance(supplied_action_snapshot_id, str)
        else ""
    )
    if expected is None:
        if not supplied and not consumption_receipt_id:
            return None
        return {
            "status": "action_snapshot_mismatch",
            "acked": False,
            "action_snapshot_id": None,
        }
    receipt_id = (
        consumption_receipt_id.strip()
        if isinstance(consumption_receipt_id, str)
        else ""
    )
    if not receipt_id:
        return {
            "status": "action_snapshot_receipt_required",
            "acked": False,
            "action_snapshot_id": expected,
        }
    receipt = conn.execute(
        """SELECT * FROM conversation_action_snapshot_receipts
           WHERE receipt_id = ? AND conversation_id = ? AND consumer = ?
             AND generation = ? AND user_message_id = ?
             AND action_snapshot_id = ?""",
        (
            receipt_id,
            conversation_id,
            consumer,
            generation,
            message_id,
            expected,
        ),
    ).fetchone()
    if receipt is None:
        return {
            "status": "action_snapshot_receipt_mismatch",
            "acked": False,
            "action_snapshot_id": expected,
        }
    if receipt["reply_message_id"] is None:
        return {
            "status": "targeted_reply_required",
            "acked": False,
            "action_snapshot_id": expected,
        }
    if not supplied:
        supplied = expected
    if supplied != expected:
        return {
            "status": "action_snapshot_mismatch",
            "acked": False,
            "action_snapshot_id": expected,
        }
    return None


def ack_user_message(
    conversation_id: str,
    consumer: str,
    generation: str,
    message_id: str,
    *,
    action_snapshot_id: str | None = None,
    consumption_receipt_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Advance a cursor only over its exact turn and consumption receipt.

    A targeted turn cannot be acknowledged without the active generation's
    receipt, already bound to a durable reply for that exact user message and
    action snapshot. The redundant ``action_snapshot_id`` echo remains
    accepted for compatibility but never authorizes an acknowledgement.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not _touch_agent_lease(
            conn,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
        ):
            if own_conn:
                conn.rollback()
            return {"status": "lease_lost", "acked": False}
        current = conn.execute(
            """SELECT last_created_at, last_message_id
               FROM conversation_consumer_cursors
               WHERE conversation_id = ? AND consumer = ?""",
            (conversation_id, consumer),
        ).fetchone()
        if current is not None and str(current["last_message_id"]) == message_id:
            delivered = conn.execute(
                "SELECT context_json FROM messages "
                "WHERE conversation_id = ? AND message_id = ? AND role = 'user'",
                (conversation_id, message_id),
            ).fetchone()
            mismatch = _action_context_ack_mismatch(
                conn,
                conversation_id=conversation_id,
                consumer=consumer,
                generation=generation,
                message_id=message_id,
                raw_context=(
                    None if delivered is None else delivered["context_json"]
                ),
                supplied_action_snapshot_id=action_snapshot_id,
                consumption_receipt_id=consumption_receipt_id,
            )
            if mismatch is not None:
                if own_conn:
                    conn.commit()
                return mismatch
            if consumption_receipt_id:
                conn.execute(
                    """UPDATE conversation_action_snapshot_receipts
                       SET acked_at = COALESCE(acked_at, ?)
                       WHERE receipt_id = ?""",
                    (_now(), consumption_receipt_id),
                )
            if own_conn:
                conn.commit()
            return {"status": "acked", "acked": True, "message_id": message_id}

        next_row = _next_user_message(
            conn,
            conversation_id=conversation_id,
            consumer=consumer,
        )
        if next_row is None:
            if own_conn:
                conn.commit()
            return {"status": "empty", "acked": False}
        if str(next_row["message_id"]) != message_id:
            if own_conn:
                conn.commit()
            return {
                "status": "out_of_order",
                "acked": False,
                "next_message_id": str(next_row["message_id"]),
            }
        mismatch = _action_context_ack_mismatch(
            conn,
            conversation_id=conversation_id,
            consumer=consumer,
            generation=generation,
            message_id=message_id,
            raw_context=next_row["context_json"],
            supplied_action_snapshot_id=action_snapshot_id,
            consumption_receipt_id=consumption_receipt_id,
        )
        if mismatch is not None:
            if own_conn:
                conn.commit()
            return mismatch
        now = _now()
        conn.execute(
            """INSERT INTO conversation_consumer_cursors
                   (conversation_id, consumer, last_created_at, last_message_id, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id, consumer) DO UPDATE SET
                   last_created_at = excluded.last_created_at,
                   last_message_id = excluded.last_message_id,
                   updated_at = excluded.updated_at""",
            (
                conversation_id,
                consumer,
                str(next_row["created_at"]),
                str(next_row["message_id"]),
                now,
            ),
        )
        if consumption_receipt_id:
            conn.execute(
                """UPDATE conversation_action_snapshot_receipts
                   SET acked_at = COALESCE(acked_at, ?)
                   WHERE receipt_id = ?""",
                (now, consumption_receipt_id),
            )
        if own_conn:
            conn.commit()
        return {"status": "acked", "acked": True, "message_id": message_id}
    except Exception:
        if own_conn and conn.in_transaction:
            conn.rollback()
        raise
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
