"""Fail-closed recovery fence for the shared Source Foundation.

The machine backup deliberately restores some Source Foundation databases at
a different point in time from retained Sources and scoped Truth stores.  A
successful filesystem swap therefore does *not* prove that those authorities
form one coherent cohort.  ``data_restore`` publishes a durable marker and
the persistence boundaries in this module's callers stay read-only until an
explicit reconciliation operation clears it.

The bypass ContextVar is intentionally private to trusted recovery code.  It
does not weaken the fence for another thread or process and it never permits a
model/provider transport retry.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from work_buddy.artifacts.io import atomic_write_bytes
from work_buddy.utils.index_lock import index_lock


RESTORE_FENCE_SCHEMA = "wb.source-foundation-restore-fence/v1"
RESTORE_FENCE_FILENAME = "source_foundation_restore_pending.json"


class SourceFoundationRestorePending(RuntimeError):
    """A Source Foundation write/dispatch is blocked pending reconciliation."""

    code = "source_foundation_restore_pending"
    retryable = False

    def __init__(self, operation: str, *, reason: str = "reconciliation_required") -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(
            "Source Foundation is read-only until restore reconciliation completes"
        )


@dataclass(frozen=True, slots=True)
class RestoreFence:
    path: Path
    valid: bool
    payload: Mapping[str, Any] | None
    error: str | None

    @property
    def active(self) -> bool:
        return self.path.exists()


_RECOVERY_WRITE_AUTHORIZED: ContextVar[bool] = ContextVar(
    "source_foundation_recovery_write_authorized",
    default=False,
)
_HELD_FENCE_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "source_foundation_restore_fence_locks",
    default=frozenset(),
)


def restore_fence_path() -> Path:
    """Return the canonical machine-level recovery marker path."""

    from work_buddy.paths import data_dir

    return data_dir("") / "db" / RESTORE_FENCE_FILENAME


def read_restore_fence(path: str | Path | None = None) -> RestoreFence:
    """Read a fence without ever interpreting malformed state as cleared."""

    target = restore_fence_path() if path is None else Path(path).expanduser().resolve()
    if not target.exists():
        return RestoreFence(target, True, None, None)
    if not target.is_file():
        return RestoreFence(target, False, None, "restore_fence_is_not_a_file")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return RestoreFence(target, False, None, "restore_fence_is_unreadable")
    if not isinstance(value, dict) or value.get("schema") != RESTORE_FENCE_SCHEMA:
        return RestoreFence(target, False, None, "restore_fence_schema_invalid")
    if not isinstance(value.get("snapshot_id"), str) or not value["snapshot_id"]:
        return RestoreFence(target, False, None, "restore_fence_snapshot_invalid")
    return RestoreFence(target, True, value, None)


@contextmanager
def restore_fence_lock(
    path: str | Path | None = None,
) -> Iterator[Path]:
    """Serialize marker updates/retirement across recovery operators."""

    target = restore_fence_path() if path is None else Path(path).expanduser().resolve()
    key = str(target)
    held = _HELD_FENCE_LOCKS.get()
    if key in held:
        yield target
        return
    lock_target = target.with_name(f".{RESTORE_FENCE_FILENAME}.reconciliation")
    with index_lock(lock_target):
        token = _HELD_FENCE_LOCKS.set(held | {key})
        try:
            yield target
        finally:
            _HELD_FENCE_LOCKS.reset(token)


def restore_fence_active(path: str | Path | None = None) -> bool:
    return read_restore_fence(path).active


def source_foundation_read_only(path: str | Path | None = None) -> bool:
    """Whether this execution context must use read-only store connections."""

    return restore_fence_active(path) and not _RECOVERY_WRITE_AUTHORIZED.get()


def require_source_foundation_writable(
    operation: str,
    *,
    path: str | Path | None = None,
) -> None:
    """Fail before a Source Foundation mutation or irreversible dispatch."""

    if _RECOVERY_WRITE_AUTHORIZED.get():
        return
    fence = read_restore_fence(path)
    if fence.active:
        raise SourceFoundationRestorePending(
            operation,
            reason=fence.error or "reconciliation_required",
        )


@contextmanager
def authorized_restore_reconciliation() -> Iterator[None]:
    """Narrow write bypass for the high-consent reconciliation operator."""

    token = _RECOVERY_WRITE_AUTHORIZED.set(True)
    try:
        yield
    finally:
        _RECOVERY_WRITE_AUTHORIZED.reset(token)


def write_restore_fence(
    payload: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> Path:
    """Atomically publish validated marker state."""

    target = restore_fence_path() if path is None else Path(path).expanduser().resolve()
    value = dict(payload)
    value["schema"] = RESTORE_FENCE_SCHEMA
    if not isinstance(value.get("snapshot_id"), str) or not value["snapshot_id"]:
        raise ValueError("restore fence requires snapshot_id")
    with restore_fence_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            target,
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        )
    return target


__all__ = [
    "RESTORE_FENCE_FILENAME",
    "RESTORE_FENCE_SCHEMA",
    "RestoreFence",
    "SourceFoundationRestorePending",
    "authorized_restore_reconciliation",
    "read_restore_fence",
    "restore_fence_lock",
    "require_source_foundation_writable",
    "restore_fence_active",
    "restore_fence_path",
    "source_foundation_read_only",
    "write_restore_fence",
]
