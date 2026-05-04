"""Universal thread actions — primitives that apply to any thread
regardless of source.

These are the implementations behind the universal-action capabilities
(``thread_dismiss``, ``thread_defer``, ``thread_rename``) registered in
the capability registry with ``is_action=True``. They surface in:

- The dashboard's per-group action chip dropdown (alongside any
  source-specific actions).
- :func:`work_buddy.pipelines.refine_clusters` as candidate proposals
  the LLM may pick.
- Any other code path that walks the action catalog (action inference
  fallbacks, search, etc.).

Implementation notes
--------------------

- ``thread_dismiss`` runs the standard FSM ``TRIG_DISMISSED_BY_USER``
  transition. Side effects (cascade on terminal entry, etc.) flow
  through the existing engine handlers.
- ``thread_defer`` sets the cached ``resurface_at`` field. The Stage 4
  Later mechanic already handles re-surfacing when that timestamp is
  reached.
- ``thread_rename`` rewrites ``inciting_event_summary["title"]`` (and
  ``description``, since the dashboard reads either). Records a
  rename event for audit; no FSM transition.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from work_buddy.threads import engine, store
from work_buddy.threads.events import (
    ACTOR_USER,
    ThreadEvent,
)
from work_buddy.threads.fsm import TRIG_DISMISSED_BY_USER

logger = logging.getLogger(__name__)


# Custom event kind for rename audit. Kept here rather than in
# events.py because it's narrowly scoped to this module.
KIND_THREAD_RENAMED = "thread_renamed"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UniversalActionError(ValueError):
    """An invariant of a universal action was violated (thread not
    found, target already terminal, etc.)."""


# ---------------------------------------------------------------------------
# thread_dismiss
# ---------------------------------------------------------------------------


def thread_dismiss(
    thread_id: str,
    *,
    reason: str | None = None,
    actor: str = ACTOR_USER,
) -> dict[str, Any]:
    """Mark ``thread_id`` as DISMISSED via the standard FSM transition.

    Returns ``{"thread_id": str, "previous_state": str, "new_state": "dismissed"}``.

    Raises :class:`UniversalActionError` if the thread isn't found OR
    is already terminal.
    """
    thread = store.get_thread(thread_id)
    if thread is None:
        raise UniversalActionError(f"Thread {thread_id!r} not found")
    if thread.is_terminal:
        raise UniversalActionError(
            f"Thread {thread_id!r} already terminal "
            f"(state={thread.fsm_state.value!r})",
        )
    previous = thread.fsm_state.value
    try:
        result = engine.transition(
            thread_id,
            TRIG_DISMISSED_BY_USER,
            data={"reason": reason or "user_dismiss"},
            actor=actor,
            fire_side_effects=True,
        )
    except engine.InvalidTransition as e:
        raise UniversalActionError(
            f"Could not dismiss {thread_id!r}: {e}",
        ) from e
    return {
        "thread_id": thread_id,
        "previous_state": previous,
        "new_state": result.next_state.value,
    }


# ---------------------------------------------------------------------------
# thread_defer
# ---------------------------------------------------------------------------


def thread_defer(
    thread_id: str,
    *,
    duration_hours: float | None = None,
    resurface_at: str | None = None,
    actor: str = ACTOR_USER,
) -> dict[str, Any]:
    """Defer ``thread_id`` so it resurfaces at a future time.

    Either supply ``duration_hours`` (relative; default 24h if both
    args are missing) or ``resurface_at`` (ISO timestamp). The cached
    ``resurface_at`` field is what the existing Stage 4 Later mechanic
    reads.

    Returns ``{"thread_id": str, "resurface_at": str}``.
    """
    thread = store.get_thread(thread_id)
    if thread is None:
        raise UniversalActionError(f"Thread {thread_id!r} not found")
    target_iso = _resolve_resurface_at(duration_hours, resurface_at)
    store.update_thread_state(
        thread_id,
        resurface_at=target_iso,
    )
    # Audit event so the timeline reflects the defer.
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind="later",  # KIND_LATER from events.py
        actor=actor,
        data={"resurface_at": target_iso},
        parent_event_id=thread.parent_event_id,
    ))
    store.update_thread_state(
        thread_id,
        parent_event_id=store.latest_event_id(thread_id),
    )
    return {"thread_id": thread_id, "resurface_at": target_iso}


def _resolve_resurface_at(
    duration_hours: float | None,
    resurface_at: str | None,
) -> str:
    if resurface_at:
        return resurface_at
    hours = duration_hours if duration_hours is not None else 24.0
    target = datetime.now(timezone.utc) + timedelta(hours=hours)
    return target.isoformat()


# ---------------------------------------------------------------------------
# thread_rename
# ---------------------------------------------------------------------------


def thread_rename(
    thread_id: str,
    *,
    new_title: str,
    actor: str = ACTOR_USER,
) -> dict[str, Any]:
    """Rewrite ``inciting_event_summary["title"]`` (and ``description``)
    on ``thread_id``.

    Records a ``thread_renamed`` audit event. Does NOT transition the
    FSM. Used by the action chip's "Rename" affordance + by the LLM
    cluster-refinement step when it overrides an algorithmic cluster
    label.
    """
    cleaned = (new_title or "").strip()
    if not cleaned:
        raise UniversalActionError("new_title required (non-empty)")

    thread = store.get_thread(thread_id)
    if thread is None:
        raise UniversalActionError(f"Thread {thread_id!r} not found")

    summary = dict(thread.inciting_event_summary or {})
    previous_title = summary.get("title")
    summary["title"] = cleaned
    summary["description"] = cleaned

    # Persist the new summary by rewriting the inciting-event-summary
    # JSON field. The store doesn't expose a single-field setter, so
    # we use the rename event as the canonical record + bump the
    # cached parent_event_id; downstream renderers read the title from
    # the inciting_event_summary cached on the row, which we update
    # via a small helper (added to store if not present).
    _set_inciting_event_summary(thread_id, summary)

    # Audit event
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind=KIND_THREAD_RENAMED,
        actor=actor,
        data={
            "previous_title": previous_title,
            "new_title": cleaned,
        },
        parent_event_id=thread.parent_event_id,
    ))
    store.update_thread_state(
        thread_id,
        parent_event_id=store.latest_event_id(thread_id),
    )

    return {
        "thread_id": thread_id,
        "previous_title": previous_title,
        "new_title": cleaned,
    }


def _set_inciting_event_summary(
    thread_id: str, summary: dict[str, Any],
) -> None:
    """Direct write to the cached ``inciting_event_summary_json`` column.

    The store doesn't currently expose this as a typed setter; the
    ``rename`` action is the only caller that needs it, so we do the
    SQL inline rather than extend the public store API. If a second
    caller arrives, promote this to ``store.update_thread_state``.
    """
    import json as _json
    conn = store.get_connection()
    try:
        conn.execute(
            "UPDATE threads SET inciting_event_summary_json = ?, "
            "updated_at = ? WHERE thread_id = ?",
            (
                _json.dumps(summary),
                datetime.now(timezone.utc).isoformat(),
                thread_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
