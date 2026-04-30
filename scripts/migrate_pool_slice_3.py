"""Migrate raw entries through the new Slice 3 Clarify pipeline.

Goal
----

After Slice 1 the live pool collected ``verdict={"raw": True}`` entries
(the verdict-pass gate was off). Slice 3 ships the new multi-record
verdict schema + Haiku deadline pre-pass. This one-shot script walks
every raw entry and re-runs the new pipeline on it so the pool catches
up with the new schema in a single batch.

What it does
------------

For each pool entry where ``verdict.get("raw") is True`` and
``state == "pending"``:

1. Build a TriageItem from the entry's persisted ``item`` dict.
2. Run the cheap deadline-extraction pass
   (``work_buddy.clarify.deadline_extract.extract_deadline_hints``).
3. Run the main Clarify pass via ``LLMRunner.call`` against
   ``MULTI_RECORD_VERDICT_SCHEMA`` at the requested tier (default
   FRONTIER_BALANCED, escalates to FRONTIER_BEST on backend / validation
   failure).
4. Merge deadline hints into the resulting records[].task_proposal.
5. UPDATE the entry's verdict in place (preserves ``state``,
   ``expires_at``, ``quarantine_reason``, ``state_changed_at``,
   ``attraction_passes``, ``forced_context`` — all Slice 1 / 1.5
   lifecycle fields).

Safety
------

- Pool snapshot is taken to
  ``data/triage_pool/pool.json.pre-slice-3-<ts>`` before any write.
- Dry-run mode prints the verdict for each item without writing.
- ``--max-items`` caps how many entries get processed (default: all).
- ``--source`` filters to one source kind (e.g. ``journal_thread``).
- Idempotent: re-running on already-migrated entries (verdicts that
  no longer carry ``raw=True``) skips them silently. Use ``--force``
  to re-run the verdict pass even on already-migrated entries.

Cost
----

Each entry processed makes:
- One Haiku call (deadline extraction; cheap).
- One Sonnet call (main Clarify pass; the entire pool currently has
  ~30 raw entries, so ~30 Sonnet calls — a few cents).
- Possibly one Opus call per entry on backend / validation escalation.

Approve the spend before running. Use ``--max-items 5`` for a probe.

Usage
-----

::

    # Probe: process 5 raw entries, dry-run, print what would happen.
    python scripts/migrate_pool_slice_3.py --dry-run --max-items 5

    # Probe with writes: process 5 entries, persist results.
    python scripts/migrate_pool_slice_3.py --max-items 5

    # Full migration:
    python scripts/migrate_pool_slice_3.py

    # Re-run the verdict pass on every entry (force):
    python scripts/migrate_pool_slice_3.py --force

Once the migration is clean, flip the verdict-pass gate back on::

    # config.local.yaml
    triage:
      verdict_pass:
        enabled: true

so new captures get verdicted in the producer's normal flow.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.clarify.background import STATE_PENDING, get_pool
from work_buddy.clarify.deadline_extract import (
    extract_deadline_hints,
    merge_hints_into_records,
)
from work_buddy.clarify.items import TriageItem
from work_buddy.clarify.verdict_call import call_for_verdict
from work_buddy.clarify.verdict_schema import MULTI_RECORD_VERDICT_SCHEMA
from work_buddy.llm import LLMRunner, ModelTier


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_journal_prompt(item: TriageItem, hints: dict[str, Any]) -> str:
    """Build a per-item prompt for journal-style migration entries.

    Uses the same shape as ``journal_triage_scan._render_item_prompt``
    minus the User's-Current-Context block (we don't rebuild the
    full registry here — the migration is an offline batch and the
    rationale is allowed to be slightly less context-rich than a
    live capture's).
    """
    hints_block = ""
    if hints.get("hint_extraction_failed"):
        hints_block = (
            "\nDeadline hints: extraction failed; rely on the text "
            "itself.\n"
        )
    elif hints.get("has_deadline") or hints.get("has_dependency"):
        parts = []
        if hints.get("has_deadline"):
            parts.append(
                f"deadline: {hints.get('deadline_date') or '(unspecified)'}"
            )
        if hints.get("has_dependency"):
            parts.append(
                f"dependency: {hints.get('dependency_hint') or '(unspecified)'}"
            )
        hints_block = f"\nDeadline hints (pre-extracted): {'; '.join(parts)}\n"
    else:
        hints_block = "\nDeadline hints: none detected.\n"

    return (
        f"Item id: {item.id}\n"
        f"Source: {item.source}\n"
        f"{hints_block}"
        f"\n--- Captured text ---\n"
        f"{(item.text or '').strip()}\n"
        f"--- End ---\n"
        f"\n(This is a migration pass — the entry was captured before "
        f"the Slice 3 Clarify schema shipped. Produce the multi-record "
        f"verdict using your best judgment from the text alone.)\n"
    )


# Reuse the journal Clarify system prompt so migration verdicts have
# the same teaching as live verdicts. Imported lazily so the script
# doesn't fail at import time if the producer module is missing.
def _system_prompt() -> str:
    from work_buddy.clarify.capabilities.journal_triage_scan import (
        _AGENT_SYSTEM_PROMPT,
    )
    return _AGENT_SYSTEM_PROMPT


def _run_clarify_for_entry(
    entry: dict[str, Any],
    runner: LLMRunner,
    tier: ModelTier,
) -> dict[str, Any] | None:
    """Run deadline + Sonnet pipeline. Return the new verdict dict.

    Returns ``None`` on backend / validation failure (caller logs and
    leaves the entry unchanged so a later run can retry).
    """
    item_dict = entry.get("item") or {}
    item = TriageItem(
        id=entry.get("item_id", "") or "",
        text=item_dict.get("text", "") or "",
        label=item_dict.get("label", "") or entry.get("item_id", ""),
        source=entry.get("source", "") or "",
        url=item_dict.get("url"),
        metadata=item_dict.get("metadata", {}) or {},
    )

    if not item.text.strip():
        # Can't classify empty text — leave the entry as raw and skip.
        return None

    hints = extract_deadline_hints(
        item.text,
        message_date=entry.get("created_at", "")[:10] or None,
        item_id=item.id,
    )
    user_prompt = _build_journal_prompt(item, hints)
    resp = call_for_verdict(
        runner=runner,
        tier=tier,
        system=_system_prompt(),
        user=user_prompt,
        output_schema=MULTI_RECORD_VERDICT_SCHEMA,
        required_fields=("rationale", "group_intent"),
        caller="migrate_slice_3",
        item_id=item.id,
    )
    if resp.is_error():
        print(
            f"  [{item.id}] LLM error tier={resp.tier_used} "
            f"kind={resp.error_kind} — {resp.error}"
        )
        return None

    verdict = resp.structured_output or {}
    if "records" in verdict:
        verdict["records"] = merge_hints_into_records(
            verdict.get("records"), hints,
        )
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print verdict for each item without writing.",
    )
    parser.add_argument(
        "--max-items", type=int, default=None,
        help="Cap how many raw entries get processed (default: all).",
    )
    parser.add_argument(
        "--source", default=None,
        help="Filter by source kind (e.g. journal_thread).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-run the verdict pass even on already-migrated entries.",
    )
    parser.add_argument(
        "--tier", default="frontier_balanced",
        help="Starting model tier (default frontier_balanced).",
    )
    args = parser.parse_args(argv)

    pool = get_pool()
    index_path = pool._index_path
    if not index_path.exists():
        print(f"No pool index at {index_path} — nothing to migrate.")
        return 0

    try:
        tier = ModelTier(args.tier)
    except ValueError:
        print(f"Unknown tier {args.tier!r}. Valid: {[t.value for t in ModelTier]}")
        return 2

    raw_data = json.loads(index_path.read_text(encoding="utf-8"))
    entries = raw_data.get("entries", [])

    # Filter to candidates.
    def _is_candidate(e: dict[str, Any]) -> bool:
        if e.get("state") != STATE_PENDING:
            return False
        if args.source and e.get("source") != args.source:
            return False
        verdict = e.get("verdict") or {}
        if args.force:
            return True
        return bool(verdict.get("raw"))

    candidates = [e for e in entries if _is_candidate(e)]
    if args.max_items is not None:
        candidates = candidates[: args.max_items]

    print(
        f"Pool has {len(entries)} total entries; "
        f"{len(candidates)} match the migration filter."
    )
    if not candidates:
        return 0

    if not args.dry_run:
        backup_path = index_path.with_suffix(
            f".pre-slice-3-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )
        shutil.copy2(index_path, backup_path)
        print(f"Snapshot: {backup_path}")

    runner = LLMRunner()
    migrated = 0
    skipped = 0
    failed = 0

    for entry in candidates:
        item_id = entry.get("item_id", "") or "?"
        print(f"\nProcessing {item_id} ({entry.get('source')})")
        new_verdict = _run_clarify_for_entry(entry, runner, tier)
        if new_verdict is None:
            failed += 1
            continue
        # Print summary
        records = new_verdict.get("records") or []
        refusal = new_verdict.get("refusal")
        if refusal:
            print(f"  → REFUSAL: {refusal.get('question')}")
        elif records:
            for r in records:
                print(f"  → {r.get('destination')}")
        else:
            print(f"  → empty records (leave-equivalent)")

        if args.dry_run:
            skipped += 1
            continue

        # Write back to entries (in-memory; persist at end).
        entry["verdict"] = new_verdict
        # Mark migration timestamp on the entry for audit.
        entry["migrated_at"] = _now_iso()
        entry["migrated_by"] = "slice-3"
        migrated += 1

    if not args.dry_run and migrated:
        index_path.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")
        print(f"\nWrote {migrated} migrated entries to {index_path}")

    print(
        f"\nDone. migrated={migrated} dry_run_skipped={skipped} failed={failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
