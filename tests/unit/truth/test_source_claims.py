from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import work_buddy.truth.source_claims as source_claims_module

from work_buddy.security.actors import ActorRef
from work_buddy.sources import (
    AttributionAssertion,
    SourceAccessDenied,
    SourceStore,
    resolve_and_reserve_source,
)
from work_buddy.truth import lifecycle, queries
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.source_claims import (
    CandidateDecisionAuthorization,
    SourceClaimActors,
    SourceClaimCandidate,
    truth_claim_propose_from_source,
)
from work_buddy.truth.source_provenance import provenance_for_subject
from work_buddy.truth.store import TruthStore


TENANT = "tenant-source-truth"


def _profile(store_id: str) -> dict[str, object]:
    return {
        "store_id": store_id,
        "profile": "source-backed-test",
        "title": "Source backed Truth",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "document_surface": {"enabled": False},
    }


def _setup(tmp_path: Path, *, content: str = "I prefer durable, source-backed memory."):
    source_store = SourceStore.create(tmp_path / "sources")
    truth_store = TruthStore.create(
        tmp_path / "truth", _profile("1" * 32)
    )
    issuer = ActorRef(
        source_store.authority_id, "truth-issuer-0001", "service", TENANT
    )
    human = ActorRef(
        source_store.authority_id, "profile-human-0001", "human", TENANT
    )
    agent = ActorRef(
        source_store.authority_id, "agent-run-00000001", "agent_run", TENANT
    )
    applier = ActorRef(
        source_store.authority_id, "truth-kernel-0001", "system", TENANT
    )
    principal = ActorRef(
        source_store.authority_id, "truth-service-0001", "service", TENANT
    )
    item = source_store.capture_source(
        content=content,
        source_role="conversation_message",
        tenant_scope_id=TENANT,
        originating_surface="test",
        attributions=(
            AttributionAssertion(
                role="author",
                actor=human,
                basis="authenticated_input_fixture",
                assurance="test",
                asserted_by=issuer,
            ),
        ),
        producer=issuer,
    )
    source_store.grant_access(
        source_ref=item.source_ref,
        principal=principal,
        purpose="truth_claim_proposal",
        access_mode="content",
        authorization_fingerprint="a" * 64,
        content_boundary={
            "representation_id": item.primary_representation_id,
            "max_bytes": len(content.encode("utf-8")),
        },
    )
    actors = SourceClaimActors(
        semantic_producer=agent,
        selector=agent,
        candidate_preparer=agent,
        execution_authorizer=human,
        applier=applier,
        run_ref="truth-analysis-run-0001",
        producer_meta={
            "model": "fixture-model",
            "harness": "pytest",
            "surface": "truth-test",
            "session_id": "fixture-session",
        },
    )
    return source_store, truth_store, item, principal, human, agent, applier, actors


def _candidate(content: str) -> SourceClaimCandidate:
    exact = "durable, source-backed memory"
    start = content.index(exact)
    return SourceClaimCandidate(
        proposition="The speaker prefers durable, source-backed memory.",
        claim_kind="preference",
        selector={"exact": exact, "start": start, "end": start + len(exact)},
        evidential_effect="supports",
        derivation_relationship="paraphrase",
        scope="store",
        candidate_id="candidate-source-0001",
        candidate_sha256="c" * 64,
    )


