"""Durable document-source dependencies for Co-work conversation history."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import threading
from pathlib import Path

from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
    source_foundation_read_only,
)
from work_buddy.conversations.store import get_connection, redact_message_content
from work_buddy.paths import resolve
from work_buddy.truth.identity import sha256_text, utc_now


_DB_PATH = resolve("db/cowork-conversation-source-dependencies")
_SCHEMA_VERSION = 1
_SCHEMA_LOCK = threading.Lock()
_REDACTION_REPLACEMENT = "[Redacted because its source was removed.]"


class ConversationSourceDependencyStoreError(RuntimeError):
    """The durable conversation-source dependency authority is unavailable."""


@dataclass(frozen=True, slots=True)
class ConversationSourceDependency:
    dependency_id: str
    store_id: str
    document_id: str
    conversation_id: str
    message_id: str
    role: str
    content_sha256: str
    relationship: str
    input_manifest_sha256: str | None
    state: str
    created_at: str
    updated_at: str


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with _SCHEMA_LOCK:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cowork_conversation_source_dependency_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cowork_conversation_source_dependencies (
                dependency_id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                message_id TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role IN ('user','agent')),
                content_sha256 TEXT NOT NULL,
                relationship TEXT NOT NULL CHECK(
                    relationship IN ('contains_exact_copy','semantic_derivative')
                ),
                input_manifest_sha256 TEXT,
                state TEXT NOT NULL DEFAULT 'active' CHECK(
                    state IN ('active','scrubbed','review_required')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cowork_conversation_dependency_document
            ON cowork_conversation_source_dependencies(store_id, document_id, state);
            """
        )
        row = conn.execute(
            "SELECT value FROM cowork_conversation_source_dependency_meta "
            "WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO cowork_conversation_source_dependency_meta(key, value) "
                "VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
        elif int(row["value"]) != _SCHEMA_VERSION:
            raise ConversationSourceDependencyStoreError(
                "unsupported_cowork_conversation_source_dependency_schema"
            )


def _validate_existing(conn: sqlite3.Connection) -> None:
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        row = conn.execute(
            "SELECT value FROM cowork_conversation_source_dependency_meta "
            "WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise ConversationSourceDependencyStoreError(
            "cowork_conversation_source_dependency_state_invalid_during_restore_reconciliation"
        ) from exc
    if (
        not integrity
        or any(str(item[0]).lower() != "ok" for item in integrity)
        or row is None
        or str(row["value"]) != str(_SCHEMA_VERSION)
    ):
        raise ConversationSourceDependencyStoreError(
            "cowork_conversation_source_dependency_state_invalid_during_restore_reconciliation"
        )


def _connect(
    path: Path | None = None,
    *,
    write: bool = False,
) -> sqlite3.Connection:
    target = (_DB_PATH if path is None else path).expanduser().resolve()
    read_only = source_foundation_read_only()
    if write:
        require_source_foundation_writable(
            "cowork_conversation_source_dependencies.write"
        )
    if read_only and not target.is_file():
        raise ConversationSourceDependencyStoreError(
            "cowork_conversation_source_dependency_state_missing_during_restore_reconciliation"
        )
    if read_only:
        conn = sqlite3.connect(
            f"file:{target}?mode=ro",
            timeout=10,
            uri=True,
        )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
        _validate_existing(conn)
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        _ensure_schema(conn)
    return conn


def _row(value: sqlite3.Row) -> ConversationSourceDependency:
    return ConversationSourceDependency(
        dependency_id=str(value["dependency_id"]),
        store_id=str(value["store_id"]),
        document_id=str(value["document_id"]),
        conversation_id=str(value["conversation_id"]),
        message_id=str(value["message_id"]),
        role=str(value["role"]),
        content_sha256=str(value["content_sha256"]),
        relationship=str(value["relationship"]),
        input_manifest_sha256=(
            None
            if value["input_manifest_sha256"] is None
            else str(value["input_manifest_sha256"])
        ),
        state=str(value["state"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
    )


def relationship_to_document(
    content: str,
    *,
    frozen_markdown: str | None,
) -> str:
    """Conservatively identify retained verbatim document material.

    A 32-character contiguous witness avoids promoting common words or short
    labels into exact-copy status while still catching ordinary quotations and
    copied sentences.  Anything else remains a semantic derivative requiring
    human review during source removal.
    """

    if isinstance(frozen_markdown, str) and frozen_markdown:
        candidate = content.strip()
        if len(candidate) >= 32:
            for index in range(0, len(candidate) - 31):
                if candidate[index : index + 32] in frozen_markdown:
                    return "contains_exact_copy"
    return "semantic_derivative"


def record_conversation_source_dependency(
    *,
    store_id: str,
    document_id: str,
    conversation_id: str,
    message_id: str,
    role: str,
    content: str,
    frozen_markdown: str | None = None,
    input_manifest_sha256: str | None = None,
    path: Path | None = None,
) -> ConversationSourceDependency:
    require_source_foundation_writable(
        "cowork_conversation_source_dependencies.record"
    )
    if role not in {"user", "agent"}:
        raise ValueError("conversation dependency role is invalid")
    relationship = relationship_to_document(
        content,
        frozen_markdown=frozen_markdown,
    )
    content_sha256 = sha256_text(content)
    dependency_id = f"cowork-conversation-message:{message_id}"
    now = utc_now()
    with _connect(path, write=True) as conn:
        existing = conn.execute(
            "SELECT * FROM cowork_conversation_source_dependencies WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if existing is not None:
            value = _row(existing)
            if (
                value.store_id != store_id
                or value.document_id != document_id
                or value.conversation_id != conversation_id
                or value.role != role
                or value.content_sha256 != content_sha256
                or value.relationship != relationship
                or value.input_manifest_sha256 != input_manifest_sha256
            ):
                raise ValueError(
                    "conversation message dependency identity was reused differently"
                )
            return value
        conn.execute(
            "INSERT INTO cowork_conversation_source_dependencies "
            "(dependency_id, store_id, document_id, conversation_id, message_id, "
            "role, content_sha256, relationship, input_manifest_sha256, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                dependency_id,
                store_id,
                document_id,
                conversation_id,
                message_id,
                role,
                content_sha256,
                relationship,
                input_manifest_sha256,
                now,
                now,
            ),
        )
        created = conn.execute(
            "SELECT * FROM cowork_conversation_source_dependencies WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert created is not None
    return _row(created)


def conversation_dependencies_for_document(
    store_id: str,
    document_id: str,
    *,
    path: Path | None = None,
) -> tuple[ConversationSourceDependency, ...]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM cowork_conversation_source_dependencies "
            "WHERE store_id = ? AND document_id = ? ORDER BY created_at, dependency_id",
            (store_id, document_id),
        ).fetchall()
    return tuple(_row(item) for item in rows)


def redact_document_conversation_dependencies(
    *,
    store_id: str,
    document_id: str,
    path: Path | None = None,
) -> dict[str, object]:
    """Scrub exact copies and keep semantic history fail-closed for review.

    The conversation itself is the inventory authority.  Dependency rows add
    the classification and input-manifest binding, but their absence can never
    make cleanup appear complete: every persisted user/agent turn in the bound
    document conversation is conservatively registered as a semantic
    derivative before redaction proceeds.
    """

    require_source_foundation_writable(
        "cowork_conversation_source_dependencies.redact"
    )

    from work_buddy.cowork.conversations import find_document_conversation

    conversation_id = find_document_conversation(
        document_id=document_id,
        store_id=store_id,
    )
    if conversation_id is not None:
        with get_connection() as conversation_conn:
            message_rows = conversation_conn.execute(
                "SELECT message_id, role, content FROM messages "
                "WHERE conversation_id = ? AND role IN ('user', 'agent') "
                "ORDER BY created_at, message_id",
                (conversation_id,),
            ).fetchall()
        known = {
            dependency.message_id
            for dependency in conversation_dependencies_for_document(
                store_id,
                document_id,
                path=path,
            )
        }
        for message in message_rows:
            message_id = str(message["message_id"])
            if message_id in known:
                continue
            record_conversation_source_dependency(
                store_id=store_id,
                document_id=document_id,
                conversation_id=conversation_id,
                message_id=message_id,
                role=str(message["role"]),
                content=str(message["content"]),
                # Without a durable classification witness the only safe
                # recovery is semantic-derivative review, never inferred
                # exact-copy deletion.
                frozen_markdown=None,
                path=path,
            )

    dependencies = conversation_dependencies_for_document(
        store_id,
        document_id,
        path=path,
    )
    scrubbed: list[str] = []
    review_required: list[str] = []
    for dependency in dependencies:
        if dependency.state == "scrubbed":
            scrubbed.append(dependency.message_id)
            continue
        if dependency.relationship == "semantic_derivative":
            review_required.append(dependency.message_id)
            with _connect(path, write=True) as conn:
                conn.execute(
                    "UPDATE cowork_conversation_source_dependencies "
                    "SET state = 'review_required', updated_at = ? "
                    "WHERE dependency_id = ? AND state != 'scrubbed'",
                    (utc_now(), dependency.dependency_id),
                )
            continue
        removed = redact_message_content(
            dependency.conversation_id,
            dependency.message_id,
            expected_content_sha256=dependency.content_sha256,
            replacement=_REDACTION_REPLACEMENT,
        )
        if not removed:
            review_required.append(dependency.message_id)
            next_state = "review_required"
        else:
            scrubbed.append(dependency.message_id)
            next_state = "scrubbed"
        with _connect(path, write=True) as conn:
            conn.execute(
                "UPDATE cowork_conversation_source_dependencies "
                "SET state = ?, updated_at = ? WHERE dependency_id = ?",
                (next_state, utc_now(), dependency.dependency_id),
            )
    return {
        "complete": not review_required,
        "scrubbed_message_ids": tuple(scrubbed),
        "review_required_message_ids": tuple(review_required),
        "dependency_count": len(dependencies),
    }


__all__ = [
    "ConversationSourceDependency",
    "ConversationSourceDependencyStoreError",
    "conversation_dependencies_for_document",
    "record_conversation_source_dependency",
    "redact_document_conversation_dependencies",
    "relationship_to_document",
]
