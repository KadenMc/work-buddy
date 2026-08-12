"""Crash-safe readable-content scrub for exact managed Co-work documents.

The immutable document/version/action identities and their SHA-256 digests are
audit history.  A sanctioned exact-copy scrub destroys only readable SQL/blob
payloads, records every covered field, and leaves a durable completion receipt
that a Sources outbox consumer can require before releasing its usage.

Mixed or semantic derivatives are deliberately not admitted by this API.  They
must follow the policy-review/invalidation path owned by their source usage.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from work_buddy.truth import ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.documents import retained_file_import_source_sha256
from work_buddy.truth.identity import canonical_json, sha256_text
from work_buddy.truth.migrations import (
    REDACTED_ACTION_CONTEXT_JSON,
    REDACTED_SELECTOR_JSON,
)
from work_buddy.truth.store import (
    PostCommitHookError,
    TruthStore,
    _record_id,
    _timestamp,
    _valid_record_id,
)


EXACT_COPY_CONTENT_CLASS = "exact_copy"
SCRUB_REDACTION_POLICY = "scrub"


@dataclass(frozen=True, slots=True)
class DocumentContentRedactionReceipt:
    id: str
    document_id: str
    replacement_document_version_id: str
    source_usage_id: str
    source_ref_json: str
    source_redaction_event_id: str
    content_class: str
    redaction_policy: str
    actor_ref_json: str
    coverage_sha256: str
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DocumentContentRedactionTarget:
    id: str
    redaction_id: str
    target_kind: str
    target_ref: str
    field_name: str
    content_sha256: str | None
    disposition: str
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DocumentContentRedactionStatusEvent:
    id: str
    redaction_id: str
    status: str
    detail_json: str
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DocumentContentRedactionResult:
    receipt: DocumentContentRedactionReceipt
    targets: tuple[DocumentContentRedactionTarget, ...]
    status: DocumentContentRedactionStatusEvent
    complete: bool
    deleted_blob_sha256s: tuple[str, ...] = ()
    shared_blob_sha256s: tuple[str, ...] = ()
    review_target_refs: tuple[str, ...] = ()


def _required_text(value: object, label: str, *, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantViolation(f"{label} must be nonempty text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise InvariantViolation(f"{label} is too long")
    return normalized


def _canonical_object(value: Mapping[str, Any] | str, label: str) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvariantViolation(f"{label} must contain canonical JSON") from exc
    else:
        decoded = dict(value)
    if not isinstance(decoded, dict) or not decoded:
        raise InvariantViolation(f"{label} must contain a nonempty object")
    return canonical_json(decoded)


def _stable_id(domain: str, payload: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json({"domain": domain, **dict(payload)}))[:32]


def _target(
    *,
    redaction_id: str,
    target_kind: str,
    target_ref: str,
    field_name: str,
    content_sha256: str | None,
    disposition: str,
    created_at: str,
) -> DocumentContentRedactionTarget:
    payload = {
        "schema": "wb.document-content-redaction-target/v1",
        "redaction_id": redaction_id,
        "target_kind": target_kind,
        "target_ref": target_ref,
        "field_name": field_name,
        "content_sha256": content_sha256,
        "disposition": disposition,
        "created_at": created_at,
    }
    canonical = sha256_text(canonical_json(payload))
    return DocumentContentRedactionTarget(
        id=_stable_id("wb.document-content-redaction-target-id/v1", payload),
        redaction_id=redaction_id,
        target_kind=target_kind,
        target_ref=target_ref,
        field_name=field_name,
        content_sha256=content_sha256,
        disposition=disposition,
        canonical_sha256=canonical,
        created_at=created_at,
    )


def _receipt_from_row(row: sqlite3.Row) -> DocumentContentRedactionReceipt:
    return DocumentContentRedactionReceipt(**dict(row))


def _target_from_row(row: sqlite3.Row) -> DocumentContentRedactionTarget:
    return DocumentContentRedactionTarget(**dict(row))


def _status_from_row(row: sqlite3.Row) -> DocumentContentRedactionStatusEvent:
    return DocumentContentRedactionStatusEvent(**dict(row))


def _targets_locked(
    conn: sqlite3.Connection, redaction_id: str
) -> tuple[DocumentContentRedactionTarget, ...]:
    return tuple(
        _target_from_row(row)
        for row in conn.execute(
            "SELECT * FROM document_content_redaction_targets "
            "WHERE redaction_id = ? ORDER BY target_kind, target_ref, field_name, id",
            (redaction_id,),
        ).fetchall()
    )


def _latest_status_locked(
    conn: sqlite3.Connection, redaction_id: str
) -> DocumentContentRedactionStatusEvent:
    row = conn.execute(
        "SELECT s.* FROM document_content_redaction_status_events AS s "
        "JOIN ledger_records AS l "
        "ON l.record_type = 'document_content_redaction_status' "
        "AND l.record_key = s.id WHERE s.redaction_id = ? "
        "ORDER BY l.seq DESC LIMIT 1",
        (redaction_id,),
    ).fetchone()
    if row is None:
        raise InvariantViolation("document content redaction has no status event")
    return _status_from_row(row)


def _insert_target_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    target: DocumentContentRedactionTarget,
) -> None:
    conn.execute(
        "INSERT INTO document_content_redaction_targets "
        "(id, redaction_id, target_kind, target_ref, field_name, content_sha256, "
        "disposition, canonical_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target.id,
            target.redaction_id,
            target.target_kind,
            target.target_ref,
            target.field_name,
            target.content_sha256,
            target.disposition,
            target.canonical_sha256,
            target.created_at,
        ),
    )
    store._insert_ledger_record_locked(
        conn, "document_content_redaction_target", target.id
    )


def _append_status_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    redaction_id: str,
    status: str,
    detail: Mapping[str, Any],
    at: str,
) -> DocumentContentRedactionStatusEvent:
    detail_json = canonical_json(dict(detail))
    payload = {
        "schema": "wb.document-content-redaction-status/v1",
        "redaction_id": redaction_id,
        "status": status,
        "detail": dict(detail),
        "created_at": at,
    }
    canonical = sha256_text(canonical_json(payload))
    event = DocumentContentRedactionStatusEvent(
        id=_record_id(None, "document content redaction status id"),
        redaction_id=redaction_id,
        status=status,
        detail_json=detail_json,
        canonical_sha256=canonical,
        created_at=at,
    )
    conn.execute(
        "INSERT INTO document_content_redaction_status_events "
        "(id, redaction_id, status, detail_json, canonical_sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            event.id,
            event.redaction_id,
            event.status,
            event.detail_json,
            event.canonical_sha256,
            event.created_at,
        ),
    )
    store._insert_ledger_record_locked(
        conn, "document_content_redaction_status", event.id
    )
    return event


def _semantic_review_targets_locked(
    conn: sqlite3.Connection,
    *,
    redaction_id: str,
    document_id: str,
    action_snapshot_ids: Sequence[str],
    created_at: str,
) -> list[DocumentContentRedactionTarget]:
    """Inventory readable semantic derivatives; never broad-delete their text."""

    result: list[DocumentContentRedactionTarget] = []
    for row in conn.execute(
        "SELECT DISTINCT c.id AS claim_id FROM expressions AS e "
        "JOIN document_spans AS s ON s.id = e.document_span_id "
        "JOIN claims AS c ON e.claim_ref_kind = 'local' AND e.claim_ref = c.id "
        "WHERE s.document_id = ? AND c.redacted_at IS NULL ORDER BY c.id",
        (document_id,),
    ).fetchall():
        result.append(
            _target(
                redaction_id=redaction_id,
                target_kind="semantic_derivative",
                target_ref=str(row["claim_id"]),
                field_name="claim.proposition",
                content_sha256=None,
                disposition="review_required",
                created_at=created_at,
            )
        )

    if not action_snapshot_ids:
        return result
    placeholders = ",".join("?" for _ in action_snapshot_ids)
    queries = (
        (
            "evaluation_result",
            "message,payload_json,evidence_selector_json",
            "SELECT r.id FROM evaluation_results AS r "
            "JOIN evaluation_runs AS run ON run.id = r.evaluation_run_id "
            f"WHERE run.action_snapshot_id IN ({placeholders}) ORDER BY r.id",
        ),
        (
            "cothink_item",
            "payload_json,rationale",
            f"SELECT id FROM cothink_items WHERE action_snapshot_id IN ({placeholders}) "
            "ORDER BY id",
        ),
        (
            "cowork_coordination_job",
            "selection_json,request_summary_json",
            f"SELECT id FROM cowork_coordination_jobs WHERE action_snapshot_id IN ({placeholders}) "
            "ORDER BY id",
        ),
    )
    for label, fields, sql in queries:
        for row in conn.execute(sql, tuple(action_snapshot_ids)).fetchall():
            result.append(
                _target(
                    redaction_id=redaction_id,
                    target_kind="semantic_derivative",
                    target_ref=str(row["id"]),
                    field_name=f"{label}.{fields}",
                    content_sha256=None,
                    disposition="review_required",
                    created_at=created_at,
                )
            )
    return result


def _build_targets_locked(
    conn: sqlite3.Connection,
    *,
    redaction_id: str,
    document_id: str,
    replacement_version_id: str,
    created_at: str,
) -> tuple[DocumentContentRedactionTarget, ...]:
    targets: list[DocumentContentRedactionTarget] = []
    versions = conn.execute(
        "SELECT id, projection_sha256, ydoc_snapshot_sha256 "
        "FROM document_versions WHERE document_id = ? AND id <> ? "
        "ORDER BY created_at, rowid",
        (document_id, replacement_version_id),
    ).fetchall()
    for row in versions:
        for field in ("projection_sha256", "ydoc_snapshot_sha256"):
            targets.append(
                _target(
                    redaction_id=redaction_id,
                    target_kind="document_version_blob",
                    target_ref=str(row["id"]),
                    field_name=field,
                    content_sha256=str(row[field]),
                    disposition="blob_cleanup",
                    created_at=created_at,
                )
            )

    document = conn.execute(
        "SELECT meta_json FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if document is not None:
        source_digest = retained_file_import_source_sha256(document["meta_json"])
        if source_digest is not None:
            targets.append(
                _target(
                    redaction_id=redaction_id,
                    target_kind="document_source_blob",
                    target_ref=document_id,
                    field_name="documents.meta_json.source.sha256",
                    content_sha256=source_digest,
                    disposition="blob_cleanup",
                    created_at=created_at,
                )
            )

    actions = conn.execute(
        "SELECT id, projection_blob_sha256, target_blob_sha256 "
        "FROM action_snapshots WHERE document_id = ? AND redacted_at IS NULL "
        "ORDER BY created_at, id",
        (document_id,),
    ).fetchall()
    action_ids: list[str] = []
    for row in actions:
        action_id = str(row["id"])
        action_ids.append(action_id)
        for field in ("projection_blob_sha256", "target_blob_sha256"):
            targets.append(
                _target(
                    redaction_id=redaction_id,
                    target_kind="action_snapshot_blob",
                    target_ref=action_id,
                    field_name=field,
                    content_sha256=str(row[field]),
                    disposition="blob_cleanup",
                    created_at=created_at,
                )
            )
        targets.append(
            _target(
                redaction_id=redaction_id,
                target_kind="action_snapshot_metadata",
                target_ref=action_id,
                field_name=(
                    "target_selector_json,context_boundary_json,"
                    "allowed_change_ranges_json"
                ),
                content_sha256=None,
                disposition="sql_tombstone",
                created_at=created_at,
            )
        )

    for row in conn.execute(
        "SELECT id FROM document_spans WHERE document_id = ? "
        "AND redacted_at IS NULL ORDER BY created_at, id",
        (document_id,),
    ).fetchall():
        targets.append(
            _target(
                redaction_id=redaction_id,
                target_kind="document_span",
                target_ref=str(row["id"]),
                field_name="selector_json,quote_exact",
                content_sha256=None,
                disposition="sql_tombstone",
                created_at=created_at,
            )
        )

    for row in conn.execute(
        "SELECT id FROM proposals WHERE document_id = ? AND redacted_at IS NULL "
        "ORDER BY created_at, id",
        (document_id,),
    ).fetchall():
        targets.append(
            _target(
                redaction_id=redaction_id,
                target_kind="proposal",
                target_ref=str(row["id"]),
                field_name=(
                    "selector_json,quote_exact,replacement,rationale,tldr,"
                    "claim_refs_json"
                ),
                content_sha256=None,
                disposition="sql_tombstone",
                created_at=created_at,
            )
        )

    targets.extend(
        _semantic_review_targets_locked(
            conn,
            redaction_id=redaction_id,
            document_id=document_id,
            action_snapshot_ids=action_ids,
            created_at=created_at,
        )
    )
    return tuple(
        sorted(
            targets,
            key=lambda item: (
                item.target_kind,
                item.target_ref,
                item.field_name,
                item.id,
            ),
        )
    )


def _tombstone_targets_locked(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    redacted_at: str,
) -> None:
    conn.execute(
        "UPDATE document_spans SET selector_json = ?, quote_exact = NULL, "
        "redacted_at = ? WHERE document_id = ? AND redacted_at IS NULL",
        (REDACTED_SELECTOR_JSON, redacted_at, document_id),
    )
    conn.execute(
        "UPDATE action_snapshots SET target_selector_json = ?, "
        "context_boundary_json = ?, allowed_change_ranges_json = '[]', "
        "redacted_at = ? WHERE document_id = ? AND redacted_at IS NULL",
        (
            REDACTED_ACTION_CONTEXT_JSON,
            REDACTED_ACTION_CONTEXT_JSON,
            redacted_at,
            document_id,
        ),
    )
    conn.execute(
        "UPDATE proposals SET selector_json = ?, quote_exact = NULL, "
        "replacement = NULL, rationale = NULL, tldr = NULL, "
        "claim_refs_json = NULL, redacted_at = ? "
        "WHERE document_id = ? AND redacted_at IS NULL",
        (REDACTED_SELECTOR_JSON, redacted_at, document_id),
    )


def _finish_cleanup(
    store: TruthStore,
    receipt: DocumentContentRedactionReceipt,
    targets: tuple[DocumentContentRedactionTarget, ...],
) -> DocumentContentRedactionResult:
    digests = tuple(
        sorted(
            {
                target.content_sha256
                for target in targets
                if target.disposition == "blob_cleanup"
                and target.content_sha256 is not None
            }
        )
    )
    deleted: list[str] = []
    shared: list[str] = []
    incomplete: list[str] = []
    review_targets = tuple(
        target.target_ref
        for target in targets
        if target.disposition == "review_required"
    )
    for digest in digests:
        path = store.resolve_blob_path(f"blobs/{digest}")
        existed = path.exists()
        try:
            removed = store._finish_blob_cleanup(digest)
        except Exception as exc:
            raise PostCommitHookError(
                "document content redaction committed but blob cleanup failed"
            ) from exc
        if removed or (existed and not path.exists()):
            deleted.append(digest)
            continue
        if path.exists():
            references = store.blob_reference_count(
                digest, live_only=False
            )
            if references:
                shared.append(digest)
            else:
                incomplete.append(digest)

    status_name = (
        "cleanup_complete"
        if not incomplete and not review_targets
        else "cleanup_incomplete"
    )
    with store.write_transaction() as conn:
        current = _latest_status_locked(conn, receipt.id)
        if current.status == "cleanup_complete":
            status = current
        elif current.status == status_name:
            status = current
        else:
            status = _append_status_locked(
                store,
                conn,
                redaction_id=receipt.id,
                status=status_name,
                detail={
                    "schema": "wb.document-content-redaction-cleanup/v1",
                    "deleted_blob_count": len(deleted),
                    "shared_blob_count": len(shared),
                    "incomplete_blob_sha256s": incomplete,
                    "review_target_count": len(review_targets),
                    "review_target_refs": review_targets,
                },
                at=_timestamp(None, "document content redaction cleanup at"),
            )
    return DocumentContentRedactionResult(
        receipt=receipt,
        targets=targets,
        status=status,
        complete=status.status == "cleanup_complete",
        deleted_blob_sha256s=tuple(deleted),
        shared_blob_sha256s=tuple(shared),
        review_target_refs=review_targets,
    )


def scrub_exact_managed_document_content(
    store: TruthStore,
    *,
    document_id: str,
    replacement_document_version_id: str,
    source_usage_id: str,
    source_ref: Mapping[str, Any] | str,
    source_redaction_event_id: str,
    actor_ref: Mapping[str, Any] | str,
    content_class: str,
    redaction_policy: str,
    at: str | None = None,
) -> DocumentContentRedactionResult:
    """Scrub one verified exact copy after its canonical tombstone is current.

    ``content_class`` and ``redaction_policy`` are mandatory, exact wire values
    rather than defaults.  The API therefore cannot be accidentally reused to
    broad-tombstone a mixed or semantic derivative.
    """

    if content_class != EXACT_COPY_CONTENT_CLASS:
        raise InvariantViolation(
            "managed document scrub requires content_class='exact_copy'"
        )
    if redaction_policy != SCRUB_REDACTION_POLICY:
        raise InvariantViolation(
            "managed document scrub requires redaction_policy='scrub'"
        )
    document_ref = _valid_record_id(document_id, "document_id")
    replacement_ref = _valid_record_id(
        replacement_document_version_id, "replacement_document_version_id"
    )
    usage_ref = _required_text(source_usage_id, "source_usage_id", maximum=512)
    source_event_ref = _required_text(
        source_redaction_event_id, "source_redaction_event_id", maximum=512
    )
    source_ref_json = _canonical_object(source_ref, "source_ref")
    actor_ref_json = _canonical_object(actor_ref, "actor_ref")
    created_at = _timestamp(at, "document content redaction at")
    identity = {
        "schema": "wb.document-content-redaction/v1",
        "document_id": document_ref,
        "replacement_document_version_id": replacement_ref,
        "source_usage_id": usage_ref,
        "source_ref": json.loads(source_ref_json),
        "source_redaction_event_id": source_event_ref,
        "content_class": content_class,
        "redaction_policy": redaction_policy,
        "actor_ref": json.loads(actor_ref_json),
    }
    redaction_id = _stable_id("wb.document-content-redaction-id/v1", identity)

    # The same document lock used by action capture and canonical replacement
    # prevents a fresh readable version/snapshot from racing the coverage scan.
    with ydoc_store.document_lock(store, document_ref):
        with store.write_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM document_content_redactions WHERE id = ?",
                (redaction_id,),
            ).fetchone()
            if existing is not None:
                receipt = _receipt_from_row(existing)
                targets = _targets_locked(conn, receipt.id)
            else:
                document = conn.execute(
                    "SELECT * FROM documents WHERE id = ?", (document_ref,)
                ).fetchone()
                replacement = conn.execute(
                    "SELECT rowid, * FROM document_versions WHERE id = ? "
                    "AND document_id = ?",
                    (replacement_ref, document_ref),
                ).fetchone()
                latest = conn.execute(
                    "SELECT rowid, * FROM document_versions WHERE document_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (document_ref,),
                ).fetchone()
                if document is None or replacement is None or latest is None:
                    raise InvariantViolation(
                        "document redaction replacement is unavailable"
                    )
                if str(latest["id"]) != replacement_ref:
                    raise InvariantViolation(
                        "document redaction replacement is not the latest version"
                    )
                if (
                    document["content_sha256"] != replacement["projection_sha256"]
                    or document["ydoc_snapshot_sha256"]
                    != replacement["ydoc_snapshot_sha256"]
                ):
                    raise InvariantViolation(
                        "document redaction replacement is not the current document"
                    )
                expected_detail = f"source-redaction:{source_event_ref}"
                if replacement["detail"] != expected_detail:
                    raise InvariantViolation(
                        "replacement version is not bound to this source redaction"
                    )

                targets = _build_targets_locked(
                    conn,
                    redaction_id=redaction_id,
                    document_id=document_ref,
                    replacement_version_id=replacement_ref,
                    created_at=created_at,
                )
                coverage_payload = [
                    {
                        "target_kind": target.target_kind,
                        "target_ref": target.target_ref,
                        "field_name": target.field_name,
                        "content_sha256": target.content_sha256,
                        "disposition": target.disposition,
                    }
                    for target in targets
                ]
                coverage_sha = sha256_text(canonical_json(coverage_payload))
                canonical_payload = {
                    **identity,
                    "coverage_sha256": coverage_sha,
                    "created_at": created_at,
                }
                receipt = DocumentContentRedactionReceipt(
                    id=redaction_id,
                    document_id=document_ref,
                    replacement_document_version_id=replacement_ref,
                    source_usage_id=usage_ref,
                    source_ref_json=source_ref_json,
                    source_redaction_event_id=source_event_ref,
                    content_class=content_class,
                    redaction_policy=redaction_policy,
                    actor_ref_json=actor_ref_json,
                    coverage_sha256=coverage_sha,
                    canonical_sha256=sha256_text(canonical_json(canonical_payload)),
                    created_at=created_at,
                )
                conn.execute("PRAGMA secure_delete = ON")
                conn.execute(
                    "INSERT INTO document_content_redactions "
                    "(id, document_id, replacement_document_version_id, "
                    "source_usage_id, source_ref_json, source_redaction_event_id, "
                    "content_class, redaction_policy, actor_ref_json, coverage_sha256, "
                    "canonical_sha256, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.id,
                        receipt.document_id,
                        receipt.replacement_document_version_id,
                        receipt.source_usage_id,
                        receipt.source_ref_json,
                        receipt.source_redaction_event_id,
                        receipt.content_class,
                        receipt.redaction_policy,
                        receipt.actor_ref_json,
                        receipt.coverage_sha256,
                        receipt.canonical_sha256,
                        receipt.created_at,
                    ),
                )
                store._insert_ledger_record_locked(
                    conn, "document_content_redaction", receipt.id
                )
                for target in targets:
                    _insert_target_locked(store, conn, target)
                _tombstone_targets_locked(
                    conn, document_id=document_ref, redacted_at=created_at
                )
                _append_status_locked(
                    store,
                    conn,
                    redaction_id=receipt.id,
                    status="content_tombstoned",
                    detail={
                        "schema": "wb.document-content-redaction-coverage/v1",
                        "target_count": len(targets),
                        "blob_target_count": sum(
                            target.disposition == "blob_cleanup" for target in targets
                        ),
                        "sql_tombstone_count": sum(
                            target.disposition == "sql_tombstone" for target in targets
                        ),
                        "review_target_count": sum(
                            target.disposition == "review_required" for target in targets
                        ),
                    },
                    at=created_at,
                )
                # Invalidate any pre-redaction recovery export before COMMIT;
                # the content-free receipt id drives crash recovery.
                store._queue_redaction_recovery_locked(conn, receipt.id)
                for digest in {
                    target.content_sha256
                    for target in targets
                    if target.disposition == "blob_cleanup"
                    and target.content_sha256 is not None
                }:
                    store._queue_blob_cleanup_locked(conn, digest)

    return _finish_cleanup(store, receipt, targets)


def get_document_content_redaction(
    store: TruthStore,
    redaction_id: str,
) -> DocumentContentRedactionResult:
    """Return one durable coverage/completion result without resolving bytes."""

    identifier = _valid_record_id(redaction_id, "document content redaction id")
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT * FROM document_content_redactions WHERE id = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise InvariantViolation("document content redaction does not exist")
        receipt = _receipt_from_row(row)
        targets = _targets_locked(conn, identifier)
        status = _latest_status_locked(conn, identifier)
    return DocumentContentRedactionResult(
        receipt=receipt,
        targets=targets,
        status=status,
        complete=status.status == "cleanup_complete",
        review_target_refs=tuple(
            target.target_ref
            for target in targets
            if target.disposition == "review_required"
        ),
    )


__all__ = [
    "DocumentContentRedactionReceipt",
    "DocumentContentRedactionResult",
    "DocumentContentRedactionStatusEvent",
    "DocumentContentRedactionTarget",
    "EXACT_COPY_CONTENT_CLASS",
    "SCRUB_REDACTION_POLICY",
    "get_document_content_redaction",
    "scrub_exact_managed_document_content",
]
