"""Co-work policy adapter for durable document provenance attestations."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from work_buddy.cowork.lifecycle_lock import document_lifecycle_lock
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.anchors import CompositeSelector, serialize_selector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.expressions import _ensure_document_span_locked
from work_buddy.truth.identity import canonical_json, sha256_text
from work_buddy.truth.provenance import (
    ATTESTATION_SCHEMA,
    PROVENANCE_AUTHORSHIP_KINDS,
    PROVENANCE_BASIS_KINDS,
    PROVENANCE_REVIEW_STATUSES,
    attestation_canonical_sha256,
    normalize_source,
)
from work_buddy.truth.store import (
    DocumentProvenanceAttestationRecord,
    TruthStore,
    _timestamp,
    _valid_digest,
    _valid_record_id,
)


AUTHORSHIP_KINDS = PROVENANCE_AUTHORSHIP_KINDS
REVIEW_STATUSES = PROVENANCE_REVIEW_STATUSES
SOURCE_KINDS = frozenset({"file_import", "paste"})
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
INPUT_ATTESTATION_SCHEMA = "cowork-authorship-attestation/v1"
CURRENT_USER_IDENTITY_STATUSES = frozenset(
    {"local_actor_ref", "account_ref"}
)


class ProvenanceActorBindingError(InvariantViolation):
    """A frozen ``current_user`` binding no longer matches the acting user."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provenance_actor_changed",
        status: int = 409,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ProvenanceConflictError(InvariantViolation):
    """A typed conflict between one command key and durable provenance."""

    def __init__(
        self,
        message: str,
        *,
        existing_attestation_id: str,
    ) -> None:
        super().__init__(message)
        self.code = "provenance_idempotency_conflict"
        self.status = 409
        self.retryable = False
        self.details = {
            "existing_attestation_id": existing_attestation_id,
        }


def _required_text(value: Any, label: str, *, maximum: int = 240) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvariantViolation(f"{label} is required")
    if len(text) > maximum:
        raise InvariantViolation(f"{label} must contain at most {maximum} characters")
    return text


def _idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise InvariantViolation(
            "idempotency_key must contain 8-128 safe characters"
        )
    return key


def actor_binding(actor: Actor) -> dict[str, str]:
    """Return the trusted identity binding clients must freeze for ``Me``.

    The local dashboard is not an authentication boundary, so its stable actor
    reference remains visibly weaker than an authenticated account reference.
    A future authenticated adapter may opt into ``account_ref`` only by
    constructing the server-side Actor with trusted
    ``meta.identity_status=account_ref``.
    """

    if (
        actor.kind != "human"
        or not isinstance(actor.ref, str)
        or not actor.ref.strip()
    ):
        raise InvariantViolation("provenance attestation requires a human actor")
    identity_status = actor.meta.get("identity_status", "local_actor_ref")
    if identity_status not in CURRENT_USER_IDENTITY_STATUSES:
        raise InvariantViolation(
            "human actor identity_status must be local_actor_ref or account_ref"
        )
    return {
        "kind": "human",
        "ref": _required_text(actor.ref, "actor.ref", maximum=512),
        "identity_status": str(identity_status),
    }


