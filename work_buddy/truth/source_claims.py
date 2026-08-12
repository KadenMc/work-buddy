"""Atomic source-backed claim proposal service.

Sources owns retained bytes, access and redaction epochs.  Truth copies the
portable evidence snapshot and receipts it needs in one local transaction.
Resolution and the final Sources redaction-epoch recheck happen before the
Truth write lock; acknowledgement happens after the Truth commit.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

from work_buddy.security.actors import ActorRef
from work_buddy.sources import (
    SourceRef,
    SourceStore,
    resolve_and_reserve_source,
)
from work_buddy.sources.models import ResolvedSource, UsageReservation
from work_buddy.truth import queries
from work_buddy.truth.anchors import CompositeSelector, reanchor, serialize_selector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.evidence_relations import validate_claim_evidence_role
from work_buddy.truth.identity import (
    canonical_json,
    claim_sha256,
    new_id,
    sha256_bytes,
    sha256_text,
)
from work_buddy.truth.source_provenance import (
    CandidateDecisionEventRecord,
    EvidenceSourceResolutionRecord,
    ProvenanceAttributionEventRecord,
    SourceUsageEventRecord,
    record_attribution_event,
    record_candidate_decision,
    record_evidence_source_resolution,
    record_operation_result,
    record_source_usage_event,
)
from work_buddy.truth.store import (
    AcquisitionOrigin,
    ClaimRecord,
    EvidenceRecord,
    EvidenceSpanRecord,
    TruthStore,
)


OPERATION_NAME = "truth_claim_propose_from_source"
SOURCE_PURPOSE = "truth_claim_proposal"
PROJECTION_SOURCE_PURPOSE = "truth_hindsight_projection"
PROJECTION_CONSUMER_DOMAIN = "hindsight_projection"
PROJECTION_PRINCIPAL_SUBJECT = "work-buddy-truth-service"
_SELECTOR_KEYS = frozenset({"exact", "prefix", "suffix", "start", "end"})
_EVIDENCE_KIND_BY_SOURCE_ROLE = {
    "conversation_message": "chat",
    "document_selection": "document",
    "fetched_passage": "web",
    "human_input": "utterance",
    "audio": "artifact",
    "transcript": "utterance",
    "agent_output": "artifact",
    "derived_content": "artifact",
    "imported_file": "artifact",
}
_ACQUISITION_BY_SOURCE_ROLE = {
    "conversation_message": "said_in_chat",
    "fetched_passage": "fetch",
    "imported_file": "import",
}


@dataclass(frozen=True, slots=True)
class SourceClaimCandidate:
    proposition: str
    claim_kind: str
    selector: Mapping[str, Any]
    evidential_effect: str
    derivation_relationship: str
    structured: Mapping[str, Any] | None = None
    scope: str = "store"
    valid_from: str | None = None
    valid_to: str | None = None
    confidence_extraction: float | None = None
    relation_diagnostics: Mapping[str, Any] | None = None
    candidate_id: str | None = None
    candidate_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class SourceClaimActors:
    semantic_producer: ActorRef
    selector: ActorRef
    applier: ActorRef
    producer_meta: Mapping[str, Any] = field(default_factory=dict)
    candidate_preparer: ActorRef | None = None
    matcher: ActorRef | None = None
    execution_authorizer: ActorRef | None = None
    substantive_reviewer: ActorRef | None = None
    semantic_reviser: ActorRef | None = None
    evidence_selector: ActorRef | None = None
    expression_relation_assessor: ActorRef | None = None
    run_ref: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateDecisionAuthorization:
    decision: str
    actor: ActorRef
    basis: str
    assurance: str
    authorization_ref: str
    authorization_context_sha256: str
    decided_at: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedSourceClaim:
    source_ref: SourceRef
    source_role: str
    reservation: UsageReservation
    resolved: ResolvedSource
    text: str
    selector: CompositeSelector
    selector_value: Mapping[str, Any]
    request_sha256: str
    idempotency_key: str
    consumer_ref: str
    evidence_id: str
    span_id: str
    resolution_record_id: str
    blob_created: bool


@dataclass(frozen=True, slots=True)
class SourceClaimWrite:
    claim: ClaimRecord
    claim_created: bool
    evidence: EvidenceRecord
    span: EvidenceSpanRecord
    relation_id: str
    resolution: EvidenceSourceResolutionRecord
    usage_event: SourceUsageEventRecord
    attribution_events: tuple[ProvenanceAttributionEventRecord, ...]
    candidate_decision: CandidateDecisionEventRecord | None
    operation_result_id: str
    operation_result_created: bool


@dataclass(frozen=True, slots=True)
class SourceClaimProposalResult:
    claim_id: str
    claim_created: bool
    claim_canonical_sha256: str
    evidence_id: str
    span_id: str
    relation_id: str
    resolution_record_id: str
    source_ref: str
    usage_id: str
    usage_status: str
    operation_result_id: str
    candidate_decision_id: str | None
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_created": self.claim_created,
            "claim_canonical_sha256": self.claim_canonical_sha256,
            "evidence_id": self.evidence_id,
            "span_id": self.span_id,
            "relation_id": self.relation_id,
            "resolution_record_id": self.resolution_record_id,
            "source_ref": self.source_ref,
            "usage_id": self.usage_id,
            "usage_status": self.usage_status,
            "operation_result_id": self.operation_result_id,
            "candidate_decision_id": self.candidate_decision_id,
            "replayed": self.replayed,
        }


def _stable_id(domain: str, value: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json({"domain": domain, **dict(value)}))[:32]


def _validate_digest(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise InvariantViolation(f"{label} must be a lowercase SHA-256 digest")
    return value


def _selector(value: Mapping[str, Any]) -> CompositeSelector:
    if not isinstance(value, Mapping) or set(value) - _SELECTOR_KEYS:
        raise InvariantViolation("source selector fields are invalid")
    return CompositeSelector(
        exact=value.get("exact"),
        prefix=value.get("prefix", ""),
        suffix=value.get("suffix", ""),
        start=value.get("start"),
        end=value.get("end"),
    )


def _selector_value(selector: CompositeSelector) -> dict[str, Any]:
    value: dict[str, Any] = {
        "exact": selector.exact,
        "prefix": selector.prefix,
        "suffix": selector.suffix,
    }
    if selector.start is not None:
        value["start"] = selector.start
    if selector.end is not None:
        value["end"] = selector.end
    return value


def _candidate_payload(candidate: SourceClaimCandidate) -> dict[str, Any]:
    role = validate_claim_evidence_role(
        {
            "schema": "claim-evidence/v1",
            "evidential_effect": candidate.evidential_effect,
            "derivation_relationship": candidate.derivation_relationship,
            **(
                {}
                if candidate.relation_diagnostics is None
                else {"diagnostics": dict(candidate.relation_diagnostics)}
            ),
        }
    ).to_role()
    selector = _selector(candidate.selector)
    selector_value = _selector_value(selector)
    payload = {
        "proposition": candidate.proposition,
        "claim_kind": candidate.claim_kind,
        "structured": (
            None if candidate.structured is None else dict(candidate.structured)
        ),
        "scope": candidate.scope,
        "valid_from": candidate.valid_from,
        "valid_to": candidate.valid_to,
        "confidence_extraction": candidate.confidence_extraction,
        "selector": selector_value,
        "evidence_relation": role,
        "candidate_id": candidate.candidate_id,
        "candidate_sha256": candidate.candidate_sha256,
    }
    # Exercise the canonical claim validator before resolving protected bytes.
    claim_sha256(
        proposition=candidate.proposition,
        claim_kind=candidate.claim_kind,
        structured=candidate.structured,
        scope=candidate.scope,
        valid_from=candidate.valid_from,
        valid_to=candidate.valid_to,
    )
    _validate_digest(candidate.candidate_sha256, "candidate_sha256")
    return payload


def source_claim_request_sha256(
    *,
    source_ref: SourceRef,
    representation_id: str,
    expected_content_sha256: str,
    expected_native_revision: str | None,
    candidate: SourceClaimCandidate,
    actors: SourceClaimActors,
    existing_claim_id: str | None,
    decision: CandidateDecisionAuthorization | None,
) -> str:
    candidate_value = _candidate_payload(candidate)
    _validate_digest(expected_content_sha256, "expected_content_sha256")
    actor_value = {
        "semantic_producer": actors.semantic_producer.to_dict(),
        "selector": actors.selector.to_dict(),
        "applier": actors.applier.to_dict(),
        "producer_meta": dict(actors.producer_meta),
        "candidate_preparer": (
            None
            if actors.candidate_preparer is None
            else actors.candidate_preparer.to_dict()
        ),
        "matcher": None if actors.matcher is None else actors.matcher.to_dict(),
        "execution_authorizer": (
            None
            if actors.execution_authorizer is None
            else actors.execution_authorizer.to_dict()
        ),
        "substantive_reviewer": (
            None
            if actors.substantive_reviewer is None
            else actors.substantive_reviewer.to_dict()
        ),
        "semantic_reviser": (
            None
            if actors.semantic_reviser is None
            else actors.semantic_reviser.to_dict()
        ),
        "evidence_selector": (
            None
            if actors.evidence_selector is None
            else actors.evidence_selector.to_dict()
        ),
        "expression_relation_assessor": (
            None
            if actors.expression_relation_assessor is None
            else actors.expression_relation_assessor.to_dict()
        ),
        "run_ref": actors.run_ref,
    }
    decision_value = None
    if decision is not None:
        decision_value = {
            "decision": decision.decision,
            "actor": decision.actor.to_dict(),
            "basis": decision.basis,
            "assurance": decision.assurance,
            "authorization_ref": decision.authorization_ref,
            "authorization_context_sha256": decision.authorization_context_sha256,
            "decided_at": decision.decided_at,
        }
    return sha256_text(
        canonical_json(
            {
                "schema": "wb.truth-claim-from-source-request/v1",
                "source_ref": source_ref.to_dict(),
                "representation_id": representation_id,
                "expected_content_sha256": expected_content_sha256,
                "expected_native_revision": expected_native_revision,
                "candidate": candidate_value,
                "actors": actor_value,
                "existing_claim_id": existing_claim_id,
                "candidate_decision": decision_value,
            }
        )
    )


def _legacy_actor(actor: ActorRef, *, producer_meta: Mapping[str, Any] | None = None) -> Actor:
    kind = actor.kind if actor.kind in {"human", "agent_run", "system"} else "system"
    return Actor(kind, actor.canonical_id, dict(producer_meta or {}))


def _assert_actor_context(actors: SourceClaimActors) -> None:
    values = [
        actors.semantic_producer,
        actors.selector,
        actors.applier,
        actors.candidate_preparer,
        actors.matcher,
        actors.execution_authorizer,
        actors.substantive_reviewer,
        actors.semantic_reviser,
        actors.evidence_selector,
        actors.expression_relation_assessor,
    ]
    tenant_ids = {actor.tenant_scope_id for actor in values if actor is not None}
    if len(tenant_ids) != 1:
        raise InvariantViolation("source-backed Truth actors cross tenant boundaries")
    if actors.semantic_producer.kind == "agent_run":
        # Truth's compatibility columns still require the historical producer
        # fields. Explicit v9 attribution remains the canonical role model.
        _legacy_actor(actors.semantic_producer, producer_meta=actors.producer_meta)


def _author_from_source(resolved: ResolvedSource) -> tuple[str, str | None]:
    authors = {
        assertion.actor
        for assertion in resolved.attributions
        if assertion.role == "author"
        and assertion.state == "identified"
        and assertion.actor is not None
    }
    if len(authors) != 1:
        return "unknown", None
    author = next(iter(authors))
    if author.kind == "human":
        return "human", author.canonical_id
    if author.kind == "agent_run":
        return "agent_run", author.canonical_id
    return "unknown", None


def _grant_projection_metadata_access(
    source_store: SourceStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    content_sha256: str,
    source_principal: ActorRef,
) -> None:
    """Authorize Truth's projection worker to register one source dependency.

    The worker receives metadata access only, for one exact representation and
    one consumer domain.  Its principal is derived from the already-authorized
    Truth principal's issuer and tenant rather than accepted from a caller.
    A deterministic binding makes source-backed proposal replay idempotent;
    revoking that binding remains effective because replay does not mint a new
    identifier.
    """

    principal = ActorRef(
        issuer_authority_id=source_principal.issuer_authority_id,
        subject=PROJECTION_PRINCIPAL_SUBJECT,
        kind="service",
        tenant_scope_id=source_principal.tenant_scope_id,
    )
    authorization = {
        "schema": "wb.truth-hindsight-source-binding/v1",
        "source_ref": source_ref.to_dict(),
        "representation_id": representation_id,
        "content_sha256": content_sha256,
        "principal": principal.to_dict(),
        "purpose": PROJECTION_SOURCE_PURPOSE,
        "consumer_domain": PROJECTION_CONSUMER_DOMAIN,
        "access_mode": "metadata",
    }
    binding_id = _stable_id(
        "work-buddy.truth-hindsight-source-binding/v1", authorization
    )
    try:
        source_store.grant_access(
            source_ref=source_ref,
            principal=principal,
            purpose=PROJECTION_SOURCE_PURPOSE,
            access_mode="metadata",
            authorization_fingerprint=sha256_text(canonical_json(authorization)),
            scope={
                "consumer_domain": PROJECTION_CONSUMER_DOMAIN,
                "use_kind": "semantic_derivative",
            },
            trusted_service_id=PROJECTION_PRINCIPAL_SUBJECT,
            content_boundary={"representation_id": representation_id},
            binding_id=binding_id,
        )
    except sqlite3.IntegrityError:
        # The deterministic identifier represents the complete immutable
        # grant. Exact replay is a no-op. A revoked grant is intentionally not
        # replaced, so operator revocation remains authoritative.
        return


def _result_from_record(record: Any, *, usage_status: str, replayed: bool) -> SourceClaimProposalResult:
    value = json.loads(record.result_json)
    return SourceClaimProposalResult(
        claim_id=str(value["claim_id"]),
        claim_created=bool(value["claim_created"]),
        claim_canonical_sha256=str(value["claim_canonical_sha256"]),
        evidence_id=str(value["evidence_id"]),
        span_id=str(value["span_id"]),
        relation_id=str(value["relation_id"]),
        resolution_record_id=str(value["resolution_record_id"]),
        source_ref=str(value["source_ref"]),
        usage_id=str(value["usage_id"]),
        usage_status=usage_status,
        operation_result_id=record.id,
        candidate_decision_id=value.get("candidate_decision_id"),
        replayed=replayed,
    )


def _current_usage_status(store: TruthStore, usage_id: str) -> str:
    with store._read_connection() as conn:
        row = conn.execute(
            "SELECT status FROM truth_source_usage_events WHERE usage_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (usage_id,),
        ).fetchone()
    return "reserved" if row is None else str(row["status"])


def _reconcile_operation_usage(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    operation: Any,
    actor: ActorRef,
) -> str:
    """Recover the Sources acknowledgement for one committed Truth result."""

    value = json.loads(operation.result_json)
    usage_id = str(value["usage_id"])
    current = _current_usage_status(truth_store, usage_id)
    if current not in {"reserved", "acknowledgement_pending"}:
        return current
    resolution_record_id = str(value["resolution_record_id"])
    with truth_store._read_connection() as conn:
        row = conn.execute(
            "SELECT consumer_ref,redaction_epoch FROM truth_source_usage_events "
            "WHERE usage_id=? AND resolution_record_id=? "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            (usage_id, resolution_record_id),
        ).fetchone()
    if row is None:
        raise InvariantViolation("committed source operation has no usage recovery receipt")
    consumer_ref = str(row["consumer_ref"])
    redaction_epoch = int(row["redaction_epoch"])
    if value.get("consumer_ref") not in {None, consumer_ref} or value.get(
        "redaction_epoch"
    ) not in {None, redaction_epoch}:
        raise InvariantViolation("committed source operation recovery metadata conflicts")
    return reconcile_source_usage(
        truth_store,
        source_store,
        resolution_record_id=resolution_record_id,
        usage_id=usage_id,
        redaction_epoch=redaction_epoch,
        consumer_ref=consumer_ref,
        actor=actor,
    )


def prepare_source_claim(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    expected_content_sha256: str,
    expected_native_revision: str | None,
    source_principal: ActorRef,
    candidate: SourceClaimCandidate,
    actors: SourceClaimActors,
    idempotency_key: str,
    existing_claim_id: str | None = None,
    decision: CandidateDecisionAuthorization | None = None,
    purpose: str = SOURCE_PURPOSE,
) -> PreparedSourceClaim:
    """Resolve/reserve source content and stage any Truth blob before its lock."""

    _assert_actor_context(actors)
    if source_principal.tenant_scope_id != actors.applier.tenant_scope_id:
        raise InvariantViolation("source principal is outside the Truth actor tenant")
    request_sha = source_claim_request_sha256(
        source_ref=source_ref,
        representation_id=representation_id,
        expected_content_sha256=expected_content_sha256,
        expected_native_revision=expected_native_revision,
        candidate=candidate,
        actors=actors,
        existing_claim_id=existing_claim_id,
        decision=decision,
    )
    # Every failed attempt gets a releasable Sources usage. Truth's own
    # idempotency key remains stable and serializes the canonical winner.
    consumer_ref = f"{idempotency_key}:{new_id()}"
    reserved = resolve_and_reserve_source(
        source_store,
        source_ref=source_ref,
        representation_id=representation_id,
        principal=source_principal,
        purpose=purpose,
        consumer_domain="truth",
        consumer_id=consumer_ref,
        use_kind="evidence_snapshot",
        disclosure_kind="managed_copy",
        redaction_policy="cascade",
        selector=dict(candidate.selector),
        expected_digest=expected_content_sha256,
    )
    blob_created = False
    try:
        if (
            expected_native_revision is not None
            and reserved.resolved.native_revision != expected_native_revision
        ):
            raise InvariantViolation(
                "source native revision changed before resolution"
            )
        try:
            text = reserved.resolved.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvariantViolation(
                "source representation is not UTF-8 text"
            ) from exc
        requested_selector = _selector(candidate.selector)
        anchored = reanchor(
            text,
            requested_selector,
            expected_snapshot_sha256=reserved.resolved.representation.content_sha256,
        )
        resolved_selector = CompositeSelector(
            exact=anchored.exact,
            prefix=requested_selector.prefix,
            suffix=requested_selector.suffix,
            start=anchored.start,
            end=anchored.end,
        )
        selector_value = _selector_value(resolved_selector)
        identity = {
            "source_ref": source_ref.to_dict(),
            "representation_id": representation_id,
            "content_sha256": reserved.resolved.representation.content_sha256,
        }
        evidence_id = _stable_id("work-buddy.truth-source-evidence/v1", identity)
        span_id = _stable_id(
            "work-buddy.truth-source-evidence-span/v1",
            {"evidence_id": evidence_id, "selector": selector_value},
        )
        resolution_id = _stable_id(
            "work-buddy.truth-source-resolution/v1",
            {
                "request_sha256": request_sha,
                "usage_id": reserved.reservation.usage_id,
            },
        )
        item = source_store.get_item(source_ref)
        if item is None:
            raise InvariantViolation("resolved source item is no longer available")
        _grant_projection_metadata_access(
            source_store,
            source_ref=source_ref,
            representation_id=representation_id,
            content_sha256=reserved.resolved.representation.content_sha256,
            source_principal=source_principal,
        )
        if len(reserved.resolved.content) > truth_store._inline_content_bytes:
            _path, blob_created = truth_store._store_blob_bytes(
                reserved.resolved.representation.content_sha256,
                reserved.resolved.content,
            )
    except Exception:
        try:
            source_store.release_usage(reserved.reservation.usage_id)
        finally:
            if blob_created:
                truth_store._remove_unreferenced_blob(
                    reserved.resolved.representation.content_sha256
                )
        raise
    return PreparedSourceClaim(
        source_ref=source_ref,
        source_role=item.source_role,
        reservation=reserved.reservation,
        resolved=reserved.resolved,
        text=text,
        selector=resolved_selector,
        selector_value=selector_value,
        request_sha256=request_sha,
        idempotency_key=idempotency_key,
        consumer_ref=consumer_ref,
        evidence_id=evidence_id,
        span_id=span_id,
        resolution_record_id=resolution_id,
        blob_created=blob_created,
    )


def _ensure_evidence(
    store: TruthStore,
    prepared: PreparedSourceClaim,
    *,
    actors: SourceClaimActors,
    conn: sqlite3.Connection,
) -> EvidenceRecord:
    existing = store.get_evidence(prepared.evidence_id, conn=conn)
    kind = _EVIDENCE_KIND_BY_SOURCE_ROLE.get(prepared.source_role, "artifact")
    if existing is not None:
        if (
            existing.content_sha256
            != prepared.resolved.representation.content_sha256
            or existing.source_locator != prepared.source_ref.uri
            or existing.kind != kind
        ):
            raise InvariantViolation("source evidence identity conflicts with stored evidence")
        return existing
    return store.capture_evidence(
        kind=kind,
        source_locator=prepared.source_ref.uri,
        actor=_legacy_actor(actors.applier),
        acquisition_method=_ACQUISITION_BY_SOURCE_ROLE.get(
            prepared.source_role, "file_read"
        ),
        content=prepared.text,
        content_sha256=prepared.resolved.representation.content_sha256,
        media_type=prepared.resolved.representation.media_type,
        acquired_at=prepared.resolved.resolved_at,
        origin=AcquisitionOrigin.PREEXISTING,
        meta={
            "source_ref": prepared.source_ref.to_dict(),
            "representation_id": prepared.resolved.representation.representation_id,
            "source_role": prepared.source_role,
            "source_fidelity": prepared.resolved.fidelity,
        },
        record_id=prepared.evidence_id,
        conn=conn,
    )


def _ensure_span(
    store: TruthStore,
    prepared: PreparedSourceClaim,
    evidence: EvidenceRecord,
    *,
    actors: SourceClaimActors,
    conn: sqlite3.Connection,
) -> EvidenceSpanRecord:
    existing = store.get_span(prepared.span_id, conn=conn)
    if existing is not None:
        if (
            existing.evidence_id != evidence.id
            or existing.selector_json != serialize_selector(prepared.selector)
            or existing.quote_exact != prepared.selector.exact
        ):
            raise InvariantViolation("source evidence span identity conflicts")
        return existing
    author_kind, author_ref = _author_from_source(prepared.resolved)
    return store.mark_span(
        evidence_id=evidence.id,
        selector=prepared.selector,
        actor=_legacy_actor(actors.applier),
        author_kind=author_kind,
        author_ref=author_ref,
        snapshot_text=prepared.text,
        record_id=prepared.span_id,
        conn=conn,
    )


def _record_attributions(
    store: TruthStore,
    *,
    prepared: PreparedSourceClaim,
    actors: SourceClaimActors,
    claim: ClaimRecord,
    claim_created: bool,
    evidence: EvidenceRecord,
    span: EvidenceSpanRecord,
    expression_id: str | None,
    decision: CandidateDecisionAuthorization | None,
    conn: sqlite3.Connection,
) -> tuple[ProvenanceAttributionEventRecord, ...]:
    events: list[ProvenanceAttributionEventRecord] = []

    def append(
        subject_kind: str,
        subject_ref: str,
        actor: ActorRef | None,
        role: str,
        basis: str,
        assurance: str,
    ) -> None:
        if actor is None:
            return
        event_id = _stable_id(
            "work-buddy.truth-provenance-attribution/v1",
            {
                "request_sha256": prepared.request_sha256,
                "subject_kind": subject_kind,
                "subject_ref": subject_ref,
                "actor": actor.to_dict(),
                "role": role,
                "basis": basis,
            },
        )
        events.append(
            record_attribution_event(
                store,
                subject_kind=subject_kind,
                subject_ref=subject_ref,
                actor=actor,
                role=role,
                basis=basis,
                assurance=assurance,
                run_ref=actors.run_ref,
                source_ref=prepared.source_ref,
                record_id=event_id,
                conn=conn,
            )
        )

    append("evidence", evidence.id, actors.applier, "applier", "source_copy", "trusted_component")
    append("evidence_span", span.id, actors.selector, "selector", "resolved_source_selector", "source_bound")
    append("evidence_span", span.id, actors.applier, "applier", "truth_kernel", "trusted_component")
    if claim_created:
        append("claim", claim.id, actors.semantic_producer, "semantic_producer", "accepted_candidate_semantics", "source_bound")
    append("claim", claim.id, actors.candidate_preparer, "candidate_preparer", "staged_candidate", "run_bound")
    append("claim", claim.id, actors.matcher, "matcher", "staged_existing_claim_match", "run_bound")
    append("claim", claim.id, actors.execution_authorizer, "execution_authorizer", "execution_authorization", "authorization_receipt")
    append("claim", claim.id, actors.substantive_reviewer, "substantive_reviewer", "explicit_substantive_review", "human_attested")
    append("claim", claim.id, actors.semantic_reviser, "semantic_reviser", "accepted_candidate_revision", "human_gesture")
    append("claim", claim.id, actors.evidence_selector, "evidence_selector", "accepted_evidence_set", "human_gesture")
    append("claim", claim.id, actors.expression_relation_assessor, "expression_relation_assessor", "accepted_expression_relation", "human_gesture")
    append("claim", claim.id, actors.applier, "applier", "truth_kernel", "trusted_component")
    if decision is not None:
        append("claim", claim.id, decision.actor, "candidate_decision_actor", decision.basis, decision.assurance)
    if expression_id is not None:
        if conn.execute(
            "SELECT 1 FROM expressions WHERE id = ?", (expression_id,)
        ).fetchone() is None:
            raise InvariantViolation("source-backed expression does not exist")
        append(
            "expression",
            expression_id,
            actors.candidate_preparer,
            "candidate_preparer",
            "staged_candidate",
            "run_bound",
        )
        append(
            "expression",
            expression_id,
            actors.expression_relation_assessor,
            "expression_relation_assessor",
            "accepted_expression_relation",
            "human_gesture",
        )
        append(
            "expression",
            expression_id,
            actors.applier,
            "applier",
            "truth_kernel",
            "trusted_component",
        )
        if decision is not None:
            append(
                "expression",
                expression_id,
                decision.actor,
                "candidate_decision_actor",
                decision.basis,
                decision.assurance,
            )
    return tuple(events)


def write_prepared_source_claim(
    store: TruthStore,
    prepared: PreparedSourceClaim,
    *,
    candidate: SourceClaimCandidate,
    actors: SourceClaimActors,
    existing_claim_id: str | None = None,
    decision: CandidateDecisionAuthorization | None = None,
    claim: ClaimRecord | None = None,
    claim_created: bool | None = None,
    expression_id: str | None = None,
    extra_result: Mapping[str, Any] | None = None,
    conn: sqlite3.Connection,
) -> SourceClaimWrite:
    """Write the complete Truth side of one prepared source use in ``conn``."""

    prior = queries.truth_operation_result(
        store,
        operation_name=OPERATION_NAME,
        idempotency_key=prepared.idempotency_key,
        conn=conn,
    )
    if prior is not None:
        raise InvariantViolation("source-backed Truth operation already committed")
    evidence = _ensure_evidence(store, prepared, actors=actors, conn=conn)
    span = _ensure_span(store, prepared, evidence, actors=actors, conn=conn)

    if claim is None:
        if existing_claim_id is not None:
            claim = store.get_claim(existing_claim_id, conn=conn)
            if claim is None:
                raise InvariantViolation("existing source-backed claim does not exist")
            created = False
        else:
            written = store.propose_claim(
                proposition=candidate.proposition,
                claim_kind=candidate.claim_kind,
                actor=_legacy_actor(
                    actors.semantic_producer, producer_meta=actors.producer_meta
                ),
                structured=candidate.structured,
                scope=candidate.scope,
                valid_from=candidate.valid_from,
                valid_to=candidate.valid_to,
                confidence_extraction=candidate.confidence_extraction,
                meta={
                    "source": "truth_claim_propose_from_source",
                    "source_ref": prepared.source_ref.to_dict(),
                    "run_ref": actors.run_ref,
                },
                record_id=_stable_id(
                    "work-buddy.truth-source-claim/v1",
                    {"request_sha256": prepared.request_sha256},
                ),
                status_event_id=_stable_id(
                    "work-buddy.truth-source-claim-status/v1",
                    {"request_sha256": prepared.request_sha256},
                ),
                conn=conn,
            )
            claim = written.claim
            created = written.created
    else:
        found = store.get_claim(claim.id, conn=conn)
        if found is None or found.canonical_sha256 != claim.canonical_sha256:
            raise InvariantViolation("supplied claim is not the transaction's claim")
        created = bool(claim_created)

    if decision is not None:
        if decision.decision == "add" and not created:
            raise InvariantViolation("an existing claim requires a connect decision")
        if decision.decision == "connect" and created:
            raise InvariantViolation("a new claim requires an add decision")
        if decision.decision not in {"add", "connect"}:
            raise InvariantViolation("source-backed claim write supports add/connect only")

    role = validate_claim_evidence_role(
        {
            "schema": "claim-evidence/v1",
            "evidential_effect": candidate.evidential_effect,
            "derivation_relationship": candidate.derivation_relationship,
            **(
                {}
                if candidate.relation_diagnostics is None
                else {"diagnostics": dict(candidate.relation_diagnostics)}
            ),
        }
    ).to_role()
    relation_id = _stable_id(
        "work-buddy.truth-source-evidence-relation/v1",
        {
            "claim_id": claim.id,
            "span_id": span.id,
            "role": role,
        },
    )
    existing_relation = conn.execute(
        "SELECT * FROM claim_links WHERE id = ?", (relation_id,)
    ).fetchone()
    if existing_relation is None:
        store.add_link(
            from_claim_id=claim.id,
            link_type="evidence_relation",
            to_kind="evidence_span",
            to_ref=span.id,
            actor=_legacy_actor(
                actors.semantic_producer, producer_meta=actors.producer_meta
            ),
            role=role,
            record_id=relation_id,
            conn=conn,
        )
    elif (
        str(existing_relation["from_claim_id"]) != claim.id
        or str(existing_relation["to_ref"]) != span.id
        or json.loads(str(existing_relation["role_json"])) != role
    ):
        raise InvariantViolation("source evidence relation identity conflicts")

    resolution = record_evidence_source_resolution(
        store,
        evidence_id=evidence.id,
        resolution=prepared.resolved.to_resolution_record(
            selector=prepared.selector_value, include_excerpt=False
        ),
        usage_id=prepared.reservation.usage_id,
        authorization_context_sha256=prepared.resolved.authorization_context_sha256,
        actor=actors.applier,
        record_id=prepared.resolution_record_id,
        conn=conn,
    )
    usage_event = record_source_usage_event(
        store,
        resolution_record_id=resolution.id,
        usage_id=prepared.reservation.usage_id,
        status="reserved",
        purpose=SOURCE_PURPOSE,
        consumer_ref=prepared.consumer_ref,
        redaction_epoch=prepared.reservation.redaction_epoch,
        actor=actors.applier,
        record_id=_stable_id(
            "work-buddy.truth-source-usage-event/v1",
            {"usage_id": prepared.reservation.usage_id, "status": "reserved"},
        ),
        conn=conn,
    )
    attributions = _record_attributions(
        store,
        prepared=prepared,
        actors=actors,
        claim=claim,
        claim_created=created,
        evidence=evidence,
        span=span,
        expression_id=expression_id,
        decision=decision,
        conn=conn,
    )
    candidate_decision = None
    if decision is not None:
        candidate_id = candidate.candidate_id or _stable_id(
            "work-buddy.truth-source-candidate/v1",
            {"request_sha256": prepared.request_sha256},
        )
        candidate_digest = candidate.candidate_sha256 or sha256_text(
            canonical_json(_candidate_payload(candidate))
        )
        candidate_decision = record_candidate_decision(
            store,
            candidate_id=candidate_id,
            candidate_sha256=candidate_digest,
            decision=decision.decision,
            claim_id=claim.id,
            actor=decision.actor,
            basis=decision.basis,
            assurance=decision.assurance,
            authorization_ref=decision.authorization_ref,
            authorization_context_sha256=decision.authorization_context_sha256,
            source_refs=(prepared.source_ref,),
            run_ref=actors.run_ref,
            decided_at=decision.decided_at,
            record_id=_stable_id(
                "work-buddy.truth-candidate-decision/v1",
                {
                    "request_sha256": prepared.request_sha256,
                    "decision": decision.decision,
                },
            ),
            conn=conn,
        )
    result_value = {
        "claim_id": claim.id,
        "claim_created": created,
        "claim_canonical_sha256": claim.canonical_sha256,
        "evidence_id": evidence.id,
        "span_id": span.id,
        "relation_id": relation_id,
        "resolution_record_id": resolution.id,
        "source_ref": prepared.source_ref.uri,
        "usage_id": prepared.reservation.usage_id,
        "usage_status": "reserved",
        "consumer_ref": prepared.consumer_ref,
        "redaction_epoch": prepared.reservation.redaction_epoch,
        "candidate_decision_id": (
            None if candidate_decision is None else candidate_decision.id
        ),
        **dict(extra_result or {}),
    }
    operation = record_operation_result(
        store,
        operation_name=OPERATION_NAME,
        idempotency_key=prepared.idempotency_key,
        request_sha256=prepared.request_sha256,
        result=result_value,
        actor=actors.applier,
        record_id=_stable_id(
            "work-buddy.truth-source-operation-result/v1",
            {"idempotency_key": prepared.idempotency_key},
        ),
        conn=conn,
    )
    return SourceClaimWrite(
        claim=claim,
        claim_created=created,
        evidence=evidence,
        span=span,
        relation_id=relation_id,
        resolution=resolution,
        usage_event=usage_event,
        attribution_events=attributions,
        candidate_decision=candidate_decision,
        operation_result_id=operation.record.id,
        operation_result_created=operation.created,
    )


def reconcile_source_usage(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    resolution_record_id: str,
    usage_id: str,
    redaction_epoch: int,
    consumer_ref: str,
    actor: ActorRef,
) -> str:
    """Acknowledge one committed managed copy and append its Truth-local state."""

    prior_status = _current_usage_status(truth_store, usage_id)
    if prior_status in {"acknowledged", "redaction_pending"}:
        return prior_status
    try:
        acknowledged = source_store.acknowledge_usage(usage_id)
        status = "acknowledged"
        error_code = None
        if acknowledged.redaction_epoch != redaction_epoch:
            status = "redaction_pending"
            error_code = "source_redacted_after_truth_commit"
    except Exception:  # durable local reservation remains the recovery handle
        status = "acknowledgement_pending"
        error_code = "source_acknowledgement_failed"
    if status == prior_status:
        return status
    record_source_usage_event(
        truth_store,
        resolution_record_id=resolution_record_id,
        usage_id=usage_id,
        status=status,
        purpose=SOURCE_PURPOSE,
        consumer_ref=consumer_ref,
        redaction_epoch=redaction_epoch,
        actor=actor,
        error_code=error_code,
        record_id=_stable_id(
            "work-buddy.truth-source-usage-event/v1",
            {"usage_id": usage_id, "status": status},
        ),
    )
    return status


def reconcile_pending_source_usages(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    actor: ActorRef,
    limit: int = 100,
) -> dict[str, int]:
    """Boundedly recover committed Truth uses left pending by process death."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    with truth_store._read_connection() as conn:
        rows = conn.execute(
            "WITH ranked AS ("
            " SELECT e.*,ROW_NUMBER() OVER (PARTITION BY usage_id "
            " ORDER BY created_at DESC,id DESC) AS position "
            " FROM truth_source_usage_events e"
            ") SELECT * FROM ranked WHERE position=1 "
            "AND status IN ('reserved','acknowledgement_pending') "
            "ORDER BY created_at,id LIMIT ?",
            (limit,),
        ).fetchall()
    counts = {"examined": len(rows), "acknowledged": 0, "pending": 0, "redaction_pending": 0}
    for row in rows:
        status = reconcile_source_usage(
            truth_store,
            source_store,
            resolution_record_id=str(row["resolution_record_id"]),
            usage_id=str(row["usage_id"]),
            redaction_epoch=int(row["redaction_epoch"]),
            consumer_ref=str(row["consumer_ref"]),
            actor=actor,
        )
        bucket = (
            "acknowledged"
            if status == "acknowledged"
            else "redaction_pending"
            if status == "redaction_pending"
            else "pending"
        )
        counts[bucket] += 1
    return counts


