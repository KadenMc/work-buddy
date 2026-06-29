"""One-time cleanup of the messages DB. Two effects, both safe to re-run:

1. Delete ``bus.event`` rows. Cross-process UI events do not flow through the
   messages table — they POST straight to the dashboard's ``/internal/bus`` — so
   any ``bus.event`` rows present are dead weight. A one-time DELETE reclaims
   them immediately instead of waiting for each to age out on the 30-day TTL.
2. Report acknowledgement-disposition pending rows. The retention predicate lets
   these reap on the TTL, so they need no action here — the scheduled
   ``artifact_cleanup`` sweep removes the aged ones. The count is printed for
   visibility.

Orphaned ``message_reads`` are cleaned and the DB is VACUUMed after a live run.

Usage::

    python -m scripts.backfill_message_cleanup            # dry-run (default)
    python -m scripts.backfill_message_cleanup --apply    # actually delete + vacuum
"""

from __future__ import annotations

import argparse
import sqlite3

from work_buddy.messaging.models import _db_path


def run(*, apply: bool) -> dict[str, int]:
    path = _db_path()
    bytes_before = path.stat().st_size if path.exists() else 0
    conn = sqlite3.connect(str(path))
    try:
        bus_events = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE type = 'bus.event'"
        ).fetchone()[0]
        ack_pending = conn.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE status = 'pending' AND disposition = 'acknowledgement'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        if apply and bus_events:
            conn.execute("DELETE FROM messages WHERE type = 'bus.event'")
            conn.execute(
                "DELETE FROM message_reads "
                "WHERE message_id NOT IN (SELECT id FROM messages)"
            )
            conn.commit()
            conn.execute("VACUUM")
    finally:
        conn.close()

    bytes_after = path.stat().st_size if path.exists() else 0
    return {
        "total_before": total,
        "bus_events_deleted": bus_events if apply else 0,
        "bus_events_found": bus_events,
        "ack_pending_remaining": ack_pending,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the deletion + VACUUM. Without it, only counts are reported.",
    )
    args = parser.parse_args()

    result = run(apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN (use --apply to execute)"
    print(f"[{mode}] messages cleanup")
    print(f"  total rows (before)     : {result['total_before']:,}")
    print(f"  bus.event rows found    : {result['bus_events_found']:,}")
    print(f"  bus.event rows deleted  : {result['bus_events_deleted']:,}")
    print(f"  ack-pending remaining   : {result['ack_pending_remaining']:,} "
          f"(reaped by the scheduled sweep as they age past TTL)")
    if args.apply:
        reclaimed = result["bytes_before"] - result["bytes_after"]
        print(f"  bytes reclaimed         : {reclaimed:,}")


if __name__ == "__main__":
    main()
