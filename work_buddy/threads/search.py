"""Search-blob maintenance + filtered query for Threads.

The search blob is a denormalized substring-searchable text field
on each Thread, rebuilt on state changes. Substring matching only;
richer (semantic / full-text) is open work.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
)
from work_buddy.threads.models import Thread

_ACTIONABLE_STATE_VALUES: tuple[str, ...] = tuple(
    s.value for s in FSMState if s.is_wait_state
) + (
    # User-feedback (2026-05-03 morning): MONITORING parents aren't
    # directly actionable themselves, but they're CONTAINERS for
    # actionable children (journal-scan parent, chrome-scrape
    # parent). Hiding them from the top-level list means the user
    # can't see "I have 3 journal items to resolve" at a glance.
    # Include MONITORING in the default actionable filter so
    # parent threads with pending children stay visible.
    FSMState.MONITORING.value,
)

# Mid-process states: the FSM is doing work but the user has nothing
# to act on. These are NOT in is_wait_state. Hidden from the default
# threads list; surfaced when the user toggles "Show mid-process"
# (Phase 4 of the autonomy plan) to see what's currently in flight.
_MID_PROCESS_STATE_VALUES: tuple[str, ...] = (
    FSMState.AWAITING_INFERENCE.value,
    FSMState.INFERRING_INTENT.value,
    FSMState.INFERRING_CONTEXT.value,
    FSMState.INFERRING_ACTION.value,
    FSMState.EXECUTING.value,
    FSMState.MONITORING.value,
    FSMState.CLEANING_UP.value,
)

logger = logging.getLogger(__name__)


def build_search_blob(thread: Thread, *, conn=None) -> str:
    """Compose the search-blob for a Thread.

    Per UX.md §10.1 — high-yield, short text only:
    - Inciting summary description / title
    - Latest intent inferred
    - Latest action proposal name + plan_summary
    - Context item labels

    NOT searchable: full event log content, action body fields
    (email body, task description), namespace tags.
    """
    parts: list[str] = []

    # Inciting summary
    summary = thread.inciting_event_summary or {}
    for key in ("description", "title", "summary"):
        v = summary.get(key)
        if isinstance(v, str) and v:
            parts.append(v)

    # Context item labels
    for ci in thread.context_items:
        if ci.label:
            parts.append(ci.label)

    # Latest *_inferred events
    events = store.list_events(thread.thread_id, conn=conn)
    for e in reversed(events):
        if e.kind == KIND_INTENT_INFERRED:
            payload = e.data.get("payload") or {}
            intent = payload.get("intent")
            if isinstance(intent, str):
                parts.append(intent)
            break
    for e in reversed(events):
        if e.kind == KIND_ACTION_INFERRED:
            payload = e.data.get("payload") or {}
            name = payload.get("name")
            if isinstance(name, str):
                parts.append(name)
            ps = payload.get("plan_summary")
            if isinstance(ps, str):
                parts.append(ps)
            params = payload.get("parameters") or {}
            # Only include short string parameters (titles, subjects).
            for pkey in ("title", "subject", "description"):
                v = params.get(pkey)
                if isinstance(v, str) and len(v) < 200:
                    parts.append(v)
            break

    # Single space-joined string, lowercased for case-insensitive search
    return " ".join(parts).lower()


def update_search_blob(thread_id: str, *, conn=None) -> Optional[str]:
    """Recompute + persist the search blob for ``thread_id``.

    Returns the new blob text, or None if the thread doesn't exist.
    """
    thread = store.get_thread(thread_id, conn=conn)
    if thread is None:
        return None
    blob = build_search_blob(thread, conn=conn)
    store.update_thread_state(thread_id, search_blob=blob, conn=conn)
    return blob


# ---------------------------------------------------------------------------
# Search query
# ---------------------------------------------------------------------------


def _build_thread_filter_clauses(
    query: str,
    *,
    parent_id: Optional[str],
    state: Optional[str],
    subtype: Optional[str],
    show_later: bool,
    actionable_only: bool,
    include_mid_process: bool,
) -> tuple[list[str], list[Any]]:
    """Compose the WHERE clauses + params shared by
    :func:`search_threads` and :func:`count_threads`.

    Kept private so the two callers cannot diverge on filter semantics
    — a count that disagrees with the listed rows is worse than no
    count at all.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if state is not None:
        clauses.append("fsm_state = ?")
        params.append(state)
    elif actionable_only and parent_id is None:
        allowed = list(_ACTIONABLE_STATE_VALUES)
        if include_mid_process:
            allowed.extend(_MID_PROCESS_STATE_VALUES)
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"fsm_state IN ({placeholders})")
        params.extend(allowed)
    if subtype is not None:
        clauses.append("subtype IS ?")
        params.append(subtype)
    if parent_id is not None:
        clauses.append("parent_id = ?")
        params.append(parent_id)
    elif parent_id is None:
        # top-level only
        clauses.append("parent_id IS NULL")
    if not show_later:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        clauses.append("(resurface_at IS NULL OR resurface_at <= ?)")
        params.append(now)
    if query:
        # Case-insensitive substring (search_blob is stored
        # lowercased so we lowercase the query)
        clauses.append("search_blob LIKE ?")
        params.append("%" + query.lower() + "%")
    return clauses, params


