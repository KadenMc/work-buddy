"""v5 Stage 2.6 — Action Catalog filter (typed lens over registries).

Pins:
- catalog_for(context) returns only is_action=True entries whose
  available_in includes the caller's context.
- find_action(name, context) returns None for non-actions or
  unavailable-in-context actions.
- ActionTemplate carries through all v5 fields.
- Registry parameter lets tests pass a fake registry without
  touching the live one.
"""

from __future__ import annotations

import pytest

from work_buddy.mcp_server.registry import Capability, WorkflowDefinition
from work_buddy.threads import actions
from work_buddy.threads.enums import InvocationContext


def _cap(
    name, *, is_action=False, available_in=None,
    intrinsic_amplifiers=None, requires_post_review=False,
    category="test",
):
    return Capability(
        name=name,
        description=f"desc-{name}",
        category=category,
        parameters={},
        callable=lambda **kw: None,
        is_action=is_action,
        available_in=available_in or {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        },
        intrinsic_amplifiers=intrinsic_amplifiers or {},
        requires_post_review=requires_post_review,
    )


def _wf(name, *, is_action=False, available_in=None, requires_post_review=False,
        improvised_origin_thread_id=None):
    return WorkflowDefinition(
        name=name,
        description=f"wf-{name}",
        workflow_file=f"{name}.json",
        execution="main",
        is_action=is_action,
        available_in=available_in or {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        },
        requires_post_review=requires_post_review,
        improvised_origin_thread_id=improvised_origin_thread_id,
    )


def _registry(*entries):
    return {e.name: e for e in entries}


# ---------------------------------------------------------------------------
# catalog_for
# ---------------------------------------------------------------------------


class TestCatalogFor:
    def test_returns_only_is_action_entries(self):
        reg = _registry(
            _cap("a", is_action=True),
            _cap("b", is_action=False),
            _cap("c", is_action=True),
        )
        names = [t.name for t in actions.catalog_for(
            InvocationContext.ACTION_PROPOSAL, registry=reg,
        )]
        assert names == ["a", "c"]

    def test_filters_by_invocation_context(self):
        reg = _registry(
            _cap("user_only", is_action=True,
                 available_in={InvocationContext.USER_INVOCATION}),
            _cap("anywhere", is_action=True,
                 available_in={
                     InvocationContext.AGENT_CONVERSATION,
                     InvocationContext.USER_INVOCATION,
                     InvocationContext.ACTION_PROPOSAL,
                 }),
        )
        # ACTION_PROPOSAL caller sees only 'anywhere'
        names = [t.name for t in actions.catalog_for(
            InvocationContext.ACTION_PROPOSAL, registry=reg,
        )]
        assert names == ["anywhere"]
        # USER_INVOCATION caller sees both
        names = [t.name for t in actions.catalog_for(
            InvocationContext.USER_INVOCATION, registry=reg,
        )]
        assert sorted(names) == ["anywhere", "user_only"]

    def test_includes_workflows(self):
        reg = _registry(
            _cap("cap1", is_action=True),
            _wf("wf1", is_action=True),
            _wf("wf2_not_action", is_action=False),
        )
        out = actions.catalog_for(
            InvocationContext.ACTION_PROPOSAL, registry=reg,
        )
        kinds = {(t.name, t.kind) for t in out}
        assert ("cap1", "capability") in kinds
        assert ("wf1", "workflow") in kinds
        assert ("wf2_not_action", "workflow") not in kinds

    def test_category_filter(self):
        reg = _registry(
            _cap("a", is_action=True, category="email"),
            _cap("b", is_action=True, category="calendar"),
            _cap("c", is_action=True, category="email"),
        )
        out = actions.catalog_for(
            InvocationContext.ACTION_PROPOSAL,
            registry=reg, include_categories=["email"],
        )
        names = [t.name for t in out]
        assert names == ["a", "c"]

    def test_fsm_internal_default_excluded(self):
        # Default available_in for new capabilities is every
        # context EXCEPT FSM_INTERNAL. Passing FSM_INTERNAL should
        # return nothing for a default-shaped action.
        reg = _registry(_cap("a", is_action=True))
        out = actions.catalog_for(
            InvocationContext.FSM_INTERNAL, registry=reg,
        )
        assert out == []

    def test_sorted_by_category_then_name(self):
        reg = _registry(
            _cap("z", is_action=True, category="aaa"),
            _cap("a", is_action=True, category="bbb"),
            _cap("b", is_action=True, category="aaa"),
        )
        names = [(t.category, t.name) for t in actions.catalog_for(
            InvocationContext.ACTION_PROPOSAL, registry=reg,
        )]
        assert names == [("aaa", "b"), ("aaa", "z"), ("bbb", "a")]


