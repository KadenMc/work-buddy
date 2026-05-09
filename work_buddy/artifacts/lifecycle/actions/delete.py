"""Delete — the default ExpiryAction.

When the trigger marks records as expired and the retention predicate
(if any) doesn't override, ``Delete`` calls the storage's
``delete_record`` (or ``delete_where`` if the storage prefers bulk
operations) to remove them.

Capabilities declared: none extra. ``Delete`` is the implicit default;
its presence is what every Lifecycle should have unless it explicitly
needs ``TransformAndDelete`` instead.
"""

from __future__ import annotations

from typing import Any

from work_buddy.artifacts.protocol import Capability, Ref, Storage


class Delete:
    """Plain delete: each expired ref is passed to storage.delete_record.

    For backends that prefer bulk operations (JsonRecords, Jsonl, the
    SQLite backends), iterating one-by-one would force N atomic
    rewrites of the underlying file. So when the storage declares
    ``BULK_PRUNEABLE``, ``Delete`` issues a single bulk delete via
    ``delete_where`` keyed on the set of expired ids.
    """

    capabilities: frozenset[Capability] = frozenset()

    def apply(
        self,
        storage: Storage,
        expired_refs: list[Ref],
        *,
        dry_run: bool,
    ) -> dict[str, int]:
        if not expired_refs:
            return {"pruned": 0, "bytes_freed": 0}

        if dry_run:
            return {"pruned": len(expired_refs), "bytes_freed": 0}

        # Prefer bulk delete when available — avoids N atomic rewrites
        # for record-set storages (JSON files, JSONL, sqlite).
        if Capability.BULK_PRUNEABLE in storage.capabilities:
            victim_ids = {ref.id for ref in expired_refs}
            n, bytes_freed = storage.delete_where(
                lambda record: _record_id_in(record, victim_ids)
            )
            return {"pruned": n, "bytes_freed": bytes_freed}

        # Fall back to per-ref delete (filesystem, directory tree).
        bytes_freed = 0
        n = 0
        for ref in expired_refs:
            freed = storage.delete_record(ref)
            if freed >= 0:
                bytes_freed += freed
                n += 1
        return {"pruned": n, "bytes_freed": bytes_freed}


def _record_id_in(record: dict[str, Any], victim_ids: set[str]) -> bool:
    """Check whether ``record``'s id is in the victim set.

    Mirrors the id-extraction logic the backends use in their
    ``ref_for`` so the bulk-delete predicate hits the same records the
    lifecycle's find_expired identified.
    """
    for key in (
        "_key", "id", "key", "task_id", "scoped_task_id",
        "_file_name", "_dir_name", "_idx", "_line_idx",
    ):
        if key in record:
            if str(record[key]) in victim_ids:
                return True
    return False
