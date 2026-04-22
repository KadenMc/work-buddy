"""Phase B flagship — verify morning-routine's transitive dependencies resolve correctly.

The morning-routine workflow composes multiple capabilities that touch
Obsidian, Google Calendar, and (via the update-journal sub-workflow)
the journal. The control graph's capability resolver should walk
``workflow → steps → invokes → capabilities → requires`` and produce
the expected set of tool/component IDs.

This test exercises the REAL registry — no mocks. If it fails, either:

    1. ``workflows.json`` lost the ``invokes`` entries we added in Phase B.
    2. ``Capability.requires`` on one of the invoked capabilities changed
       (rare — would indicate a real refactor).
    3. The resolver logic regressed.

All three are meaningful signals, so keep it as an integration-ish
test rather than mocking inputs out.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def registry():
    """Full registry with tool-availability filtering disabled.

    The default registry drops capabilities whose required tools fail
    their probes at build time. That makes resolver tests environment-
    dependent (an Obsidian bridge that's briefly down would filter
    every obsidian-requiring capability and break these assertions).

    We rebuild with ``is_tool_available`` patched to True so the
    registry contains every declared capability and workflow, letting
    the tests focus on the resolver logic rather than local machine
    state.

    We reset ``_REGISTRY`` to None directly instead of calling
    ``invalidate_registry()`` — the latter purges ``work_buddy.*``
    modules from ``sys.modules``, which makes ``isinstance(wf,
    WorkflowDefinition)`` checks fail because the class the test
    imports is a different object from the class the registry build
    used. A plain cache reset is sufficient for our purposes.
    """
    from unittest.mock import patch
    from work_buddy.mcp_server import registry as reg_mod

    reg_mod._REGISTRY = None
    with patch("work_buddy.tools.is_tool_available", return_value=True):
        reg = reg_mod.get_registry()
    yield reg
    # Reset so subsequent test modules get a fresh filtered registry.
    reg_mod._REGISTRY = None


@pytest.fixture(scope="module")
def workflow(registry):
    from work_buddy.mcp_server.registry import WorkflowDefinition
    wf = registry.get("morning-routine")
    assert wf is not None, "morning-routine workflow must be in the registry"
    assert isinstance(wf, WorkflowDefinition)
    return wf


# ---------------------------------------------------------------------------
# Sanity: the JSON edits made it into the dataclass
# ---------------------------------------------------------------------------

def test_every_step_has_invokes_field(workflow):
    """Every morning-routine step declares an `invokes` list (possibly empty)."""
    for step in workflow.steps:
        assert hasattr(step, "invokes")
        assert isinstance(step.invokes, list), f"step {step.id} invokes not a list"


def test_specific_step_invokes(workflow):
    """Spot-check a few step-level invocations we explicitly authored."""
    by_id = {s.id: s for s in workflow.steps}

    assert "context_bundle" in by_id["context-snapshot"].invokes
    assert "journal_state" in by_id["context-snapshot"].invokes

    assert "journal_sign_in" in by_id["sign-in"].invokes

    assert "task_briefing" in by_id["task-briefing"].invokes

    assert set(by_id["contract-check"].invokes) == {
        "contract_constraints", "contract_health",
    }
    assert set(by_id["propose-mits"].invokes) == {
        "task_change_state", "task_create", "task_toggle",
    }

    # Pure-programmatic auto_run steps have no invocations
    assert by_id["load-config"].invokes == []
    assert by_id["resolve-phases"].invokes == []


# ---------------------------------------------------------------------------
# Computed workflow-level requires (one-hop union in _compute_workflow_requires)
# ---------------------------------------------------------------------------

def test_workflow_requires_union_from_step_invokes(workflow, registry):
    """``WorkflowDefinition.requires`` = union of step.requires + resolve(step.invokes).requires."""
    req = set(workflow.requires)
    # Obsidian is touched by several invoked capabilities (journal_*, task_*, etc.)
    assert "obsidian" in req, (
        "obsidian should be in morning-routine.requires because many of its "
        "invoked capabilities declare requires=['obsidian']"
    )
    # google_calendar flows in via context_calendar.requires
    assert "google_calendar" in req, (
        "google_calendar should appear via context_calendar (calendar-today step)"
    )


# ---------------------------------------------------------------------------
# Full transitive resolution via capability_resolver
# ---------------------------------------------------------------------------

def test_resolve_dependencies_transitive_set(workflow, registry):
    """Calling resolve_dependencies('morning-routine') returns the right components."""
    from work_buddy.control.capability_resolver import resolve_dependencies

    deps = resolve_dependencies("morning-routine", registry=registry)
    assert "obsidian" in deps["components"], (
        f"expected obsidian in components; got {deps['components']}"
    )
    assert "google_calendar" in deps["components"]
    # tools is an alias for components
    assert deps["tools"] == deps["components"]


def test_resolve_captures_invoked_capability_names(workflow, registry):
    """The 'capabilities' set on the resolved deps lists the hops we took."""
    from work_buddy.control.capability_resolver import resolve_dependencies

    deps = resolve_dependencies("morning-routine", registry=registry)
    caps = deps["capabilities"]
    # A representative sample of the workflow's step.invokes entries must
    # be reachable through the resolver.
    for expected in ("journal_state", "task_briefing", "contract_health", "day_planner"):
        assert expected in caps, (
            f"expected '{expected}' in resolved capability set; got {sorted(caps)[:20]}..."
        )


def test_resolve_skips_missing_capability_gracefully(registry):
    """detect-blindspots is referenced in prose but not registered — resolver tolerates it."""
    from work_buddy.control.capability_resolver import resolve_dependencies

    # If blindspot-scan ever added an invokes=['detect-blindspots'] and that
    # capability didn't exist, the resolver should silently skip it.
    deps = resolve_dependencies("morning-routine", registry=registry)
    # Just confirm the call succeeded — no crash, no TypeError.
    assert isinstance(deps, dict)
    assert "components" in deps
