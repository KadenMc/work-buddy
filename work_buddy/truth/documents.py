"""Registration, lifecycle, materialization, and drift for co-work documents.

The store module owns durable inserts (the _insert_*_locked seam). This module
owns the policy that decides which document event may be appended and how the
active/retired lifecycle and drift state are projected from the append-only
doc_event log. Lifecycle is never an UPDATEd status column (PRD section 5, I12).
"""

from __future__ import annotations

import json
import os
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
    DOCUMENT_VERSION_KINDS,
    DocEventRecord,
    DocumentRecord,
    DocumentVersionRecord,
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
_YDOC_GENERATION_DOMAIN = b"cowork-ydoc-generation/v1\0"
SOURCE_WRITEBACK_SAME_FILE = "same_file"
SOURCE_WRITEBACK_NEVER = "never"
RETAINED_FILE_IMPORT_SOURCE_KINDS = frozenset(
    {"file_import", "imported_markdown"}
)


def document_path_key(path: str) -> str:
    """Return the machine-local uniqueness key for a normalized document path."""

    relative_path = _require_text(path, "path")
    return relative_path.casefold() if os.name == "nt" else relative_path


def _insert_path_key_locked(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    path: str,
) -> None:
    try:
        conn.execute(
            "INSERT INTO document_path_keys (document_id, path_key) VALUES (?, ?)",
            (document_id, document_path_key(path)),
        )
    except sqlite3.IntegrityError as exc:
        raise InvariantViolation(
            "document path collides with an existing path on this host"
        ) from exc


def _version_record(
    *,
    document_id: str,
    kind: str,
    projection_sha256: str,
    ydoc_snapshot_sha256: str,
    structured_head_sha256: str,
    actor: Actor,
    at: str | None,
    detail: str | None = None,
    version_id: str | None = None,
) -> DocumentVersionRecord:
    if kind not in DOCUMENT_VERSION_KINDS:
        raise InvariantViolation(
            f"document version kind must be one of {sorted(DOCUMENT_VERSION_KINDS)}"
        )
    return DocumentVersionRecord(
        id=_record_id(version_id, "document version id"),
        document_id=_valid_record_id(document_id, "document_id"),
        kind=kind,
        projection_sha256=_valid_digest(projection_sha256, "projection_sha256"),
        ydoc_snapshot_sha256=_valid_digest(
            ydoc_snapshot_sha256, "ydoc_snapshot_sha256"
        ),
        structured_head_sha256=_valid_digest(
            structured_head_sha256, "structured_head_sha256"
        ),
        created_at=_timestamp(at, "document version at"),
        actor_kind=actor.kind,
        actor_ref=actor.ref,
        detail=detail,
    )


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


def source_writeback_policy(document: DocumentRecord) -> str:
    """Return whether Co-work may materialize into the document's source path.

    Historical and explicitly created documents remain file-backed. Documents
    initialized through From file carry a durable ``never`` policy:
    their selected file is an import source, not a Save target.
    """

    try:
        meta = json.loads(document.meta_json) if document.meta_json else {}
    except (TypeError, json.JSONDecodeError):
        return SOURCE_WRITEBACK_NEVER
    if not isinstance(meta, dict):
        return SOURCE_WRITEBACK_NEVER
    source = meta.get("source")
    if source is None:
        return SOURCE_WRITEBACK_SAME_FILE
    if not isinstance(source, dict):
        return SOURCE_WRITEBACK_NEVER
    policy = source.get("writeback_policy")
    if policy is None:
        return SOURCE_WRITEBACK_NEVER
    # Only the one explicit writeback policy is permissive. Corrupt or future
    # unknown values fail closed instead of turning an acquisition source into
    # a Save target.
    return (
        SOURCE_WRITEBACK_SAME_FILE
        if policy == SOURCE_WRITEBACK_SAME_FILE
        else SOURCE_WRITEBACK_NEVER
    )


def source_is_detached(document: DocumentRecord) -> bool:
    """True when the selected file was only an import source."""

    return source_writeback_policy(document) == SOURCE_WRITEBACK_NEVER


