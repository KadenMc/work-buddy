"""Registration, lifecycle, materialization, and drift for co-work documents.

The store module owns durable inserts (the _insert_*_locked seam). This module
owns the policy that decides which document event may be appended and how the
active/retired lifecycle and drift state are projected from the append-only
doc_event log. Lifecycle is never an UPDATEd status column (PRD section 5, I12).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from work_buddy.truth.contracts import (
    Actor,
    InvariantViolation,
    validate_agent_producer_meta,
)
from work_buddy.truth.identity import (
    canonical_json,
    sha256_bytes,
)
from work_buddy.truth.store import (
    DOCUMENT_CLASSES,
    DocEventRecord,
    DocumentRecord,
    TruthStore,
    _record_id,
    _require_text,
    _timestamp,
    _valid_digest,
    _valid_record_id,
)


# Lifecycle markers, latest-wins by rowid insertion order (doc_events has no
# local seq). A 'retired' marker after any 'registered'/'reimported' marker
# projects the document as retired.
_LIFECYCLE_KINDS = frozenset({"registered", "reimported", "retired"})


def _producer_meta_json(actor: Actor, extra: Mapping[str, Any] | None = None) -> str | None:
    """Return the durable producer-identity meta_json for a write (I11)."""
    data: dict[str, Any] = dict(extra or {})
    if actor.kind == "agent_run":
        validate_agent_producer_meta(actor.meta)
        for key in ("model", "harness", "surface", "session_id"):
            value = actor.meta.get(key)
            if value is not None:
                data[key] = value
    return canonical_json(data) if data else None


def register_document(
    store: TruthStore,
    *,
    path: str,
    title: str | None = None,
    document_class: str,
    content_sha256: str,
    ydoc_snapshot_sha256: str | None = None,
    actor: Actor,
    at: str | None = None,
    document_id: str | None = None,
) -> DocumentRecord:
    """Register a scope-relative file as a cowork doc, idempotent by path.

    Returns the existing row on repeat. On a fresh registration appends BOTH a
    'registered' and an 'imported' doc_event in the same transaction (N6, so the
    R10 imported flag reflects the import leg), on a repeat appends neither.
    """
    relative_path = _require_text(path, "path")
    doc_class = _require_text(document_class, "document_class")
    if doc_class not in DOCUMENT_CLASSES:
        raise InvariantViolation(
            f"document_class must be one of {sorted(DOCUMENT_CLASSES)}"
        )
    content_digest = _valid_digest(content_sha256, "content_sha256")
    snapshot_digest = (
        None
        if ydoc_snapshot_sha256 is None
        else _valid_digest(ydoc_snapshot_sha256, "ydoc_snapshot_sha256")
    )
    identifier = _record_id(document_id, "document id")
    created = _timestamp(at, "registered at")
    meta_json = _producer_meta_json(actor)
    title_value = None if title is None else _require_text(title, "title")

    with store.write_transaction() as conn:
        existing = store._get_document_by_path_locked(conn, relative_path)
        if existing is not None:
            return existing
        record = DocumentRecord(
            id=identifier,
            path=relative_path,
            title=title_value,
            document_class=doc_class,
            content_sha256=content_digest,
            ydoc_snapshot_sha256=snapshot_digest,
            created_at=created,
            created_by_kind=actor.kind,
            created_by_ref=actor.ref,
            meta_json=meta_json,
        )
        store._insert_document_locked(conn, record)
        store._insert_doc_event_locked(
            conn,
            DocEventRecord(
                id=_record_id(None, "doc event id"),
                document_id=identifier,
                kind="registered",
                at=created,
                actor_kind=actor.kind,
                actor_ref=actor.ref,
                content_sha256=content_digest,
                ydoc_snapshot_sha256=snapshot_digest,
                detail=None,
            ),
        )
        store._insert_doc_event_locked(
            conn,
            DocEventRecord(
                id=_record_id(None, "doc event id"),
                document_id=identifier,
                kind="imported",
                at=created,
                actor_kind=actor.kind,
                actor_ref=actor.ref,
                content_sha256=content_digest,
                ydoc_snapshot_sha256=snapshot_digest,
                detail=None,
            ),
        )
        return record


def get_document(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> DocumentRecord:
    """Return one document row or raise if unknown."""
    identifier = _valid_record_id(document_id, "document_id")
    if conn is not None:
        record = store._get_document_locked(conn, identifier)
    else:
        with store._read_connection() as read_conn:
            record = store._get_document_locked(read_conn, identifier)
    if record is None:
        raise InvariantViolation(f"document does not exist: {identifier}")
    return record


def list_documents(
    store: TruthStore,
    *,
    include_retired: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[DocumentRecord, ...]:
    """List registered documents, retired ones filtered by default."""

    def _collect(read_conn: sqlite3.Connection) -> tuple[DocumentRecord, ...]:
        rows = read_conn.execute(
            "SELECT * FROM documents ORDER BY created_at, id"
        ).fetchall()
        records = tuple(DocumentRecord(**dict(row)) for row in rows)
        if include_retired:
            return records
        return tuple(
            record
            for record in records
            if _lifecycle_locked(store, read_conn, record.id) != "retired"
        )

    if conn is not None:
        return _collect(conn)
    with store._read_connection() as read_conn:
        return _collect(read_conn)


def _lifecycle_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    document_id: str,
) -> str:
    events = store._document_events_locked(conn, document_id)
    state = "active"
    for event in events:
        if event.kind in _LIFECYCLE_KINDS:
            state = "retired" if event.kind == "retired" else "active"
    return state


def current_lifecycle(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Project 'active' | 'retired' from the doc_event log by rowid order."""
    identifier = _valid_record_id(document_id, "document_id")
    if conn is not None:
        get_document(store, identifier, conn=conn)
        return _lifecycle_locked(store, conn, identifier)
    with store._read_connection() as read_conn:
        get_document(store, identifier, conn=read_conn)
        return _lifecycle_locked(store, read_conn, identifier)


