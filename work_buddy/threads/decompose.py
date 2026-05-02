"""Sub-thread spawning + decompose Standard Action.

Stage 2.8 deliverable. Per DESIGN.md §5.4 + §10.5:

- A Thread can spawn sub-threads via the ``decompose`` Standard
  Action. Sub-threads have ``parent_id`` set; the same Thread
  entity, just hierarchical.
- Sub-threads inherit autonomy from the parent and may override
  DOWN axis-by-axis (more conservative); never UP. The
  ``work_buddy.threads.autonomy`` validator enforces this.
- Parent transitions to ``MONITORING`` after decomposition.
- When all children terminate, the parent transitions to ``DONE``.
- Force-close on the parent cascades a ``parent_force_close``
  signal to live children.

Public API
----------

- ``decompose_thread(parent_id, source_items, *, autonomy_override,
  inciting_summary)`` — spawn N sub-threads, mark the parent as
  decomposed, transition parent to MONITORING.
- ``cascade_terminal_to_parent(thread_id)`` — when a child reaches
  terminal, evaluate whether the parent should advance to DONE.
  Wired via state-entry handlers on the terminal states (DONE,
  DISMISSED, HANDED_OFF).
- ``force_close_parent(thread_id, *, actor)`` — closes the parent
  and cascades parent_force_close to live children.

Stage 2.8 ships the spawn + cascade mechanics. Wiring decompose
as a callable Standard Action in the registry (so action
inference can propose it) lands in Stage 2.x as the catalog
grows.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from work_buddy.threads import autonomy, engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_FSM_ENGINE,
    KIND_PARENT_FORCE_CLOSE,
    KIND_SUBTHREAD_TERMINAL_REPORTED,
    KIND_SUBTHREADS_SPAWNED,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_DISMISSED_BY_USER,
    TRIG_EXECUTION_DONE,
    TRIG_PARENT_FORCE_CLOSE,
)
from work_buddy.threads.models import (
    AutonomyPolicy,
    ContextItem,
    Thread,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DecomposeRefused(ValueError):
    """Decompose preconditions failed (parent not found, source
    list empty, autonomy override widens, etc.)."""


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def decompose_thread(
    parent_id: str,
    source_items: Iterable[ContextItem | dict],
    *,
    autonomy_override: Optional[AutonomyPolicy] = None,
    inciting_summary_extra: Optional[dict[str, Any]] = None,
    actor: str = ACTOR_FSM_ENGINE,
    conn=None,
) -> list[str]:
    """Spawn N sub-threads under ``parent_id``, one per source item.

    Returns the list of new child thread IDs in source order.

    Each child:
    - Inherits parent's autonomy_policy by default. If
      ``autonomy_override`` is provided, the override is validated
      against the parent (override-down only); if it widens, this
      function raises :class:`DecomposeRefused`.
    - Carries the source item as its first ContextItem.
    - Records an inciting_event_summary including the parent and
      the source item.

    The parent then:
    - Records a ``subthreads_spawned`` event listing the children.
    - Transitions to ``MONITORING`` (via engine.transition is
      not used because the parent's current state may be EXECUTING
      — direct cache update is the right escape valve here, since
      MONITORING is a special "parent-of-decomposed" state outside
      the normal transition table).

    Raises:
    - :class:`DecomposeRefused` for empty source list, unknown
      parent, or override-up attempt.
    """
    items = [
        ContextItem.from_dict(i) if isinstance(i, dict) else i
        for i in source_items
    ]
    if not items:
        raise DecomposeRefused(
            "decompose_thread requires at least one source item",
        )

    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        parent = store.get_thread(parent_id, conn=conn)
        if parent is None:
            raise DecomposeRefused(f"Parent thread {parent_id!r} not found")

        child_policy = autonomy_override or parent.autonomy_policy
        if autonomy_override is not None:
            # Will raise OverrideUpRejected if widening
            try:
                autonomy.validate_override_down(
                    parent.autonomy_policy, autonomy_override,
                )
            except autonomy.OverrideUpRejected as e:
                raise DecomposeRefused(str(e)) from e

        child_ids: list[str] = []
        for item in items:
            inciting = {
                "source": "decompose",
                "parent_id": parent_id,
                "context_item": item.to_dict(),
            }
            if inciting_summary_extra:
                inciting.update(inciting_summary_extra)

            child = Thread(
                parent_id=parent_id,
                autonomy_policy=child_policy,
                context_items=(item,),
                inciting_event_summary=inciting,
            )
            store.insert_thread(child, conn=conn)
            child_ids.append(child.thread_id)

        # Record decomposition on the parent
        store.append_event(
            ThreadEvent(
                thread_id=parent_id,
                kind=KIND_SUBTHREADS_SPAWNED,
                actor=actor,
                data={
                    "child_thread_ids": child_ids,
                    "source_count": len(items),
                },
                # Use the parent's current parent_event_id as the
                # optimistic-lock target; if a transition has
                # raced ahead, this will surface as a conflict.
                parent_event_id=parent.parent_event_id,
            ),
            conn=conn,
        )

        # Transition parent to MONITORING. This is one of the
        # special "manual cache update" cases — MONITORING isn't
        # reachable from the normal transition table; the FSM
        # treats it as a parent-of-decomposed state outside the
        # forward-progress flow.
        store.update_thread_state(
            parent_id,
            fsm_state=FSMState.MONITORING.value,
            conn=conn,
        )

        # Stage 4.7: write-time linearization. Compute the semantic
        # order across the spawned children and persist
        # ``order_index`` on each. Per UX.md §8.2, this is the only
        # write-side trigger; render-time NEVER recomputes.
        # Failure is non-fatal — fall back to creation-order.
        try:
            from work_buddy.threads.linearization import linearize_after_spawn
            linearize_after_spawn(parent_id, conn=conn)
        except Exception as e:
            logger.warning(
                "Linearization after decompose failed: %s; "
                "siblings keep default order_index=0", e,
            )

        return child_ids
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Cascade: child terminal → maybe-advance parent
# ---------------------------------------------------------------------------


def cascade_terminal_to_parent(
    thread_id: str, *, conn=None,
) -> Optional[str]:
    """When a child thread reaches terminal, check whether the
    parent (if any) should now advance from MONITORING → DONE.

    Returns the parent's resulting state value if a transition
    fired, or None if no parent / parent isn't monitoring / not
    all children terminal yet.

    Wired via engine.register_state_entry_handler on each terminal
    state in Stage 2.9 bootstrap.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        thread = store.get_thread(thread_id, conn=conn)
        if thread is None or thread.parent_id is None:
            return None

        parent = store.get_thread(thread.parent_id, conn=conn)
        if parent is None or parent.fsm_state != FSMState.MONITORING:
            return None

        # Record the child-terminal report on the parent
        store.append_event(
            ThreadEvent(
                thread_id=parent.thread_id,
                kind=KIND_SUBTHREAD_TERMINAL_REPORTED,
                actor=ACTOR_FSM_ENGINE,
                data={
                    "child_thread_id": thread.thread_id,
                    "child_terminal_state": thread.fsm_state.value,
                },
                parent_event_id=parent.parent_event_id,
            ),
            conn=conn,
        )
        # Re-read parent — append_event bumped parent_event_id
        parent = store.get_thread(parent.thread_id, conn=conn)

        # Are all children terminal?
        children = store.list_threads(
            parent_id=parent.thread_id, conn=conn,
        )
        if not all(c.is_terminal for c in children):
            return None

        # Yes — advance parent to DONE via the MONITORING+execution_done
        # branch resolver (see engine._default_branch_resolver).
        result = engine.transition(
            parent.thread_id,
            TRIG_EXECUTION_DONE,
            data={"all_terminal": True},
            actor=ACTOR_FSM_ENGINE,
            conn=conn,
            # Fire side effects so any monitoring-completion
            # handler downstream gets called too.
            fire_side_effects=True,
        )
        return result.next_state.value
    finally:
        if own_conn:
            conn.close()


