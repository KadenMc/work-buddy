"""Lifecycle, gesture, and atomic belief-revision tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    InvariantViolation,
    TransitionError,
)
from work_buddy.truth.identity import new_id, truth_uri
from work_buddy.truth.lifecycle import TruthLifecycle, hash_context
from work_buddy.truth.store import AcquisitionOrigin, PremiseRef, TruthStore


NOW = "2026-07-14T10:00:00.000+00:00"
LATER = "2026-07-14T10:01:00.000+00:00"
TWO_HOURS = "2026-07-14T12:00:00.000+00:00"
HUMAN = Actor("human", "user-1")
OTHER_HUMAN = Actor("human", "user-2")
SYSTEM = Actor("system", "truth-sweep")
AGENT = Actor(
    "agent_run",
    "run-1",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "unit",
        "session_id": "session-1",
        "call_id": "call-1",
    },
)


def _profile(*, store_id: str | None = None) -> dict[str, object]:
    return {
        "store_id": store_id or new_id(),
        "profile": "lifecycle-test",
        "title": "Lifecycle test store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "proposal_max_age": "2h",
    }


@pytest.fixture
def store(truth_root: Path) -> TruthStore:
    return TruthStore.create(truth_root, _profile())


@pytest.fixture
def lifecycle(store: TruthStore) -> TruthLifecycle:
    return TruthLifecycle(store)


def _claim(
    store: TruthStore,
    proposition: str,
    *,
    kind: str = "fact",
    actor: Actor = HUMAN,
    valid_from: str | None = None,
    valid_to: str | None = None,
    conn: sqlite3.Connection | None = None,
):
    return store.propose_claim(
        proposition=proposition,
        claim_kind=kind,
        actor=actor,
        valid_from=valid_from,
        valid_to=valid_to,
        created_at=NOW,
        status_at=NOW,
        conn=conn,
    ).claim


def _gesture(
    lifecycle: TruthLifecycle,
    subject,
    *,
    kind: str = "confirm",
    context: str | None = None,
    actor: Actor = HUMAN,
    surface: str = "dashboard",
    at: str = LATER,
    expires_at: str | None = None,
    conn: sqlite3.Connection | None = None,
):
    return lifecycle.mint_gesture(
        subject_ref=subject.id,
        actor=actor,
        surface=surface,
        kind=kind,
        displayed_payload_sha256=(
            subject.canonical_sha256
            if hasattr(subject, "canonical_sha256")
            else subject.content_sha256
            if hasattr(subject, "content_sha256")
            else subject.span_sha256
        ),
        context_sha256=context,
        at=at,
        expires_at=expires_at,
        conn=conn,
    )


def _confirm(
    lifecycle: TruthLifecycle,
    claim,
    *,
    kind: str = "confirm",
    context: str | None = None,
    at: str = LATER,
    conn: sqlite3.Connection | None = None,
):
    gesture = _gesture(
        lifecycle,
        claim,
        kind=kind,
        context=context,
        at=at,
        conn=conn,
    )
    result = lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=context,
        observed_at=at,
        at=at,
        conn=conn,
    )
    return result, gesture


def _support(
    store: TruthStore,
    claim,
    *,
    content: str,
    actor: Actor = HUMAN,
    origin: AcquisitionOrigin | None = None,
    external_reviewed: bool = False,
    derived_from_store: str | None = None,
):
    method = "fetch" if origin == AcquisitionOrigin.EXTERNAL else "paste"
    evidence = store.capture_evidence(
        kind="document",
        source_locator=f"file:///{new_id()}.md",
        actor=actor,
        acquisition_method=method,
        content=content,
        origin=origin,
        external_reviewed=external_reviewed,
        derived_from_store=derived_from_store,
        created_at=NOW,
        acquired_at=NOW,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact=content),
        actor=actor,
        created_at=NOW,
    )
    store.add_link(
        from_claim_id=claim.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=actor,
        created_at=NOW,
    )
    return evidence, span


def test_confirmation_is_human_gestured_exact_and_idempotent(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "The migration completed")
    context = hash_context({"receipts": ["migration.log"]})
    result, gesture = _confirm(lifecycle, claim, context=context)

    assert result.created is True
    assert result.event is not None and result.event.status == "confirmed"
    assert result.gesture.consumed_at == LATER
    assert lifecycle.latest_status(claim.id).status == "confirmed"
    with pytest.raises(GestureError, match="consumed"):
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=context,
            observed_at=LATER,
        )

    fresh = _gesture(lifecycle, claim, context=context)
    with pytest.raises(TransitionError, match="already confirmed"):
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=fresh.id,
            actor=HUMAN,
            expected_context_sha256=context,
            observed_at=LATER,
        )
    assert lifecycle.verify_gesture(
        fresh.id,
        actor=HUMAN,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        expected_context_sha256=context,
        allowed_kinds={"confirm"},
        observed_at=LATER,
    ).consumed_at is None
    conn = store.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM claim_status_events "
            "WHERE claim_id = ? AND status = 'confirmed'",
            (claim.id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("actor", "actor"),
        ("subject", "subject"),
        ("payload", "payload"),
        ("context", "context"),
        ("kind", "kind"),
    ],
)
def test_gesture_verification_fails_closed_on_every_binding(
    store: TruthStore,
    lifecycle: TruthLifecycle,
    mutation: str,
    match: str,
):
    claim = _claim(store, f"Binding check {mutation}")
    other = _claim(store, f"Other binding {mutation}")
    context = hash_context({"claim": claim.id})
    gesture = _gesture(lifecycle, claim, context=context)
    kwargs = {
        "actor": HUMAN,
        "subject_ref": claim.id,
        "payload_sha256": claim.canonical_sha256,
        "expected_context_sha256": context,
        "allowed_kinds": {"confirm"},
        "observed_at": LATER,
    }
    if mutation == "actor":
        kwargs["actor"] = OTHER_HUMAN
    elif mutation == "subject":
        kwargs["subject_ref"] = other.id
    elif mutation == "payload":
        kwargs["payload_sha256"] = "a" * 64
    elif mutation == "context":
        kwargs["expected_context_sha256"] = "b" * 64
    else:
        kwargs["allowed_kinds"] = {"reject_plain"}
    with pytest.raises(GestureError, match=match):
        lifecycle.verify_gesture(gesture.id, **kwargs)
    assert lifecycle.verify_gesture(
        gesture.id,
        actor=HUMAN,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        expected_context_sha256=context,
        allowed_kinds={"confirm"},
        observed_at=LATER,
    ).consumed_at is None


def test_gesture_mint_is_server_composed_and_deferred_expiry_is_exact(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "A deferred decision")
    with pytest.raises(GestureError, match="displayed payload"):
        lifecycle.mint_gesture(
            subject_ref=claim.id,
            actor=HUMAN,
            surface="dashboard",
            kind="confirm",
            displayed_payload_sha256="f" * 64,
            at=NOW,
        )
    with pytest.raises(GestureError, match="later"):
        _gesture(
            lifecycle,
            claim,
            at=NOW,
            expires_at=NOW,
        )

    gesture = _gesture(
        lifecycle,
        claim,
        at=NOW,
        expires_at=LATER,
    )
    with pytest.raises(GestureError, match="cannot predate"):
        lifecycle.verify_gesture(
            gesture.id,
            actor=HUMAN,
            subject_ref=claim.id,
            payload_sha256=claim.canonical_sha256,
            expected_context_sha256=None,
            allowed_kinds={"confirm"},
            observed_at="2026-07-14T09:59:59.999+00:00",
        )
    lifecycle.verify_gesture(
        gesture.id,
        actor=HUMAN,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        expected_context_sha256=None,
        allowed_kinds={"confirm"},
        observed_at="2026-07-14T10:00:59.999+00:00",
    )
    with pytest.raises(GestureError, match="expired"):
        lifecycle.verify_gesture(
            gesture.id,
            actor=HUMAN,
            subject_ref=claim.id,
            payload_sha256=claim.canonical_sha256,
            expected_context_sha256=None,
            allowed_kinds={"confirm"},
            observed_at=LATER,
        )


def test_redaction_gesture_seam_handles_claim_evidence_and_span_subjects(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Sensitive assertion")
    evidence, span = _support(store, claim, content="private receipt")
    subjects = (
        (claim, claim.canonical_sha256),
        (evidence, evidence.content_sha256),
        (span, span.span_sha256),
    )
    for subject, digest in subjects:
        context = hash_context({"redact": subject.id})
        gesture = _gesture(
            lifecycle,
            subject,
            kind="redact",
            context=context,
        )
        consumed = lifecycle.verify_and_consume_gesture(
            gesture.id,
            actor=HUMAN,
            subject_ref=subject.id,
            payload_sha256=digest,
            expected_context_sha256=context,
            allowed_kinds={"redact"},
            observed_at=LATER,
        )
        assert consumed.consumed_at == LATER


def test_agent_confirmation_is_structurally_refused(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Humans confirm assertions", actor=AGENT)
    gesture = _gesture(lifecycle, claim)
    with pytest.raises(GestureError, match="human"):
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=AGENT,
            expected_context_sha256=None,
            observed_at=LATER,
        )
    assert lifecycle.latest_status(claim.id).status == "proposed"


def test_profile_confirmation_surface_is_enforced_at_decision_time(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Surface policy applies now")
    gesture = _gesture(lifecycle, claim, surface="chat_consent")
    with pytest.raises(GestureError, match="not allowed"):
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )
    assert lifecycle.verify_gesture(
        gesture.id,
        actor=HUMAN,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        expected_context_sha256=None,
        allowed_kinds={"confirm"},
        observed_at=LATER,
    ).consumed_at is None


def test_needs_review_is_rule_only_overlay_and_human_reaffirmation_clears_it(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Review overlays base truth")
    confirmed, _ = _confirm(lifecycle, claim)
    with pytest.raises(TransitionError, match="sweep or rule"):
        lifecycle.mark_needs_review(
            claim_id=claim.id,
            actor=HUMAN,
            basis_kind="rule",
        )
    review = lifecycle.mark_needs_review(
        claim_id=claim.id,
        actor=SYSTEM,
        basis_kind="sweep",
        basis_ref=new_id(),
        at=NOW,
    )
    assert review.created is True
    assert lifecycle.latest_status(claim.id).status == "needs_review"
    assert lifecycle.latest_status(claim.id, include_overlay=False).status == "confirmed"
    duplicate = lifecycle.mark_needs_review(
        claim_id=claim.id,
        actor=SYSTEM,
        basis_kind="sweep",
    )
    assert duplicate.created is False

    reaffirmed, _ = _confirm(lifecycle, claim, kind="reaffirm", at=NOW)
    assert reaffirmed.created is True
    assert reaffirmed.event is not None
    assert reaffirmed.event.seq > review.event.seq
    assert lifecycle.latest_status(claim.id).status == "confirmed"
    assert confirmed.event is not None
    assert reaffirmed.event.seq > confirmed.event.seq


def test_proposed_retracted_and_terminal_transitions_are_append_only(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Withdraw this proposal")
    event_id = new_id()
    result = lifecycle.transition_claim(
        claim_id=claim.id,
        status="retracted",
        actor=HUMAN,
        basis_kind="redaction",
        basis_ref=new_id(),
        event_id=event_id,
        at=LATER,
    )
    assert result.event.id == event_id
    assert result.previous_status == "proposed"
    duplicate = lifecycle.transition_claim(
        claim_id=claim.id,
        status="retracted",
        actor=HUMAN,
        basis_kind="redaction",
        basis_ref=new_id(),
    )
    assert duplicate.created is False
    with pytest.raises(TransitionError, match="terminal"):
        lifecycle.mark_needs_review(
            claim_id=claim.id,
            actor=SYSTEM,
            basis_kind="rule",
        )
    with pytest.raises(TransitionError, match="specialized"):
        lifecycle.transition_claim(
            claim_id=claim.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
        )


def test_confirmed_and_challenged_can_retract(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    confirmed_claim = _claim(store, "Confirmed then withdrawn")
    _confirm(lifecycle, confirmed_claim)
    assert lifecycle.transition_claim(
        claim_id=confirmed_claim.id,
        status="retracted",
        actor=SYSTEM,
        basis_kind="policy",
        basis_ref="policy-1",
    ).event.status == "retracted"

    challenged_claim = _claim(store, "Challenged then withdrawn")
    challenger = _claim(store, "Counter evidence exists")
    _support(store, challenger, content="counter receipt")
    _confirm(lifecycle, challenged_claim)
    lifecycle.challenge_claim(
        claim_id=challenged_claim.id,
        challenging_claim_id=challenger.id,
        actor=AGENT,
    )
    assert lifecycle.transition_claim(
        claim_id=challenged_claim.id,
        status="retracted",
        actor=HUMAN,
        basis_kind="redaction",
        basis_ref=new_id(),
    ).event.status == "retracted"


def test_challenge_requires_conflict_edge_and_evidence_then_can_reaffirm(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    target = _claim(store, "Release is ready")
    challenger = _claim(store, "Release check is failing", actor=AGENT)
    _confirm(lifecycle, target)
    with pytest.raises(TransitionError, match="supporting evidence"):
        lifecycle.challenge_claim(
            claim_id=target.id,
            challenging_claim_id=challenger.id,
            actor=AGENT,
        )
    _, span = _support(store, challenger, content="check failed", actor=AGENT)
    challenged = lifecycle.challenge_claim(
        claim_id=target.id,
        challenging_claim_id=challenger.id,
        actor=AGENT,
        note="CI receipt conflicts",
    )
    assert challenged.event.status == "challenged"
    conn = store.connect()
    try:
        conflict = conn.execute(
            "SELECT * FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'conflicts_with' AND to_ref = ?",
            (challenger.id, target.id),
        ).fetchone()
        support = conn.execute(
            "SELECT 1 FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'supports_span' AND to_ref = ?",
            (challenger.id, span.id),
        ).fetchone()
    finally:
        conn.close()
    assert conflict is not None and support is not None

    reaffirmed, _ = _confirm(lifecycle, target, kind="reaffirm")
    assert reaffirmed.event is not None
    assert reaffirmed.event.status == "confirmed"


def test_weakest_link_blocks_local_premises_until_all_are_confirmed(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    first = _claim(store, "Input one")
    second = _claim(store, "Input two")
    derived = _claim(store, "Conclusion")
    store.add_derivation(
        claim_id=derived.id,
        method="deduction",
        premises=[first.id, second.id],
        actor=AGENT,
    )
    assessment = lifecycle.assess_premises(derived.id)
    assert assessment.confirmed is False
    assert set(assessment.local_unconfirmed) == {first.id, second.id}
    gesture = _gesture(lifecycle, derived)
    with pytest.raises(TransitionError, match="weakest-link"):
        lifecycle.confirm_claim(
            claim_id=derived.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )
    _confirm(lifecycle, first)
    _confirm(lifecycle, second)
    result = lifecycle.confirm_claim(
        claim_id=derived.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
    )
    assert result.event is not None and result.event.status == "confirmed"


def test_wb_truth_uri_premises_resolve_same_store_and_fail_soft_cross_store(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    premise = _claim(store, "URI input")
    conclusion = _claim(store, "URI conclusion")
    store.add_derivation(
        claim_id=conclusion.id,
        method="uri-test",
        premises=[PremiseRef("uri", truth_uri(store.store_id, "claim", premise.id))],
        actor=AGENT,
    )
    before = lifecycle.assess_premises(conclusion.id)
    assert before.local_unconfirmed == (premise.id,)
    assert before.unresolved_uris == ()
    _confirm(lifecycle, premise)
    assert lifecycle.assess_premises(conclusion.id).confirmed is True

    foreign_uri = truth_uri(new_id(), "claim", new_id())
    cross_store = _claim(store, "Cross-store conclusion")
    store.add_derivation(
        claim_id=cross_store.id,
        method="cross-store",
        premises=[PremiseRef("uri", foreign_uri)],
        actor=AGENT,
    )
    assessment = lifecycle.assess_premises(cross_store.id)
    assert assessment.unresolved_uris == (foreign_uri,)
    gesture = _gesture(lifecycle, cross_store)
    with pytest.raises(TransitionError, match="weakest-link"):
        lifecycle.confirm_claim(
            claim_id=cross_store.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )
    assert lifecycle.latest_status(cross_store.id).status == "proposed"


def test_quarantined_support_requires_explicit_override_gesture(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "External report is accurate")
    _support(
        store,
        claim,
        content="unreviewed external report",
        origin=AcquisitionOrigin.EXTERNAL,
    )
    support = lifecycle.assess_support(claim.id)
    assert support.quarantined_only is True
    ordinary = _gesture(lifecycle, claim)
    with pytest.raises(GestureError, match="kind"):
        lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=ordinary.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )
    override = _gesture(lifecycle, claim, kind="confirm_quarantined_support")
    confirmed = lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=override.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=LATER,
    )
    assert confirmed.event is not None and confirmed.event.status == "confirmed"


def test_store_derived_support_never_bootstraps_truth_and_agent_only_is_flagged(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    circular = _claim(store, "The store proves itself")
    _support(
        store,
        circular,
        content="generated projection",
        derived_from_store=store.store_id,
    )
    assessment = lifecycle.assess_support(circular.id)
    assert assessment.store_derived_only is True
    assert assessment.usable_span_ids == ()
    gesture = _gesture(lifecycle, circular)
    with pytest.raises(TransitionError, match="non-store-derived"):
        lifecycle.confirm_claim(
            claim_id=circular.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )

    agent_claim = _claim(store, "Agent-authored observation", actor=AGENT)
    _support(store, agent_claim, content="agent output", actor=AGENT)
    agent_support = lifecycle.assess_support(agent_claim.id)
    assert agent_support.agent_authored_only is True
    confirmed, _ = _confirm(lifecycle, agent_claim)
    assert confirmed.event is not None and confirmed.event.status == "confirmed"


@pytest.mark.parametrize(
    "reason",
    [
        "updated",
        "corrected",
        "refined",
        "valid_time_closed",
        "source_retracted",
        "preference_changed",
    ],
)
def test_all_typed_supersession_reasons_are_durable(
    store: TruthStore,
    lifecycle: TruthLifecycle,
    reason: str,
):
    predecessor = _claim(store, f"Predecessor {reason}")
    _confirm(lifecycle, predecessor)
    dated_from = reason in {"updated", "preference_changed"}
    successor = _claim(
        store,
        f"Successor {reason}",
        valid_from="2026-07-15" if dated_from else None,
        valid_to="2026-07-15" if reason == "valid_time_closed" else None,
    )
    link = lifecycle.supersede_claim(
        successor_claim_id=successor.id,
        predecessor_claim_id=predecessor.id,
        reason=reason,
        actor=HUMAN,
    )
    assert reason in str(link.role_json)
    assert lifecycle.latest_status(predecessor.id).status == "confirmed"
    assert lifecycle.latest_status(successor.id).status == "proposed"
    assert lifecycle.supersede_claim(
        successor_claim_id=successor.id,
        predecessor_claim_id=predecessor.id,
        reason=reason,
        actor=HUMAN,
    ).id == link.id


@pytest.mark.parametrize(
    "reason",
    ["updated", "preference_changed"],
)
def test_dated_supersession_reasons_require_successor_valid_from(
    store: TruthStore,
    lifecycle: TruthLifecycle,
    reason: str,
):
    predecessor = _claim(store, f"Dated predecessor {reason}")
    successor = _claim(store, f"Undated successor {reason}")
    _confirm(lifecycle, predecessor)
    with pytest.raises(TransitionError, match="valid_from"):
        lifecycle.supersede_claim(
            successor_claim_id=successor.id,
            predecessor_claim_id=predecessor.id,
            reason=reason,
            actor=HUMAN,
        )


def test_valid_time_closed_requires_successor_valid_to(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    predecessor = _claim(store, "Open interval predecessor")
    successor = _claim(
        store,
        "Still missing interval close",
        valid_from="2026-07-15",
    )
    _confirm(lifecycle, predecessor)
    with pytest.raises(TransitionError, match="valid_to"):
        lifecycle.supersede_claim(
            successor_claim_id=successor.id,
            predecessor_claim_id=predecessor.id,
            reason="valid_time_closed",
            actor=HUMAN,
        )


def test_successor_confirmation_atomically_supersedes_predecessor(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    predecessor = _claim(store, "ElectricRAG version 120")
    successor = _claim(
        store,
        "ElectricRAG version 144",
        valid_from="2026-07-14",
    )
    _confirm(lifecycle, predecessor)
    link = lifecycle.supersede_claim(
        successor_claim_id=successor.id,
        predecessor_claim_id=predecessor.id,
        reason="updated",
        actor=HUMAN,
    )
    assert lifecycle.latest_status(predecessor.id).status == "confirmed"
    result, _ = _confirm(lifecycle, successor)
    assert result.event is not None and result.event.status == "confirmed"
    assert len(result.superseded_events) == 1
    assert result.superseded_events[0].basis_ref == link.id
    assert lifecycle.latest_status(predecessor.id).status == "superseded"


def test_challenged_predecessor_can_be_resolved_by_supersession(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    predecessor = _claim(store, "Original disputed wording")
    challenger = _claim(store, "Dispute with receipt")
    _support(store, challenger, content="dispute receipt")
    _confirm(lifecycle, predecessor)
    lifecycle.challenge_claim(
        claim_id=predecessor.id,
        challenging_claim_id=challenger.id,
        actor=HUMAN,
    )
    successor = _claim(store, "Corrected wording")
    lifecycle.supersede_claim(
        successor_claim_id=successor.id,
        predecessor_claim_id=predecessor.id,
        reason="corrected",
        actor=HUMAN,
    )
    _confirm(lifecycle, successor)
    assert lifecycle.latest_status(predecessor.id).status == "superseded"


def test_single_confirmed_successor_race_lands_competitor_in_needs_review(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    predecessor = _claim(store, "One current deployment")
    _confirm(lifecycle, predecessor)
    successors = [
        _claim(store, "Deployment candidate A", valid_from="2026-07-15"),
        _claim(store, "Deployment candidate B", valid_from="2026-07-15"),
    ]
    gestures = []
    for successor in successors:
        lifecycle.supersede_claim(
            successor_claim_id=successor.id,
            predecessor_claim_id=predecessor.id,
            reason="updated",
            actor=HUMAN,
        )
        gestures.append(_gesture(lifecycle, successor))

    def confirm(index: int):
        return lifecycle.confirm_claim(
            claim_id=successors[index].id,
            gesture_id=gestures[index].id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, range(2)))
    assert sum(result.event is not None for result in results) == 1
    assert sum(result.needs_review_event is not None for result in results) == 1
    statuses = {lifecycle.latest_status(item.id).status for item in successors}
    assert statuses == {"confirmed", "needs_review"}
    assert lifecycle.latest_status(predecessor.id).status == "superseded"


def test_plain_rejection_is_gestured_and_does_not_redact_content(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    source = _claim(store, "Reject without replacement")
    gesture = _gesture(lifecycle, source, kind="reject_plain")
    result = lifecycle.reject_claim(
        source_claim_id=source.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        expected_context_sha256=None,
        observed_at=LATER,
    )
    assert result.source_event.status == "rejected"
    assert result.result_claim is None
    assert store.get_claim(source.id).redacted_at is None
    with pytest.raises(TransitionError, match="cannot reject"):
        lifecycle.reject_claim(
            source_claim_id=source.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            reason_class="reject_plain",
            expected_context_sha256=None,
        )


def test_reject_as_false_confirms_preallocated_negative_and_refutes_source(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    source = _claim(store, "The deployment passed")
    negative = _claim(store, "The deployment did not pass")
    receipts = {"evidence": ["ci-failure"], "source_hash": source.canonical_sha256}
    context = lifecycle.rejection_context_sha256(source.id, receipts)
    gesture = _gesture(
        lifecycle,
        negative,
        kind="reject_as_false",
        context=context,
    )
    result = lifecycle.reject_claim(
        source_claim_id=source.id,
        result_claim_id=negative.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_as_false",
        expected_context_sha256=context,
        displayed_receipts=receipts,
        observed_at=LATER,
    )
    assert result.source_event.status == "rejected"
    assert result.result_event is not None and result.result_event.status == "confirmed"
    assert result.source_event.basis_ref == result.result_event.basis_ref == gesture.id
    assert result.refutes_link is not None
    assert result.refutes_link.from_claim_id == negative.id
    assert result.refutes_link.to_ref == source.id
    assert result.gesture.consumed_at == LATER


def test_reject_as_preference_uses_safe_result_bound_gesture(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    source = _claim(store, "I prefer weekly status emails", kind="preference")
    replacement = _claim(store, "I prefer no status emails", kind="preference")
    receipts = {"dialog": "preference correction", "evidence": [source.id]}
    context = lifecycle.rejection_context_sha256(source.id, receipts)
    gesture = _gesture(
        lifecycle,
        replacement,
        kind="reject_as_preference",
        context=context,
    )
    result = lifecycle.reject_claim(
        source_claim_id=source.id,
        result_claim_id=replacement.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_as_preference",
        expected_context_sha256=context,
        displayed_receipts=receipts,
        observed_at=LATER,
    )
    assert result.source_event.status == "rejected"
    assert result.result_event is not None and result.result_event.status == "confirmed"
    assert result.result_claim == replacement
    assert result.refutes_link is None


def test_reasoned_rejection_refuses_stale_receipts_and_wrong_result_kind(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    source = _claim(store, "Original preference", kind="preference")
    wrong = _claim(store, "Not typed as preference")
    receipts = {"receipt": "shown"}
    context = lifecycle.rejection_context_sha256(source.id, receipts)
    gesture = _gesture(
        lifecycle,
        wrong,
        kind="reject_as_preference",
        context=context,
    )
    with pytest.raises(TransitionError, match="preference result"):
        lifecycle.reject_claim(
            source_claim_id=source.id,
            result_claim_id=wrong.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            reason_class="reject_as_preference",
            expected_context_sha256=context,
            displayed_receipts=receipts,
        )
    assert lifecycle.latest_status(source.id).status == "proposed"

    replacement = _claim(store, "Replacement preference", kind="preference")
    replacement_gesture = _gesture(
        lifecycle,
        replacement,
        kind="reject_as_preference",
        context=context,
    )
    with pytest.raises(GestureError, match="source and displayed receipts"):
        lifecycle.reject_claim(
            source_claim_id=source.id,
            result_claim_id=replacement.id,
            gesture_id=replacement_gesture.id,
            actor=HUMAN,
            reason_class="reject_as_preference",
            expected_context_sha256=context,
            displayed_receipts={"receipt": "changed"},
        )
    assert lifecycle.latest_status(source.id).status == "proposed"
    assert lifecycle.latest_status(replacement.id).status == "proposed"


def test_reasoned_rejection_rolls_back_source_link_gesture_and_result_on_failure(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    source = _claim(store, "Rollback source")
    negative = _claim(store, "Rollback negative")
    receipts = {"receipt": "atomic"}
    context = lifecycle.rejection_context_sha256(source.id, receipts)
    gesture = _gesture(
        lifecycle,
        negative,
        kind="reject_as_false",
        context=context,
    )
    duplicate_event_id = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        lifecycle.reject_claim(
            source_claim_id=source.id,
            result_claim_id=negative.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            reason_class="reject_as_false",
            expected_context_sha256=context,
            displayed_receipts=receipts,
            source_event_id=duplicate_event_id,
            result_event_id=duplicate_event_id,
            observed_at=LATER,
        )
    assert lifecycle.latest_status(source.id).status == "proposed"
    assert lifecycle.latest_status(negative.id).status == "proposed"
    assert lifecycle.verify_gesture(
        gesture.id,
        actor=HUMAN,
        subject_ref=negative.id,
        payload_sha256=negative.canonical_sha256,
        expected_context_sha256=context,
        allowed_kinds={"reject_as_false"},
        observed_at=LATER,
    ).consumed_at is None
    conn = store.connect()
    try:
        links = conn.execute(
            "SELECT COUNT(*) FROM claim_links WHERE from_claim_id = ? "
            "AND link_type = 'refutes' AND to_ref = ?",
            (negative.id, source.id),
        ).fetchone()[0]
    finally:
        conn.close()
    assert links == 0


def test_proposal_expiry_uses_proposed_event_time_and_is_idempotent(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Fold confirmation expires")
    with pytest.raises(TransitionError, match="not reached"):
        lifecycle.expire_claim(
            claim_id=claim.id,
            actor=SYSTEM,
            observed_at="2026-07-14T11:59:59.999+00:00",
        )
    with pytest.raises(TransitionError, match="system rule"):
        lifecycle.expire_claim(
            claim_id=claim.id,
            actor=HUMAN,
            observed_at=TWO_HOURS,
        )
    expired = lifecycle.expire_claim(
        claim_id=claim.id,
        actor=SYSTEM,
        observed_at=TWO_HOURS,
    )
    assert expired.event.status == "expired"
    assert expired.event.at == TWO_HOURS
    assert expired.event.basis_ref == "proposal_max_age:7200"
    duplicate = lifecycle.expire_claim(
        claim_id=claim.id,
        actor=SYSTEM,
        observed_at="2026-07-15T12:00:00.000+00:00",
    )
    assert duplicate.created is False
    with pytest.raises(TransitionError, match="cannot confirm"):
        _confirm(lifecycle, claim)


def test_session_end_expiry_is_an_explicit_system_only_rule(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Unconfirmed co-think micro proposal")
    expired = lifecycle.expire_claim(
        claim_id=claim.id,
        actor=SYSTEM,
        observed_at=LATER,
        rule="session_end",
    )
    assert expired.event.status == "expired"
    assert expired.event.basis_kind == "rule"
    assert expired.event.basis_ref == "session_end"

    other = _claim(store, "Unsupported expiry rule")
    with pytest.raises(TransitionError, match="unsupported expiry rule"):
        lifecycle.expire_claim(
            claim_id=other.id,
            actor=SYSTEM,
            observed_at=LATER,
            rule="disconnect_guess",
        )


def test_transition_timestamp_cannot_predate_claim_creation(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Time order is durable")
    with pytest.raises(TransitionError, match="predate"):
        lifecycle.transition_claim(
            claim_id=claim.id,
            status="retracted",
            actor=SYSTEM,
            basis_kind="policy",
            at="2026-07-14T09:59:59.999+00:00",
        )
    assert lifecycle.latest_status(claim.id).status == "proposed"


def test_confirmation_inside_caller_transaction_rolls_back_status_and_consumption(
    store: TruthStore,
    lifecycle: TruthLifecycle,
):
    claim = _claim(store, "Caller owns atomicity")
    gesture = _gesture(lifecycle, claim)
    conn = store.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = lifecycle.confirm_claim(
            claim_id=claim.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            expected_context_sha256=None,
            observed_at=LATER,
            conn=conn,
        )
        assert result.event is not None and result.event.status == "confirmed"
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert lifecycle.latest_status(claim.id).status == "proposed"
    assert lifecycle.verify_gesture(
        gesture.id,
        actor=HUMAN,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        expected_context_sha256=None,
        allowed_kinds={"confirm"},
        observed_at=LATER,
    ).consumed_at is None


def test_supplied_connections_fail_closed_across_stores(
    store: TruthStore,
    lifecycle: TruthLifecycle,
    tmp_path: Path,
):
    claim = _claim(store, "Connection stays in its store")
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign = TruthStore.create(foreign_root, _profile())
    conn = foreign.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(InvariantViolation, match="different truth store"):
            lifecycle.transition_claim(
                claim_id=claim.id,
                status="retracted",
                actor=SYSTEM,
                basis_kind="policy",
                conn=conn,
            )
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert lifecycle.latest_status(claim.id).status == "proposed"
