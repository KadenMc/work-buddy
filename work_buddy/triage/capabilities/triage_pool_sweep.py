"""``triage_pool_sweep`` capability — daily liveness pass over the pool (Slice 1).

Walks every pending :class:`PoolEntry`, checks two things:

1. **TTL expiry.** If the entry is past its ``expires_at`` the state
   transitions ``pending → stale``. Soft signal — entries are still
   on disk; the Review tab just stops showing them.

2. **Source-specific quarantine triggers.** Per the entry's source
   descriptor (see :mod:`work_buddy.triage.sources`), the configured
   quarantine triggers (``source_removed``,
   ``source_edited_beyond_match``, …) are evaluated. The first one
   that fires transitions ``pending → quarantined`` with a
   ``quarantine_reason`` recorded for audit.

Order
-----

Quarantine takes precedence over stale: if an entry would be both
expired AND has a quarantine trigger firing, we quarantine (more
specific signal — the source is GONE, not just old).

Defensive
---------

The sweep is unattended. One bad entry must not poison the whole
pass. Per-entry checks are wrapped; failures are logged and the
entry is skipped (no transition). Trigger-function defensiveness is
the responsibility of :mod:`sources_triggers`.

Cadence
-------

Run daily via ``sidecar_jobs/triage-pool-sweep.md``. Manual runs
via ``mcp__work-buddy__wb_run("triage_pool_sweep", {"dry_run": true})``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.background import (
    PoolEntry,
    STATE_PENDING,
    STATE_QUARANTINED,
    STATE_STALE,
    get_pool,
)
from work_buddy.triage.sources import (
    SourceDescriptor,
    get_descriptor,
)
from work_buddy.triage.sources_triggers import evaluate_triggers

logger = get_logger(__name__)


def triage_pool_sweep(
    *,
    dry_run: bool = False,
    source: str | None = None,
    max_entries: int | None = None,
) -> dict[str, Any]:
    """Sweep the pool for stale + quarantinable entries.

    Args:
        dry_run: When True, computes what would change but doesn't
            write back. Useful for a rehearsal before the cron fires
            and for manual inspection.
        source: When set, only entries from this source are inspected
            (e.g. ``"journal_thread"``). Other entries are untouched.
        max_entries: Safety cap on entries inspected per pass.

    Returns:
        Stats dict: ``{
            "checked": int,
            "stale_marked": int,
            "quarantined": int,
            "errors": int,
            "by_source": {
                source_name: {checked, stale, quarantined, errors},
                ...
            },
            "samples": [...],   # first few quarantine reasons (audit aid)
            "dry_run": bool,
        }``
    """
    pool = get_pool()
    pending = pool.pending(source=source, max_items=max_entries)

    now = datetime.now(timezone.utc)

    stats_by_source: dict[str, dict[str, int]] = {}
    samples: list[dict[str, Any]] = []
    to_stale: list[tuple[str, str]] = []
    to_quarantine: list[tuple[tuple[str, str], str]] = []
    errors = 0

    for entry in pending:
        s = stats_by_source.setdefault(entry.source or "", {
            "checked": 0, "stale": 0, "quarantined": 0, "errors": 0,
        })
        s["checked"] += 1

        descriptor = get_descriptor(entry.source or "")

        # 1. Quarantine triggers (more specific than TTL — run first).
        quarantine_reason: str | None = None
        if descriptor and descriptor.quarantine_triggers:
            try:
                quarantine_reason = evaluate_triggers(entry, descriptor)
            except Exception as exc:  # defensive: log + skip
                logger.warning(
                    "triage_pool_sweep: trigger evaluation crashed for "
                    "%s/%s: %s", entry.run_id, entry.item_id, exc,
                )
                s["errors"] += 1
                errors += 1
                continue

        if quarantine_reason:
            to_quarantine.append(
                ((entry.run_id, entry.item_id), quarantine_reason),
            )
            s["quarantined"] += 1
            if len(samples) < 8:
                samples.append({
                    "run_id": entry.run_id,
                    "item_id": entry.item_id,
                    "source": entry.source,
                    "transition": "quarantined",
                    "reason": quarantine_reason,
                })
            continue

        # 2. TTL expiry — soft signal.
        if entry.expires_at and _is_past(entry.expires_at, now):
            to_stale.append((entry.run_id, entry.item_id))
            s["stale"] += 1
            if len(samples) < 8:
                samples.append({
                    "run_id": entry.run_id,
                    "item_id": entry.item_id,
                    "source": entry.source,
                    "transition": "stale",
                    "expires_at": entry.expires_at,
                })

    # Apply transitions (or skip in dry run).
    stale_marked = 0
    quarantined = 0
    if not dry_run:
        if to_stale:
            stale_marked = pool.mark_stale(to_stale)
        if to_quarantine:
            # Group by reason so mark_state's reason field is correct.
            by_reason: dict[str, list[tuple[str, str]]] = {}
            for key, reason in to_quarantine:
                by_reason.setdefault(reason, []).append(key)
            for reason, keys in by_reason.items():
                quarantined += pool.quarantine(keys, reason=reason)
    else:
        stale_marked = len(to_stale)
        quarantined = len(to_quarantine)

    result: dict[str, Any] = {
        "status": "ok",
        "dry_run": dry_run,
        "checked": len(pending),
        "stale_marked": stale_marked,
        "quarantined": quarantined,
        "errors": errors,
        "by_source": stats_by_source,
        "samples": samples,
    }
    logger.info(
        "triage_pool_sweep: checked=%d stale=%d quarantined=%d errors=%d "
        "dry_run=%s",
        result["checked"], result["stale_marked"], result["quarantined"],
        result["errors"], dry_run,
    )
    return result


def _is_past(iso_ts: str, now: datetime) -> bool:
    """Return True if ``iso_ts`` is strictly before ``now``."""
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < now