def test_atomic_source_claim_preserves_ai_producer_and_human_decision(
    tmp_path: Path,
) -> None:
    content = "I prefer durable, source-backed memory."
    (
        source_store,
        truth_store,
        item,
        principal,
        human,
        agent,
        _applier,
        actors,
    ) = _setup(tmp_path, content=content)
    result = truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=source_store.get_representation(
            item.primary_representation_id
        ).content_sha256,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=actors,
        idempotency_key="source-claim-mutation-0001",
        decision=CandidateDecisionAuthorization(
            decision="add",
            actor=human,
            basis="authenticated_loopback_ui_gesture",
            assurance="enrolled_local_session_gesture",
            authorization_ref="gesture-receipt-0001",
            authorization_context_sha256="d" * 64,
        ),
    )

    assert result.claim_created is True
    assert result.usage_status == "acknowledged"
    assert lifecycle.TruthLifecycle(truth_store).latest_status(result.claim_id).status == "proposed"
    claim = truth_store.get_claim(result.claim_id)
    assert claim is not None
    assert claim.created_by_kind == "agent_run"
    assert claim.created_by_ref == agent.canonical_id
    span = truth_store.get_span(result.span_id)
    assert span is not None
    assert span.quote_exact == "durable, source-backed memory"
    assert span.author_kind == "human"
    assert span.author_ref == human.canonical_id

    relation = truth_store.get_link(result.relation_id)
    assert relation is not None
    assert relation.link_type == "evidence_relation"
    assert json.loads(relation.role_json or "{}") == {
        "derivation_relationship": "paraphrase",
        "evidential_effect": "supports",
        "schema": "claim-evidence/v1",
    }
    decisions = queries.candidate_decisions(
        truth_store, candidate_id="candidate-source-0001"
    )
    assert len(decisions) == 1
    assert decisions[0].decision == "add"
    projection = provenance_for_subject(
        truth_store, subject_kind="claim", subject_ref=result.claim_id
    )
    by_role = {event.role: event for event in projection.events}
    assert json.loads(by_role["semantic_producer"].actor_ref_json) == agent.to_dict()
    assert json.loads(by_role["candidate_decision_actor"].actor_ref_json) == human.to_dict()
    assert "lifecycle_decision_actor" not in by_role

    replay = truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=source_store.get_representation(
            item.primary_representation_id
        ).content_sha256,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=actors,
        idempotency_key="source-claim-mutation-0001",
        decision=CandidateDecisionAuthorization(
            decision="add",
            actor=human,
            basis="authenticated_loopback_ui_gesture",
            assurance="enrolled_local_session_gesture",
            authorization_ref="gesture-receipt-0001",
            authorization_context_sha256="d" * 64,
        ),
    )
    assert replay.replayed is True
    assert replay.usage_id == result.usage_id
    assert queries.candidate_decisions(
        truth_store, candidate_id="candidate-source-0001"
    ) == decisions


def test_source_claim_idempotency_conflict_does_not_resolve_again(tmp_path: Path) -> None:
    content = "I prefer durable, source-backed memory."
    source_store, truth_store, item, principal, _human, _agent, _applier, actors = (
        _setup(tmp_path, content=content)
    )
    digest = source_store.get_representation(item.primary_representation_id).content_sha256
    truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=digest,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=actors,
        idempotency_key="source-claim-mutation-conflict",
    )
    changed = replace(_candidate(content), proposition="A different proposition.")
    with pytest.raises(InvariantViolation, match="different source request"):
        truth_claim_propose_from_source(
            truth_store,
            source_store,
            source_ref=item.source_ref,
            representation_id=item.primary_representation_id,
            expected_content_sha256=digest,
            expected_native_revision=None,
            source_principal=principal,
            candidate=changed,
            actors=actors,
            idempotency_key="source-claim-mutation-conflict",
        )


