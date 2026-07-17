"""Invariant tests for the co-work document engine (registration + lifecycle)."""

from __future__ import annotations

import sqlite3

import pytest

from work_buddy.truth import documents
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_bytes, sha256_text


# Shared actors/timestamps mirror the truth conftest refs. The mint and register
# fixtures default to the same human ref, so gestures verify against these.
NOW = "2026-07-17T12:00:00.000+00:00"
LATER = "2026-07-17T12:05:00.000+00:00"
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


def _hash(text: str) -> str:
    return sha256_text(text)


def test_registration_is_idempotent_by_path(document_store):
    store, _ = document_store
    first = documents.register_document(
        store,
        path="docs/design.md",
        document_class="co_authored",
        content_sha256=_hash("v0"),
        actor=HUMAN,
        at=NOW,
    )
    second = documents.register_document(
        store,
        path="docs/design.md",
        document_class="generated",
        content_sha256=_hash("different"),
        actor=AGENT,
        at=LATER,
    )
    assert second.id == first.id
    assert second.document_class == "co_authored"
    with store.connect() as conn:
        events = store._document_events_locked(conn, first.id)
    # A fresh registration appends registered + imported. The repeat appends none.
    assert [event.kind for event in events] == ["registered", "imported"]


def test_registration_rejects_unknown_document_class(document_store):
    store, _ = document_store
    with pytest.raises(InvariantViolation):
        documents.register_document(
            store,
            path="docs/x.md",
            document_class="freeform",
            content_sha256=_hash("v0"),
            actor=HUMAN,
            at=NOW,
        )


def test_lifecycle_projects_active_then_retired(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    assert documents.current_lifecycle(store, document_id) == "active"
    documents.retire_document(store, document_id=document_id, actor=HUMAN, at=LATER)
    assert documents.current_lifecycle(store, document_id) == "retired"


def test_list_documents_filters_retired(document_store, register_document):
    store, _ = document_store
    live, _, _ = register_document(store, path="docs/live.md")
    retired, _, _ = register_document(store, path="docs/gone.md")
    documents.retire_document(store, document_id=retired, actor=HUMAN, at=LATER)
    live_ids = {record.id for record in documents.list_documents(store)}
    all_ids = {
        record.id for record in documents.list_documents(store, include_retired=True)
    }
    assert live in live_ids and retired not in live_ids
    assert {live, retired} <= all_ids


def test_record_materialization_advances_pointer(document_store, register_document):
    store, _ = document_store
    document_id, h0, _ = register_document(store)
    h1 = _hash("materialized-body")
    documents.record_materialization(
        store, document_id=document_id, content_sha256=h1, actor=HUMAN, at=LATER
    )
    assert documents.get_document(store, document_id).content_sha256 == h1
    with store.connect() as conn:
        kinds = [e.kind for e in store._document_events_locked(conn, document_id)]
    assert kinds[-1] == "materialized"


def test_drift_state_is_a_pure_read(document_store, register_document):
    store, _ = document_store
    document_id, h0, _ = register_document(store)
    # A matching hash is clean, a different one is drifted, and neither writes.
    assert documents.drift_state(store, document_id, current_file_sha256=h0) == "clean"
    with store.connect() as conn:
        before = len(store._document_events_locked(conn, document_id))
    assert (
        documents.drift_state(store, document_id, current_file_sha256=_hash("edited"))
        == "drifted"
    )
    with store.connect() as conn:
        after = len(store._document_events_locked(conn, document_id))
    assert before == after


def test_drift_state_missing_when_file_absent(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store, path="docs/never-written.md")
    assert documents.drift_state(store, document_id) == "missing"


def test_detect_drift_appends_event_only_on_change(document_store, register_document):
    store, _ = document_store
    document_id, h0, _ = register_document(store)
    assert (
        documents.detect_drift(
            store,
            document_id=document_id,
            current_file_sha256=h0,
            actor=SYSTEM,
            at=LATER,
        )
        is None
    )
    event = documents.detect_drift(
        store,
        document_id=document_id,
        current_file_sha256=_hash("out-of-band"),
        actor=SYSTEM,
        at=LATER,
    )
    assert event is not None and event.kind == "drift_detected"


def test_reimport_records_change_and_advances(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    h1 = sha256_bytes(b"out-of-band edit")
    documents.reimport_document(
        store, document_id=document_id, content_sha256=h1, actor=HUMAN, at=LATER
    )
    assert documents.get_document(store, document_id).content_sha256 == h1
    with store.connect() as conn:
        kinds = [e.kind for e in store._document_events_locked(conn, document_id)]
    assert kinds[-1] == "reimported"


def test_advance_snapshot_prunes_prior_blob(document_store, register_document):
    store, _ = document_store
    from work_buddy.truth import ydoc_store

    document_id, _, prior_snapshot = register_document(store)
    prior_path = store.resolve_blob_path(f"blobs/{prior_snapshot}")
    assert prior_path.exists()
    new_snapshot = ydoc_store.write_snapshot(store, snapshot=b"second-snapshot")
    documents.advance_snapshot(
        store,
        document_id=document_id,
        ydoc_snapshot_sha256=new_snapshot,
        actor=HUMAN,
        at=LATER,
    )
    assert documents.get_document(store, document_id).ydoc_snapshot_sha256 == new_snapshot
    # The now-unreferenced prior snapshot blob is pruned.
    assert not prior_path.exists()


def test_append_only_triggers_reject_mutation(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    conn = store.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        with pytest.raises(sqlite3.IntegrityError):
            # Rewriting an identity column is forbidden by the carve-out trigger.
            conn.execute(
                "UPDATE documents SET path = 'docs/moved.md' WHERE id = ?",
                (document_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM doc_events WHERE document_id = ?", (document_id,))
    finally:
        conn.close()


def test_unknown_document_raises(document_store):
    store, _ = document_store
    with pytest.raises(InvariantViolation):
        documents.get_document(store, "0" * 32)