def search_threads(
    query: str,
    *,
    parent_id: Optional[str] = None,
    state: Optional[str] = None,
    subtype: Optional[str] = None,
    show_later: bool = False,
    actionable_only: bool = True,
    include_mid_process: bool = False,
    limit: int = 50,
    offset: int = 0,
    conn=None,
) -> list[Thread]:
    """Substring-search top-level Threads (or sub-threads if
    parent_id is given).

    Filters compose AND-style: every set filter must hold. Empty
    ``query`` returns the unfiltered list (per filter chips).

    ``actionable_only`` (default True): top-level results are
    restricted to states where the user has something to do —
    wait states only. PROPOSED, INFERRING_*, EXECUTING,
    MONITORING, and terminal states are noise on the main
    list. The 'state' filter chip can override (an explicit
    state filter implies the user wants to see those even if
    not actionable).

    ``include_mid_process`` (default False): when True, the result
    set additionally includes threads in mid-process states
    (AWAITING_INFERENCE, INFERRING_*, EXECUTING, MONITORING,
    CLEANING_UP). Used by the dashboard's "Show mid-process"
    toggle so users can audit what's currently in flight without
    polluting the default actionable list. Has no effect when
    ``actionable_only`` is False (no filter is applied at all).

    ``offset`` (default 0): skip N matching rows before returning
    ``limit`` of them. Paired with :func:`count_threads` for paged
    listings.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        clauses, params = _build_thread_filter_clauses(
            query,
            parent_id=parent_id,
            state=state,
            subtype=subtype,
            show_later=show_later,
            actionable_only=actionable_only,
            include_mid_process=include_mid_process,
        )
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        if parent_id is not None:
            order = "ORDER BY order_index ASC, updated_at DESC"
        else:
            order = (
                "ORDER BY (resurface_at IS NULL) ASC, "
                "resurface_at DESC, updated_at DESC"
            )
        params.append(limit)
        params.append(offset)
        rows = conn.execute(
            f"SELECT * FROM threads {where} {order} LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [Thread.from_row(dict(r)) for r in rows]
    finally:
        if own_conn:
            conn.close()


def count_threads(
    query: str = "",
    *,
    parent_id: Optional[str] = None,
    state: Optional[str] = None,
    subtype: Optional[str] = None,
    show_later: bool = False,
    actionable_only: bool = True,
    include_mid_process: bool = False,
    conn=None,
) -> int:
    """Count threads matching the same filters as :func:`search_threads`.

    Used by paged listings to compute total page count without
    materializing every matching row.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        clauses, params = _build_thread_filter_clauses(
            query,
            parent_id=parent_id,
            state=state,
            subtype=subtype,
            show_later=show_later,
            actionable_only=actionable_only,
            include_mid_process=include_mid_process,
        )
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM threads {where}",
            params,
        ).fetchone()
        return int(row["n"]) if row is not None else 0
    finally:
        if own_conn:
            conn.close()
