"""JSONL append-only backend.

One record per line. Used by the LLM escalation log
(``logs/escalations.log``) and any future log-shaped artifact whose
records are JSON dicts but whose write path is append-only.

Capabilities declared:
    RECORDS, APPEND_ONLY, BULK_PRUNEABLE.

Notably absent: LISTABLE / DELETABLE. Reads go through the consumer's
typed API (``escalation_log.read_escalations(...)``); the unified
surface is lifecycle-only.

Malformed lines (mid-write garbage, partial JSON) are preserved
verbatim through prune — defensive against partial-line writes that
shouldn't get silently swept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from work_buddy.artifacts.io import atomic_write_text
from work_buddy.artifacts.protocol import StorageTrait, Ref


class JsonlStorage:
    """Append-only JSONL file backend.

    Args:
        path: Path to the .jsonl/.log file.
        artifact_name: Name embedded in returned Refs (defaults to file stem).
        preserve_malformed_lines: If True (default), lines that don't
            parse as JSON are kept verbatim through prune. Set False
            only if you actually want to evict garbage.
    """

    capabilities: frozenset[StorageTrait] = frozenset({
        StorageTrait.RECORDS,
        StorageTrait.APPEND_ONLY,
        StorageTrait.BULK_PRUNEABLE,
    })

    def __init__(
        self,
        *,
        path: Path,
        artifact_name: str | None = None,
        preserve_malformed_lines: bool = True,
    ) -> None:
        self._path = path
        self._artifact_name = artifact_name or path.stem
        self._preserve_malformed = preserve_malformed_lines

    # --------------------------------------------------------- Storage API

    def iter_records(self) -> Iterable[dict[str, Any]]:
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return
        for idx, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # surface only well-formed records
            if isinstance(rec, dict):
                yield {**rec, "_line_idx": idx}

    def ref_for(self, record: dict[str, Any]) -> Ref:
        # JSONL records don't have stable ids by default. Use line index
        # as the canonical id; consumers needing semantic IDs can stash
        # one in the record dict and the trigger picks it up.
        rid = str(record.get("id", record.get("trace_id", record.get("_line_idx", "unknown"))))
        return Ref(
            id=rid,
            artifact_name=self._artifact_name,
            metadata={k: v for k, v in record.items() if k != "_line_idx"},
        )

    def delete_record(self, ref: Ref) -> int:
        # JsonlStorage doesn't declare DELETABLE; this is here for
        # internal symmetry but not part of the protocol surface used
        # by Artifact.delete().
        raise NotImplementedError(
            "JsonlStorage doesn't support per-record delete; use delete_where"
        )

    def delete_where(
        self, predicate: Callable[[dict[str, Any]], bool]
    ) -> tuple[int, int]:
        if not self._path.exists():
            return (0, 0)
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return (0, 0)

        bytes_before = self._path.stat().st_size
        kept_lines: list[str] = []
        n_pruned = 0

        for idx, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                if self._preserve_malformed:
                    kept_lines.append(line)
                # If preserve_malformed is False, drop the line.
                continue

            if isinstance(rec, dict):
                check_record = {**rec, "_line_idx": idx}
                if predicate(check_record):
                    n_pruned += 1
                    continue
            kept_lines.append(line)

        if n_pruned == 0:
            return (0, 0)

        new_text = "\n".join(kept_lines) + ("\n" if kept_lines else "")
        atomic_write_text(self._path, new_text)
        bytes_after = self._path.stat().st_size
        return (n_pruned, max(0, bytes_before - bytes_after))

    def size_bytes(self) -> int:
        return self._path.stat().st_size if self._path.exists() else 0
