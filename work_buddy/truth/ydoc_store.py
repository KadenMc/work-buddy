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

import hashlib
import json
import os
import secrets
import struct
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.backups.source_foundation_restore import (
    require_source_foundation_writable,
)
from work_buddy.truth.contracts import InvariantViolation
from work_buddy.truth.identity import sha256_bytes
from work_buddy.truth.store import TruthStore, _valid_digest


_RUNTIME_DIRNAME = "runtime"
_UPDATE_LOG_NAME = "updates.log"
_STATE_NAME = "state.json"
_COMPACTION_MARKER_NAME = "compaction-recovery.json"
_LENGTH_PREFIX = struct.Struct(">I")  # 4-byte big-endian opaque batch length
_HEAD_DOMAIN = b"cowork-yjs-structured-head/v1\x00"
_CURSOR_PREFIX = "cowork-cursor-v1"
MAX_OPAQUE_SEGMENT_BYTES = 64 * 1024 * 1024


class StructuredHeadConflict(InvariantViolation):
    """The caller attempted to append against a stale structured head."""


class CompactionRecoveryRequired(InvariantViolation):
    """A compaction boundary cannot be repaired without human inspection."""


class UpdateLogCorruption(InvariantViolation):
    """The update log contains a malformed complete frame."""


@dataclass(frozen=True, slots=True)
class SnapshotReplacement:
    """Prepared opaque snapshot and durable post-commit log rotation receipt."""

    snapshot_sha256: str
    structured_head_sha256: str
    epoch_cursor: str


@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    """Operational proof that one projection rode one compaction CAS."""

    id: str
    document_id: str
    ydoc_snapshot_sha256: str
    structured_head_sha256: str
    ydoc_generation_sha256: str
    projection_sha256: str


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


def _runtime_dir(store: TruthStore, document_id: str, *, create: bool) -> Path:
    ref = _document_ref(document_id)
    path = store.paths.sidecar / _RUNTIME_DIRNAME / ref
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir(store: TruthStore, document_id: str) -> Path:
    """Create and return the gitignored per-document runtime directory."""

    require_source_foundation_writable("truth.ydoc_runtime_create")
    return _runtime_dir(store, document_id, create=True)


def _update_log_path(
    store: TruthStore,
    document_id: str,
    *,
    create_parent: bool = False,
) -> Path:
    return _runtime_dir(store, document_id, create=create_parent) / _UPDATE_LOG_NAME


def _state_path(store: TruthStore, document_id: str, *, create: bool = False) -> Path:
    return _runtime_dir(store, document_id, create=create) / _STATE_NAME


def _marker_path(store: TruthStore, document_id: str, *, create: bool = False) -> Path:
    return _runtime_dir(store, document_id, create=create) / _COMPACTION_MARKER_NAME


@contextmanager
def document_lock(
    store: TruthStore,
    document_id: str,
    *,
    path_key: str | None = None,
    timeout: float = 10.0,
) -> Iterator[None]:
    """Acquire external-migration -> store -> path? -> document locks."""

    from contextlib import ExitStack

    from work_buddy.truth.locks import (
        document_lock as shared_document_lock,
        path_lock,
        store_lock,
    )

    with ExitStack() as stack:
        stack.enter_context(store.migration_write_lock(timeout=timeout))
        stack.enter_context(store_lock(store.paths.sidecar, timeout=timeout))
        if path_key is not None:
            stack.enter_context(
                path_lock(store.paths.sidecar, path_key, timeout=timeout)
            )
        stack.enter_context(
            shared_document_lock(
                store.paths.sidecar,
                document_id,
                timeout=timeout,
            )
        )
        yield


def structured_head_from_segments(
    snapshot: object,
    updates: tuple[bytes, ...] | list[bytes],
) -> str:
    """Hash canonical domain-separated framing of snapshot plus update batches."""

    snapshot_bytes = _as_bytes(snapshot, "snapshot")
    digest = hashlib.sha256()
    digest.update(_HEAD_DOMAIN)
    digest.update(_LENGTH_PREFIX.pack(len(snapshot_bytes)))
    digest.update(snapshot_bytes)
    for update in updates:
        payload = _as_bytes(update, "update")
        digest.update(_LENGTH_PREFIX.pack(len(payload)))
        digest.update(payload)
    return digest.hexdigest()


