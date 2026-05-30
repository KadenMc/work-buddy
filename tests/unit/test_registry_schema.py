"""Phase 0 schema tests — `invokes` fields and computed workflow requires.

These tests exercise the registry dataclasses directly, without loading
the full MCP registry. They are fast and do not depend on the workflow units.
"""

from __future__ import annotations

from work_buddy.mcp_server.registry import (
    AutoRun,
    Capability,
    WorkflowDefinition,
    WorkflowStep,
    _compute_workflow_requires,
)


def _noop(**_kwargs):
    return None


def test_capability_invokes_default_empty():
    """Existing `Capability(...)` calls without `invokes` still construct."""
    cap = Capability(
        name="sample",
        description="",
        category="status",
        parameters={},
        callable=_noop,
    )
    assert cap.invokes == []
    assert cap.requires == []


def test_workflow_step_invokes_default_empty():
    step = WorkflowStep(id="s1", name="s1", instruction="", step_type="reasoning")
    assert step.invokes == []


def test_workflow_definition_requires_default_empty():
    wf = WorkflowDefinition(
        name="wf", description="", workflow_file="store:test", execution="main",
    )
    assert wf.requires == []


def test_compute_workflow_requires_from_step_requires():
    """A workflow whose step declares `requires=[...]` inherits those IDs."""
    wf = WorkflowDefinition(
        name="wf1", description="", workflow_file="store:test", execution="main",
        steps=[
            WorkflowStep(
                id="s1", name="s1", instruction="", step_type="code",
                requires=["obsidian"],
            ),
        ],
    )
    registry = {"wf1": wf}
    _compute_workflow_requires(registry)
    assert wf.requires == ["obsidian"]


def test_compute_workflow_requires_from_capability_invokes():
    """A step.invokes=[cap] pulls in that capability's requires."""
    cap = Capability(
        name="task_create",
        description="", category="tasks", parameters={},
        callable=_noop, requires=["obsidian"],
    )
    wf = WorkflowDefinition(
        name="wf2", description="", workflow_file="store:test", execution="main",
        steps=[
            WorkflowStep(
                id="s1", name="s1", instruction="", step_type="reasoning",
                invokes=["task_create"],
            ),
        ],
    )
    registry = {"task_create": cap, "wf2": wf}
    _compute_workflow_requires(registry)
    assert wf.requires == ["obsidian"]


def test_compute_workflow_requires_unions_and_dedupes():
    cap_a = Capability(
        name="cap_a", description="", category="x", parameters={},
        callable=_noop, requires=["obsidian", "hindsight"],
    )
    cap_b = Capability(
        name="cap_b", description="", category="x", parameters={},
        callable=_noop, requires=["obsidian"],
    )
    wf = WorkflowDefinition(
        name="wf3", description="", workflow_file="store:test", execution="main",
        steps=[
            WorkflowStep(
                id="s1", name="s1", instruction="", step_type="code",
                requires=["postgresql"], invokes=["cap_a"],
            ),
            WorkflowStep(
                id="s2", name="s2", instruction="", step_type="code",
                invokes=["cap_b"],
            ),
        ],
    )
    registry = {"cap_a": cap_a, "cap_b": cap_b, "wf3": wf}
    _compute_workflow_requires(registry)
    # Sorted union: hindsight, obsidian, postgresql — each appearing once
    assert wf.requires == ["hindsight", "obsidian", "postgresql"]


def test_compute_workflow_requires_skips_unknown_capability():
    """Invoking a capability that isn't in the registry is a no-op (not an error)."""
    wf = WorkflowDefinition(
        name="wf4", description="", workflow_file="store:test", execution="main",
        steps=[
            WorkflowStep(
                id="s1", name="s1", instruction="", step_type="reasoning",
                invokes=["does_not_exist"],
            ),
        ],
    )
    registry = {"wf4": wf}
    _compute_workflow_requires(registry)
    assert wf.requires == []


def test_workflow_step_invokes_parsed_from_store_dict():
    """`_discover_workflows_from_store` reads `invokes` from step dicts.

    This test fakes the store load to exercise the parsing path without
    depending on the real workflow-unit shape.
    """
    from work_buddy.mcp_server import registry as reg_mod
    from work_buddy.knowledge.model import WorkflowUnit

    unit = WorkflowUnit(
        path="test/wf",
        name="wf5",
        description="",
        workflow_name="wf5",
        steps=[{
            "id": "s1",
            "name": "s1",
            "step_type": "reasoning",
            "invokes": ["cap_x", "cap_y"],
            "requires": [],
            "depends_on": [],
        }],
        step_instructions={"s1": ""},
        execution="main",
    )

    import unittest.mock as mock

    with mock.patch.object(
        reg_mod, "load_store", create=True, return_value={"test/wf": unit},
    ):
        # The function imports load_store inline, so patch inside the module it imports from
        with mock.patch("work_buddy.knowledge.store.load_store",
                        return_value={"test/wf": unit}):
            workflows = reg_mod._discover_workflows_from_store()

    assert len(workflows) == 1
    wf = workflows[0]
    assert wf.name == "wf5"
    assert wf.steps[0].invokes == ["cap_x", "cap_y"]
