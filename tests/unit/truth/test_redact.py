from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from work_buddy.truth.anchors import CompositeSelector
from work_buddy.truth.contracts import Actor, GestureError, InvariantViolation
from work_buddy.truth.identity import new_id, utc_now
from work_buddy.truth.lifecycle import TruthLifecycle
from work_buddy.truth.redact import TruthRedactor, policy_basis_ref
from work_buddy.truth.store import GestureRecord, TruthStore


HUMAN = Actor("human", "human:test")
SYSTEM = Actor("system", "system:truth-policy")


def _profile(**overrides):
    profile = {
        "store_id": new_id(),
        "profile": "redaction-test",
        "title": "Redaction test store",
        "allowed_claim_kinds": ["fact"],
        "required_fields": {},
        "gate": {
            "rejected_content": "redact",
            "confirmation_surfaces": ["dashboard"],
            "block_materialize_on_flags": False,
        },
        "projection": "none",
        "export_committed": True,
        "proposal_max_age": "2h",
        "extensions": {"expired_proposal_content": "redact"},
    }
    profile.update(overrides)
    return profile


class _Lifecycle:
    """A strict local double for the separately tested lifecycle component."""

    def __init__(self, store: TruthStore) -> None:
        self.store = store

    def verify_and_consume_gesture(
        self,
        *,
        gesture_id,
        actor,
        subject_ref,
        payload_sha256,
        expected_context_sha256,
        allowed_kinds,
        observed_at,
        conn,
    ):
        gesture = self.store._get_gesture_locked(conn, gesture_id)
        if gesture is None:
            raise GestureError("gesture does not exist")
        if actor.kind != "human" or gesture.actor_ref != actor.ref:
            raise GestureError("gesture actor mismatch")
        if gesture.kind not in allowed_kinds:
            raise GestureError("gesture kind mismatch")
        if gesture.subject_ref != subject_ref:
            raise GestureError("gesture subject mismatch")
        if gesture.payload_sha256 != payload_sha256:
            raise GestureError("gesture payload mismatch")
        if gesture.context_sha256 != expected_context_sha256:
            raise GestureError("gesture context mismatch")
        return self.store._consume_gesture_locked(conn, gesture_id, observed_at)

    def transition_claim(
        self,
        *,
        claim_id,
        status,
        actor,
        basis_kind,
        basis_ref,
        note,
        at,
        conn,
    ):
        event = self.store._insert_status_event_locked(
            conn,
            claim_id=claim_id,
            status=status,
            actor=actor,
            basis_kind=basis_kind,
            basis_ref=basis_ref,
            note=note,
            at=at,
        )
        return SimpleNamespace(event=event, created=True)


@pytest.fixture
def store(truth_root: Path) -> TruthStore:
    return TruthStore.create(truth_root, _profile(), inline_content_bytes=8)


@pytest.fixture
def redactor(store: TruthStore) -> TruthRedactor:
    return TruthRedactor(store, lifecycle=_Lifecycle(store))


def _claim(store: TruthStore, proposition: str = "Keep this claim"):
    return store.propose_claim(
        proposition=proposition,
        claim_kind="fact",
        actor=HUMAN,
    ).claim


def _status(store: TruthStore, claim_id: str, status: str):
    with store.write_transaction() as conn:
        return store._insert_status_event_locked(
            conn,
            claim_id=claim_id,
            status=status,
            actor=SYSTEM,
            basis_kind="rule",
            basis_ref=f"test:{status}",
        )


def _gesture(
    store: TruthStore,
    *,
    subject_ref: str,
    payload_sha256: str,
    context_sha256: str | None = None,
    actor_ref: str = HUMAN.ref or "",
) -> GestureRecord:
    record = GestureRecord(
        id=new_id(),
        at=utc_now(),
        surface="dashboard",
        actor_ref=actor_ref,
        kind="redact",
        subject_ref=subject_ref,
        payload_sha256=payload_sha256,
        payload_excerpt="content shown to the human",
        context_sha256=context_sha256,
        expires_at=None,
        consumed_at=None,
    )
    with store.write_transaction() as conn:
        return store._insert_gesture_locked(conn, record)


