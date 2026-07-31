"""Explicit replacement fallback for out-of-band Markdown reimport."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.cowork.lifecycle_state import inspect_lifecycle_state
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.source_observation import (
    SourceObservationError,
    read_document_source,
)
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.store import DocumentRecord, TruthStore, _valid_digest


MAX_SNAPSHOT_BYTES = ydoc_store.MAX_OPAQUE_SEGMENT_BYTES
INTENT_TTL = timedelta(minutes=15)


class ReimportError(InvariantViolation):
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
class ReimportIntent:
    id: str
    idempotency_key: str
    actor_ref: str
    document_id: str
    state: str
    expected_file_sha256: str
    prior_projection_sha256: str
    prior_snapshot_sha256: str
    prior_structured_head_sha256: str
    source_byte_length: int
    staged_path: str
    document_version_id: str
    created_at: str
    expires_at: str
    receipt: dict[str, Any] | None
    recovery_detail: str | None


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
        raise ReimportError("human_actor_required", "a dashboard human actor is required")
    return actor.ref


def _intent(row: sqlite3.Row) -> ReimportIntent:
    return ReimportIntent(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        actor_ref=row["actor_ref"],
        document_id=row["document_id"],
        state=row["state"],
        expected_file_sha256=row["expected_file_sha256"],
        prior_projection_sha256=row["prior_projection_sha256"],
        prior_snapshot_sha256=row["prior_snapshot_sha256"],
        prior_structured_head_sha256=row["prior_structured_head_sha256"],
        source_byte_length=int(row["source_byte_length"]),
        staged_path=row["staged_path"],
        document_version_id=row["document_version_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        receipt=None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
        recovery_detail=row["recovery_detail"],
    )


def _load_any(
    store: TruthStore,
    intent_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> ReimportIntent:
    if conn is None:
        with store._read_connection() as read_conn:
            row = read_conn.execute(
                "SELECT * FROM cowork_reimport_intents WHERE id = ?", (intent_id,)
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM cowork_reimport_intents WHERE id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        raise ReimportError("intent_not_found", "reimport intent does not exist", status=404)
    return _intent(row)


def _load(
    store: TruthStore,
    intent_id: str,
    actor_ref: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> ReimportIntent:
    value = _load_any(store, intent_id, conn=conn)
    if value.actor_ref != actor_ref:
        raise ReimportError("intent_actor_mismatch", "reimport intent belongs to another actor", status=403)
    return value


def _stage_path(store: TruthStore, intent_id: str) -> Path:
    return store.paths.runtime / "reimport" / intent_id / "source.md"


def _owned_stage_path(store: TruthStore, intent: ReimportIntent) -> Path:
    """Return the exact intent-owned staging file, rejecting redirected paths."""

    expected = Path(os.path.abspath(_stage_path(store, intent.id)))
    staged = Path(intent.staged_path)
    actual = Path(os.path.abspath(staged))
    if actual != expected or staged.is_symlink():
        raise ReimportError(
            "recovery_required",
            "The replacement staging path is outside its owned runtime slot.",
            status=409,
            retryable=True,
        )
    try:
        if staged.exists() and staged.resolve() != expected:
            raise ReimportError(
                "recovery_required",
                "The replacement staging path is redirected.",
                status=409,
                retryable=True,
            )
    except OSError as exc:
        raise ReimportError(
            "recovery_required",
            "The replacement staging path cannot be verified.",
            status=409,
            retryable=True,
        ) from exc
    return staged


def _remove_verified_stage(store: TruthStore, intent: ReimportIntent) -> bool:
    path = _owned_stage_path(store, intent)
    if not path.exists():
        return False
    if not path.is_file():
        raise ReimportError(
            "recovery_required",
            "The replacement has an unsafe staged source.",
            status=409,
            retryable=True,
        )
    data = path.read_bytes()
    if (
        len(data) != intent.source_byte_length
        or sha256_bytes(data) != intent.expected_file_sha256
    ):
        raise ReimportError(
            "recovery_required",
            "The replacement's staged source needs manual cleanup.",
            status=409,
            retryable=True,
        )
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return True


def _require_file_backed_source(document: DocumentRecord) -> None:
    """Reject the legacy replacement workflow for detached import sources."""

    if documents.source_is_detached(document):
        raise ReimportError(
            "source_writeback_forbidden",
            (
                "From file keeps its own managed Co-work copy. Reimport cannot "
                "replace that copy from a changed source file."
            ),
            status=409,
            details={
                "source_writeback": documents.SOURCE_WRITEBACK_NEVER,
                "recovery_action": "open_existing_managed_copy",
            },
        )


def prepare_reimport(
    store: TruthStore,
    *,
    document_id: str,
    actor: Actor,
    idempotency_key: str,
) -> tuple[ReimportIntent, bool]:
    actor_ref = _actor_ref(actor)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ReimportError("invalid_idempotency_key", "a bounded idempotency_key is required")
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_reimport_intents WHERE actor_ref = ? AND idempotency_key = ?",
            (actor_ref, key),
        ).fetchone()
    if row is not None:
        prior = _intent(row)
        if prior.document_id != document_id:
            raise ReimportError("idempotency_conflict", "idempotency key was used for another reimport", status=409)
        _require_file_backed_source(
            documents.get_document(store, prior.document_id)
        )
        return prior, False

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise ReimportError("document_retired", "retired documents cannot be reimported", status=409)
        _require_file_backed_source(document)
        if not document_surface_allowed(store, document):
            raise ReimportError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        state = inspect_lifecycle_state(store, document)
        if state.initialization_state != "ready" or state.structured_head_sha256 is None:
            raise ReimportError("document_not_ready", f"document is {state.initialization_state}", status=409)
        try:
            current_source = read_document_source(store, document)
        except SourceObservationError as exc:
            raise ReimportError(
                exc.code,
                str(exc),
                status=exc.status,
                details=exc.details,
                retryable=exc.retryable,
            ) from exc
        if current_source.sha256 == document.content_sha256:
            raise ReimportError("no_external_drift", "external Markdown matches Co-work", status=409)
        if state.unmaterialized_structured_edits:
            raise ReimportError(
                "unmaterialized_structured_edits",
                "Save or discard Co-work edits before replacing from the external file",
                status=409,
            )
        if document.ydoc_snapshot_sha256 is None:
            raise ReimportError(
                "document_not_ready",
                "The structured document is not initialized.",
                status=409,
            )
        assert current_source.data is not None
        source = current_source.data
        source_sha = current_source.sha256
        intent_id = new_id()
        staged = _stage_path(store, intent_id)
        atomic_write_bytes(staged, source)
        if sha256_bytes(staged.read_bytes()) != source_sha:
            raise ReimportError("staging_failed", "staged external Markdown failed verification", status=500)
        now = _now()
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO cowork_reimport_intents (id, idempotency_key, actor_ref, document_id, state, expected_file_sha256, prior_projection_sha256, prior_snapshot_sha256, prior_structured_head_sha256, source_byte_length, staged_path, document_version_id, created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    key,
                    actor_ref,
                    document.id,
                    source_sha,
                    document.content_sha256,
                    document.ydoc_snapshot_sha256,
                    state.structured_head_sha256,
                    len(source),
                    str(staged),
                    new_id(),
                    now,
                    now,
                    _expiry(),
                ),
            )
        return _load(store, intent_id, actor_ref), True


def read_reimport_source(
    store: TruthStore,
    *,
    intent_id: str,
    actor: Actor,
) -> tuple[ReimportIntent, bytes]:
    intent = _load(store, intent_id, _actor_ref(actor))
    if intent.state not in {"prepared", "committed"}:
        raise ReimportError("intent_not_readable", f"reimport intent is {intent.state}", status=409)
    path = _owned_stage_path(store, intent)
    if not path.is_file():
        raise ReimportError("staged_source_missing", "staged reimport source is missing", status=409, retryable=True)
    data = path.read_bytes()
    if len(data) != intent.source_byte_length or sha256_bytes(data) != intent.expected_file_sha256:
        raise ReimportError("staged_source_corrupt", "staged reimport source failed verification", status=409)
    return intent, data


def _cleanup_committed_stage(store: TruthStore, intent: ReimportIntent) -> None:
    _remove_verified_stage(store, intent)


def commit_reimport(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
    replacement_snapshot: bytes,
    replacement_snapshot_sha256: str,
) -> dict[str, Any]:
    actor_ref = _actor_ref(actor)
    intent = _load(store, intent_id, actor_ref)
    if intent.document_id != document_id:
        raise ReimportError("intent_document_mismatch", "reimport intent belongs to another document", status=409)
    if intent.state == "committed" and intent.receipt is not None:
        try:
            ydoc_store.recover_compaction(store, document_id=document_id)
        except ydoc_store.CompactionRecoveryRequired as exc:
            raise ReimportError("recovery_required", str(exc), status=409, retryable=True) from exc
        _cleanup_committed_stage(store, intent)
        return intent.receipt
    if intent.state != "prepared":
        raise ReimportError("intent_not_committable", f"reimport intent is {intent.state}", status=409)
    if _expired(intent.expires_at):
        raise ReimportError("intent_expired", "reimport intent expired; prepare again", status=409)
    if not isinstance(replacement_snapshot, (bytes, bytearray, memoryview)):
        raise ReimportError("invalid_snapshot", "replacement snapshot must be binary")
    snapshot = bytes(replacement_snapshot)
    if len(snapshot) > MAX_SNAPSHOT_BYTES:
        raise ReimportError("snapshot_too_large", "replacement snapshot exceeds the size limit", status=413)
    new_snapshot_sha = _valid_digest(
        replacement_snapshot_sha256, "replacement_snapshot_sha256"
    )
    if sha256_bytes(snapshot) != new_snapshot_sha:
        raise ReimportError("snapshot_hash_mismatch", "replacement snapshot hash does not match")

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        intent = _load(store, intent_id, actor_ref)
        if intent.document_id != document_id:
            raise ReimportError(
                "intent_document_mismatch",
                "reimport intent belongs to another document",
                status=409,
            )
        if intent.state == "committed" and intent.receipt is not None:
            ydoc_store.recover_compaction_locked(store, document_id=document_id)
            _cleanup_committed_stage(store, intent)
            return intent.receipt
        if intent.state != "prepared":
            raise ReimportError(
                "intent_not_committable",
                f"reimport intent is {intent.state}",
                status=409,
            )
        if _expired(intent.expires_at):
            raise ReimportError(
                "intent_expired",
                "reimport intent expired; prepare again",
                status=409,
            )
        document = documents.get_document(store, document_id)
        if documents.current_lifecycle(store, document.id) != "active":
            raise ReimportError(
                "document_retired",
                "Retired documents cannot be replaced in Co-work.",
                status=409,
            )
        _require_file_backed_source(document)
        if not document_surface_allowed(store, document):
            raise ReimportError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        state = inspect_lifecycle_state(store, document)
        if state.initialization_state != "ready" or state.structured_head_sha256 is None:
            raise ReimportError("document_not_ready", f"document is {state.initialization_state}", status=409)
        if (
            document.content_sha256 != intent.prior_projection_sha256
            or document.ydoc_snapshot_sha256 != intent.prior_snapshot_sha256
            or state.structured_head_sha256 != intent.prior_structured_head_sha256
        ):
            raise ReimportError("stale_structured_head", "Co-work document changed before reimport commit", status=409)
        if state.update_tail_present or state.unmaterialized_structured_edits:
            raise ReimportError("unmaterialized_structured_edits", "Co-work edits appeared before reimport commit", status=409)
        if state.current_file_sha256 != intent.expected_file_sha256:
            raise ReimportError("stale_file", "external Markdown changed before reimport commit", status=409, details={"current_file_sha256": state.current_file_sha256})
        staged_path = _owned_stage_path(store, intent)
        if not staged_path.is_file():
            raise ReimportError("staged_source_missing", "staged reimport source is missing", status=409)
        source = staged_path.read_bytes()
        if sha256_bytes(source) != intent.expected_file_sha256:
            raise ReimportError("staged_source_corrupt", "staged reimport source failed verification", status=409)
        replacement = ydoc_store.prepare_snapshot_replacement_locked(
            store,
            document_id=document_id,
            snapshot=snapshot,
            expected_new_snapshot_sha256=new_snapshot_sha,
            expected_current_snapshot_sha256=intent.prior_snapshot_sha256,
            expected_current_structured_head_sha256=intent.prior_structured_head_sha256,
        )
        store._store_blob_bytes(intent.expected_file_sha256, source)
        at = _now()
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_intent = _load(store, intent.id, actor_ref, conn=conn)
            if current_intent.state != "prepared":
                raise ReimportError("intent_not_committable", "reimport intent changed before commit", status=409)
            stale_ids: list[str] = []
            system = Actor("system", None)
            for proposal in proposals.open_proposals(store, document_id=document_id, conn=conn):
                proposals.expire_proposal(
                    store,
                    proposal_id=proposal.id,
                    basis_kind="rule",
                    basis_ref=f"reimport:{intent.id}",
                    actor=system,
                    at=at,
                    conn=conn,
                )
                stale_ids.append(proposal.id)
            _, version, event = documents.commit_document_version(
                store,
                document_id=document_id,
                kind="reimported",
                projection_sha256=intent.expected_file_sha256,
                ydoc_snapshot_sha256=replacement.snapshot_sha256,
                structured_head_sha256=replacement.structured_head_sha256,
                actor=actor,
                at=at,
                detail=f"explicit_replacement:{intent.id}",
                version_id=intent.document_version_id,
                conn=conn,
            )
            receipt = {
                "ok": True,
                "intent_id": intent.id,
                "document_id": document_id,
                "source_sha256": intent.expected_file_sha256,
                "snapshot_sha256": replacement.snapshot_sha256,
                "structured_head_sha256": replacement.structured_head_sha256,
                "document_version_id": version.id,
                "doc_event_id": event.id,
                "staled_proposal_ids": stale_ids,
                "reimported_at": at,
            }
            conn.execute(
                "UPDATE cowork_reimport_intents SET state = 'committed', replacement_snapshot_sha256 = ?, replacement_structured_head_sha256 = ?, updated_at = ?, committed_at = ?, receipt_json = ?, recovery_detail = NULL WHERE id = ? AND state = 'prepared'",
                (
                    replacement.snapshot_sha256,
                    replacement.structured_head_sha256,
                    at,
                    at,
                    canonical_json(receipt),
                    intent.id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            ydoc_store.abort_snapshot_replacement_locked(
                store,
                document_id=document_id,
                expected_snapshot_sha256=replacement.snapshot_sha256,
            )
            raise
        finally:
            conn.close()
        try:
            ydoc_store.finish_snapshot_replacement_locked(
                store,
                document_id=document_id,
                expected_snapshot_sha256=replacement.snapshot_sha256,
            )
        except ydoc_store.CompactionRecoveryRequired as exc:
            with store.write_transaction() as recovery_conn:
                recovery_conn.execute(
                    "UPDATE cowork_reimport_intents SET recovery_detail = 'recovery_required:snapshot_log_rotation' WHERE id = ?",
                    (intent.id,),
                )
            raise ReimportError("recovery_required", str(exc), status=409, retryable=True) from exc
        _cleanup_committed_stage(store, intent)
        store._run_on_commit()
        return receipt


def _record_recovery_detail(
    store: TruthStore,
    intent_id: str,
    detail: str,
    *,
    state: str | None = None,
) -> None:
    assignments = "recovery_detail = ?, updated_at = ?"
    params: list[Any] = [detail, _now()]
    if state is not None:
        assignments = "state = ?, " + assignments
        params.insert(0, state)
    params.append(intent_id)
    with store.write_transaction() as conn:
        conn.execute(
            f"UPDATE cowork_reimport_intents SET {assignments} WHERE id = ?",  # noqa: S608 - assignments is static above
            tuple(params),
        )


def recover_reimport_intents(store: TruthStore) -> dict[str, int]:
    """Finish interrupted replacements and expire abandoned preparations."""

    counts = {"cancelled": 0, "committed": 0, "recovery_required": 0}
    now = _now()
    with store._read_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cowork_reimport_intents WHERE "
            "(state = 'prepared' AND expires_at < ?) OR state = 'committed' "
            "ORDER BY created_at",
            (now,),
        ).fetchall()

    candidates: list[ReimportIntent] = []
    for row in rows:
        intent = _intent(row)
        if intent.state == "prepared":
            candidates.append(intent)
            continue
        try:
            staged_residue = _owned_stage_path(store, intent).exists()
        except ReimportError:
            staged_residue = True
        if (
            staged_residue
            or intent.recovery_detail is not None
            or ydoc_store.compaction_recovery_pending(
                store, document_id=intent.document_id
            )
        ):
            candidates.append(intent)

    for candidate in candidates:
        try:
            document = documents.get_document(store, candidate.document_id)
            with ydoc_store.document_lock(
                store,
                document.id,
                path_key=documents.document_path_key(document.path),
                timeout=0.01,
            ):
                intent = _load_any(store, candidate.id)
                if intent.state == "prepared" and _expired(intent.expires_at):
                    try:
                        _remove_verified_stage(store, intent)
                    except ReimportError as exc:
                        _record_recovery_detail(
                            store,
                            intent.id,
                            f"recovery_required:{exc.code}",
                            state="failed",
                        )
                        counts["recovery_required"] += 1
                        continue
                    with store.write_transaction() as conn:
                        conn.execute(
                            "UPDATE cowork_reimport_intents SET state = 'cancelled', "
                            "recovery_detail = NULL, updated_at = ? "
                            "WHERE id = ? AND state = 'prepared'",
                            (_now(), intent.id),
                        )
                    counts["cancelled"] += 1
                    continue
                if intent.state != "committed":
                    continue
                try:
                    ydoc_store.recover_compaction_locked(
                        store, document_id=document.id
                    )
                    _cleanup_committed_stage(store, intent)
                except (ReimportError, ydoc_store.CompactionRecoveryRequired) as exc:
                    _record_recovery_detail(
                        store,
                        intent.id,
                        f"recovery_required:{type(exc).__name__}",
                    )
                    counts["recovery_required"] += 1
                    continue
                if intent.recovery_detail is not None:
                    with store.write_transaction() as conn:
                        conn.execute(
                            "UPDATE cowork_reimport_intents SET recovery_detail = NULL, "
                            "updated_at = ? WHERE id = ? AND state = 'committed'",
                            (_now(), intent.id),
                        )
                counts["committed"] += 1
        except TimeoutError:
            # A live request still owns the document lock.
            continue
        except (ReimportError, InvariantViolation):
            counts["recovery_required"] += 1
    return counts


def cancel_reimport(
    store: TruthStore,
    *,
    document_id: str,
    intent_id: str,
    actor: Actor,
) -> dict[str, Any]:
    actor_ref = _actor_ref(actor)
    intent = _load(store, intent_id, actor_ref)
    if intent.document_id != document_id:
        raise ReimportError(
            "intent_document_mismatch",
            "This replacement belongs to another document.",
            status=409,
        )
    if intent.state == "cancelled":
        return {"ok": True, "intent_id": intent.id, "state": "cancelled"}
    if intent.state == "committed":
        raise ReimportError(
            "intent_already_committed",
            "A completed replacement cannot be cancelled.",
            status=409,
        )
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_reimport_intents SET state = 'cancelled', updated_at = ? WHERE id = ? AND state = 'prepared'",
            (_now(), intent.id),
        )
    _remove_verified_stage(store, intent)
    return {"ok": True, "intent_id": intent.id, "state": "cancelled"}


__all__ = [
    "ReimportError",
    "ReimportIntent",
    "cancel_reimport",
    "commit_reimport",
    "prepare_reimport",
    "read_reimport_source",
    "recover_reimport_intents",
]
