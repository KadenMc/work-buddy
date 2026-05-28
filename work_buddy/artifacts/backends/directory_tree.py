"""Directory-tree backend for sessions, log files, and per-record JSON files.

Three sub-shapes are supported, controlled by the ``shape`` constructor arg:

* ``DirShape.SESSION_DIRS`` — each immediate child of the root is a
  session directory containing a ``manifest.json`` plus arbitrary
  files. Records are the directories themselves; deletion is
  ``shutil.rmtree``. Used by ``agent_sessions``.
* ``DirShape.LOG_FILES`` — flat tree of files (recursively); each file
  is a record. Deletion is ``Path.unlink``. Used by ``logs/global``.
* ``DirShape.JSON_FILES`` — each immediate child is a JSON file; the
  file's contents are the record. Used by ``notifications`` (one
  ``.json`` per request under ``agents/consent/requests/``).

Capabilities declared:
    RECORDS, LISTABLE, DELETABLE.

The DirectoryTree backend is intentionally the most flexible (and
admittedly the most awkward fit) — its three shapes paper over what
would otherwise be three nearly-identical backends.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.protocol import StorageTrait, Ref


class DirShape(str, Enum):
    SESSION_DIRS = "session_dirs"  # each child is a session directory
    LOG_FILES = "log_files"        # recursive tree of files
    JSON_FILES = "json_files"      # each child is a .json file


class DirectoryTreeStorage:
    """Per-record directory or file backend.

    Args:
        root: Root directory.
        shape: Layout — see :class:`DirShape`.
        manifest_filename: For ``SESSION_DIRS``, the file inside each
            child directory whose contents identify the record (default
            ``manifest.json``).
        artifact_name: Name embedded in returned Refs.
    """

    capabilities: frozenset[StorageTrait] = frozenset({
        StorageTrait.RECORDS,
        StorageTrait.LISTABLE,
        StorageTrait.DELETABLE,
    })

    def __init__(
        self,
        *,
        root: Path,
        shape: DirShape,
        manifest_filename: str = "manifest.json",
        artifact_name: str | None = None,
    ) -> None:
        self._root = root
        self._shape = shape
        self._manifest = manifest_filename
        self._artifact_name = artifact_name or root.name

    # --------------------------------------------------------- Storage API

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self._root.is_dir():
            return

        if self._shape == DirShape.SESSION_DIRS:
            for entry in self._root.iterdir():
                if not entry.is_dir():
                    continue
                manifest = entry / self._manifest
                if not manifest.exists():
                    continue
                try:
                    raw = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                # Compute latest mtime for activity check
                latest_mtime = self._latest_mtime(entry)
                yield {
                    **raw,
                    "_dir_path": str(entry),
                    "_dir_name": entry.name,
                    "_latest_mtime": latest_mtime.isoformat(),
                }

        elif self._shape == DirShape.LOG_FILES:
            for f in self._root.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                    size = f.stat().st_size
                except OSError:
                    continue
                yield {
                    "_file_path": str(f),
                    "_file_name": f.name,
                    "_mtime": mtime.isoformat(),
                    "_size_bytes": size,
                }

        else:  # JSON_FILES
            for f in self._root.iterdir():
                if not f.is_file() or f.suffix != ".json":
                    continue
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(raw, dict):
                    continue
                yield {
                    **raw,
                    "_file_path": str(f),
                    "_file_name": f.name,
                }

    def ref_for(self, record: dict[str, Any]) -> Ref:
        if self._shape == DirShape.SESSION_DIRS:
            rid = record.get("_dir_name") or record.get("session_id", "unknown")
        elif self._shape == DirShape.LOG_FILES:
            rid = record.get("_file_name", "unknown")
        else:
            rid = record.get("id") or record.get("_file_name", "unknown")
        return Ref(
            id=str(rid),
            artifact_name=self._artifact_name,
            metadata={k: v for k, v in record.items()
                      if not k.startswith("_") or k in ("_dir_path", "_file_path")},
        )

    def delete_record(self, ref: Ref) -> int:
        if self._shape == DirShape.SESSION_DIRS:
            entry = self._root / ref.id
            if not entry.is_dir():
                return 0
            size = sum(
                f.stat().st_size for f in entry.rglob("*") if f.is_file()
            )
            shutil.rmtree(entry, ignore_errors=True)
            return size

        elif self._shape == DirShape.LOG_FILES:
            # Need to find the file by name (rglob could match multiple
            # at different depths; first hit wins).
            for f in self._root.rglob(ref.id):
                if f.is_file():
                    try:
                        size = f.stat().st_size
                    except OSError:
                        size = 0
                    f.unlink(missing_ok=True)
                    return size
            return 0

        else:  # JSON_FILES
            f = self._root / ref.id
            if not f.is_file():
                # Try with .json suffix
                f = self._root / f"{ref.id}.json"
                if not f.is_file():
                    return 0
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            f.unlink(missing_ok=True)
            return size

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        """Iterate records, delete those matching predicate.

        Not declared in capabilities — but supported as a primitive for
        consistency. Bulk operations on directory trees are
        per-record-delete loops anyway; there's no atomic-rewrite win.
        """
        n = 0
        bytes_freed = 0
        for record in list(self.iter_records()):
            if predicate(record):
                ref = self.ref_for(record)
                bytes_freed += self.delete_record(ref)
                n += 1
        return (n, bytes_freed)

    def size_bytes(self) -> int:
        if not self._root.exists():
            return 0
        total = 0
        for f in self._root.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
        return total

    # ----------------------------------------------------------- internals

    @staticmethod
    def _latest_mtime(directory: Path) -> datetime:
        """Most recent mtime of any file in the directory tree."""
        latest = datetime.fromtimestamp(0, tz=timezone.utc)
        for f in directory.rglob("*"):
            if not f.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime > latest:
                    latest = mtime
            except OSError:
                continue
        return latest
