"""Schema-v9 source receipts, evidence relations, and provenance history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.security.actors import ActorRef
from work_buddy.sources.models import SourceRef, SourceResolutionRecord
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation, TransitionError
from work_buddy.truth.evidence_relations import (
    CLAIM_EVIDENCE_SCHEMA,
    classify_claim_evidence_role,
)
from work_buddy.truth.export import FORMAT_VERSION, export_store, import_store
from work_buddy.truth.identity import new_id, sha256_bytes
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.queries import (
    candidate_decisions,
    claim_evidence_relations,
    evidence_source_resolutions,
    integrity_findings,
    truth_operation_result,
)
from work_buddy.truth.source_provenance import (
    LEGACY_PROVENANCE_CLASSIFICATION,
    provenance_for_subject,
    record_attribution_event,
    record_candidate_decision,
    record_evidence_source_resolution,
    record_operation_result,
    record_source_usage_event,
)
from work_buddy.truth.store import TruthStore


NOW = "2026-08-09T16:00:00.000+00:00"
LATER = "2026-08-09T16:01:00.000+00:00"
SYSTEM = Actor("system", "truth-v9-test")


def _actor(kind: str = "agent_run", subject: str = "worker-run-0001") -> ActorRef:
    return ActorRef(
        issuer_authority_id="issuer-authority-0001",
        subject=subject,
        kind=kind,
        tenant_scope_id="tenant-scope-0001",
    )


def _profile(*, strict: bool = False) -> dict[str, object]:
    profile: dict[str, object] = {
        "store_id": new_id(),
        "profile": "source-backed-truth",
        "title": "Source-backed Truth",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }
    if strict:
        profile["support_policy"] = {
            "fact": {
                "minimum_usable_supports": 1,
                "allowed_effects": ["supports"],
                "allow_human_assertion_as_source": False,
            }
        }
    return profile


class _EmptyRegistry:
    def paths_for_store_id(self, _store_id: str):
        return ()


def _source_fixture(store: TruthStore):
    text = "The retained source supports this proposition."
    source_ref = SourceRef("source-authority-0001", "source-item-0000001")
    evidence = store.capture_evidence(
        kind="document",
        source_locator=source_ref.uri,
        actor=SYSTEM,
        acquisition_method="file_read",
        content=text,
        media_type="text/plain",
        record_id="11" * 16,
        acquired_at=NOW,
        created_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=text),
        actor=SYSTEM,
        author_kind="unknown",
        record_id="12" * 16,
        created_at=NOW,
    )
    claim = store.propose_claim(
        proposition="The retained source supports this proposition.",
        claim_kind="fact",
        actor=SYSTEM,
        record_id="13" * 16,
        status_event_id="14" * 16,
        created_at=NOW,
        status_at=NOW,
    ).claim
    return text, source_ref, evidence, span, claim


def test_v9_records_round_trip_without_promoting_legacy_authorship(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    text, source_ref, evidence, span, claim = _source_fixture(store)

    # Old created_by fields remain historical compatibility data rather than
    # silently becoming an issuer-qualified semantic producer.
    legacy = provenance_for_subject(
        store, subject_kind="claim", subject_ref=claim.id
    )
    assert legacy.classification == LEGACY_PROVENANCE_CLASSIFICATION
    assert legacy.events == ()

    resolution = SourceResolutionRecord(
        source_ref=source_ref,
        representation_id="representation-0001",
        content_sha256=sha256_bytes(text.encode("utf-8")),
        media_type="text/plain",
        byte_length=len(text.encode("utf-8")),
        selector={"type": "TextQuoteSelector", "exact": text},
        excerpt=None,
        resolver_id="sources-retained",
        resolver_version="1",
        observation_id="observation-0001",
        redaction_epoch=0,
        resolved_at=NOW,
    )
    receipt = record_evidence_source_resolution(
        store,
        evidence_id=evidence.id,
        resolution=resolution,
        usage_id="source-usage-0001",
        authorization_context_sha256="a" * 64,
        actor=_actor(),
        record_id="15" * 16,
        created_at=NOW,
    )
    usage = record_source_usage_event(
        store,
        resolution_record_id=receipt.id,
        usage_id=receipt.usage_id,
        status="acknowledgement_pending",
        purpose="truth_claim_propose_from_source",
        consumer_ref=claim.id,
        redaction_epoch=0,
        actor=_actor(),
        record_id="16" * 16,
        created_at=NOW,
    )
    link = store.add_link(
        from_claim_id=claim.id,
        link_type="evidence_relation",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=SYSTEM,
        role={
            "schema": CLAIM_EVIDENCE_SCHEMA,
            "evidential_effect": "supports",
            "derivation_relationship": "direct_statement",
        },
        record_id="17" * 16,
        created_at=NOW,
    )
    attribution = record_attribution_event(
        store,
        subject_kind="claim",
        subject_ref=claim.id,
        actor=_actor(),
        role="semantic_producer",
        basis="accepted_unchanged_ai_candidate",
        assurance="run_bound",
        run_ref="analysis-run-0001",
        source_ref=source_ref,
        record_id="18" * 16,
        asserted_at=NOW,
    )
    decision = record_candidate_decision(
        store,
        candidate_id="candidate-0001",
        candidate_sha256="b" * 64,
        decision="add",
        claim_id=claim.id,
        actor=_actor("human", "local-profile-0001"),
        basis="bound_local_gesture",
        assurance="authenticated_loopback_profile",
        authorization_ref="gesture-receipt-0001",
        authorization_context_sha256="c" * 64,
        source_refs=[source_ref],
        run_ref="analysis-run-0001",
        record_id="19" * 16,
        decided_at=LATER,
    )
    operation = record_operation_result(
        store,
        operation_name="truth_claim_propose_from_source",
        idempotency_key="mutation-0001",
        request_sha256="d" * 64,
        result={"claim_id": claim.id, "decision_id": decision.id},
        actor=_actor("human", "local-profile-0001"),
        record_id="1a" * 16,
        created_at=LATER,
    )

    assert usage.status == "acknowledgement_pending"
    assert evidence_source_resolutions(store, evidence.id) == (receipt,)
    assert candidate_decisions(store, candidate_id="candidate-0001") == (decision,)
    assert truth_operation_result(
        store,
        operation_name="truth_claim_propose_from_source",
        idempotency_key="mutation-0001",
    ) == operation.record
    projected = claim_evidence_relations(store, claim.id)
    assert [(item.link_id, item.classification, item.usable_support) for item in projected] == [
        (link.id, "validated", True)
    ]
    assert provenance_for_subject(
        store, subject_kind="claim", subject_ref=claim.id
    ).events == (attribution,)
    assert not [item for item in integrity_findings(store) if item.severity == "error"]

    exported = export_store(store)
    header = json.loads(exported.path.read_text(encoding="utf-8").splitlines()[0])
    assert header["format_version"] == FORMAT_VERSION == 10
    target = tmp_path / "target"
    target.mkdir()
    restored = import_store(
        exported.path.read_bytes(), target, registry=_EmptyRegistry()
    ).store
    assert evidence_source_resolutions(restored, evidence.id) == (receipt,)
    assert candidate_decisions(restored, candidate_id="candidate-0001") == (
        decision,
    )
    assert provenance_for_subject(
        restored, subject_kind="claim", subject_ref=claim.id
    ).events == (attribution,)


def test_evidence_relation_rejects_ad_hoc_role_and_projects_legacy_conservatively(
    tmp_path: Path,
):
    root = tmp_path / "store"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    _text, _source_ref, _evidence, span, claim = _source_fixture(store)
    with pytest.raises(InvariantViolation, match="claim-evidence"):
        store.add_link(
            from_claim_id=claim.id,
            link_type="evidence_relation",
            to_kind="evidence_span",
            to_ref=span.id,
            actor=SYSTEM,
            role={"relationship": "supports"},
        )
    classified = classify_claim_evidence_role(
        link_type="evidence_relation",
        role_json='{"relationship":"supports"}',
    )
    assert classified.classification == "legacy_unspecified"
    assert classified.is_positive is False


def test_strict_support_policy_counts_only_allowed_positive_effects(tmp_path: Path):
    root = tmp_path / "strict"
    root.mkdir()
    store = TruthStore.create(root, _profile(strict=True))
    _text, _source_ref, _evidence, span, claim = _source_fixture(store)
    store.add_link(
        from_claim_id=claim.id,
        link_type="evidence_relation",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=SYSTEM,
        role={
            "schema": CLAIM_EVIDENCE_SCHEMA,
            "evidential_effect": "partially_supports",
            "derivation_relationship": "inference",
        },
    )
    assert claim_evidence_relations(store, claim.id)[0].usable_support is False
    with store.connect() as conn:
        assessment = TruthLifecycle(store).assess_support(claim.id, conn=conn)
    assert assessment.support_span_ids == (span.id,)
    assert assessment.usable_span_ids == ()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(TransitionError, match="requires at least 1"):
            TruthLifecycle(store)._ensure_confirmation_ready_locked(conn, claim.id)
        conn.execute("ROLLBACK")


def test_operation_idempotency_conflicts_on_request_digest(tmp_path: Path):
    root = tmp_path / "operation"
    root.mkdir()
    store = TruthStore.create(root, _profile())
    actor = _actor("human", "local-profile-0001")
    first = record_operation_result(
        store,
        operation_name="truth_claim_propose_from_source",
        idempotency_key="same-key-0001",
        request_sha256="1" * 64,
        result={"claim_id": "2" * 32},
        actor=actor,
    )
    replay = record_operation_result(
        store,
        operation_name="truth_claim_propose_from_source",
        idempotency_key="same-key-0001",
        request_sha256="1" * 64,
        result={"ignored": "replay returns the durable original"},
        actor=actor,
    )
    assert first.created is True
    assert replay.created is False
    assert replay.record == first.record
    with pytest.raises(InvariantViolation, match="different request"):
        record_operation_result(
            store,
            operation_name="truth_claim_propose_from_source",
            idempotency_key="same-key-0001",
            request_sha256="3" * 64,
            result={},
            actor=actor,
        )
