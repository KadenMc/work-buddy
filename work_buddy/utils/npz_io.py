"""Atomic, self-healing ``.npz`` read/write for work-buddy's vector caches.

Numpy vector caches (the IR dense-vector store, the knowledge-index content /
alias caches) are **regenerable**: the source of truth lives elsewhere (SQLite
+ JSONL sessions, the knowledge store), so the goal here is resilience, not
durability. A write must never leave a corrupt canonical file, and a read of a
corrupt file must degrade gracefully instead of raising.

This module owns the two primitives that make that true, so every vector cache
shares one implementation:

- :func:`atomic_save_npz` — write to a sibling temp, ``fsync``, then
  ``os.replace`` onto the canonical path. A crash mid-write leaves either the
  intact old file or an orphaned temp, never a truncated canonical.
- :func:`safe_load_npz` — return ``None`` (not raise) for a missing, empty, or
  corrupt file, so callers fall back to a cold rebuild.

## fsync without numpy's ``.npz`` rewrite

``np.savez_compressed`` opens and closes its *own* handle when given a path, so
the caller gets no file descriptor to ``fsync`` — and a path argument also
triggers numpy's "append ``.npz`` if missing" rewrite. Passing an already-open
binary file object avoids both: numpy writes through the caller's handle (no
re-open, no rename), so the handle can be ``fsync``'d and the temp keeps the
name it was given.

## Temp naming and recovery

Temps are named ``<canonical-without-.npz>.<pid>.tmp.npz`` so a recovery sweep
(see ``work_buddy/ir/store.py::recover_vector_store``) can tell an orphan left
by a dead writer from a temp a live writer is mid-write on. The trailing
``.tmp.npz`` keeps numpy from appending another suffix.

## Windows replace caveat

``os.replace`` raises ``PermissionError`` (WinError 5) if the destination is
still open by another process — typically a reader that leaked its ``NpzFile``
handle. :func:`safe_load_npz` uses ``with np.load(...)`` and materializes every
array inside the block so the handle is closed before return; :func:`atomic_replace`
additionally retries the rename a few times to absorb transient AV / indexer
locks.
"""

from __future__ import annotations

import os
import pickle
import time
import zipfile
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Brief retry schedule for the temp -> canonical rename. Windows occasionally
# refuses the rename when AV / Defender / a file indexer momentarily holds the
# destination open; backing off a few hundred ms is enough.
_REPLACE_RETRY_DELAYS_S = (0.05, 0.1, 0.2)

# Temp files are ``<canonical-without-.npz>.<pid>.tmp.npz``. The trailing
# ``.tmp.npz`` both keeps numpy from appending a suffix and gives the recovery
# sweep an unambiguous discriminator from canonical ``*.npz`` files.
TEMP_SUFFIX = ".tmp.npz"
TEMP_GLOB = f"*{TEMP_SUFFIX}"


def atomic_replace(tmp: Path, path: Path) -> None:
    """Rename ``tmp`` over ``path``, retrying briefly on transient locks."""
    last_err: Exception | None = None
    for delay in (0.0, *_REPLACE_RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            tmp.replace(path)
            return
        except PermissionError as e:  # WinError 5 surfaces as PermissionError
            last_err = e
    assert last_err is not None
    raise last_err


def temp_path_for(path: Path, pid: int | None = None) -> Path:
    """Sibling temp path for ``path``: ``<name-without-.npz>.<pid>.tmp.npz``.

    ``path.with_suffix("")`` strips only the trailing ``.npz``, so multi-dot
    canonical names (``work_buddy_ir.task_note.body.npz``) keep their inner
    dots.
    """
    if pid is None:
        pid = os.getpid()
    base = path.with_suffix("")  # drop the trailing .npz
    return base.parent / f"{base.name}.{pid}{TEMP_SUFFIX}"


def pid_from_temp_name(name: str) -> int | None:
    """Extract the writer PID from a temp file name, or ``None`` if unparseable.

    ``work_buddy_ir.conversation.12345.tmp.npz`` -> ``12345``. A non-numeric
    token (or a name not matching the convention) yields ``None``; the sweep
    then falls back to its mtime-age guard.
    """
    if not name.endswith(TEMP_SUFFIX):
        return None
    stem = name[: -len(TEMP_SUFFIX)]  # strip ".tmp.npz"
    token = stem.rsplit(".", 1)[-1]
    return int(token) if token.isdigit() else None


def atomic_save_npz(
    path: Path,
    *,
    tmp_path: Path | None = None,
    **arrays: Any,
) -> Path:
    """Save ``**arrays`` to ``path`` atomically (temp + fsync + replace).

    Writes the compressed archive to a sibling temp, flushes and ``fsync``s it,
    then ``os.replace``s it onto ``path`` — so a crash mid-write can never leave
    a truncated canonical file.

    Args:
        path: Canonical destination path.
        tmp_path: Override the temp path. Defaults to a PID-namespaced sibling
            (see :func:`temp_path_for`) so a recovery sweep can identify
            orphans. Single-process callers that don't need sweep-awareness may
            pass a fixed name.
        **arrays: Forwarded to ``np.savez_compressed`` (array name -> value).

    Returns:
        ``path`` (the canonical destination).
    """
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    if tmp_path is None:
        tmp_path = temp_path_for(path)

    try:
        # Pass an open file object (not a path) so numpy neither re-opens nor
        # appends ".npz", and so we own the fd for fsync.
        with open(tmp_path, "wb") as fh:
            np.savez_compressed(fh, **arrays)
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp_path, path)
    finally:
        # On success the temp was renamed away; on any failure (savez error or a
        # replace that exhausted its retries) clean the partial/leftover temp so
        # it doesn't masquerade as an orphan. The old canonical is untouched
        # either way. A hard kill skips this finally — that orphan is what the
        # recovery sweep cleans by dead-PID.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as exc:
                logger.warning("Could not remove temp %s: %s", tmp_path, exc)

    return path


def safe_load_npz(path: Path) -> dict[str, "np.ndarray"] | None:
    """Load an ``.npz`` file, returning ``None`` instead of raising on trouble.

    Returns ``None`` if the file is missing, zero-byte, or fails to load/parse
    (a truncated or corrupt archive). On success, returns a dict mapping each
    archive key to a fully-materialized array.

    Every array is read **inside** the ``with np.load(...)`` block so the
    underlying ZipFile handle is closed before return — on Windows a leaked
    handle makes a subsequent ``os.replace`` onto this path fail with WinError 5.
    """
    import numpy as np

    if not path.exists():
        return None
    try:
        if path.stat().st_size == 0:
            logger.warning("Vector file %s is zero bytes; treating as absent.", path)
            return None
        with np.load(path, allow_pickle=True) as data:
            # Materialize every array while the handle is open. np.load is lazy,
            # so corruption can surface here on access rather than on np.load().
            return {key: data[key] for key in data.files}
    except (EOFError, zipfile.BadZipFile, ValueError, OSError, pickle.UnpicklingError) as exc:
        # BadZipFile and UnpicklingError subclass Exception (not OSError/ValueError),
        # so they must be caught explicitly. A 0-byte or truncated archive surfaces
        # as BadZipFile/EOFError; a bit-rotted non-zip file (numpy falls back to
        # pickle and chokes) surfaces as UnpicklingError. All mean "corrupt → absent".
        logger.warning("Vector file %s is unreadable (%s); treating as absent.", path, exc)
        return None
