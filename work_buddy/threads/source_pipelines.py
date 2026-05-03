"""Source-pipeline → v5 Thread spawn helpers.

Stage 4.12 + 4.13 deliverable. The journal scanner and Chrome
triage scanner are existing v4 producers. This module provides
the bridge: take their output (TriageItem-shaped dicts) and
spawn v5 Threads with proper inciting_event_summary so cleanup
adapters can later mutate the source.

UX.md §15 Stage 4.12 (journal) + 4.13 (Chrome).

The helpers are intentionally thin — they don't replace the
existing v4 pipelines; they layer on top. v4 paths still produce
PoolEntries during transition; v5 paths produce Threads.
Stage 4.14 deletes the v4 paths.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_INCITING_EVENT,
    KIND_INTENT_INFERRED,
    KIND_SUBTHREADS_SPAWNED,
    KIND_THREAD_CREATED,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Journal-source spawner
# ---------------------------------------------------------------------------


def spawn_thread_from_journal_item(
    triage_item: dict[str, Any],
    *,
    note_path: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> Optional[str]:
    """Create a v5 Thread from a journal-source TriageItem.

    Args:
        triage_item: dict shape — id, text, label, source='journal_thread',
            metadata{thread_id, line_count, journal_date, ...}
        note_path: vault-relative path to the inciting note. If None,
            derive from metadata.journal_date as 'journal/<date>.md'.
            (Matches work_buddy/journal.py's vault-rel path
            convention.)

    Returns:
        New v5 thread_id on success; None on failure (logged).
    """
    if not isinstance(triage_item, dict):
        return None
    md = triage_item.get("metadata") or {}
    if note_path is None:
        journal_date = md.get("journal_date")
        if not journal_date:
            logger.warning(
                "spawn_thread_from_journal_item: no journal_date in metadata "
                "and no note_path passed; can't determine source",
            )
            return None
        note_path = f"journal/{journal_date}.md"

    raw_text = triage_item.get("text") or ""
    label = triage_item.get("label") or triage_item.get("id") or "(journal item)"

    # The cleanup adapter matches by exact line_text. For multi-line
    # threads this is approximate — the adapter walks per-line, so
    # passing the first non-empty line gives a usable handle. The
    # adapter also returns source_already_gone if exact match fails,
    # so a stale or edited journal won't error.
    first_line = next(
        (ln.strip() for ln in raw_text.split("\n") if ln.strip()),
        raw_text.strip(),
    )

    inciting = {
        "source": "journal_note",
        "note_path": note_path,
        "line_text": first_line,
        "journal_date": md.get("journal_date"),
        "thread_id_hint": md.get("thread_id"),
        "line_count": md.get("line_count"),
        "label": label,
        "description": label,
    }

    ctx_item = ContextItem(
        id=triage_item.get("id") or "journal_item",
        source="journal_note",
        type="todo_line",
        label=label,
        payload={"raw_text": raw_text[:500]},
    )

    # Apply default autonomy (PLAN_THEN_REVIEW unless overridden in
    # config). The bare AutonomyPolicy() default would block every
    # wait state and force the user to confirm every inference step.
    from work_buddy.threads.autonomy import default_spawn_policy
    thread = Thread(
        parent_id=parent_id,
        context_items=(ctx_item,),
        inciting_event_summary=inciting,
        autonomy_policy=default_spawn_policy(),
    )
    try:
        store.insert_thread(thread)
        # Inciting event + thread_created
        e1 = store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=thread.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={"source_pipeline": "journal_triage_scan"},
            parent_event_id=e1.id,
        ))
        # Bump the cache's parent_event_id so the next transition has
        # the right optimistic-lock target.
        store.update_thread_state(
            thread.thread_id,
            parent_event_id=store.latest_event_id(thread.thread_id),
        )
        # Kickoff transition PROPOSED -> AWAITING_INFERENCE. Fires
        # the bootstrap-registered handler that enqueues into the
        # LLM-call queue. Without this, the thread dead-ends.
        _kickoff_inference(thread.thread_id)
        return thread.thread_id
    except Exception as e:
        logger.warning(
            "spawn_thread_from_journal_item: insert failed: %s", e,
        )
        return None


def _kickoff_inference(thread_id: str) -> None:
    """Fire PROPOSED -> AWAITING_INFERENCE for a freshly-spawned
    Thread. Non-fatal — logs on failure (the thread is already
    persisted; user can manually trigger via dashboard later)."""
    try:
        from work_buddy.threads import engine
        from work_buddy.threads.fsm import TRIG_BEGIN_INFERENCE
        engine.transition(
            thread_id, TRIG_BEGIN_INFERENCE,
            actor="inciting",
            fire_side_effects=True,
        )
    except Exception as e:
        logger.warning(
            "_kickoff_inference for %s failed: %s — thread will sit "
            "in PROPOSED until manually advanced",
            thread_id, e,
        )


def spawn_parent_thread_from_journal_scan(
    *,
    journal_date: str,
    item_count: int,
    scan_id: Optional[str] = None,
) -> Optional[str]:
    """Create the parent "scan" Thread for a journal scan.

    User-feedback fix #3 (2026-05-03 morning): a journal scan is
    a single conceptual unit that produces N TODO-line items. The
    user observed that each TODO line should be a SUB-THREAD under
    a parent "scan" thread, not a top-level thread.

    The parent has known intent + action (no LLM needed):
    - Intent: "Process daily notes" (confidence 1.0). Generic
      because the same parent shape is used regardless of which
      day(s) the scan covered — distinguishing context lives in
      `inciting.title` ("Journal scan: 2026-04-30").
    - Action: standard "decompose" (confidence 1.0).

    The parent sits in MONITORING from the start; it never goes
    through inference. As children reach terminal states the
    cascade-on-terminal handler (decompose.cascade_terminal_to_parent)
    advances the parent to DONE when all are terminal. Standard
    decompose pattern.

    Returns the new parent thread_id, or None on failure.
    """
    if scan_id is None:
        scan_id = uuid.uuid4().hex[:8]
    title = f"Journal scan: {journal_date}"
    description = f"{title} ({item_count} item{'s' if item_count != 1 else ''})"
    inciting = {
        "source": "journal_scan",
        "title": title,
        "description": description,
        "journal_date": journal_date,
        "scan_id": scan_id,
        "item_count": item_count,
    }
    from work_buddy.threads.autonomy import default_spawn_policy
    # Insert directly in MONITORING — the parent is a structural
    # container, not a thing that goes through inference. This
    # mirrors what decompose_thread does for parents that fan out
    # via the decompose action.
    parent = Thread(
        fsm_state=FSMState.MONITORING,
        inciting_event_summary=inciting,
        autonomy_policy=default_spawn_policy(),
    )
    try:
        store.insert_thread(parent)
        # Inciting + thread_created
        e1 = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={"source_pipeline": "journal_v5_scan"},
            parent_event_id=e1.id,
        ))
        # Pre-record the known intent + action. We use confidence
        # 1.0 because these aren't inferred — they're definitionally
        # true for any journal scan. actor=inciting (not agent)
        # since no LLM was consulted.
        intent_event = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="inciting",
            data={
                "target": "intent",
                "payload": {
                    "intent": "Process daily notes",
                },
                "confidence": 1.0,
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
            },
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="inciting",
            data={
                "target": "action",
                "payload": {
                    "kind": "standard",
                    "name": "decompose",
                    "plan_summary": (
                        "Spawn one sub-thread per inciting line"
                    ),
                    "irreversibility": "low",
                    "regret_potential": "low",
                    "risk_amplifier": False,
                },
                "confidence": 1.0,
                "tier_used": None,
                "model_used": None,
                "synthetic": True,
            },
            parent_event_id=intent_event.id,
        ))
        # Bump cache parent_event_id so any later writes have the
        # right optimistic-lock target.
        store.update_thread_state(
            parent.thread_id,
            parent_event_id=store.latest_event_id(parent.thread_id),
        )
        return parent.thread_id
    except Exception as e:
        logger.warning(
            "spawn_parent_thread_from_journal_scan: failed: %s", e,
        )
        return None


def spawn_threads_from_journal_scan(
    items: list[dict[str, Any]],
    *,
    journal_date: Optional[str] = None,
) -> dict[str, Any]:
    """Spawn the journal-scan thread tree: 1 parent + N sub-threads.

    User-feedback fix #3 (2026-05-03 morning): each TODO line is
    now a sub-thread under a per-scan parent, not a standalone
    top-level thread.

    Args:
        items: list of TriageItem-shaped dicts from the journal
            adapter.
        journal_date: ``YYYY-MM-DD``; required for the parent's
            inciting summary.

    Returns:
        ``{
            "parent_id": str,
            "sub_thread_ids": [str],
            "count": int,
        }``
        Sub-threads that fail to spawn are skipped (logged); count
        reflects successful spawns.

    Returns a count-zero dict if the parent itself fails to spawn
    (the per-line spawn calls were never made).

    The empty-items case still produces a parent (count=0) — the
    user can see "we ran the scan and there was nothing actionable
    today" rather than an unrendered void.
    """
    if journal_date is None:
        # Best-effort fallback: derive from the first item's metadata.
        if items:
            md = (items[0] or {}).get("metadata") or {}
            journal_date = md.get("journal_date")
    if journal_date is None:
        # Fall back to "unknown" — the cleanup adapter still works
        # because each child carries its own note_path.
        journal_date = "unknown"
    parent_id = spawn_parent_thread_from_journal_scan(
        journal_date=journal_date,
        item_count=len(items),
    )
    if parent_id is None:
        return {"parent_id": None, "sub_thread_ids": [], "count": 0}
    note_path = f"journal/{journal_date}.md"
    sub_ids: list[str] = []
    for item in items:
        tid = spawn_thread_from_journal_item(
            item,
            note_path=note_path,
            parent_id=parent_id,
        )
        if tid is not None:
            sub_ids.append(tid)
    # Record subthreads_spawned on the parent so the audit trail
    # is consistent with the canonical decompose pattern.
    if sub_ids:
        try:
            from work_buddy.threads.linearization import (
                linearize_after_spawn,
            )
            store.append_event(ThreadEvent(
                thread_id=parent_id,
                kind=KIND_SUBTHREADS_SPAWNED,
                actor="inciting",
                data={
                    "child_thread_ids": sub_ids,
                    "source_count": len(sub_ids),
                    "source_pipeline": "journal_v5_scan",
                },
            ))
            # Update parent_event_id and run linearization so the
            # children get sensible order_index values.
            store.update_thread_state(
                parent_id,
                parent_event_id=store.latest_event_id(parent_id),
            )
            try:
                linearize_after_spawn(parent_id)
            except Exception as e:
                logger.warning(
                    "linearize_after_spawn for journal parent %s "
                    "failed: %s; siblings keep order_index=0",
                    parent_id, e,
                )
        except Exception as e:
            logger.warning(
                "subthreads_spawned event for journal parent %s "
                "failed: %s",
                parent_id, e,
            )
    return {
        "parent_id": parent_id,
        "sub_thread_ids": sub_ids,
        "count": len(sub_ids),
    }


# ---------------------------------------------------------------------------
# Chrome-source spawner (Stage 4.13 — also lives here for symmetry)
# ---------------------------------------------------------------------------


def spawn_parent_thread_from_chrome_scrape(
    *,
    scrape_id: Optional[str] = None,
    summary: Optional[str] = None,
) -> Optional[str]:
    """Create the parent Thread for a Chrome triage scrape.

    Returns the new thread_id. Sub-Threads are spawned via
    decompose with the scraped tabs as source items.
    """
    inciting = {
        "source": "chrome_scrape",
        "scrape_id": scrape_id,
        "description": summary or "Chrome triage",
        "title": summary or "Chrome triage",
    }
    from work_buddy.threads.autonomy import default_spawn_policy
    parent = Thread(
        inciting_event_summary=inciting,
        autonomy_policy=default_spawn_policy(),
    )
    try:
        store.insert_thread(parent)
        e1 = store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_INCITING_EVENT,
            actor="inciting",
            data=inciting,
        ))
        store.append_event(ThreadEvent(
            thread_id=parent.thread_id,
            kind=KIND_THREAD_CREATED,
            actor="inciting",
            data={"source_pipeline": "chrome_triage"},
            parent_event_id=e1.id,
        ))
        store.update_thread_state(
            parent.thread_id,
            parent_event_id=store.latest_event_id(parent.thread_id),
        )
        # NOTE: chrome_scrape parent does NOT kickoff inference —
        # the inference target for a "scrape root" isn't well-defined;
        # the meaningful work is on the per-tab sub-threads spawned
        # via decompose. The decompose path leaves the parent in
        # MONITORING (not PROPOSED), so no kickoff needed here. Each
        # spawned sub-thread (in PROPOSED) gets its own kickoff via
        # _kickoff_inference if the spawner chooses (Stage 4.13's
        # spawn_threads_from_chrome_scrape calls decompose_thread,
        # which spawns sub-threads in PROPOSED — those need
        # kickoff too; see decompose's own integration).
        return parent.thread_id
    except Exception as e:
        logger.warning(
            "spawn_parent_thread_from_chrome_scrape: failed: %s", e,
        )
        return None


def chrome_tab_to_context_item(tab: dict[str, Any]) -> ContextItem:
    """Convert a chrome-tab dict into a ContextItem suitable for
    decompose. Used by the Chrome pipeline (Stage 4.13)."""
    tab_id = tab.get("id") or tab.get("tab_id") or "tab"
    title = tab.get("title") or tab.get("url") or tab_id
    return ContextItem(
        id=str(tab_id),
        source="chrome_tab",
        type="tab",
        label=str(title),
        payload={
            "url": tab.get("url"),
            "title": title,
            "window_id": tab.get("window_id"),
            "group_id": tab.get("group_id"),
            "tab_index": tab.get("tab_index"),
        },
    )


def spawn_threads_from_chrome_scrape(
    *,
    tabs: list[dict[str, Any]],
    scrape_id: Optional[str] = None,
    summary: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """End-to-end Chrome scrape → v5 Thread tree.

    Creates the parent Thread (chrome_scrape inciting source) AND
    spawns sub-Threads via the decompose action (one per tab).
    Stage 4.7's write-time linearization runs automatically inside
    decompose so the resulting sub-thread order is semantic.

    Returns:
        {
            'parent_id': str,
            'sub_thread_ids': list[str],
            'count': int,
        }
        or None on failure (logged).
    """
    if not tabs:
        return None
    parent_id = spawn_parent_thread_from_chrome_scrape(
        scrape_id=scrape_id, summary=summary,
    )
    if parent_id is None:
        return None
    try:
        from work_buddy.threads.decompose import decompose_thread
        ctx_items = [chrome_tab_to_context_item(t) for t in tabs]
        sub_ids = decompose_thread(
            parent_id, ctx_items,
            inciting_summary_extra={
                "scrape_id": scrape_id,
                "source_pipeline": "chrome_triage",
            },
        )
        return {
            "parent_id": parent_id,
            "sub_thread_ids": sub_ids,
            "count": len(sub_ids),
        }
    except Exception as e:
        logger.warning(
            "spawn_threads_from_chrome_scrape: decompose failed: %s", e,
        )
        return {
            "parent_id": parent_id,
            "sub_thread_ids": [],
            "count": 0,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Chrome-tab cleanup adapter (stub — closing tabs via the existing
# Chrome native-messaging host is not currently supported; the host
# only exports tab state. This adapter ships as a placeholder that
# returns a clean "not yet implemented" failure so the UI's Clean
# Up button on Chrome tabs surfaces honestly.)
# ---------------------------------------------------------------------------


def _chrome_tab_can_clean_up(thread) -> bool:  # type: ignore[no-untyped-def]
    summary = getattr(thread, "inciting_event_summary", None) or {}
    # We say "yes we can" so the UI shows the button; the cleanup
    # call returns a friendly failure detail. This is intentionally
    # discoverable: users learn the gap and can ask for it to be
    # implemented.
    return summary.get("source") == "chrome_tab"


def _chrome_tab_cleanup(thread):  # type: ignore[no-untyped-def]
    from work_buddy.threads.cleanup import CleanupResult
    return CleanupResult(
        success=False,
        detail=(
            "Chrome tab cleanup is not yet wired (the extension's "
            "native-messaging host is export-only today). Tab will "
            "remain open; please close it manually."
        ),
    )


def register_chrome_tab_cleanup_adapter() -> None:
    """Register the Chrome-tab cleanup adapter. Bootstrap calls this
    alongside the journal adapter (Stage 4.4 + 4.13)."""
    from work_buddy.threads.cleanup import (
        CleanupAdapter, register_cleanup_adapter,
    )
    register_cleanup_adapter(CleanupAdapter(
        source="chrome_tab",
        can_clean_up=_chrome_tab_can_clean_up,
        cleanup=_chrome_tab_cleanup,
        description="(stub) close the Chrome tab — not yet wired.",
    ))
