"""External-editor-safe Markdown projection publication for Co-work."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.cowork.paths import CoworkPathError, resolve_writeback_target
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.cowork.readiness import classify_document
from work_buddy.truth import documents, proposals, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes
from work_buddy.truth.store import TruthStore, _valid_digest


MAX_RENDERED_BYTES = 16 * 1024 * 1024


class MaterializationError(InvariantViolation):
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
class MaterializationIntent:
    id: str
    idempotency_key: str | None
    actor_ref: str
    document_id: str
    state: str
    expected_file_sha256: str
    expected_structured_head_sha256: str
    snapshot_sha256: str
    rendered_sha256: str
    staged_path: str | None
    quarantine_path: str | None
    document_version_id: str
    created_at: str
    updated_at: str
    committed_at: str | None
    receipt_json: str | None
    recovery_detail: str | None

    @property
    def receipt(self) -> dict[str, Any] | None:
        if not self.receipt_json:
            return None
        value = json.loads(self.receipt_json)
        return value if isinstance(value, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _actor_ref(actor: Actor) -> str:
    if actor.kind != "human" or not actor.ref:
        raise MaterializationError(
            "actor_forbidden", "Save requires a human actor", status=403
        )
    return actor.ref


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise MaterializationError(
            "file_unavailable", "Markdown file cannot be read", status=409, retryable=True
        ) from exc


def _intent_from_row(row: Any) -> MaterializationIntent:
    return MaterializationIntent(**dict(row))


def _exclusive_publish(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise MaterializationError(
            "external_write_race",
            "another process created the Markdown file during Save",
            status=409,
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if path.is_file() and _hash_file(path) == sha256_bytes(payload):
            path.unlink(missing_ok=True)
        raise


def _paths(target: Path, intent_id: str) -> tuple[Path, Path]:
    prefix = f".{target.name}.wbuddy-{intent_id}"
    return target.with_name(prefix + ".new"), target.with_name(prefix + ".previous")


def _set_failed(
    store: TruthStore,
    intent_id: str,
    *,
    detail: str,
    recovery_required: bool,
) -> None:
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_materialization_intents SET state = 'failed', "
            "updated_at = ?, recovery_detail = ? WHERE id = ?",
            (
                _now(),
                "recovery_required:" + detail if recovery_required else detail,
                intent_id,
            ),
        )


def _restore_previous(
    *,
    target: Path,
    quarantine: Path,
    rendered_sha256: str,
    expected_file_sha256: str,
) -> bool:
    """Restore only exact known bytes; return False when both must be retained."""

    target_hash = _hash_file(target)
    quarantine_hash = _hash_file(quarantine)
    if quarantine_hash != expected_file_sha256:
        return False
    if target_hash is None:
        os.replace(quarantine, target)
        return True
    if target_hash == rendered_sha256:
        target.unlink()
        os.replace(quarantine, target)
        return True
    return False


def _load_intent(
    store: TruthStore,
    intent_id: str,
) -> MaterializationIntent:
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM cowork_materialization_intents WHERE id = ?",
            (intent_id,),
        ).fetchone()
    if row is None:
        raise MaterializationError(
            "save_not_found", "Save intent does not exist", status=404
        )
    return _intent_from_row(row)


def _cleanup_committed_locked(
    store: TruthStore,
    intent: MaterializationIntent,
    *,
    target: Path,
) -> dict[str, Any]:
    receipt = intent.receipt
    if receipt is None or _hash_file(target) != intent.rendered_sha256:
        raise MaterializationError(
            "recovery_required",
            "The completed Save needs manual recovery.",
            status=409,
            retryable=True,
            details={"intent_id": intent.id},
        )
    ydoc_store.recover_compaction_locked(
        store, document_id=intent.document_id
    )
    staged = Path(intent.staged_path) if intent.staged_path else None
    quarantine = Path(intent.quarantine_path) if intent.quarantine_path else None
    if staged is not None and staged.exists():
        if _hash_file(staged) != intent.rendered_sha256:
            raise MaterializationError(
                "recovery_required",
                "The completed Save has an unsafe staged file.",
                status=409,
                retryable=True,
                details={"intent_id": intent.id},
            )
        staged.unlink()
    if quarantine is not None and quarantine.exists():
        if _hash_file(quarantine) != intent.expected_file_sha256:
            raise MaterializationError(
                "recovery_required",
                "The completed Save has an unsafe captured file.",
                status=409,
                retryable=True,
                details={"intent_id": intent.id, "quarantine_path": str(quarantine)},
            )
        quarantine.unlink()
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_materialization_intents SET staged_path = NULL, "
            "quarantine_path = NULL, recovery_detail = NULL, updated_at = ? "
            "WHERE id = ? AND state = 'committed'",
            (_now(), intent.id),
        )
    return receipt


def _recover_intent_locked(
    store: TruthStore,
    intent: MaterializationIntent,
    *,
    target: Path,
) -> MaterializationIntent:
    """Recover one Save while its path/document operation lock is held."""

    intent = _load_intent(store, intent.id)
    if intent.state == "committed":
        _cleanup_committed_locked(store, intent, target=target)
        return _load_intent(store, intent.id)
    if intent.state not in {"prepared", "publishing", "failed"}:
        return intent

    ydoc_store.recover_compaction_locked(
        store, document_id=intent.document_id
    )
    staged = Path(intent.staged_path) if intent.staged_path else None
    quarantine = Path(intent.quarantine_path) if intent.quarantine_path else None
    staged_hash = _hash_file(staged) if staged is not None else None
    quarantine_hash = _hash_file(quarantine) if quarantine is not None else None
    target_hash = _hash_file(target)
    safe = staged_hash in {None, intent.rendered_sha256}
    if quarantine_hash == intent.expected_file_sha256 and quarantine is not None:
        safe = safe and _restore_previous(
            target=target,
            quarantine=quarantine,
            rendered_sha256=intent.rendered_sha256,
            expected_file_sha256=intent.expected_file_sha256,
        )
    elif quarantine_hash is None:
        safe = safe and target_hash == intent.expected_file_sha256
    else:
        safe = False
    if safe and staged is not None:
        staged.unlink(missing_ok=True)
    now = _now()
    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE cowork_materialization_intents SET state = ?, updated_at = ?, "
            "recovery_detail = ? WHERE id = ? AND state IN "
            "('prepared', 'publishing', 'failed')",
            (
                "prepared" if safe else "failed",
                now,
                "retry_safe" if safe else "recovery_required:ambiguous_files",
                intent.id,
            ),
        )
    return _load_intent(store, intent.id)


def recover_materialization_intent_locked(
    store: TruthStore,
    intent_id: str,
) -> MaterializationIntent:
    initial = _load_intent(store, intent_id)
    document = documents.get_document(store, initial.document_id)
    resolved = resolve_writeback_target(store, document)
    return _recover_intent_locked(
        store, initial, target=resolved.path
    )


def recover_materialization_intent(
    store: TruthStore,
    intent_id: str,
) -> MaterializationIntent:
    initial = _load_intent(store, intent_id)
    document = documents.get_document(store, initial.document_id)
    with ydoc_store.document_lock(
        store,
        document.id,
        path_key=documents.document_path_key(document.path),
    ):
        return recover_materialization_intent_locked(
            store, intent_id
        )


def commit_managed_projection(
    store: TruthStore,
    *,
    document_id: str,
    rendered_markdown: str,
    rendered_sha256: str,
    expected_structured_head_sha256: str,
    snapshot_sha256: str,
    actor: Actor,
    replacement_snapshot: bytes,
    replacement_snapshot_sha256: str,
    version_kind: str = "materialized",
    version_detail: str | None = None,
    commit_callback: Callable[
        [sqlite3.Connection, Mapping[str, Any]], Mapping[str, Any] | None
    ]
    | None = None,
    lock_preflight: Callable[[], Mapping[str, Any] | None] | None = None,
    resolving_flag_proposal_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Advance an imported document without writing back to its source file.

    From file creates a durable structured Co-work document from an acquisition
    source whose authorship and human-review state are attested separately.
    Review decisions still need an atomic projection + snapshot commit, but
    that projection remains an internal content-addressed version. The selected
    source file is never a publication target.
    """

    _actor_ref(actor)
    if not isinstance(rendered_markdown, str):
        raise MaterializationError("invalid_markdown", "rendered_markdown must be text")
    rendered = rendered_markdown.encode("utf-8")
    if len(rendered) > MAX_RENDERED_BYTES:
        raise MaterializationError(
            "rendered_too_large",
            "rendered Markdown exceeds the size limit",
            status=413,
        )
    rendered_digest = _valid_digest(rendered_sha256, "rendered_sha256")
    expected_head = _valid_digest(
        expected_structured_head_sha256, "expected_structured_head_sha256"
    )
    expected_snapshot = _valid_digest(snapshot_sha256, "snapshot_sha256")
    replacement_digest = _valid_digest(
        replacement_snapshot_sha256, "replacement_snapshot_sha256"
    )
    if sha256_bytes(rendered) != rendered_digest:
        raise MaterializationError(
            "rendered_hash_mismatch", "rendered Markdown hash does not match"
        )

    initial = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial.path),
    ):
        # A previous managed projection may have stopped after staging its
        # replacement marker. Finish a committed pointer or discard an
        # uncommitted marker before any idempotency/preflight decision.
        ydoc_store.recover_compaction_locked(
            store,
            document_id=document_id,
        )
        if lock_preflight is not None:
            prior_receipt = lock_preflight()
            if prior_receipt is not None:
                return dict(prior_receipt)
        document = documents.get_document(store, document_id)
        if not documents.source_is_detached(document):
            raise MaterializationError(
                "managed_projection_forbidden",
                "Only source-detached imports use managed projection commits.",
                status=409,
            )
        if documents.current_lifecycle(store, document.id) != "active":
            raise MaterializationError(
                "document_retired",
                "Retired documents cannot be changed in Co-work.",
                status=409,
            )
        if not document_surface_allowed(store, document):
            raise MaterializationError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        readiness = classify_document(store, document)
        if readiness.initialization_state != "ready":
            raise MaterializationError(
                "document_not_ready",
                f"document is {readiness.initialization_state}",
                status=409,
            )
        if document.ydoc_snapshot_sha256 != expected_snapshot:
            raise MaterializationError(
                "snapshot_mismatch",
                "Y.Doc snapshot changed before review commit",
                status=409,
                details={"server_snapshot_sha256": document.ydoc_snapshot_sha256},
            )
        # Review applies a complete replacement snapshot. Keep the durable
        # update tail in the CAS calculation: prepare_snapshot_replacement_locked
        # will only rotate it after proving this exact live head is still current.
        live_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=expected_snapshot,
        )
        if live_head != expected_head:
            raise MaterializationError(
                "stale_structured_head",
                "structured document changed before review commit",
                status=409,
                details={"server_structured_head_sha256": live_head},
            )
        if store.profile.gate.block_materialize_on_flags:
            resolving_flags = frozenset(resolving_flag_proposal_ids)
            flags = [
                item
                for item in proposals.open_proposals(
                    store, document_id=document.id
                )
                if item.replacement is None and item.id not in resolving_flags
            ]
            if flags:
                raise MaterializationError(
                    "open_flags_block_save",
                    "Resolve open review flags before applying changes",
                    status=409,
                    details={"open_flag_count": len(flags)},
                )

        # Content-addressed projection storage may safely precede the pointer
        # transaction; an orphan is harmless and later refcount cleanup owns it.
        store._store_blob_bytes(rendered_digest, rendered)
        replacement = ydoc_store.prepare_snapshot_replacement_locked(
            store,
            document_id=document.id,
            snapshot=replacement_snapshot,
            expected_new_snapshot_sha256=replacement_digest,
            expected_current_snapshot_sha256=expected_snapshot,
            expected_current_structured_head_sha256=live_head,
            projection_sha256=rendered_digest,
        )
        version_id = new_id()
        receipt = {
            "ok": True,
            "materialization_intent_id": None,
            # Compatibility name consumed by the sitting client. For a
            # source-detached import this is an internal projection digest,
            # never a hash of bytes written to the selected source.
            "new_file_sha256": rendered_digest,
            "structured_head_sha256": replacement.structured_head_sha256,
            "snapshot_sha256": replacement.snapshot_sha256,
            "document_version_id": version_id,
            "materialized_at": _now(),
            "drift_state": "clean",
            "source_writeback": documents.SOURCE_WRITEBACK_NEVER,
        }
        conn = store.connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            if commit_callback is not None:
                additions = commit_callback(conn, receipt)
                if additions:
                    receipt.update(dict(additions))
            documents.commit_document_version(
                store,
                document_id=document.id,
                kind=version_kind,
                projection_sha256=rendered_digest,
                ydoc_snapshot_sha256=replacement.snapshot_sha256,
                structured_head_sha256=replacement.structured_head_sha256,
                actor=actor,
                at=receipt["materialized_at"],
                detail=version_detail,
                version_id=version_id,
                conn=conn,
            )
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            ydoc_store.abort_snapshot_replacement_locked(
                store,
                document_id=document.id,
                expected_snapshot_sha256=replacement.snapshot_sha256,
            )
            raise
        finally:
            conn.close()
        if committed:
            try:
                ydoc_store.finish_snapshot_replacement_locked(
                    store,
                    document_id=document.id,
                    expected_snapshot_sha256=replacement.snapshot_sha256,
                )
            except Exception as exc:
                raise MaterializationError(
                    "recovery_required",
                    "The structured change committed but its update log requires recovery.",
                    status=409,
                    retryable=True,
                ) from exc
            store._run_on_commit()
        return receipt


