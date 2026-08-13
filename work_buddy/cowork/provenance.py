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
    PROVENANCE_SOURCE_KINDS,
    PROVENANCE_SPAN_SOURCE_BASIS_PAIRS,
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
SOURCE_KINDS = PROVENANCE_SOURCE_KINDS
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
INPUT_ATTESTATION_SCHEMA = "cowork-authorship-attestation/v1"
CURRENT_USER_IDENTITY_STATUSES = frozenset(
    {"local_actor_ref", "account_ref"}
)
SPAN_SOURCE_BASIS_PAIRS = PROVENANCE_SPAN_SOURCE_BASIS_PAIRS
AUTOMATIC_SHORT_TEXT_MAX_CHARS = 599


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


class ProvenanceReviewError(InvariantViolation):
    """A typed refusal to turn an unsafe target into a review assertion."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 409,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = dict(details or {})


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
    normalized_attestation: Mapping[str, Any] | None = None,
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
    normalized = (
        normalize_attestation(attestation, actor=actor)
        if normalized_attestation is None
        else normalized_attestation
    )
    normalized_source = _source(source)
    if supersedes_id is not None:
        prior = store._get_document_provenance_attestation_locked(
            conn,
            _valid_record_id(supersedes_id, "supersedes_id"),
        )
        if prior is None:
            raise InvariantViolation(
                f"superseded attestation does not exist: {supersedes_id}"
            )
        if (
            prior.document_id != document_ref
            or prior.target_kind != target_kind
            or prior.document_version_id != document_version_id
            or prior.document_span_id != document_span_id
            or prior.target_structured_head_sha256
            != target_structured_head_sha256
        ):
            raise InvariantViolation(
                "superseded attestation must describe the same frozen target"
            )
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
    """Anchor one exact range and attest its authorship and review state.

    The source/basis pair is deliberately closed.  In particular, a caller
    cannot describe selected legacy text as direct entry or use a generic user
    attestation to manufacture the stronger direct-entry observation.
    """

    if actor.kind != "human" or not actor.ref:
        raise InvariantViolation("provenance attestation requires a human actor")
    selector = CompositeSelector(exact=exact, prefix=prefix, suffix=suffix)
    normalized = normalize_attestation(attestation, actor=actor)
    key = _idempotency_key(idempotency_key)
    document_ref = _valid_record_id(document_id, "document_id")
    source_value = source or {"kind": "paste", "format": "plain_text"}
    normalized_source = _source(source_value)
    source_basis = (normalized_source["kind"], basis_kind)
    if source_basis not in SPAN_SOURCE_BASIS_PAIRS:
        raise InvariantViolation(
            "source.kind and basis_kind are not an allowed provenance pair"
        )
    if basis_kind in {
        "automatic_short_text_attribution",
        "automatic_direct_entry_attribution",
    }:
        current_actor = actor_binding(actor)
        if (
            normalized["authorship"]["kind"] != "human"
            or normalized["authorship"]["contributors"] != [current_actor]
            or normalized["human_review"]
            != {"status": "not_applicable", "reviewers": []}
        ):
            raise InvariantViolation(
                "automatic attribution requires text authored by the acting user"
            )
        if (
            basis_kind == "automatic_short_text_attribution"
            and len(exact) > AUTOMATIC_SHORT_TEXT_MAX_CHARS
        ):
            raise InvariantViolation(
                "automatic short-text attribution exceeds the size limit"
            )
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
                        source=normalized_source,
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
                    source=normalized_source,
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


def portable_attestation(
    row: DocumentProvenanceAttestationRecord,
) -> dict[str, Any]:
    """Return the stable wire representation for one append-only record."""

    return {
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


def list_attestations(
    store: TruthStore,
    document_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return portable attestations in append order, including history."""

    rows = store.list_document_provenance_attestations(document_id, conn=conn)
    return [portable_attestation(row) for row in rows]


def _target_key(
    row: DocumentProvenanceAttestationRecord,
) -> tuple[str, str]:
    target_id = row.document_version_id or row.document_span_id or ""
    return row.target_kind, target_id


