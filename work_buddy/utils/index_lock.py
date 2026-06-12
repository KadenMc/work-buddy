"""Generic per-index advisory lock.

A long index build (the vault encode is multi-minute) must not race the 5-minute
rebuild cron. This is a coarse, cross-platform advisory lock built on an
``O_CREAT | O_EXCL`` lockfile — the exclusive create is the SOLE arbiter of
ownership, so exactly one waiter wins even when two processes reclaim a stale lock
at the same instant.

A holder writes ``{pid, started_at, heartbeat}`` to ``<target>.lock``. The lock is
HONORED while the holder PID is alive AND the file is fresh (mtime within
``stale_after_s``); otherwise it is reclaimed. The two gates together survive a
crashed holder (dead PID), a PID-reused holder (stale age), and a slow build (a
daemon heartbeat thread, started on acquire, :func:`refresh`es the lock while held,
so even a multi-hour build never looks stale).

An *unparseable* lock (the sub-millisecond window between the ``O_EXCL`` create and
the first holder write, or a hand-corrupted file) is honored while fresh and only
reclaimed once stale — this closes the empty-file race where a concurrent acquirer
could otherwise read the just-created file as empty and steal it.

Mirrors the dead-PID + stale-age discipline of ``ir/store.py::recover_vector_store``
and the atomic-write of ``sidecar/pid.py``. Distinct from
``markdown_db/storage_helpers.file_lock`` (age-only, no PID-liveness).
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

from work_buddy.logging_config import get_logger
from work_buddy.utils.process import is_process_alive

logger = get_logger(__name__)

DEFAULT_STALE_AFTER_S = 3600.0

# On Windows a delete/replace fails with PermissionError (WinError 5/32) while
# another thread/process momentarily has the lockfile open for reading. The reader
# holds it for sub-ms, so a brief backoff absorbs the contention (same mitigation
# ``npz_io.atomic_replace`` uses).
_RETRY_DELAYS_S = (0.0, 0.02, 0.05, 0.1, 0.2, 0.4)


def _lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


def _unlink_quiet(lock: Path) -> None:
    """Unlink the lockfile, tolerating absence and a transient Windows lock."""
    last: Exception | None = None
    for delay in _RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            lock.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:  # a reader has it momentarily open
            last = exc
    logger.warning("index_lock: could not unlink %s after retries: %s", lock, last)


def _replace_with_retry(tmp: str, dst: Path) -> None:
    last: Exception | None = None
    for delay in _RETRY_DELAYS_S:
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as exc:  # dst momentarily open by a reader
            last = exc
    assert last is not None
    raise last


def _read_holder(lock: Path) -> dict | None:
    """Return the holder dict, or ``None`` for a missing/partial/corrupt lockfile.

    ``read_text`` closes its handle immediately, so this never leaves a handle open
    that could block a later ``os.replace`` on Windows.
    """
    try:
        data = json.loads(lock.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    return data


def _write_holder_atomic(lock: Path, holder: dict) -> None:
    """Write the holder dict atomically (temp + ``os.replace``), never truncating."""
    fd, tmp = tempfile.mkstemp(dir=str(lock.parent), prefix=f".{lock.name}.", suffix=".tmp")
    try:
        os.write(fd, json.dumps(holder).encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        _replace_with_retry(tmp, lock)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _keep(holder: dict | None, age: float, stale_after_s: float) -> bool:
    """Whether a lock should be HONORED (vs reclaimed): fresh AND (live PID or
    not-yet-identifiable mid-create)."""
    if age >= stale_after_s:
        return False
    if holder is None:
        return True  # fresh but unparseable → assume a holder is mid-create
    return is_process_alive(holder["pid"])


def is_locked(target: Path, *, stale_after_s: float = DEFAULT_STALE_AFTER_S) -> bool:
    """Whether a live, fresh holder currently owns the lock. READ-ONLY.

    Never mutates the filesystem — safe for the cron to call to decide whether to
    skip a run. A dead-PID or stale lock reads as unlocked (the next builder
    reclaims it).
    """
    lock = _lock_path(target)
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False  # no lockfile
    return _keep(_read_holder(lock), age, stale_after_s)


def refresh(target: Path) -> None:
    """Bump the holder heartbeat + mtime (call periodically during a long hold)."""
    lock = _lock_path(target)
    holder = _read_holder(lock) or {"pid": os.getpid(), "started_at": time.time()}
    holder["pid"] = os.getpid()
    holder["heartbeat"] = time.time()
    try:
        _write_holder_atomic(lock, holder)
    except OSError as exc:
        logger.warning("index_lock: heartbeat refresh failed for %s: %s", lock, exc)


@contextlib.contextmanager
def index_lock(
    target: Path,
    *,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    timeout: float = 30.0,
    poll: float = 0.2,
) -> Iterator[None]:
    """Acquire the advisory lock for ``target`` (its ``<name>.lock`` sibling).

    ``O_CREAT | O_EXCL`` is the sole arbiter of ownership. A stale lock (dead PID or
    older than ``stale_after_s``) is reclaimed by unlinking it and retrying the
    exclusive create — the create, not the unlink, decides the winner, so two
    concurrent reclaimers cannot both acquire.

    Raises ``TimeoutError`` if a live, fresh holder keeps the lock past ``timeout``.

    While held, a daemon thread re-stamps the heartbeat every ``stale_after_s / 3``,
    so a long hold (the vault encode is multi-hour) never ages out and looks
    abandoned to ``is_locked`` or a would-be reclaimer. The thread self-stops the
    instant the lock is no longer ours, and ``_release`` joins it before unlinking —
    it can neither clobber a successor's lock nor re-create the one it just removed.
    """
    lock = _lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout

    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "heartbeat": time.time(),
                }).encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            break  # acquired
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue  # vanished between create-fail and stat → retry create
            except OSError:
                age = float("inf")
            if _keep(_read_holder(lock), age, stale_after_s):
                if time.time() >= deadline:
                    raise TimeoutError(
                        f"index_lock: {lock} held by a live holder "
                        f"(age {age:.0f}s); timed out after {timeout}s"
                    )
                time.sleep(poll)
                continue
            # Stale (dead PID or aged-out) → reclaim and re-arbitrate via O_EXCL.
            _unlink_quiet(lock)
            continue

    # Keep our lock fresh for the whole hold: a daemon thread re-stamps the heartbeat
    # periodically so a long build never ages past ``stale_after_s`` and looks
    # abandoned to ``is_locked`` or a concurrent reclaimer. It self-stops the moment
    # the lock is no longer ours — a build paused past the stale window can be
    # reclaimed, and we must never clobber the successor's lock.
    stop = threading.Event()

    def _heartbeat() -> None:
        interval = max(1.0, stale_after_s / 3)
        while not stop.wait(interval):
            holder = _read_holder(lock)
            if holder is None or holder.get("pid") != os.getpid():
                return  # reclaimed out from under us → stop, don't clobber the successor
            refresh(target)

    beat = threading.Thread(target=_heartbeat, name=f"index-lock-hb:{lock.name}", daemon=True)
    beat.start()

    def _release() -> None:
        # Stop heartbeating and let any in-flight refresh finish BEFORE unlinking, so
        # the thread can't re-create the lockfile we just removed.
        stop.set()
        beat.join(timeout=5.0)
        # Unlink only if WE still hold it — a build that got stale-reclaimed must
        # not delete its successor's lock.
        holder = _read_holder(lock)
        if holder is not None and holder.get("pid") == os.getpid():
            _unlink_quiet(lock)

    atexit.register(_release)  # crash net for a hard exit
    try:
        yield
    finally:
        atexit.unregister(_release)
        _release()
