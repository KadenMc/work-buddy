from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.hindsight_projection.contracts import DesiredProjectionState
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore
from work_buddy.hindsight_projection.truth_reader import (
    TruthHindsightProjectionPolicy,
    TruthStoreProjectionReader,
    projection_policy_from_config,
    record_source_redaction_attention,
)
from work_buddy.security.actors import ActorRef
from work_buddy.sources.models import SourceRef, SourceResolutionRecord
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import new_id
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.store import TruthStore
from work_buddy.truth.source_provenance import (
    record_evidence_source_resolution,
    record_source_usage_event,
)


NOW = "2026-08-09T12:00:00.000+00:00"
LATER = "2026-08-09T12:01:00.000+00:00"
HUMAN = Actor("human", "projection-reviewer")


def _profile() -> dict[str, object]:
    return {
        "store_id": new_id(),
        "profile": "hindsight-projection-test",
        "title": "Hindsight projection test",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": False,
        "support_policy": {
            "fact": {
                "minimum_usable_supports": 0,
                "allowed_effects": ["supports"],
                "allow_human_assertion_as_source": True,
            }
        },
    }


@pytest.fixture
def truth_store(tmp_path: Path) -> TruthStore:
    return TruthStore.create(tmp_path / "truth", _profile())


@pytest.fixture
def enabled_policy() -> TruthHindsightProjectionPolicy:
    return TruthHindsightProjectionPolicy(
        enabled=True,
        policy_id="confirmed_current_v1",
        authorization_ref="policy:truth-hindsight:test",
    )


def _propose(store: TruthStore, proposition: str = "The migration completed."):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
        created_at=NOW,
        status_at=NOW,
    ).claim


def _confirm(
    store: TruthStore,
    claim,
    *,
    conn=None,
):
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="confirm",
        displayed_payload_sha256=claim.canonical_sha256,
        at=LATER,
        conn=conn,
    )
    return lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
        at=LATER,
        conn=conn,
    )


def test_policy_rollout_is_explicit_and_never_invents_enabled_authorization() -> None:
    disabled = projection_policy_from_config({}, store_id="a" * 32)
    assert disabled.enabled is False

    with pytest.raises(ValueError, match="requires authorization_ref"):
        projection_policy_from_config(
            {"hindsight": {"truth_projection": {"enabled": True}}},
            store_id="a" * 32,
        )


def test_reader_projects_only_exact_current_confirmed_claim(
    truth_store: TruthStore,
    enabled_policy: TruthHindsightProjectionPolicy,
) -> None:
    claim = _propose(truth_store)
    _confirm(truth_store, claim)
    reader = TruthStoreProjectionReader(truth_store, policy=enabled_policy)

    desired = reader.desired_for_claim(
        claim.id,
        enabled_policy.policy_id,
        at=LATER,
    )
    snapshot = reader.resolve_snapshot(desired, at=LATER)

    assert desired.desired_state is DesiredProjectionState.UPSERT
    assert desired.reason_code == "claim_confirmed"
    assert snapshot.proposition == claim.proposition
    assert snapshot.lifecycle_status == "confirmed"
    assert snapshot.claim_canonical_sha256 == claim.canonical_sha256
    assert snapshot.source_dependencies == ()


def test_policy_change_has_a_new_generation_and_disables_existing_projection(
    truth_store: TruthStore,
    enabled_policy: TruthHindsightProjectionPolicy,
) -> None:
    claim = _propose(truth_store)
    _confirm(truth_store, claim)
    enabled = TruthStoreProjectionReader(truth_store, policy=enabled_policy)
    enabled_intent = enabled.desired_for_claim(
        claim.id, enabled_policy.policy_id, at=LATER
    )
    disabled_policy = TruthHindsightProjectionPolicy(
        enabled=False,
        policy_id=enabled_policy.policy_id,
        authorization_ref="policy:truth-hindsight:disabled",
    )
    disabled_intent = TruthStoreProjectionReader(
        truth_store, policy=disabled_policy
    ).desired_for_claim(claim.id, disabled_policy.policy_id, at=LATER)

    assert disabled_intent.desired_state is DesiredProjectionState.REMOVE
    assert disabled_intent.reason_code == "projection_policy_disabled"
    assert disabled_intent.claim_generation != enabled_intent.claim_generation


def test_crossing_valid_time_boundary_creates_a_new_outbox_generation(
    truth_store: TruthStore,
    enabled_policy: TruthHindsightProjectionPolicy,
) -> None:
    claim = truth_store.propose_claim(
        proposition="The migration window is active.",
        claim_kind="fact",
        actor=HUMAN,
        valid_from=NOW,
        valid_to="2026-08-09T12:02:00.000+00:00",
        created_at=NOW,
        status_at=NOW,
    ).claim
    _confirm(truth_store, claim)
    reader = TruthStoreProjectionReader(truth_store, policy=enabled_policy)
    active = reader.desired_for_claim(
        claim.id,
        enabled_policy.policy_id,
        at=LATER,
    )
    expired = reader.desired_for_claim(
        claim.id,
        enabled_policy.policy_id,
        at="2026-08-09T12:03:00.000+00:00",
    )

    assert active.desired_state is DesiredProjectionState.UPSERT
    assert expired.desired_state is DesiredProjectionState.REMOVE
    assert expired.reason_code == "claim_expired"
    assert active.claim_generation != expired.claim_generation

    # The generation distinction is operationally required: both immutable
    # intents must coexist so the later expiry can supersede the live upsert.
    projection_store = TruthHindsightProjectionStore(truth_store.paths.db)
    projection_store.enqueue(active)
    projection_store.enqueue(expired)
    assert projection_store.current_effect(
        claim.id, enabled_policy.policy_id
    ).spec.request_sha256 == expired.request_sha256


