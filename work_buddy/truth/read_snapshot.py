"""Optimistic read barrier for a process that cannot create writer lockfiles."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

from work_buddy.truth.contracts import InvariantViolation


class ReadSnapshotBusy(InvariantViolation):
    """A writer overlapped a read; discard the assembled response and retry."""


def _metadata(path: Path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def document_read_snapshot(store, document_id: str):
    """Verify one stable DB/runtime view without acquiring a writable lock.

    All document writers hold the store/document or lifecycle marker while
    changing the SQLite pointer or append log. Refuse an existing or new marker,
    changed runtime metadata, or any SQLite commit observed on one continuously
    open connection. The caller must finish assembling its response inside this
    context; an overlap discards that response instead of publishing mixed bytes.
    """
    from work_buddy.paths import _data_base
    from work_buddy.truth.locks import folder_path_key
    from work_buddy.utils.index_lock import is_locked

    runtime = store.paths.sidecar / "runtime"
    try:
        path_key = folder_path_key(store.paths.root)
    except (OSError, ValueError) as exc:
        raise ReadSnapshotBusy("Folder is moving or unavailable; retry the read-only snapshot.") from exc
    folder_digest = hashlib.sha256(f"{path_key}:{store.store_id}".encode("ascii")).hexdigest()
    lifecycle_digest = hashlib.sha256(f"{store.store_id}\0{document_id}".encode()).hexdigest()
    document_digest = hashlib.sha256(document_id.encode()).hexdigest()
    data_root = _data_base()
    markers = [
        data_root / "runtime/cowork-folder-locks" / "by-store" / folder_digest,
        data_root / "runtime/cowork-document-lifecycle-locks" / lifecycle_digest,
        runtime / "locks" / "store",
        runtime / "locks" / "documents" / document_digest,
    ]
    def observe():
        if any(is_locked(path) for path in markers):
            raise ReadSnapshotBusy("Document is changing; retry the read-only snapshot.")
        document_runtime = runtime / document_id
        return {str(path): _metadata(path) for path in (
            store.paths.root, store.paths.db, document_runtime,
            document_runtime / "state.json", document_runtime / "updates.log",
            document_runtime / "compaction-recovery.json",
        )}

    with store._read_connection() as connection:
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        before = observe()
        yield
        after = observe()
        if before != after or connection.execute("PRAGMA data_version").fetchone()[0] != version:
            raise ReadSnapshotBusy("Document changed during the read-only snapshot; retry.")