def _append_doc_event_with_pointer(
    store: TruthStore,
    *,
    document_id: str,
    kind: str,
    actor: Actor,
    at: str | None,
    content_sha256: str | None = None,
    ydoc_snapshot_sha256: str | None = None,
    detail: str | None = None,
    advance_content: str | None = None,
    advance_snapshot: str | None = None,
) -> DocEventRecord:
    identifier = _valid_record_id(document_id, "document_id")
    timestamp = _timestamp(at, "doc event at")
    with store.write_transaction() as conn:
        if store._get_document_locked(conn, identifier) is None:
            raise InvariantViolation(f"document does not exist: {identifier}")
        if advance_content is not None or advance_snapshot is not None:
            store._advance_document_pointers_locked(
                conn,
                document_id=identifier,
                content_sha256=advance_content,
                ydoc_snapshot_sha256=advance_snapshot,
            )
        return store._insert_doc_event_locked(
            conn,
            DocEventRecord(
                id=_record_id(None, "doc event id"),
                document_id=identifier,
                kind=kind,
                at=timestamp,
                actor_kind=actor.kind,
                actor_ref=actor.ref,
                content_sha256=content_sha256,
                ydoc_snapshot_sha256=ydoc_snapshot_sha256,
                detail=detail,
            ),
        )


