"""Cross-process serialization for Co-work document lifecycle boundaries.

Conversation state lives in the house conversations database while document
lifecycle state lives in a per-folder Truth database.  SQLite cannot make one
transaction atomic across those independently owned stores.  Operations that
cross that boundary therefore acquire this lock *before* opening either
database:

    document lifecycle lock -> Truth database -> conversations database

Each nested database transaction is completed before the next database is
opened.  The lock is process-wide, cross-process, and re-entrant within one
request context so domain functions can enforce the same boundary even when an
HTTP adapter already holds it through a larger operation.
"""

from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from work_buddy.paths import data_dir
from work_buddy.utils.index_lock import index_lock


DEFAULT_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 30.0
_HELD_DOCUMENT_LIFECYCLE_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "cowork_held_document_lifecycle_locks",
    default=frozenset(),
)


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def document_lifecycle_lock_target(
    store_id: str,
    document_id: str,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Return the stable machine-local lock target for one document."""

    store_ref = _require_identifier(store_id, "store_id")
    document_ref = _require_identifier(document_id, "document_id")
    if data_root is None:
        root = data_dir("runtime/cowork-document-lifecycle-locks")
    else:
        root = (
            Path(data_root).expanduser().resolve()
            / "runtime"
            / "cowork-document-lifecycle-locks"
        )
        root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        f"{store_ref}\0{document_ref}".encode("utf-8")
    ).hexdigest()
    return root / digest


@contextlib.contextmanager
def document_lifecycle_lock(
    store_id: str,
    document_id: str,
    *,
    data_root: str | Path | None = None,
    timeout: float = DEFAULT_LIFECYCLE_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize lifecycle checks and cross-store effects for one document."""

    target = document_lifecycle_lock_target(
        store_id,
        document_id,
        data_root=data_root,
    )
    key = str(target.resolve())
    held = _HELD_DOCUMENT_LIFECYCLE_LOCKS.get()
    if key in held:
        yield
        return
    with index_lock(target, timeout=timeout):
        token = _HELD_DOCUMENT_LIFECYCLE_LOCKS.set(held | {key})
        try:
            yield
        finally:
            _HELD_DOCUMENT_LIFECYCLE_LOCKS.reset(token)


__all__ = [
    "DEFAULT_LIFECYCLE_LOCK_TIMEOUT_SECONDS",
    "document_lifecycle_lock",
    "document_lifecycle_lock_target",
]
