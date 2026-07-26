"""Unit tests for the opaque Yjs blob transport (R3 / R4 framing and slicing)."""

from __future__ import annotations

import struct
from contextlib import contextmanager

import pytest

from work_buddy.cowork import transport
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_bytes

from .conftest import HUMAN, NOW

_PREFIX = struct.Struct(">I")


def test_frame_round_trips_multiple_segments():
    segments = [b"", b"one", b"\x00\x01\x02", b"a" * 300]
    body = transport.frame_segments(segments)
    assert transport.unframe_segments(body) == segments


def test_unframe_rejects_truncated_body():
    body = _PREFIX.pack(10) + b"short"
    with pytest.raises(InvariantViolation):
        transport.unframe_segments(body)


def test_framing_bounds_each_opaque_segment(monkeypatch):
    monkeypatch.setattr(ydoc_store, "MAX_OPAQUE_SEGMENT_BYTES", 8)

    with pytest.raises(transport.OpaqueSegmentTooLarge) as outbound:
        transport.frame_segments([b"allowed", b"123456789"])
    assert outbound.value.segment_index == 1
    assert outbound.value.size == 9

    oversized_header = _PREFIX.pack(9) + b"123456789"
    with pytest.raises(transport.OpaqueSegmentTooLarge) as inbound:
        transport.unframe_segments(oversized_header)
    assert inbound.value.segment_index == 0
    assert inbound.value.limit == 8


def test_pull_without_offset_leads_with_snapshot(seeded):
    store = seeded["store"]
    document = seeded["document"]
    body, headers = transport.pull_ydoc(store, document)
    segments = transport.unframe_segments(body)
    assert segments == [seeded["snapshot_bytes"]]
    assert headers["X-WB-Snapshot-Sha256"] == seeded["snapshot_sha256"]
    assert headers["X-WB-Doc-Sha256"] == seeded["content_sha256"]
    assert headers["X-WB-Projection-Sha256"] == seeded["content_sha256"]
    assert headers["X-WB-Ydoc-Head-Sha256"]
    assert headers["X-WB-Ydoc-Generation"] == documents.current_ydoc_generation(
        store, document.id
    )
    assert headers["X-WB-Next-Offset"] == "cowork-cursor-v1:0:0"


def test_pull_with_offset_returns_only_later_batches(seeded):
    store = seeded["store"]
    document = seeded["document"]
    first = ydoc_store.append_update(store, document_id=document.id, update=b"batch-1")
    ydoc_store.append_update(store, document_id=document.id, update=b"batch-2")
    cursor = ydoc_store.encode_cursor("0", int(first))
    body, headers = transport.pull_ydoc(store, document, since_offset=cursor)
    segments = transport.unframe_segments(body)
    assert segments == [b"batch-2"]
    assert "X-WB-Snapshot-Sha256" not in headers


def test_pull_snapshots_payload_cursor_and_head_under_document_lock(
    seeded, monkeypatch
):
    store = seeded["store"]
    document = seeded["document"]
    original_lock = ydoc_store.document_lock
    original_read_snapshot = ydoc_store.read_snapshot
    original_read_epoch_updates = ydoc_store.read_epoch_updates
    original_current_head = ydoc_store.current_structured_head
    state = {"held": False, "entries": 0}

    @contextmanager
    def tracking_lock(*args, **kwargs):
        with original_lock(*args, **kwargs):
            state["held"] = True
            state["entries"] += 1
            try:
                yield
            finally:
                state["held"] = False

    def checked_snapshot(*args, **kwargs):
        assert state["held"] is True
        return original_read_snapshot(*args, **kwargs)

    def checked_epoch_updates(*args, **kwargs):
        assert state["held"] is True
        return original_read_epoch_updates(*args, **kwargs)

    def checked_current_head(*args, **kwargs):
        assert state["held"] is True
        return original_current_head(*args, **kwargs)

    monkeypatch.setattr(ydoc_store, "document_lock", tracking_lock)
    monkeypatch.setattr(ydoc_store, "read_snapshot", checked_snapshot)
    monkeypatch.setattr(ydoc_store, "read_epoch_updates", checked_epoch_updates)
    monkeypatch.setattr(ydoc_store, "current_structured_head", checked_current_head)

    body, headers = transport.pull_ydoc(store, document)
    segments = transport.unframe_segments(body)

    assert state == {"held": False, "entries": 1}
    assert headers["X-WB-Ydoc-Head-Sha256"] == (
        ydoc_store.structured_head_from_segments(segments[0], segments[1:])
    )


def test_pull_refreshes_snapshot_pointer_inside_lock(seeded):
    store = seeded["store"]
    stale_document = seeded["document"]
    prior_head = ydoc_store.current_structured_head(
        store,
        document_id=stale_document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    replacement = b"YDOC-COMPACTED-SNAPSHOT-v3"
    replacement_sha256 = sha256_bytes(replacement)

    ydoc_store.compact_and_advance(
        store,
        document_id=stale_document.id,
        snapshot=replacement,
        expected_snapshot_sha256=replacement_sha256,
        expected_structured_head_sha256=prior_head,
        actor=HUMAN,
        at=NOW,
    )

    body, headers = transport.pull_ydoc(store, stale_document)

    assert transport.unframe_segments(body) == [replacement]
    assert headers["X-WB-Snapshot-Sha256"] == replacement_sha256
    assert headers["X-WB-Ydoc-Head-Sha256"] == (
        ydoc_store.structured_head_from_segments(replacement, ())
    )


def test_push_appends_batch_and_reports_next_offset(seeded):
    store = seeded["store"]
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    payload, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=b"human-edit-batch",
        base_structured_head_sha256=head,
        base_ydoc_generation=documents.current_ydoc_generation(
            store, document.id
        ),
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["applied"] is True
    assert payload["doc_sha256"] == seeded["content_sha256"]
    batches, _ = ydoc_store.read_updates(store, document_id=document.id)
    assert batches == (b"human-edit-batch",)


def test_push_stale_base_is_rejected(seeded):
    store = seeded["store"]
    document = seeded["document"]
    payload, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=b"human-edit-batch",
        base_sha256="0" * 64,
        base_ydoc_generation=documents.current_ydoc_generation(
            store, document.id
        ),
    )
    assert status == 409
    assert payload["error"] == "stale_base"
    assert payload["server_doc_sha256"] == seeded["content_sha256"]
    # Nothing was appended on a rejected push.
    batches, _ = ydoc_store.read_updates(store, document_id=document.id)
    assert batches == ()