def publish_projection(
    store: TruthStore,
    *,
    document_id: str,
    rendered_markdown: str,
    rendered_sha256: str,
    expected_file_sha256: str,
    expected_structured_head_sha256: str,
    snapshot_sha256: str,
    actor: Actor,
    idempotency_key: str | None = None,
    replacement_snapshot: bytes | None = None,
    replacement_snapshot_sha256: str | None = None,
    version_kind: str = "materialized",
    version_detail: str | None = None,
    commit_callback: Callable[
        [sqlite3.Connection, Mapping[str, Any]], Mapping[str, Any] | None
    ]
    | None = None,
    lock_preflight: Callable[[], Mapping[str, Any] | None] | None = None,
    resolving_flag_proposal_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Publish Markdown iff file and structured heads still match.

    The old file is captured in a same-directory quarantine before the new file
    is created. Any unexplained byte change leaves both byte sets intact and
    returns recovery_required rather than overwriting either one.
    """

    actor_ref = _actor_ref(actor)
    if not isinstance(rendered_markdown, str):
        raise MaterializationError("invalid_markdown", "rendered_markdown must be text")
    rendered = rendered_markdown.encode("utf-8")
    if len(rendered) > MAX_RENDERED_BYTES:
        raise MaterializationError("rendered_too_large", "rendered Markdown exceeds the size limit", status=413)
    rendered_digest = _valid_digest(rendered_sha256, "rendered_sha256")
    expected_file = _valid_digest(expected_file_sha256, "expected_file_sha256")
    expected_head = _valid_digest(
        expected_structured_head_sha256, "expected_structured_head_sha256"
    )
    expected_snapshot = _valid_digest(snapshot_sha256, "snapshot_sha256")
    if sha256_bytes(rendered) != rendered_digest:
        raise MaterializationError(
            "rendered_hash_mismatch", "rendered Markdown hash does not match"
        )
    if (replacement_snapshot is None) != (replacement_snapshot_sha256 is None):
        raise MaterializationError(
            "invalid_snapshot_replacement",
            "replacement snapshot bytes and hash must be supplied together",
        )
    key = None if idempotency_key is None else str(idempotency_key).strip()
    if key == "":
        key = None
    if key is not None and len(key) > 128:
        raise MaterializationError("invalid_idempotency_key", "idempotency key is too long")

    prior: MaterializationIntent | None = None
    if key is not None:
        with store._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM cowork_materialization_intents "
                "WHERE actor_ref = ? AND idempotency_key = ?",
                (actor_ref, key),
            ).fetchone()
        if row is not None:
            prior = _intent_from_row(row)
            same = (
                prior.document_id == document_id
                and prior.expected_file_sha256 == expected_file
                and prior.expected_structured_head_sha256 == expected_head
                and prior.snapshot_sha256 == expected_snapshot
                and prior.rendered_sha256 == rendered_digest
            )
            if not same:
                raise MaterializationError(
                    "idempotency_conflict",
                    "idempotency key was used for a different Save",
                    status=409,
                )

    initial_document = documents.get_document(store, document_id)
    with ydoc_store.document_lock(
        store,
        document_id,
        path_key=documents.document_path_key(initial_document.path),
    ):
        reusable_intent: MaterializationIntent | None = None
        if prior is not None:
            try:
                prior_target = resolve_writeback_target(
                    store,
                    initial_document,
                ).path
            except CoworkPathError as exc:
                raise MaterializationError(
                    "invalid_path", str(exc), status=409
                ) from exc
            recovered = _recover_intent_locked(
                store, prior, target=prior_target
            )
            if recovered.state == "committed" and recovered.receipt is not None:
                return recovered.receipt
            if (
                recovered.state == "prepared"
                and recovered.recovery_detail == "retry_safe"
            ):
                reusable_intent = recovered
            else:
                raise MaterializationError(
                    "recovery_required",
                    "An earlier Save with this key needs recovery.",
                    status=409,
                    retryable=True,
                    details={
                        "intent_id": recovered.id,
                        "state": recovered.state,
                        "recovery_detail": recovered.recovery_detail,
                    },
                )
        if lock_preflight is not None:
            prior_receipt = lock_preflight()
            if prior_receipt is not None:
                return dict(prior_receipt)
        document = documents.get_document(store, document_id)
        if documents.source_is_detached(document):
            raise MaterializationError(
                "source_writeback_forbidden",
                "This document was created with From file. Co-work will not change its source file.",
                status=409,
            )
        if documents.current_lifecycle(store, document.id) != "active":
            raise MaterializationError(
                "document_retired",
                "Retired documents cannot be saved in Co-work.",
                status=409,
            )
        if not document_surface_allowed(store, document):
            raise MaterializationError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                status=403,
            )
        readiness = classify_document(store, document)
        if readiness.initialization_state != "ready":
            raise MaterializationError(
                "document_not_ready",
                f"document is {readiness.initialization_state}",
                status=409,
            )
        if document.ydoc_snapshot_sha256 != expected_snapshot:
            raise MaterializationError(
                "snapshot_mismatch",
                "Y.Doc snapshot changed before Save",
                status=409,
                details={"server_snapshot_sha256": document.ydoc_snapshot_sha256},
            )
        # An ordinary Save cannot publish while opaque updates remain outside
        # its snapshot. A sitting supplies a complete replacement snapshot,
        # however, and prepare_snapshot_replacement_locked CAS-binds that
        # replacement to the exact live structured head before rotating the
        # durable update tail.
        if (
            replacement_snapshot is None
            and ydoc_store.update_tail_present(store, document_id=document.id)
        ):
            raise MaterializationError(
                "update_tail_present",
                "compact pending structured edits before Save",
                status=409,
            )
        live_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=expected_snapshot,
        )
        if live_head != expected_head:
            raise MaterializationError(
                "stale_structured_head",
                "structured document changed before Save",
                status=409,
                details={"server_structured_head_sha256": live_head},
            )
        committed_snapshot = expected_snapshot
        committed_head = live_head
        replacement_prepared = False
        if replacement_snapshot is not None:
            committed_snapshot = ydoc_store.write_snapshot(
                store,
                snapshot=replacement_snapshot,
                expected_sha256=replacement_snapshot_sha256,
            )
            committed_head = ydoc_store.structured_head_from_segments(
                replacement_snapshot, ()
            )
        if store.profile.gate.block_materialize_on_flags:
            resolving_flags = frozenset(resolving_flag_proposal_ids)
            flags = [
                item
                for item in proposals.open_proposals(store, document_id=document.id)
                if item.replacement is None and item.id not in resolving_flags
            ]
            if flags:
                raise MaterializationError(
                    "open_flags_block_save",
                    "Resolve open review flags before Save",
                    status=409,
                    details={"open_flag_count": len(flags)},
                )

        try:
            resolved = resolve_writeback_target(store, document)
        except CoworkPathError as exc:
            raise MaterializationError("invalid_path", str(exc), status=409) from exc
        current_hash = _hash_file(resolved.path)
        if current_hash is None:
            raise MaterializationError("missing_file", "Markdown file is missing", status=409)
        if current_hash != expected_file:
            raise MaterializationError(
                "stale_file",
                "Markdown file changed outside Co-work",
                status=409,
                details={"current_file_sha256": current_hash},
            )

        intent_id = reusable_intent.id if reusable_intent is not None else new_id()
        version_id = (
            reusable_intent.document_version_id
            if reusable_intent is not None
            else new_id()
        )
        staged, quarantine = _paths(resolved.path, intent_id)
        now = _now()
        with store.write_transaction() as conn:
            if reusable_intent is None:
                conn.execute(
                    "INSERT INTO cowork_materialization_intents ("
                    "id, idempotency_key, actor_ref, document_id, state, "
                    "expected_file_sha256, expected_structured_head_sha256, "
                    "snapshot_sha256, rendered_sha256, staged_path, quarantine_path, "
                    "document_version_id, created_at, updated_at, committed_at, "
                    "receipt_json, recovery_detail) "
                    "VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "NULL, NULL, NULL)",
                    (
                        intent_id,
                        key,
                        actor_ref,
                        document.id,
                        expected_file,
                        expected_head,
                        expected_snapshot,
                        rendered_digest,
                        str(staged),
                        str(quarantine),
                        version_id,
                        now,
                        now,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE cowork_materialization_intents SET state = 'prepared', "
                    "staged_path = ?, quarantine_path = ?, updated_at = ?, "
                    "committed_at = NULL, receipt_json = NULL, recovery_detail = NULL "
                    "WHERE id = ? AND state = 'prepared' "
                    "AND recovery_detail = 'retry_safe'",
                    (str(staged), str(quarantine), now, intent_id),
                )
                if cursor.rowcount != 1:
                    raise MaterializationError(
                        "save_state_conflict",
                        "Save recovery changed before retry.",
                        status=409,
                        retryable=True,
                    )
        atomic_write_bytes(staged, rendered)
        if _hash_file(staged) != rendered_digest:
            _set_failed(store, intent_id, detail="staged_bytes_corrupt", recovery_required=False)
            raise MaterializationError("staging_failed", "staged Markdown failed verification", status=500)

        # Re-resolve under the operation lock immediately before publication.
        resolved = resolve_writeback_target(store, document)
        with store.write_transaction() as conn:
            conn.execute(
                "UPDATE cowork_materialization_intents SET state = 'publishing', "
                "updated_at = ? WHERE id = ? AND state = 'prepared'",
                (_now(), intent_id),
            )
        try:
            os.replace(resolved.path, quarantine)
        except OSError as exc:
            staged.unlink(missing_ok=True)
            _set_failed(store, intent_id, detail="quarantine_failed", recovery_required=False)
            raise MaterializationError(
                "quarantine_failed",
                "Markdown file could not be captured safely",
                status=409,
                retryable=True,
            ) from exc

        captured = _hash_file(quarantine)
        if captured != expected_file:
            restored = _restore_previous(
                target=resolved.path,
                quarantine=quarantine,
                rendered_sha256=rendered_digest,
                expected_file_sha256=expected_file,
            )
            staged.unlink(missing_ok=True)
            _set_failed(
                store,
                intent_id,
                detail="captured_file_changed",
                recovery_required=not restored,
            )
            raise MaterializationError(
                "stale_file" if restored else "recovery_required",
                "Markdown file changed during Save",
                status=409,
                details={"captured_file_sha256": captured},
            )

        try:
            _exclusive_publish(resolved.path, staged.read_bytes())
        except Exception:
            restored = _restore_previous(
                target=resolved.path,
                quarantine=quarantine,
                rendered_sha256=rendered_digest,
                expected_file_sha256=expected_file,
            )
            staged.unlink(missing_ok=True)
            _set_failed(
                store,
                intent_id,
                detail="publish_conflict",
                recovery_required=not restored,
            )
            raise
        staged.unlink(missing_ok=True)

        if _hash_file(quarantine) != expected_file:
            _set_failed(store, intent_id, detail="quarantine_changed", recovery_required=True)
            raise MaterializationError(
                "recovery_required",
                "captured external bytes changed; both versions were retained",
                status=409,
                details={"quarantine_path": str(quarantine)},
            )
        if _hash_file(resolved.path) != rendered_digest:
            _set_failed(store, intent_id, detail="published_target_changed", recovery_required=True)
            raise MaterializationError(
                "recovery_required",
                "published Markdown changed before commit; both versions were retained",
                status=409,
                details={"quarantine_path": str(quarantine)},
            )

        if replacement_snapshot is not None:
            # Snapshot replacement rotates the Y.Doc cursor state. Persist the
            # exact rendered projection before staging that rotation so its
            # recovery marker can carry a receipt for the replacement state.
            # An orphaned content-addressed blob is harmless on rollback.
            store._store_blob_bytes(rendered_digest, rendered)
            try:
                replacement = ydoc_store.prepare_snapshot_replacement_locked(
                    store,
                    document_id=document.id,
                    snapshot=replacement_snapshot,
                    expected_new_snapshot_sha256=committed_snapshot,
                    expected_current_snapshot_sha256=expected_snapshot,
                    expected_current_structured_head_sha256=live_head,
                    projection_sha256=rendered_digest,
                )
                replacement_prepared = True
                if (
                    replacement.snapshot_sha256 != committed_snapshot
                    or replacement.structured_head_sha256 != committed_head
                ):
                    raise MaterializationError(
                        "snapshot_replacement_mismatch",
                        "prepared replacement snapshot changed unexpectedly",
                        status=409,
                    )
            except Exception:
                if replacement_prepared:
                    ydoc_store.abort_snapshot_replacement_locked(
                        store,
                        document_id=document.id,
                        expected_snapshot_sha256=committed_snapshot,
                    )
                restored = _restore_previous(
                    target=resolved.path,
                    quarantine=quarantine,
                    rendered_sha256=rendered_digest,
                    expected_file_sha256=expected_file,
                )
                _set_failed(
                    store,
                    intent_id,
                    detail="snapshot_prepare_failed",
                    recovery_required=not restored,
                )
                raise

        if replacement_snapshot is None:
            store._store_blob_bytes(rendered_digest, rendered)
        receipt = {
            "ok": True,
            "materialization_intent_id": intent_id,
            "new_file_sha256": rendered_digest,
            "structured_head_sha256": committed_head,
            "snapshot_sha256": committed_snapshot,
            "document_version_id": version_id,
            "materialized_at": _now(),
            "drift_state": "clean",
        }
        conn = store.connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            if commit_callback is not None:
                additions = commit_callback(conn, receipt)
                if additions:
                    receipt.update(dict(additions))
            documents.commit_document_version(
                store,
                document_id=document.id,
                kind=version_kind,
                projection_sha256=rendered_digest,
                ydoc_snapshot_sha256=committed_snapshot,
                structured_head_sha256=committed_head,
                actor=actor,
                at=receipt["materialized_at"],
                detail=version_detail,
                version_id=version_id,
                conn=conn,
            )
            conn.execute(
                "UPDATE cowork_materialization_intents SET state = 'committed', "
                "updated_at = ?, committed_at = ?, receipt_json = ?, "
                "recovery_detail = NULL WHERE id = ? AND state = 'publishing'",
                (
                    receipt["materialized_at"],
                    receipt["materialized_at"],
                    canonical_json(receipt),
                    intent_id,
                ),
            )
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if replacement_prepared:
                ydoc_store.abort_snapshot_replacement_locked(
                    store,
                    document_id=document.id,
                    expected_snapshot_sha256=committed_snapshot,
                )
            restored = _restore_previous(
                target=resolved.path,
                quarantine=quarantine,
                rendered_sha256=rendered_digest,
                expected_file_sha256=expected_file,
            )
            _set_failed(
                store,
                intent_id,
                detail="database_commit_failed",
                recovery_required=not restored,
            )
            raise
        finally:
            conn.close()
        if committed:
            if replacement_prepared:
                try:
                    ydoc_store.finish_snapshot_replacement_locked(
                        store,
                        document_id=document.id,
                        expected_snapshot_sha256=committed_snapshot,
                    )
                except Exception as exc:
                    with store.write_transaction() as recovery_conn:
                        recovery_conn.execute(
                            "UPDATE cowork_materialization_intents SET "
                            "recovery_detail = 'recovery_required:snapshot_log_rotation' "
                            "WHERE id = ?",
                            (intent_id,),
                        )
                    raise MaterializationError(
                        "recovery_required",
                        "structured snapshot committed but its update log requires recovery",
                        status=409,
                        retryable=True,
                        details={"intent_id": intent_id},
                    ) from exc
            if _hash_file(quarantine) == expected_file:
                quarantine.unlink(missing_ok=True)
                with store.write_transaction() as cleanup_conn:
                    cleanup_conn.execute(
                        "UPDATE cowork_materialization_intents SET staged_path = NULL, "
                        "quarantine_path = NULL, recovery_detail = NULL, updated_at = ? "
                        "WHERE id = ? AND state = 'committed'",
                        (_now(), intent_id),
                    )
            else:
                with store.write_transaction() as recovery_conn:
                    recovery_conn.execute(
                        "UPDATE cowork_materialization_intents SET "
                        "recovery_detail = 'recovery_required:quarantine_changed_after_commit' "
                        "WHERE id = ?",
                        (intent_id,),
                    )
            store._run_on_commit()
        return receipt


def recover_materializations(store: TruthStore) -> dict[str, int]:
    """Recover abandoned Saves under the same lock used to publish them."""

    counts = {"restored": 0, "committed": 0, "recovery_required": 0}
    with store._read_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM cowork_materialization_intents WHERE state IN "
            "('prepared', 'publishing', 'failed') OR "
            "(state = 'committed' AND (staged_path IS NOT NULL OR "
            "quarantine_path IS NOT NULL OR recovery_detail IS NOT NULL)) "
            "ORDER BY created_at"
        ).fetchall()
    for raw in rows:
        candidate = _intent_from_row(raw)
        document = documents.get_document(store, candidate.document_id)
        try:
            with ydoc_store.document_lock(
                store,
                document.id,
                path_key=documents.document_path_key(document.path),
                timeout=0.01,
            ):
                resolved = resolve_writeback_target(store, document)
                recovered = _recover_intent_locked(
                    store,
                    _load_intent(store, candidate.id),
                    target=resolved.path,
                )
                if recovered.state == "committed":
                    counts["committed"] += 1
                elif (
                    recovered.state == "prepared"
                    and recovered.recovery_detail == "retry_safe"
                ):
                    counts["restored"] += 1
                else:
                    counts["recovery_required"] += 1
        except TimeoutError:
            # A live publisher still owns the operation lock.
            continue
        except MaterializationError:
            counts["recovery_required"] += 1
    return counts


__all__ = [
    "MAX_RENDERED_BYTES",
    "MaterializationError",
    "MaterializationIntent",
    "commit_managed_projection",
    "publish_projection",
    "recover_materialization_intent",
    "recover_materialization_intent_locked",
    "recover_materializations",
]
