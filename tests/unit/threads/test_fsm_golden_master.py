"""Golden-master snapshot of the Thread resolution FSM.

This is a *parity oracle* for the WorkItem-base extraction
(`.data/designs/workflow-induction/design/08-...`). The extraction moves
universal fields off `Thread` onto a new `WorkItem` base but must NOT
touch the FSM. This test freezes the entire transition table, the
state-entry side-effect catalog, and the reachability map as literals so
any accidental change to `fsm.py` / the engine's branch reachability is
caught immediately.

If this test fails after a change you believe is intentional, regenerate
the literals deliberately — do not loosen the assertions.
"""

from __future__ import annotations

from work_buddy.threads import engine, fsm
from work_buddy.threads.enums import FSMState


def _serialize_table() -> dict[str, str]:
    out: dict[str, str] = {}
    for (state, trig), outcome in fsm.TRANSITION_TABLE.items():
        key = f"{state.value}|{trig}"
        if outcome.next_state is not None:
            out[key] = f"->{outcome.next_state.value}"
        elif outcome.next_state_via_branch is not None:
            out[key] = f"branch:{outcome.next_state_via_branch}"
        else:
            out[key] = "unspecified"
    return out


EXPECTED_TABLE: dict[str, str] = {
    "awaiting_action_clarification|cleanup_requested": "->cleaning_up",
    "awaiting_action_clarification|dismissed_by_user": "->dismissed",
    "awaiting_action_clarification|parent_force_close": "->dismissed",
    "awaiting_action_clarification|provided": "->awaiting_confirmation",
    "awaiting_confirmation|cleanup_requested": "->cleaning_up",
    "awaiting_confirmation|dismissed_by_user": "->dismissed",
    "awaiting_confirmation|edited": "->awaiting_confirmation",
    "awaiting_confirmation|execute": "->executing",
    "awaiting_confirmation|parent_force_close": "->dismissed",
    "awaiting_confirmation|redirected": "->awaiting_inference",
    "awaiting_confirmation|rejected": "->dismissed",
    "awaiting_context_clarification|cleanup_requested": "->cleaning_up",
    "awaiting_context_clarification|dismissed_by_user": "->dismissed",
    "awaiting_context_clarification|parent_force_close": "->dismissed",
    "awaiting_context_clarification|provided": "->awaiting_inference",
    "awaiting_context_confirmation|cleanup_requested": "->cleaning_up",
    "awaiting_context_confirmation|confirmed": "->awaiting_inference",
    "awaiting_context_confirmation|dismissed_by_user": "->dismissed",
    "awaiting_context_confirmation|edited": "->awaiting_inference",
    "awaiting_context_confirmation|parent_force_close": "->dismissed",
    "awaiting_context_confirmation|redirected": "->awaiting_inference",
    "awaiting_inference|dismissed_by_user": "->dismissed",
    "awaiting_inference|parent_force_close": "->dismissed",
    "awaiting_intent_clarification|cleanup_requested": "->cleaning_up",
    "awaiting_intent_clarification|dismissed_by_user": "->dismissed",
    "awaiting_intent_clarification|parent_force_close": "->dismissed",
    "awaiting_intent_clarification|provided": "->awaiting_inference",
    "awaiting_intent_confirmation|cleanup_requested": "->cleaning_up",
    "awaiting_intent_confirmation|confirmed": "->awaiting_inference",
    "awaiting_intent_confirmation|dismissed_by_user": "->dismissed",
    "awaiting_intent_confirmation|edited": "->awaiting_inference",
    "awaiting_intent_confirmation|parent_force_close": "->dismissed",
    "awaiting_intent_confirmation|redirected": "->awaiting_inference",
    "awaiting_redirect|cleanup_requested": "->cleaning_up",
    "awaiting_redirect|dismissed_by_user": "->dismissed",
    "awaiting_redirect|parent_force_close": "->dismissed",
    "awaiting_redirect|provided": "->awaiting_inference",
    "awaiting_redirect|redirected": "->awaiting_inference",
    "awaiting_review|cleanup_requested": "->cleaning_up",
    "awaiting_review|dismissed_by_user": "->dismissed",
    "awaiting_review|parent_force_close": "->dismissed",
    "awaiting_review|redirected": "->awaiting_inference",
    "awaiting_review|review_accepted": "->done",
    "awaiting_review|timeout": "->done",
    "cleaning_up|cleanup_failed": "->done_cleanup_unsuccessful",
    "cleaning_up|cleanup_succeeded": "->done_cleanup_successful",
    "cleaning_up|dismissed_by_user": "->dismissed",
    "cleaning_up|parent_force_close": "->dismissed",
    "done_cleanup_unsuccessful|accept_cleanup_failure": "->done",
    "done_cleanup_unsuccessful|dismissed_by_user": "->dismissed",
    "done_cleanup_unsuccessful|parent_force_close": "->dismissed",
    "done_cleanup_unsuccessful|retry_cleanup": "->cleaning_up",
    "executing|execution_done": "branch:done_or_review",
    "executing|execution_failed": "->awaiting_redirect",
    "executing|parent_force_close": "->dismissed",
    "inferring_action|dismissed_by_user": "->dismissed",
    "inferring_action|inference_done": "branch:action_review_or_execute",
    "inferring_action|inference_failed": "->awaiting_action_clarification",
    "inferring_action|parent_force_close": "->dismissed",
    "inferring_context|dismissed_by_user": "->dismissed",
    "inferring_context|inference_done": "branch:context_review_or_advance",
    "inferring_context|inference_failed": "->awaiting_context_clarification",
    "inferring_context|parent_force_close": "->dismissed",
    "inferring_intent|dismissed_by_user": "->dismissed",
    "inferring_intent|inference_done": "branch:intent_review_or_advance",
    "inferring_intent|inference_failed": "->awaiting_intent_clarification",
    "inferring_intent|parent_force_close": "->dismissed",
    "monitoring|dismissed_by_user": "->dismissed",
    "monitoring|execution_done": "branch:done_when_all_subthreads_terminal",
    "monitoring|parent_force_close": "->dismissed",
    "proposed|begin_inference": "->awaiting_inference",
    "proposed|dismissed_by_user": "->dismissed",
    "proposed|parent_force_close": "->dismissed",
    "proposed|timeout": "->dismissed",
}

