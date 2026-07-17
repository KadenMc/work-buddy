"""Invariant tests for opaque Y.Doc snapshot + update-log persistence."""

from __future__ import annotations

import pytest

from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.identity import sha256_bytes


NOW = "2026-07-17T12:00:00.000+00:00"
LATER = "2026-07-17T12:05:00.000+00:00"
HUMAN = Actor("human", "reviewer-kaden")


def test_append_and_read_updates_round_trip(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    off1 = ydoc_store.append_update(store, document_id=document_id, update=b"batch-one")
    off2 = ydoc_store.append_update(store, document_id=document_id, update=b"batch-two")
    assert int(off2) > int(off1)
    batches, next_offset = ydoc_store.read_updates(store, document_id=document_id)
    assert batches == (b"batch-one", b"batch-two")
    assert next_offset == off2


def test_read_updates_slices_by_offset(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    first = ydoc_store.append_update(store, document_id=document_id, update=b"one")
    ydoc_store.append_update(store, document_id=document_id, update=b"two")
    batches, _ = ydoc_store.read_updates(
        store, document_id=document_id, since_offset=first
    )
    assert batches == (b"two",)


def test_append_update_rejects_non_bytes(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    with pytest.raises(InvariantViolation):
        ydoc_store.append_update(store, document_id=document_id, update="not bytes")


def test_runtime_dir_is_created_under_sidecar(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    runtime = ydoc_store.runtime_dir(store, document_id)
    assert runtime.is_dir()
    assert runtime.parent.name == "runtime"
    assert runtime.parent.parent == store.paths.sidecar


def test_write_and_read_snapshot(document_store):
    store, _ = document_store
    payload = b"opaque-compacted-snapshot"
    digest = ydoc_store.write_snapshot(store, snapshot=payload)
    assert digest == sha256_bytes(payload)
    assert ydoc_store.read_snapshot(store, snapshot_sha256=digest) == payload


def test_write_snapshot_verifies_expected_hash(document_store):
    store, _ = document_store
    with pytest.raises(InvariantViolation):
        ydoc_store.write_snapshot(
            store, snapshot=b"payload", expected_sha256=sha256_bytes(b"different")
        )


def test_read_snapshot_missing_raises(document_store):
    store, _ = document_store
    with pytest.raises(InvariantViolation):
        ydoc_store.read_snapshot(store, snapshot_sha256=sha256_bytes(b"absent"))


def test_compact_truncates_the_update_log(document_store, register_document):
    store, _ = document_store
    document_id, _, _ = register_document(store)
    ydoc_store.append_update(store, document_id=document_id, update=b"pre-compaction")
    digest = ydoc_store.compact(
        store, document_id=document_id, snapshot=b"compacted-state"
    )
    assert ydoc_store.read_snapshot(store, snapshot_sha256=digest) == b"compacted-state"
    batches, next_offset = ydoc_store.read_updates(store, document_id=document_id)
    assert batches == ()
    assert next_offset == "0"


def test_prune_removes_unreferenced_snapshot(document_store):
    store, _ = document_store
    digest = ydoc_store.write_snapshot(store, snapshot=b"orphan-snapshot")
    blob = store.resolve_blob_path(f"blobs/{digest}")
    assert blob.exists()
    assert ydoc_store.prune_snapshot_blob(store, snapshot_sha256=digest) is True
    assert not blob.exists()


def test_prune_keeps_referenced_snapshot(document_store, register_document):
    store, _ = document_store
    document_id, _, snapshot = register_document(store)
    blob = store.resolve_blob_path(f"blobs/{snapshot}")
    # The document row references this snapshot, so pruning must not remove it.
    assert ydoc_store.prune_snapshot_blob(store, snapshot_sha256=snapshot) is False
    assert blob.exists()


def test_snapshot_advance_updates_document_pointer(document_store, register_document):
    store, _ = document_store
    document_id, _, first = register_document(store)
    second = ydoc_store.write_snapshot(store, snapshot=b"second-state")
    documents.advance_snapshot(
        store,
        document_id=document_id,
        ydoc_snapshot_sha256=second,
        actor=HUMAN,
        at=LATER,
    )
    assert documents.get_document(store, document_id).ydoc_snapshot_sha256 == second