def _target_projection(
    rows: list[DocumentProvenanceAttestationRecord],
    *,
    current_structured_head_sha256: str | None,
    span_selector: CompositeSelector | None,
) -> dict[str, Any]:
    first = rows[0]
    superseded_ids = {
        row.supersedes_id for row in rows if row.supersedes_id is not None
    }
    leaves = [row for row in rows if row.id not in superseded_ids]
    portable_history = [portable_attestation(row) for row in rows]
    portable_leaves = [portable_attestation(row) for row in leaves]
    target_heads = sorted(
        {row.target_structured_head_sha256 for row in rows}
    )
    has_conflicting_heads = len(target_heads) != 1
    representative_head = target_heads[0]
    resolved = len(leaves) == 1 and not has_conflicting_heads
    if has_conflicting_heads or current_structured_head_sha256 is None:
        currentness = "unavailable"
    elif first.target_kind == "document_version":
        currentness = (
            "current"
            if representative_head == current_structured_head_sha256
            else "stale"
        )
    else:
        # Exact span selectors may survive unrelated document edits, but only
        # the editor can prove that by uniquely resolving the complete quote
        # anchor.  Never call a changed-head span current on the server alone.
        currentness = (
            "current"
            if representative_head == current_structured_head_sha256
            else "requires_reanchor"
        )
    effective = portable_leaves[0] if resolved else None
    review_eligibility = "eligible"
    if not resolved:
        review_eligibility = "conflicted"
    elif currentness != "current":
        review_eligibility = "stale_target"
    elif effective is None or effective["authorship"]["kind"] not in {
        "ai",
        "mixed",
    }:
        review_eligibility = "not_ai_authored"
    elif effective["human_review"]["status"] == "reviewed":
        review_eligibility = "already_reviewed"
    elif effective["human_review"]["status"] == "not_applicable":
        review_eligibility = "not_applicable"
    target_id = first.document_version_id or first.document_span_id
    return {
        "projection_id": f"{first.target_kind}:{target_id}",
        "target": {
            "kind": first.target_kind,
            "document_version_id": first.document_version_id,
            "document_span_id": first.document_span_id,
            "structured_head_sha256": representative_head,
            "currentness": currentness,
        },
        "span": (
            None
            if span_selector is None
            else {
                "exact": span_selector.exact,
                "prefix": span_selector.prefix,
                "suffix": span_selector.suffix,
            }
        ),
        "resolution": "resolved" if resolved else "conflicted",
        "review_eligibility": review_eligibility,
        "issue": (
            None
            if resolved
            else {
                "code": "conflicting_effective_attestations",
                "message": "This target has incompatible effective provenance records.",
            }
        ),
        "effective_attestation": effective,
        "effective_attestations": portable_leaves,
        "history": portable_history,
    }


