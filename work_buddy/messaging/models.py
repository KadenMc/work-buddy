"""SQLite database schema and query layer for inter-agent messages."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.config import load_config

# Message disposition — the sender-declared intent that drives Stop-hook
# block-worthiness. It is orthogonal to ``priority`` (which sets blocking
# *intensity* among actionable messages — see ``BLOCK_UNTIL_RESOLVED_PRIORITIES``).
#   "actionable":      the agent must see/handle it; may block the Stop hook. Default.
#   "acknowledgement": an auto-ack of something already handled in-band; never blocks.
DISPOSITION_ACTIONABLE = "actionable"
DISPOSITION_ACKNOWLEDGEMENT = "acknowledgement"


def _classify_disposition(
    type: str,
    subject: str,
    tags: list[str] | None,
    status: str = "pending",
    body: str | None = None,
) -> str:
    """Infer a message's disposition from known plumbing signals.

    The fallback when a caller does not declare a disposition (e.g. the Obsidian
    plugin's out-of-band ``consent_grant`` POST), and the rule used to backfill
    legacy rows. Conservative: anything not recognised as plumbing is
    ``actionable`` so the Stop hook never silently stops blocking what it used to.
    """
    tags = tags or []
    # A non-pending message is already handled — never an action item.
    if status and status != "pending":
        return DISPOSITION_ACKNOWLEDGEMENT
    # Consent decisions echoed back for the sidecar to record are plumbing the
    # gateway already handled in-band: the Obsidian-modal fallback posts subject
    # ``consent_grant``/tag ``consent-callback``; the notification-system path
    # posts a ``notification_response`` whose body title is "Consent: <op>".
    if subject == "consent_grant" or "consent-callback" in tags:
        return DISPOSITION_ACKNOWLEDGEMENT
    if body:
        try:
            payload = json.loads(body)
            title = payload.get("title", "") if isinstance(payload, dict) else ""
            if isinstance(title, str) and title.startswith("Consent:"):
                return DISPOSITION_ACKNOWLEDGEMENT
        except (ValueError, TypeError):
            pass
    return DISPOSITION_ACTIONABLE


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS messages (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT,
    sender            TEXT NOT NULL,
    sender_session    TEXT,
    recipient         TEXT NOT NULL,
    recipient_session TEXT,
    type              TEXT NOT NULL,
    priority          TEXT NOT NULL DEFAULT 'normal',
    status            TEXT NOT NULL DEFAULT 'pending',
    subject           TEXT NOT NULL,
    body              TEXT,
    in_reply_to       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT,
    tags              TEXT,
    disposition       TEXT
);

CREATE TABLE IF NOT EXISTS message_reads (
    message_id     TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    reader_project TEXT,
    read_at        TEXT NOT NULL,
    PRIMARY KEY (message_id, session_id),
    FOREIGN KEY (message_id) REFERENCES messages(id)
);

CREATE INDEX IF NOT EXISTS idx_recipient
    ON messages(recipient, status);
CREATE INDEX IF NOT EXISTS idx_recipient_session
    ON messages(recipient, recipient_session, status);
CREATE INDEX IF NOT EXISTS idx_thread
    ON messages(thread_id);
"""


def _db_path(cfg: dict[str, Any] | None = None) -> Path:
    """Resolve the database file path from config."""
    if cfg is None:
        cfg = load_config()
    custom = cfg.get("messaging", {}).get("db_path")
    if custom:
        from work_buddy.paths import repo_root
        p = Path(custom) if Path(custom).is_absolute() else repo_root() / custom
    else:
        from work_buddy.paths import resolve
        p = resolve("db/messages")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_connection(cfg: dict[str, Any] | None = None) -> sqlite3.Connection:
    """Open (or create) the messages database with WAL mode."""
    path = _db_path(cfg)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Run forward-only migrations for schema changes."""
    # v1: add reader_project to message_reads
    cols = {r[1] for r in conn.execute("PRAGMA table_info(message_reads)").fetchall()}
    if "reader_project" not in cols:
        conn.execute("ALTER TABLE message_reads ADD COLUMN reader_project TEXT")
        conn.commit()

    # v2: add disposition to messages and backfill existing rows from the
    # classifier, so legacy plumbing (consent callbacks already in the inbox)
    # stops blocking the Stop hook too. Guarded by the column check → idempotent.
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "disposition" not in mcols:
        conn.execute("ALTER TABLE messages ADD COLUMN disposition TEXT")
        for r in conn.execute(
            "SELECT id, type, subject, tags, status, body FROM messages"
        ).fetchall():
            row_tags = json.loads(r["tags"]) if r["tags"] else []
            disp = _classify_disposition(
                r["type"], r["subject"], row_tags, status=r["status"], body=r["body"]
            )
            conn.execute(
                "UPDATE messages SET disposition = ? WHERE id = ?", (disp, r["id"])
            )
        conn.commit()


def _generate_id(sender: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{ts}-{sender}-{short}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_message(
    conn: sqlite3.Connection,
    *,
    sender: str,
    recipient: str,
    type: str,
    subject: str,
    body: str | None = None,
    sender_session: str | None = None,
    recipient_session: str | None = None,
    thread_id: str | None = None,
    priority: str = "normal",
    status: str = "pending",
    in_reply_to: str | None = None,
    tags: list[str] | None = None,
    disposition: str | None = None,
) -> dict[str, Any]:
    """Insert a new message and return it as a dict.

    ``status`` defaults to ``'pending'``. Callers that emit fire-and-forget
    notifications (e.g. the retry sweep's success FYIs) pass a terminal status
    such as ``'resolved'`` so the message never enters the pending/block path and
    is pruned on the normal TTL.

    ``disposition`` declares whether this message is an action item for the agent
    (``"actionable"`` — may block the Stop hook) or an auto-acknowledgement of
    something already handled in-band (``"acknowledgement"`` — never blocks).
    Senders that know their intent should pass it; when omitted it is inferred by
    ``_classify_disposition`` (conservative default ``"actionable"``).
    """
    msg_id = _generate_id(sender)
    now = _now_iso()

    if thread_id is None:
        thread_id = f"thr-{msg_id}"

    if disposition is None:
        disposition = _classify_disposition(type, subject, tags, status=status, body=body)

    conn.execute(
        """\
        INSERT INTO messages
            (id, thread_id, sender, sender_session, recipient,
             recipient_session, type, priority, status, subject,
             body, in_reply_to, created_at, updated_at, tags, disposition)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg_id, thread_id, sender, sender_session, recipient,
            recipient_session, type, priority, status, subject,
            body, in_reply_to, now, now,
            json.dumps(tags) if tags else None,
            disposition,
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone())


def get_message(conn: sqlite3.Connection, msg_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        return None
    msg = dict(row)
    msg["read_by"] = _get_readers(conn, msg_id)
    return msg


# ---------------------------------------------------------------------------
# Read tracking
# ---------------------------------------------------------------------------

def record_read(
    conn: sqlite3.Connection,
    msg_id: str,
    session_id: str,
    reader_project: str | None = None,
) -> None:
    """Record that a session has read a message."""
    conn.execute(
        "INSERT OR IGNORE INTO message_reads (message_id, session_id, reader_project, read_at) VALUES (?, ?, ?, ?)",
        (msg_id, session_id, reader_project, _now_iso()),
    )
    conn.commit()


def has_been_read_by(conn: sqlite3.Connection, msg_id: str, session_id: str) -> bool:
    """Check if a specific session has read a message."""
    row = conn.execute(
        "SELECT 1 FROM message_reads WHERE message_id = ? AND session_id = ?",
        (msg_id, session_id),
    ).fetchone()
    return row is not None


def _get_readers(conn: sqlite3.Connection, msg_id: str) -> list[dict[str, str]]:
    """Get all sessions that have read a message."""
    rows = conn.execute(
        "SELECT session_id, reader_project, read_at FROM message_reads WHERE message_id = ? ORDER BY read_at ASC",
        (msg_id,),
    ).fetchall()
    return [{"session_id": r["session_id"], "reader_project": r["reader_project"], "read_at": r["read_at"]} for r in rows]


def _get_recipient_readers(
    conn: sqlite3.Connection, msg_id: str, recipient: str
) -> list[dict[str, str]]:
    """Get only sessions from the recipient project that have read a message.

    A message is considered "new" for the recipient only if no session
    belonging to the recipient project has read it.  Reads by the sender
    or other projects are irrelevant for that determination.
    """
    rows = conn.execute(
        "SELECT session_id, reader_project, read_at FROM message_reads "
        "WHERE message_id = ? AND reader_project = ? ORDER BY read_at ASC",
        (msg_id, recipient),
    ).fetchall()
    return [{"session_id": r["session_id"], "reader_project": r["reader_project"], "read_at": r["read_at"]} for r in rows]


def query_messages(
    conn: sqlite3.Connection,
    *,
    recipient: str | None = None,
    session: str | None = None,
    status: str | None = None,
    sender: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query messages with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if recipient:
        clauses.append("recipient = ?")
        params.append(recipient)
    if session:
        # Return messages targeted to this session OR broadcast (NULL)
        clauses.append("(recipient_session IS NULL OR recipient_session = ?)")
        params.append(session)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if sender:
        clauses.append("sender = ?")
        params.append(sender)
    if thread_id:
        clauses.append("thread_id = ?")
        params.append(thread_id)

    where = " AND ".join(clauses) if clauses else "1=1"
    sql = f"SELECT * FROM messages WHERE {where} ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_status(
    conn: sqlite3.Connection,
    msg_id: str,
    new_status: str,
) -> dict[str, Any] | None:
    """Update a message's status. Returns the updated message or None."""
    conn.execute(
        "UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, _now_iso(), msg_id),
    )
    conn.commit()
    return get_message(conn, msg_id)


def get_thread(conn: sqlite3.Connection, thread_id: str) -> list[dict[str, Any]]:
    """Get all messages in a thread, chronologically."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
        (thread_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_reply(
    conn: sqlite3.Connection,
    parent_id: str,
    *,
    sender: str,
    body: str,
    sender_session: str | None = None,
    recipient_session: str | None = None,
    type: str = "ack",
    priority: str = "normal",
    tags: list[str] | None = None,
    disposition: str = DISPOSITION_ACKNOWLEDGEMENT,
) -> dict[str, Any] | None:
    """Reply to an existing message, inheriting thread_id and swapping sender/recipient.

    recipient_session defaults to None (broadcast to any session in the project).
    Pass the parent's sender_session explicitly if you want to target that specific session.

    Replies default to ``acknowledgement`` disposition (a confirmation, not an
    action item); pass ``disposition="actionable"`` for a reply the recipient must act on.
    """
    parent = get_message(conn, parent_id)
    if parent is None:
        return None

    return create_message(
        conn,
        sender=sender,
        recipient=parent["sender"],
        recipient_session=recipient_session,
        type=type,
        subject=f"Re: {parent['subject']}",
        body=body,
        sender_session=sender_session,
        thread_id=parent["thread_id"],
        priority=priority,
        in_reply_to=parent_id,
        tags=tags,
        disposition=disposition,
    )


# ---------------------------------------------------------------------------
# Summary for hook context injection
# ---------------------------------------------------------------------------

# Priorities whose pending messages keep blocking the Stop hook even after the
# recipient has read them — until they are explicitly resolved (e.g. via the
# /tmp/wb/resolve helper). Lower-priority messages surface once (while unread)
# then release. Set to an empty set to collapse to "surface once" for every
# priority (pure block-on-unread semantics).
BLOCK_UNTIL_RESOLVED_PRIORITIES = {"high", "urgent"}

def summarize_pending(
    conn: sqlite3.Connection,
    recipient: str,
    session: str | None = None,
    max_chars: int = 9500,
    ttl_days: int | None = None,
    include_instructions: bool = True,
    unread_only: bool = False,
) -> str:
    """Build a human-readable summary of pending messages for context injection.

    Filters messages by: unread by anyone OR created within ttl_days.
    This keeps the summary fresh without losing unseen messages.

    When include_instructions is False (e.g. UserPromptSubmit), only the
    message list is returned — the curl instructions are omitted to save context.

    When ``unread_only`` is True, messages the recipient has already read are
    excluded regardless of the TTL window. The Stop hook uses this so that a
    message which has been surfaced once (and auto-marked read by a prior render)
    does not keep the agent's turn blocked — it surfaces exactly once, then the
    next render returns empty and the block releases. The non-blocking summaries
    (SessionStart / UserPromptSubmit) leave it False and still show read-but-recent
    messages as context.

    Block-worthiness is gated first by ``disposition``: on the Stop path
    (``unread_only=True``) ``acknowledgement`` messages are excluded outright —
    they are auto-acks of work already handled in-band and must never block. Only
    ``actionable`` messages reach the unread/priority logic below.
    """
    if ttl_days is None:
        from work_buddy.config import load_config
        cfg = load_config()
        ttl_days = cfg.get("messaging", {}).get("summary_ttl_days", 7)

    msgs = query_messages(conn, recipient=recipient, session=session, status="pending")
    if not msgs:
        return ""

    # Mark read status and filter by TTL
    # Only consider reads by the *recipient* project when deciding new vs read.
    # Reads by the sender or other projects don't count.
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    filtered = []
    new_count = 0
    for m in msgs:
        # Disposition gates block-worthiness first: acknowledgement messages are
        # auto-acks of work already handled in-band (consent grants, FYIs) and
        # must never block the Stop hook. They still appear in the non-blocking
        # context summaries (unread_only=False) for visibility.
        disposition = m.get("disposition") or DISPOSITION_ACTIONABLE
        if unread_only and disposition == DISPOSITION_ACKNOWLEDGEMENT:
            continue

        recipient_readers = _get_recipient_readers(conn, m["id"], recipient)
        is_read = len(recipient_readers) > 0
        m["_read"] = is_read
        m["_readers"] = recipient_readers

        # Among actionable messages: unread ones always surface. For the
        # non-blocking summaries (unread_only=False) also keep read messages
        # within the TTL window so SessionStart/UserPromptSubmit retain recent
        # context. The Stop hook passes unread_only=True: a read message is
        # dropped (it surfaced once, now releases) UNLESS it is high/urgent
        # priority, which keeps blocking until resolved — /tmp/wb/resolve is the
        # discoverable exit.
        if not is_read:
            new_count += 1
            filtered.append(m)
        elif unread_only:
            if m["priority"] in BLOCK_UNTIL_RESOLVED_PRIORITIES:
                filtered.append(m)
        else:
            try:
                created = datetime.fromisoformat(m["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created >= cutoff:
                    filtered.append(m)
            except (ValueError, TypeError):
                filtered.append(m)  # Include if we can't parse the date

    if not filtered:
        return ""

    msgs = filtered

    lines = [f"MESSAGES: {len(msgs)} pending for {recipient} ({new_count} new)\n"]
    for m in msgs:
        age = _format_age(m["created_at"])
        tag = "" if m["_read"] else " *NEW*"
        if m["_read"]:
            short_ids = [r["session_id"][:8] for r in m["_readers"]]
            tag = f" (read by {', '.join(short_ids)})"
        target = ""
        if m.get("recipient_session"):
            target = f" [session-targeted]"
        body_hint = ""
        if m.get("body"):
            word_count = len(m["body"].split())
            body_hint = f" [~{word_count} words]"
        lines.append(
            f"  - {tag.strip() + ' ' if tag.strip() else ''}[{m['type']}] from {m['sender']}: "
            f"{m['subject']} ({age}){target}{body_hint}"
        )
        lines.append(f"    id: {m['id']}")

    if new_count > 0:
        lines.append("")
        lines.append("You have new messages. Read them and inform the user of their contents.")
        lines.append("To read: bash /tmp/wb/read --id <message-id>")

    if include_instructions:
        lines.append("")
        lines.append("Messaging commands (run --help for details):")
        lines.append("  bash /tmp/wb/read --id <message-id>")
        lines.append("  bash /tmp/wb/reply --id <message-id> --body \"...\"")
        lines.append("  bash /tmp/wb/send --to <recipient> --subject \"...\" --body \"...\"")
        lines.append("  bash /tmp/wb/resolve --id <message-id>   # clear a seen notification")

    # Auto-mark new messages as read by this session since they appeared in the summary
    if session:
        for m in msgs:
            if not m["_read"]:
                record_read(conn, m["id"], session, reader_project=recipient)

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n  ... (truncated)"
    return summary


def _format_age(iso_timestamp: str) -> str:
    """Format a timestamp as a human-readable age string."""
    try:
        created = datetime.fromisoformat(iso_timestamp)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - created
        hours = delta.total_seconds() / 3600
        if hours < 1:
            minutes = int(delta.total_seconds() / 60)
            return f"{minutes}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        days = int(hours / 24)
        return f"{days}d ago"
    except (ValueError, TypeError):
        return "unknown age"


# ---------------------------------------------------------------------------
# Lifecycle registration — messages artifact
# ---------------------------------------------------------------------------
#
# Registers a SqliteRowsStorage + PerRecordTtl(created_at, default 30d)
# + Delete + retention_predicate(keep pending) + SessionTagged
# (sender_session, recipient_session) artifact under "messages".
#
# The retention predicate protects only *actionable* pending messages from the
# age-based sweep. Acknowledgement-disposition rows (auto-acks of in-band-handled
# work — consent echoes, fire-and-forget FYIs) carry no standing obligation, so
# they reap on the normal TTL even while pending instead of accumulating forever.

import logging as _logging
_logger = _logging.getLogger(__name__)


def _messages_retention(record: dict) -> bool:
    """Return True (keep) for messages that must survive the age-based sweep.

    Keeps a row only while it is an unresolved *action item*: status still
    ``pending`` and disposition ``actionable`` (the conservative default a
    missing disposition resolves to). Acknowledgements are auto-acks of work
    already handled in-band and carry no obligation, so they fall through to the
    TTL even while pending. Resolved/terminal rows are never kept here.
    """
    return (
        record.get("status") == "pending"
        and (record.get("disposition") or DISPOSITION_ACTIONABLE)
        == DISPOSITION_ACTIONABLE
    )


def _register_messages_artifact() -> None:
    try:
        from work_buddy.artifacts import (
            Artifact,
            Delete,
            Lifecycle,
            PerRecordTtl,
            register_artifact,
            SessionTagged,
            SqliteRowsStorage,
        )

        register_artifact(Artifact(
            name="messages",
            storage=SqliteRowsStorage(
                db_path=_db_path(),
                table="messages",
                id_column="id",
                post_delete_sql=[
                    # Clean orphaned message_reads in the same transaction
                    # as the parent delete — same as prune_messages_db.
                    "DELETE FROM message_reads "
                    "WHERE message_id NOT IN (SELECT id FROM messages)",
                ],
                vacuum_on_delete=True,
            ),
            lifecycle=Lifecycle(
                trigger=PerRecordTtl(
                    ttl_field="created_at",
                    default_ttl_days=30,
                ),
                action=Delete(),
                retention_predicate=_messages_retention,
            ),
            provenance=SessionTagged(
                session_columns=["sender_session", "recipient_session"],
            ),
        ))
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("Failed to register messages artifact: %s", exc)


_register_messages_artifact()
