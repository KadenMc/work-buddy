"""Prepared, fail-closed removal of a document from Co-work."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from work_buddy.cowork.lifecycle_state import inspect_lifecycle_state
from work_buddy.cowork.lifecycle_lock import document_lifecycle_lock
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_text
from work_buddy.truth.store import TruthStore


INTENT_TTL = timedelta(minutes=15)
CONSEQUENCE = (
    "Remove this document from Co-work. Its Markdown file and full history will remain."
)


class RetirementError(InvariantViolation):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RetirementIntent:
    id: str
    idempotency_key: str
    actor_ref: str
    document_id: str
    state: str
    expected_file_sha256: str
    expected_projection_sha256: str
    expected_snapshot_sha256: str
    expected_structured_head_sha256: str
    consequence_sha256: str
    created_at: str
    expires_at: str
    receipt: dict[str, Any] | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _expiry() -> str:
    return (datetime.now(timezone.utc) + INTENT_TTL).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _expired(value: str) -> bool:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(
        timezone.utc
    )


def _actor_ref(actor: Actor) -> str:
    if actor.kind != "human" or not actor.ref:
        raise RetirementError("human_actor_required", "A dashboard human actor is required.")
    return actor.ref


def _intent(row: sqlite3.Row) -> RetirementIntent:
    return RetirementIntent(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        actor_ref=row["actor_ref"],
        document_id=row["document_id"],
        state=row["state"],
        expected_file_sha256=row["expected_file_sha256"],
        expected_projection_sha256=row["expected_projection_sha256"],
        expected_snapshot_sha256=row["expected_snapshot_sha256"],
        expected_structured_head_sha256=row["expected_structured_head_sha256"],
        consequence_sha256=row["consequence_sha256"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        receipt=None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
    )


def _load(
    store: TruthStore,
    intent_id: str,
    actor_ref: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> RetirementIntent:
    if conn is None:
        with store._read_connection() as read_conn:
            row = read_conn.execute(
                "SELECT * FROM cowork_retirement_intents WHERE id = ?", (intent_id,)
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM cowork_retirement_intents WHERE id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        raise RetirementError("intent_not_found", "Removal confirmation does not exist.", status=404)
    value = _intent(row)
    if value.actor_ref != actor_ref:
        raise RetirementError("intent_actor_mismatch", "Removal confirmation belongs to another person.", status=403)
    return value


def _require_no_recovery(store: TruthStore, document_id: str) -> None:
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT id, state, recovery_detail FROM cowork_materialization_intents WHERE document_id = ? AND (state IN ('prepared', 'publishing') OR recovery_detail LIKE 'recovery_required:%') ORDER BY created_at DESC LIMIT 1",
            (document_id,),
        ).fetchone()
    if row is not None:
        raise RetirementError(
            "recovery_required",
            "Finish recovering the most recent Save before removing this document.",
            status=409,
            retryable=True,
            details={"operation_id": row["id"]},
        )


def prepare_retirement(
    store: TruthStore,
    *,
    document_id: str,
    actor: Actor,
    idempotency_key: str,
) -> tuple[RetirementIntent, bool]:
    actor_ref = _actor_ref(actor)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise RetirementError("invalid_idempotency_key", "A bounded idempotency_key is required.")
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_retirement_intents WHERE actor_ref = ? AND idempotency_key = ?",
            (actor_ref, key),
        ).fetchone()
    if row is not None:
        prior = _intent(row)
        if prior.document_id != document_id:
            raise RetirementError("idempotency_conflict", "This request key was used for another document.", status=409)
        return prior, False

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise RetirementError("already_retired", "This document is already removed from Co-work.", status=409)
        if not document_surface_allowed(store, document):
            raise RetirementError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        _require_no_recovery(store, document_id)
        state = inspect_lifecycle_state(store, document)
        if state.initialization_state != "ready" or state.structured_head_sha256 is None:
            raise RetirementError("document_not_ready", "This document must be repaired before removal.", status=409)
        if state.drift_state == "missing":
            raise RetirementError("missing_file", "The Markdown file is missing; resolve it before removal.", status=409)
        if state.drift_state == "drifted":
            raise RetirementError("external_changes", "Review or replace the external Markdown changes before removal.", status=409)
        if state.unmaterialized_structured_edits:
            raise RetirementError("unsaved_edits", "Save the latest Co-work edits before removal.", status=409)
        if not state.baseline_available:
            raise RetirementError("baseline_unavailable", "The last saved Markdown baseline must be repaired before removal.", status=409)
        if document.ydoc_snapshot_sha256 is None or state.current_file_sha256 is None:
            raise RetirementError("document_not_ready", "This document is not ready for removal.", status=409)
        now = _now()
        intent_id = new_id()
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO cowork_retirement_intents (id, idempotency_key, actor_ref, document_id, state, expected_file_sha256, expected_projection_sha256, expected_snapshot_sha256, expected_structured_head_sha256, consequence_sha256, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    key,
                    actor_ref,
                    document.id,
                    state.current_file_sha256,
                    document.content_sha256,
                    document.ydoc_snapshot_sha256,
                    state.structured_head_sha256,
                    sha256_text(CONSEQUENCE),
                    now,
                    now,
                    _expiry(),
                ),
            )
        return _load(store, intent_id, actor_ref), True


def commit_retirement(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """Commit retirement while excluding conversation creation and feedback."""

    with document_lifecycle_lock(store.store_id, document_id):
        return _commit_retirement_locked(
            store,
            document_id=document_id,
            intent_id=intent_id,
            actor=actor,
        )


def _commit_retirement_locked(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
) -> dict[str, Any]:
    actor_ref = _actor_ref(actor)
    intent = _load(store, intent_id, actor_ref)
    if intent.document_id != document_id:
        raise RetirementError("intent_document_mismatch", "Removal confirmation belongs to another document.", status=409)
    if intent.state == "committed" and intent.receipt is not None:
        return intent.receipt
    if intent.state != "prepared":
        raise RetirementError("intent_not_committable", f"Removal confirmation is {intent.state}.", status=409)
    if _expired(intent.expires_at):
        raise RetirementError("intent_expired", "Removal confirmation expired; review it again.", status=409)

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        intent = _load(store, intent_id, actor_ref)
        if intent.document_id != document_id:
            raise RetirementError(
                "intent_document_mismatch",
                "Removal confirmation belongs to another document.",
                status=409,
            )
        if intent.state == "committed" and intent.receipt is not None:
            return intent.receipt
        if intent.state != "prepared":
            raise RetirementError(
                "intent_not_committable",
                f"Removal confirmation is {intent.state}.",
                status=409,
            )
        if _expired(intent.expires_at):
            raise RetirementError(
                "intent_expired",
                "Removal confirmation expired; review it again.",
                status=409,
            )
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise RetirementError("already_retired", "This document is already removed from Co-work.", status=409)
        if not document_surface_allowed(store, document):
            raise RetirementError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        _require_no_recovery(store, document_id)
        state = inspect_lifecycle_state(store, document)
        if (
            not state.clean_materialized
            or not state.baseline_available
            or state.current_file_sha256 != intent.expected_file_sha256
            or document.content_sha256 != intent.expected_projection_sha256
            or document.ydoc_snapshot_sha256 != intent.expected_snapshot_sha256
            or state.structured_head_sha256 != intent.expected_structured_head_sha256
        ):
            raise RetirementError(
                "confirmation_stale",
                "The document changed after confirmation; review removal again.",
                status=409,
            )
        at = _now()
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_intent = _load(store, intent.id, actor_ref, conn=conn)
            if current_intent.state != "prepared":
                raise RetirementError("intent_not_committable", "Removal confirmation changed before commit.", status=409)
            event = documents.retire_document(
                store,
                document_id=document_id,
                actor=actor,
                at=at,
                conn=conn,
            )
            receipt = {
                "ok": True,
                "intent_id": intent.id,
                "document_id": document_id,
                "lifecycle": "retired",
                "retired_at": event.at,
                "doc_event_id": event.id,
                "file_retained": True,
                "history_retained": True,
            }
            conn.execute(
                "UPDATE cowork_retirement_intents SET state = 'committed', updated_at = ?, committed_at = ?, receipt_json = ?, recovery_detail = NULL WHERE id = ? AND state = 'prepared'",
                (at, at, canonical_json(receipt), intent.id),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        store._run_on_commit()
        return receipt


__all__ = [
    "CONSEQUENCE",
    "RetirementError",
    "RetirementIntent",
    "commit_retirement",
    "prepare_retirement",
]