EXPECTED_SIDE_EFFECT_STATES: set[str] = {
    "awaiting_action_clarification",
    "awaiting_confirmation",
    "awaiting_context_clarification",
    "awaiting_context_confirmation",
    "awaiting_inference",
    "awaiting_intent_clarification",
    "awaiting_intent_confirmation",
    "awaiting_redirect",
    "awaiting_review",
    "cleaning_up",
    "dismissed",
    "done",
    "done_cleanup_successful",
    "done_cleanup_unsuccessful",
    "executing",
    "handed_off",
    "inferring_action",
    "inferring_context",
    "inferring_intent",
    "monitoring",
}

EXPECTED_REACHABLE: dict[str, list[str]] = {
    "awaiting_action_clarification": ["awaiting_confirmation", "cleaning_up", "dismissed"],
    "awaiting_confirmation": ["awaiting_confirmation", "awaiting_inference", "cleaning_up", "dismissed", "executing"],
    "awaiting_context_clarification": ["awaiting_inference", "cleaning_up", "dismissed"],
    "awaiting_context_confirmation": ["awaiting_inference", "cleaning_up", "dismissed"],
    "awaiting_inference": ["dismissed"],
    "awaiting_intent_clarification": ["awaiting_inference", "cleaning_up", "dismissed"],
    "awaiting_intent_confirmation": ["awaiting_inference", "cleaning_up", "dismissed"],
    "awaiting_redirect": ["awaiting_inference", "cleaning_up", "dismissed"],
    "awaiting_review": ["awaiting_inference", "cleaning_up", "dismissed", "done"],
    "cleaning_up": ["dismissed", "done_cleanup_successful", "done_cleanup_unsuccessful"],
    "dismissed": [],
    "done": [],
    "done_cleanup_successful": [],
    "done_cleanup_unsuccessful": ["cleaning_up", "dismissed", "done"],
    "executing": ["awaiting_redirect", "awaiting_review", "dismissed", "done"],
    "handed_off": [],
    "inferring_action": ["awaiting_action_clarification", "awaiting_confirmation", "dismissed", "executing"],
    "inferring_context": ["awaiting_context_clarification", "awaiting_context_confirmation", "awaiting_inference", "dismissed"],
    "inferring_intent": ["awaiting_inference", "awaiting_intent_clarification", "awaiting_intent_confirmation", "dismissed"],
    "monitoring": ["dismissed", "done", "monitoring"],
    "proposed": ["awaiting_inference", "dismissed"],
}


def test_transition_table_is_frozen():
    """The full (state, trigger) -> outcome mapping is unchanged."""
    assert _serialize_table() == EXPECTED_TABLE


def test_transition_table_cell_count():
    """Guards against silent additions/removals even if a swap nets zero."""
    assert len(fsm.TRANSITION_TABLE) == 74


def test_state_entry_side_effects_keys_frozen():
    actual = {s.value for s in fsm.STATE_ENTRY_SIDE_EFFECTS}
    assert actual == EXPECTED_SIDE_EFFECT_STATES


def test_engine_reachability_frozen():
    actual = {
        s.value: sorted(x.value for x in engine.reachable_states_from(s))
        for s in FSMState
    }
    assert actual == EXPECTED_REACHABLE
