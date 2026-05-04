"""Group-relationship operations — Stage 5 grouping pattern.

Parallel to :mod:`work_buddy.threads.decompose`, which owns the
decompose-relationship operations (subthreads_spawned + cascade-on-
terminal). This module owns the group-relationship operations:

- :func:`move_thread_to_parent` — rewrite a sub-thread's ``parent_id``
  so it sits under a different sibling group-parent. Records
  ``KIND_ITEM_MOVED`` events on both the old and the new parent
  (shared ``migration_id``); fires the empty-group auto-DISMISS
  cascade on the old parent if it has no children remaining.

- :func:`bulk_submit_group` — run the accept transition on every
  child of a group-parent that's currently in
  ``awaiting_confirmation``. Returns counts so the dashboard can
  toast "Submitted N items, M failed".

Validation guarantees:

- Both source and destination MUST be group-relationship parents
  (``parent_relationship == 'group'``). Moving sub-threads between
  decompose-parents would break the FSM contract (each decompose-
  parent's children are bound to a specific decompose action) and
  is rejected.

- Both parents MUST share the same ``originating_scrape_id``. This
  prevents accidental cross-scrape moves (e.g. a tab from yesterday's
  Chrome scrape landing in today's group). NULL == NULL is also
  rejected — group-parents without a scope id can't be siblings.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.events import (
    ACTOR_FSM_ENGINE,
    KIND_ITEM_MOVED,
    ThreadEvent,
)

logger = logging.getLogger(__name__)


class MoveValidationError(ValueError):
    """Raised when ``move_thread_to_parent`` rejects a move.

    Carries a short ``reason`` string suitable for surfacing back to
    the user via a toast / 4xx body.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def _validate_move(
    item, new_parent, old_parent,
) -> None:
    """Reject moves that would violate the group-relationship invariants.

    Rules (raise MoveValidationError on the first violation):
    1. ``new_parent`` must exist.
    2. ``new_parent`` must be a group-relationship parent.
    3. The item must currently have an ``old_parent`` that's also a
       group-relationship parent (otherwise the move is between
       unrelated trees, which we don't allow).
    4. Both parents must share a non-NULL ``originating_scrape_id``.
       NULL == NULL is rejected — group-parents without a scope id
       aren't siblings of anything.
    5. Source and destination must not be the same thread (no-op
       moves are silently dropped, but explicit same-id moves are
       a sign of a frontend bug — reject so we notice).
    """
    if new_parent is None:
        raise MoveValidationError(
            "destination_not_found",
            "Destination parent thread not found.",
        )
    if new_parent.parent_relationship != "group":
        raise MoveValidationError(
            "destination_not_group",
            "Destination parent is not a group-relationship parent.",
        )
    if old_parent is None:
        raise MoveValidationError(
            "source_orphan",
            "Item has no parent — moves require a source parent.",
        )
    if old_parent.parent_relationship != "group":
        raise MoveValidationError(
            "source_not_group",
            "Source parent is not a group-relationship parent. Items "
            "can only move between group-parents; decompose-parent "
            "children are bound to their parent's decompose action.",
        )
    src_scope = old_parent.originating_scrape_id
    dst_scope = new_parent.originating_scrape_id
    if not src_scope or not dst_scope:
        raise MoveValidationError(
            "missing_scrape_scope",
            "Both source and destination must have an "
            "originating_scrape_id (sibling-scope id) for moves to "
            "be valid.",
        )
    if src_scope != dst_scope:
        raise MoveValidationError(
            "scope_mismatch",
            f"Source scrape {src_scope!r} differs from destination "
            f"scrape {dst_scope!r}; moves between unrelated scrapes "
            "are rejected to prevent cross-run leakage.",
        )
    if old_parent.thread_id == new_parent.thread_id:
        raise MoveValidationError(
            "same_parent",
            "Source and destination are the same parent — no-op.",
        )


