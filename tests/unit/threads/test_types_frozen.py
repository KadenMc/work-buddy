"""v5 Stage 1.2 — Thread entity type signatures are locked.

These tests pin the public surface so downstream stages can program
against frozen signatures. Failures here mean the type contract
shifted under a downstream consumer.

See:
- DESIGN.md §5 (Thread)
- DESIGN.md §11 (Autonomy)
- DESIGN.md §12 (ContextItem)
- DESIGN.md §13 (Event log)
- DESIGN.md §15 (ResolutionRequest)
- DESIGN.md §7.6 (Transition table)
"""

from __future__ import annotations

import pytest

from work_buddy.threads.enums import (
    ActionKind,
    Authorship,
    FSMState,
    InferenceTarget,
    InvocationContext,
    ReasoningTier,
    SurfaceUrgency,
    tier_at_or_above,
    tier_rank,
)
from work_buddy.threads.events import (
    ACTOR_AGENT,
    ACTOR_FSM_ENGINE,
    ALL_KINDS,
    KIND_ACTION_APPROVED,
    KIND_INTENT_INFERRED,
    OptimisticLockConflict,
    ThreadEvent,
    validate_kind,
)
from work_buddy.threads.fsm import (
    STATE_ENTRY_SIDE_EFFECTS,
    TRANSITION_TABLE,
    TRIG_CONFIRMED,
    TRIG_DISMISSED_BY_USER,
    TRIG_EDITED,
    TRIG_EXECUTE,
    TRIG_EXECUTION_DONE,
    TRIG_EXECUTION_FAILED,
    TRIG_INFERENCE_DONE,
    TRIG_INFERENCE_FAILED,
    TRIG_PROVIDED,
    TRIG_REDIRECTED,
    TRIG_REJECTED,
    TRIG_REVIEW_ACCEPTED,
    lookup,
    valid_triggers_from,
)
from work_buddy.threads.models import (
    AutonomyPolicy,
    ContextItem,
    Proposal,
    ResolutionRequest,
    Task,
    Thread,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestFSMStatePredicates:
    def test_terminal_states(self):
        assert FSMState.DONE.is_terminal
        assert FSMState.DISMISSED.is_terminal
        assert FSMState.HANDED_OFF.is_terminal
        assert not FSMState.PROPOSED.is_terminal
        assert not FSMState.AWAITING_CONFIRMATION.is_terminal

    def test_wait_states(self):
        assert FSMState.AWAITING_CONFIRMATION.is_wait_state
        assert FSMState.AWAITING_INTENT_CLARIFICATION.is_wait_state
        assert FSMState.AWAITING_REVIEW.is_wait_state
        assert not FSMState.PROPOSED.is_wait_state
        assert not FSMState.EXECUTING.is_wait_state

    def test_confirmation_vs_clarification(self):
        assert FSMState.AWAITING_INTENT_CONFIRMATION.is_confirmation_state
        assert FSMState.AWAITING_CONFIRMATION.is_confirmation_state
        assert not FSMState.AWAITING_INTENT_CONFIRMATION.is_clarification_state

        assert FSMState.AWAITING_INTENT_CLARIFICATION.is_clarification_state
        assert FSMState.AWAITING_ACTION_CLARIFICATION.is_clarification_state
        assert not FSMState.AWAITING_INTENT_CLARIFICATION.is_confirmation_state

    def test_inferring_predicate(self):
        assert FSMState.INFERRING_INTENT.is_inferring
        assert FSMState.INFERRING_CONTEXT.is_inferring
        assert FSMState.INFERRING_ACTION.is_inferring
        assert not FSMState.AWAITING_INFERENCE.is_inferring  # queued, not running


class TestReasoningTierLadder:
    def test_seven_tiers_in_order(self):
        # Cheaper → more capable. The 5 existing tiers plus 2 new
        # (AGENT_HEADLESS, USER) = 7 total. Note DESIGN.md §9.2 calls
        # this an "eight-tier ladder" but only counts 5 + 2 = 7
        # values; the off-by-one is in the design doc's prose.
        assert tier_rank(ReasoningTier.LOCAL_TOOL_CALLING) == 0
        assert tier_rank(ReasoningTier.USER) == 6
        assert tier_rank(ReasoningTier.AGENT_HEADLESS) == 5
        assert tier_rank(ReasoningTier.LOCAL_TOOL_CALLING) < tier_rank(
            ReasoningTier.FRONTIER_BEST,
        )

    def test_tier_at_or_above(self):
        assert tier_at_or_above(
            ReasoningTier.AGENT_HEADLESS, ReasoningTier.FRONTIER_FAST,
        )
        assert tier_at_or_above(
            ReasoningTier.LOCAL_FAST, ReasoningTier.LOCAL_FAST,
        )
        assert not tier_at_or_above(
            ReasoningTier.LOCAL_FAST, ReasoningTier.AGENT_HEADLESS,
        )


class TestAuthorshipEnum:
    """Carries through from PR #70 cleanup; v5 keeps the enum."""

    def test_three_values(self):
        assert {a.value for a in Authorship} == {
            "user", "agent_approved", "agent_unapproved",
        }


class TestInvocationContextValues:
    def test_five_contexts(self):
        assert {c.value for c in InvocationContext} == {
            "agent_conversation",
            "agent_autonomous",
            "fsm_internal",
            "action_proposal",
            "user_invocation",
        }


# ---------------------------------------------------------------------------
# ContextItem
# ---------------------------------------------------------------------------


class TestContextItem:
    def test_round_trip(self):
        c = ContextItem(
            id="tab-1", source="chrome", type="tab", label="Hello",
            payload={"url": "https://example.com"},
        )
        c2 = ContextItem.from_dict(c.to_dict())
        assert c == c2

    def test_payload_default_empty_dict(self):
        c = ContextItem(id="x", source="s", type="t", label="L")
        assert c.payload == {}

    def test_frozen(self):
        c = ContextItem(id="x", source="s", type="t", label="L")
        with pytest.raises(Exception):  # FrozenInstanceError
            c.id = "different"  # type: ignore


# ---------------------------------------------------------------------------
# AutonomyPolicy
# ---------------------------------------------------------------------------


class TestAutonomyPolicy:
    def test_defaults_load(self):
        ap = AutonomyPolicy()
        assert 0.0 <= ap.inference_confidence_floor <= 1.0
        assert ap.budget_usd > 0
        assert ap.pause_on_risk_amplifier is True
        assert ap.inference_floor_tier == ReasoningTier.FRONTIER_FAST

    def test_round_trip(self):
        ap = AutonomyPolicy(
            auto_advance_states=frozenset({
                FSMState.PROPOSED, FSMState.AWAITING_INFERENCE,
            }),
            consent_required_kinds=frozenset({KIND_ACTION_APPROVED}),
            inference_confidence_floor=0.7,
            budget_usd=2.0,
            inference_floor_tier=ReasoningTier.FRONTIER_BALANCED,
            inference_ceiling_tier=ReasoningTier.AGENT_HEADLESS,
        )
        ap2 = AutonomyPolicy.from_dict(ap.to_dict())
        assert ap2.auto_advance_states == ap.auto_advance_states
        assert ap2.consent_required_kinds == ap.consent_required_kinds
        assert ap2.inference_confidence_floor == 0.7
        assert ap2.budget_usd == 2.0
        assert ap2.inference_floor_tier == ReasoningTier.FRONTIER_BALANCED


# ---------------------------------------------------------------------------
# Thread / Task
# ---------------------------------------------------------------------------


class TestThread:
    def test_default_construction(self):
        t = Thread()
        assert t.thread_id.startswith("th-")
        assert t.fsm_state == FSMState.PROPOSED
        assert t.parent_id is None
        assert t.subtype is None
        assert t.is_terminal is False
        assert t.is_task is False

    def test_to_dict_round_trip(self):
        import json
        t = Thread(parent_id="th-parent")
        d = t.to_dict()
        # to_dict gives nested dicts; from_row expects JSON columns
        row = {
            "thread_id": d["thread_id"],
            "parent_id": d["parent_id"],
            "subtype": d["subtype"],
            "fsm_state": d["fsm_state"],
            "parent_event_id": d["parent_event_id"],
            "autonomy_policy_json": json.dumps(d["autonomy_policy"]),
            "context_items_json": json.dumps(d["context_items"]),
            "risk_profile_json": json.dumps(d["risk_profile"]),
            "inciting_event_summary_json": json.dumps(d["inciting_event_summary"]),
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "archived_at": d["archived_at"],
            "current_focus_thread_id": d["current_focus_thread_id"],
        }
        t2 = Thread.from_row(row)
        assert t2.thread_id == t.thread_id
        assert t2.parent_id == "th-parent"

    def test_terminal_predicate(self):
        t = Thread(fsm_state=FSMState.DONE)
        assert t.is_terminal


class TestTask:
    def test_subtype_fixed(self):
        task = Task()
        assert task.subtype == "task"
        assert task.is_task

    def test_methods_stub_in_stage_1(self):
        task = Task()
        with pytest.raises(NotImplementedError):
            task.sync_to_markdown()
        with pytest.raises(NotImplementedError):
            task.master_list_position()


# ---------------------------------------------------------------------------
# ResolutionRequest
# ---------------------------------------------------------------------------


class TestResolutionRequest:
    def test_card_kind_consent(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.AWAITING_CONFIRMATION,
            proposing_actor="agent",
            urgency=SurfaceUrgency.SURFACE_NOW,
        )
        assert rr.card_kind() == "consent"

    def test_card_kind_confirmation(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.AWAITING_CONTEXT_CONFIRMATION,
            proposing_actor="agent",
            urgency=SurfaceUrgency.DEFER,
        )
        assert rr.card_kind() == "confirmation"

    def test_card_kind_clarification(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.AWAITING_INTENT_CLARIFICATION,
            proposing_actor=None,
            urgency=SurfaceUrgency.DEFER,
        )
        assert rr.card_kind() == "clarification"

    def test_card_kind_review(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.AWAITING_REVIEW,
            proposing_actor="agent",
            urgency=SurfaceUrgency.DEFER,
        )
        assert rr.card_kind() == "review"

    def test_card_kind_redirect(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.AWAITING_REDIRECT,
            proposing_actor="user",
            urgency=SurfaceUrgency.DEFER,
        )
        assert rr.card_kind() == "redirect"

    def test_card_kind_invalid_state_raises(self):
        rr = ResolutionRequest(
            thread_id="th-x",
            fsm_state=FSMState.PROPOSED,  # not a wait state
            proposing_actor=None,
            urgency=SurfaceUrgency.DEFER,
        )
        with pytest.raises(ValueError):
            rr.card_kind()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEventCatalog:
    def test_validate_kind_accepts_known(self):
        validate_kind(KIND_INTENT_INFERRED)
        validate_kind(KIND_ACTION_APPROVED)

    def test_validate_kind_rejects_unknown(self):
        with pytest.raises(ValueError):
            validate_kind("garbage")

    def test_all_kinds_includes_lifecycle(self):
        # Pin the lifecycle kinds — these are load-bearing
        for k in (
            "inciting_event", "thread_created", "thread_completed",
            "thread_dismissed", "thread_handed_off", "thread_archived",
        ):
            assert k in ALL_KINDS


class TestThreadEvent:
    def test_construction_defaults(self):
        e = ThreadEvent(
            thread_id="th-1", kind=KIND_INTENT_INFERRED, actor=ACTOR_AGENT,
        )
        assert e.id is None  # not yet persisted
        assert e.timestamp  # auto-filled
        assert e.data == {}
        assert e.parent_event_id is None

    def test_from_row_with_json_data(self):
        row = {
            "id": 42,
            "thread_id": "th-1",
            "kind": KIND_INTENT_INFERRED,
            "actor": ACTOR_FSM_ENGINE,
            "data_json": '{"foo": "bar"}',
            "parent_event_id": 41,
            "timestamp": "2026-05-02T00:00:00+00:00",
            "migration_id": None,
            "inference_tier": "frontier_fast",
        }
        e = ThreadEvent.from_row(row)
        assert e.id == 42
        assert e.data == {"foo": "bar"}
        assert e.parent_event_id == 41
        assert e.inference_tier == "frontier_fast"


# ---------------------------------------------------------------------------
# Transition table (DESIGN.md §7.6)
# ---------------------------------------------------------------------------


class TestTransitionTable:
    """Pin the canonical transitions. Failures here mean the FSM
    spec moved without a corresponding DESIGN.md update."""

    def test_inferring_intent_done_goes_to_intent_confirmation(self):
        out = lookup(FSMState.INFERRING_INTENT, TRIG_INFERENCE_DONE)
        assert out.next_state == FSMState.AWAITING_INTENT_CONFIRMATION
        assert not out.unspecified

    def test_inferring_intent_failed_goes_to_clarification(self):
        out = lookup(FSMState.INFERRING_INTENT, TRIG_INFERENCE_FAILED)
        assert out.next_state == FSMState.AWAITING_INTENT_CLARIFICATION

    def test_inferring_action_done_goes_to_awaiting_confirmation(self):
        # NOT awaiting_action_confirmation — that state was merged into
        # awaiting_confirmation per DESIGN.md correction #8.
        out = lookup(FSMState.INFERRING_ACTION, TRIG_INFERENCE_DONE)
        assert out.next_state == FSMState.AWAITING_CONFIRMATION

    def test_redirect_loops_back_to_awaiting_inference(self):
        for s in (
            FSMState.AWAITING_INTENT_CONFIRMATION,
            FSMState.AWAITING_CONTEXT_CONFIRMATION,
            FSMState.AWAITING_CONFIRMATION,
            FSMState.AWAITING_REVIEW,
            FSMState.AWAITING_REDIRECT,
        ):
            out = lookup(s, TRIG_REDIRECTED)
            assert out.next_state == FSMState.AWAITING_INFERENCE, (
                f"redirect from {s} should go to awaiting_inference, "
                f"got {out.next_state}"
            )

    def test_provided_from_clarification_goes_to_inference(self):
        for s in (
            FSMState.AWAITING_INTENT_CLARIFICATION,
            FSMState.AWAITING_CONTEXT_CLARIFICATION,
            FSMState.AWAITING_REDIRECT,
        ):
            out = lookup(s, TRIG_PROVIDED)
            assert out.next_state == FSMState.AWAITING_INFERENCE

    def test_action_clarification_provided_goes_to_confirmation(self):
        out = lookup(FSMState.AWAITING_ACTION_CLARIFICATION, TRIG_PROVIDED)
        assert out.next_state == FSMState.AWAITING_CONFIRMATION

    def test_awaiting_confirmation_execute_goes_to_executing(self):
        out = lookup(FSMState.AWAITING_CONFIRMATION, TRIG_EXECUTE)
        assert out.next_state == FSMState.EXECUTING

    def test_awaiting_confirmation_rejected_goes_to_dismissed(self):
        out = lookup(FSMState.AWAITING_CONFIRMATION, TRIG_REJECTED)
        assert out.next_state == FSMState.DISMISSED

    def test_executing_done_branches_done_or_review(self):
        out = lookup(FSMState.EXECUTING, TRIG_EXECUTION_DONE)
        assert out.next_state is None  # branched
        assert out.next_state_via_branch == "done_or_review"

    def test_executing_failed_goes_to_redirect(self):
        out = lookup(FSMState.EXECUTING, TRIG_EXECUTION_FAILED)
        assert out.next_state == FSMState.AWAITING_REDIRECT

    def test_review_accepted_goes_to_done(self):
        out = lookup(FSMState.AWAITING_REVIEW, TRIG_REVIEW_ACCEPTED)
        assert out.next_state == FSMState.DONE

    def test_dismissed_user_from_resolution_states_works(self):
        # Resolution-phase states accept TRIG_DISMISSED_BY_USER.
        # EXECUTING does NOT — once dispatched, the user has to wait
        # for the action's runtime to terminate; dismissal mid-flight
        # is a separate concern that lands as TRIG_PARENT_FORCE_CLOSE
        # if applicable, or via a runtime-specific cancel path.
        # MONITORING also accepts (cascades to children).
        resolution_states = [
            s for s in FSMState
            if not s.is_terminal and s not in {FSMState.EXECUTING}
        ]
        for s in resolution_states:
            out = lookup(s, TRIG_DISMISSED_BY_USER)
            assert not out.unspecified, f"dismissed_by_user invalid from {s}"
            assert out.next_state == FSMState.DISMISSED

    def test_terminal_states_have_no_outgoing(self):
        for s in (FSMState.DONE, FSMState.DISMISSED, FSMState.HANDED_OFF):
            for t in (TRIG_CONFIRMED, TRIG_INFERENCE_DONE, TRIG_EXECUTE):
                out = lookup(s, t)
                assert out.unspecified, (
                    f"terminal state {s} should reject trigger {t}"
                )

    def test_invalid_trigger_in_state_yields_unspecified(self):
        # awaiting_confirmation should not accept TRIG_PROVIDED
        out = lookup(FSMState.AWAITING_CONFIRMATION, TRIG_PROVIDED)
        assert out.unspecified

    def test_valid_triggers_helper(self):
        trigs = valid_triggers_from(FSMState.AWAITING_CONFIRMATION)
        assert TRIG_EXECUTE in trigs
        assert TRIG_REJECTED in trigs
        assert TRIG_REDIRECTED in trigs
        assert TRIG_PROVIDED not in trigs

    def test_every_wait_state_has_documented_side_effect(self):
        for s in FSMState:
            if s.is_wait_state:
                assert s in STATE_ENTRY_SIDE_EFFECTS, (
                    f"wait state {s} has no documented side effect"
                )

    def test_awaiting_inference_side_effect_names_llm_queue(self):
        # Pin the load-bearing assertion: the LLM queue is the
        # mechanism, NOT a queue inside threads/.
        effect = STATE_ENTRY_SIDE_EFFECTS[FSMState.AWAITING_INFERENCE]
        assert "work_buddy.llm" in effect


# ---------------------------------------------------------------------------
# Optimistic-lock conflict
# ---------------------------------------------------------------------------


class TestOptimisticLockConflict:
    def test_is_runtime_error(self):
        assert issubclass(OptimisticLockConflict, RuntimeError)


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


class TestProposal:
    def test_to_dict(self):
        p = Proposal(
            target=InferenceTarget.INTENT.value,
            payload={"intent": "schedule a call"},
            confidence=0.82,
            tier_used=ReasoningTier.FRONTIER_BALANCED,
            cost_usd=0.005,
        )
        d = p.to_dict()
        assert d["target"] == "intent"
        assert d["confidence"] == 0.82
        assert d["tier_used"] == "frontier_balanced"
