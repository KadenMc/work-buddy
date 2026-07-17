"""Opaque Y.Doc snapshot and update-log persistence (C3, PRD section 5).

The single dashboard client is the ONLY Yjs interpreter in v1. Every function
here moves OPAQUE bytes: it content-addresses, appends, slices by offset, and
refcounts, and it NEVER constructs, merges, or diffs Yjs state. The client
computes every compacted snapshot and every update batch and hands them to the
server as bytes plus a declared sha256, which the server verifies by re-hashing
only.

Snapshots are authoritative in blobs/ (durable, content-addressed, exported like
evidence blobs). The incremental update log lives in runtime/ (local, gitignored,
excluded from export): its loss on machine death is documented and acceptable,
because the ledger keeps every decision and the exported snapshot keeps the exact
structured document.
"""

from __future__ import annotations

import struct
from pathlib import Path

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import TruthStore, _valid_digest


_RUNTIME_DIRNAME = "runtime"
_UPDATE_LOG_NAME = "updates.log"
_LENGTH_PREFIX = struct.Struct(">I")  # 4-byte big-endian opaque batch length


def _as_bytes(value: object, label: str) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise InvariantViolation(f"{label} must be opaque bytes")


def _document_ref(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise InvariantViolation("document_id must be a nonempty string")
    ref = document_id.strip().lower()
    # Reuse the record-id shape as a path-safe token so runtime paths cannot
    # traverse outside the runtime directory.
    from work_buddy.truth.store import _valid_record_id

    return _valid_record_id(ref, "document_id")


def runtime_dir(store: TruthStore, document_id: str) -> Path:
    """Return the gitignored per-document runtime directory (opaque update log)."""
    ref = _document_ref(document_id)
    path = store.paths.sidecar / _RUNTIME_DIRNAME / ref
    path.mkdir(parents=True, exist_ok=True)
    return path


def _update_log_path(store: TruthStore, document_id: str) -> Path:
    return runtime_dir(store, document_id) / _UPDATE_LOG_NAME


def append_update(store: TruthStore, *, document_id: str, update: object) -> str:
    """Append one OPAQUE client update batch to the local log, return next offset.

    The batch is never interpreted as Yjs. The returned offset token is opaque:
    a later read_updates(since_offset=token) yields the batches appended after it.
    """
    payload = _as_bytes(update, "update")
    log_path = _update_log_path(store, document_id)
    with open(log_path, "ab") as handle:
        handle.write(_LENGTH_PREFIX.pack(len(payload)))
        handle.write(payload)
        handle.flush()
        offset = handle.tell()
    return str(offset)


def read_updates(
    store: TruthStore,
    *,
    document_id: str,
    since_offset: str | None = None,
) -> tuple[tuple[bytes, ...], str]:
    """Return the opaque update batches appended after since_offset plus the next
    offset token. A byte slice, not a Yjs diff. since_offset None reads from the
    start of the current log (everything after the latest compaction snapshot).
    """
    log_path = _update_log_path(store, document_id)
    if not log_path.is_file():
        return (), "0"
    start = 0
    if since_offset is not None:
        try:
            start = int(since_offset)
        except (TypeError, ValueError) as exc:
            raise InvariantViolation("since_offset must be an opaque offset token") from exc
        if start < 0:
            raise InvariantViolation("since_offset must be a non-negative offset")
    data = log_path.read_bytes()
    if start > len(data):
        raise InvariantViolation("since_offset is past the end of the update log")
    batches: list[bytes] = []
    cursor = start
    header = _LENGTH_PREFIX.size
    while cursor < len(data):
        if cursor + header > len(data):
            raise InvariantViolation("update log is truncated mid-frame")
        (length,) = _LENGTH_PREFIX.unpack(data[cursor : cursor + header])
        cursor += header
        if cursor + length > len(data):
            raise InvariantViolation("update log is truncated mid-batch")
        batches.append(data[cursor : cursor + length])
        cursor += length
    return tuple(batches), str(cursor)


def write_snapshot(
    store: TruthStore,
    *,
    snapshot: object,
    expected_sha256: str | None = None,
) -> str:
    """Content-address a client-compacted snapshot blob into blobs/<sha256>.

    Verifies the blob re-hashes to expected_sha256 when supplied and returns its
    digest (the evidence-blob idiom). Bytes are never parsed.
    """
    payload = _as_bytes(snapshot, "snapshot")
    digest = sha256_bytes(payload)
    if expected_sha256 is not None:
        expected = _valid_digest(expected_sha256, "expected_sha256")
        if expected != digest:
            raise InvariantViolation(
                "snapshot bytes do not match expected_sha256"
            )
    store._store_blob_bytes(digest, payload)
    return digest


def read_snapshot(store: TruthStore, *, snapshot_sha256: str) -> bytes:
    """Read a durable snapshot blob by digest (opaque bytes for Y.applyUpdate)."""
    digest = _valid_digest(snapshot_sha256, "snapshot_sha256")
    path = store.resolve_blob_path(f"blobs/{digest}")
    if not path.is_file():
        raise InvariantViolation(f"snapshot blob does not exist: {digest}")
    data = path.read_bytes()
    if sha256_bytes(data) != digest:
        raise InvariantViolation(f"snapshot blob failed verification: {digest}")
    return data


def compact(
    store: TruthStore,
    *,
    document_id: str,
    snapshot: object,
    expected_sha256: str | None = None,
) -> str:
    """Client-driven compaction: persist the client snapshot and truncate the log.

    Persists the CLIENT-supplied compacted snapshot to blobs/, verifies its
    digest, truncates the superseded runtime update log, and returns the snapshot
    digest for advance_snapshot(). The server does not compute the snapshot.
    """
    digest = write_snapshot(store, snapshot=snapshot, expected_sha256=expected_sha256)
    log_path = _update_log_path(store, document_id)
    # Truncate the now-superseded incremental log. The snapshot subsumes it.
    atomic_write_bytes(log_path, b"")
    return digest


def prune_snapshot_blob(store: TruthStore, *, snapshot_sha256: str) -> bool:
    """Remove a snapshot blob once no documents row references it.

    Follows the evidence-blob refcount discipline: the deletion runs while
    BEGIN IMMEDIATE excludes captures/redactions that could change the refcount,
    and a shared digest (also referenced by evidence) is retained. Returns True
    only when a blob was actually removed.
    """
    digest = _valid_digest(snapshot_sha256, "snapshot_sha256")
    cleanup = store._open_connection()
    removed = False
    try:
        cleanup.execute("BEGIN IMMEDIATE")
        document_refs = int(
            cleanup.execute(
                "SELECT COUNT(*) FROM documents WHERE ydoc_snapshot_sha256 = ?",
                (digest,),
            ).fetchone()[0]
        )
        evidence_refs = int(
            cleanup.execute(
                "SELECT COUNT(*) FROM evidence WHERE content_sha256 = ? "
                "AND content_path IS NOT NULL",
                (digest,),
            ).fetchone()[0]
        )
        if document_refs == 0 and evidence_refs == 0:
            blob = store.resolve_blob_path(f"blobs/{digest}")
            existed = blob.exists()
            blob.unlink(missing_ok=True)
            removed = existed and not blob.exists()
        cleanup.execute("COMMIT")
    except Exception:
        if cleanup.in_transaction:
            cleanup.execute("ROLLBACK")
        raise
    finally:
        cleanup.close()
    return removed


__all__ = [
    "append_update",
    "compact",
    "prune_snapshot_blob",
    "read_snapshot",
    "read_updates",
    "runtime_dir",
    "write_snapshot",
]
