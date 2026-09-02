"""Cross-process serialization for the legacy Projects dual surface.

Legacy Project operations can span both a Markdown file and the Projects
SQLite store.  A SQLite ``BEGIN IMMEDIATE`` cannot guard the whole operation:
the store adapters open their own writer connections and would deadlock behind
the guard transaction.  Instead, authority transitions and complete legacy
operations share this small OS-backed advisory lock.

The lock file contains no Project data.  It is never reclaimed based on age;
the operating system releases the byte-range/flock lock when a process exits.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar


_P = ParamSpec("_P")
_R = TypeVar("_R")
_LOCK_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 0.05

_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_thread_state = threading.local()


def _canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def _thread_lock_for(key: str) -> threading.RLock:
    with _registry_guard:
        return _thread_locks.setdefault(key, threading.RLock())


def _depths() -> dict[str, int]:
    depths = getattr(_thread_state, "depths", None)
    if depths is None:
        depths = {}
        _thread_state.depths = depths
    return depths


def _try_lock(handle: Any) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_process_lock(
    path: str | Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold a re-entrant, thread- and process-exclusive advisory lock."""

    lock_path = Path(path).expanduser().resolve()
    key = _canonical_key(lock_path)
    local_lock = _thread_lock_for(key)
    with local_lock:
        depths = _depths()
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())

            deadline = time.monotonic() + timeout_seconds
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for operation lock: {lock_path.name}"
                    )
                time.sleep(_POLL_SECONDS)

            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                _unlock(handle)


def project_operation_lock_path() -> Path:
    """Return the coordination lock adjacent to the configured Projects DB."""

    from work_buddy.projects import store as project_store

    database = project_store._db_path().expanduser().resolve()
    return database.with_name(f".{database.name}.legacy-operation.lock")


@contextmanager
def project_authority_transition_lock() -> Iterator[None]:
    """Serialize a Projects authority transition with legacy operations."""

    with exclusive_process_lock(project_operation_lock_path()):
        yield


@contextmanager
def legacy_project_operation() -> Iterator[None]:
    """Admit and hold one complete legacy Markdown/SQLite operation.

    Authority is checked only after the shared lock is acquired.  Because
    pause and seal transitions take the same lock, an admitted operation must
    finish before the fence can commit, and a queued operation observes the
    fence before touching either legacy Markdown or the store.
    """

    with project_authority_transition_lock():
        from work_buddy.projects.authority import require_markdown_write_allowed

        require_markdown_write_allowed()
        yield


def serialized_project_authority_transition(
    function: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Decorate a Projects authority method with the shared operation lock."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with project_authority_transition_lock():
            return function(*args, **kwargs)

    return wrapped


__all__ = [
    "exclusive_process_lock",
    "legacy_project_operation",
    "project_authority_transition_lock",
    "project_operation_lock_path",
    "serialized_project_authority_transition",
]
