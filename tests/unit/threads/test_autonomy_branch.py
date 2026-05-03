"""Tests for the autonomy-gated branch resolvers.

Phase 1 of the autonomy implementation. Verifies:

- Default policy (empty auto_advance_states) → never advance.
- PLAN_THEN_REVIEW → auto-advance intent + context, surface action.
- END_TO_END → auto-advance everything (where confidence allows).
- HANDS_OFF → never auto-advance, regardless of confidence.
- Confidence below floor → no advance even when state is allowed.
- consent_required_kinds covering an event_kind → no advance.
- Action irreversibility / regret / risk_amplifier checks.
- Conservative defaults: missing fields treated as 'high'.
- Audit metadata stashed on the data dict for engine pickup.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import autonomy, autonomy_branch, store
from work_buddy.threads.enums import ActionKind, FSMState
from work_buddy.threads.events import (
    KIND_AUTO_ADVANCE_DECISION,
    KIND_INTENT_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_ACTION_INFERRED,
)
from work_buddy.threads.models import AutonomyPolicy, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _thread_with_policy(policy: AutonomyPolicy, *, state=FSMState.INFERRING_INTENT) -> Thread:
    t = Thread(autonomy_policy=policy, fsm_state=state)
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# Pure-function predicates
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_confidence_meets_floor_pass(self):
        policy = AutonomyPolicy(inference_confidence_floor=0.6)
        assert autonomy_branch._confidence_meets_floor(
            {"confidence": 0.6}, policy,
        )
        assert autonomy_branch._confidence_meets_floor(
            {"confidence": 0.95}, policy,
        )

    def test_confidence_meets_floor_fail(self):
        policy = AutonomyPolicy(inference_confidence_floor=0.6)
        assert not autonomy_branch._confidence_meets_floor(
            {"confidence": 0.5}, policy,
        )
        assert not autonomy_branch._confidence_meets_floor(
            {"confidence": None}, policy,
        )
        # Missing key → not meeting floor
        assert not autonomy_branch._confidence_meets_floor({}, policy)

    def test_state_in_auto_advance_states(self):
        policy = autonomy.PLAN_THEN_REVIEW
        assert autonomy_branch._state_is_auto_advanceable(
            FSMState.AWAITING_INTENT_CONFIRMATION, policy,
        )
        assert not autonomy_branch._state_is_auto_advanceable(
            FSMState.AWAITING_CONFIRMATION, policy,
        )

    def test_kind_not_consent_gated(self):
        policy = AutonomyPolicy(
            consent_required_kinds=frozenset({"action_approved"}),
        )
        assert autonomy_branch._kind_not_consent_gated(
            "intent_inferred", policy,
        )
        assert not autonomy_branch._kind_not_consent_gated(
            "action_approved", policy,
        )


# ---------------------------------------------------------------------------
# Action metadata extraction
# ---------------------------------------------------------------------------


class TestActionMetadata:
    def test_payload_with_all_fields(self):
        meta = autonomy_branch._action_metadata({
            "kind": "improvised",
            "name": "send_msg",
            "irreversibility": "low",
            "regret_potential": "medium",
            "risk_amplifier": True,
        })
        assert meta["kind"] == ActionKind.IMPROVISED
        assert meta["irreversibility"] == "low"
        assert meta["regret_potential"] == "medium"
        assert meta["risk_amplifier"] is True

    def test_missing_fields_default_to_high(self):
        """Conservative defaults: missing risk fields treated as
        'high' so the resolver forces user review."""
        meta = autonomy_branch._action_metadata({
            "kind": "improvised",
            "name": "uncertain_thing",
        })
        assert meta["irreversibility"] == "high"
        assert meta["regret_potential"] == "high"

    def test_unknown_kind(self):
        meta = autonomy_branch._action_metadata({
            "kind": "garbage",
            "name": "x",
        })
        assert meta["kind"] is None


# ---------------------------------------------------------------------------
# Resolver: intent
# ---------------------------------------------------------------------------


class TestResolveIntent:
    def test_default_policy_surfaces_confirmation(self, fresh_db):
        """Empty auto_advance_states → never advance."""
        t = _thread_with_policy(AutonomyPolicy())
        data = {"confidence": 0.99}
        result = autonomy_branch.resolve_intent_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_INTENT_CONFIRMATION
        # Audit was stashed
        assert autonomy_branch._AUDIT_DATA_KEY in data
        assert data[autonomy_branch._AUDIT_DATA_KEY]["advance"] is False

    def test_plan_then_review_advances_high_confidence(self, fresh_db):
        t = _thread_with_policy(autonomy.PLAN_THEN_REVIEW)
        data = {"confidence": 0.9}
        result = autonomy_branch.resolve_intent_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_INFERENCE
        assert data[autonomy_branch._AUDIT_DATA_KEY]["advance"] is True

    def test_plan_then_review_blocks_low_confidence(self, fresh_db):
        t = _thread_with_policy(autonomy.PLAN_THEN_REVIEW)
        data = {"confidence": 0.3}  # below floor of 0.6
        result = autonomy_branch.resolve_intent_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_INTENT_CONFIRMATION
        audit = data[autonomy_branch._AUDIT_DATA_KEY]
        assert audit["advance"] is False
        assert any("confidence" in axis for axis in audit["axes"])

    def test_hands_off_never_advances(self, fresh_db):
        t = _thread_with_policy(autonomy.HANDS_OFF)
        data = {"confidence": 1.0}
        result = autonomy_branch.resolve_intent_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_INTENT_CONFIRMATION

    def test_missing_thread_uses_safe_default(self, fresh_db):
        """If the thread has somehow vanished between transition and
        resolver, we surface the confirmation state (safe path)."""
        result = autonomy_branch.resolve_intent_branch(
            "th-does-not-exist", {"confidence": 0.99},
        )
        assert result == FSMState.AWAITING_INTENT_CONFIRMATION


# ---------------------------------------------------------------------------
# Resolver: context (mostly mirrors intent)
# ---------------------------------------------------------------------------


class TestResolveContext:
    def test_plan_then_review_advances_to_action_inference(self, fresh_db):
        t = _thread_with_policy(
            autonomy.PLAN_THEN_REVIEW, state=FSMState.INFERRING_CONTEXT,
        )
        data = {"confidence": 0.85}
        result = autonomy_branch.resolve_context_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_INFERENCE


# ---------------------------------------------------------------------------
# Resolver: action
# ---------------------------------------------------------------------------


class TestResolveAction:
    def test_plan_then_review_blocks_action(self, fresh_db):
        """PLAN_THEN_REVIEW does NOT have AWAITING_CONFIRMATION in
        its auto_advance_states — so the user always reviews the
        proposed action before execution."""
        t = _thread_with_policy(
            autonomy.PLAN_THEN_REVIEW, state=FSMState.INFERRING_ACTION,
        )
        data = {
            "confidence": 0.95,
            "kind": "improvised",
            "name": "send_email",
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
        }
        result = autonomy_branch.resolve_action_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_CONFIRMATION

    def test_end_to_end_executes_low_risk_action(self, fresh_db):
        t = _thread_with_policy(
            autonomy.END_TO_END, state=FSMState.INFERRING_ACTION,
        )
        data = {
            "confidence": 0.95,
            "kind": "improvised",
            "name": "log_note",
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
        }
        result = autonomy_branch.resolve_action_branch(t.thread_id, data)
        assert result == FSMState.EXECUTING

    def test_end_to_end_blocks_irreversible_action(self, fresh_db):
        t = _thread_with_policy(
            autonomy.END_TO_END, state=FSMState.INFERRING_ACTION,
        )
        data = {
            "confidence": 0.95,
            "kind": "improvised",
            "name": "delete_files",
            "irreversibility": "high",
            "regret_potential": "high",
            "risk_amplifier": True,
        }
        result = autonomy_branch.resolve_action_branch(t.thread_id, data)
        # Risk-amplified, irreversibility above 'medium' threshold
        assert result == FSMState.AWAITING_CONFIRMATION

    def test_end_to_end_blocks_when_kind_not_allowed(self, fresh_db):
        # An end_to_end policy that only allows STANDARD kinds
        from dataclasses import replace
        narrow = replace(
            autonomy.END_TO_END,
            allowed_action_kinds=frozenset({ActionKind.STANDARD}),
        )
        t = _thread_with_policy(narrow, state=FSMState.INFERRING_ACTION)
        data = {
            "confidence": 0.95,
            "kind": "improvised",
            "name": "x",
            "irreversibility": "low",
            "regret_potential": "low",
            "risk_amplifier": False,
        }
        result = autonomy_branch.resolve_action_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_CONFIRMATION

    def test_action_with_missing_risk_fields_blocks(self, fresh_db):
        """Conservative default: improvised action with no declared
        irreversibility/regret → treated as 'high' → block."""
        t = _thread_with_policy(
            autonomy.END_TO_END, state=FSMState.INFERRING_ACTION,
        )
        data = {
            "confidence": 0.95,
            "kind": "improvised",
            "name": "uncertain",
        }
        result = autonomy_branch.resolve_action_branch(t.thread_id, data)
        assert result == FSMState.AWAITING_CONFIRMATION


# ---------------------------------------------------------------------------
# Resolve-by-label dispatch
# ---------------------------------------------------------------------------


class TestResolveByLabel:
    def test_known_labels_dispatch(self, fresh_db):
        t = _thread_with_policy(autonomy.PLAN_THEN_REVIEW)
        data = {"confidence": 0.9}
        assert autonomy_branch.resolve_by_label(
            "intent_review_or_advance", t.thread_id, data,
        ) == FSMState.AWAITING_INFERENCE

    def test_unknown_label_returns_none(self, fresh_db):
        # Caller (engine) falls back to the default branch resolver.
        assert autonomy_branch.resolve_by_label(
            "not_a_real_label", "th-x", {},
        ) is None