def test_gesture_redaction_retains_claim_identity_hash_and_appends_costatus(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "confirmed")
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
        context_sha256="ab" * 32,
    )

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
        expected_context_sha256="ab" * 32,
    )

    redacted = store.get_claim(claim.id)
    assert redacted is not None
    assert redacted.proposition == "[redacted]"
    assert redacted.structured_json is None
    assert redacted.canonical_sha256 == claim.canonical_sha256
    assert redacted.redacted_at == result.event.at
    assert result.status_event is not None
    assert result.status_event.status == "retracted"
    assert result.status_event.basis_kind == "redaction"
    assert result.status_event.basis_ref == result.event.id
    with store.connect() as conn:
        assert store._get_gesture_locked(conn, gesture.id).consumed_at is not None

    with store.connect() as conn:
        ledger_types = [
            row[0]
            for row in conn.execute(
                "SELECT record_type FROM ledger_records ORDER BY seq"
            )
        ]
    assert ledger_types[-2:] == ["redaction_event", "claim_status_event"]


def test_redaction_composes_with_the_real_lifecycle_gesture_seam(
    store: TruthStore,
) -> None:
    lifecycle = TruthLifecycle(store)
    redactor = TruthRedactor(store, lifecycle=lifecycle)
    claim = _claim(store, "One exact private statement")
    gesture = lifecycle.mint_gesture(
        subject_ref=claim.id,
        actor=HUMAN,
        surface="dashboard",
        kind="redact",
        displayed_payload_sha256=claim.canonical_sha256,
    )

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=gesture.id,
    )

    assert result.status_event is not None
    assert lifecycle.latest_status(claim.id).status == "retracted"
    with store.connect() as conn:
        assert store._get_gesture_locked(conn, gesture.id).consumed_at is not None


@pytest.mark.parametrize(
    ("terminal", "reason"),
    [("rejected", "rejected_content"), ("expired", "expired_content")],
)
def test_profile_policy_redacts_never_confirmed_terminal_claims(
    store: TruthStore,
    redactor: TruthRedactor,
    terminal: str,
    reason: str,
) -> None:
    claim = _claim(store, f"Policy {terminal}")
    _status(store, claim.id, terminal)

    result = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=SYSTEM,
        reason=reason,
        basis_kind="policy",
        basis_ref=policy_basis_ref(store, reason),
    )

    assert store.get_claim(claim.id).proposition == "[redacted]"
    assert result.status_event is None


def test_expiry_and_policy_redaction_compose_in_one_outer_transaction(
    store: TruthStore,
) -> None:
    lifecycle = TruthLifecycle(store)
    redactor = TruthRedactor(store, lifecycle=lifecycle)
    claim = store.propose_claim(
        proposition="Discard this untouched option",
        claim_kind="fact",
        actor=HUMAN,
        created_at="2026-07-14T10:00:00+00:00",
        status_at="2026-07-14T10:00:00+00:00",
    ).claim
    observed = "2026-07-14T12:00:01+00:00"

    with store.write_transaction() as conn:
        lifecycle.expire_claim(
            claim_id=claim.id,
            actor=SYSTEM,
            observed_at=observed,
            conn=conn,
        )
        result = redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="expired_content",
            basis_kind="policy",
            basis_ref=policy_basis_ref(store, "expired_content"),
            at=observed,
            conn=conn,
        )

    assert result.status_event is None
    assert lifecycle.latest_status(claim.id).status == "expired"
    assert store.get_claim(claim.id).proposition == "[redacted]"


def test_policy_cannot_redact_content_that_was_ever_confirmed(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "confirmed")
    _status(store, claim.id, "retracted")

    with pytest.raises(InvariantViolation, match="ever confirmed"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="rejected_content",
            basis_kind="policy",
            basis_ref=policy_basis_ref(store, "rejected_content"),
        )


