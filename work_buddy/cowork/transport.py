"""Opaque Yjs blob transport for the co-work HTTP surface (R3 / R4, C3).

v1 has NO server-side Yjs runtime. The single dashboard client is the only Yjs
interpreter. The server here moves OPAQUE bytes only: it frames segments with a
4-byte big-endian length prefix, slices its append log by an opaque offset for
pulls, content-addresses a client-compacted snapshot on push, and never merges,
diffs, or constructs Yjs state.

These functions hold no Flask, so the route layer stays a thin adapter and the
framing plus persistence discipline is unit-testable on its own.
"""

from __future__ import annotations

import struct
from typing import Any

from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.store import DocumentRecord, TruthStore

# 4-byte big-endian length prefix per opaque segment (matches the update-log
# framing the kernel ydoc_store already uses for its runtime batches).
_LENGTH_PREFIX = struct.Struct(">I")


def frame_segment(segment: bytes) -> bytes:
    """Return one length-prefixed opaque segment."""
    if not isinstance(segment, (bytes, bytearray, memoryview)):
        raise InvariantViolation("segment must be opaque bytes")
    payload = bytes(segment)
    return _LENGTH_PREFIX.pack(len(payload)) + payload


def frame_segments(segments: list[bytes]) -> bytes:
    """Frame an ordered list of opaque segments into one body."""
    return b"".join(frame_segment(segment) for segment in segments)


def unframe_segments(body: bytes) -> list[bytes]:
    """Split a length-prefixed body back into its opaque segments."""
    data = bytes(body)
    segments: list[bytes] = []
    cursor = 0
    header = _LENGTH_PREFIX.size
    total = len(data)
    while cursor < total:
        if cursor + header > total:
            raise InvariantViolation("framed body is truncated mid-header")
        (length,) = _LENGTH_PREFIX.unpack(data[cursor : cursor + header])
        cursor += header
        if cursor + length > total:
            raise InvariantViolation("framed body is truncated mid-segment")
        segments.append(data[cursor : cursor + length])
        cursor += length
    return segments


def pull_ydoc(
    store: TruthStore,
    document: DocumentRecord,
    *,
    since_offset: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Assemble the R3 PULL body plus its response headers.

    With NO offset, returns the latest compacted snapshot blob (when the
    document has one) followed by every update batch appended after it. With an
    offset, returns ONLY the update batches appended after that offset and no
    snapshot. The server never diffs, it slices its opaque append log by offset.
    """
    headers: dict[str, str] = {"X-WB-Doc-Sha256": document.content_sha256}
    if since_offset is None:
        segments: list[bytes] = []
        snapshot_sha256 = document.ydoc_snapshot_sha256
        if snapshot_sha256 is not None:
            segments.append(
                ydoc_store.read_snapshot(store, snapshot_sha256=snapshot_sha256)
            )
            headers["X-WB-Snapshot-Sha256"] = snapshot_sha256
        batches, next_offset = ydoc_store.read_updates(
            store, document_id=document.id
        )
        segments.extend(batches)
        headers["X-WB-Next-Offset"] = next_offset
        return frame_segments(segments), headers
    batches, next_offset = ydoc_store.read_updates(
        store, document_id=document.id, since_offset=since_offset
    )
    headers["X-WB-Next-Offset"] = next_offset
    return frame_segments(list(batches)), headers


def push_ydoc(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    *,
    body: bytes,
    base_sha256: str | None = None,
    compacted_snapshot_sha256: str | None = None,
    at: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Apply one R4 PUSH of an opaque update batch (human direct edits only).

    Optimistic concurrency: a supplied base hash that no longer matches the
    server's content hash rejects with 409 stale_base and mutates nothing. On a
    compaction the body frames the batch then the client-compacted snapshot, and
    the server content-addresses and verifies the snapshot, advances the
    snapshot pointer through the engine, and truncates the superseded log. The
    server appends the opaque batch either way and interprets no bytes.
    """
    if base_sha256 is not None and base_sha256.strip().lower() != document.content_sha256:
        return (
            {
                "ok": False,
                "error": "stale_base",
                "server_doc_sha256": document.content_sha256,
            },
            409,
        )
    if compacted_snapshot_sha256 is not None:
        segments = unframe_segments(body)
        if len(segments) != 2:
            raise InvariantViolation(
                "a compacted push must frame the update batch then the snapshot"
            )
        batch, snapshot = segments
    else:
        batch, snapshot = bytes(body), None

    next_offset = ydoc_store.append_update(store, document_id=document.id, update=batch)
    if snapshot is not None:
        # compact() persists and verifies the client snapshot blob and truncates
        # the now-superseded update log, advance_snapshot() moves the durable
        # pointer, prunes the prior blob, and audits the advance.
        digest = ydoc_store.compact(
            store,
            document_id=document.id,
            snapshot=snapshot,
            expected_sha256=compacted_snapshot_sha256,
        )
        documents.advance_snapshot(
            store,
            document_id=document.id,
            ydoc_snapshot_sha256=digest,
            actor=actor,
            at=at,
        )
        _, next_offset = ydoc_store.read_updates(store, document_id=document.id)
    return (
        {
            "ok": True,
            "applied": True,
            "doc_sha256": document.content_sha256,
            "next_offset": next_offset,
        },
        200,
    )


__all__ = [
    "frame_segment",
    "frame_segments",
    "pull_ydoc",
    "push_ydoc",
    "unframe_segments",
]
