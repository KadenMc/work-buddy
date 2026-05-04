"""journal_v5_scan — canonical journal → v5 Thread spawn pipeline.

Calls the existing journal segmenter
(``work_buddy.clarify.adapters.journal.collect_same_day_candidates``)
and spawns a v5 Thread per TriageItem via the spawn helper from
``source_pipelines.py``.

Distinct from the v4 ``journal_triage_scan``: that capability runs
the full Clarify pipeline (segment → enrich → LLM verdict pass →
ClarifyPool entry). This capability runs ONLY the segment step and
writes straight to v5 Threads. Once Stage 4.14 retires the v4 pool,
this becomes the only journal-to-Thread path; the v4 capability is
removed at that point.

Each spawned Thread:
- ``inciting_event_summary['source'] = 'journal_note'`` so the
  journal-note cleanup adapter applies (the dashboard's "Clean Up"
  button deletes the inciting line from the vault).
- ``note_path`` derived as ``journal/<YYYY-MM-DD>.md`` (matches
  ``work_buddy/journal.py``'s vault-rel convention).
- ``line_text`` taken as the first non-empty line of the segment's
  raw text — used by the cleanup adapter for exact-text matching.
- ``autonomy_policy`` set via ``default_spawn_policy()`` to
  ``plan_then_review``: the agent auto-advances through intent and
  context inference and pauses at action approval. Configurable via
  ``threads.default_autonomy_composition`` in ``config.yaml``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from work_buddy.threads.source_pipelines import (
    spawn_threads_from_journal_scan,
)

logger = logging.getLogger(__name__)


def journal_v5_scan(
    *,
    journal_date: Optional[str] = None,
    profile: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Segment a daily journal and produce v5 Threads.

    Args:
        journal_date: ``YYYY-MM-DD`` or None for today.
        profile: Override the configured ``triage.segment_profile``.
            Defaults to whatever ``resolve_profile`` returns for
            'segment' (typically ``local_general``).
        dry_run: If True, return the segmented items + the would-be
            spawn metadata without writing.

    Returns:
        {
            "status": "ok" | "dry_run" | "no_items",
            "journal_date": str,
            "item_count": int,
            "spawned_thread_ids": [str],   # absent for dry_run
            "items": [...],                # only for dry_run
        }
    """
    from work_buddy.clarify.adapters.journal import collect_same_day_candidates
    from work_buddy.clarify.config import load_triage_config, resolve_profile

    cfg = load_triage_config()
    seg_profile = resolve_profile(cfg, "segment", override=profile)

    items, content_hash = collect_same_day_candidates(
        journal_date=journal_date, profile=seg_profile,
    )

    # Resolve journal_date for the response (None → today's effective date)
    effective_date = (
        items[0].metadata.get("journal_date") if items else journal_date
    )

    if not items:
        return {
            "status": "no_items",
            "journal_date": effective_date,
            "item_count": 0,
            "spawned_thread_ids": [],
            "content_hash": content_hash,
        }

    if dry_run:
        return {
            "status": "dry_run",
            "journal_date": effective_date,
            "item_count": len(items),
            "items": [it.to_dict() for it in items],
            "content_hash": content_hash,
        }

    item_dicts = [it.to_dict() for it in items]
    spawn_result = spawn_threads_from_journal_scan(
        item_dicts, journal_date=effective_date,
    )
    # v2 return shape: ``{umbrella_id, child_thread_ids, total_count,
    # child_count}``. The umbrella replaces the old "parent" thread;
    # children are group sub-threads (one per tag-similarity cluster),
    # each holding its segments as ``context_items``.
    umbrella_id = spawn_result.get("umbrella_id")
    child_ids = spawn_result.get("child_thread_ids") or []
    logger.info(
        "journal_v5_scan: spawned umbrella %s + %d group children "
        "for %s (%d items distributed)",
        umbrella_id, len(child_ids), effective_date,
        spawn_result.get("total_count") or 0,
    )
    return {
        "status": "ok",
        "journal_date": effective_date,
        "item_count": len(items),
        "umbrella_id": umbrella_id,
        "child_thread_ids": child_ids,
        # Back-compat aliases for any existing callers reading the
        # old keys. New consumers should use ``umbrella_id`` +
        # ``child_thread_ids``.
        "parent_thread_id": umbrella_id,
        "spawned_thread_ids": child_ids,
        "content_hash": content_hash,
    }
