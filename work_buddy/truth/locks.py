"""Ordered cross-process locks for Folder and Truth/Co-work mutations."""

from __future__ import annotations

import contextlib
import hashlib
import os
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from work_buddy.utils.index_lock import index_lock


DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
_HELD_MIGRATION_STORE_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "truth_held_external_migration_store_locks",
    default=frozenset(),
)


def canonical_folder_path(folder: str | Path) -> Path:
    """Return one canonical, case-normalized host Folder path."""

    resolved = Path(folder).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"Folder is not a directory: {resolved}")
    return Path(os.path.normcase(str(resolved)))


def folder_path_key(folder: str | Path) -> str:
    """Stable machine-local key used for exact-Folder locks."""

    value = str(canonical_folder_path(folder)).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def folder_lock_root(data_root: str | Path | None = None) -> Path:
    if data_root is None:
        from work_buddy.paths import data_dir

        return data_dir("runtime/cowork-folder-locks")
    root = Path(data_root).expanduser().resolve() / "runtime" / "cowork-folder-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextlib.contextmanager
def hierarchy_lock(
    *,
    data_root: str | Path | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize the rare publication of parent/child Folder authorities."""

    root = folder_lock_root(data_root)
    with index_lock(root / "hierarchy", timeout=timeout):
        yield


@contextlib.contextmanager
def exact_folder_lock(
    folder: str | Path,
    *,
    data_root: str | Path | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lock one canonical host Folder without writing inside that Folder."""

    root = folder_lock_root(data_root)
    target = root / "by-path" / folder_path_key(folder)
    with index_lock(target, timeout=timeout):
        yield


@contextlib.contextmanager
def folder_operation_locks(
    folder: str | Path,
    *,
    data_root: str | Path | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Acquire the required hierarchy -> exact-Folder lock order."""

    with ExitStack() as stack:
        stack.enter_context(hierarchy_lock(data_root=data_root, timeout=timeout))
        stack.enter_context(
            exact_folder_lock(folder, data_root=data_root, timeout=timeout)
        )
        yield


@contextlib.contextmanager
def store_lock(
    sidecar: str | Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lock one established store through its component-local runtime area."""

    target = Path(sidecar).expanduser().resolve() / "runtime" / "locks" / "store"
    with index_lock(target, timeout=timeout):
        yield


@contextlib.contextmanager
def migration_store_lock(
    folder: str | Path,
    store_id: str,
    *,
    data_root: str | Path | None = None,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lock a store being moved without placing the lock inside the moved tree."""

    if not isinstance(store_id, str) or not store_id:
        raise ValueError("store_id is required")
    root = folder_lock_root(data_root)
    target = root / "by-store" / hashlib.sha256(
        f"{folder_path_key(folder)}:{store_id}".encode("ascii")
    ).hexdigest()
    target_key = os.path.normcase(str(target.resolve()))
    held = _HELD_MIGRATION_STORE_LOCKS.get()
    if target_key in held:
        yield
        return
    with index_lock(target, timeout=timeout):
        token = _HELD_MIGRATION_STORE_LOCKS.set(held | {target_key})
        try:
            yield
        finally:
            _HELD_MIGRATION_STORE_LOCKS.reset(token)


@contextlib.contextmanager
def path_lock(
    sidecar: str | Path,
    path_key: str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lock one normalized document path inside an established store."""

    digest = hashlib.sha256(path_key.encode("utf-8")).hexdigest()
    target = Path(sidecar).expanduser().resolve() / "runtime" / "locks" / "paths" / digest
    with index_lock(target, timeout=timeout):
        yield


@contextlib.contextmanager
def document_lock(
    sidecar: str | Path,
    document_id: str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Lock one document's structured-head compare/append/compact sequence."""

    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    target = (
        Path(sidecar).expanduser().resolve()
        / "runtime"
        / "locks"
        / "documents"
        / digest
    )
    with index_lock(target, timeout=timeout):
        yield


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "canonical_folder_path",
    "document_lock",
    "exact_folder_lock",
    "folder_lock_root",
    "folder_operation_locks",
    "folder_path_key",
    "hierarchy_lock",
    "migration_store_lock",
    "path_lock",
    "store_lock",
]
