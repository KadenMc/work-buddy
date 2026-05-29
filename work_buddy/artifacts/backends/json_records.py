"""JSON-records backend for caches and ledgers.

Supports both record shapes seen in the codebase:

* **dict-keyed** (e.g. ``llm_cache``, ``segmentation_cache``) — the
  file is ``{key: entry, ...}`` and each value is a record.
* **list-of-records** (e.g. ``chrome_ledger``) — the file is
  ``[record, record, ...]``. Optionally wrapped in
  ``{"snapshots": [...]}``.

The shape is configured at construction time. Both shapes share the
same lifecycle semantics — each record can be filtered by the trigger,
optionally retained by predicate, and bulk-pruned via atomic rewrite.

Capabilities declared:
    RECORDS, BULK_PRUNEABLE.

Notably absent: LISTABLE / DELETABLE. JSON-records files are not
designed for random-access "give me record X by id" reads — consumers
have their own typed read APIs (``cache.get(scoped_task_id)``,
``chrome_ledger.get_tabs_at(...)``) and the unified surface stays
lifecycle-only.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.io import atomic_write_text
from work_buddy.artifacts.protocol import StorageTrait, Ref


class JsonRecordsShape(str, Enum):
    """How records are laid out inside the JSON file."""

    DICT = "dict"           # {key: entry, ...}
    LIST = "list"           # [entry, entry, ...]
    LIST_WRAPPED = "list_wrapped"  # {"snapshots": [...]} or similar


class JsonRecordsStorage:
    """JSON file storing many records.

    Args:
        path: Path to the JSON file.
        shape: How records are laid out (see :class:`JsonRecordsShape`).
        wrapper_key: For ``LIST_WRAPPED`` shape, the dict key holding the
            list (e.g. ``"snapshots"`` for chrome ledger). Ignored for
            other shapes.
        artifact_name: Name to embed in returned Refs (defaults to the
            file stem).
    """

    capabilities: frozenset[StorageTrait] = frozenset({
        StorageTrait.RECORDS,
        StorageTrait.BULK_PRUNEABLE,
    })

    def __init__(
        self,
        *,
        path: Path,
        shape: JsonRecordsShape,
        wrapper_key: str | None = None,
        artifact_name: str | None = None,
    ) -> None:
        if shape == JsonRecordsShape.LIST_WRAPPED and not wrapper_key:
            raise ValueError(
                "JsonRecordsStorage(LIST_WRAPPED) requires wrapper_key"
            )
        self._path = path
        self._shape = shape
        self._wrapper_key = wrapper_key
        self._artifact_name = artifact_name or path.stem

    # --------------------------------------------------------- internals

    def _load(self) -> tuple[Any, list[dict[str, Any]] | dict[str, dict[str, Any]]]:
        """Return (raw, records). ``records`` is the dict-or-list section."""
        if not self._path.exists():
            empty = {} if self._shape == JsonRecordsShape.DICT else []
            return empty, empty
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            empty = {} if self._shape == JsonRecordsShape.DICT else []
            return empty, empty

        if self._shape == JsonRecordsShape.DICT:
            return raw, raw if isinstance(raw, dict) else {}
        if self._shape == JsonRecordsShape.LIST:
            return raw, raw if isinstance(raw, list) else []
        # LIST_WRAPPED
        if isinstance(raw, dict):
            inner = raw.get(self._wrapper_key, [])
            return raw, inner if isinstance(inner, list) else []
        # File looked like LIST_WRAPPED but is actually bare list:
        # tolerate by using the bare list.
        if isinstance(raw, list):
            return {self._wrapper_key: raw}, raw
        return raw, []

    def _write(self, raw: Any, new_records: Any) -> None:
        if self._shape == JsonRecordsShape.DICT:
            atomic_write_text(self._path, json.dumps(new_records, ensure_ascii=False))
        elif self._shape == JsonRecordsShape.LIST:
            atomic_write_text(self._path, json.dumps(new_records, ensure_ascii=False))
        else:  # LIST_WRAPPED
            if not isinstance(raw, dict):
                raw = {}
            raw[self._wrapper_key] = new_records
            atomic_write_text(self._path, json.dumps(raw, ensure_ascii=False))

    def _record_id(self, record: dict[str, Any], fallback_idx: int) -> str:
        """Best-effort id extraction. Falls back to index for list-shape."""
        for key in ("id", "key", "task_id", "scoped_task_id"):
            if key in record:
                return str(record[key])
        return f"_idx_{fallback_idx}"

    # --------------------------------------------------------- Storage API

    def iter_records(self) -> Iterable[dict[str, Any]]:
        _raw, records = self._load()
        if self._shape == JsonRecordsShape.DICT:
            assert isinstance(records, dict)
            for key, value in records.items():
                # Inject the dict key as the canonical id so triggers
                # and predicates can refer to it generically.
                if isinstance(value, dict):
                    yield {**value, "_key": key}
                else:
                    # Non-dict value (rare); wrap.
                    yield {"_key": key, "_value": value}
        else:
            assert isinstance(records, list)
            for idx, value in enumerate(records):
                if isinstance(value, dict):
                    yield {**value, "_idx": idx}

    def ref_for(self, record: dict[str, Any]) -> Ref:
        if self._shape == JsonRecordsShape.DICT:
            rid = str(record.get("_key", self._record_id(record, 0)))
        else:
            rid = self._record_id(record, record.get("_idx", 0))
        return Ref(
            id=rid,
            artifact_name=self._artifact_name,
            metadata={k: v for k, v in record.items()
                      if k not in ("_key", "_idx")},
        )

    def delete_record(self, ref: Ref) -> int:
        # JsonRecordsStorage doesn't declare DELETABLE — but bulk delete
        # is supported. Implement single-id delete via predicate for
        # internal use.
        bytes_before = self.size_bytes()
        raw, records = self._load()
        if self._shape == JsonRecordsShape.DICT:
            assert isinstance(records, dict)
            if ref.id not in records:
                return 0
            del records[ref.id]
            self._write(raw, records)
        else:
            assert isinstance(records, list)
            new_records = []
            removed = False
            for idx, item in enumerate(records):
                if isinstance(item, dict):
                    if self._record_id({**item, "_idx": idx}, idx) == ref.id:
                        removed = True
                        continue
                new_records.append(item)
            if not removed:
                return 0
            self._write(raw, new_records)
        bytes_after = self.size_bytes()
        return max(0, bytes_before - bytes_after)

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        bytes_before = self.size_bytes()
        raw, records = self._load()
        n_pruned = 0

        if self._shape == JsonRecordsShape.DICT:
            assert isinstance(records, dict)
            kept: dict[str, Any] = {}
            for key, value in records.items():
                check_record = (
                    {**value, "_key": key} if isinstance(value, dict)
                    else {"_key": key, "_value": value}
                )
                if predicate(check_record):
                    n_pruned += 1
                    continue
                kept[key] = value
            if n_pruned == 0:
                return (0, 0)
            self._write(raw, kept)
        else:
            assert isinstance(records, list)
            kept_list: list[Any] = []
            for idx, item in enumerate(records):
                check_record = (
                    {**item, "_idx": idx} if isinstance(item, dict)
                    else {"_idx": idx, "_value": item}
                )
                if predicate(check_record):
                    n_pruned += 1
                    continue
                kept_list.append(item)
            if n_pruned == 0:
                return (0, 0)
            self._write(raw, kept_list)

        bytes_after = self.size_bytes()
        return (n_pruned, max(0, bytes_before - bytes_after))

    def size_bytes(self) -> int:
        return self._path.stat().st_size if self._path.exists() else 0
