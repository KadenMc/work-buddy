"""v5 Stage 1.5 — Capability/WorkflowDefinition gain new fields.

Pins the contract:
- ``is_action`` defaults False (preserves v4 — nothing is an Action
  Catalog entry until opted in).
- ``available_in`` defaults to {AGENT_CONVERSATION, AGENT_AUTONOMOUS,
  ACTION_PROPOSAL, USER_INVOCATION} — every context EXCEPT
  FSM_INTERNAL.
- ``intrinsic_amplifiers`` defaults empty.
- ``parameter_schema_for_action`` defaults empty dict.
- ``requires_post_review`` defaults False (action goes straight to
  ``done`` after success unless opted in).

DESIGN.md §10 (Action Catalog) is the spec.
"""

from __future__ import annotations

from work_buddy.mcp_server.registry import Capability, WorkflowDefinition
from work_buddy.threads.enums import InvocationContext


class TestCapabilityV5Defaults:
    def _basic(self, **kwargs):
        return Capability(
            name=kwargs.pop("name", "test"),
            description=kwargs.pop("description", ""),
            category=kwargs.pop("category", "test"),
            parameters=kwargs.pop("parameters", {}),
            callable=kwargs.pop("callable", lambda: None),
            **kwargs,
        )

    def test_is_action_default_false(self):
        c = self._basic()
        assert c.is_action is False

    def test_available_in_default_excludes_fsm_internal(self):
        c = self._basic()
        assert InvocationContext.FSM_INTERNAL not in c.available_in
        # All other four contexts ARE present by default
        assert c.available_in == {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        }

    def test_intrinsic_amplifiers_default_empty(self):
        assert self._basic().intrinsic_amplifiers == {}

    def test_parameter_schema_for_action_default_empty(self):
        assert self._basic().parameter_schema_for_action == {}

    def test_requires_post_review_default_false(self):
        assert self._basic().requires_post_review is False

    def test_independent_default_factory_per_instance(self):
        # Mutating one instance's available_in must NOT affect another's.
        # Default-factory bug check (sharing a dict/set reference is a
        # classic dataclass mistake).
        a = self._basic()
        b = self._basic()
        a.available_in.add(InvocationContext.FSM_INTERNAL)
        assert InvocationContext.FSM_INTERNAL not in b.available_in


class TestCapabilityV5OptIn:
    def test_action_template_with_full_v5_fields(self):
        c = Capability(
            name="send_email",
            description="Send an email.",
            category="email",
            parameters={"to": {"type": "string", "required": True}},
            callable=lambda **kwargs: None,
            is_action=True,
            intrinsic_amplifiers={
                "reversibility": "irreversible",
                "regret_potential": "high",
            },
            parameter_schema_for_action={
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
            },
            requires_post_review=True,
        )
        assert c.is_action
        assert c.intrinsic_amplifiers["reversibility"] == "irreversible"
        assert c.parameter_schema_for_action["required"] == ["to", "subject", "body"]
        assert c.requires_post_review

    def test_fsm_internal_capability_overrides_available_in(self):
        c = Capability(
            name="migrate_context",
            description="FSM-only context migration",
            category="threads",
            parameters={},
            callable=lambda: None,
            available_in={InvocationContext.FSM_INTERNAL},
        )
        assert c.available_in == {InvocationContext.FSM_INTERNAL}
        assert InvocationContext.AGENT_CONVERSATION not in c.available_in


class TestWorkflowDefinitionV5Defaults:
    def _basic(self, **kwargs):
        return WorkflowDefinition(
            name=kwargs.pop("name", "test_wf"),
            description=kwargs.pop("description", ""),
            workflow_file=kwargs.pop("workflow_file", "test.json"),
            execution=kwargs.pop("execution", "main"),
            **kwargs,
        )

    def test_is_action_default_false(self):
        assert self._basic().is_action is False

    def test_available_in_default(self):
        assert InvocationContext.FSM_INTERNAL not in self._basic().available_in

    def test_intrinsic_amplifiers_default_empty(self):
        assert self._basic().intrinsic_amplifiers == {}

    def test_requires_post_review_default_false(self):
        assert self._basic().requires_post_review is False

    def test_improvised_origin_default_none(self):
        assert self._basic().improvised_origin_thread_id is None

    def test_workflow_can_be_action_with_origin(self):
        w = self._basic(
            is_action=True,
            requires_post_review=True,
            improvised_origin_thread_id="th-source-of-improv",
        )
        assert w.is_action
        assert w.requires_post_review
        assert w.improvised_origin_thread_id == "th-source-of-improv"
