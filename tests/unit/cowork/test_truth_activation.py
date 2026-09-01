from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from work_buddy.cowork.truth_activation import (
    PROVENANCE_DOCUMENT_CONTRACT,
    WORKING_DOCUMENT_CONTRACT,
    TruthActivationError,
    abort_document_admission,
    bind_pending_document_admission_decision,
    commit_document_admission,
    provision_document_policy,
    require_truth_access,
    resolve_document_truth_policy,
    transition_document_truth_activation,
)
from work_buddy.cowork.truth_analysis import TruthAnalysisError, _require_run_truth
from work_buddy.truth import documents, expressions
from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor
from work_buddy.truth.identity import sha256_bytes


HUMAN = Actor("human", "truth-policy-test")


def _document(
    store_ctx,
    name: str,
    *,
    contract: str = WORKING_DOCUMENT_CONTRACT,
    activation: str | None = "disabled",
    commit_admission: bool = True,
    decision_id: str | None = None,
    decision_sha256: str | None = None,
):
    body = f"{name} note"
    store = store_ctx["store"]
    with store.write_transaction() as conn:
        document = documents.register_document(
            store,
            path=f"docs/{name}.md",
            title=name,
            document_class="co_authored",
            content_sha256=sha256_bytes(body.encode("utf-8")),
            actor=HUMAN,
            conn=conn,
        )
        policy = provision_document_policy(
            store,
            document_id=document.id,
            interaction_contract_id=contract,
            initial_activation=activation,
            actor=HUMAN,
            intent_id=f"{name}:create",
            coordinator_decision_id=decision_id,
            coordinator_decision_sha256=decision_sha256,
            commit_admission=commit_admission,
            conn=conn,
        )
    return document, policy


def test_allowed_activation_is_cas_guarded_and_pauses_retained_truth(store_ctx):
    store = store_ctx["store"]
    document, policy = _document(store_ctx, "activation-laws")

    assert policy.activation_state == "disabled"
    assert policy.truth_mutable is False
    assert policy.provenance_enabled is True
    with pytest.raises(TruthActivationError, match="not enabled"):
        require_truth_access(store, document.id, mutation=True)

    enabled = transition_document_truth_activation(
        store,
        document_id=document.id,
        next_state="enabled",
        expected_activation_revision=1,
        actor=HUMAN,
        intent_id="activation-laws:enable",
    )
    assert enabled.activation_revision == 2
    with pytest.raises(TruthActivationError) as stale:
        transition_document_truth_activation(
            store,
            document_id=document.id,
            next_state="disabled",
            expected_activation_revision=1,
            actor=HUMAN,
            intent_id="activation-laws:stale",
        )
    assert stale.value.code == "activation_revision_conflict"

    disabled = transition_document_truth_activation(
        store,
        document_id=document.id,
        next_state="disabled",
        expected_activation_revision=2,
        actor=HUMAN,
        intent_id="activation-laws:disable-empty",
    )
    assert disabled.activation_revision == 3
    transition_document_truth_activation(
        store,
        document_id=document.id,
        next_state="enabled",
        expected_activation_revision=3,
        actor=HUMAN,
        intent_id="activation-laws:reenable",
    )

    claim = store.propose_claim(
        proposition="The task note retains a claim.",
        claim_kind="fact",
        actor=HUMAN,
    ).claim
    span = expressions.ensure_document_span(
        store,
        document_id=document.id,
        selector=CompositeSelector(exact="activation-laws note"),
        quote_exact="activation-laws note",
        actor=HUMAN,
    )
    expressions.mark_expression(
        store,
        document_span_id=span.id,
        claim_ref=claim.id,
        role="quote",
        actor=HUMAN,
    )
    with pytest.raises(TruthActivationError) as unsafe_disable:
        transition_document_truth_activation(
            store,
            document_id=document.id,
            next_state="disabled",
            expected_activation_revision=4,
            actor=HUMAN,
            intent_id="activation-laws:disable-with-ledger",
        )
    assert unsafe_disable.value.code == "invalid_activation_transition"

    paused = transition_document_truth_activation(
        store,
        document_id=document.id,
        next_state="paused",
        expected_activation_revision=4,
        actor=HUMAN,
        intent_id="activation-laws:pause",
    )
    assert paused.truth_observable is True
    assert paused.truth_mutable is False
    require_truth_access(store, document.id, mutation=False)
    with pytest.raises(TruthActivationError) as paused_write:
        require_truth_access(store, document.id, mutation=True)
    assert paused_write.value.code == "truth_paused"