def _resolve_person(value: Any, *, actor: Actor, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvariantViolation(f"{label} must identify a person")
    kind = str(value.get("kind") or "").strip()
    if kind == "current_user":
        unexpected = set(value) - {"kind", "ref", "identity_status"}
        if unexpected:
            raise InvariantViolation(
                f"{label} contains unsupported fields: {sorted(unexpected)}"
            )
        raw_ref = value.get("ref")
        raw_status = value.get("identity_status")
        if not isinstance(raw_ref, str):
            raise InvariantViolation(f"{label}.ref is required")
        if not isinstance(raw_status, str):
            raise InvariantViolation(f"{label}.identity_status is required")
        captured_ref = _required_text(
            raw_ref,
            f"{label}.ref",
            maximum=512,
        )
        captured_status = _required_text(
            raw_status,
            f"{label}.identity_status",
            maximum=40,
        )
        if captured_status not in CURRENT_USER_IDENTITY_STATUSES:
            raise InvariantViolation(
                f"{label}.identity_status must be local_actor_ref or account_ref"
            )
        current = actor_binding(actor)
        if captured_ref != current["ref"]:
            raise ProvenanceActorBindingError(
                "The acting user changed after this provenance determination "
                "was captured."
            )
        if captured_status != current["identity_status"]:
            raise ProvenanceActorBindingError(
                "The acting user's identity binding changed after this "
                "provenance determination was captured."
            )
        return current
    if kind == "named_person":
        unexpected = set(value) - {"kind", "display_name"}
        if unexpected:
            raise InvariantViolation(
                f"{label} contains unsupported fields: {sorted(unexpected)}"
            )
        return {
            "kind": "human",
            "display_name": _required_text(
                value.get("display_name"),
                f"{label}.display_name",
                maximum=160,
            ),
            # A typed name is useful but is not an authenticated account ID.
            "identity_status": "claimed_name",
        }
    raise InvariantViolation(
        f"{label}.kind must be current_user or named_person"
    )


def normalize_attestation(value: Any, *, actor: Actor) -> dict[str, Any]:
    """Validate UI input and resolve ``current_user`` to its local actor ref."""

    if actor.kind != "human" or not actor.ref:
        raise InvariantViolation("provenance attestation requires a human actor")
    if not isinstance(value, Mapping):
        raise InvariantViolation("attestation must be an object")
    if value.get("schema") != INPUT_ATTESTATION_SCHEMA:
        raise InvariantViolation(
            f"attestation.schema must be {INPUT_ATTESTATION_SCHEMA}"
        )

    raw_authorship = value.get("authorship")
    if not isinstance(raw_authorship, Mapping):
        raise InvariantViolation("attestation.authorship must be an object")
    authorship_kind = str(raw_authorship.get("kind") or "").strip()
    if authorship_kind not in AUTHORSHIP_KINDS:
        raise InvariantViolation(
            f"authorship.kind must be one of {sorted(AUTHORSHIP_KINDS)}"
        )
    raw_contributors = raw_authorship.get("contributors") or []
    if not isinstance(raw_contributors, list):
        raise InvariantViolation("authorship.contributors must be a list")
    contributors = [
        _resolve_person(item, actor=actor, label="authorship contributor")
        for item in raw_contributors
    ]
    if authorship_kind in {"human", "mixed"} and not contributors:
        raise InvariantViolation(
            f"{authorship_kind} authorship requires at least one human contributor"
        )
    if authorship_kind in {"ai", "unknown"} and contributors:
        raise InvariantViolation(
            f"{authorship_kind} authorship cannot carry human contributors"
        )

    raw_review = value.get("human_review")
    if not isinstance(raw_review, Mapping):
        raise InvariantViolation("attestation.human_review must be an object")
    review_status = str(raw_review.get("status") or "").strip()
    if review_status not in REVIEW_STATUSES:
        raise InvariantViolation(
            f"human_review.status must be one of {sorted(REVIEW_STATUSES)}"
        )
    raw_reviewers = raw_review.get("reviewers") or []
    if not isinstance(raw_reviewers, list):
        raise InvariantViolation("human_review.reviewers must be a list")
    reviewers = [
        _resolve_person(item, actor=actor, label="reviewer")
        for item in raw_reviewers
    ]
    if review_status == "reviewed" and not reviewers:
        raise InvariantViolation("reviewed content requires at least one reviewer")
    if review_status != "reviewed" and reviewers:
        raise InvariantViolation(
            f"{review_status} content cannot carry reviewer identities"
        )

    return {
        "authorship": {
            "kind": authorship_kind,
            "contributors": contributors,
        },
        "human_review": {
            "status": review_status,
            "reviewers": reviewers,
        },
    }


def _source(value: Mapping[str, Any]) -> dict[str, Any]:
    source = normalize_source(value)
    if source["kind"] not in SOURCE_KINDS:
        raise InvariantViolation(f"source.kind must be one of {sorted(SOURCE_KINDS)}")
    return source


def _attestation_id(
    *,
    store_id: str,
    document_id: str,
    actor_ref: str,
    idempotency_key: str,
) -> str:
    material = canonical_json(
        {
            "schema": ATTESTATION_SCHEMA,
            "store_id": store_id,
            "document_id": document_id,
            "actor_ref": actor_ref,
            "idempotency_key": idempotency_key,
        }
    )
    return sha256_text(material)[:32]


def _record_values(record: DocumentProvenanceAttestationRecord) -> dict[str, Any]:
    return {
        field: getattr(record, field)
        for field in DocumentProvenanceAttestationRecord.__dataclass_fields__
    }


def _record_attestation_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    document_id: str,
    attestation: Mapping[str, Any],
    source: Mapping[str, Any],
    actor: Actor,
    idempotency_key: str,
    target_kind: str,
    document_version_id: str | None,
    document_span_id: str | None,
    target_structured_head_sha256: str,
    basis_kind: str,
    basis_ref: str | None,
    supersedes_id: str | None,
    at: str | None,
) -> DocumentProvenanceAttestationRecord:
    document_ref = _valid_record_id(document_id, "document_id")
    document = store._get_document_locked(conn, document_ref)
    if document is None:
        raise InvariantViolation(f"document does not exist: {document_ref}")
    if actor.kind != "human" or not actor.ref:
        raise InvariantViolation("provenance attestation requires a human actor")
    key = _idempotency_key(idempotency_key)
    if basis_kind not in PROVENANCE_BASIS_KINDS:
        raise InvariantViolation(
            f"basis_kind must be one of {sorted(PROVENANCE_BASIS_KINDS)}"
        )
    normalized = normalize_attestation(attestation, actor=actor)
    normalized_source = _source(source)
    identifier = _attestation_id(
        store_id=store.store_id,
        document_id=document_ref,
        actor_ref=actor.ref,
        idempotency_key=key,
    )
    attester_meta = dict(actor.meta) if actor.meta else None
    canonical = attestation_canonical_sha256(
        document_id=document_ref,
        target_kind=target_kind,
        document_version_id=document_version_id,
        document_span_id=document_span_id,
        target_structured_head_sha256=target_structured_head_sha256,
        authorship_kind=normalized["authorship"]["kind"],
        human_contributors=normalized["authorship"]["contributors"],
        review_status=normalized["human_review"]["status"],
        human_reviewers=normalized["human_review"]["reviewers"],
        source_kind=normalized_source["kind"],
        source=normalized_source,
        basis_kind=basis_kind,
        basis_ref=basis_ref,
        supersedes_id=supersedes_id,
        attested_by_kind=actor.kind,
        attested_by_ref=actor.ref,
        attested_by_meta=attester_meta,
    )
    existing = store._get_document_provenance_attestation_locked(
        conn,
        identifier,
    )
    if existing is not None:
        if existing.canonical_sha256 != canonical:
            raise InvariantViolation(
                "idempotency_key was already used for a different "
                "provenance attestation"
            )
        return existing
    canonical_match = conn.execute(
        "SELECT id FROM document_provenance_attestations "
        "WHERE canonical_sha256 = ?",
        (canonical,),
    ).fetchone()
    if canonical_match is not None:
        raise ProvenanceConflictError(
            (
                "An equivalent provenance attestation already exists under "
                "another idempotency key."
            ),
            existing_attestation_id=str(canonical_match["id"]),
        )

    record = DocumentProvenanceAttestationRecord(
        id=identifier,
        document_id=document_ref,
        target_kind=target_kind,
        document_version_id=document_version_id,
        document_span_id=document_span_id,
        target_structured_head_sha256=target_structured_head_sha256,
        authorship_kind=normalized["authorship"]["kind"],
        human_contributors_json=canonical_json(
            normalized["authorship"]["contributors"]
        ),
        review_status=normalized["human_review"]["status"],
        human_reviewers_json=canonical_json(
            normalized["human_review"]["reviewers"]
        ),
        source_kind=normalized_source["kind"],
        source_json=canonical_json(normalized_source),
        basis_kind=basis_kind,
        basis_ref=basis_ref,
        supersedes_id=supersedes_id,
        idempotency_key=key,
        canonical_sha256=canonical,
        created_at=_timestamp(at, "provenance attestation at"),
        attested_by_kind=actor.kind,
        attested_by_ref=actor.ref,
        attested_by_meta_json=(
            canonical_json(attester_meta) if attester_meta is not None else None
        ),
    )
    return store._insert_document_provenance_attestation_locked(conn, record)


