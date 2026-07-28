"""Conversation-scoped agent execution selection and provenance.

The conversations database is the durable authority for which execution
provider/model owns a conversation.  Provider discovery and validation live in
``work_buddy.agent_execution``; this module only persists a trusted, immutable
snapshot without coupling the generic conversation store to any provider SDK.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from work_buddy.conversations.store import get_connection


EXECUTION_METADATA_KEY = "cowork_agent_execution"
EXECUTION_SCHEMA_VERSION = 1
_UNSET = object()


class ConversationExecutionConflict(RuntimeError):
    """The caller tried to replace a selection based on a stale revision."""


class ConversationExecutionCorrupt(RuntimeError):
    """Persisted execution authority exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ConversationExecution:
    """One trusted execution snapshot and its optimistic-lock revision."""

    provider_id: str
    model_id: str
    provider_label: str
    model_label: str
    revision: str | None
    persisted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_label": self.provider_label,
            "model_label": self.model_label,
            "revision": self.revision,
            "persisted": self.persisted,
        }

    def lease_snapshot(self) -> dict[str, Any]:
        """Return the immutable execution identity copied into an agent lease."""
        return {
            "schema_version": EXECUTION_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_label": self.provider_label,
            "model_label": self.model_label,
        }

    def producer_snapshot(self) -> dict[str, Any]:
        """Return trusted provenance suitable for an assistant message."""
        return self.lease_snapshot()


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def _selection_fields(selection: Mapping[str, Any]) -> dict[str, str]:
    return {
        "provider_id": _require_text(selection.get("provider_id"), "provider_id"),
        "model_id": _require_text(selection.get("model_id"), "model_id"),
        "provider_label": _require_text(
            selection.get("provider_label"), "provider_label"
        ),
        "model_label": _require_text(selection.get("model_label"), "model_label"),
    }


def _decode_metadata(raw: object) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ConversationExecutionCorrupt(
                "Saved conversation metadata is invalid"
            ) from exc
    else:
        decoded = raw
    if not isinstance(decoded, dict):
        raise ConversationExecutionCorrupt(
            "Saved conversation metadata is invalid"
        )
    return dict(decoded)


def _decode_persisted(value: object) -> ConversationExecution | None:
    if not isinstance(value, dict):
        return None
    schema_version = value.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != EXECUTION_SCHEMA_VERSION
    ):
        return None
    try:
        fields = _selection_fields(value)
        revision = _require_text(value.get("revision"), "revision")
    except ValueError:
        return None
    return ConversationExecution(
        **fields,
        revision=revision,
        persisted=True,
    )


def _persisted_from_metadata(
    metadata: Mapping[str, Any],
) -> ConversationExecution | None:
    if EXECUTION_METADATA_KEY not in metadata:
        return None
    persisted = _decode_persisted(metadata.get(EXECUTION_METADATA_KEY))
    if persisted is None:
        raise ConversationExecutionCorrupt(
            "Saved Co-work execution selection is invalid"
        )
    return persisted


def projected_execution(
    conversation_id: str | None,
    default_selection: Mapping[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
) -> ConversationExecution:
    """Read a persisted selection or project the server default without writing."""
    default_fields = _selection_fields(default_selection)
    if conversation_id is None:
        return ConversationExecution(
            **default_fields,
            revision=None,
            persisted=False,
        )
    own_conn = conn is None
    active = get_connection() if own_conn else conn
    try:
        row = active.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Conversation not found: {conversation_id}")
        metadata = _decode_metadata(row["metadata"])
        persisted = _persisted_from_metadata(metadata)
        if persisted is not None:
            return persisted
        return ConversationExecution(
            **default_fields,
            revision=None,
            persisted=False,
        )
    finally:
        if own_conn:
            active.close()


def set_execution(
    conversation_id: str,
    selection: Mapping[str, Any],
    *,
    expected_revision: str | None | object = _UNSET,
    conn: sqlite3.Connection | None = None,
) -> ConversationExecution:
    """Persist one trusted selection with compare-and-swap semantics.

    ``expected_revision=None`` means that no selection may already be pinned.
    Omitting ``expected_revision`` performs an unconditional trusted write.
    Re-selecting the same provider/model is idempotent and keeps its revision.
    """
    fields = _selection_fields(selection)
    own_conn = conn is None
    active = get_connection() if own_conn else conn
    if own_conn:
        active.execute("BEGIN IMMEDIATE")
    try:
        row = active.execute(
            "SELECT metadata FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Conversation not found: {conversation_id}")
        metadata = _decode_metadata(row["metadata"])
        current = _persisted_from_metadata(metadata)
        current_revision = None if current is None else current.revision
        if (
            current is not None
            and current.provider_id == fields["provider_id"]
            and current.model_id == fields["model_id"]
        ):
            if own_conn:
                active.commit()
            return current
        if (
            expected_revision is not _UNSET
            and expected_revision != current_revision
        ):
            raise ConversationExecutionConflict("execution_selection_changed")

        result = ConversationExecution(
            **fields,
            revision=uuid.uuid4().hex,
            persisted=True,
        )
        metadata[EXECUTION_METADATA_KEY] = {
            key: value
            for key, value in result.to_dict().items()
            if key != "persisted"
        }
        active.execute(
            """UPDATE conversations
               SET metadata = ?, updated_at = ?
               WHERE conversation_id = ?""",
            (
                json.dumps(metadata),
                datetime.now(timezone.utc).isoformat(),
                conversation_id,
            ),
        )
        if own_conn:
            active.commit()
        return result
    except Exception:
        if own_conn and active.in_transaction:
            active.rollback()
        raise
    finally:
        if own_conn:
            active.close()


def producer_for_lease(
    *,
    conversation_id: str,
    consumer: str,
    generation: str,
    conn: sqlite3.Connection,
) -> dict[str, Any] | None:
    """Read trusted producer provenance from the exact fenced lease."""
    row = conn.execute(
        """SELECT execution_json
           FROM conversation_agent_leases
           WHERE conversation_id = ? AND consumer = ? AND generation = ?
             AND status IN ('starting', 'running')""",
        (conversation_id, consumer, generation),
    ).fetchone()
    if row is None:
        return None
    raw = row["execution_json"]
    if not isinstance(raw, str) or not raw:
        return None
    try:
        execution = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(execution, dict):
        return None
    schema_version = execution.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != EXECUTION_SCHEMA_VERSION
    ):
        return None
    try:
        fields = _selection_fields(execution)
    except ValueError:
        return None
    return {
        "schema_version": EXECUTION_SCHEMA_VERSION,
        **fields,
    }


__all__ = [
    "ConversationExecution",
    "ConversationExecutionConflict",
    "ConversationExecutionCorrupt",
    "EXECUTION_METADATA_KEY",
    "EXECUTION_SCHEMA_VERSION",
    "producer_for_lease",
    "projected_execution",
    "set_execution",
]
