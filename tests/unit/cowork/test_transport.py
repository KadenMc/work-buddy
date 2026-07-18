"""Unit tests for the opaque Yjs blob transport (R3 / R4 framing and slicing)."""

from __future__ import annotations

import struct

import pytest

from work_buddy.cowork import transport
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import InvariantViolation

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


def test_pull_without_offset_leads_with_snapshot(seeded):
    store = seeded["store"]
    document = seeded["document"]
    body, headers = transport.pull_ydoc(store, document)
    segments = transport.unframe_segments(body)
    assert segments == [seeded["snapshot_bytes"]]
    assert headers["X-WB-Snapshot-Sha256"] == seeded["snapshot_sha256"]
    assert headers["X-WB-Doc-Sha256"] == seeded["content_sha256"]
    assert headers["X-WB-Next-Offset"] == "0"


def test_pull_with_offset_returns_only_later_batches(seeded):
    store = seeded["store"]
    document = seeded["document"]
    first = ydoc_store.append_update(store, document_id=document.id, update=b"batch-1")
    ydoc_store.append_update(store, document_id=document.id, update=b"batch-2")
    body, headers = transport.pull_ydoc(store, document, since_offset=first)
    segments = transport.unframe_segments(body)
    assert segments == [b"batch-2"]
    assert "X-WB-Snapshot-Sha256" not in headers


def test_push_appends_batch_and_reports_next_offset(seeded):
    store = seeded["store"]
    document = seeded["document"]
    payload, status = transport.push_ydoc(
        store, document, HUMAN, body=b"human-edit-batch"
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
    )
    assert status == 409
    assert payload["error"] == "stale_base"
    assert payload["server_doc_sha256"] == seeded["content_sha256"]
    # Nothing was appended on a rejected push.
    batches, _ = ydoc_store.read_updates(store, document_id=document.id)
    assert batches == ()


def test_push_compaction_advances_snapshot_and_truncates_log(seeded):
    store = seeded["store"]
    document = seeded["document"]
    new_snapshot = b"YDOC-COMPACTED-SNAPSHOT-v2"
    from work_buddy.truth.identity import sha256_bytes

    declared = sha256_bytes(new_snapshot)
    body = transport.frame_segments([b"final-batch", new_snapshot])
    payload, status = transport.push_ydoc(
        store,
        document,
        HUMAN,
        body=body,
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
    assert payload["next_offset"] == next_offset == "0"


def test_push_compaction_requires_two_segments(seeded):
    store = seeded["store"]
    document = seeded["document"]
    body = transport.frame_segments([b"only-one-segment"])
    with pytest.raises(InvariantViolation):
        transport.push_ydoc(
            store,
            document,
            HUMAN,
            body=body,
            compacted_snapshot_sha256="0" * 64,
        )
