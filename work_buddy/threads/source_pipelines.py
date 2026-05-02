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
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.events import (
    KIND_INCITING_EVENT,
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
) -> Optional[str]:
    """Create a v5 Thread from a journal-source TriageItem.

    Args:
        triage_item: dict shape — id, text, label, source='journal_thread',
            metadata{thread_id, line_count, journal_date, ...}
        note_path: vault-relative path to the inciting note. If None,
            derive from metadata.journal_date as 'Daily/<date>.md'.
            (Tunable via config in a future stage.)

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
        note_path = f"Daily/{journal_date}.md"

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

    thread = Thread(
        context_items=(ctx_item,),
        inciting_event_summary=inciting,
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
        return thread.thread_id
    except Exception as e:
        logger.warning(
            "spawn_thread_from_journal_item: insert failed: %s", e,
        )
        return None


def spawn_threads_from_journal_scan(
    items: list[dict[str, Any]],
    *,
    journal_date: Optional[str] = None,
) -> list[str]:
    """Spawn v5 Threads from a list of journal TriageItems.

    Returns the list of new thread_ids in the same order as input.
    Items that fail to spawn are skipped (logged).
    """
    note_path = f"Daily/{journal_date}.md" if journal_date else None
    out: list[str] = []
    for item in items:
        tid = spawn_thread_from_journal_item(item, note_path=note_path)
        if tid is not None:
            out.append(tid)
    return out


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
    parent = Thread(inciting_event_summary=inciting)
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
