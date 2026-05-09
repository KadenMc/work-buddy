"""Shared low-level I/O primitives for artifact backends.

Hoisted from the original ``ArtifactStore._atomic_write`` so every
backend that writes a single-file payload (filesystem blobs, JSON
records, JSONL logs) can share the same crash-safe write idiom.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically via temp file + rename.

    Crash-safe: if interrupted, the original file (if any) is preserved
    intact; the temp file is cleaned up on error.

    Pattern: ``mkstemp`` in the parent directory → ``os.write`` →
    ``os.replace``. The atomic ``replace`` is the key: it's a
    single-syscall rename on POSIX and Windows that either fully
    succeeds or fully fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, prefix=".artifact_", suffix=".tmp"
    )
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Convenience wrapper: encode ``text`` and atomically write."""
    atomic_write_bytes(path, text.encode(encoding))
