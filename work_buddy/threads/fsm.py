"""FSM scaffold — state catalog and transition table.

Stage 1 deliverable: data structures only. Engine wiring lands in
Stage 2 (this module gains a ``transition()`` function that consults
the table, applies the transition, and writes the corresponding
event).

See DESIGN.md §7.6 for the canonical transition table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from work_buddy.threads.enums import FSMState


# ---------------------------------------------------------------------------
# Trigger events
# ---------------------------------------------------------------------------
#
# These are FSM-level *trigger* names — abstractions over the lower-
# level event kinds in events.py. The transition function maps
# (state, trigger) → next_state. Concrete event kinds may map to one
# trigger (e.g. ``intent_confirmed`` → ``confirmed``) or several may
# share a trigger.
# ---------------------------------------------------------------------------


# Inference outcome
TRIG_INFERENCE_DONE = "inference_done"
TRIG_INFERENCE_FAILED = "inference_failed"

# Confirmation cards
TRIG_CONFIRMED = "confirmed"
TRIG_EDITED = "edited"  # in-place edit; advances forward
TRIG_REDIRECTED = "redirected"  # back to inference

# Clarification cards
TRIG_PROVIDED = "provided"

# Action gate
TRIG_REJECTED = "rejected"  # awaiting_confirmation → dismissed
TRIG_EXECUTE = "execute"    # awaiting_confirmation → executing

# Execution
TRIG_EXECUTION_DONE = "execution_done"
TRIG_EXECUTION_FAILED = "execution_failed"

# Review
TRIG_REVIEW_ACCEPTED = "review_accepted"

# User-initiated terminal
TRIG_DISMISSED_BY_USER = "dismissed_by_user"

# Time-based
TRIG_TIMEOUT = "timeout"

# Cascade from a parent's force-close
TRIG_PARENT_FORCE_CLOSE = "parent_force_close"

# Stage 4: cleanup triggers (UX.md §6.5)
TRIG_CLEANUP_REQUESTED = "cleanup_requested"
TRIG_CLEANUP_SUCCEEDED = "cleanup_succeeded"
TRIG_CLEANUP_FAILED = "cleanup_failed"
TRIG_RETRY_CLEANUP = "retry_cleanup"
TRIG_ACCEPT_CLEANUP_FAILURE = "accept_cleanup_failure"


ALL_TRIGGERS: frozenset[str] = frozenset({
    TRIG_INFERENCE_DONE,
    TRIG_INFERENCE_FAILED,
    TRIG_CONFIRMED,
    TRIG_EDITED,
    TRIG_REDIRECTED,
    TRIG_PROVIDED,
    TRIG_REJECTED,
    TRIG_EXECUTE,
    TRIG_EXECUTION_DONE,
    TRIG_EXECUTION_FAILED,
    TRIG_REVIEW_ACCEPTED,
    TRIG_DISMISSED_BY_USER,
    TRIG_TIMEOUT,
    TRIG_PARENT_FORCE_CLOSE,
})


# ---------------------------------------------------------------------------
# Transition outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransitionOutcome:
    """Result of consulting the transition table.

    ``next_state`` is the resulting FSMState; ``next_state_via_branch``
    is set when the cell's resolution depends on a runtime condition
    (e.g. ``executing → done OR awaiting_review`` depending on whether
    the executed action requested review).

    ``unspecified=True`` means the cell is empty in the canonical
    table and the trigger is not valid in that state.
    """

    next_state: Optional[FSMState] = None
    next_state_via_branch: Optional[str] = None
    unspecified: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# The transition table
# ---------------------------------------------------------------------------
#
# Mirrors DESIGN.md §7.6 row by row. Cells given as plain FSMState
# yield a deterministic transition; cells that branch carry a
# ``next_state_via_branch`` string identifying the runtime condition
# the engine evaluates.
# ---------------------------------------------------------------------------


def _t(state: FSMState) -> TransitionOutcome:
    return TransitionOutcome(next_state=state)


def _branch(label: str) -> TransitionOutcome:
    return TransitionOutcome(next_state_via_branch=label)


def _empty() -> TransitionOutcome:
    return TransitionOutcome(unspecified=True)


# {(from_state, trigger): TransitionOutcome}
TRANSITION_TABLE: dict[tuple[FSMState, str], TransitionOutcome] = {

    # --- proposed ------------------------------------------------------
    (FSMState.PROPOSED, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.PROPOSED, TRIG_TIMEOUT): _t(FSMState.DISMISSED),
    # PROPOSED accepts parent_force_close so sub-threads spawned
    # via decompose() (start state PROPOSED) cascade-close cleanly.
    # DESIGN.md §7.6's table is silent on this cell; we treat the
    # absence as an oversight in the spec rather than an
    # intentional rejection.
    (FSMState.PROPOSED, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_inference -------------------------------------------
    (FSMState.AWAITING_INFERENCE, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_INFERENCE, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- inferring_intent ---------------------------------------------
    (FSMState.INFERRING_INTENT, TRIG_INFERENCE_DONE): _t(FSMState.AWAITING_INTENT_CONFIRMATION),
    (FSMState.INFERRING_INTENT, TRIG_INFERENCE_FAILED): _t(FSMState.AWAITING_INTENT_CLARIFICATION),
    (FSMState.INFERRING_INTENT, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.INFERRING_INTENT, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- inferring_context --------------------------------------------
    (FSMState.INFERRING_CONTEXT, TRIG_INFERENCE_DONE): _t(FSMState.AWAITING_CONTEXT_CONFIRMATION),
    (FSMState.INFERRING_CONTEXT, TRIG_INFERENCE_FAILED): _t(FSMState.AWAITING_CONTEXT_CLARIFICATION),
    (FSMState.INFERRING_CONTEXT, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.INFERRING_CONTEXT, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- inferring_action ---------------------------------------------
    (FSMState.INFERRING_ACTION, TRIG_INFERENCE_DONE): _t(FSMState.AWAITING_CONFIRMATION),
    (FSMState.INFERRING_ACTION, TRIG_INFERENCE_FAILED): _t(FSMState.AWAITING_ACTION_CLARIFICATION),
    (FSMState.INFERRING_ACTION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.INFERRING_ACTION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_intent_confirmation ---------------------------------
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_CONFIRMED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_EDITED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_REDIRECTED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_intent_clarification --------------------------------
    (FSMState.AWAITING_INTENT_CLARIFICATION, TRIG_PROVIDED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_INTENT_CLARIFICATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_INTENT_CLARIFICATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_context_confirmation --------------------------------
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_CONFIRMED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_EDITED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_REDIRECTED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_context_clarification -------------------------------
    (FSMState.AWAITING_CONTEXT_CLARIFICATION, TRIG_PROVIDED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_CONTEXT_CLARIFICATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_CONTEXT_CLARIFICATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_action_clarification --------------------------------
    (FSMState.AWAITING_ACTION_CLARIFICATION, TRIG_PROVIDED): _t(FSMState.AWAITING_CONFIRMATION),
    (FSMState.AWAITING_ACTION_CLARIFICATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_ACTION_CLARIFICATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_confirmation (action gate) --------------------------
    (FSMState.AWAITING_CONFIRMATION, TRIG_EDITED): _t(FSMState.AWAITING_CONFIRMATION),
    (FSMState.AWAITING_CONFIRMATION, TRIG_REDIRECTED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_CONFIRMATION, TRIG_REJECTED): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_CONFIRMATION, TRIG_EXECUTE): _t(FSMState.EXECUTING),
    (FSMState.AWAITING_CONFIRMATION, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_CONFIRMATION, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- executing -----------------------------------------------------
    (FSMState.EXECUTING, TRIG_EXECUTION_DONE): _branch("done_or_review"),
    (FSMState.EXECUTING, TRIG_EXECUTION_FAILED): _t(FSMState.AWAITING_REDIRECT),
    (FSMState.EXECUTING, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_review (post-execution, opt-in) ---------------------
    (FSMState.AWAITING_REVIEW, TRIG_REDIRECTED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_REVIEW, TRIG_REVIEW_ACCEPTED): _t(FSMState.DONE),
    (FSMState.AWAITING_REVIEW, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_REVIEW, TRIG_TIMEOUT): _t(FSMState.DONE),  # auto-accept
    (FSMState.AWAITING_REVIEW, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- awaiting_redirect --------------------------------------------
    (FSMState.AWAITING_REDIRECT, TRIG_PROVIDED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_REDIRECT, TRIG_REDIRECTED): _t(FSMState.AWAITING_INFERENCE),
    (FSMState.AWAITING_REDIRECT, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.AWAITING_REDIRECT, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- monitoring (parent-of-decomposed) ----------------------------
    (FSMState.MONITORING, TRIG_EXECUTION_DONE): _branch("done_when_all_subthreads_terminal"),
    (FSMState.MONITORING, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),  # cascades to children
    # MONITORING also accepts parent_force_close — a sub-thread
    # may itself decompose and become a parent; force-closing its
    # own parent must cascade through.
    (FSMState.MONITORING, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # --- cleanup transitions (Stage 4 — UX.md §6.5) -------------------
    # User clicked Clean Up while in any wait state. Adapter is
    # invoked synchronously or queued. Thread enters CLEANING_UP.
    (FSMState.AWAITING_INTENT_CONFIRMATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_CONTEXT_CONFIRMATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_INTENT_CLARIFICATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_CONTEXT_CLARIFICATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_ACTION_CLARIFICATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_CONFIRMATION, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_REVIEW, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),
    (FSMState.AWAITING_REDIRECT, TRIG_CLEANUP_REQUESTED): _t(FSMState.CLEANING_UP),

    # Adapter result
    (FSMState.CLEANING_UP, TRIG_CLEANUP_SUCCEEDED): _t(FSMState.DONE_CLEANUP_SUCCESSFUL),
    (FSMState.CLEANING_UP, TRIG_CLEANUP_FAILED): _t(FSMState.DONE_CLEANUP_UNSUCCESSFUL),
    # User can dismiss mid-cleanup (e.g., the adapter's stuck waiting
    # on a locked file). Cascade-close also works.
    (FSMState.CLEANING_UP, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.CLEANING_UP, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # User responses to a failed cleanup
    (FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_RETRY_CLEANUP): _t(FSMState.CLEANING_UP),
    (FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_ACCEPT_CLEANUP_FAILURE): _t(FSMState.DONE),
    (FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_DISMISSED_BY_USER): _t(FSMState.DISMISSED),
    (FSMState.DONE_CLEANUP_UNSUCCESSFUL, TRIG_PARENT_FORCE_CLOSE): _t(FSMState.DISMISSED),

    # Terminals have no outgoing transitions (no entries).
}


def lookup(
    state: FSMState, trigger: str,
) -> TransitionOutcome:
    """Look up the transition for ``(state, trigger)``.

    Returns ``TransitionOutcome(unspecified=True)`` if the trigger is
    not valid in that state. The Stage 2 engine is responsible for
    rejecting unspecified transitions cleanly.
    """
    return TRANSITION_TABLE.get((state, trigger), _empty())


def valid_triggers_from(state: FSMState) -> frozenset[str]:
    """Return the set of triggers that have a defined transition out
    of ``state``."""
    return frozenset(t for (s, t) in TRANSITION_TABLE if s == state)


# ---------------------------------------------------------------------------
# State-entry side effects (stub list — Stage 2 wires)
# ---------------------------------------------------------------------------
#
# When the engine transitions a Thread into a new state, certain
# side effects fire. This dict documents them for Stage 2 to
# implement.
# ---------------------------------------------------------------------------

STATE_ENTRY_SIDE_EFFECTS: dict[FSMState, str] = {
    FSMState.AWAITING_INFERENCE:
        "enqueue inference request into work_buddy.llm.queue",
    FSMState.INFERRING_INTENT:
        "(none — set by inference worker after dequeue)",
    FSMState.INFERRING_CONTEXT:
        "(none — set by inference worker after dequeue)",
    FSMState.INFERRING_ACTION:
        "(none — set by inference worker after dequeue)",
    FSMState.AWAITING_INTENT_CONFIRMATION:
        "publish ResolutionRequest (confirmation card) via consent system",
    FSMState.AWAITING_INTENT_CLARIFICATION:
        "publish ResolutionRequest (clarification card) via consent system",
    FSMState.AWAITING_CONTEXT_CONFIRMATION:
        "publish ResolutionRequest (confirmation card) via consent system",
    FSMState.AWAITING_CONTEXT_CLARIFICATION:
        "publish ResolutionRequest (clarification card) via consent system",
    FSMState.AWAITING_ACTION_CLARIFICATION:
        "publish ResolutionRequest (clarification card) via consent system",
    FSMState.AWAITING_CONFIRMATION:
        "publish ResolutionRequest (consent card) via consent system",
    FSMState.AWAITING_REVIEW:
        "publish ResolutionRequest (review card) — only entered if action template requested it",
    FSMState.AWAITING_REDIRECT:
        "publish ResolutionRequest (redirect card) via consent system",
    FSMState.EXECUTING:
        "dispatch action into capability/workflow/agent runtime; record execution_started event",
    FSMState.MONITORING:
        "watch sub-threads; transition to done when all terminal",
    FSMState.DONE:
        "record thread_completed event; archive if subtype != 'task'",
    FSMState.DISMISSED:
        "record thread_dismissed event; cascade to live sub-threads if parent",
    FSMState.HANDED_OFF:
        "record thread_handed_off event",

    # Stage 4: cleanup states (UX.md §6.4)
    FSMState.CLEANING_UP:
        "invoke registered cleanup adapter (work_buddy.threads.cleanup); "
        "fires cleanup_succeeded or cleanup_failed trigger",
    FSMState.DONE_CLEANUP_SUCCESSFUL:
        "record source_cleaned_up terminal event",
    FSMState.DONE_CLEANUP_UNSUCCESSFUL:
        "publish ResolutionRequest (retry/accept-failure card) via consent system",
}