def record_materialization(
    store: TruthStore,
    *,
    document_id: str,
    content_sha256: str,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord:
    """Advance the latest materialized content pointer and append 'materialized'."""
    digest = _valid_digest(content_sha256, "content_sha256")
    return _append_doc_event_with_pointer(
        store,
        document_id=document_id,
        kind="materialized",
        actor=actor,
        at=at,
        content_sha256=digest,
        advance_content=digest,
    )


def advance_snapshot(
    store: TruthStore,
    *,
    document_id: str,
    ydoc_snapshot_sha256: str,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord:
    """Advance the latest compacted Y.Doc snapshot pointer and audit it.

    Records a 'materialized' doc_event carrying the new snapshot digest (the
    doc_event kind set has no dedicated snapshot verb, and the materialized
    kind is where ydoc_snapshot_sha256 is documented). A now-unreferenced prior
    snapshot blob is pruned through ydoc_store.prune_snapshot_blob.
    """
    digest = _valid_digest(ydoc_snapshot_sha256, "ydoc_snapshot_sha256")
    identifier = _valid_record_id(document_id, "document_id")
    prior = get_document(store, identifier).ydoc_snapshot_sha256
    event = _append_doc_event_with_pointer(
        store,
        document_id=identifier,
        kind="materialized",
        actor=actor,
        at=at,
        ydoc_snapshot_sha256=digest,
        detail="ydoc_snapshot_advance",
        advance_snapshot=digest,
    )
    if prior is not None and prior != digest:
        from work_buddy.truth.ydoc_store import prune_snapshot_blob

        prune_snapshot_blob(store, snapshot_sha256=prior)
    return event


def detect_drift(
    store: TruthStore,
    *,
    document_id: str,
    current_file_sha256: str,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord | None:
    """Compare the on-disk file hash to the last materialized hash.

    Appends a 'drift_detected' doc_event when they differ, blocking silent
    regeneration. MUTATES (appends a doc_event), so callable only from POST
    paths. Returns the appended event, or None when the file is clean.
    """
    digest = _valid_digest(current_file_sha256, "current_file_sha256")
    identifier = _valid_record_id(document_id, "document_id")
    document = get_document(store, identifier)
    if document.content_sha256 == digest:
        return None
    return _append_doc_event_with_pointer(
        store,
        document_id=identifier,
        kind="drift_detected",
        actor=actor,
        at=at,
        content_sha256=digest,
        detail="on_disk_hash_differs_from_materialized",
    )


def drift_state(
    store: TruthStore,
    document_id: str,
    *,
    current_file_sha256: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Pure READ projection (N7): return 'clean' | 'drifted' | 'missing'.

    Compares current_file_sha256 (or the on-disk read of the scope-relative
    path) to documents.content_sha256, appending NOTHING. The GET routes call
    this, never detect_drift, so a read never writes a doc_event.
    """
    identifier = _valid_record_id(document_id, "document_id")
    document = get_document(store, identifier, conn=conn)
    observed = current_file_sha256
    if observed is None:
        target = store.paths.root / document.path
        if not target.is_file():
            return "missing"
        observed = sha256_bytes(target.read_bytes())
    else:
        observed = _valid_digest(observed, "current_file_sha256")
    return "clean" if observed == document.content_sha256 else "drifted"


def reimport_document(
    store: TruthStore,
    *,
    document_id: str,
    content_sha256: str,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord:
    """Ingest an out-of-band file edit as an unattested reimport change set.

    Appends a 'reimported' doc_event and advances the content pointer, never
    overwriting silently.
    """
    digest = _valid_digest(content_sha256, "content_sha256")
    return _append_doc_event_with_pointer(
        store,
        document_id=document_id,
        kind="reimported",
        actor=actor,
        at=at,
        content_sha256=digest,
        advance_content=digest,
    )


def retire_document(
    store: TruthStore,
    *,
    document_id: str,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord:
    """Append a 'retired' doc_event, retaining the row and its history."""
    return _append_doc_event_with_pointer(
        store,
        document_id=document_id,
        kind="retired",
        actor=actor,
        at=at,
    )


def mark_session(
    store: TruthStore,
    *,
    document_id: str,
    opening: bool,
    actor: Actor,
    at: str | None = None,
) -> DocEventRecord:
    """Append a session_opened|session_closed marker for co-think continuity."""
    kind = "session_opened" if opening else "session_closed"
    return _append_doc_event_with_pointer(
        store,
        document_id=document_id,
        kind=kind,
        actor=actor,
        at=at,
    )


__all__ = [
    "advance_snapshot",
    "current_lifecycle",
    "detect_drift",
    "drift_state",
    "get_document",
    "list_documents",
    "mark_session",
    "record_materialization",
    "register_document",
    "reimport_document",
    "retire_document",
]