def test_unsupported_contract_has_receipt_without_activation_or_claim_access(store_ctx):
    store = store_ctx["store"]
    document, policy = _document(
        store_ctx,
        "provenance-only",
        contract=PROVENANCE_DOCUMENT_CONTRACT,
        activation=None,
    )

    assert policy.eligibility == "unsupported"
    assert policy.activation_state is None
    assert policy.admission_state is None
    assert policy.recovery_reason is None
    assert policy.provenance_enabled is True
    assert policy.truth_observable is False
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM document_truth_policy_receipts "
            "WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM document_truth_activation_transitions "
            "WHERE document_id = ?",
            (document.id,),
        ).fetchone()[0] == 0


def test_admission_stage_commit_abort_and_contract_immutability(store_ctx):
    store = store_ctx["store"]
    decision_sha = "a" * 64
    first, staged = _document(
        store_ctx,
        "staged-commit",
        activation="enabled",
        decision_id="task-create:one",
        decision_sha256=decision_sha,
        commit_admission=False,
    )
    assert staged.admission_state == "pending"
    assert staged.truth_mutable is False
    committed = commit_document_admission(
        store,
        document_id=first.id,
        expected_seal_revision=1,
        coordinator_decision_id="task-create:one",
        coordinator_decision_sha256=decision_sha,
        actor=HUMAN,
    )
    assert committed.admission_state == "committed"
    assert committed.truth_mutable is True
    replay = commit_document_admission(
        store,
        document_id=first.id,
        expected_seal_revision=1,
        coordinator_decision_id="task-create:one",
        coordinator_decision_sha256=decision_sha,
        actor=HUMAN,
    )
    assert replay.admission_seal_revision == 2

    second, _ = _document(
        store_ctx,
        "staged-abort",
        decision_id="task-create:two",
        decision_sha256="b" * 64,
        commit_admission=False,
    )
    aborted = abort_document_admission(
        store,
        document_id=second.id,
        expected_seal_revision=1,
        coordinator_decision_id="task-create:two",
        coordinator_decision_sha256="b" * 64,
        actor=HUMAN,
    )
    assert aborted.admission_state == "aborted"
    assert aborted.truth_mutable is False

    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE interaction_contract_definitions "
                "SET definition_json = '{}' WHERE contract_id = ?",
                (WORKING_DOCUMENT_CONTRACT,),
            )


