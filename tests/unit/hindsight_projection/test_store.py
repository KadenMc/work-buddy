from __future__ import annotations

import sqlite3

import pytest

from work_buddy.hindsight_projection.contracts import (
    DesiredProjectionState,
    OutboxState,
    ProjectionConflict,
    ProjectionValidationError,
)
from work_buddy.hindsight_projection.schema import (
    install_truth_hindsight_projection_schema,
)
from work_buddy.hindsight_projection.store import TruthHindsightProjectionStore

from .conftest import NOW, digest, make_spec


def test_schema_install_stays_inside_callers_truth_transaction(tmp_path) -> None:
    db_path = tmp_path / "rollback.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        install_truth_hindsight_projection_schema(conn)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = "
            "'truth_hindsight_projection_outbox'"
        ).fetchone() is None
    finally:
        conn.close()


def test_enqueue_is_truth_transactional_idempotent_and_content_minimized(
    projection_store,
) -> None:
    spec = make_spec()
    conn = projection_store.connect()
    try:
        with pytest.raises(ProjectionValidationError):
            projection_store.enqueue_in_transaction(conn, spec)

        conn.execute("BEGIN IMMEDIATE")
        first = projection_store.enqueue_in_transaction(conn, spec)
        replay = projection_store.enqueue_in_transaction(conn, spec)
        conn.commit()
    finally:
        conn.close()

    assert replay.effect_id == first.effect_id
    assert first.state is OutboxState.PENDING
    raw = projection_store.db_path.read_bytes()
    assert b"I prefer concise" not in raw


def test_same_generation_with_different_intent_conflicts(projection_store) -> None:
    original = make_spec()
    projection_store.enqueue(original)
    conflicting = make_spec(
        generation=original.claim_generation,
        desired=DesiredProjectionState.REMOVE,
        reason="claim_challenged",
    )
    with pytest.raises(ProjectionConflict):
        projection_store.enqueue(conflicting)


def test_new_generation_supersedes_only_work_that_has_not_crossed_delivery(
    projection_store,
) -> None:
    old = projection_store.enqueue(make_spec())
    newer = projection_store.enqueue(
        make_spec(
            generation=digest("generation-2"),
            desired=DesiredProjectionState.REMOVE,
            reason="claim_challenged",
        )
    )
    assert projection_store.get_effect(old.effect_id).state is OutboxState.SUPERSEDED
    assert projection_store.current_effect("claim-0001", "default").effect_id == newer.effect_id


def test_expired_worker_lease_reconciles_instead_of_replaying(projection_store) -> None:
    effect = projection_store.enqueue(make_spec())
    first = projection_store.acquire_next(
        worker_id="worker-0001", at=NOW, lease_seconds=1
    )
    assert first is not None
    recovered = projection_store.acquire_next(
        worker_id="worker-0002",
        at="2026-08-09T12:00:02.000Z",
        lease_seconds=10,
    )
    assert recovered is not None
    assert recovered.effect.effect_id == effect.effect_id
    assert recovered.attempt_no == first.attempt_no
    assert recovered.reconcile_existing is True


def test_disabled_rollout_stays_awake_for_removal_and_source_cleanup(
    projection_store,
) -> None:
    assert projection_store.has_tracked_projection_state() is False
    remove = make_spec(
        desired=DesiredProjectionState.REMOVE,
        reason="source_redacted",
        purge=True,
    )
    effect = projection_store.enqueue(remove)
    assert projection_store.has_tracked_projection_state() is True

    with projection_store.write_transaction() as conn:
        conn.execute(
            "UPDATE truth_hindsight_projection_outbox SET state = 'delivered' "
            "WHERE effect_id = ?",
            (effect.effect_id,),
        )
        conn.execute(
            "INSERT INTO truth_hindsight_projection_source_cleanup "
            "(cleanup_id, effect_id, source_ref, authorization_ref, reason_code, "
            "state, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                "cleanup-0001",
                effect.effect_id,
                "wb-source://authority1/item/derived01",
                remove.authorization_ref,
                "truth_projection_source_redacted",
                NOW,
            ),
        )
    assert projection_store.has_tracked_projection_state() is True

    projection_store.complete_source_cleanup("cleanup-0001", at=NOW)
    assert projection_store.has_tracked_projection_state() is False