def record_document_attestation(
    store: TruthStore,
    *,
    document_id: str,
    attestation: Mapping[str, Any],
    source: Mapping[str, Any],
    actor: Actor,
    idempotency_key: str,
    document_version_id: str | None = None,
    basis_kind: str = "user_attestation",
    basis_ref: str | None = None,
    supersedes_id: str | None = None,
    at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> DocumentProvenanceAttestationRecord:
    """Append an attestation against one exact immutable document version."""

    document_ref = _valid_record_id(document_id, "document_id")
    key = _idempotency_key(idempotency_key)
    with store.write_transaction(conn) as write_conn:
        identifier = _attestation_id(
            store_id=store.store_id,
            document_id=document_ref,
            actor_ref=actor.ref or "",
            idempotency_key=key,
        )
        existing = store._get_document_provenance_attestation_locked(
            write_conn,
            identifier,
        )
        if existing is not None:
            if (
                document_version_id is not None
                and _valid_record_id(
                    document_version_id,
                    "document_version_id",
                )
                != existing.document_version_id
            ):
                raise InvariantViolation(
                    "idempotency_key was already used for a different "
                    "document version"
                )
            version_id = existing.document_version_id
            target_head = existing.target_structured_head_sha256
        else:
            versions = store._document_versions_locked(write_conn, document_ref)
            if document_version_id is None:
                if not versions:
                    raise InvariantViolation(
                        "document provenance requires an immutable document version"
                    )
                version = versions[-1]
            else:
                requested = _valid_record_id(
                    document_version_id,
                    "document_version_id",
                )
                version = store._get_document_version_locked(
                    write_conn,
                    requested,
                )
                if version is None or version.document_id != document_ref:
                    raise InvariantViolation(
                        "document_version does not belong to the document"
                    )
            version_id = version.id
            target_head = version.structured_head_sha256
        return _record_attestation_locked(
            store,
            write_conn,
            document_id=document_ref,
            attestation=attestation,
            source=source,
            actor=actor,
            idempotency_key=key,
            target_kind="document_version",
            document_version_id=version_id,
            document_span_id=None,
            target_structured_head_sha256=target_head,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            supersedes_id=supersedes_id,
            at=at,
        )


def record_span_attestation(
    store: TruthStore,
    *,
    document_id: str,
    exact: str,
    prefix: str = "",
    suffix: str = "",
    attestation: Mapping[str, Any],
    actor: Actor,
    idempotency_key: str,
    source: Mapping[str, Any] | None = None,
    basis_kind: str = "user_attestation",
    basis_ref: str | None = None,
    supersedes_id: str | None = None,
    expected_structured_head_sha256: str | None = None,
    at: str | None = None,
) -> tuple[DocumentProvenanceAttestationRecord, str]:
    """Anchor one pasted range and attest its authorship and review state."""

    if actor.kind != "human" or not actor.ref:
        raise InvariantViolation("provenance attestation requires a human actor")
    selector = CompositeSelector(exact=exact, prefix=prefix, suffix=suffix)
    normalized = normalize_attestation(attestation, actor=actor)
    key = _idempotency_key(idempotency_key)
    document_ref = _valid_record_id(document_id, "document_id")
    source_value = source or {"kind": "paste", "format": "plain_text"}
    expected_head = (
        None
        if expected_structured_head_sha256 is None
        else _valid_digest(
            expected_structured_head_sha256,
            "expected_structured_head_sha256",
        )
    )
    with document_lifecycle_lock(store.store_id, document_ref):
        # Freeze the same Y.Doc update boundary used by pushes, compaction, and
        # materialization while comparing the caller's structured-head
        # precondition and appending the span attestation.
        with ydoc_store.document_lock(store, document_ref):
            with store.write_transaction() as conn:
                document = store._get_document_locked(conn, document_ref)
                if document is None:
                    raise InvariantViolation(
                        f"document does not exist: {document_ref}"
                    )
                if documents._lifecycle_locked(store, conn, document.id) != "active":
                    raise InvariantViolation(
                        "provenance cannot be recorded on a retired document"
                    )
                if not document_surface_allowed(store, document):
                    raise InvariantViolation(
                        "This document is not available in Co-work for this folder"
                    )
                identifier = _attestation_id(
                    store_id=store.store_id,
                    document_id=document.id,
                    actor_ref=actor.ref,
                    idempotency_key=key,
                )
                existing = store._get_document_provenance_attestation_locked(
                    conn,
                    identifier,
                )
                if existing is not None:
                    if existing.document_span_id is None:
                        raise InvariantViolation(
                            "idempotency_key was used for a document-level attestation"
                        )
                    existing_span = store._get_document_span_locked(
                        conn,
                        existing.document_span_id,
                    )
                    if (
                        existing_span is None
                        or existing_span.document_id != document.id
                        or existing_span.quote_exact != exact
                        or existing_span.selector_json
                        != serialize_selector(selector)
                    ):
                        raise InvariantViolation(
                            "idempotency_key was already used for a different "
                            "provenance span"
                        )
                    if (
                        expected_head is not None
                        and existing.target_structured_head_sha256 != expected_head
                    ):
                        raise InvariantViolation(
                            "provenance target changed before the attestation was recorded"
                        )
                    event = _record_attestation_locked(
                        store,
                        conn,
                        document_id=document.id,
                        attestation=attestation,
                        source=source_value,
                        actor=actor,
                        idempotency_key=key,
                        target_kind="document_span",
                        document_version_id=None,
                        document_span_id=existing.document_span_id,
                        target_structured_head_sha256=(
                            existing.target_structured_head_sha256
                        ),
                        basis_kind=basis_kind,
                        basis_ref=basis_ref,
                        supersedes_id=supersedes_id,
                        at=at,
                    )
                    return event, existing.document_span_id

                author_kind = (
                    "human"
                    if normalized["authorship"]["kind"] == "human"
                    else "unknown"
                )
                author_ref = (
                    normalized["authorship"]["contributors"][0].get("ref")
                    if author_kind == "human"
                    and len(normalized["authorship"]["contributors"]) == 1
                    else None
                )
                span = _ensure_document_span_locked(
                    store,
                    conn,
                    document_id=document.id,
                    selector=selector,
                    quote_exact=exact,
                    actor=actor,
                    author_kind=author_kind,
                    author_ref=author_ref,
                    at=at,
                    # Identical words can occur in multiple pasted locations.
                    reuse_existing=False,
                )
                if document.ydoc_snapshot_sha256 is None:
                    raise InvariantViolation(
                        "document has no frozen structured baseline"
                    )
                target_head = ydoc_store.current_structured_head(
                    store,
                    document_id=document.id,
                    snapshot_sha256=document.ydoc_snapshot_sha256,
                )
                if expected_head is not None and target_head != expected_head:
                    raise InvariantViolation(
                        "provenance target changed before the attestation was recorded"
                    )
                event = _record_attestation_locked(
                    store,
                    conn,
                    document_id=document.id,
                    attestation=attestation,
                    source=source_value,
                    actor=actor,
                    idempotency_key=key,
                    target_kind="document_span",
                    document_version_id=None,
                    document_span_id=span.id,
                    target_structured_head_sha256=target_head,
                    basis_kind=basis_kind,
                    basis_ref=basis_ref,
                    supersedes_id=supersedes_id,
                    at=at,
                )
                return event, span.id


def list_attestations(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return portable attestations in append order, including history."""

    rows = store.list_document_provenance_attestations(document_id, conn=conn)
    return [
        {
            "attestation_id": row.id,
            "at": row.created_at,
            "asserted_by": {
                "kind": row.attested_by_kind,
                "ref": row.attested_by_ref,
                "meta": (
                    json.loads(row.attested_by_meta_json)
                    if row.attested_by_meta_json
                    else None
                ),
            },
            "schema": ATTESTATION_SCHEMA,
            "scope": {
                "kind": row.target_kind,
                "document_version_id": row.document_version_id,
                "document_span_id": row.document_span_id,
                "structured_head_sha256": row.target_structured_head_sha256,
            },
            "authorship": {
                "kind": row.authorship_kind,
                "contributors": json.loads(row.human_contributors_json),
            },
            "human_review": {
                "status": row.review_status,
                "reviewers": json.loads(row.human_reviewers_json),
            },
            "source": json.loads(row.source_json),
            "basis": {"kind": row.basis_kind, "ref": row.basis_ref},
            "supersedes_id": row.supersedes_id,
            "idempotency_key": row.idempotency_key,
            "canonical_sha256": row.canonical_sha256,
        }
        for row in rows
    ]


__all__ = [
    "ATTESTATION_SCHEMA",
    "AUTHORSHIP_KINDS",
    "CURRENT_USER_IDENTITY_STATUSES",
    "ProvenanceActorBindingError",
    "ProvenanceConflictError",
    "REVIEW_STATUSES",
    "actor_binding",
    "list_attestations",
    "normalize_attestation",
    "record_document_attestation",
    "record_span_attestation",
]
