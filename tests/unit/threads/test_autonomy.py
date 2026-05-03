"""v5 Stage 2.7 — Autonomy composition + override-down validation.

Pins:
- compose() merges layered policies axis-by-axis (Omegaconf-style).
- Saved compositions: end_to_end, plan_then_review, hands_off.
- Override-down validation per DESIGN.md §11.3 (children-only-go-down).
- Effective-policy resolution walks the parent chain.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import autonomy, store
from work_buddy.threads.enums import (
    ActionKind,
    FSMState,
    InvocationContext,
    ReasoningTier,
)
from work_buddy.threads.events import KIND_ACTION_APPROVED
from work_buddy.threads.models import AutonomyPolicy, Thread


# ---------------------------------------------------------------------------
# Saved compositions
# ---------------------------------------------------------------------------


class TestSavedCompositions:
    def test_end_to_end_minimal_consent(self):
        ap = autonomy.END_TO_END
        assert ap.consent_required_kinds == frozenset()
        assert FSMState.AWAITING_CONFIRMATION in ap.auto_advance_states
        assert ap.pause_on_risk_amplifier  # safety net

    def test_plan_then_review_requires_action_approved(self):
        ap = autonomy.PLAN_THEN_REVIEW
        assert KIND_ACTION_APPROVED in ap.consent_required_kinds
        # Inference still auto-advances
        assert FSMState.INFERRING_ACTION in ap.auto_advance_states
        # But action gate does NOT
        assert FSMState.AWAITING_CONFIRMATION not in ap.auto_advance_states

    def test_hands_off_no_auto_advance(self):
        ap = autonomy.HANDS_OFF
        assert ap.auto_advance_states == frozenset()
        assert KIND_ACTION_APPROVED in ap.consent_required_kinds
        assert ap.budget_usd <= 0.20  # smaller default budget

    def test_get_composition_known(self):
        assert autonomy.get_composition("end_to_end") is autonomy.END_TO_END

    def test_get_composition_unknown_raises(self):
        with pytest.raises(KeyError):
            autonomy.get_composition("nope")


# ---------------------------------------------------------------------------
# compose()
# ---------------------------------------------------------------------------


class TestCompose:
    def test_no_layers_returns_default(self):
        ap = autonomy.compose()
        assert ap == AutonomyPolicy()

    def test_single_layer_returns_that_layer(self):
        original = autonomy.END_TO_END
        ap = autonomy.compose(original)
        assert ap.budget_usd == original.budget_usd
        assert ap.auto_advance_states == original.auto_advance_states

    def test_dict_layer_partial_override(self):
        ap = autonomy.compose(
            autonomy.PLAN_THEN_REVIEW,
            {"budget_usd": 5.0},
        )
        # The dict only overrides budget_usd; everything else from
        # PLAN_THEN_REVIEW is preserved.
        assert ap.budget_usd == 5.0
        assert ap.auto_advance_states == autonomy.PLAN_THEN_REVIEW.auto_advance_states
        assert ap.consent_required_kinds == autonomy.PLAN_THEN_REVIEW.consent_required_kinds

    def test_later_layer_wins(self):
        ap = autonomy.compose(
            autonomy.END_TO_END,
            autonomy.HANDS_OFF,  # this should win for every axis
        )
        assert ap.auto_advance_states == frozenset()  # hands_off

    def test_none_layers_skipped(self):
        ap = autonomy.compose(None, autonomy.END_TO_END, None)
        assert ap.budget_usd == autonomy.END_TO_END.budget_usd

    def test_unsupported_layer_type_raises(self):
        with pytest.raises(TypeError):
            autonomy.compose(autonomy.END_TO_END, 42)

    def test_compose_does_not_mutate_layers(self):
        before_states = autonomy.END_TO_END.auto_advance_states
        autonomy.compose(autonomy.END_TO_END, {"budget_usd": 99})
        assert autonomy.END_TO_END.auto_advance_states is before_states

    def test_three_layer_chain(self):
        # global_default < parent < thread
        ap = autonomy.compose(
            None,                                 # global default
            autonomy.PLAN_THEN_REVIEW,            # parent
            {"budget_usd": 1.5},                  # thread override
        )
        assert ap.budget_usd == 1.5
        # Other axes inherit from PLAN_THEN_REVIEW
        assert KIND_ACTION_APPROVED in ap.consent_required_kinds


# ---------------------------------------------------------------------------
# Override-down validation
# ---------------------------------------------------------------------------


class TestOverrideDownValidation:
    def test_identical_policies_are_valid(self):
        autonomy.validate_override_down(
            autonomy.END_TO_END, autonomy.END_TO_END,
        )

    def test_hands_off_under_end_to_end_is_valid(self):
        # Going from end_to_end (parent) to hands_off (child) is
        # downward — more conservative on every axis.
        autonomy.validate_override_down(
            autonomy.END_TO_END, autonomy.HANDS_OFF,
        )

    def test_end_to_end_under_hands_off_raises(self):
        # Going from hands_off (parent) to end_to_end (child) widens.
        with pytest.raises(autonomy.OverrideUpRejected) as exc_info:
            autonomy.validate_override_down(
                autonomy.HANDS_OFF, autonomy.END_TO_END,
            )
        # The error names violating axes
        assert "auto_advance_states" in str(exc_info.value) or \
               "consent_required_kinds" in str(exc_info.value)

    def test_widening_budget_raises(self):
        parent = autonomy.compose(autonomy.END_TO_END, {"budget_usd": 0.50})
        child = autonomy.compose(autonomy.END_TO_END, {"budget_usd": 5.00})
        with pytest.raises(autonomy.OverrideUpRejected):
            autonomy.validate_override_down(parent, child)

    def test_lowering_budget_is_valid(self):
        parent = autonomy.compose(autonomy.END_TO_END, {"budget_usd": 5.00})
        child = autonomy.compose(autonomy.END_TO_END, {"budget_usd": 0.50})
        autonomy.validate_override_down(parent, child)

    def test_raising_confidence_floor_is_valid(self):
        # Higher floor = more conservative (refuse low-confidence acts)
        parent = autonomy.compose(autonomy.END_TO_END, {"inference_confidence_floor": 0.3})
        child = autonomy.compose(autonomy.END_TO_END, {"inference_confidence_floor": 0.8})
        autonomy.validate_override_down(parent, child)

    def test_lowering_confidence_floor_raises(self):
        parent = autonomy.compose(autonomy.END_TO_END, {"inference_confidence_floor": 0.8})
        child = autonomy.compose(autonomy.END_TO_END, {"inference_confidence_floor": 0.3})
        with pytest.raises(autonomy.OverrideUpRejected):
            autonomy.validate_override_down(parent, child)

    def test_lowering_irreversibility_threshold_is_valid(self):
        # 'low' is more conservative than 'high'
        parent = autonomy.compose(autonomy.END_TO_END, {"irreversibility_threshold": "high"})
        child = autonomy.compose(autonomy.END_TO_END, {"irreversibility_threshold": "low"})
        autonomy.validate_override_down(parent, child)

    def test_disabling_pause_on_risk_amplifier_raises(self):
        # parent has it on; child cannot turn it off
        parent = autonomy.compose(autonomy.END_TO_END, {"pause_on_risk_amplifier": True})
        child = autonomy.compose(autonomy.END_TO_END, {"pause_on_risk_amplifier": False})
        with pytest.raises(autonomy.OverrideUpRejected):
            autonomy.validate_override_down(parent, child)

    def test_lowering_inference_ceiling_is_valid(self):
        # Lower ceiling = more conservative (cap escalation lower)
        parent = autonomy.compose(autonomy.END_TO_END, {
            "inference_ceiling_tier": ReasoningTier.AGENT_HEADLESS,
        })
        child = autonomy.compose(autonomy.END_TO_END, {
            "inference_ceiling_tier": ReasoningTier.FRONTIER_BALANCED,
        })
        autonomy.validate_override_down(parent, child)

    def test_raising_inference_floor_is_valid(self):
        # Higher floor = "don't go cheaper than this" = more conservative
        parent = autonomy.compose(autonomy.END_TO_END, {
            "inference_floor_tier": ReasoningTier.LOCAL_FAST,
        })
        child = autonomy.compose(autonomy.END_TO_END, {
            "inference_floor_tier": ReasoningTier.FRONTIER_BALANCED,
        })
        autonomy.validate_override_down(parent, child)

    def test_violations_helper_lists_axes(self):
        bad = autonomy.violations_for_override_down(
            autonomy.HANDS_OFF, autonomy.END_TO_END,
        )
        assert "auto_advance_states" in bad
        assert "consent_required_kinds" in bad

    def test_is_override_down_predicate(self):
        assert autonomy.is_override_down(
            autonomy.END_TO_END, autonomy.HANDS_OFF,
        )
        assert not autonomy.is_override_down(
            autonomy.HANDS_OFF, autonomy.END_TO_END,
        )


# ---------------------------------------------------------------------------
# Effective policy (walk parent chain)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


class TestEffectivePolicy:
    def test_no_parent_returns_thread_policy(self, fresh_db):
        t = Thread(autonomy_policy=autonomy.HANDS_OFF)
        ap = autonomy.effective_policy_for(t, walk_parents=False)
        assert ap == autonomy.HANDS_OFF

    def test_with_parent_composes(self, fresh_db):
        parent = Thread(autonomy_policy=autonomy.PLAN_THEN_REVIEW)
        store.insert_thread(parent)

        # Child overrides budget down; everything else inherits
        child_policy = autonomy.compose(
            autonomy.PLAN_THEN_REVIEW, {"budget_usd": 0.10},
        )
        child = Thread(parent_id=parent.thread_id, autonomy_policy=child_policy)
        store.insert_thread(child)

        effective = autonomy.effective_policy_for(child)
        assert effective.budget_usd == 0.10
        assert effective.consent_required_kinds == autonomy.PLAN_THEN_REVIEW.consent_required_kinds

    def test_three_level_chain(self, fresh_db):
        gp = Thread(autonomy_policy=autonomy.END_TO_END)
        store.insert_thread(gp)
        p = Thread(
            parent_id=gp.thread_id,
            autonomy_policy=autonomy.compose(
                autonomy.END_TO_END, {"budget_usd": 1.0},
            ),
        )
        store.insert_thread(p)
        leaf = Thread(
            parent_id=p.thread_id,
            autonomy_policy=autonomy.compose(
                autonomy.END_TO_END,
                {"budget_usd": 1.0, "inference_confidence_floor": 0.9},
            ),
        )
        store.insert_thread(leaf)

        effective = autonomy.effective_policy_for(leaf)
        assert effective.inference_confidence_floor == 0.9
        # Budget propagates from middle layer
        assert effective.budget_usd == 1.0

    def test_walk_parents_false_short_circuits(self, fresh_db):
        parent = Thread(autonomy_policy=autonomy.HANDS_OFF)
        store.insert_thread(parent)
        child = Thread(
            parent_id=parent.thread_id,
            autonomy_policy=autonomy.END_TO_END,
        )
        store.insert_thread(child)
        # walk_parents=False returns the Thread's own policy (regardless of override-down)
        ap = autonomy.effective_policy_for(child, walk_parents=False)
        assert ap == autonomy.END_TO_END
