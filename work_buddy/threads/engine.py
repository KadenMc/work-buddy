"""FSM engine — applies transitions defined in fsm.py to real Threads.

Brings the Stage-1 transition table to life:

    transition(thread_id, trigger, *, actor, data, parent_event_id, ...)
        ↓
    look up (state, trigger) → next_state in TRANSITION_TABLE
        ↓
    record state_transition event under optimistic lock
        ↓
    update threads.fsm_state cache
        ↓
    fire side effects (enqueue inference / publish ResolutionRequest /
                       dispatch action / etc.) per STATE_ENTRY_SIDE_EFFECTS

The engine is a thin dispatcher. Side-effect implementations live in
their own modules (queue publish, consent publish, action dispatch)
and are wired through callbacks so this engine stays testable without
the full subsystem stack online.

Branching transitions
---------------------

Some cells in the transition table return a ``next_state_via_branch``
label rather than a deterministic state (e.g. EXECUTING + execution_done
→ ``done_or_review``). The engine resolves these via a branch resolver
the caller can override; the default resolver implements the documented
DESIGN.md rules.

DESIGN.md §7.6 (transition table), §7.7 (requirements), §13 (event log).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from work_buddy.threads import fsm, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_FSM_ENGINE,
    KIND_STATE_TRANSITION,
    OptimisticLockConflict,
    ThreadEvent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidTransition(ValueError):
    """The (state, trigger) cell is empty in the transition table."""


class ThreadNotFound(KeyError):
    """No Thread with that ID."""


# ---------------------------------------------------------------------------
# Branch resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchContext:
    """Inputs the branch resolver may inspect."""
    thread_id: str
    current_state: FSMState
    trigger: str
    branch_label: str
    data: dict[str, Any]


BranchResolver = Callable[[BranchContext], FSMState]


def _default_branch_resolver(ctx: BranchContext) -> FSMState:
    """Default rules for branched transitions.

    - ``done_or_review`` (EXECUTING + execution_done):
        - if ``data['requires_post_review']`` is True → AWAITING_REVIEW
        - else → DONE
    - ``done_when_all_subthreads_terminal`` (MONITORING + execution_done):
        - if ``data['all_terminal']`` is True → DONE
        - else stay in MONITORING (engine treats this as a no-op
          rather than a transition; we return MONITORING to make
          that explicit).
    - ``intent_review_or_advance`` /
      ``context_review_or_advance`` /
      ``action_review_or_execute`` (autonomy-gated):
        - delegate to ``work_buddy.threads.autonomy_branch``,
          which reads the thread's effective AutonomyPolicy and
          decides whether to skip the would-be wait state.
    """
    label = ctx.branch_label
    if label == "done_or_review":
        if ctx.data.get("requires_post_review"):
            return FSMState.AWAITING_REVIEW
        return FSMState.DONE
    if label == "done_when_all_subthreads_terminal":
        if ctx.data.get("all_terminal"):
            return FSMState.DONE
        return FSMState.MONITORING

    # Autonomy-gated branches. Lazy import to avoid an import cycle
    # (autonomy_branch imports from autonomy + store, which both
    # reference engine indirectly through events.OptimisticLockConflict).
    from work_buddy.threads import autonomy_branch
    auto_choice = autonomy_branch.resolve_by_label(
        label, ctx.thread_id, ctx.data,
    )
    if auto_choice is not None:
        return auto_choice

    raise InvalidTransition(
        f"Branch label {label!r} has no resolver",
    )


# ---------------------------------------------------------------------------
# Side-effect dispatch
# ---------------------------------------------------------------------------
#
# Side effects fire when the engine *enters* a state. The engine
# delegates to a side-effect dispatcher; a default no-op dispatcher
# is provided so the engine is fully testable in isolation.
# ---------------------------------------------------------------------------


SideEffectFn = Callable[["TransitionResult"], None]


def _noop_side_effect(_: "TransitionResult") -> None:
    pass


_REGISTERED_SIDE_EFFECTS: dict[FSMState, list[SideEffectFn]] = {}


def register_state_entry_handler(
    state: FSMState, fn: SideEffectFn,
) -> None:
    """Register a side-effect handler for entering ``state``.

    Multiple handlers per state are allowed; they run in registration
    order, all-or-nothing. Wired by Stage 2.5 (publish
    ResolutionRequest), Stage 2.4 (enqueue inference), Stage 2.x
    (dispatch action).
    """
    _REGISTERED_SIDE_EFFECTS.setdefault(state, []).append(fn)


def clear_state_entry_handlers() -> None:
    """Test/utility: drop all registered handlers."""
    _REGISTERED_SIDE_EFFECTS.clear()


def _fire_side_effects(result: "TransitionResult") -> None:
    handlers = _REGISTERED_SIDE_EFFECTS.get(result.next_state, ())
    for h in handlers:
        try:
            h(result)
        except Exception as e:
            logger.exception(
                "State-entry handler for %s raised %s; continuing.",
                result.next_state, e,
            )


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionResult:
    thread_id: str
    prev_state: FSMState
    next_state: FSMState
    trigger: str
    event_id: int
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transition(
    thread_id: str,
    trigger: str,
    *,
    actor: str = ACTOR_FSM_ENGINE,
    data: Optional[dict[str, Any]] = None,
    parent_event_id: Optional[int] = ...,  # type: ignore[assignment]
    branch_resolver: Optional[BranchResolver] = None,
    fire_side_effects: bool = True,
    conn=None,
) -> TransitionResult:
    """Apply a single transition to ``thread_id``.

    Steps:

    1. Read current state from threads.fsm_state.
    2. Look up (state, trigger) in TRANSITION_TABLE; refuse if empty.
    3. Resolve branch labels via ``branch_resolver`` (defaults provided).
    4. Append a ``state_transition`` event under optimistic lock
       (uses the Thread's stored ``parent_event_id`` unless the
       caller supplies one).
    5. Update the threads.fsm_state cache to ``next_state`` and bump
       parent_event_id.
    6. If ``fire_side_effects`` is True, run state-entry handlers
       for ``next_state``.

    Raises:
        ThreadNotFound — no row for thread_id.
        InvalidTransition — empty cell or unresolved branch.
        OptimisticLockConflict — re-fetch state and retry.
    """
    data = data or {}
    own_conn = conn is None
    if own_conn:
        conn = store.get_connection()
    try:
        thread = store.get_thread(thread_id, conn=conn)
        if thread is None:
            raise ThreadNotFound(f"No Thread {thread_id!r}")

        outcome = fsm.lookup(thread.fsm_state, trigger)
        if outcome.unspecified:
            raise InvalidTransition(
                f"Trigger {trigger!r} is not valid in state "
                f"{thread.fsm_state.value!r}",
            )

        # Resolve branched transitions
        next_state: FSMState
        if outcome.next_state_via_branch is not None:
            resolver = branch_resolver or _default_branch_resolver
            next_state = resolver(BranchContext(
                thread_id=thread_id,
                current_state=thread.fsm_state,
                trigger=trigger,
                branch_label=outcome.next_state_via_branch,
                data=data,
            ))
        else:
            assert outcome.next_state is not None
            next_state = outcome.next_state

        # Optimistic lock: prefer caller-supplied lock target;
        # fall back to the thread's stored parent_event_id.
        if parent_event_id is ...:  # sentinel
            expected_parent = thread.parent_event_id
        else:
            expected_parent = parent_event_id

        # If the branch resolver stashed audit metadata for an
        # auto-advance decision, extract it from the data dict so it
        # doesn't pollute the state_transition payload. We write the
        # audit event AFTER state_transition lands so the resolver
        # can stay pure (no DB writes during resolution → no
        # optimistic-lock invalidation).
        from work_buddy.threads.autonomy_branch import _AUDIT_DATA_KEY
        autonomy_audit = data.pop(_AUDIT_DATA_KEY, None)

        # Append the transition event
        event = store.append_event(
            ThreadEvent(
                thread_id=thread_id,
                kind=KIND_STATE_TRANSITION,
                actor=actor,
                data={
                    "from": thread.fsm_state.value,
                    "to": next_state.value,
                    "trigger": trigger,
                    **data,
                },
                parent_event_id=expected_parent,
            ),
            conn=conn,
        )

        # Update the cache to the new state. The new event id is the
        # parent_event_id for the next transition.
        store.update_thread_state(
            thread_id,
            fsm_state=next_state.value,
            parent_event_id=event.id,
            conn=conn,
        )

        # If the resolver stashed audit data, append the
        # auto_advance_decision event now using the just-written
        # state_transition event as the parent_event_id.
        if autonomy_audit is not None:
            from work_buddy.threads.events import KIND_AUTO_ADVANCE_DECISION
            try:
                audit_event = store.append_event(
                    ThreadEvent(
                        thread_id=thread_id,
                        kind=KIND_AUTO_ADVANCE_DECISION,
                        actor=ACTOR_FSM_ENGINE,
                        data=autonomy_audit,
                        parent_event_id=event.id,
                    ),
                    conn=conn,
                )
                # Bump the cache's parent_event_id to the audit event so
                # subsequent transitions read the right lock target.
                store.update_thread_state(
                    thread_id,
                    parent_event_id=audit_event.id,
                    conn=conn,
                )
            except Exception as audit_exc:
                logger.warning(
                    "auto_advance_decision audit write failed for %s: %s",
                    thread_id, audit_exc,
                )

        result = TransitionResult(
            thread_id=thread_id,
            prev_state=thread.fsm_state,
            next_state=next_state,
            trigger=trigger,
            event_id=event.id,
            data=data,
        )
    finally:
        if own_conn:
            conn.close()

    if fire_side_effects:
        _fire_side_effects(result)

    return result


_BRANCH_REACH: dict[str, set[FSMState]] = {
    "done_or_review": {FSMState.DONE, FSMState.AWAITING_REVIEW},
    "done_when_all_subthreads_terminal": {
        FSMState.DONE, FSMState.MONITORING,
    },
    # Autonomy-gated: either advance to the next inference or
    # surface the confirmation card.
    "intent_review_or_advance": {
        FSMState.AWAITING_INFERENCE, FSMState.AWAITING_INTENT_CONFIRMATION,
    },
    "context_review_or_advance": {
        FSMState.AWAITING_INFERENCE, FSMState.AWAITING_CONTEXT_CONFIRMATION,
    },
    "action_review_or_execute": {
        FSMState.EXECUTING, FSMState.AWAITING_CONFIRMATION,
    },
}


def reachable_states_from(state: FSMState) -> set[FSMState]:
    """Static analysis helper: every state reachable in one trigger
    from ``state`` (branched transitions return all possible
    branches)."""
    reach: set[FSMState] = set()
    for (s, _trig), out in fsm.TRANSITION_TABLE.items():
        if s != state:
            continue
        if out.next_state is not None:
            reach.add(out.next_state)
        elif out.next_state_via_branch in _BRANCH_REACH:
            reach.update(_BRANCH_REACH[out.next_state_via_branch])
    return reach
