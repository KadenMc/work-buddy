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
from work_buddy.cowork.policy import document_surface_allowed
from work_buddy.truth.contracts import Actor, InvariantViolation
from work_buddy.truth.store import DocumentRecord, TruthStore, _valid_digest

# 4-byte big-endian length prefix per opaque segment (matches the update-log
# framing the kernel ydoc_store already uses for its runtime batches).
_LENGTH_PREFIX = struct.Struct(">I")


class OpaqueSegmentTooLarge(InvariantViolation):
    def __init__(self, *, segment_index: int, size: int, limit: int) -> None:
        super().__init__(
            f"opaque segment {segment_index} is {size} bytes; limit is {limit}"
        )
        self.segment_index = segment_index
        self.size = size
        self.limit = limit


def _frame_segment(segment: bytes, *, segment_index: int) -> bytes:
    if not isinstance(segment, (bytes, bytearray, memoryview)):
        raise InvariantViolation("segment must be opaque bytes")
    payload = bytes(segment)
    limit = ydoc_store.MAX_OPAQUE_SEGMENT_BYTES
    if len(payload) > limit:
        raise OpaqueSegmentTooLarge(
            segment_index=segment_index,
            size=len(payload),
            limit=limit,
        )
    return _LENGTH_PREFIX.pack(len(payload)) + payload


def frame_segment(segment: bytes) -> bytes:
    """Return one length-prefixed opaque segment."""
    return _frame_segment(segment, segment_index=0)


def frame_segments(segments: list[bytes]) -> bytes:
    """Frame an ordered list of opaque segments into one body."""
    return b"".join(
        _frame_segment(segment, segment_index=index)
        for index, segment in enumerate(segments)
    )


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
        limit = ydoc_store.MAX_OPAQUE_SEGMENT_BYTES
        if length > limit:
            raise OpaqueSegmentTooLarge(
                segment_index=len(segments),
                size=length,
                limit=limit,
            )
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
    # Snapshot the pointer, epoch/log slice, and structured-head digest under
    # the same lock used by append and compaction. Otherwise a concurrent
    # append can make the header describe bytes absent from this response, or a
    # compaction can pair a stale DocumentRecord pointer with a new epoch.
    with ydoc_store.document_lock(store, document.id):
        current = documents.get_document(store, document.id)
        snapshot_sha256 = current.ydoc_snapshot_sha256
        if snapshot_sha256 is None:
            raise InvariantViolation(
                "registered document has no canonical Y.Doc snapshot; "
                "bootstrap is required"
            )
        snapshot = ydoc_store.read_snapshot(store, snapshot_sha256=snapshot_sha256)
        batches, next_offset, cursor_reset = ydoc_store.read_epoch_updates(
            store,
            document_id=current.id,
            since_cursor=since_offset,
        )
        structured_head = ydoc_store.current_structured_head(
            store,
            document_id=current.id,
            snapshot_sha256=snapshot_sha256,
        )
        ydoc_generation = documents.current_ydoc_generation(store, current.id)
        headers: dict[str, str] = {
            # This document header is the Markdown projection pointer. It never
            # aliases the structured concurrency head.
            "X-WB-Doc-Sha256": current.content_sha256,
            "X-WB-Projection-Sha256": current.content_sha256,
            "X-WB-Ydoc-Head-Sha256": structured_head,
            "X-WB-Ydoc-Generation": ydoc_generation,
            "X-WB-Next-Offset": next_offset,
        }
        full_pull = since_offset is None or cursor_reset
        segments: list[bytes] = []
        if full_pull:
            segments.append(snapshot)
            headers["X-WB-Snapshot-Sha256"] = snapshot_sha256
        if cursor_reset:
            headers["X-WB-Cursor-Reset"] = "1"
        segments.extend(batches)
        return frame_segments(segments), headers


class PushGateError(InvariantViolation):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _size_failure(code: str, error: OpaqueSegmentTooLarge) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "error": {
                "code": code,
                "message": str(error),
                "details": {
                    "size_bytes": error.size,
                    "limit_bytes": error.limit,
                },
            },
        },
        413,
    )