def _project_attestations_locked(
    store: TruthStore,
    document_id: str,
    *,
    current_structured_head_sha256: str | None,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    document_ref = _valid_record_id(document_id, "document_id")
    current_head = (
        None
        if current_structured_head_sha256 is None
        else _valid_digest(
            current_structured_head_sha256,
            "current_structured_head_sha256",
        )
    )
    rows = list(
        store.list_document_provenance_attestations(document_ref, conn=conn)
    )
    groups: dict[
        tuple[str, str], list[DocumentProvenanceAttestationRecord]
    ] = {}
    for row in rows:
        groups.setdefault(_target_key(row), []).append(row)

    document_targets: list[dict[str, Any]] = []
    span_targets_by_id: dict[str, list[dict[str, Any]]] = {}
    for (_kind, target_id), target_rows in groups.items():
        if target_rows[0].target_kind == "document_version":
            document_targets.append(
                _target_projection(
                    target_rows,
                    current_structured_head_sha256=current_head,
                    span_selector=None,
                )
            )
            continue
        span = store._get_document_span_locked(conn, target_id)
        selector = None
        selector_issue: dict[str, str] | None = None
        if span is None or span.document_id != document_ref:
            selector_issue = {
                "code": "missing_span_target",
                "message": "The recorded provenance span is unavailable.",
            }
        else:
            try:
                selector = CompositeSelector.from_json(span.selector_json)
            except Exception:  # noqa: BLE001 - corrupt provenance stays inspectable
                selector_issue = {
                    "code": "invalid_span_selector",
                    "message": "The recorded provenance anchor is invalid.",
                }
        projection = _target_projection(
            target_rows,
            current_structured_head_sha256=current_head,
            span_selector=selector,
        )
        if selector_issue is not None:
            projection["span"] = None
            projection["resolution"] = "conflicted"
            projection["effective_attestation"] = None
            projection["review_eligibility"] = "conflicted"
            projection["issue"] = selector_issue
            projection["target"]["currentness"] = "unavailable"
        span_targets_by_id[target_id] = [projection]

    # A span ID has one stable projection entry. If legacy/corrupt history
    # carries several frozen heads, _target_projection aggregates it as an
    # explicit conflict instead of emitting ambiguous duplicate display IDs.
    span_rows = conn.execute(
        "SELECT id FROM document_spans WHERE document_id = ? "
        "ORDER BY created_at, id",
        (document_ref,),
    ).fetchall()
    extant_span_ids = [str(span_row["id"]) for span_row in span_rows]
    span_targets = [
        projection
        for span_id in extant_span_ids
        for projection in span_targets_by_id.get(span_id, [])
    ]
    orphaned_span_ids = sorted(
        set(span_targets_by_id) - set(extant_span_ids),
        key=lambda span_id: (
            span_targets_by_id[span_id][0]["history"][0]["at"],
            span_id,
        ),
    )
    span_targets.extend(
        projection
        for span_id in orphaned_span_ids
        for projection in span_targets_by_id[span_id]
    )
    current_document_targets = [
        target
        for target in document_targets
        if target["target"]["currentness"] == "current"
    ]
    if len(current_document_targets) == 1:
        document_default = current_document_targets[0]
    elif len(current_document_targets) > 1:
        effective = [
            attestation
            for target in current_document_targets
            for attestation in target["effective_attestations"]
        ]
        history = [
            attestation
            for target in current_document_targets
            for attestation in target["history"]
        ]
        document_default = {
            "projection_id": "document_version:current-conflict",
            "target": {
                "kind": "document_version",
                "document_version_id": None,
                "document_span_id": None,
                "structured_head_sha256": current_head,
                "currentness": "current",
            },
            "span": None,
            "resolution": "conflicted",
            "review_eligibility": "conflicted",
            "issue": {
                "code": "conflicting_document_defaults",
                "message": "The current document has incompatible provenance defaults.",
            },
            "effective_attestation": None,
            "effective_attestations": effective,
            "history": history,
        }
    else:
        document_default = document_targets[-1] if document_targets else None
    visible_targets = (
        ([] if document_default is None else [document_default]) + span_targets
    )

    def _effective(target: Mapping[str, Any]) -> Mapping[str, Any] | None:
        value = target.get("effective_attestation")
        return value if isinstance(value, Mapping) else None

    summary = {
        "total_targets": len(visible_targets),
        "current_span_count": sum(
            target["target"]["currentness"] == "current"
            for target in span_targets
        ),
        "ai_unreviewed_count": sum(
            effective is not None
            and effective["authorship"]["kind"] in {"ai", "mixed"}
            and effective["human_review"]["status"]
            in {"not_reviewed", "unknown"}
            for effective in (_effective(target) for target in visible_targets)
        ),
        "reviewed_count": sum(
            effective is not None
            and effective["human_review"]["status"] == "reviewed"
            for effective in (_effective(target) for target in visible_targets)
        ),
        "conflicted_count": sum(
            target["resolution"] == "conflicted" for target in visible_targets
        ),
        "stale_count": sum(
            target["target"]["currentness"] != "current"
            for target in visible_targets
        ),
        "unrecorded": (
            document_default is None
            or document_default["target"]["currentness"] != "current"
        ),
    }
    return {
        "schema": "cowork-provenance-view/v1",
        "current_structured_head_sha256": current_head,
        "document_default": document_default,
        "spans": span_targets,
        "history": [portable_attestation(row) for row in rows],
        "summary": summary,
    }


def project_attestations(
    store: TruthStore,
    document_id: str,
    *,
    current_structured_head_sha256: str | None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Project append-only history into safe, deterministic effective targets."""

    if conn is not None:
        return _project_attestations_locked(
            store,
            document_id,
            current_structured_head_sha256=current_structured_head_sha256,
            conn=conn,
        )
    with store._read_connection() as read_conn:
        return _project_attestations_locked(
            store,
            document_id,
            current_structured_head_sha256=current_structured_head_sha256,
            conn=read_conn,
        )


def _validate_review_target_locked(
    store: TruthStore,
    conn: sqlite3.Connection,
    *,
    document_id: str,
    prior: DocumentProvenanceAttestationRecord,
) -> None:
    """Fail closed unless the predecessor still names a valid frozen target."""

    def mismatch(message: str) -> ProvenanceReviewError:
        return ProvenanceReviewError(
            message,
            code="provenance_review_target_mismatch",
            details={
                "target_kind": prior.target_kind,
                "document_version_id": prior.document_version_id,
                "document_span_id": prior.document_span_id,
            },
        )

    try:
        target_head = _valid_digest(
            prior.target_structured_head_sha256,
            "target_structured_head_sha256",
        )
    except InvariantViolation as exc:
        raise mismatch(
            "The provenance attestation has an invalid frozen target."
        ) from exc

    if prior.target_kind == "document_version":
        if prior.document_version_id is None or prior.document_span_id is not None:
            raise mismatch(
                "The provenance attestation has an invalid version target."
            )
        version = store._get_document_version_locked(
            conn,
            prior.document_version_id,
        )
        if version is None or version.document_id != document_id:
            raise mismatch(
                "The provenance version target is unavailable for this document."
            )
        if version.structured_head_sha256 != target_head:
            raise mismatch(
                "The provenance version no longer matches its frozen target."
            )
        return

    if prior.target_kind != "document_span":
        raise mismatch("The provenance attestation has an unsupported target.")
    if prior.document_span_id is None or prior.document_version_id is not None:
        raise mismatch("The provenance attestation has an invalid span target.")

    span = store._get_document_span_locked(conn, prior.document_span_id)
    if span is None or span.document_id != document_id:
        raise mismatch(
            "The provenance span target is unavailable for this document."
        )
    try:
        selector = CompositeSelector.from_json(span.selector_json)
    except Exception as exc:  # noqa: BLE001 - corrupt anchors must fail closed
        raise mismatch("The provenance span target has an invalid anchor.") from exc
    if (
        span.redacted_at is not None
        or span.quote_exact is None
        or selector.exact != span.quote_exact
        or span.span_sha256 != sha256_text(span.quote_exact)
    ):
        raise mismatch("The provenance span target has an invalid quote anchor.")


def record_human_review(
    store: TruthStore,
    *,
    document_id: str,
    attestation_id: str,
    actor: Actor,
    idempotency_key: str,
    expected_structured_head_sha256: str,
    at: str | None = None,
) -> DocumentProvenanceAttestationRecord:
    """Append a human-review successor without rewriting authorship or source.

    This command is intentionally narrower than a generic correction. It can
    review only the sole effective AI/mixed leaf at the exact current document
    head. The human click becomes the new record's basis; the predecessor keeps
    its original automatic/proposal/import basis in append-only history.
    """

    if actor.kind != "human" or not actor.ref:
        raise InvariantViolation("provenance review requires a human actor")
    document_ref = _valid_record_id(document_id, "document_id")
    prior_id = _valid_record_id(attestation_id, "attestation_id")
    key = _idempotency_key(idempotency_key)
    expected_head = _valid_digest(
        expected_structured_head_sha256,
        "expected_structured_head_sha256",
    )
    reviewer = actor_binding(actor)

    with document_lifecycle_lock(store.store_id, document_ref):
        with ydoc_store.document_lock(store, document_ref):
            with store.write_transaction() as conn:
                document = store._get_document_locked(conn, document_ref)
                if document is None:
                    raise ProvenanceReviewError(
                        f"document does not exist: {document_ref}",
                        code="provenance_review_target_mismatch",
                        status=404,
                    )
                prior = store._get_document_provenance_attestation_locked(
                    conn,
                    prior_id,
                )
                if prior is None:
                    raise ProvenanceReviewError(
                        "That provenance attestation no longer exists.",
                        code="provenance_attestation_not_found",
                        status=404,
                    )
                if prior.document_id != document_ref:
                    raise ProvenanceReviewError(
                        "The attestation does not belong to this document.",
                        code="provenance_review_target_mismatch",
                    )
                _validate_review_target_locked(
                    store,
                    conn,
                    document_id=document_ref,
                    prior=prior,
                )

                normalized = {
                    "authorship": {
                        "kind": prior.authorship_kind,
                        "contributors": json.loads(
                            prior.human_contributors_json
                        ),
                    },
                    "human_review": {
                        "status": "reviewed",
                        "reviewers": [reviewer],
                    },
                }
                source = json.loads(prior.source_json)
                identifier = _attestation_id(
                    store_id=store.store_id,
                    document_id=document_ref,
                    actor_ref=actor.ref,
                    idempotency_key=key,
                )
                expected_canonical = attestation_canonical_sha256(
                    document_id=document_ref,
                    target_kind=prior.target_kind,
                    document_version_id=prior.document_version_id,
                    document_span_id=prior.document_span_id,
                    target_structured_head_sha256=(
                        prior.target_structured_head_sha256
                    ),
                    authorship_kind=prior.authorship_kind,
                    human_contributors=normalized["authorship"]["contributors"],
                    review_status="reviewed",
                    human_reviewers=[reviewer],
                    source_kind=prior.source_kind,
                    source=source,
                    basis_kind="user_attestation",
                    basis_ref=prior.id,
                    supersedes_id=prior.id,
                    attested_by_kind=actor.kind,
                    attested_by_ref=actor.ref,
                    attested_by_meta=(dict(actor.meta) if actor.meta else None),
                )
                existing = store._get_document_provenance_attestation_locked(
                    conn,
                    identifier,
                )
                if existing is not None:
                    if existing.canonical_sha256 != expected_canonical:
                        raise InvariantViolation(
                            "idempotency_key was already used for a different "
                            "provenance attestation"
                        )
                    return existing

                if documents._lifecycle_locked(
                    store,
                    conn,
                    document.id,
                ) != "active":
                    raise ProvenanceReviewError(
                        "provenance cannot be reviewed on a retired document",
                        code="provenance_review_state_conflict",
                    )
                if not document_surface_allowed(store, document):
                    raise ProvenanceReviewError(
                        "This document is not available in Co-work for this folder",
                        code="provenance_review_forbidden",
                        status=403,
                    )
                if document.ydoc_snapshot_sha256 is None:
                    raise ProvenanceReviewError(
                        "document has no frozen structured baseline",
                        code="provenance_review_state_conflict",
                    )
                current_head = ydoc_store.current_structured_head(
                    store,
                    document_id=document.id,
                    snapshot_sha256=document.ydoc_snapshot_sha256,
                )
                if (
                    current_head != expected_head
                    or prior.target_structured_head_sha256 != current_head
                ):
                    raise ProvenanceReviewError(
                        "The provenance target changed before review was recorded.",
                        code="provenance_target_changed",
                        retryable=True,
                    )
                if (
                    prior.authorship_kind not in {"ai", "mixed"}
                    or prior.review_status not in {"not_reviewed", "unknown"}
                ):
                    raise ProvenanceReviewError(
                        "Only unreviewed AI or mixed-authored content can be "
                        "marked reviewed.",
                        code="provenance_review_ineligible",
                        status=400,
                    )

                all_rows = store._document_provenance_attestations_locked(
                    conn,
                    document_ref,
                )
                same_target = [
                    row
                    for row in all_rows
                    if row.target_kind == prior.target_kind
                    and row.document_version_id == prior.document_version_id
                    and row.document_span_id == prior.document_span_id
                ]
                if any(
                    row.target_structured_head_sha256
                    != prior.target_structured_head_sha256
                    for row in same_target
                ):
                    raise ProvenanceReviewError(
                        "The target has incompatible frozen provenance records.",
                        code="provenance_review_conflict",
                    )
                superseded = {
                    row.supersedes_id
                    for row in same_target
                    if row.supersedes_id is not None
                }
                leaves = [row.id for row in same_target if row.id not in superseded]
                if leaves != [prior.id]:
                    raise ProvenanceReviewError(
                        "The attestation is no longer the sole effective record "
                        "for this target.",
                        code="provenance_review_conflict",
                        details={"effective_attestation_ids": leaves},
                    )

                return _record_attestation_locked(
                    store,
                    conn,
                    document_id=document_ref,
                    attestation={},
                    normalized_attestation=normalized,
                    source=source,
                    actor=actor,
                    idempotency_key=key,
                    target_kind=prior.target_kind,
                    document_version_id=prior.document_version_id,
                    document_span_id=prior.document_span_id,
                    target_structured_head_sha256=(
                        prior.target_structured_head_sha256
                    ),
                    basis_kind="user_attestation",
                    basis_ref=prior.id,
                    supersedes_id=prior.id,
                    at=at,
                )


__all__ = [
    "ATTESTATION_SCHEMA",
    "AUTOMATIC_SHORT_TEXT_MAX_CHARS",
    "AUTHORSHIP_KINDS",
    "CURRENT_USER_IDENTITY_STATUSES",
    "ProvenanceActorBindingError",
    "ProvenanceConflictError",
    "ProvenanceReviewError",
    "REVIEW_STATUSES",
    "SPAN_SOURCE_BASIS_PAIRS",
    "actor_binding",
    "list_attestations",
    "normalize_attestation",
    "portable_attestation",
    "project_attestations",
    "record_document_attestation",
    "record_human_review",
    "record_span_attestation",
]