def move_thread_to_parent(
    thread_id: str,
    new_parent_id: str,
    *,
    actor: str = "user",
    conn=None,
) -> dict[str, Any]:
    """Move ``thread_id`` to a different sibling group-parent.

    Validates source and destination per :func:`_validate_move`,
    rewrites the item's ``parent_id`` cache, records ``KIND_ITEM_MOVED``
    events on both the old and the new parent (shared ``migration_id``
    so an auditor can correlate the two halves), and fires the
    empty-group auto-DISMISS cascade on the old parent.

    Returns:
        ``{"thread_id": str, "from_parent": str, "to_parent": str,
           "migration_id": str, "old_parent_dismissed": bool,
           "old_parent_state": str | None}``

    Raises :class:`MoveValidationError` on any invariant violation.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        item = store.get_thread(thread_id, conn=conn)
        if item is None:
            raise MoveValidationError(
                "item_not_found",
                f"Item thread {thread_id!r} not found.",
            )
        old_parent_id = item.parent_id
        old_parent = (
            store.get_thread(old_parent_id, conn=conn)
            if old_parent_id else None
        )
        new_parent = store.get_thread(new_parent_id, conn=conn)

        _validate_move(item, new_parent, old_parent)
        # _validate_move guarantees old_parent / new_parent are non-None
        # past this point.

        migration_id = uuid.uuid4().hex[:16]
        scrape_scope = old_parent.originating_scrape_id

        # 1. Rewrite the item's parent_id cache. The event-log
        #    move records below carry the canonical history; this
        #    keeps query-by-parent_id correct without a join walk.
        store.update_thread_state(
            thread_id, parent_id=new_parent_id, conn=conn,
        )

        # 2. Record KIND_ITEM_MOVED on the OLD parent (audit half 1).
        old_event_data = {
            "item_id": thread_id,
            "from_parent": old_parent_id,
            "to_parent": new_parent_id,
            "originating_scrape_id": scrape_scope,
            "direction": "outgoing",
        }
        store.append_event(
            ThreadEvent(
                thread_id=old_parent_id,
                kind=KIND_ITEM_MOVED,
                actor=actor,
                data=old_event_data,
                migration_id=migration_id,
            ),
            conn=conn,
        )

        # 3. Record KIND_ITEM_MOVED on the NEW parent (audit half 2).
        new_event_data = {
            "item_id": thread_id,
            "from_parent": old_parent_id,
            "to_parent": new_parent_id,
            "originating_scrape_id": scrape_scope,
            "direction": "incoming",
        }
        store.append_event(
            ThreadEvent(
                thread_id=new_parent_id,
                kind=KIND_ITEM_MOVED,
                actor=actor,
                data=new_event_data,
                migration_id=migration_id,
            ),
            conn=conn,
        )

        # 4. Cascade: did the OLD parent just go empty? If so, the
        #    decompose.cascade_after_item_moved hook auto-DISMISSes it.
        from work_buddy.threads.decompose import cascade_after_item_moved
        old_parent_state = cascade_after_item_moved(
            old_parent_id, conn=conn,
        )

        return {
            "thread_id": thread_id,
            "from_parent": old_parent_id,
            "to_parent": new_parent_id,
            "migration_id": migration_id,
            "old_parent_dismissed": old_parent_state == "dismissed",
            "old_parent_state": old_parent_state,
        }
    finally:
        if own_conn:
            conn.close()


def spawn_sibling_group(
    reference_parent_id: str,
    *,
    label: str = "New group",
    actor: str = "user",
    conn=None,
) -> dict[str, Any]:
    """Create a new sibling group-parent under the same scrape.

    Used by the dashboard's "drop here to create a new group" zone:
    the user drags items into an empty area, the frontend calls this
    to spawn a fresh sibling, then immediately calls the move op to
    redirect the dragged items into it.

    Args:
        reference_parent_id: any existing group-parent in the scrape.
            The new sibling inherits its ``originating_scrape_id``.
        label: short title for the new group; user can rename later
            via the standard intent-edit flow.
        actor: who initiated; recorded on the inciting event.

    Returns ``{"parent_id": str, "originating_scrape_id": str,
                 "label": str}``.

    Raises ``MoveValidationError`` if the reference isn't a group-
    parent or has no scope id.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        ref = store.get_thread(reference_parent_id, conn=conn)
        if ref is None or ref.parent_relationship != "group":
            raise MoveValidationError(
                "reference_not_group",
                "Reference parent must be a group-relationship "
                "parent in the target scrape.",
            )
        scope = ref.originating_scrape_id
        if not scope:
            raise MoveValidationError(
                "reference_missing_scope",
                "Reference parent has no originating_scrape_id; "
                "can't create siblings without a scope.",
            )
        from work_buddy.threads.autonomy import default_spawn_policy
        from work_buddy.threads.enums import FSMState
        from work_buddy.threads.events import (
            KIND_INCITING_EVENT,
            KIND_THREAD_CREATED,
        )
        from work_buddy.threads.models import Thread
        # Inherit the reference's inciting source field so cleanup
        # adapters / dashboard filters keep working.
        ref_inciting = ref.inciting_event_summary or {}
        inciting = {
            "source": ref_inciting.get("source", "chrome_scrape"),
            "scrape_id": ref_inciting.get("scrape_id"),
            "title": label,
            "description": label,
            "user_created_sibling": True,
        }
        new_parent = Thread(
            inciting_event_summary=inciting,
            autonomy_policy=default_spawn_policy(),
            parent_relationship="group",
            originating_scrape_id=scope,
            fsm_state=FSMState.MONITORING,
        )
        store.insert_thread(new_parent, conn=conn)
        e1 = store.append_event(
            ThreadEvent(
                thread_id=new_parent.thread_id,
                kind=KIND_INCITING_EVENT,
                actor=actor,
                data=inciting,
            ),
            conn=conn,
        )
        store.append_event(
            ThreadEvent(
                thread_id=new_parent.thread_id,
                kind=KIND_THREAD_CREATED,
                actor=actor,
                data={
                    "source_pipeline": "user_spawn_sibling",
                    "parent_relationship": "group",
                    "originating_scrape_id": scope,
                    "reference_parent_id": reference_parent_id,
                },
                parent_event_id=e1.id,
            ),
            conn=conn,
        )
        store.update_thread_state(
            new_parent.thread_id,
            parent_event_id=store.latest_event_id(new_parent.thread_id),
            conn=conn,
        )
        return {
            "parent_id": new_parent.thread_id,
            "originating_scrape_id": scope,
            "label": label,
        }
    finally:
        if own_conn:
            conn.close()