def _read_runtime_state(store: TruthStore, document_id: str) -> dict[str, object]:
    state_path = _state_path(store, document_id)
    if not state_path.is_file():
        return {"schema": "cowork-cursor/v1", "epoch": "0"}
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvariantViolation("Y.Doc runtime cursor state is corrupt") from exc
    if not isinstance(value, dict):
        raise InvariantViolation("Y.Doc runtime cursor state is invalid")
    epoch = value.get("epoch")
    if not isinstance(epoch, str) or not epoch:
        raise InvariantViolation("Y.Doc runtime cursor epoch is invalid")
    return value


def _read_epoch(store: TruthStore, document_id: str) -> str:
    return str(_read_runtime_state(store, document_id)["epoch"])


def _projection_receipt_payload(receipt: ProjectionReceipt) -> dict[str, str]:
    return {
        "schema": "cowork-projection-receipt/v1",
        "id": receipt.id,
        "document_id": receipt.document_id,
        "ydoc_snapshot_sha256": receipt.ydoc_snapshot_sha256,
        "structured_head_sha256": receipt.structured_head_sha256,
        "ydoc_generation_sha256": receipt.ydoc_generation_sha256,
        "projection_sha256": receipt.projection_sha256,
    }


def _parse_projection_receipt(value: object) -> ProjectionReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") != (
        "cowork-projection-receipt/v1"
    ):
        raise InvariantViolation("Y.Doc projection receipt is invalid")
    document_id = _document_ref(value.get("document_id"))
    return ProjectionReceipt(
        id=_valid_digest(value.get("id"), "projection receipt id"),
        document_id=document_id,
        ydoc_snapshot_sha256=_valid_digest(
            value.get("ydoc_snapshot_sha256"),
            "projection receipt snapshot",
        ),
        structured_head_sha256=_valid_digest(
            value.get("structured_head_sha256"),
            "projection receipt structured head",
        ),
        ydoc_generation_sha256=_valid_digest(
            value.get("ydoc_generation_sha256"),
            "projection receipt Y.Doc generation",
        ),
        projection_sha256=_valid_digest(
            value.get("projection_sha256"),
            "projection receipt projection",
        ),
    )