def test_pending_admission_append_binds_final_coordinator_decision(store_ctx):
    store = store_ctx["store"]
    document, staged = _document(
        store_ctx,
        "decision-binding",
        activation="enabled",
        decision_id="provisional-task-decision",
        decision_sha256="a" * 64,
        commit_admission=False,
    )
    assert staged.admission_seal_revision == 1

    bound = bind_pending_document_admission_decision(
        store,
        document_id=document.id,
        intent_id="decision-binding:create",
        expected_seal_revision=1,
        provisional_coordinator_decision_id="provisional-task-decision",
        provisional_coordinator_decision_sha256="a" * 64,
        coordinator_decision_id="final-task-decision",
        coordinator_decision_sha256="b" * 64,
        actor=HUMAN,
    )
    replay = bind_pending_document_admission_decision(
        store,
        document_id=document.id,
        intent_id="decision-binding:create",
        expected_seal_revision=1,
        provisional_coordinator_decision_id="provisional-task-decision",
        provisional_coordinator_decision_sha256="a" * 64,
        coordinator_decision_id="final-task-decision",
        coordinator_decision_sha256="b" * 64,
        actor=HUMAN,
    )

    assert bound.admission_state == "pending"
    assert bound.admission_seal_revision == replay.admission_seal_revision == 2
    assert bound.coordinator_decision_id == "final-task-decision"
    with pytest.raises(TruthActivationError) as changed:
        bind_pending_document_admission_decision(
            store,
            document_id=document.id,
            intent_id="decision-binding:create",
            expected_seal_revision=1,
            provisional_coordinator_decision_id="provisional-task-decision",
            provisional_coordinator_decision_sha256="a" * 64,
            coordinator_decision_id="another-final-decision",
            coordinator_decision_sha256="c" * 64,
            actor=HUMAN,
        )
    assert changed.value.code == "admission_seal_revision_conflict"

    committed = commit_document_admission(
        store,
        document_id=document.id,
        expected_seal_revision=2,
        coordinator_decision_id="final-task-decision",
        coordinator_decision_sha256="b" * 64,
        actor=HUMAN,
    )
    assert committed.admission_state == "committed"
    assert committed.admission_seal_revision == 3
    with store.connect() as conn:
        states = conn.execute(
            "SELECT state FROM document_truth_admission_seal_events "
            "WHERE document_id=? ORDER BY seal_revision",
            (document.id,),
        ).fetchall()
    assert [str(row["state"]) for row in states] == [
        "pending",
        "pending",
        "committed",
    ]


def test_policy_projection_is_resolved_from_append_only_history(store_ctx):
    store = store_ctx["store"]
    document, expected = _document(
        store_ctx, "projection", activation="enabled"
    )
    observed = resolve_document_truth_policy(store, document.id)
    assert observed.policy_fingerprint == expected.policy_fingerprint
    assert observed.to_dict()["capabilities"] == {
        "provenance": True,
        "truth_observe": True,
        "truth_mutate": True,
        "truth_analysis": True,
    }


def test_analysis_run_is_fenced_when_activation_revision_changes(store_ctx):
    store = store_ctx["store"]
    document, policy = _document(store_ctx, "analysis-race", activation="enabled")
    run = SimpleNamespace(
        document_id=document.id,
        activation_revision=policy.activation_revision,
    )

    _require_run_truth(store, run)
    transition_document_truth_activation(
        store,
        document_id=document.id,
        next_state="disabled",
        expected_activation_revision=policy.activation_revision,
        actor=HUMAN,
        intent_id="analysis-race:disable",
    )

    with pytest.raises(TruthAnalysisError) as stale:
        _require_run_truth(store, run)
    assert stale.value.code == "truth_activation_changed"
    assert stale.value.details == {"reason": "activation_revision_conflict"}


def test_document_summary_and_projection_opt_out_are_policy_aware(
    client, seeded, monkeypatch
):
    store = seeded["store"]
    document = seeded["document"]

    listing = client.get(f"/api/truth/doc/list?store_id={store.store_id}")
    assert listing.status_code == 200
    summary = listing.get_json()["docs"][0]
    assert summary["capability_envelope"]["schema"] == (
        "wb.cowork-document-capabilities/v1"
    )
    assert summary["capability_envelope"]["truth"]["activation"] == "enabled"

    from work_buddy.cowork import api

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled Truth projection queried claim/expression state")

    monkeypatch.setattr(api.expressions, "expressions_for_document", forbidden)
    monkeypatch.setattr(api.queries, "resolve_claim_states", forbidden)
    response = client.get(
        f"/api/truth/doc/{document.id}?store_id={store.store_id}&include_truth=0"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["expressions"] == []
    assert payload["truth_projection_included"] is False
    assert payload["provenance"] is not None
    assert payload["capability_envelope"]["modules"]["truth"] is True

    invalid = client.get(
        f"/api/truth/doc/{document.id}?store_id={store.store_id}&include_truth=maybe"
    )
    assert invalid.status_code == 400