def test_push_rejects_an_old_generation_when_snapshot_and_head_repeat(seeded):
    store = seeded["store"]
    document = seeded["document"]
    generation_before = documents.current_ydoc_generation(store, document.id)
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    projection = (seeded["root"] / document.path).read_bytes()
    store._store_blob_bytes(document.content_sha256, projection)
    documents.commit_document_version(
        store,
        document_id=document.id,
        kind="reimported",
        projection_sha256=document.content_sha256,
        ydoc_snapshot_sha256=seeded["snapshot_sha256"],
        structured_head_sha256=head,
        actor=HUMAN,
        at=NOW,
        detail="identical_opaque_replacement",
    )
    refreshed = documents.get_document(store, document.id)

    payload, status = transport.push_ydoc(
        store,
        refreshed,
        HUMAN,
        body=b"old-generation-edit",
        base_structured_head_sha256=head,
        base_ydoc_generation=generation_before,
    )

    assert status == 409
    assert payload["error"]["code"] == "ydoc_generation_changed"
    assert ydoc_store.read_updates(store, document_id=document.id)[0] == ()


def test_push_compaction_advances_snapshot_and_truncates_log(seeded):
    store = seeded["store"]
    document = seeded["document"]
    generation_before = documents.current_ydoc_generation(store, document.id)
    new_snapshot = b"YDOC-COMPACTED-SNAPSHOT-v2"
    declared = sha256_bytes(new_snapshot)
    body = transport.frame_segments([b"final-batch", new_snapshot])
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    payload, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=body,
        base_structured_head_sha256=head,
        base_ydoc_generation=generation_before,
        compacted_snapshot_sha256=declared,
        at=NOW,
    )
    assert status == 200
    # The durable snapshot pointer advanced to the client-compacted snapshot.
    refreshed = documents.get_document(store, document.id)
    assert refreshed.ydoc_snapshot_sha256 == declared
    # The superseded update log is truncated.
    batches, next_offset = ydoc_store.read_updates(store, document_id=document.id)
    assert batches == ()
    assert next_offset == "0"
    assert payload["next_offset"].startswith("cowork-cursor-v1:")
    assert payload["ydoc_generation"] == generation_before
    _, headers = transport.pull_ydoc(store, document)
    assert headers["X-WB-Ydoc-Generation"] == generation_before


def test_push_compaction_requires_two_segments(seeded):
    store = seeded["store"]
    document = seeded["document"]
    body = transport.frame_segments([b"only-one-segment"])
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    with pytest.raises(InvariantViolation):
        transport.push_ydoc(
            store,
            document,
            HUMAN,
            body=body,
            base_structured_head_sha256=head,
            base_ydoc_generation=documents.current_ydoc_generation(
                store, document.id
            ),
            compacted_snapshot_sha256="0" * 64,
        )


def test_push_size_failures_are_typed_and_mutate_nothing(seeded, monkeypatch):
    store = seeded["store"]
    document = seeded["document"]
    head = ydoc_store.current_structured_head(
        store,
        document_id=document.id,
        snapshot_sha256=seeded["snapshot_sha256"],
    )
    before_document = documents.get_document(store, document.id)
    before_blobs = sorted(path.name for path in store.paths.blobs.iterdir())
    before_export = store.paths.claims_export.read_bytes()
    monkeypatch.setattr(ydoc_store, "MAX_OPAQUE_SEGMENT_BYTES", 8)

    update_payload, update_status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=b"123456789",
        base_structured_head_sha256=head,
        base_ydoc_generation=documents.current_ydoc_generation(
            store, document.id
        ),
    )
    assert update_status == 413
    assert update_payload["error"]["code"] == "update_too_large"

    oversized_snapshot = b"abcdefghi"
    compacted_body = (
        _PREFIX.pack(1)
        + b"u"
        + _PREFIX.pack(len(oversized_snapshot))
        + oversized_snapshot
    )
    snapshot_payload, snapshot_status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=compacted_body,
        base_structured_head_sha256=head,
        base_ydoc_generation=documents.current_ydoc_generation(
            store, document.id
        ),
        compacted_snapshot_sha256=sha256_bytes(oversized_snapshot),
    )
    assert snapshot_status == 413
    assert snapshot_payload["error"]["code"] == "snapshot_too_large"

    assert documents.get_document(store, document.id) == before_document
    assert ydoc_store.read_updates(store, document_id=document.id)[0] == ()
    assert sorted(path.name for path in store.paths.blobs.iterdir()) == before_blobs
    assert store.paths.claims_export.read_bytes() == before_export
