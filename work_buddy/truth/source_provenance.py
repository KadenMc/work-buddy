"""Source-backed Truth receipts and outcome-aware provenance records.

This module is the schema-v9 integration seam for the later atomic
``truth_claim_propose_from_source`` composite.  It accepts only bounded,
content-free resolution metadata after Sources has resolved exact bytes inside
the trusted backend; it never treats an agent-supplied receipt as authority.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from work_buddy.security.actors import ActorRef, InvalidActorReference
from work_buddy.sources.models import (
    RESOLUTION_RECORD_SCHEMA,
    SourceRef,
    SourceResolutionRecord,
    canonical_sha256,
)
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import canonical_json, new_id, sha256_bytes, utc_now
from work_buddy.truth.store import TruthStore


ATTRIBUTION_ROLES = frozenset(
    {
        "semantic_producer",
        "selector",
        "candidate_preparer",
        "matcher",
        "semantic_reviser",
        "evidence_selector",
        "expression_relation_assessor",
        "applier",
        "execution_authorizer",
        "substantive_reviewer",
        "candidate_decision_actor",
        "lifecycle_decision_actor",
    }
)
CANDIDATE_DECISIONS = frozenset({"add", "connect", "dismiss"})
ATTRIBUTION_SUBJECT_KINDS = frozenset(
    {"claim", "expression", "evidence", "evidence_span"}
)
SOURCE_USAGE_STATUSES = frozenset(
    {
        "reserved",
        "acknowledgement_pending",
        "acknowledged",
        "release_pending",
        "released",
        "redaction_pending",
    }
)
LEGACY_PROVENANCE_CLASSIFICATION = "legacy_unspecified"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class EvidenceSourceResolutionRecord:
    id: str
    evidence_id: str
    source_ref_json: str
    representation_id: str
    content_sha256: str
    media_type: str
    byte_length: int
    selector_json: str | None
    resolver_id: str
    resolver_version: str
    observation_id: str
    redaction_epoch: int
    resolved_at: str
    usage_id: str
    authorization_context_sha256: str
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class SourceUsageEventRecord:
    id: str
    resolution_record_id: str
    usage_id: str
    status: str
    purpose: str
    consumer_ref: str
    redaction_epoch: int
    error_code: str | None
    canonical_sha256: str
    created_at: str
    created_by_kind: str
    created_by_ref: str | None
    created_by_meta_json: str | None


@dataclass(frozen=True, slots=True)
class ProvenanceAttributionEventRecord:
    id: str
    subject_kind: str
    subject_ref: str
    actor_ref_json: str
    role: str
    basis: str
    assurance: str
    run_ref: str | None
    source_ref_json: str | None
    asserted_at: str
    supersedes_id: str | None
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CandidateDecisionEventRecord:
    id: str
    candidate_id: str
    candidate_sha256: str
    decision: str
    claim_id: str | None
    actor_ref_json: str
    basis: str
    assurance: str
    authorization_ref: str
    authorization_context_sha256: str
    run_ref: str | None
    source_refs_json: str
    decided_at: str
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TruthOperationResultRecord:
    id: str
    operation_name: str
    idempotency_key: str
    request_sha256: str
    result_json: str
    result_sha256: str
    actor_ref_json: str
    canonical_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class IdempotentTruthResult:
    record: TruthOperationResultRecord
    created: bool


@dataclass(frozen=True, slots=True)
class ProvenanceProjection:
    classification: str
    events: tuple[ProvenanceAttributionEventRecord, ...]


def _record_id(value: str | None, label: str) -> str:
    chosen = new_id() if value is None else value
    if not isinstance(chosen, str) or _RECORD_ID_RE.fullmatch(chosen) is None:
        raise InvariantViolation(f"{label} must be 32 lowercase hex characters")
    return chosen


def _text(value: str, label: str, *, limit: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or "\x00" in value
    ):
        raise InvariantViolation(f"{label} must be bounded nonempty text")
    return value.strip()


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvariantViolation(f"{label} must be a lowercase SHA-256 digest")
    return value


def _actor_json(actor: ActorRef) -> str:
    if not isinstance(actor, ActorRef):
        raise InvariantViolation("provenance actor must be an issuer-qualified ActorRef")
    return canonical_json(actor.to_dict())


def _source_json(source_ref: SourceRef | None) -> str | None:
    if source_ref is None:
        return None
    if not isinstance(source_ref, SourceRef):
        raise InvariantViolation("source_ref must be an authority-qualified SourceRef")
    return canonical_json(source_ref.to_dict())


def _actor_columns(actor: ActorRef) -> tuple[str, str | None, str]:
    legacy_kind = actor.kind if actor.kind in {"human", "agent_run", "system"} else "system"
    return legacy_kind, actor.canonical_id, canonical_json({"actor_ref": actor.to_dict()})


def _row(record_type: type[Any], row: sqlite3.Row | None) -> Any | None:
    return None if row is None else record_type(**dict(row))


def resolution_record_payload(
    *,
    evidence_id: str,
    resolution: SourceResolutionRecord,
    usage_id: str,
    authorization_context_sha256: str,
) -> dict[str, Any]:
    """Return the canonical, content-free portable resolution payload."""

    if resolution.schema != RESOLUTION_RECORD_SCHEMA:
        raise InvariantViolation("source resolution uses an unsupported schema")
    if resolution.excerpt is not None:
        raise InvariantViolation(
            "Truth resolution receipts must not duplicate source content"
        )
    if isinstance(resolution.byte_length, bool) or resolution.byte_length < 0:
        raise InvariantViolation("source resolution byte_length is invalid")
    if isinstance(resolution.redaction_epoch, bool) or resolution.redaction_epoch < 0:
        raise InvariantViolation("source resolution redaction_epoch is invalid")
    _digest(resolution.content_sha256, "resolution content_sha256")
    _digest(authorization_context_sha256, "authorization_context_sha256")
    selector = None if resolution.selector is None else dict(resolution.selector)
    try:
        canonical_json(selector)
    except (TypeError, ValueError) as exc:
        raise InvariantViolation("source resolution selector is not canonical JSON") from exc
    return {
        "schema": resolution.schema,
        "evidence_id": _record_id(evidence_id, "evidence_id"),
        "source_ref": resolution.source_ref.to_dict(),
        "representation_id": _text(
            resolution.representation_id, "representation_id"
        ),
        "content_sha256": resolution.content_sha256,
        "media_type": _text(resolution.media_type, "media_type", limit=256),
        "byte_length": resolution.byte_length,
        "selector": selector,
        "resolver_id": _text(resolution.resolver_id, "resolver_id", limit=256),
        "resolver_version": _text(
            resolution.resolver_version, "resolver_version", limit=256
        ),
        "observation_id": _text(
            resolution.observation_id, "observation_id", limit=256
        ),
        "redaction_epoch": resolution.redaction_epoch,
        "resolved_at": _text(resolution.resolved_at, "resolved_at", limit=128),
        "usage_id": _text(usage_id, "usage_id", limit=256),
        "authorization_context_sha256": authorization_context_sha256,
    }


def record_evidence_source_resolution(
    store: TruthStore,
    *,
    evidence_id: str,
    resolution: SourceResolutionRecord,
    usage_id: str,
    authorization_context_sha256: str,
    actor: ActorRef,
    record_id: str | None = None,
    created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> EvidenceSourceResolutionRecord:
    """Persist a portable resolution receipt beside copied Truth evidence."""

    identifier = _record_id(record_id, "resolution record id")
    payload = resolution_record_payload(
        evidence_id=evidence_id,
        resolution=resolution,
        usage_id=usage_id,
        authorization_context_sha256=authorization_context_sha256,
    )
    canonical = canonical_sha256(payload)
    created = created_at or utc_now()
    actor_kind, actor_ref, actor_meta = _actor_columns(actor)
    record = EvidenceSourceResolutionRecord(
        id=identifier,
        evidence_id=str(payload["evidence_id"]),
        source_ref_json=canonical_json(payload["source_ref"]),
        representation_id=str(payload["representation_id"]),
        content_sha256=str(payload["content_sha256"]),
        media_type=str(payload["media_type"]),
        byte_length=int(payload["byte_length"]),
        selector_json=(
            None if payload["selector"] is None else canonical_json(payload["selector"])
        ),
        resolver_id=str(payload["resolver_id"]),
        resolver_version=str(payload["resolver_version"]),
        observation_id=str(payload["observation_id"]),
        redaction_epoch=int(payload["redaction_epoch"]),
        resolved_at=str(payload["resolved_at"]),
        usage_id=str(payload["usage_id"]),
        authorization_context_sha256=str(payload["authorization_context_sha256"]),
        canonical_sha256=canonical,
        created_at=created,
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
    )
    with store.write_transaction(conn) as write_conn:
        evidence = store._get_evidence_locked(write_conn, record.evidence_id)
        if evidence is None:
            raise InvariantViolation("resolution receipt evidence does not exist")
        if evidence.content_sha256 != record.content_sha256:
            raise InvariantViolation(
                "resolution receipt digest does not match copied Truth evidence"
            )
        existing = write_conn.execute(
            "SELECT * FROM evidence_source_resolution_records "
            "WHERE id = ? OR canonical_sha256 = ? OR usage_id = ? ORDER BY id",
            (record.id, record.canonical_sha256, record.usage_id),
        ).fetchall()
        if existing:
            first = EvidenceSourceResolutionRecord(**dict(existing[0]))
            if first.canonical_sha256 != record.canonical_sha256:
                raise InvariantViolation(
                    "source resolution identity was reused for another resolution"
                )
            return first
        write_conn.execute(
            "INSERT INTO evidence_source_resolution_records "
            "(id, evidence_id, source_ref_json, representation_id, content_sha256, "
            "media_type, byte_length, selector_json, resolver_id, resolver_version, "
            "observation_id, redaction_epoch, resolved_at, usage_id, "
            "authorization_context_sha256, canonical_sha256, created_at, "
            "created_by_kind, created_by_ref, created_by_meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(getattr(record, field) for field in record.__dataclass_fields__),
        )
        store._insert_ledger_record_locked(
            write_conn, "evidence_source_resolution", record.id
        )
        return record


def record_source_usage_event(
    store: TruthStore,
    *,
    resolution_record_id: str,
    usage_id: str,
    status: str,
    purpose: str,
    consumer_ref: str,
    redaction_epoch: int,
    actor: ActorRef,
    error_code: str | None = None,
    record_id: str | None = None,
    created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> SourceUsageEventRecord:
    if status not in SOURCE_USAGE_STATUSES:
        raise InvariantViolation("source usage event has an invalid status")
    if isinstance(redaction_epoch, bool) or redaction_epoch < 0:
        raise InvariantViolation("source usage redaction_epoch is invalid")
    identifier = _record_id(record_id, "source usage event id")
    created = created_at or utc_now()
    payload = {
        "resolution_record_id": _record_id(
            resolution_record_id, "resolution_record_id"
        ),
        "usage_id": _text(usage_id, "usage_id", limit=256),
        "status": status,
        "purpose": _text(purpose, "purpose", limit=256),
        "consumer_ref": _text(consumer_ref, "consumer_ref", limit=512),
        "redaction_epoch": redaction_epoch,
        "error_code": (
            None if error_code is None else _text(error_code, "error_code", limit=128)
        ),
        "created_at": created,
    }
    actor_kind, actor_ref, actor_meta = _actor_columns(actor)
    record = SourceUsageEventRecord(
        id=identifier,
        canonical_sha256=canonical_sha256(payload),
        created_by_kind=actor_kind,
        created_by_ref=actor_ref,
        created_by_meta_json=actor_meta,
        **payload,
    )
    with store.write_transaction(conn) as write_conn:
        resolution = write_conn.execute(
            "SELECT usage_id, redaction_epoch FROM evidence_source_resolution_records "
            "WHERE id = ?",
            (record.resolution_record_id,),
        ).fetchone()
        if resolution is None:
            raise InvariantViolation("source usage resolution receipt does not exist")
        if (
            resolution["usage_id"] != record.usage_id
            or int(resolution["redaction_epoch"]) != record.redaction_epoch
        ):
            raise InvariantViolation("source usage event does not match its resolution")
        existing = write_conn.execute(
            "SELECT * FROM truth_source_usage_events WHERE id = ?",
            (record.id,),
        ).fetchone()
        if existing is not None:
            prior = SourceUsageEventRecord(**dict(existing))
            if prior.canonical_sha256 != record.canonical_sha256:
                raise InvariantViolation(
                    "source usage event identity was reused with another payload"
                )
            return prior
        write_conn.execute(
            "INSERT INTO truth_source_usage_events "
            "(id, resolution_record_id, usage_id, status, purpose, consumer_ref, "
            "redaction_epoch, error_code, canonical_sha256, created_at, "
            "created_by_kind, created_by_ref, created_by_meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(getattr(record, field) for field in record.__dataclass_fields__),
        )
        store._insert_ledger_record_locked(write_conn, "truth_source_usage_event", record.id)
        return record


def record_attribution_event(
    store: TruthStore,
    *,
    subject_kind: str,
    subject_ref: str,
    actor: ActorRef,
    role: str,
    basis: str,
    assurance: str,
    run_ref: str | None = None,
    source_ref: SourceRef | None = None,
    supersedes_id: str | None = None,
    asserted_at: str | None = None,
    record_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> ProvenanceAttributionEventRecord:
    if subject_kind not in ATTRIBUTION_SUBJECT_KINDS:
        raise InvariantViolation("attribution subject kind is invalid")
    if role not in ATTRIBUTION_ROLES:
        raise InvariantViolation("attribution role is invalid")
    subject = _record_id(subject_ref, "attribution subject_ref")
    identifier = _record_id(record_id, "attribution event id")
    asserted = asserted_at or utc_now()
    actor_json = _actor_json(actor)
    source_json = _source_json(source_ref)
    payload = {
        "subject_kind": subject_kind,
        "subject_ref": subject,
        "actor_ref": json.loads(actor_json),
        "role": role,
        "basis": _text(basis, "basis", limit=256),
        "assurance": _text(assurance, "assurance", limit=256),
        "run_ref": None if run_ref is None else _text(run_ref, "run_ref", limit=512),
        "source_ref": None if source_json is None else json.loads(source_json),
        "asserted_at": asserted,
        "supersedes_id": (
            None
            if supersedes_id is None
            else _record_id(supersedes_id, "supersedes_id")
        ),
    }
    record = ProvenanceAttributionEventRecord(
        id=identifier,
        actor_ref_json=actor_json,
        source_ref_json=source_json,
        canonical_sha256=canonical_sha256(payload),
        created_at=asserted,
        **{key: value for key, value in payload.items() if key not in {"actor_ref", "source_ref"}},
    )
    with store.write_transaction(conn) as write_conn:
        _require_subject(write_conn, subject_kind, subject)
        existing = write_conn.execute(
            "SELECT * FROM provenance_attribution_events WHERE id = ?",
            (record.id,),
        ).fetchone()
        if existing is not None:
            prior = ProvenanceAttributionEventRecord(**dict(existing))
            if prior.canonical_sha256 != record.canonical_sha256:
                raise InvariantViolation(
                    "attribution event identity was reused with another payload"
                )
            return prior
        if record.supersedes_id is not None:
            prior = write_conn.execute(
                "SELECT subject_kind, subject_ref, role FROM provenance_attribution_events "
                "WHERE id = ?",
                (record.supersedes_id,),
            ).fetchone()
            if prior is None:
                raise InvariantViolation("superseded attribution event does not exist")
            if tuple(prior) != (subject_kind, subject, role):
                raise InvariantViolation(
                    "an attribution supersession cannot change subject or role"
                )
        write_conn.execute(
            "INSERT INTO provenance_attribution_events "
            "(id, subject_kind, subject_ref, actor_ref_json, role, basis, assurance, "
            "run_ref, source_ref_json, asserted_at, supersedes_id, canonical_sha256, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(getattr(record, field) for field in record.__dataclass_fields__),
        )
        store._insert_ledger_record_locked(write_conn, "provenance_attribution_event", record.id)
        return record


def record_candidate_decision(
    store: TruthStore,
    *,
    candidate_id: str,
    candidate_sha256: str,
    decision: str,
    claim_id: str | None,
    actor: ActorRef,
    basis: str,
    assurance: str,
    authorization_ref: str,
    authorization_context_sha256: str,
    source_refs: Sequence[SourceRef] = (),
    run_ref: str | None = None,
    decided_at: str | None = None,
    record_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> CandidateDecisionEventRecord:
    if decision not in CANDIDATE_DECISIONS:
        raise InvariantViolation("candidate decision is invalid")
    if (decision == "dismiss") != (claim_id is None):
        raise InvariantViolation(
            "add/connect decisions require a claim; dismiss decisions must not assign one"
        )
    identifier = _record_id(record_id, "candidate decision id")
    claim = None if claim_id is None else _record_id(claim_id, "claim_id")
    source_values = [source.to_dict() for source in source_refs]
    # Ordered disclosure/source context is meaningful; duplicates are not.
    if len({canonical_json(item) for item in source_values}) != len(source_values):
        raise InvariantViolation("candidate decision source_refs contain duplicates")
    decided = decided_at or utc_now()
    actor_json = _actor_json(actor)
    payload = {
        "candidate_id": _text(candidate_id, "candidate_id", limit=512),
        "candidate_sha256": _digest(candidate_sha256, "candidate_sha256"),
        "decision": decision,
        "claim_id": claim,
        "actor_ref": json.loads(actor_json),
        "basis": _text(basis, "basis", limit=256),
        "assurance": _text(assurance, "assurance", limit=256),
        "authorization_ref": _text(
            authorization_ref, "authorization_ref", limit=512
        ),
        "authorization_context_sha256": _digest(
            authorization_context_sha256, "authorization_context_sha256"
        ),
        "run_ref": None if run_ref is None else _text(run_ref, "run_ref", limit=512),
        "source_refs": source_values,
        "decided_at": decided,
    }
    record = CandidateDecisionEventRecord(
        id=identifier,
        actor_ref_json=actor_json,
        source_refs_json=canonical_json(source_values),
        canonical_sha256=canonical_sha256(payload),
        created_at=decided,
        **{key: value for key, value in payload.items() if key not in {"actor_ref", "source_refs"}},
    )
    with store.write_transaction(conn) as write_conn:
        if record.claim_id is not None and store._get_claim_locked(
            write_conn, record.claim_id
        ) is None:
            raise InvariantViolation("candidate decision claim does not exist")
        existing = write_conn.execute(
            "SELECT * FROM candidate_decision_events WHERE id = ?",
            (record.id,),
        ).fetchone()
        if existing is not None:
            prior = CandidateDecisionEventRecord(**dict(existing))
            if prior.canonical_sha256 != record.canonical_sha256:
                raise InvariantViolation(
                    "candidate decision identity was reused with another payload"
                )
            return prior
        write_conn.execute(
            "INSERT INTO candidate_decision_events "
            "(id, candidate_id, candidate_sha256, decision, claim_id, actor_ref_json, "
            "basis, assurance, authorization_ref, authorization_context_sha256, "
            "run_ref, source_refs_json, decided_at, canonical_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(getattr(record, field) for field in record.__dataclass_fields__),
        )
        store._insert_ledger_record_locked(write_conn, "candidate_decision_event", record.id)
        return record


def record_operation_result(
    store: TruthStore,
    *,
    operation_name: str,
    idempotency_key: str,
    request_sha256: str,
    result: Mapping[str, Any],
    actor: ActorRef,
    record_id: str | None = None,
    created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> IdempotentTruthResult:
    identifier = _record_id(record_id, "operation result id")
    created = created_at or utc_now()
    result_json = canonical_json(dict(result))
    result_digest = sha256_bytes(result_json.encode("utf-8"))
    actor_json = _actor_json(actor)
    payload = {
        "operation_name": _text(operation_name, "operation_name", limit=256),
        "idempotency_key": _text(idempotency_key, "idempotency_key", limit=512),
        "request_sha256": _digest(request_sha256, "request_sha256"),
        "result_sha256": result_digest,
        "actor_ref": json.loads(actor_json),
    }
    record = TruthOperationResultRecord(
        id=identifier,
        result_json=result_json,
        result_sha256=result_digest,
        actor_ref_json=actor_json,
        canonical_sha256=canonical_sha256(payload),
        created_at=created,
        **{key: value for key, value in payload.items() if key != "actor_ref" and key != "result_sha256"},
    )
    with store.write_transaction(conn) as write_conn:
        existing = write_conn.execute(
            "SELECT * FROM truth_operation_results "
            "WHERE operation_name = ? AND idempotency_key = ?",
            (record.operation_name, record.idempotency_key),
        ).fetchone()
        if existing is not None:
            prior = TruthOperationResultRecord(**dict(existing))
            if prior.request_sha256 != record.request_sha256:
                raise InvariantViolation(
                    "truth idempotency key was reused with a different request"
                )
            return IdempotentTruthResult(prior, created=False)
        write_conn.execute(
            "INSERT INTO truth_operation_results "
            "(id, operation_name, idempotency_key, request_sha256, result_json, "
            "result_sha256, actor_ref_json, canonical_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(getattr(record, field) for field in record.__dataclass_fields__),
        )
        store._insert_ledger_record_locked(write_conn, "truth_operation_result", record.id)
        return IdempotentTruthResult(record, created=True)


def provenance_for_subject(
    store: TruthStore,
    *,
    subject_kind: str,
    subject_ref: str,
    conn: sqlite3.Connection | None = None,
) -> ProvenanceProjection:
    """Return explicit events or a conservative legacy classification."""

    if subject_kind not in ATTRIBUTION_SUBJECT_KINDS:
        raise InvariantViolation("attribution subject kind is invalid")
    subject = _record_id(subject_ref, "subject_ref")
    sql = (
        "SELECT * FROM provenance_attribution_events "
        "WHERE subject_kind = ? AND subject_ref = ? ORDER BY asserted_at, id"
    )
    if conn is None:
        with store._read_connection() as read_conn:
            _require_subject(read_conn, subject_kind, subject)
            rows = read_conn.execute(sql, (subject_kind, subject)).fetchall()
    else:
        store._validate_connection_target(conn)
        _require_subject(conn, subject_kind, subject)
        rows = conn.execute(sql, (subject_kind, subject)).fetchall()
    events = tuple(ProvenanceAttributionEventRecord(**dict(row)) for row in rows)
    return ProvenanceProjection(
        classification=("attributed" if events else LEGACY_PROVENANCE_CLASSIFICATION),
        events=events,
    )


def _require_subject(
    conn: sqlite3.Connection, subject_kind: str, subject_ref: str
) -> None:
    table = {
        "claim": "claims",
        "expression": "expressions",
        "evidence": "evidence",
        "evidence_span": "evidence_spans",
    }[subject_kind]
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (subject_ref,)).fetchone() is None:
        raise InvariantViolation("attribution subject does not exist")


def validate_actor_ref_json(value: str, label: str = "actor_ref_json") -> ActorRef:
    try:
        raw = json.loads(value)
        return ActorRef.from_dict(raw)
    except (json.JSONDecodeError, InvalidActorReference, TypeError) as exc:
        raise InvariantViolation(f"{label} is not a valid ActorRef") from exc


def validate_source_ref_json(value: str, label: str = "source_ref_json") -> SourceRef:
    try:
        raw = json.loads(value)
        return SourceRef.from_dict(raw)
    except Exception as exc:
        raise InvariantViolation(f"{label} is not a valid SourceRef") from exc


__all__ = [
    "ATTRIBUTION_ROLES",
    "ATTRIBUTION_SUBJECT_KINDS",
    "CANDIDATE_DECISIONS",
    "LEGACY_PROVENANCE_CLASSIFICATION",
    "SOURCE_USAGE_STATUSES",
    "CandidateDecisionEventRecord",
    "EvidenceSourceResolutionRecord",
    "IdempotentTruthResult",
    "ProvenanceAttributionEventRecord",
    "ProvenanceProjection",
    "SourceUsageEventRecord",
    "TruthOperationResultRecord",
    "provenance_for_subject",
    "record_attribution_event",
    "record_candidate_decision",
    "record_evidence_source_resolution",
    "record_operation_result",
    "record_source_usage_event",
    "resolution_record_payload",
    "validate_actor_ref_json",
    "validate_source_ref_json",
]