@pytest.mark.parametrize("boundary", ["before_ack", "after_ack_before_truth_event"])
def test_committed_source_claim_replay_recovers_usage_acknowledgement(
    tmp_path: Path, monkeypatch, boundary: str
) -> None:
    content = "I prefer durable, source-backed memory."
    source_store, truth_store, item, principal, _human, _agent, _applier, actors = (
        _setup(tmp_path, content=content)
    )
    digest = source_store.get_representation(item.primary_representation_id).content_sha256
    real_reconcile = source_claims_module.reconcile_source_usage
    real_record = source_claims_module.record_source_usage_event
    if boundary == "before_ack":
        monkeypatch.setattr(
            source_claims_module,
            "reconcile_source_usage",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated stop before Sources acknowledgement")
            ),
        )
    else:
        def stop_after_ack(*args, **kwargs):
            if kwargs.get("status") == "acknowledged":
                raise RuntimeError("simulated stop before Truth acknowledgement receipt")
            return real_record(*args, **kwargs)

        monkeypatch.setattr(source_claims_module, "record_source_usage_event", stop_after_ack)

    with pytest.raises(RuntimeError, match="simulated stop"):
        truth_claim_propose_from_source(
            truth_store,
            source_store,
            source_ref=item.source_ref,
            representation_id=item.primary_representation_id,
            expected_content_sha256=digest,
            expected_native_revision=None,
            source_principal=principal,
            candidate=_candidate(content),
            actors=actors,
            idempotency_key=f"source-claim-recovery-{boundary}",
        )

    prior = queries.truth_operation_result(
        truth_store,
        operation_name=source_claims_module.OPERATION_NAME,
        idempotency_key=f"source-claim-recovery-{boundary}",
    )
    assert prior is not None
    usage_id = str(json.loads(prior.result_json)["usage_id"])
    conn = source_store.connect()
    try:
        source_status = str(
            conn.execute(
                "SELECT status FROM source_usage_intents WHERE usage_id=?", (usage_id,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert source_status == (
        "reserved" if boundary == "before_ack" else "acknowledged"
    )

    monkeypatch.setattr(source_claims_module, "reconcile_source_usage", real_reconcile)
    monkeypatch.setattr(source_claims_module, "record_source_usage_event", real_record)
    recovered = truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=digest,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=actors,
        idempotency_key=f"source-claim-recovery-{boundary}",
    )
    assert recovered.replayed is True
    assert recovered.usage_status == "acknowledged"


def test_source_claim_grants_bounded_metadata_access_for_truth_projection(
    tmp_path: Path,
) -> None:
    content = "I prefer durable, source-backed memory."
    source_store, truth_store, item, principal, _human, _agent, _applier, actors = (
        _setup(tmp_path, content=content)
    )
    digest = source_store.get_representation(
        item.primary_representation_id
    ).content_sha256
    truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=digest,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=actors,
        idempotency_key="source-claim-projection-binding",
    )
    projection_principal = ActorRef(
        issuer_authority_id=principal.issuer_authority_id,
        subject="work-buddy-truth-service",
        kind="service",
        tenant_scope_id=principal.tenant_scope_id,
    )
    with pytest.raises(SourceAccessDenied):
        resolve_and_reserve_source(
            source_store,
            source_ref=item.source_ref,
            representation_id=item.primary_representation_id,
            principal=projection_principal,
            purpose="truth_hindsight_projection",
            consumer_domain="not-hindsight",
            consumer_id="wrong-projection-consumer",
            use_kind="semantic_derivative",
            disclosure_kind="metadata_only",
            redaction_policy="invalidate",
            expected_digest=digest,
        )
    reserved = resolve_and_reserve_source(
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        principal=projection_principal,
        purpose="truth_hindsight_projection",
        consumer_domain="hindsight_projection",
        consumer_id="truth-projection-consumer",
        use_kind="semantic_derivative",
        disclosure_kind="metadata_only",
        redaction_policy="invalidate",
        expected_digest=digest,
    )
    assert reserved.resolved.content == b""
    assert reserved.resolved.representation.content_sha256 == digest
    source_store.release_usage(reserved.reservation.usage_id)


def test_connect_existing_preserves_original_claim_producer(tmp_path: Path) -> None:
    content = "I prefer durable, source-backed memory."
    (
        source_store,
        truth_store,
        item,
        principal,
        human,
        _agent,
        _applier,
        actors,
    ) = _setup(tmp_path, content=content)
    existing = truth_store.propose_claim(
        proposition="The speaker prefers durable, source-backed memory.",
        claim_kind="preference",
        actor=Actor("human", "original-human-author"),
    ).claim
    result = truth_claim_propose_from_source(
        truth_store,
        source_store,
        source_ref=item.source_ref,
        representation_id=item.primary_representation_id,
        expected_content_sha256=source_store.get_representation(
            item.primary_representation_id
        ).content_sha256,
        expected_native_revision=None,
        source_principal=principal,
        candidate=_candidate(content),
        actors=replace(actors, matcher=actors.semantic_producer),
        idempotency_key="source-claim-connect-0001",
        existing_claim_id=existing.id,
        decision=CandidateDecisionAuthorization(
            decision="connect",
            actor=human,
            basis="authenticated_loopback_ui_gesture",
            assurance="enrolled_local_session_gesture",
            authorization_ref="gesture-connect-0001",
            authorization_context_sha256="e" * 64,
        ),
    )
    assert result.claim_id == existing.id
    assert result.claim_created is False
    unchanged = truth_store.get_claim(existing.id)
    assert unchanged is not None
    assert unchanged.created_by_ref == "original-human-author"
    roles = {
        event.role
        for event in provenance_for_subject(
            truth_store, subject_kind="claim", subject_ref=existing.id
        ).events
    }
    assert "matcher" in roles
    assert "candidate_decision_actor" in roles
    assert "semantic_producer" not in roles