def test_lifecycle_and_projection_intent_share_the_truth_transaction(
    truth_store: TruthStore,
    enabled_policy: TruthHindsightProjectionPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _propose(truth_store)
    monkeypatch.setattr(
        "work_buddy.hindsight_projection.truth_reader.configured_projection_policy",
        lambda _store: enabled_policy,
    )

    with pytest.raises(RuntimeError, match="rollback fixture"):
        with truth_store.write_transaction() as conn:
            _confirm(truth_store, claim, conn=conn)
            row = conn.execute(
                "SELECT desired_state FROM truth_hindsight_projection_outbox "
                "WHERE claim_id = ?",
                (claim.id,),
            ).fetchone()
            assert row is not None and row["desired_state"] == "upsert"
            raise RuntimeError("rollback fixture")

    assert TruthLifecycle(truth_store).latest_status(claim.id).status == "proposed"
    assert TruthHindsightProjectionStore(truth_store.paths.db).current_effect(
        claim.id, enabled_policy.policy_id
    ) is None

    _confirm(truth_store, claim)
    effect = TruthHindsightProjectionStore(truth_store.paths.db).current_effect(
        claim.id, enabled_policy.policy_id
    )
    assert effect is not None
    assert effect.spec.desired_state is DesiredProjectionState.UPSERT


def test_source_redaction_becomes_truth_attention_and_purge_intent(
    tmp_path: Path,
    enabled_policy: TruthHindsightProjectionPolicy,
) -> None:
    profile = _profile()
    profile["support_policy"] = {
        "fact": {
            "minimum_usable_supports": 1,
            "allowed_effects": ["supports"],
            "allow_human_assertion_as_source": True,
        }
    }
    store = TruthStore.create(tmp_path / "source-truth", profile)
    claim = _propose(store, "The migration completed successfully.")
    exact = "migration completed successfully"
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///migration-report.md",
        actor=HUMAN,
        acquisition_method="paste",
        content=exact,
        created_at=NOW,
        acquired_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=exact),
        actor=HUMAN,
        created_at=NOW,
    )
    store.add_link(
        from_claim_id=claim.id,
        link_type="evidence_relation",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        role={
            "schema": "claim-evidence/v1",
            "evidential_effect": "supports",
            "derivation_relationship": "paraphrase",
        },
        created_at=NOW,
    )
    source_ref = SourceRef("authority1", "sourceitem1")
    source_actor = ActorRef("authority1", "truth-service", "service", "tenant001")
    resolution = record_evidence_source_resolution(
        store,
        evidence_id=evidence.id,
        resolution=SourceResolutionRecord(
            source_ref=source_ref,
            representation_id="representation-0001",
            content_sha256=evidence.content_sha256,
            media_type="text/plain",
            byte_length=len(exact.encode("utf-8")),
            selector={"kind": "whole"},
            excerpt=None,
            resolver_id="fixture",
            resolver_version="1",
            observation_id="observation-0001",
            redaction_epoch=0,
            resolved_at=NOW,
        ),
        usage_id="truth-source-usage-0001",
        authorization_context_sha256="a" * 64,
        actor=source_actor,
        created_at=NOW,
    )
    record_source_usage_event(
        store,
        resolution_record_id=resolution.id,
        usage_id=resolution.usage_id,
        status="acknowledged",
        purpose="truth_claim_proposal",
        consumer_ref=f"truth-claim:{claim.id}",
        redaction_epoch=0,
        actor=source_actor,
        created_at=NOW,
    )
    _confirm(store, claim)
    reader = TruthStoreProjectionReader(store, policy=enabled_policy)
    upsert = reader.desired_for_claim(claim.id, enabled_policy.policy_id, at=LATER)
    assert upsert.desired_state is DesiredProjectionState.UPSERT
    assert len(reader.resolve_snapshot(upsert, at=LATER).source_dependencies) == 1
    projection_store = TruthHindsightProjectionStore(store.paths.db)
    projection_store.enqueue(upsert)

    remove = record_source_redaction_attention(
        store,
        claim_id=claim.id,
        source_ref=source_ref.uri,
        representation_id="representation-0001",
        redaction_event_id="source-redaction-0001",
        redaction_epoch=1,
        actor=source_actor,
        at="2026-08-09T12:02:00.000+00:00",
        policy=enabled_policy,
    )

    assert remove.desired_state is DesiredProjectionState.REMOVE
    assert remove.reason_code == "source_redacted"
    assert remove.purge_projection_source is True
    assert projection_store.current_effect(
        claim.id, enabled_policy.policy_id
    ).spec.request_sha256 == remove.request_sha256