def list_sibling_group_parents(
    parent_id: str,
    *,
    include_self: bool = True,
    conn=None,
) -> list[Any]:
    """Return every group-parent sharing this parent's
    ``originating_scrape_id``.

    Used by the dashboard to render the multi-column group view —
    the active group + its siblings appear side-by-side. Order:
    ``created_at`` ascending so the column order is stable across
    re-renders.

    Returns the live ``Thread`` objects (not dicts). When
    ``include_self`` is False, the active parent is filtered out.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        parent = store.get_thread(parent_id, conn=conn)
        if parent is None or parent.parent_relationship != "group":
            return []
        scope = parent.originating_scrape_id
        if not scope:
            return [parent] if include_self else []
        rows = conn.execute(
            "SELECT * FROM threads "
            "WHERE originating_scrape_id = ? "
            "  AND parent_relationship = 'group' "
            "  AND parent_id IS NULL "
            "ORDER BY created_at ASC",
            (scope,),
        ).fetchall()
        from work_buddy.threads.models import Thread
        siblings = [Thread.from_row(dict(r)) for r in rows]
        if not include_self:
            siblings = [s for s in siblings if s.thread_id != parent_id]
        return siblings
    finally:
        if own_conn:
            conn.close()


def bulk_submit_group(
    parent_id: str,
    *,
    actor: str = "user",
    conn=None,
) -> dict[str, Any]:
    """Run the accept transition on every child of ``parent_id`` that's
    currently in ``awaiting_confirmation``.

    Used by the dashboard's "Submit all" affordance on a group's
    column header. The user can also accept items individually via
    the standard per-thread Accept flow — this is purely a
    convenience for "I've reviewed everything in this group; ship
    it all".

    Returns:
        ``{"parent_id": str, "submitted": int, "failed": int,
           "skipped": int, "results": [{"thread_id", "ok", "error"}]}``

    Failures are collected per-item; one bad item never blocks the
    rest of the batch.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        parent = store.get_thread(parent_id, conn=conn)
        if parent is None:
            raise MoveValidationError(
                "parent_not_found",
                f"Parent thread {parent_id!r} not found.",
            )
        if parent.parent_relationship != "group":
            raise MoveValidationError(
                "parent_not_group",
                "Bulk submit is only available for group-relationship "
                "parents.",
            )
        children = store.list_threads(parent_id=parent_id, conn=conn)
        results: list[dict[str, Any]] = []
        submitted = 0
        failed = 0
        skipped = 0
        from work_buddy.threads import engine
        from work_buddy.threads.fsm import TRIG_CONFIRMED
        for child in children:
            if child.fsm_state.value != "awaiting_confirmation":
                results.append({
                    "thread_id": child.thread_id,
                    "ok": False,
                    "skipped": True,
                    "reason": (
                        f"not_awaiting_confirmation:{child.fsm_state.value}"
                    ),
                })
                skipped += 1
                continue
            try:
                engine.transition(
                    child.thread_id,
                    TRIG_CONFIRMED,
                    data={"actor": actor, "via": "group_bulk_submit"},
                    actor=actor,
                    conn=conn,
                    fire_side_effects=True,
                )
                results.append({
                    "thread_id": child.thread_id,
                    "ok": True,
                })
                submitted += 1
            except Exception as e:  # defensive — one bad item shouldn't block the batch
                logger.warning(
                    "bulk_submit_group: child %s failed: %s",
                    child.thread_id, e,
                )
                results.append({
                    "thread_id": child.thread_id,
                    "ok": False,
                    "error": str(e),
                })
                failed += 1
        return {
            "parent_id": parent_id,
            "submitted": submitted,
            "failed": failed,
            "skipped": skipped,
            "results": results,
        }
    finally:
        if own_conn:
            conn.close()
