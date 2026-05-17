"""Filesystem helpers for markdown-surface writes.

Two concerns the :class:`~work_buddy.markdown_db.base.MarkdownDB`
orchestration needs and that are worth getting right exactly once:

- :func:`atomic_write_text` — write-to-temp-then-rename so a crash mid
  write never leaves a half-written markdown file. ``os.replace`` is
  atomic on the same filesystem on both POSIX and Windows.
- :func:`file_lock` — a coarse advisory lock keyed on a lockfile next to
  the target, so two processes (the cron drift job and an interactive
  ``apply_mutation``) do not interleave a read-modify-write on the same
  markdown file.

Plus :func:`mtime_utc`, a small convenience for freshness comparisons.

Stdlib only — no external dependency.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a temp file in the same directory (so ``os.replace`` stays
    on one filesystem and is therefore atomic), flushes + fsyncs it, then
    renames it over the target. A crash at any point leaves either the
    old file intact or the new file complete — never a truncated mix.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


@contextlib.contextmanager
def file_lock(
    target: Path, *, timeout: float = 10.0, poll: float = 0.05,
) -> Iterator[None]:
    """Acquire a coarse advisory lock for ``target``.

    Uses an ``O_CREAT | O_EXCL`` lockfile (``<target>.lock``) — the
    create-exclusive open is atomic, so exactly one waiter wins. Other
    waiters poll until the lockfile disappears or ``timeout`` elapses.

    This is advisory: it only excludes other callers that also use
    :func:`file_lock` on the same target. That covers every MarkdownDB
    write path (``apply_mutation`` and the drift reconciler); it does
    NOT exclude a human editing the file in Obsidian — that case is
    handled by the drift loop, not by locking.

    A stale lockfile (older than ``timeout`` × 6) is reclaimed: a
    process that crashed mid-write should not wedge the system forever.
    """
    target = Path(target)
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    stale_after = timeout * 6

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            # Reclaim a stale lock from a crashed holder.
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_after:
                    logger.warning(
                        "file_lock: reclaiming stale lock %s (age %.0fs)",
                        lock_path, age,
                    )
                    with contextlib.suppress(OSError):
                        lock_path.unlink()
                    continue
            except OSError:
                # Lockfile vanished between the open and the stat —
                # race resolved itself; retry immediately.
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"file_lock: could not acquire {lock_path} within "
                    f"{timeout}s (held by another writer)"
                )
            time.sleep(poll)

    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def mtime_utc(path: Path) -> datetime | None:
    """Return ``path``'s modification time as a UTC datetime, or ``None``.

    ``None`` when the path does not exist or cannot be stat'd.
    """
    try:
        ts = Path(path).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)