def push_ydoc(
    store: TruthStore,
    document: DocumentRecord,
    actor: Actor,
    *,
    body: bytes,
    base_sha256: str | None = None,
    base_structured_head_sha256: str | None = None,
    base_ydoc_generation: str | None = None,
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
    raw_body = bytes(body)
    if (
        compacted_snapshot_sha256 is None
        and len(raw_body) > ydoc_store.MAX_OPAQUE_SEGMENT_BYTES
    ):
        return _size_failure(
            "update_too_large",
            OpaqueSegmentTooLarge(
                segment_index=0,
                size=len(raw_body),
                limit=ydoc_store.MAX_OPAQUE_SEGMENT_BYTES,
            ),
        )

    expected_generation = _valid_digest(
        base_ydoc_generation, "X-WB-Base-Ydoc-Generation"
    )

    def _guard() -> None:
        current = documents.get_document(store, document.id)
        if documents.current_lifecycle(store, current.id) != "active":
            raise PushGateError(
                "document_retired",
                "This document has been removed from Co-work.",
                409,
            )
        if not document_surface_allowed(store, current):
            raise PushGateError(
                "policy_forbidden",
                "This document is not available in Co-work for this folder.",
                403,
            )
        if documents.current_ydoc_generation(store, current.id) != expected_generation:
            raise PushGateError(
                "ydoc_generation_changed",
                "The structured document was replaced before this update.",
                409,
            )
        if current.ydoc_snapshot_sha256 != document.ydoc_snapshot_sha256:
            raise ydoc_store.StructuredHeadConflict(
                "structured snapshot changed before update"
            )

    try:
        _guard()
    except PushGateError as exc:
        return (
            {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc), "details": {}},
            },
            exc.status,
        )
    snapshot_sha256 = document.ydoc_snapshot_sha256
    if snapshot_sha256 is None:
        raise InvariantViolation(
            "registered document has no canonical Y.Doc snapshot; bootstrap is required"
        )
    expected_head = (base_structured_head_sha256 or "").strip().lower()
    if not expected_head and base_sha256:
        projection_base = base_sha256.strip().lower()
        if projection_base != document.content_sha256:
            server_head = ydoc_store.current_structured_head(
                store,
                document_id=document.id,
                snapshot_sha256=snapshot_sha256,
            )
            return (
                {
                    "ok": False,
                    "error": "stale_base",
                    "server_doc_sha256": document.content_sha256,
                    "server_structured_head_sha256": server_head,
                },
                409,
            )
        # A matching projection precondition is serialized under the document
        # lock by the CAS operation before it adopts the current structured head.
        expected_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=snapshot_sha256,
        )
    if not expected_head:
        raise InvariantViolation("X-WB-Base-Ydoc-Sha256 is required")
    if compacted_snapshot_sha256 is not None:
        try:
            segments = unframe_segments(raw_body)
        except OpaqueSegmentTooLarge as exc:
            if exc.segment_index == 0:
                return _size_failure("update_too_large", exc)
            if exc.segment_index == 1:
                return _size_failure("snapshot_too_large", exc)
            raise InvariantViolation(
                "a compacted push contains an unexpected oversized segment"
            ) from exc
        if len(segments) != 2:
            raise InvariantViolation(
                "a compacted push must frame the update batch then the snapshot"
            )
        batch, snapshot = segments
    else:
        batch, snapshot = raw_body, None

    try:
        if snapshot is not None:
            _, structured_head, next_offset = ydoc_store.compact_and_advance(
                store,
                document_id=document.id,
                snapshot=snapshot,
                expected_snapshot_sha256=compacted_snapshot_sha256,
                expected_structured_head_sha256=expected_head,
                actor=actor,
                at=at,
                lock_guard=_guard,
            )
        else:
            next_offset, structured_head = ydoc_store.append_update_cas(
                store,
                document_id=document.id,
                snapshot_sha256=snapshot_sha256,
                update=batch,
                expected_structured_head_sha256=expected_head,
                lock_guard=_guard,
            )
    except PushGateError as exc:
        return (
            {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc), "details": {}},
            },
            exc.status,
        )
    except ydoc_store.StructuredHeadConflict:
        server_head = ydoc_store.current_structured_head(
            store,
            document_id=document.id,
            snapshot_sha256=documents.get_document(
                store, document.id
            ).ydoc_snapshot_sha256
            or snapshot_sha256,
        )
        return (
            {
                "ok": False,
                "error": "stale_base",
                "server_doc_sha256": document.content_sha256,
                "server_structured_head_sha256": server_head,
            },
            409,
        )
    return (
        {
            "ok": True,
            "applied": True,
            # doc_sha256 is the Markdown projection hash.
            "doc_sha256": document.content_sha256,
            "projection_sha256": document.content_sha256,
            "structured_head_sha256": structured_head,
            "ydoc_head_sha256": structured_head,
            "ydoc_generation": expected_generation,
            "next_offset": next_offset,
        },
        200,
    )


__all__ = [
    "OpaqueSegmentTooLarge",
    "frame_segment",
    "frame_segments",
    "pull_ydoc",
    "push_ydoc",
    "unframe_segments",
]