def cascade_handler(transition_result) -> None:
    """engine.register_state_entry_handler-compatible adapter.

    Stage 2.9 bootstrap registers this for DONE / DISMISSED /
    HANDED_OFF.
    """
    cascade_terminal_to_parent(transition_result.thread_id)


def register_cascade_handlers() -> None:
    """Wire ``cascade_handler`` to every terminal state."""
    for state in (FSMState.DONE, FSMState.DISMISSED, FSMState.HANDED_OFF):
        engine.register_state_entry_handler(state, cascade_handler)


# ---------------------------------------------------------------------------
# Force-close (parent → cascade to live children)
# ---------------------------------------------------------------------------


def force_close_parent(
    parent_id: str,
    *,
    actor: str = "user",
    conn=None,
) -> dict[str, Any]:
    """Close ``parent_id`` and cascade ``parent_force_close`` to all
    live (non-terminal) children.

    Returns a summary: ``{closed_parent: bool, cascaded: [child_ids]}``.

    The parent transitions to DISMISSED via the normal transition
    table (most non-terminal states accept TRIG_DISMISSED_BY_USER).
    Children that are themselves in MONITORING get the cascade
    too; this can recur, but the table only allows
    parent_force_close from a fixed set of states, so the recursion
    bottoms out.
    """
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        parent = store.get_thread(parent_id, conn=conn)
        if parent is None:
            raise DecomposeRefused(f"Parent {parent_id!r} not found")

        cascaded: list[str] = []

        children = store.list_threads(parent_id=parent_id, conn=conn)
        for child in children:
            if child.is_terminal:
                continue
            try:
                engine.transition(
                    child.thread_id,
                    TRIG_PARENT_FORCE_CLOSE,
                    actor=actor,
                    conn=conn,
                    # Don't recursively fire side effects mid-cascade
                    # to avoid surfacing a flood of cards while the
                    # parent's also closing. The cascade itself is
                    # the side effect.
                    fire_side_effects=False,
                )
                cascaded.append(child.thread_id)
            except engine.InvalidTransition:
                # Some transient states may not accept
                # parent_force_close (e.g. EXECUTING accepts it).
                # Skip but log for diagnostics.
                logger.warning(
                    "Skipped force-close on child %s in state %s",
                    child.thread_id, child.fsm_state.value,
                )

        # Record the force-close on the parent
        store.append_event(
            ThreadEvent(
                thread_id=parent_id,
                kind=KIND_PARENT_FORCE_CLOSE,
                actor=actor,
                data={"cascaded": cascaded},
                parent_event_id=parent.parent_event_id,
            ),
            conn=conn,
        )
        # And transition the parent to DISMISSED.
        try:
            engine.transition(
                parent_id,
                TRIG_DISMISSED_BY_USER,
                actor=actor,
                conn=conn,
                fire_side_effects=False,
            )
            closed_parent = True
        except engine.InvalidTransition:
            closed_parent = False

        return {
            "closed_parent": closed_parent,
            "cascaded": cascaded,
        }
    finally:
        if own_conn:
            conn.close()
