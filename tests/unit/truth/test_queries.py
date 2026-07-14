"""Ledger-derived query, sweep, and integrity behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.fingerprints import FingerprintStatus
from work_buddy.truth.identity import (
    canonical_json,
    new_id,
    sha256_text,
    truth_uri,
)
from work_buddy.truth.lifecycle import TruthLifecycle, negated_proposition
from work_buddy.truth.queries import (
    PremiseResolution,
    SweepFindingSpec,
    claims_as_of,
    conflicts,
    current_claims,
    integrity_findings,
    link_fingerprint_states,
    needs_review,
    rebuild_claims_current,
    record_sweep,
    resolve_claim_states,
    source_integrity_states,
    source_sweep_candidates,
    successor_races,
    supersession_sweep_candidates,
)
from work_buddy.truth.redact import TruthRedactor, policy_basis_ref
from work_buddy.truth.store import (
    ClaimLinkRecord,
    ClaimRecord,
    GestureRecord,
    TruthStore,
)


T0 = "2026-01-01T00:00:00.000+00:00"
T1 = "2026-01-01T01:00:00.000+00:00"
T2 = "2026-01-01T02:00:00.000+00:00"
T3 = "2026-01-01T03:00:00.000+00:00"
HUMAN = Actor("human", "user-1")
SYSTEM = Actor("system", "truth-query-test")


def _profile() -> dict[str, object]:
    return {
        "store_id": new_id(),
        "profile": "test",
        "title": "Query test store",
        "allowed_claim_kinds": ["fact", "preference"],
        "required_fields": {},
        "gate": {
            "rejected_content": "retain",
            "confirmation_surfaces": ["dashboard", "cli"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
    }


@pytest.fixture
def store(truth_root: Path) -> TruthStore:
    return TruthStore.create(truth_root, _profile())


def _claim(
    store: TruthStore,
    proposition: str,
    *,
    claim_kind: str = "fact",
    created_at: str = T0,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> ClaimRecord:
    return store.propose_claim(
        proposition=proposition,
        claim_kind=claim_kind,
        actor=HUMAN,
        created_at=created_at,
        status_at=created_at,
        valid_from=valid_from,
        valid_to=valid_to,
    ).claim


def _confirm(
    store: TruthStore,
    claim: ClaimRecord,
    *,
    at: str = T1,
) -> None:
    gesture_id = new_id()
    gesture = GestureRecord(
        id=gesture_id,
        at=at,
        surface="dashboard",
        actor_ref="user-1",
        kind="confirm",
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        payload_excerpt=claim.proposition,
        context_sha256=None,
        expires_at=None,
        consumed_at=at,
    )
    with store.write_transaction() as conn:
        store._insert_gesture_locked(conn, gesture)
        store._insert_status_event_locked(
            conn,
            claim_id=claim.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture_id,
            at=at,
        )


def _overlay(
    store: TruthStore,
    claim: ClaimRecord,
    *,
    at: str = T1,
    basis_kind: str = "sweep",
) -> None:
    with store.write_transaction() as conn:
        store._insert_status_event_locked(
            conn,
            claim_id=claim.id,
            status="needs_review",
            actor=SYSTEM,
            basis_kind=basis_kind,
            basis_ref=new_id(),
            at=at,
        )


def _supersede(
    store: TruthStore,
    successor: ClaimRecord,
    predecessor: ClaimRecord,
    *,
    reason: str,
    at: str = T2,
) -> ClaimLinkRecord:
    return store.add_link(
        from_claim_id=successor.id,
        link_type="supersedes",
        to_kind="claim",
        to_ref=predecessor.id,
        actor=HUMAN,
        role={"supersession_reason": reason},
        created_at=at,
    )


def _reasoned_rejection(
    store: TruthStore,
    *,
    kind: str,
) -> tuple[ClaimRecord, ClaimRecord, GestureRecord]:
    preference = kind == "reject_as_preference"
    source = _claim(
        store,
        f"{kind} source",
        claim_kind="preference" if preference else "fact",
    )
    result = _claim(
        store,
        (
            negated_proposition(source.proposition)
            if kind == "reject_as_false"
            else f"{kind} result"
        ),
        claim_kind="preference" if preference else "fact",
    )
    gesture = GestureRecord(
        id=new_id(),
        at=T1,
        surface="dashboard",
        actor_ref="user-1",
        kind=kind,
        subject_ref=result.id,
        payload_sha256=result.canonical_sha256,
        payload_excerpt=result.proposition,
        context_sha256=sha256_text(f"bound context for {source.id}"),
        expires_at=None,
        consumed_at=T1,
    )
    with store.write_transaction() as conn:
        store._insert_gesture_locked(conn, gesture)
        store._insert_status_event_locked(
            conn,
            claim_id=source.id,
            status="rejected",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=kind,
            at=T1,
        )
        store._insert_status_event_locked(
            conn,
            claim_id=result.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture.id,
            note=kind,
            at=T1,
        )
    if kind == "reject_as_false":
        store.add_link(
            from_claim_id=result.id,
            link_type="refutes",
            to_kind="claim",
            to_ref=source.id,
            actor=HUMAN,
            created_at=T1,
        )
    return source, result, gesture


def _states(store: TruthStore) -> dict[str, object]:
    return {state.claim_id: state for state in resolve_claim_states(store)}


def _raw_status(
    store: TruthStore,
    claim: ClaimRecord,
    *,
    status: str,
    actor: Actor,
    basis_kind: str,
    basis_ref: str | None,
    at: str,
    note: str | None = None,
) -> str:
    event_id = new_id()
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store._insert_status_event_locked(
            conn,
            claim_id=claim.id,
            status=status,
            actor=actor,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            note=note,
            event_id=event_id,
            at=at,
        )
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
    return event_id


def _raw_link(
    store: TruthStore,
    successor: ClaimRecord,
    predecessor: ClaimRecord,
    *,
    reason: str,
    at: str = T1,
) -> ClaimLinkRecord:
    record = ClaimLinkRecord(
        id=new_id(),
        from_claim_id=successor.id,
        link_type="supersedes",
        to_kind="claim",
        to_ref=predecessor.id,
        role_json=canonical_json({"supersession_reason": reason}),
        target_fingerprint=None,
        fingerprint_reviewed_at=None,
        created_at=at,
        created_by_kind=HUMAN.kind,
        created_by_ref=HUMAN.ref,
    )
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store._insert_link_locked(conn, record)
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
    return record


def test_status_overlay_activates_at_boundary_and_human_gesture_clears_it(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Overlay boundary")
    _overlay(store, claim, at=T1)

    before = resolve_claim_states(store, belief_at=T0)[0]
    at_overlay = resolve_claim_states(store, belief_at=T1)[0]
    assert before.status == "proposed"
    assert before.needs_review is False
    assert at_overlay.status == "needs_review"
    assert at_overlay.base_status == "proposed"
    assert at_overlay.needs_review is True

    _confirm(store, claim, at=T2)
    historical = needs_review(store, belief_at=T1)
    assert [(item.subject_ref, item.base_status) for item in historical] == [
        (claim.id, "proposed")
    ]
    assert needs_review(store, belief_at=T2) == ()
    assert resolve_claim_states(store)[0].status == "confirmed"


def test_conflict_is_a_valid_needs_review_overlay_basis(store: TruthStore) -> None:
    claim = _claim(store, "Conflicted proposal")
    _overlay(store, claim, basis_kind="conflict")
    assert needs_review(store)[0].subject_ref == claim.id
    assert "invalid_review_overlay_basis" not in {
        item.code for item in integrity_findings(store)
    }


def test_as_of_and_valid_time_boundaries_use_ledger_not_projection(
    store: TruthStore,
) -> None:
    predecessor = _claim(store, "Old fact", valid_from="2020-01-01")
    successor = _claim(
        store,
        "New fact",
        created_at=T1,
        valid_from="2022-01-01",
    )
    _confirm(store, predecessor, at=T1)
    _confirm(store, successor, at=T2)
    _supersede(store, successor, predecessor, reason="updated", at=T3)

    before_link = claims_as_of(
        store,
        belief_at=T2,
        valid_at="2022-01-01",
    )
    assert {item.claim_id for item in before_link} == {
        predecessor.id,
        successor.id,
    }
    before_boundary = claims_as_of(
        store,
        belief_at=T3,
        valid_at="2021-12-31T23:59:59.999+00:00",
    )
    at_boundary = claims_as_of(
        store,
        belief_at=T3,
        valid_at="2022-01-01",
    )
    assert {item.claim_id for item in before_boundary} == {predecessor.id}
    assert {item.claim_id for item in at_boundary} == {successor.id}

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM claims_current").fetchone()[0] == 0


def test_as_of_preserves_pre_redaction_belief_as_a_tombstone(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Historically believed secret")
    _confirm(store, claim, at=T1)
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="redact",
        displayed_payload_sha256=claim.canonical_sha256,
        at=T3,
    )
    TruthRedactor(store, lifecycle=lifecycle).redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
        at=T3,
    )

    historical = claims_as_of(store, belief_at=T2)
    assert [item.claim_id for item in historical] == [claim.id]
    assert historical[0].claim.proposition == "[redacted]"
    assert historical[0].claim.canonical_sha256 == claim.canonical_sha256
    assert historical[0].health == "redacted"
    assert "content_redacted_after_belief" in (historical[0].health_reason or "")
    assert claims_as_of(store, belief_at=T3) == ()
    assert current_claims(store) == ()


def test_interval_derivation_respects_each_supersession_reason(
    store: TruthStore,
) -> None:
    corrected_old = _claim(store, "Wrong wording", valid_from="2020-01-01")
    corrected_new = _claim(
        store,
        "Correct wording",
        valid_from="2021-06-01",
    )
    closed_old = _claim(store, "Temporary fact", valid_from="2019-01-01")
    closed_new = _claim(store, "Temporary fact closed", valid_to="2023-01-01")
    refined_old = _claim(
        store,
        "Rough fact",
        valid_from="2018-01-01",
        valid_to="2024-01-01",
    )
    refined_new = _claim(store, "Precise fact")
    preference_old = _claim(
        store,
        "Old preference",
        claim_kind="preference",
        valid_from="2017-01-01",
    )
    preference_new = _claim(
        store,
        "New preference",
        claim_kind="preference",
        valid_from="2022-05-01",
    )
    for claim in (
        corrected_old,
        corrected_new,
        closed_old,
        closed_new,
        refined_old,
        refined_new,
        preference_old,
        preference_new,
    ):
        _confirm(store, claim)
    _supersede(store, corrected_new, corrected_old, reason="corrected")
    _supersede(store, closed_new, closed_old, reason="valid_time_closed")
    _supersede(store, refined_new, refined_old, reason="refined")
    _supersede(
        store,
        preference_new,
        preference_old,
        reason="preference_changed",
    )

    states = _states(store)
    assert states[corrected_old.id].voided is True
    assert states[corrected_old.id].effective_valid_to is None
    before_correction = claims_as_of(
        store,
        belief_at=T1,
        valid_at="2020-06-01",
    )
    after_correction = claims_as_of(
        store,
        belief_at=T2,
        valid_at="2020-06-01",
    )
    assert corrected_old.id in {item.claim_id for item in before_correction}
    assert corrected_old.id not in {item.claim_id for item in after_correction}
    assert states[closed_old.id].effective_valid_to == "2023-01-01"
    assert states[closed_new.id].effective_valid_from == "2019-01-01"
    assert states[refined_new.id].effective_valid_from == "2018-01-01"
    assert states[refined_new.id].effective_valid_to == "2024-01-01"
    assert states[preference_old.id].effective_valid_to == "2022-05-01"
    assert states[preference_new.id].effective_valid_from == "2022-05-01"


def test_projection_rebuild_is_deterministic_idempotent_and_ledger_neutral(
    store: TruthStore,
) -> None:
    clean = _claim(store, "Projected")
    review = _claim(store, "Projected review")
    _confirm(store, clean)
    _overlay(store, review)
    timestamp = "2026-01-02T00:00:00.000+00:00"
    with store.connect() as conn:
        ledger_before = conn.execute("SELECT COUNT(*) FROM ledger_records").fetchone()[
            0
        ]

    first = rebuild_claims_current(store, rebuilt_at=timestamp)
    with store.connect() as conn:
        rows_first = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM claims_current ORDER BY claim_id"
            ).fetchall()
        ]
    second = rebuild_claims_current(store, rebuilt_at=timestamp)
    with store.connect() as conn:
        rows_second = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM claims_current ORDER BY claim_id"
            ).fetchall()
        ]
        ledger_after = conn.execute("SELECT COUNT(*) FROM ledger_records").fetchone()[0]
    assert first == second
    assert rows_first == rows_second
    assert ledger_before == ledger_after
    projected = {row[0]: row for row in rows_second}
    assert projected[clean.id][1] == "confirmed"
    assert projected[review.id][1] == "needs_review"


def test_competing_confirmed_successors_are_reported_deterministically(
    store: TruthStore,
) -> None:
    predecessor = _claim(store, "One predecessor")
    first = _claim(store, "First successor")
    second = _claim(store, "Second successor")
    for claim in (predecessor, first, second):
        _confirm(store, claim)
    first_link = _supersede(store, first, predecessor, reason="updated")
    second_link = _supersede(store, second, predecessor, reason="updated", at=T3)

    assert successor_races(store) == (
        successor_races(store)[0].__class__(
            predecessor_id=predecessor.id,
            successor_ids=tuple(sorted((first.id, second.id))),
            link_ids=tuple(sorted((first_link.id, second_link.id))),
        ),
    )
    assert _states(store)[predecessor.id].health == "conflict"


def test_conflicts_honor_link_and_retraction_boundaries(store: TruthStore) -> None:
    left = _claim(store, "Left")
    right = _claim(store, "Right")
    _confirm(store, left)
    _confirm(store, right)
    link = store.add_link(
        from_claim_id=left.id,
        link_type="conflicts_with",
        to_kind="claim",
        to_ref=right.id,
        actor=HUMAN,
        role={"conflict_type": "undercut", "conflict_class": "scope"},
        created_at=T2,
    )
    assert conflicts(store, belief_at=T1) == ()
    active = conflicts(store, claim_id=right.id, belief_at=T2)
    assert [
        (item.link_id, item.conflict_type, item.conflict_class) for item in active
    ] == [(link.id, "undercut", "scope")]
    store.retract_link(link_id=link.id, actor=HUMAN, at=T3)
    assert len(conflicts(store, belief_at=T2)) == 1
    assert conflicts(store, belief_at=T3) == ()


def test_sweep_findings_join_review_queue_and_resolution_is_half_open(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Sweep me")
    sweep_id = new_id()
    recorded = record_sweep(
        store,
        kind="supersession",
        findings=(SweepFindingSpec("claim", claim.id, "dependent is stale"),),
        params={"root": claim.id},
        at=T2,
        sweep_id=sweep_id,
    )
    retried = record_sweep(
        store,
        kind="supersession",
        findings=(SweepFindingSpec("claim", claim.id, "dependent is stale"),),
        params={"root": claim.id},
        at=T2,
        sweep_id=sweep_id,
    )
    assert recorded == retried
    with pytest.raises(InvariantViolation, match="finding set"):
        record_sweep(
            store,
            kind="supersession",
            findings=(SweepFindingSpec("claim", claim.id, "different"),),
            params={"root": claim.id},
            at=T2,
            sweep_id=sweep_id,
        )
    assert needs_review(store, belief_at=T1) == ()
    assert needs_review(store, belief_at=T2)[0].findings == ("dependent is stale",)

    with store.write_transaction() as conn:
        conn.execute(
            "UPDATE sweep_findings SET resolved_at = ?, resolved_by_ref = ? "
            "WHERE id = ?",
            (T3, "user-1", recorded.finding_ids[0]),
        )
    assert len(needs_review(store, belief_at=T2)) == 1
    assert needs_review(store, belief_at=T3) == ()


def test_recursive_supersession_sweep_handles_same_store_uris_and_cycles(
    store: TruthStore,
) -> None:
    root = _claim(store, "Root")
    child = _claim(store, "Child")
    grandchild = _claim(store, "Grandchild")
    last = _claim(store, "Last")
    store.add_derivation(
        claim_id=child.id,
        method="deduction",
        premises=[root.id],
        actor=HUMAN,
    )
    store.add_derivation(
        claim_id=grandchild.id,
        method="deduction",
        premises=[truth_uri(store.store_id, "claim", child.id)],
        actor=HUMAN,
    )
    store.add_derivation(
        claim_id=last.id,
        method="deduction",
        premises=[grandchild.id],
        actor=HUMAN,
    )
    store.add_derivation(
        claim_id=root.id,
        method="cycle-fixture",
        premises=[last.id],
        actor=HUMAN,
    )

    candidates = supersession_sweep_candidates(store, root.id)
    assert [(item.subject_ref, item.depth) for item in candidates] == [
        (child.id, 1),
        (grandchild.id, 2),
        (last.id, 3),
    ]
    assert all(root.id not in item.path[1:] for item in candidates)


def test_source_sweep_follows_support_then_derivations_and_ignores_retractions(
    store: TruthStore,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///source.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="alpha beta",
        created_at=T0,
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="alpha"),
        actor=HUMAN,
        created_at=T1,
    )
    sourced = _claim(store, "Sourced")
    derived = _claim(store, "Derived")
    support = store.add_link(
        from_claim_id=sourced.id,
        link_type="supports_span",
        to_kind="evidence_span",
        to_ref=span.id,
        actor=HUMAN,
        created_at=T2,
    )
    store.add_derivation(
        claim_id=derived.id,
        method="deduction",
        premises=[sourced.id],
        actor=HUMAN,
    )
    assert [
        item.subject_ref
        for item in source_sweep_candidates(store, evidence_id=evidence.id)
    ] == [sourced.id, derived.id]
    store.retract_link(link_id=support.id, actor=HUMAN, at=T3)
    assert source_sweep_candidates(store, span_id=span.id) == ()


def test_source_integrity_distinguishes_snapshots_hash_only_and_corruption(
    store: TruthStore,
) -> None:
    file_evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///source.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="captured",
    )
    web_evidence = store.capture_evidence(
        kind="web",
        source_locator="https://example.com/page",
        actor=HUMAN,
        acquisition_method="fetch",
        content_sha256=sha256_text("remote-only"),
        meta={"retrieved_at": T0},
    )
    blob_evidence = store.capture_evidence(
        kind="artifact",
        source_locator="file:///artifact.bin",
        actor=HUMAN,
        acquisition_method="import",
        content=b"blob bytes",
    )
    assert blob_evidence.content_path is not None
    store.resolve_blob_path(blob_evidence.content_path).write_bytes(b"corrupt")

    states = {state.evidence_id: state for state in source_integrity_states(store)}
    assert states[file_evidence.id].state == "valid"
    assert states[file_evidence.id].verifiability_class == "A"
    assert states[web_evidence.id].state == "valid"
    assert states[web_evidence.id].snapshot_present is False
    assert states[web_evidence.id].verifiability_class == "D"
    assert states[blob_evidence.id].state == "corrupt_snapshot"


def test_fingerprint_states_cover_current_stale_unreviewed_and_immutable(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Fingerprints")
    reviewed = store.add_link(
        from_claim_id=claim.id,
        link_type="about_entity",
        to_kind="entity",
        to_ref="entity-1",
        actor=HUMAN,
        target_content="version one",
    )
    unreviewed = store.add_link(
        from_claim_id=claim.id,
        link_type="about_entity",
        to_kind="entity",
        to_ref="entity-2",
        actor=HUMAN,
    )
    immutable = store.add_link(
        from_claim_id=claim.id,
        link_type="relates_to",
        to_kind="entity",
        to_ref="entity-3",
        actor=HUMAN,
    )
    same = link_fingerprint_states(
        store,
        current_targets={reviewed.id: reviewed.target_fingerprint},
    )
    statuses = {item.link_id: item.status for item in same}
    assert statuses[reviewed.id] is FingerprintStatus.CURRENT
    assert statuses[unreviewed.id] is FingerprintStatus.UNREVIEWED
    assert statuses[immutable.id] is FingerprintStatus.NOT_APPLICABLE
    changed = link_fingerprint_states(
        store,
        current_targets={reviewed.id: sha256_text("version two")},
    )
    assert {item.link_id: item.status for item in changed}[
        reviewed.id
    ] is FingerprintStatus.STALE


def test_integrity_is_clean_for_valid_rows_and_reports_raw_corruption(
    store: TruthStore,
) -> None:
    valid = _claim(store, "Valid claim")
    _confirm(store, valid)
    assert integrity_findings(store) == ()

    broken = _claim(store, "Missing gesture")
    missing_gesture_id = new_id()
    raw_status = store.connect()
    try:
        raw_status.execute("BEGIN IMMEDIATE")
        store._insert_status_event_locked(
            raw_status,
            claim_id=broken.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=missing_gesture_id,
            at=T2,
        )
        raw_status.execute("COMMIT")
    finally:
        if raw_status.in_transaction:
            raw_status.execute("ROLLBACK")
        raw_status.close()

    dangling_target = new_id()
    raw = store.connect()
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("BEGIN IMMEDIATE")
        store._insert_link_locked(
            raw,
            ClaimLinkRecord(
                id=new_id(),
                from_claim_id=valid.id,
                link_type="conflicts_with",
                to_kind="claim",
                to_ref=dangling_target,
                role_json=None,
                target_fingerprint=None,
                fingerprint_reviewed_at=None,
                created_at=T3,
                created_by_kind=HUMAN.kind,
                created_by_ref=HUMAN.ref,
            ),
        )
        raw.execute("COMMIT")
        raw.execute("DROP TRIGGER claims_append_only_update")
        raw.execute(
            "UPDATE claims SET canonical_sha256 = ? WHERE id = ?",
            ("0" * 64, valid.id),
        )
    finally:
        if raw.in_transaction:
            raw.execute("ROLLBACK")
        raw.close()

    codes = {item.code for item in integrity_findings(store)}
    assert "claim_hash_mismatch" in codes
    assert "dangling_status_gesture" in codes
    assert "dangling_link_claim" in codes


def test_integrity_accepts_human_reaffirmation_that_clears_review_overlay(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Reaffirm after review")
    _confirm(store, claim, at=T1)
    lifecycle = TruthLifecycle(store)
    lifecycle.mark_needs_review(
        claim_id=claim.id,
        actor=SYSTEM,
        basis_kind="rule",
        basis_ref="freshness",
        at=T2,
    )
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="reaffirm",
        displayed_payload_sha256=claim.canonical_sha256,
        at=T3,
    )
    lifecycle.confirm_claim(
        claim_id=claim.id,
        gesture_id=gesture.id,
        actor=HUMAN,
        expected_context_sha256=None,
        observed_at=T3,
        at=T3,
    )

    assert integrity_findings(store) == ()


def test_integrity_enforces_status_actor_basis_gesture_and_surface_laws(
    store: TruthStore,
) -> None:
    review = _claim(store, "Review actor law")
    _raw_status(
        store,
        review,
        status="needs_review",
        actor=HUMAN,
        basis_kind="rule",
        basis_ref=None,
        at=T1,
    )

    expired = _claim(store, "Expiry actor law")
    _raw_status(
        store,
        expired,
        status="expired",
        actor=HUMAN,
        basis_kind="rule",
        basis_ref=None,
        at=T1,
    )

    retracted = _claim(store, "Retraction basis law")
    _raw_status(
        store,
        retracted,
        status="retracted",
        actor=SYSTEM,
        basis_kind="rule",
        basis_ref="cleanup",
        at=T1,
    )

    rejected = _claim(store, "Rejection gesture law")
    invalid_reject_gesture = GestureRecord(
        id=new_id(),
        at=T1,
        surface="unapproved-surface",
        actor_ref="user-1",
        kind="scope",
        subject_ref=rejected.id,
        payload_sha256=rejected.canonical_sha256,
        payload_excerpt=rejected.proposition,
        context_sha256=None,
        expires_at=None,
        consumed_at=T1,
    )
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store._insert_gesture_locked(conn, invalid_reject_gesture)
        store._insert_status_event_locked(
            conn,
            claim_id=rejected.id,
            status="rejected",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=invalid_reject_gesture.id,
            note="reject_plain",
            at=T1,
        )
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

    challenged = _claim(store, "Challenge target")
    challenger = _claim(store, "Challenge source")
    _confirm(store, challenged, at=T1)
    conflict_link = store.add_link(
        from_claim_id=challenger.id,
        link_type="conflicts_with",
        to_kind="claim",
        to_ref=challenged.id,
        actor=HUMAN,
        created_at=T2,
    )
    _raw_status(
        store,
        challenged,
        status="challenged",
        actor=SYSTEM,
        basis_kind="conflict_link",
        basis_ref=conflict_link.id,
        at=T2,
    )

    predecessor = _claim(store, "Supersession target")
    successor = _claim(store, "Supersession source")
    _confirm(store, predecessor, at=T1)
    supersedes = _raw_link(
        store,
        successor,
        predecessor,
        reason="refined",
        at=T1,
    )
    _confirm(store, successor, at=T2)
    _raw_status(
        store,
        predecessor,
        status="superseded",
        actor=Actor("agent_run", "run-1"),
        basis_kind="claim_link",
        basis_ref=supersedes.id,
        at=T2,
    )

    codes = {item.code for item in integrity_findings(store)}
    assert {
        "invalid_review_overlay_actor",
        "invalid_review_overlay_basis_ref",
        "invalid_expiry_basis",
        "invalid_expiry_basis_ref",
        "invalid_retraction_basis",
        "invalid_rejection_gesture_kind",
        "invalid_rejection_surface",
        "invalid_challenge_basis",
        "invalid_superseded_basis",
    } <= codes


def test_integrity_requires_bidirectional_atomic_supersession_pairs(
    store: TruthStore,
) -> None:
    missing_predecessor = _claim(store, "Missing superseded half")
    missing_successor = _claim(store, "Confirmed successor without pair")
    _confirm(store, missing_predecessor, at=T1)
    _raw_link(
        store,
        missing_successor,
        missing_predecessor,
        reason="refined",
        at=T1,
    )
    _confirm(store, missing_successor, at=T2)

    late_predecessor = _claim(store, "Late superseded half")
    late_successor = _claim(store, "Earlier successor confirmation")
    _confirm(store, late_predecessor, at=T1)
    late_link = _raw_link(
        store,
        late_successor,
        late_predecessor,
        reason="refined",
        at=T1,
    )
    _confirm(store, late_successor, at=T2)
    _raw_status(
        store,
        late_predecessor,
        status="superseded",
        actor=HUMAN,
        basis_kind="claim_link",
        basis_ref=late_link.id,
        at=T3,
    )

    orphan_predecessor = _claim(store, "Superseded without confirmation")
    orphan_successor = _claim(store, "Unconfirmed successor")
    _confirm(store, orphan_predecessor, at=T1)
    orphan_link = _raw_link(
        store,
        orphan_successor,
        orphan_predecessor,
        reason="refined",
        at=T1,
    )
    _raw_status(
        store,
        orphan_predecessor,
        status="superseded",
        actor=HUMAN,
        basis_kind="claim_link",
        basis_ref=orphan_link.id,
        at=T2,
    )

    codes = {item.code for item in integrity_findings(store)}
    assert "active_supersession_without_status" in codes
    assert "supersession_time_mismatch" in codes
    assert "superseded_before_successor_confirmation" in codes
    assert "superseded_without_successor" in codes


def test_integrity_uses_frozen_supersession_reasons_and_required_intervals(
    store: TruthStore,
) -> None:
    links: dict[str, ClaimLinkRecord] = {}
    for reason, valid_from, valid_to in (
        ("source_retracted", None, None),
        ("preference_changed", "2025-01-01", None),
        ("correction", None, None),
        ("expanded_scope", None, None),
        ("updated", None, None),
        ("valid_time_closed", None, None),
    ):
        predecessor = _claim(store, f"{reason} predecessor")
        successor = _claim(
            store,
            f"{reason} successor",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        links[reason] = _raw_link(
            store,
            successor,
            predecessor,
            reason=reason,
        )

    findings = integrity_findings(store)
    invalid_reason_refs = {
        item.subject_ref
        for item in findings
        if item.code == "invalid_supersession_reason"
    }
    assert invalid_reason_refs == {
        links["correction"].id,
        links["expanded_scope"].id,
    }
    assert any(
        item.code == "supersession_missing_successor_valid_from"
        and item.subject_ref == links["updated"].id
        for item in findings
    )
    assert any(
        item.code == "supersession_missing_successor_valid_to"
        and item.subject_ref == links["valid_time_closed"].id
        for item in findings
    )


def test_integrity_weakest_link_treats_review_overlay_as_unconfirmed(
    store: TruthStore,
) -> None:
    premise = _claim(store, "Premise under review")
    conclusion = _claim(store, "Derived conclusion")
    _confirm(store, premise, at=T1)
    _overlay(store, premise, at=T2, basis_kind="rule")
    store.add_derivation(
        claim_id=conclusion.id,
        method="entailment",
        premises=[premise.id],
        actor=HUMAN,
        created_at=T2,
    )
    _confirm(store, conclusion, at=T3)

    assert any(
        item.code == "confirmed_derivation_has_unconfirmed_premise" and item.subject_ref
        for item in integrity_findings(store)
    )


def test_integrity_allows_only_fully_bound_reasoned_rejection_replay(
    store: TruthStore,
) -> None:
    _reasoned_rejection(store, kind="reject_as_false")
    _reasoned_rejection(store, kind="reject_as_preference")
    assert integrity_findings(store) == ()

    first = _claim(store, "Arbitrary replay first")
    second = _claim(store, "Arbitrary replay second")
    gesture = GestureRecord(
        id=new_id(),
        at=T2,
        surface="dashboard",
        actor_ref="user-1",
        kind="confirm",
        subject_ref=first.id,
        payload_sha256=first.canonical_sha256,
        payload_excerpt=first.proposition,
        context_sha256=None,
        expires_at=None,
        consumed_at=T2,
    )
    with store.write_transaction() as conn:
        store._insert_gesture_locked(conn, gesture)
        store._insert_status_event_locked(
            conn,
            claim_id=first.id,
            status="confirmed",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture.id,
            at=T2,
        )
        store._insert_status_event_locked(
            conn,
            claim_id=second.id,
            status="rejected",
            actor=HUMAN,
            basis_kind="gesture",
            basis_ref=gesture.id,
            at=T2,
        )
    assert "gesture_replay" in {item.code for item in integrity_findings(store)}


def test_integrity_is_fail_soft_for_malformed_rows_and_checks_producer_trust_ledger(
    store: TruthStore,
) -> None:
    agent_claim = _claim(store, "Raw agent claim")
    malformed = store.capture_evidence(
        kind="document",
        source_locator="file:///malformed.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="malformed source",
        created_at=T0,
    )
    reviewed_external = store.capture_evidence(
        kind="web",
        source_locator="https://example.test/source",
        actor=HUMAN,
        acquisition_method="fetch",
        origin="external",
        external_reviewed=True,
        content="reviewed source",
        created_at=T0,
    )
    producer_meta = canonical_json(
        {
            "model": "test-model",
            "harness": "pytest",
            "surface": "test",
            "session_id": "session-1",
        }
    )

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER claims_append_only_update")
        conn.execute("DROP TRIGGER evidence_append_only_update")
        conn.execute(
            "UPDATE claims SET created_by_kind = 'agent_run', "
            "created_by_ref = 'run-bad', meta_json = '{}' WHERE id = ?",
            (agent_claim.id,),
        )
        conn.execute(
            "UPDATE evidence SET kind = ?, source_locator = ?, "
            "acquired_by_kind = 'agent_run', acquired_by_ref = 'run-bad', "
            "meta_json = '{}' WHERE id = ?",
            (42, 42, malformed.id),
        )
        conn.execute(
            "UPDATE evidence SET acquired_by_kind = 'agent_run', "
            "acquired_by_ref = 'run-reviewed', meta_json = ? WHERE id = ?",
            (producer_meta, reviewed_external.id),
        )
        conn.execute(
            "INSERT INTO ledger_records (record_type, record_key) VALUES (?, ?)",
            ("future_record_type", new_id()),
        )
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

    source_states = source_integrity_states(store)
    malformed_state = next(
        item for item in source_states if item.evidence_id == malformed.id
    )
    assert malformed_state.state == "invalid_locator"

    findings = integrity_findings(store)
    codes = {item.code for item in findings}
    assert {
        "missing_agent_producer_identity",
        "human_trust_without_human_origin",
        "agent_cleared_external_quarantine",
        "invalid_evidence_kind",
        "source_invalid_locator",
        "unknown_ledger_record_type",
    } <= codes


def test_integrity_checks_redaction_gesture_binding_and_freshness(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Redaction integrity target")
    _confirm(store, claim, at=T1)
    lifecycle = TruthLifecycle(store)
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="redact",
        displayed_payload_sha256=claim.canonical_sha256,
        at=T2,
    )
    redaction = TruthRedactor(store, lifecycle=lifecycle).redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
        at=T2,
    )

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER gestures_append_only_update")
        conn.execute("DROP TRIGGER redaction_events_append_only_update")
        conn.execute("DROP TRIGGER claim_status_events_append_only_update")
        conn.execute(
            "UPDATE gestures SET kind = 'scope', consumed_at = ? WHERE id = ?",
            (T1, gesture.id),
        )
        conn.execute(
            "UPDATE redaction_events SET actor_ref = 'another-user', "
            "reason = 'unknown-reason' WHERE id = ?",
            (redaction.event.id,),
        )
        conn.execute(
            "UPDATE claim_status_events SET at = ? WHERE basis_kind = 'redaction' "
            "AND basis_ref = ?",
            (T3, redaction.event.id),
        )
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

    codes = {item.code for item in integrity_findings(store)}
    assert {
        "invalid_redaction_reason",
        "redaction_gesture_actor_mismatch",
        "invalid_redaction_gesture_kind",
        "redaction_gesture_consumption_mismatch",
        "status_redaction_time_mismatch",
        "status_redaction_actor_mismatch",
        "status_redaction_reason_mismatch",
    } <= codes


def test_integrity_checks_standing_policy_redaction_contract(store: TruthStore) -> None:
    rejected = _claim(store, "Retained rejection")
    lifecycle = TruthLifecycle(store)
    reject_gesture = lifecycle.mint_gesture(
        subject_ref=rejected.id,
        actor=HUMAN,
        surface="dashboard",
        kind="reject_plain",
        displayed_payload_sha256=rejected.canonical_sha256,
        at=T1,
    )
    lifecycle.reject_claim(
        source_claim_id=rejected.id,
        gesture_id=reject_gesture.id,
        actor=HUMAN,
        reason_class="reject_plain",
        expected_context_sha256=None,
        observed_at=T1,
        at=T1,
    )
    confirmed = _claim(store, "Ever-confirmed policy target")
    _confirm(store, confirmed, at=T1)

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER claims_append_only_update")
        events = (
            (new_id(), rejected, "wrong-policy-key"),
            (
                new_id(),
                confirmed,
                policy_basis_ref(store, "rejected_content"),
            ),
        )
        for event_id, claim, basis_ref in events:
            conn.execute(
                "UPDATE claims SET proposition = '[redacted]', "
                "structured_json = NULL, redacted_at = ? WHERE id = ?",
                (T2, claim.id),
            )
            conn.execute(
                "INSERT INTO redaction_events "
                "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
                "basis_ref, reason) VALUES (?, 'claim', ?, ?, ?, 'policy', ?, ?)",
                (
                    event_id,
                    claim.id,
                    T2,
                    "truth-policy-test",
                    basis_ref,
                    "rejected_content",
                ),
            )
            store._insert_ledger_record_locked(conn, "redaction_event", event_id)
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

    codes = {item.code for item in integrity_findings(store)}
    assert {
        "invalid_policy_redaction_basis_ref",
        "undeclared_policy_redaction",
        "invalid_policy_redaction_status",
        "policy_redaction_of_confirmed",
    } <= codes


def test_cross_store_premise_resolution_is_fail_soft_and_status_aware(
    store: TruthStore,
) -> None:
    claim = _claim(store, "Cross-store conclusion")
    premise_uri = truth_uri(new_id(), "claim", new_id())
    store.add_derivation(
        claim_id=claim.id,
        method="federated",
        premises=[premise_uri],
        actor=HUMAN,
    )
    unresolved = {item.code for item in integrity_findings(store)}
    assert "external_premise_unresolved" in unresolved

    failed = {
        item.code
        for item in integrity_findings(
            store,
            cross_store_resolver=lambda _uri: (_ for _ in ()).throw(
                RuntimeError("registry offline")
            ),
        )
    }
    assert "external_premise_resolution_failed" in failed
    resolved = integrity_findings(
        store,
        cross_store_resolver=lambda _uri: PremiseResolution(
            exists=True,
            status="confirmed",
        ),
    )
    assert not any(item.code.startswith("external_premise_") for item in resolved)


def test_redaction_integrity_accepts_evidence_cascade_and_direct_span_gestures(
    store: TruthStore,
) -> None:
    cascade_evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///cascade.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="alpha beta",
        created_at=T0,
    )
    cascade_spans = (
        store.mark_span(
            evidence_id=cascade_evidence.id,
            selector=CompositeSelector(exact="alpha"),
            actor=HUMAN,
            created_at=T1,
        ),
        store.mark_span(
            evidence_id=cascade_evidence.id,
            selector=CompositeSelector(exact="beta"),
            actor=HUMAN,
            created_at=T1,
        ),
    )
    direct_evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///direct.txt",
        actor=HUMAN,
        acquisition_method="paste",
        content="gamma delta",
        created_at=T0,
    )
    direct_span = store.mark_span(
        evidence_id=direct_evidence.id,
        selector=CompositeSelector(exact="gamma"),
        actor=HUMAN,
        created_at=T1,
    )
    cascade_gesture = GestureRecord(
        id=new_id(),
        at=T2,
        surface="dashboard",
        actor_ref="user-1",
        kind="redact",
        subject_ref=cascade_evidence.id,
        payload_sha256=cascade_evidence.content_sha256,
        payload_excerpt="cascade.txt",
        context_sha256=None,
        expires_at=None,
        consumed_at=T2,
    )
    direct_gesture = GestureRecord(
        id=new_id(),
        at=T2,
        surface="dashboard",
        actor_ref="user-1",
        kind="redact",
        subject_ref=direct_span.id,
        payload_sha256=direct_span.span_sha256,
        payload_excerpt="gamma",
        context_sha256=None,
        expires_at=None,
        consumed_at=T2,
    )
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        store._insert_gesture_locked(conn, cascade_gesture)
        store._insert_gesture_locked(conn, direct_gesture)
        conn.execute(
            "UPDATE evidence SET content = NULL, content_path = NULL, "
            "redacted_at = ? WHERE id = ?",
            (T2, cascade_evidence.id),
        )
        conn.execute(
            "UPDATE evidence_spans SET quote_exact = NULL, redacted_at = ? "
            "WHERE evidence_id = ?",
            (T2, cascade_evidence.id),
        )
        conn.execute(
            "UPDATE evidence_spans SET quote_exact = NULL, redacted_at = ? "
            "WHERE id = ?",
            (T2, direct_span.id),
        )

        events = [
            (
                new_id(),
                "evidence",
                cascade_evidence.id,
                cascade_gesture.id,
            ),
            *[
                (new_id(), "span", span.id, cascade_gesture.id)
                for span in cascade_spans
            ],
            (
                new_id(),
                "evidence_span",
                direct_span.id,
                direct_gesture.id,
            ),
        ]
        for event_id, subject_kind, subject_ref, gesture_id in events:
            conn.execute(
                "INSERT INTO redaction_events "
                "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
                "basis_ref, reason) VALUES (?, ?, ?, ?, ?, 'gesture', ?, ?)",
                (
                    event_id,
                    subject_kind,
                    subject_ref,
                    T2,
                    "user-1",
                    gesture_id,
                    "privacy",
                ),
            )
            store._insert_ledger_record_locked(conn, "redaction_event", event_id)
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()

    findings = integrity_findings(store)
    assert {item.code for item in findings} == {"redaction_subject_kind_alias"}
    assert not any(
        item.code == "redaction_gesture_subject_mismatch" for item in findings
    )


def test_redacted_claim_never_appears_as_current(store: TruthStore) -> None:
    claim = _claim(store, "Private fact")
    _confirm(store, claim)
    redaction_id = new_id()
    gesture_id = new_id()
    with store.write_transaction() as conn:
        store._insert_gesture_locked(
            conn,
            GestureRecord(
                id=gesture_id,
                at=T2,
                surface="dashboard",
                actor_ref="user-1",
                kind="redact",
                subject_ref=claim.id,
                payload_sha256=claim.canonical_sha256,
                payload_excerpt=claim.proposition,
                context_sha256=None,
                expires_at=None,
                consumed_at=T2,
            ),
        )
        conn.execute(
            "UPDATE claims SET proposition = '[redacted]', structured_json = NULL, "
            "redacted_at = ? WHERE id = ?",
            (T2, claim.id),
        )
        conn.execute(
            "INSERT INTO redaction_events "
            "(id, subject_kind, subject_ref, at, actor_ref, basis_kind, "
            "basis_ref, reason) VALUES (?, 'claim', ?, ?, ?, 'gesture', ?, ?)",
            (redaction_id, claim.id, T2, "user-1", gesture_id, "privacy"),
        )
        store._insert_ledger_record_locked(conn, "redaction_event", redaction_id)
        store._insert_status_event_locked(
            conn,
            claim_id=claim.id,
            status="retracted",
            actor=HUMAN,
            basis_kind="redaction",
            basis_ref=redaction_id,
            note="privacy",
            at=T2,
        )
    assert current_claims(store) == ()
    rebuild_claims_current(store, rebuilt_at=T3)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, health FROM claims_current WHERE claim_id = ?",
            (claim.id,),
        ).fetchone()
    assert tuple(row) == ("retracted", "redacted")
    assert integrity_findings(store) == ()