# ---------------------------------------------------------------------------
# find_action
# ---------------------------------------------------------------------------


class TestFindAction:
    def test_returns_template_for_visible_action(self):
        reg = _registry(_cap("send_email", is_action=True,
                             requires_post_review=True))
        t = actions.find_action(
            "send_email", context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        )
        assert t is not None
        assert t.name == "send_email"
        assert t.requires_post_review is True

    def test_returns_none_for_non_action(self):
        reg = _registry(_cap("not_an_action", is_action=False))
        assert actions.find_action(
            "not_an_action",
            context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        ) is None

    def test_returns_none_when_not_visible_in_context(self):
        reg = _registry(_cap("a", is_action=True,
                             available_in={InvocationContext.USER_INVOCATION}))
        assert actions.find_action(
            "a", context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        ) is None

    def test_returns_none_when_name_not_registered(self):
        reg = _registry(_cap("known", is_action=True))
        assert actions.find_action(
            "unknown", context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        ) is None


# ---------------------------------------------------------------------------
# ActionTemplate carries v5 fields
# ---------------------------------------------------------------------------


class TestActionTemplate:
    def test_intrinsic_amplifiers_passed_through(self):
        reg = _registry(_cap("send_email", is_action=True,
                             intrinsic_amplifiers={
                                 "reversibility": "irreversible",
                                 "regret_potential": "high",
                             }))
        t = actions.find_action(
            "send_email", context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        )
        assert t.intrinsic_amplifiers["reversibility"] == "irreversible"

    def test_workflow_origin_thread_passed_through(self):
        reg = _registry(_wf("graduated_workflow", is_action=True,
                            improvised_origin_thread_id="th-source"))
        t = actions.find_action(
            "graduated_workflow",
            context=InvocationContext.ACTION_PROPOSAL,
            registry=reg,
        )
        assert t.kind == "workflow"
        assert t.improvised_origin_thread_id == "th-source"

    def test_immutability(self):
        reg = _registry(_cap("a", is_action=True))
        t = actions.find_action(
            "a", context=InvocationContext.ACTION_PROPOSAL, registry=reg,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            t.name = "different"  # type: ignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestAllActionNames:
    def test_returns_all_regardless_of_context(self):
        reg = _registry(
            _cap("a", is_action=True,
                 available_in={InvocationContext.FSM_INTERNAL}),
            _cap("b", is_action=True,
                 available_in={InvocationContext.USER_INVOCATION}),
            _cap("c", is_action=False),
        )
        assert actions.all_action_names(registry=reg) == ["a", "b"]


class TestHasAmplifierAbove:
    def test_below_threshold(self):
        reg = _registry(_cap("e", is_action=True,
                             intrinsic_amplifiers={"reversibility": "low"}))
        t = actions.find_action(
            "e", context=InvocationContext.ACTION_PROPOSAL, registry=reg,
        )
        assert actions.has_amplifier_above(t, "reversibility", "medium") is False

    def test_above_threshold(self):
        reg = _registry(_cap("e", is_action=True,
                             intrinsic_amplifiers={"regret_potential": "high"}))
        t = actions.find_action(
            "e", context=InvocationContext.ACTION_PROPOSAL, registry=reg,
        )
        assert actions.has_amplifier_above(t, "regret_potential", "medium") is True

    def test_missing_dimension(self):
        reg = _registry(_cap("e", is_action=True, intrinsic_amplifiers={}))
        t = actions.find_action(
            "e", context=InvocationContext.ACTION_PROPOSAL, registry=reg,
        )
        assert actions.has_amplifier_above(t, "reversibility", "low") is False