def truth_claim_propose_from_source(
    truth_store: TruthStore,
    source_store: SourceStore,
    *,
    source_ref: SourceRef,
    representation_id: str,
    expected_content_sha256: str,
    expected_native_revision: str | None,
    source_principal: ActorRef,
    candidate: SourceClaimCandidate,
    actors: SourceClaimActors,
    idempotency_key: str,
    existing_claim_id: str | None = None,
    decision: CandidateDecisionAuthorization | None = None,
) -> SourceClaimProposalResult:
    """Resolve one exact source and commit one idempotent supported proposal."""

    request_sha = source_claim_request_sha256(
        source_ref=source_ref,
        representation_id=representation_id,
        expected_content_sha256=expected_content_sha256,
        expected_native_revision=expected_native_revision,
        candidate=candidate,
        actors=actors,
        existing_claim_id=existing_claim_id,
        decision=decision,
    )
    prior = queries.truth_operation_result(
        truth_store,
        operation_name=OPERATION_NAME,
        idempotency_key=idempotency_key,
    )
    if prior is not None:
        if prior.request_sha256 != request_sha:
            raise InvariantViolation(
                "truth idempotency key was reused with a different source request"
            )
        status = _reconcile_operation_usage(
            truth_store,
            source_store,
            operation=prior,
            actor=actors.applier,
        )
        return _result_from_record(prior, usage_status=status, replayed=True)

    prepared = prepare_source_claim(
        truth_store,
        source_store,
        source_ref=source_ref,
        representation_id=representation_id,
        expected_content_sha256=expected_content_sha256,
        expected_native_revision=expected_native_revision,
        source_principal=source_principal,
        candidate=candidate,
        actors=actors,
        idempotency_key=idempotency_key,
        existing_claim_id=existing_claim_id,
        decision=decision,
    )
    committed = False
    try:
        source_store.precommit_recheck_usage(prepared.reservation.usage_id)
        with truth_store.write_transaction() as conn:
            # Concurrent callers may both reserve Sources uses. Only the first
            # Truth idempotency result wins; the loser releases its reservation.
            prior = queries.truth_operation_result(
                truth_store,
                operation_name=OPERATION_NAME,
                idempotency_key=idempotency_key,
                conn=conn,
            )
            if prior is not None:
                if prior.request_sha256 != prepared.request_sha256:
                    raise InvariantViolation(
                        "truth idempotency key was reused with a different source request"
                    )
                write = None
            else:
                write = write_prepared_source_claim(
                    truth_store,
                    prepared,
                    candidate=candidate,
                    actors=actors,
                    existing_claim_id=existing_claim_id,
                    decision=decision,
                    conn=conn,
                )
        if write is None:
            source_store.release_usage(prepared.reservation.usage_id)
            if prepared.blob_created:
                truth_store._remove_unreferenced_blob(
                    prepared.resolved.representation.content_sha256
                )
            assert prior is not None
            status = _reconcile_operation_usage(
                truth_store,
                source_store,
                operation=prior,
                actor=actors.applier,
            )
            return _result_from_record(prior, usage_status=status, replayed=True)
        committed = True
        usage_status = reconcile_source_usage(
            truth_store,
            source_store,
            resolution_record_id=write.resolution.id,
            usage_id=prepared.reservation.usage_id,
            redaction_epoch=prepared.reservation.redaction_epoch,
            consumer_ref=prepared.consumer_ref,
            actor=actors.applier,
        )
        record = queries.truth_operation_result(
            truth_store,
            operation_name=OPERATION_NAME,
            idempotency_key=idempotency_key,
        )
        assert record is not None
        return _result_from_record(
            record, usage_status=usage_status, replayed=not write.operation_result_created
        )
    finally:
        if not committed:
            try:
                source_store.release_usage(prepared.reservation.usage_id)
            finally:
                if prepared.blob_created:
                    truth_store._remove_unreferenced_blob(
                        prepared.resolved.representation.content_sha256
                    )


__all__ = [
    "CandidateDecisionAuthorization",
    "OPERATION_NAME",
    "PreparedSourceClaim",
    "PROJECTION_CONSUMER_DOMAIN",
    "PROJECTION_PRINCIPAL_SUBJECT",
    "PROJECTION_SOURCE_PURPOSE",
    "SOURCE_PURPOSE",
    "SourceClaimActors",
    "SourceClaimCandidate",
    "SourceClaimProposalResult",
    "SourceClaimWrite",
    "prepare_source_claim",
    "reconcile_pending_source_usages",
    "reconcile_source_usage",
    "source_claim_request_sha256",
    "truth_claim_propose_from_source",
    "write_prepared_source_claim",
]