def _write_epoch(
    store: TruthStore,
    document_id: str,
    epoch: str,
    *,
    projection_receipt: ProjectionReceipt | None = None,
) -> None:
    state: dict[str, object] = {
        "schema": "cowork-cursor/v2",
        "epoch": epoch,
    }
    if projection_receipt is not None:
        state["projection_receipt"] = _projection_receipt_payload(
            projection_receipt
        )
    atomic_write_bytes(
        _state_path(store, document_id, create=True),
        json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def current_projection_receipt(
    store: TruthStore,
    *,
    document_id: str,
) -> ProjectionReceipt | None:
    """Return the projection checkpoint published by the latest compaction."""

    state = _read_runtime_state(store, document_id)
    return _parse_projection_receipt(state.get("projection_receipt"))


def projection_receipt_matches(
    store: TruthStore,
    *,
    receipt_id: str,
    document_id: str,
    ydoc_snapshot_sha256: str,
    structured_head_sha256: str,
    ydoc_generation_sha256: str,
    projection_sha256: str,
) -> bool:
    """Validate the complete tuple admitted by the latest compaction CAS."""

    receipt = current_projection_receipt(store, document_id=document_id)
    if receipt is None:
        return False
    return receipt == ProjectionReceipt(
        id=_valid_digest(receipt_id, "projection receipt id"),
        document_id=_document_ref(document_id),
        ydoc_snapshot_sha256=_valid_digest(
            ydoc_snapshot_sha256, "projection receipt snapshot"
        ),
        structured_head_sha256=_valid_digest(
            structured_head_sha256, "projection receipt structured head"
        ),
        ydoc_generation_sha256=_valid_digest(
            ydoc_generation_sha256, "projection receipt Y.Doc generation"
        ),
        projection_sha256=_valid_digest(
            projection_sha256, "projection receipt projection"
        ),
    )


def encode_cursor(epoch: str, offset: int) -> str:
    return f"{_CURSOR_PREFIX}:{epoch}:{offset}"


def decode_cursor(cursor: str) -> tuple[str, int]:
    if not isinstance(cursor, str):
        raise InvariantViolation("cursor must be an opaque text token")
    parts = cursor.split(":")
    if len(parts) != 3 or parts[0] != _CURSOR_PREFIX or not parts[1]:
        raise InvariantViolation("cursor is not a supported opaque token")
    try:
        offset = int(parts[2])
    except ValueError as exc:
        raise InvariantViolation("cursor offset is invalid") from exc
    if offset < 0:
        raise InvariantViolation("cursor offset is invalid")
    return parts[1], offset


def _append_update_unlocked(
    store: TruthStore,
    *,
    document_id: str,
    update: object,
) -> str:
    payload = _as_bytes(update, "update")
    if len(payload) > MAX_OPAQUE_SEGMENT_BYTES:
        raise InvariantViolation("opaque Y.Doc update exceeds the segment size limit")
    # Invalidate the recovery projection before making an unrepresented tail
    # durable. If the process dies at either boundary, the safe outcomes are an
    # export without the update (append never happened) or no export at all;
    # there is never a published artifact falsely claiming to be current.
    try:
        store.paths.claims_export.unlink(missing_ok=True)
    except OSError as exc:
        raise InvariantViolation(
            "the stale recovery export could not be invalidated before Y.Doc append"
        ) from exc
    # A tail changes the structured head without a corresponding canonical
    # Markdown projection. Invalidate any prior projection receipt before the
    # update becomes durable; a failed append may cause a harmless recapture.
    _write_epoch(store, document_id, _read_epoch(store, document_id))
    log_path = _update_log_path(store, document_id, create_parent=True)
    with open(log_path, "ab") as handle:
        handle.write(_LENGTH_PREFIX.pack(len(payload)))
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        offset = handle.tell()
    return str(offset)


def append_update(store: TruthStore, *, document_id: str, update: object) -> str:
    """Append one OPAQUE client update batch to the local log, return next offset.

    The batch is never interpreted as Yjs. The returned offset token is opaque:
    a later read_updates(since_offset=token) yields the batches appended after it.
    """
    require_source_foundation_writable("truth.ydoc_append")
    with document_lock(store, document_id):
        repair_update_log_locked(store, document_id=document_id)
        return _append_update_unlocked(
            store, document_id=document_id, update=update
        )


def _scan_update_log(
    data: bytes,
) -> tuple[tuple[bytes, ...], int, bool]:
    """Parse complete frames and identify only an incomplete final append."""

    batches: list[bytes] = []
    cursor = 0
    header = _LENGTH_PREFIX.size
    while cursor < len(data):
        frame_start = cursor
        if cursor + header > len(data):
            return tuple(batches), frame_start, True
        (length,) = _LENGTH_PREFIX.unpack(data[cursor : cursor + header])
        if length > MAX_OPAQUE_SEGMENT_BYTES:
            raise UpdateLogCorruption(
                "update log contains an oversized or malformed frame"
            )
        cursor += header
        if cursor + length > len(data):
            return tuple(batches), frame_start, True
        batches.append(data[cursor : cursor + length])
        cursor += length
    return tuple(batches), cursor, False


def repair_update_log_locked(store: TruthStore, *, document_id: str) -> bool:
    """Truncate a provably incomplete final frame while retaining its prefix.

    The caller must hold ``document_lock``. Complete malformed frames remain a
    hard error; only a short final header or payload is discarded.
    """

    require_source_foundation_writable("truth.ydoc_repair")
    log_path = _update_log_path(store, document_id)
    if not log_path.is_file():
        return False
    data = log_path.read_bytes()
    _batches, boundary, incomplete = _scan_update_log(data)
    if not incomplete:
        return False
    with open(log_path, "r+b") as handle:
        handle.truncate(boundary)
        handle.flush()
        os.fsync(handle.fileno())
    return True


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
    batches, boundary, incomplete = _scan_update_log(data[start:])
    if incomplete:
        raise InvariantViolation("update log has an incomplete final frame")
    return batches, str(start + boundary)


def update_tail_present(store: TruthStore, *, document_id: str) -> bool:
    log_path = _update_log_path(store, document_id)
    return log_path.is_file() and log_path.stat().st_size > 0


def current_structured_head(
    store: TruthStore,
    *,
    document_id: str,
    snapshot_sha256: str,
) -> str:
    if _marker_path(store, document_id).is_file():
        raise CompactionRecoveryRequired(
            "Y.Doc compaction recovery must finish before reading the head"
        )
    snapshot = read_snapshot(store, snapshot_sha256=snapshot_sha256)
    updates, _ = read_updates(store, document_id=document_id)
    return structured_head_from_segments(snapshot, updates)


def read_epoch_updates(
    store: TruthStore,
    *,
    document_id: str,
    since_cursor: str | None = None,
) -> tuple[tuple[bytes, ...], str, bool]:
    """Read an epoch-scoped tail, resetting stale/legacy cursor tokens safely."""

    epoch = _read_epoch(store, document_id)
    cursor_reset = False
    offset: str | None = None
    if since_cursor is not None:
        try:
            cursor_epoch, cursor_offset = decode_cursor(since_cursor)
        except InvariantViolation:
            # Numeric v1 offsets cannot be safely applied after a compaction.
            cursor_epoch, cursor_offset = "", 0
            cursor_reset = True
        if cursor_epoch == epoch and not cursor_reset:
            offset = str(cursor_offset)
        else:
            cursor_reset = True
    batches, next_offset = read_updates(
        store,
        document_id=document_id,
        since_offset=offset,
    )
    return batches, encode_cursor(epoch, int(next_offset)), cursor_reset


def append_update_cas(
    store: TruthStore,
    *,
    document_id: str,
    snapshot_sha256: str,
    update: object,
    expected_structured_head_sha256: str,
    lock_guard: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Append one opaque update iff the structured head still matches."""

    require_source_foundation_writable("truth.ydoc_append")
    expected = _valid_digest(
        expected_structured_head_sha256, "expected_structured_head_sha256"
    )
    with document_lock(store, document_id):
        if lock_guard is not None:
            lock_guard()
        repair_update_log_locked(store, document_id=document_id)
        live = current_structured_head(
            store,
            document_id=document_id,
            snapshot_sha256=snapshot_sha256,
        )
        if live != expected:
            raise StructuredHeadConflict(
                f"stale structured head; server head is {live}"
            )
        offset = _append_update_unlocked(
            store,
            document_id=document_id,
            update=update,
        )
        new_head = current_structured_head(
            store,
            document_id=document_id,
            snapshot_sha256=snapshot_sha256,
        )
        return encode_cursor(_read_epoch(store, document_id), int(offset)), new_head


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
    require_source_foundation_writable("truth.ydoc_snapshot")
    payload = _as_bytes(snapshot, "snapshot")
    if len(payload) > MAX_OPAQUE_SEGMENT_BYTES:
        raise InvariantViolation("opaque Y.Doc snapshot exceeds the segment size limit")
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
    require_source_foundation_writable("truth.ydoc_compact")
    with document_lock(store, document_id):
        repair_update_log_locked(store, document_id=document_id)
        digest = write_snapshot(
            store, snapshot=snapshot, expected_sha256=expected_sha256
        )
        log_path = _update_log_path(store, document_id, create_parent=True)
        # Compatibility-only filesystem primitive. New transport uses
        # compact_and_advance so pointer/version commit precedes log rotation.
        atomic_write_bytes(log_path, b"")
        return digest


def _read_marker(store: TruthStore, document_id: str) -> dict[str, object] | None:
    path = _marker_path(store, document_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompactionRecoveryRequired("compaction marker is corrupt") from exc
    if not isinstance(value, dict):
        raise CompactionRecoveryRequired("compaction marker is invalid")
    return value


def _finish_compaction_unlocked(
    store: TruthStore,
    document_id: str,
    marker: dict[str, object],
) -> None:
    new_epoch = marker.get("new_epoch")
    if not isinstance(new_epoch, str) or not new_epoch:
        raise CompactionRecoveryRequired("compaction marker has no target epoch")
    try:
        recorded_log_sha256 = _valid_digest(
            marker.get("old_log_sha256"), "compaction update log"
        )
        projection_receipt = _parse_projection_receipt(
            marker.get("projection_receipt")
        )
    except InvariantViolation as exc:
        raise CompactionRecoveryRequired(
            "compaction marker contains invalid recovery state"
        ) from exc
    if (
        projection_receipt is not None
        and projection_receipt.document_id != _document_ref(document_id)
    ):
        raise CompactionRecoveryRequired(
            "compaction projection receipt belongs to another document"
        )
    old_log = _update_log_path(store, document_id)
    data = old_log.read_bytes() if old_log.is_file() else b""
    actual_log_sha256 = sha256_bytes(data)
    if (
        actual_log_sha256 != recorded_log_sha256
        and actual_log_sha256 != sha256_bytes(b"")
    ):
        raise CompactionRecoveryRequired(
            "update log changed across the compaction recovery boundary"
        )
    # The finish sequence is intentionally idempotent. A process may die after
    # the old log is atomically emptied or after the epoch/receipt is published
    # but before the marker is removed. Recovery can safely repeat both writes
    # because every caller has already proved that the durable document pointer
    # names this marker's target snapshot.
    atomic_write_bytes(
        _update_log_path(store, document_id, create_parent=True),
        b"",
    )
    _write_epoch(
        store,
        document_id,
        new_epoch,
        projection_receipt=projection_receipt,
    )
    _marker_path(store, document_id).unlink(missing_ok=True)


def prepare_snapshot_replacement_locked(
    store: TruthStore,
    *,
    document_id: str,
    snapshot: object,
    expected_new_snapshot_sha256: str,
    expected_current_snapshot_sha256: str,
    expected_current_structured_head_sha256: str,
    projection_sha256: str | None = None,
) -> SnapshotReplacement:
    """Stage a complete replacement snapshot while the caller holds the doc lock.

    The marker makes the SQLite-pointer/opaque-update-log boundary recoverable.
    No canonical pointer changes here; callers must either commit that pointer and
    call ``finish_snapshot_replacement_locked`` or roll back and call ``abort``.
    """

    require_source_foundation_writable("truth.ydoc_snapshot_replace")
    from work_buddy.truth import documents

    repair_update_log_locked(store, document_id=document_id)
    if _read_marker(store, document_id) is not None:
        raise CompactionRecoveryRequired(
            "an earlier snapshot replacement requires recovery"
        )
    expected_snapshot = _valid_digest(
        expected_current_snapshot_sha256, "expected_current_snapshot_sha256"
    )
    expected_head = _valid_digest(
        expected_current_structured_head_sha256,
        "expected_current_structured_head_sha256",
    )
    projection_digest = (
        None
        if projection_sha256 is None
        else _valid_digest(projection_sha256, "projection_sha256")
    )
    document = documents.get_document(store, document_id)
    if document.ydoc_snapshot_sha256 != expected_snapshot:
        raise StructuredHeadConflict("the current Y.Doc snapshot changed")
    live_head = current_structured_head(
        store,
        document_id=document_id,
        snapshot_sha256=expected_snapshot,
    )
    if live_head != expected_head:
        raise StructuredHeadConflict(
            f"stale structured head; server head is {live_head}"
        )

    digest = write_snapshot(
        store,
        snapshot=snapshot,
        expected_sha256=expected_new_snapshot_sha256,
    )
    new_snapshot = read_snapshot(store, snapshot_sha256=digest)
    new_head = structured_head_from_segments(new_snapshot, ())
    projection_receipt = (
        None
        if projection_digest is None
        else ProjectionReceipt(
            id=secrets.token_hex(32),
            document_id=_document_ref(document_id),
            ydoc_snapshot_sha256=digest,
            structured_head_sha256=new_head,
            ydoc_generation_sha256=documents.current_ydoc_generation(
                store, document_id
            ),
            projection_sha256=projection_digest,
        )
    )
    old_log_path = _update_log_path(store, document_id)
    old_log = old_log_path.read_bytes() if old_log_path.is_file() else b""
    new_epoch = secrets.token_hex(16)
    marker = {
        "schema": "cowork-compaction-recovery/v1",
        "target_snapshot_sha256": digest,
        "old_log_sha256": sha256_bytes(old_log),
        "old_epoch": _read_epoch(store, document_id),
        "new_epoch": new_epoch,
    }
    if projection_receipt is not None:
        marker["projection_receipt"] = _projection_receipt_payload(
            projection_receipt
        )
    atomic_write_bytes(
        _marker_path(store, document_id, create=True),
        json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    return SnapshotReplacement(
        snapshot_sha256=digest,
        structured_head_sha256=new_head,
        epoch_cursor=encode_cursor(new_epoch, 0),
    )


def finish_snapshot_replacement_locked(
    store: TruthStore,
    *,
    document_id: str,
    expected_snapshot_sha256: str,
) -> None:
    """Finish the prepared log rotation after its pointer transaction commits."""

    require_source_foundation_writable("truth.ydoc_snapshot_finish")
    from work_buddy.truth import documents

    expected = _valid_digest(expected_snapshot_sha256, "expected_snapshot_sha256")
    marker = _read_marker(store, document_id)
    if marker is None or marker.get("target_snapshot_sha256") != expected:
        raise CompactionRecoveryRequired(
            "snapshot replacement marker is missing or stale"
        )
    document = documents.get_document(store, document_id)
    if document.ydoc_snapshot_sha256 != expected:
        raise CompactionRecoveryRequired(
            "snapshot pointer did not commit before log rotation"
        )
    _finish_compaction_unlocked(store, document_id, marker)


def abort_snapshot_replacement_locked(
    store: TruthStore,
    *,
    document_id: str,
    expected_snapshot_sha256: str,
) -> None:
    """Remove an uncommitted marker while retaining its harmless content blob."""

    require_source_foundation_writable("truth.ydoc_snapshot_abort")
    from work_buddy.truth import documents

    expected = _valid_digest(expected_snapshot_sha256, "expected_snapshot_sha256")
    marker = _read_marker(store, document_id)
    if marker is None:
        return
    if marker.get("target_snapshot_sha256") != expected:
        raise CompactionRecoveryRequired(
            "another snapshot replacement owns the marker"
        )
    if documents.get_document(store, document_id).ydoc_snapshot_sha256 == expected:
        raise CompactionRecoveryRequired(
            "committed snapshot replacement requires recovery"
        )
    _marker_path(store, document_id).unlink(missing_ok=True)


def recover_compaction_locked(store: TruthStore, *, document_id: str) -> bool:
    """Recover one compaction while the caller holds ``document_lock``."""

    require_source_foundation_writable("truth.ydoc_compaction_recover")
    marker = _read_marker(store, document_id)
    if marker is None:
        return False
    from work_buddy.truth import documents

    document = documents.get_document(store, document_id)
    target = marker.get("target_snapshot_sha256")
    if document.ydoc_snapshot_sha256 == target:
        _finish_compaction_unlocked(store, document_id, marker)
        store._run_on_commit()
    else:
        # The database transaction never committed. The old log remains
        # authoritative and the content-addressed target blob is harmless.
        _marker_path(store, document_id).unlink(missing_ok=True)
    return True


def compaction_recovery_pending(store: TruthStore, *, document_id: str) -> bool:
    """Report whether a document has a restart-visible compaction marker."""

    return _marker_path(store, document_id).is_file()


def recover_compaction(store: TruthStore, *, document_id: str) -> bool:
    """Deterministically finish or roll back one interrupted compaction."""

    require_source_foundation_writable("truth.ydoc_compaction_recover")
    with document_lock(store, document_id):
        return recover_compaction_locked(store, document_id=document_id)


def compact_and_advance(
    store: TruthStore,
    *,
    document_id: str,
    snapshot: object,
    expected_snapshot_sha256: str,
    expected_structured_head_sha256: str,
    actor: object,
    at: str | None = None,
    lock_guard: Callable[[], None] | None = None,
    projection_sha256: str | None = None,
) -> tuple[str, str, str, ProjectionReceipt | None]:
    """Commit a compacted snapshot/version, then rotate its included tail.

    Returns ``(snapshot_sha256, structured_head_sha256, epoch_cursor,
    projection_receipt)``. A receipt exists only when the caller supplied a
    server-verified projection digest for this compaction.
    A durable marker closes every crash boundary between the SQLite commit and
    update-log rotation.
    """

    require_source_foundation_writable("truth.ydoc_compact_advance")
    from work_buddy.cowork.paths import resolve_document_source_path
    from work_buddy.truth import documents
    from work_buddy.truth.contracts import Actor

    if not isinstance(actor, Actor):
        raise InvariantViolation("actor must be a durable Actor")
    expected_head = _valid_digest(
        expected_structured_head_sha256, "expected_structured_head_sha256"
    )
    projection_digest = (
        None
        if projection_sha256 is None
        else _valid_digest(projection_sha256, "projection_sha256")
    )
    with document_lock(store, document_id):
        if lock_guard is not None:
            lock_guard()
        repair_update_log_locked(store, document_id=document_id)
        if _read_marker(store, document_id) is not None:
            raise CompactionRecoveryRequired(
                "an earlier compaction requires recovery"
            )
        document = documents.get_document(store, document_id)
        if document.ydoc_snapshot_sha256 is None:
            raise InvariantViolation("document requires bootstrap before compaction")
        live_head = current_structured_head(
            store,
            document_id=document_id,
            snapshot_sha256=document.ydoc_snapshot_sha256,
        )
        if live_head != expected_head:
            raise StructuredHeadConflict(
                f"stale structured head; server head is {live_head}"
            )
        digest = write_snapshot(
            store,
            snapshot=snapshot,
            expected_sha256=expected_snapshot_sha256,
        )
        new_snapshot = read_snapshot(store, snapshot_sha256=digest)
        compacted_head = structured_head_from_segments(new_snapshot, ())
        projection_receipt = (
            None
            if projection_digest is None
            else ProjectionReceipt(
                id=secrets.token_hex(32),
                document_id=_document_ref(document_id),
                ydoc_snapshot_sha256=digest,
                structured_head_sha256=compacted_head,
                ydoc_generation_sha256=documents.current_ydoc_generation(
                    store, document_id
                ),
                projection_sha256=projection_digest,
            )
        )

        projection_blob = store.resolve_blob_path(f"blobs/{document.content_sha256}")
        if not projection_blob.is_file():
            resolved = resolve_document_source_path(store, document)
            if resolved.path.is_file():
                current_bytes = resolved.path.read_bytes()
                if sha256_bytes(current_bytes) == document.content_sha256:
                    store._store_blob_bytes(document.content_sha256, current_bytes)
        if not projection_blob.is_file():
            raise InvariantViolation(
                "projection baseline is unavailable; compact after repairing it"
            )

        log_path = _update_log_path(store, document_id)
        old_log = log_path.read_bytes() if log_path.is_file() else b""
        new_epoch = secrets.token_hex(16)
        marker = {
            "schema": "cowork-compaction-recovery/v1",
            "target_snapshot_sha256": digest,
            "old_log_sha256": sha256_bytes(old_log),
            "old_epoch": _read_epoch(store, document_id),
            "new_epoch": new_epoch,
        }
        if projection_receipt is not None:
            marker["projection_receipt"] = _projection_receipt_payload(
                projection_receipt
            )
        atomic_write_bytes(
            _marker_path(store, document_id, create=True),
            json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

        conn = store.connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            documents.commit_document_version(
                store,
                document_id=document_id,
                kind="snapshot_compacted",
                projection_sha256=document.content_sha256,
                ydoc_snapshot_sha256=digest,
                structured_head_sha256=compacted_head,
                actor=actor,
                at=at,
                detail="ydoc_snapshot_advance",
                conn=conn,
            )
            conn.execute("COMMIT")
            committed = True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            _marker_path(store, document_id).unlink(missing_ok=True)
            raise
        finally:
            conn.close()
        if committed:
            _finish_compaction_unlocked(store, document_id, marker)
            store._run_on_commit()
        return (
            digest,
            compacted_head,
            encode_cursor(new_epoch, 0),
            projection_receipt,
        )


def prune_snapshot_blob(store: TruthStore, *, snapshot_sha256: str) -> bool:
    """Remove a content-addressed blob once no durable row references it.

    Follows the store-wide blob refcount discipline: the deletion runs while
    BEGIN IMMEDIATE excludes captures/redactions that could change the
    refcount. Shared evidence, document/version, action-snapshot, and retained
    import-source references all keep the blob live. Returns True only when a
    blob was actually removed.
    """
    require_source_foundation_writable("truth.ydoc_snapshot_prune")
    digest = _valid_digest(snapshot_sha256, "snapshot_sha256")
    cleanup = store._open_connection()
    removed = False
    try:
        cleanup.execute("BEGIN IMMEDIATE")
        references = store.blob_reference_count(
            digest,
            live_only=False,
            conn=cleanup,
        )
        if references == 0:
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
    "CompactionRecoveryRequired",
    "compaction_recovery_pending",
    "MAX_OPAQUE_SEGMENT_BYTES",
    "ProjectionReceipt",
    "SnapshotReplacement",
    "StructuredHeadConflict",
    "UpdateLogCorruption",
    "abort_snapshot_replacement_locked",
    "append_update",
    "append_update_cas",
    "compact",
    "compact_and_advance",
    "current_structured_head",
    "current_projection_receipt",
    "decode_cursor",
    "document_lock",
    "encode_cursor",
    "finish_snapshot_replacement_locked",
    "prune_snapshot_blob",
    "prepare_snapshot_replacement_locked",
    "projection_receipt_matches",
    "read_epoch_updates",
    "recover_compaction_locked",
    "read_snapshot",
    "read_updates",
    "repair_update_log_locked",
    "recover_compaction",
    "runtime_dir",
    "structured_head_from_segments",
    "update_tail_present",
    "write_snapshot",
]
