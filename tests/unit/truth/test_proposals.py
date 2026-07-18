"""Invariant tests for the co-work proposal ledger and its decision paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.truth import documents, proposals
from work_buddy.truth.contracts import (
    Actor,
    GestureError,
    TransitionError,
)
from work_buddy.truth.identity import sha256_text


NOW = "2026-07-17T12:00:00.000+00:00"
LATER = "2026-07-17T12:05:00.000+00:00"
LATEST = "2026-07-17T12:10:00.000+00:00"
HUMAN = Actor("human", "reviewer-kaden")
SYSTEM = Actor("system", "truth-cowork-test")
AGENT = Actor(
    "agent_run",
    "cowork-agent-run",
    {
        "model": "test-model",
        "harness": "pytest",
        "surface": "cowork",
        "session_id": "session-1",
        "call_id": "call-1",
    },
)

_SELECTOR = [
    {
        "type": "TextQuoteSelector",
        "exact": "120 queries",
        "prefix": "is ",
        "suffix": ".",
    }
]


def _claim(store, proposition="Evaluation size is 120 queries", kind="fact"):
    return store.propose_claim(
        proposition=proposition,
        claim_kind=kind,
        actor=AGENT,
        created_at=NOW,
        status_at=NOW,
    ).claim


def _propose(
    store,
    document_id,
    base,
    *,
    quote="120 queries",
    replacement="144 queries",
    rationale=None,
    claim_refs=None,
    expires_at=None,
):
    return proposals.propose_edit(
        store,
        document_id=document_id,
        base_content_sha256=base,
        selector=_SELECTOR,
        quote_exact=quote,
        replacement=replacement,
        rationale=rationale,
        claim_refs=claim_refs,
        actor=AGENT,
        expires_at=expires_at,
        at=NOW,
    )


def test_propose_opens_with_initial_status(document_store, register_document):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    latest = proposals.latest_proposal_status(store, proposal.id)
    assert latest.status == "open"
    assert latest.decision is None
    assert proposal.replacement == "144 queries"


def test_distinct_proposals_have_distinct_canonical_hashes(
    document_store, register_document
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    a = _propose(store, document_id, base, quote="120 queries", replacement="144")
    b = _propose(store, document_id, base, quote="120 queries", replacement="200")
    assert a.canonical_sha256 != b.canonical_sha256


def test_dedup_suppresses_live_duplicate(document_store, register_document):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    first = _propose(store, document_id, base)
    duplicate = _propose(store, document_id, base)
    assert duplicate.id == first.id


def test_dedup_allows_repropose_after_expiry(document_store, register_document):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    first = _propose(store, document_id, base)
    proposals.expire_proposal(
        store, proposal_id=first.id, basis_kind="rule", actor=SYSTEM, at=LATER
    )
    reproposed = _propose(store, document_id, base)
    assert reproposed.id != first.id


def test_accept_confirm_applies_and_mints_expression(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    claim = _claim(store)
    proposal = _propose(
        store,
        document_id,
        base,
        claim_refs=[{"claim": claim.id, "role": "paraphrase"}],
    )
    gesture = mint_proposal_gesture(store, proposal, kind="confirm")
    result = proposals.accept_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        observed_at=LATER,
        at=LATER,
    )
    assert result.status_event.status == "applied"
    assert result.status_event.decision == "confirm"
    assert len(result.expressions) == 1
    assert result.expressions[0].role == "paraphrase"


def test_edit_confirm_applies_without_minting(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    claim = _claim(store)
    proposal = _propose(
        store, document_id, base, claim_refs=[{"claim": claim.id, "role": "instantiation"}]
    )
    gesture = mint_proposal_gesture(store, proposal, kind="edit_confirm")
    result = proposals.accept_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        amended_replacement="the human's amended phrasing",
        observed_at=LATER,
        at=LATER,
    )
    assert result.status_event.status == "applied"
    assert result.status_event.decision == "edit_confirm"
    assert result.expressions == ()


def test_edit_confirm_requires_amended_replacement(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    gesture = mint_proposal_gesture(store, proposal, kind="edit_confirm")
    with pytest.raises(TransitionError):
        proposals.accept_proposal(
            store,
            proposal_id=proposal.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            observed_at=LATER,
        )


def test_stale_base_proposal_cannot_be_applied(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    # The document advances past the base the proposal was composed against.
    documents.record_materialization(
        store,
        document_id=document_id,
        content_sha256=sha256_text("advanced-content"),
        actor=HUMAN,
        at=LATER,
    )
    gesture = mint_proposal_gesture(store, proposal, kind="confirm")
    with pytest.raises(TransitionError):
        proposals.accept_proposal(
            store,
            proposal_id=proposal.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            observed_at=LATEST,
        )


def test_reject_plain_closes_and_redacts(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    gesture = mint_proposal_gesture(store, proposal, kind="reject_plain")
    proposals.reject_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        observed_at=LATER,
        at=LATER,
    )
    redacted = proposals.get_proposal(store, proposal.id)
    assert proposals.latest_proposal_status(store, proposal.id).status == "closed"
    # Anti-anchoring: content nulls out, ids and hashes and dedup_key survive.
    assert redacted.redacted_at is not None
    assert redacted.quote_exact is None and redacted.replacement is None
    assert redacted.canonical_sha256 == proposal.canonical_sha256
    assert redacted.dedup_key == proposal.dedup_key


def test_reject_as_preference_records_result(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    preferred = _claim(store, proposition="Prefer 'evaluation set'", kind="preference")
    gesture = mint_proposal_gesture(store, proposal, kind="reject_as_preference")
    result = proposals.reject_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_as_preference",
        result_claim_id=preferred.id,
        observed_at=LATER,
        at=LATER,
    )
    assert result.result_claim_id == preferred.id
    assert preferred.id in (result.status_event.note or "")


def test_reject_as_false_from_claim_refs_mints_confirmed_negation(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    from work_buddy.truth.lifecycle import TruthLifecycle, negated_proposition

    document_id, base, _ = register_document(store)
    claim = _claim(store)
    proposal = _propose(
        store, document_id, base, claim_refs=[{"claim": claim.id, "role": "instantiation"}]
    )
    gesture = mint_proposal_gesture(store, proposal, kind="reject_as_false")
    result = proposals.decide_reject_as_false(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        observed_at=LATER,
        at=LATER,
    )
    assert result.status_event.status == "closed"
    assert result.status_event.decision == "reject_as_false"
    assert result.negation_claim.proposition == negated_proposition(claim.proposition)
    assert result.refutes_link is not None
    # The negation is confirmed under the one proposal-bound gesture.
    assert (
        TruthLifecycle(store).latest_status(result.negation_claim.id).status
        == "confirmed"
    )
    assert proposals.get_proposal(store, proposal.id).redacted_at is not None


def test_reject_as_false_from_negation_text_is_verbatim(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)  # no claim_refs
    gesture = mint_proposal_gesture(store, proposal, kind="reject_as_false")
    result = proposals.decide_reject_as_false(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        negation_text="The evaluation size is not 144 queries",
        observed_at=LATER,
        at=LATER,
    )
    assert result.negation_claim.proposition == "The evaluation size is not 144 queries"
    # No claim_refs means nothing to refute.
    assert result.refutes_link is None


def test_reject_as_false_hard_fails_without_basis(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)  # no claim_refs
    gesture = mint_proposal_gesture(store, proposal, kind="reject_as_false")
    with pytest.raises(TransitionError):
        proposals.decide_reject_as_false(
            store,
            proposal_id=proposal.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            observed_at=LATER,
        )
    # The hard-fail leaves the proposal open (no silent downgrade).
    assert proposals.latest_proposal_status(store, proposal.id).status == "open"


def test_redirect_and_defer_keep_open(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    redirected = _propose(store, document_id, base, quote="120 queries", replacement="A")
    deferred = _propose(store, document_id, base, quote="120 queries", replacement="B")
    g1 = mint_proposal_gesture(store, redirected, kind="redirect")
    proposals.redirect_proposal(
        store,
        proposal_id=redirected.id,
        gesture_id=g1.id,
        actor=HUMAN,
        note="cite the 2024 paper",
        observed_at=LATER,
    )
    g2 = mint_proposal_gesture(store, deferred, kind="defer")
    proposals.defer_proposal(
        store, proposal_id=deferred.id, gesture_id=g2.id, actor=HUMAN, observed_at=LATER
    )
    r = proposals.latest_proposal_status(store, redirected.id)
    d = proposals.latest_proposal_status(store, deferred.id)
    assert r.status == "open" and r.decision == "redirect" and r.note == "cite the 2024 paper"
    assert d.status == "open" and d.decision == "defer"


def test_endorse_and_dismiss_require_flags(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    flag = proposals.propose_edit(
        store,
        document_id=document_id,
        base_content_sha256=base,
        selector=_SELECTOR,
        quote_exact="120 queries",
        replacement=None,
        rationale="unsourced number",
        actor=AGENT,
        at=NOW,
    )
    endorse_gesture = mint_proposal_gesture(store, flag, kind="endorse")
    proposals.endorse_flag(
        store,
        proposal_id=flag.id,
        gesture_id=endorse_gesture.id,
        actor=HUMAN,
        observed_at=LATER,
    )
    assert proposals.latest_proposal_status(store, flag.id).status == "open"

    edit = _propose(store, document_id, base)
    bad_gesture = mint_proposal_gesture(store, edit, kind="endorse")
    with pytest.raises(TransitionError):
        proposals.endorse_flag(
            store,
            proposal_id=edit.id,
            gesture_id=bad_gesture.id,
            actor=HUMAN,
            observed_at=LATER,
        )


def test_dismiss_flag_closes_and_redacts(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    flag = proposals.propose_edit(
        store,
        document_id=document_id,
        base_content_sha256=base,
        selector=_SELECTOR,
        quote_exact="120 queries",
        replacement=None,
        rationale="unsourced number",
        actor=AGENT,
        at=NOW,
    )
    gesture = mint_proposal_gesture(store, flag, kind="reject_plain")
    result = proposals.dismiss_flag(
        store,
        proposal_id=flag.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        observed_at=LATER,
    )
    assert result.status_event.status == "closed"
    assert result.status_event.decision == "dismiss"
    assert proposals.get_proposal(store, flag.id).redacted_at is not None


def test_terminal_proposal_rejects_further_decisions(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    gesture = mint_proposal_gesture(store, proposal, kind="reject_plain")
    proposals.reject_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        observed_at=LATER,
    )
    # Proposal terminal statuses are applied|closed|expired, distinct from the
    # claim TERMINAL_STATUSES set.
    assert proposals.latest_proposal_status(store, proposal.id).status == "closed"
    again = mint_proposal_gesture(store, proposal, kind="defer", gesture_id=None)
    with pytest.raises(TransitionError):
        proposals.defer_proposal(
            store,
            proposal_id=proposal.id,
            gesture_id=again.id,
            actor=HUMAN,
            observed_at=LATEST,
        )


def test_agent_cannot_decide(document_store, register_document, mint_proposal_gesture):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    gesture = mint_proposal_gesture(store, proposal, kind="confirm")
    with pytest.raises(GestureError):
        proposals.accept_proposal(
            store,
            proposal_id=proposal.id,
            gesture_id=gesture.id,
            actor=AGENT,
            observed_at=LATER,
        )


def test_gesture_with_wrong_hash_is_rejected(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    other = _propose(store, document_id, base, quote="120 queries", replacement="99")
    # A gesture minted against a different proposal's hash must not authorize this one.
    wrong = mint_proposal_gesture(store, other, kind="confirm")
    with pytest.raises(GestureError):
        store_proposal = proposals.get_proposal(store, proposal.id)
        proposals.accept_proposal(
            store,
            proposal_id=store_proposal.id,
            gesture_id=wrong.id,
            actor=HUMAN,
            observed_at=LATER,
        )


def test_gesture_is_single_use(
    document_store, register_document, mint_proposal_gesture
):
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(store, document_id, base)
    gesture = mint_proposal_gesture(store, proposal, kind="redirect")
    proposals.redirect_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        note="first pass",
        observed_at=LATER,
    )
    # The proposal stays open, but the consumed gesture cannot be reused.
    with pytest.raises(GestureError):
        proposals.redirect_proposal(
            store,
            proposal_id=proposal.id,
            gesture_id=gesture.id,
            actor=HUMAN,
            note="second pass",
            observed_at=LATEST,
        )


def test_cowork_doc_workload_walkthrough(document_store):
    """Walk every decision verb end to end via the frozen document workload."""
    from .fixture_runner import CoworkWorkloadRunner, load_cowork_workload

    store, _ = document_store
    fixture_path = (
        Path(__file__).parents[2]
        / "fixtures"
        / "truth"
        / "cowork"
        / "cowork_doc_workload.yaml"
    )
    workload = load_cowork_workload(fixture_path)
    result = CoworkWorkloadRunner(store, workload).run()

    # Nine decided marked items (p1..p7 plus the two flags), each one gesture.
    assert len(result.gesture_hashes) == 9
    assert result.assertions["one_gesture_per_marked_item_with_distinct_hashes"] == (
        "passed"
    )
    assert result.assertions["agent_self_decide_rejected"] == "passed"
    assert result.assertions["stale_view_mark_rejected"] == "passed"
    # The full export v3 round trip (including the ydoc snapshot blob) runs live.
    assert result.assertions[
        "export_v3_round_trips_lossless_including_ydoc_blob"
    ] == "passed"


def test_import_accepts_accepted_rejected_and_reject_as_false_history(
    tmp_path, document_store, register_document, mint_proposal_gesture
):
    """C1 regression: proposal-decision gestures survive an export round trip.

    An accepted proposal, a rejected-and-redacted proposal, and a reject_as_false
    proposal (whose one proposal-bound gesture also confirms the minted negation)
    each leave gestures whose subject is a proposal. The proposal-aware integrity
    gate must accept the store, so integrity_findings is error-clean and
    import_store re-imports the bundle byte for byte.
    """
    from work_buddy.truth.export import export_store, import_store
    from work_buddy.truth.queries import integrity_findings

    from .fixture_runner import EmptyRegistry

    store, _ = document_store
    document_id, base, _ = register_document(store)

    accepted = _propose(
        store, document_id, base, quote="120 queries", replacement="144 queries"
    )
    g_accept = mint_proposal_gesture(store, accepted, kind="confirm")
    proposals.accept_proposal(
        store,
        proposal_id=accepted.id,
        gesture_id=g_accept.id,
        actor=HUMAN,
        observed_at=LATER,
        at=LATER,
    )

    rejected = _propose(
        store,
        document_id,
        base,
        quote="baseline model",
        replacement="reference baseline model",
    )
    g_reject = mint_proposal_gesture(store, rejected, kind="reject_plain")
    proposals.reject_proposal(
        store,
        proposal_id=rejected.id,
        gesture_id=g_reject.id,
        actor=HUMAN,
        reason_class="reject_plain",
        observed_at=LATER,
        at=LATER,
    )

    claim = _claim(store)
    false_prop = _propose(
        store,
        document_id,
        base,
        quote="public sources",
        replacement="licensed public sources",
        claim_refs=[{"claim": claim.id, "role": "instantiation"}],
    )
    g_false = mint_proposal_gesture(store, false_prop, kind="reject_as_false")
    proposals.decide_reject_as_false(
        store,
        proposal_id=false_prop.id,
        gesture_id=g_false.id,
        actor=HUMAN,
        observed_at=LATER,
        at=LATER,
    )

    errors = [f for f in integrity_findings(store) if f.severity == "error"]
    assert errors == [], f"decision-gesture store is not clean: {errors!r}"

    exported = export_store(store)
    target = tmp_path / "import-target"
    target.mkdir()
    restored = import_store(exported.path, target, registry=EmptyRegistry()).store
    reexport = export_store(restored, tmp_path / "reexport.jsonl")
    assert reexport.path.read_bytes() == exported.path.read_bytes()


def test_redacting_decision_scrubs_the_gesture_excerpt(
    document_store, register_document, mint_proposal_gesture
):
    """C2 regression: a redacting decision tombstones the consumed gesture receipt.

    Under gate.rejected_content == redact the proposal content nulls out, and the
    consumed gesture's readable payload_excerpt must be scrubbed to the same
    '[redacted]' sentinel in the same transaction so a rejected quote and
    replacement never survive in a receipt (I10 anti-anchoring).
    """
    store, _ = document_store
    document_id, base, _ = register_document(store)
    proposal = _propose(
        store,
        document_id,
        base,
        quote="Original sentence",
        replacement="DEFAMATORY-REPLACEMENT-TEXT",
    )
    gesture = mint_proposal_gesture(store, proposal, kind="reject_plain")
    # Before the decision the receipt carries the quote-and-replacement verbatim.
    assert "DEFAMATORY-REPLACEMENT-TEXT" in gesture.payload_excerpt
    assert "Original sentence" in gesture.payload_excerpt

    proposals.reject_proposal(
        store,
        proposal_id=proposal.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        observed_at=LATER,
        at=LATER,
    )

    conn = store.connect()
    try:
        excerpt = conn.execute(
            "SELECT payload_excerpt FROM gestures WHERE id = ?", (gesture.id,)
        ).fetchone()["payload_excerpt"]
    finally:
        conn.close()
    assert excerpt == "[redacted]"
    assert "DEFAMATORY-REPLACEMENT-TEXT" not in excerpt
    assert "Original sentence" not in excerpt


def test_canonical_and_dedup_helpers_are_pure():
    args = dict(
        document_id="d" * 32,
        base_content_sha256=sha256_text("base"),
        selector=_SELECTOR,
        quote_exact="120 queries",
        replacement="144 queries",
        rationale=None,
        tldr=None,
        claim_refs=[{"claim": "c" * 32, "role": "instantiation"}],
    )
    assert proposals.proposal_canonical_sha256(
        **args
    ) == proposals.proposal_canonical_sha256(**args)
    key = proposals.proposal_dedup_key(
        document_id="d" * 32, quote_exact="120  queries", replacement="144 queries"
    )
    assert key == proposals.proposal_dedup_key(
        document_id="d" * 32, quote_exact="120 queries", replacement="144 queries"
    )