def retained_file_import_source_sha256(meta_json: str | None) -> str | None:
    """Return the retained source-blob digest declared by document metadata.

    A source digest is a live blob reference only when all of these conditions
    hold: ``source`` is an object, its kind is ``file_import`` or the historical
    ``imported_markdown`` spelling, its writeback policy is ``never``, and its
    SHA-256 is a canonical lowercase digest. This deliberately excludes normal
    file-backed documents and arbitrary hashes in producer metadata.

    The reference is *soft-required*: newly captured imports retain the exact
    source bytes and therefore export that blob, while historical imports may
    have only the digest. A missing historical blob does not make the document
    unreadable or its export non-portable; integrity inspection reports that
    reduced recovery fidelity as a warning.
    """

    try:
        meta = json.loads(meta_json) if meta_json else {}
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    source = meta.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("kind") not in RETAINED_FILE_IMPORT_SOURCE_KINDS:
        return None
    if source.get("writeback_policy") != SOURCE_WRITEBACK_NEVER:
        return None
    digest = source.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    return digest


def _provision_default_document_truth_policy(
    store: TruthStore,
    document: DocumentRecord,
    *,
    actor: Actor,
    conn: sqlite3.Connection,
) -> None:
    """Preserve legacy full Co-work behavior for uncoordinated registrations.

    Domain coordinators pass an outer transaction and provision their explicit
    contract before it commits. Legacy callers own no such coordinator, so the
    document and its compatibility policy must become visible atomically.
    """

    from work_buddy.cowork.truth_activation import (
        LEGACY_FULL_COWORK_CONTRACT,
        provision_document_policy,
    )

    provision_document_policy(
        store,
        document_id=document.id,
        interaction_contract_id=LEGACY_FULL_COWORK_CONTRACT,
        initial_activation="enabled",
        explicit_truth_acknowledged=True,
        actor=actor,
        intent_id=f"document-registration:{document.id}:legacy-truth-policy",
        conn=conn,
    )


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
    conn: sqlite3.Connection | None = None,
) -> DocumentRecord:
    """Register a scope-relative file as a cowork doc, idempotent by path.

    Returns the existing row on repeat. On a fresh registration appends BOTH a
    'registered' and an 'imported' doc_event in the same transaction (N6, so the
    R10 imported flag reflects the import leg), on a repeat appends neither.

    Registration is terminal with respect to retirement: a repeat on a retired
    path returns the retired row unchanged and does NOT revive it. Revival is a
    distinct path (reimport_document -> a 'reimported' event -> active).
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

    owns_transaction = conn is None
    with store.write_transaction(conn) as write_conn:
        existing = store._get_document_by_path_locked(write_conn, relative_path)
        if existing is not None:
            if owns_transaction:
                _provision_default_document_truth_policy(
                    store, existing, actor=actor, conn=write_conn
                )
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
        store._insert_document_locked(write_conn, record)
        _insert_path_key_locked(write_conn, document_id=identifier, path=relative_path)
        store._insert_doc_event_locked(
            write_conn,
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
            write_conn,
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
        if owns_transaction:
            _provision_default_document_truth_policy(
                store, record, actor=actor, conn=write_conn
            )
        return record


def register_ready_document(
    store: TruthStore,
    *,
    path: str,
    title: str | None,
    document_class: str,
    projection_bytes: bytes,
    ydoc_snapshot_sha256: str,
    structured_head_sha256: str,
    actor: Actor,
    mode: str,
    document_meta: Mapping[str, Any] | None = None,
    at: str | None = None,
    document_id: str | None = None,
    version_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[DocumentRecord, DocumentVersionRecord, bool]:
    """Atomically publish a fully initialized document and initial version.

    The caller owns any create-if-absent source-file publication.  This engine
    seam makes the document visible only after both exact projection bytes and
    the opaque Y.Doc snapshot are durable and digest-valid.
    """

    if mode not in {"create", "import"}:
        raise InvariantViolation("ready document registration mode must be create or import")
    if not isinstance(projection_bytes, (bytes, bytearray, memoryview)):
        raise InvariantViolation("projection_bytes must be exact bytes")
    relative_path = _require_text(path, "path")
    doc_class = _require_text(document_class, "document_class")
    if doc_class not in DOCUMENT_CLASSES:
        raise InvariantViolation(
            f"document_class must be one of {sorted(DOCUMENT_CLASSES)}"
        )
    snapshot_digest = _valid_digest(
        ydoc_snapshot_sha256, "ydoc_snapshot_sha256"
    )
    head_digest = _valid_digest(structured_head_sha256, "structured_head_sha256")
    projection = bytes(projection_bytes)
    projection_digest = sha256_bytes(projection)
    identifier = _record_id(document_id, "document id")
    created = _timestamp(at, "registered at")
    title_value = None if title is None else _require_text(title, "title")

    from work_buddy.truth import ydoc_store

    snapshot = ydoc_store.read_snapshot(store, snapshot_sha256=snapshot_digest)
    expected_head = ydoc_store.structured_head_from_segments(snapshot, ())
    if expected_head != head_digest:
        raise InvariantViolation(
            "structured_head_sha256 does not describe the initial snapshot"
        )
    # Filesystem blobs are content-addressed; an orphan after a later database
    # failure is harmless and is removed only by a proven refcount sweep.
    store._store_blob_bytes(projection_digest, projection)

    meta_json = _producer_meta_json(actor, document_meta)
    record = DocumentRecord(
        id=identifier,
        path=relative_path,
        title=title_value,
        document_class=doc_class,
        content_sha256=projection_digest,
        ydoc_snapshot_sha256=snapshot_digest,
        created_at=created,
        created_by_kind=actor.kind,
        created_by_ref=actor.ref,
        meta_json=meta_json,
    )
    version = _version_record(
        document_id=identifier,
        kind="initial_import",
        projection_sha256=projection_digest,
        ydoc_snapshot_sha256=snapshot_digest,
        structured_head_sha256=head_digest,
        actor=actor,
        at=created,
        detail=mode,
        version_id=version_id,
    )
    with store.write_transaction(conn) as write_conn:
        existing = store._get_document_by_path_locked(write_conn, relative_path)
        if existing is not None:
            existing_versions = store._document_versions_locked(
                write_conn, existing.id
            )
            if (
                existing.content_sha256 == projection_digest
                and existing.ydoc_snapshot_sha256 == snapshot_digest
                and existing_versions
                and existing_versions[-1].structured_head_sha256 == head_digest
            ):
                if conn is None:
                    _provision_default_document_truth_policy(
                        store, existing, actor=actor, conn=write_conn
                    )
                return existing, existing_versions[-1], False
            raise InvariantViolation("document path is already registered differently")
        store._insert_document_locked(write_conn, record)
        _insert_path_key_locked(
            write_conn, document_id=identifier, path=relative_path
        )
        store._insert_document_version_locked(write_conn, version)
        for kind in ("registered", "imported", "initialized"):
            store._insert_doc_event_locked(
                write_conn,
                DocEventRecord(
                    id=_record_id(None, "doc event id"),
                    document_id=identifier,
                    kind=kind,
                    at=created,
                    actor_kind=actor.kind,
                    actor_ref=actor.ref,
                    content_sha256=projection_digest,
                    ydoc_snapshot_sha256=snapshot_digest,
                    detail=mode,
                ),
            )
        if conn is None:
            _provision_default_document_truth_policy(
                store, record, actor=actor, conn=write_conn
            )
        return record, version, True


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


def document_versions(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[DocumentVersionRecord, ...]:
    identifier = _valid_record_id(document_id, "document_id")
    if conn is not None:
        get_document(store, identifier, conn=conn)
        return store._document_versions_locked(conn, identifier)
    with store._read_connection() as read_conn:
        get_document(store, identifier, conn=read_conn)
        return store._document_versions_locked(read_conn, identifier)


def current_document_version(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> DocumentVersionRecord | None:
    versions = document_versions(store, document_id, conn=conn)
    return versions[-1] if versions else None


def current_ydoc_generation(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Return the stable logical Y.Doc lineage used for outbox compatibility.

    Snapshot compaction and Markdown materialization append document versions but
    preserve the same CRDT lineage. An explicit reimport is a destructive Y.Doc
    replacement, so the newest reimport version id becomes part of the generation.
    """

    identifier = _valid_record_id(document_id, "document_id")

    def _read(read_conn: sqlite3.Connection) -> str:
        get_document(store, identifier, conn=read_conn)
        row = read_conn.execute(
            "SELECT id FROM document_versions "
            "WHERE document_id = ? AND kind = 'reimported' "
            "ORDER BY rowid DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        replacement_id = "" if row is None else str(row["id"])
        identity = f"{identifier}\0{replacement_id}".encode("utf-8")
        return sha256_bytes(_YDOC_GENERATION_DOMAIN + identity)

    if conn is not None:
        return _read(conn)
    with store._read_connection() as read_conn:
        return _read(read_conn)


def commit_document_version(
    store: TruthStore,
    *,
    document_id: str,
    kind: str,
    projection_sha256: str,
    ydoc_snapshot_sha256: str,
    structured_head_sha256: str,
    actor: Actor,
    at: str | None = None,
    detail: str | None = None,
    version_id: str | None = None,
    event_kind: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[DocumentRecord, DocumentVersionRecord, DocEventRecord]:
    """Advance current pointers and append one coherent immutable version."""

    identifier = _valid_record_id(document_id, "document_id")
    version = _version_record(
        document_id=identifier,
        kind=kind,
        projection_sha256=projection_sha256,
        ydoc_snapshot_sha256=ydoc_snapshot_sha256,
        structured_head_sha256=structured_head_sha256,
        actor=actor,
        at=at,
        detail=detail,
        version_id=version_id,
    )
    doc_event_kind = event_kind or (
        "snapshot_compacted" if kind == "snapshot_compacted" else kind
    )
    if doc_event_kind not in {
        "repaired",
        "materialized",
        "reimported",
        "snapshot_compacted",
    }:
        raise InvariantViolation("unsupported document version event kind")
    with store.write_transaction(conn) as write_conn:
        current = store._get_document_locked(write_conn, identifier)
        if current is None:
            raise InvariantViolation(f"document does not exist: {identifier}")
        refreshed = store._advance_document_pointers_locked(
            write_conn,
            document_id=identifier,
            content_sha256=version.projection_sha256,
            ydoc_snapshot_sha256=version.ydoc_snapshot_sha256,
        )
        store._insert_document_version_locked(write_conn, version)
        event = store._insert_doc_event_locked(
            write_conn,
            DocEventRecord(
                id=_record_id(None, "doc event id"),
                document_id=identifier,
                kind=doc_event_kind,
                at=version.created_at,
                actor_kind=actor.kind,
                actor_ref=actor.ref,
                content_sha256=version.projection_sha256,
                ydoc_snapshot_sha256=version.ydoc_snapshot_sha256,
                detail=detail,
            ),
        )
        return refreshed, version, event


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
    conn: sqlite3.Connection | None = None,
) -> DocEventRecord:
    identifier = _valid_record_id(document_id, "document_id")
    timestamp = _timestamp(at, "doc event at")
    with store.write_transaction(conn) as write_conn:
        if store._get_document_locked(write_conn, identifier) is None:
            raise InvariantViolation(f"document does not exist: {identifier}")
        if advance_content is not None or advance_snapshot is not None:
            store._advance_document_pointers_locked(
                write_conn,
                document_id=identifier,
                content_sha256=advance_content,
                ydoc_snapshot_sha256=advance_snapshot,
            )
        return store._insert_doc_event_locked(
            write_conn,
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
    identifier = _valid_record_id(document_id, "document_id")
    document = get_document(store, identifier)
    if document.ydoc_snapshot_sha256 is not None:
        from work_buddy.truth import ydoc_store

        from work_buddy.cowork.paths import resolve_writeback_target

        target = resolve_writeback_target(store, document).path
        if target.is_file():
            projection_bytes = target.read_bytes()
            if sha256_bytes(projection_bytes) == digest:
                store._store_blob_bytes(digest, projection_bytes)
        projection_blob = store.resolve_blob_path(f"blobs/{digest}")
        if projection_blob.is_file():
            head = ydoc_store.current_structured_head(
                store,
                document_id=identifier,
                snapshot_sha256=document.ydoc_snapshot_sha256,
            )
            _, _, event = commit_document_version(
                store,
                document_id=identifier,
                kind="materialized",
                projection_sha256=digest,
                ydoc_snapshot_sha256=document.ydoc_snapshot_sha256,
                structured_head_sha256=head,
                actor=actor,
                at=at,
            )
            return event
    return _append_doc_event_with_pointer(
        store,
        document_id=identifier,
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

    New stores append a dedicated snapshot_compacted version. Historical blobs
    remain retained while any version references them.
    """
    digest = _valid_digest(ydoc_snapshot_sha256, "ydoc_snapshot_sha256")
    identifier = _valid_record_id(document_id, "document_id")
    document = get_document(store, identifier)
    from work_buddy.truth import ydoc_store

    snapshot = ydoc_store.read_snapshot(store, snapshot_sha256=digest)
    structured_head = ydoc_store.structured_head_from_segments(snapshot, ())
    projection_blob = store.resolve_blob_path(f"blobs/{document.content_sha256}")
    if not projection_blob.is_file():
        from work_buddy.cowork.paths import resolve_document_source_path

        target = resolve_document_source_path(store, document).path
        if target.is_file():
            content = target.read_bytes()
            if sha256_bytes(content) == document.content_sha256:
                store._store_blob_bytes(document.content_sha256, content)
    if store.resolve_blob_path(f"blobs/{document.content_sha256}").is_file():
        _, _, event = commit_document_version(
            store,
            document_id=identifier,
            kind="snapshot_compacted",
            projection_sha256=document.content_sha256,
            ydoc_snapshot_sha256=digest,
            structured_head_sha256=structured_head,
            actor=actor,
            at=at,
            detail="ydoc_snapshot_advance",
        )
        return event
    return _append_doc_event_with_pointer(
        store,
        document_id=identifier,
        kind="snapshot_compacted",
        actor=actor,
        at=at,
        content_sha256=document.content_sha256,
        ydoc_snapshot_sha256=digest,
        detail="ydoc_snapshot_advance_baseline_unavailable",
        advance_snapshot=digest,
    )


def repair_document_snapshot(
    store: TruthStore,
    *,
    document_id: str,
    projection_bytes: bytes,
    ydoc_snapshot_sha256: str,
    structured_head_sha256: str,
    actor: Actor,
    at: str | None = None,
    version_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[DocumentRecord, DocumentVersionRecord, DocEventRecord]:
    """Make one legacy bootstrap-required document ready without re-registering it."""

    identifier = _valid_record_id(document_id, "document_id")
    if not isinstance(projection_bytes, (bytes, bytearray, memoryview)):
        raise InvariantViolation("projection_bytes must be exact bytes")
    projection = bytes(projection_bytes)
    document = get_document(store, identifier)
    if sha256_bytes(projection) != document.content_sha256:
        raise InvariantViolation("repair source no longer matches the projection pointer")

    from work_buddy.truth import ydoc_store

    tail, _ = ydoc_store.read_updates(store, document_id=identifier)
    if tail:
        raise InvariantViolation("legacy update tail requires manual recovery")
    snapshot_digest = _valid_digest(
        ydoc_snapshot_sha256, "ydoc_snapshot_sha256"
    )
    snapshot = ydoc_store.read_snapshot(store, snapshot_sha256=snapshot_digest)
    head = _valid_digest(structured_head_sha256, "structured_head_sha256")
    if ydoc_store.structured_head_from_segments(snapshot, ()) != head:
        raise InvariantViolation("structured head does not describe the repair snapshot")
    store._store_blob_bytes(document.content_sha256, projection)
    return commit_document_version(
        store,
        document_id=identifier,
        kind="repaired",
        projection_sha256=document.content_sha256,
        ydoc_snapshot_sha256=snapshot_digest,
        structured_head_sha256=head,
        actor=actor,
        at=at,
        detail="safe_snapshot_repair",
        version_id=version_id,
        event_kind="repaired",
        conn=conn,
    )


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
        from work_buddy.cowork.paths import resolve_document_source_path

        target = resolve_document_source_path(store, document).path
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
    conn: sqlite3.Connection | None = None,
) -> DocEventRecord:
    """Append a 'retired' doc_event, retaining the row and its history."""
    return _append_doc_event_with_pointer(
        store,
        document_id=document_id,
        kind="retired",
        actor=actor,
        at=at,
        conn=conn,
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
    "RETAINED_FILE_IMPORT_SOURCE_KINDS",
    "SOURCE_WRITEBACK_NEVER",
    "SOURCE_WRITEBACK_SAME_FILE",
    "advance_snapshot",
    "commit_document_version",
    "current_lifecycle",
    "current_document_version",
    "current_ydoc_generation",
    "detect_drift",
    "document_path_key",
    "document_versions",
    "drift_state",
    "get_document",
    "list_documents",
    "mark_session",
    "record_materialization",
    "register_document",
    "register_ready_document",
    "retained_file_import_source_sha256",
    "repair_document_snapshot",
    "reimport_document",
    "retire_document",
    "source_is_detached",
    "source_writeback_policy",
]
