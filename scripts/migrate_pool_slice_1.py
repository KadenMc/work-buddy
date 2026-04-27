"""One-shot migration over the live triage pool (Slice 1).

Goals
-----

Bring every entry currently on disk into alignment with the new
Slice 1 schema and lifecycle:

1. **Backfill ``state``** — legacy entries get ``state=reviewed`` if
   they have ``reviewed_at``, else ``state=pending``. (PoolEntry.from_dict
   already infers this for reads, but we persist the field explicitly so
   subsequent code doesn't need the fallback path.)
2. **Recompute ``item_content_hash``** — the Slice 1 normalization is
   stricter (NFKC + lowercase + markdown-bullet strip + whitespace
   collapse). Existing hashes were computed under the looser rules, so
   cross-run dedup is broken between old and new entries until we
   rewrite them.
3. **Backfill ``expires_at``** for pending entries — the source
   descriptor's TTL (e.g. journal 5d) is computed from each entry's
   ``created_at``. Entries whose computed ``expires_at`` is already in
   the past will be marked ``state=stale`` by the next sweep.
4. **Run ``triage_pool_sweep`` once** — sweeps the now-tagged pool and
   transitions stale + quarantined entries based on live source state.

Safety
------

- Pool snapshot is taken to ``data/triage_pool/pool.json.pre-migration-<ts>``
  before any write.
- Dry-run mode prints what would change without writing.
- Idempotent: running twice produces the same result. Backfill skips
  fields that are already populated.

Usage
-----

::

    # Rehearsal:
    python scripts/migrate_pool_slice_1.py --dry-run

    # Apply:
    python scripts/migrate_pool_slice_1.py
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from work_buddy.triage.background import (
    STATE_PENDING,
    STATE_REVIEWED,
    _compute_expires_at,
    get_pool,
    item_content_hash,
)
from work_buddy.triage.capabilities.triage_pool_sweep import triage_pool_sweep


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change without writing.",
    )
    parser.add_argument(
        "--skip-sweep", action="store_true",
        help="Backfill only; don't run the sweep afterward.",
    )
    args = parser.parse_args(argv)

    pool = get_pool()
    index_path = pool._index_path
    if not index_path.exists():
        print(f"No pool index at {index_path} — nothing to migrate.")
        return 0

    raw_data = json.loads(index_path.read_text(encoding="utf-8"))
    entries = raw_data.get("entries", [])
    print(f"Loaded {len(entries)} entries from {index_path}")

    # Snapshot before write
    if not args.dry_run:
        backup_path = index_path.with_suffix(
            f".pre-migration-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )
        shutil.copy2(index_path, backup_path)
        print(f"Snapshot: {backup_path}")

    # ---- Backfill counters ------------------------------------------------
    state_backfilled = 0
    hash_recomputed = 0
    hash_changed = 0
    expires_backfilled = 0
    now_iso = _now_iso()

    for entry in entries:
        # 1. state backfill
        if "state" not in entry or entry.get("state") is None:
            entry["state"] = (
                STATE_REVIEWED if entry.get("reviewed_at") else STATE_PENDING
            )
            entry["state_changed_at"] = entry.get("reviewed_at") or now_iso
            state_backfilled += 1

        # 2. hash recompute
        item = entry.get("item") or {}
        text = item.get("text", "") or ""
        src = item.get("source", entry.get("source", "")) or ""
        new_hash = item_content_hash(src, text)
        old_hash = entry.get("item_content_hash")
        if new_hash != old_hash:
            entry["item_content_hash"] = new_hash
            hash_recomputed += 1
            if old_hash is not None:
                hash_changed += 1

        # 3. expires_at backfill (only for pending entries — reviewed
        # entries don't need a TTL; they've already been actioned).
        if (
            entry.get("state") == STATE_PENDING
            and not entry.get("expires_at")
        ):
            created_at = entry.get("created_at", "")
            entry_source = entry.get("source", "")
            new_expiry = _compute_expires_at(entry_source, created_at)
            if new_expiry:
                entry["expires_at"] = new_expiry
                expires_backfilled += 1

    print()
    print(f"  state backfilled:     {state_backfilled}")
    print(f"  hash recomputed:      {hash_recomputed}  (of which "
          f"{hash_changed} actually changed)")
    print(f"  expires_at backfilled: {expires_backfilled}")

    # Hash-collision summary (post-recompute) — duplicate-hash counts.
    from collections import Counter
    counts = Counter()
    for entry in entries:
        if entry.get("state") == STATE_PENDING:
            counts[entry.get("item_content_hash") or ""] += 1
    dup_groups = {h: n for h, n in counts.items() if n > 1}
    print(
        f"  pending-entry duplicate hash groups: "
        f"{len(dup_groups)} (max group size {max(dup_groups.values()) if dup_groups else 0})"
    )

    # Apply backfill
    if not args.dry_run:
        index_path.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")
        print(f"\nWrote backfill to {index_path}")
    else:
        print("\n(dry-run — no writes)")

    # Run sweep
    if args.skip_sweep:
        print("\n(--skip-sweep — leaving lifecycle pass to the cron)")
        return 0

    print("\nRunning triage_pool_sweep ...")
    sweep_result = triage_pool_sweep(dry_run=args.dry_run)
    print(json.dumps(sweep_result, indent=2))

    print("\nMigration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