def test_policy_is_exact_reason_status_and_profile_bound(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    _status(store, claim.id, "rejected")

    with pytest.raises(InvariantViolation, match="exactly"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="rejected_content",
            basis_kind="policy",
            basis_ref="profile:anything:gate.rejected_content",
        )
    with pytest.raises(InvariantViolation, match="standing policies"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=SYSTEM,
            reason="privacy",
            basis_kind="policy",
            basis_ref="profile:redaction-test:privacy",
        )


def test_gesture_mismatch_rolls_back_content_event_and_consumption(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256="ff" * 32,
    )

    with pytest.raises(GestureError, match="payload mismatch"):
        redactor.redact(
            subject_kind="claim",
            subject_ref=claim.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
        )

    assert store.get_claim(claim.id).redacted_at is None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM redaction_events").fetchone()[0] == 0
        assert store._get_gesture_locked(conn, gesture.id).consumed_at is None


def test_evidence_redaction_cascades_quotes_and_deletes_unshared_blob(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///source.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"private source bytes",
    )
    span = store.mark_span(
        evidence_id=evidence.id,
        selector=CompositeSelector(exact="private"),
        actor=HUMAN,
        snapshot_text="private source bytes",
    )
    path = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    result = redactor.redact(
        subject_kind="evidence",
        subject_ref=evidence.id,
        actor=HUMAN,
        reason="source_takedown",
        basis_kind="gesture",
        basis_ref=gesture.id,
    )

    assert store.get_evidence(evidence.id).content_path is None
    assert store.get_span(span.id).quote_exact is None
    assert result.cascade_events[0].subject_ref == span.id
    assert result.blob_deleted is True
    assert not path.exists()


def test_shared_blob_survives_until_last_reference_is_redacted(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    content = b"one shared private blob"
    rows = [
        store.capture_evidence(
            kind="document",
            source_locator=f"file:///source-{index}.bin",
            actor=HUMAN,
            acquisition_method="paste",
            content=content,
        )
        for index in (1, 2)
    ]
    path = store.resolve_blob_path(rows[0].content_path or "")

    first = redactor.redact(
        subject_kind="evidence",
        subject_ref=rows[0].id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=_gesture(
            store,
            subject_ref=rows[0].id,
            payload_sha256=rows[0].content_sha256,
        ).id,
    )
    assert first.blob_deleted is False
    assert path.exists()

    second = redactor.redact(
        subject_kind="evidence",
        subject_ref=rows[1].id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=_gesture(
            store,
            subject_ref=rows[1].id,
            payload_sha256=rows[1].content_sha256,
        ).id,
    )
    assert second.blob_deleted is True
    assert not path.exists()


def test_caller_transaction_defers_cleanup_and_rollback_preserves_blob(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    evidence = store.capture_evidence(
        kind="document",
        source_locator="file:///rollback.bin",
        actor=HUMAN,
        acquisition_method="paste",
        content=b"rollback private bytes",
    )
    path = store.resolve_blob_path(evidence.content_path or "")
    gesture = _gesture(
        store,
        subject_ref=evidence.id,
        payload_sha256=evidence.content_sha256,
    )

    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = redactor.redact(
            subject_kind="evidence",
            subject_ref=evidence.id,
            actor=HUMAN,
            reason="privacy",
            basis_kind="gesture",
            basis_ref=gesture.id,
            conn=conn,
        )
        assert result.blob_cleanup_pending is True
        assert path.exists()
        conn.rollback()
    finally:
        conn.close()

    assert store.get_evidence(evidence.id).redacted_at is None
    assert path.exists()
    assert redactor.cleanup_redacted_blob(evidence.content_sha256) is False


def test_redaction_is_idempotent_without_consuming_a_second_gesture(
    store: TruthStore,
    redactor: TruthRedactor,
) -> None:
    claim = _claim(store)
    first_gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )
    first = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=first_gesture.id,
    )
    second_gesture = _gesture(
        store,
        subject_ref=claim.id,
        payload_sha256=claim.canonical_sha256,
    )

    second = redactor.redact(
        subject_kind="claim",
        subject_ref=claim.id,
        actor=HUMAN,
        reason="privacy",
        basis_kind="gesture",
        basis_ref=second_gesture.id,
    )

    assert second.created is False
    assert second.event == first.event
    with store.connect() as conn:
        assert store._get_gesture_locked(conn, second_gesture.id).consumed_at is None
