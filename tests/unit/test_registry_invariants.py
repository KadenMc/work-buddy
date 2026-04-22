"""Phase D — CI-time invariants for the MCP registry.

Fast-failing assertions that run on every `pytest tests/unit/` pass.
Catches common drift modes as the `invokes` backfill (Phase C) rolls
out:

    1. Every `invokes` entry (on Capability or WorkflowStep) must name
       an existing registry entry. Typos, renames, and dangling
       references all surface here.
    2. Every workflow step with auto_run must have a callable that
       starts with `work_buddy.` (the conductor's import-security rule).
    3. WorkflowDefinition.requires must actually equal the computed
       union — guards against someone hand-editing this field.

The test exercises the REAL registry (no mocks). If you add a new
capability that genuinely should have no invocations, either:

    - Leave `invokes` unset (defaults to []). The invariants treat
      missing as empty — no audit marker required.
    - Leave it genuinely empty with `invokes=[]`. Same result.

If the test FAILS, the error message names the offending entry
directly — do not edit this test to make it pass unless the invariant
itself changed.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def registry():
    from work_buddy.mcp_server.registry import get_registry
    return get_registry()


# ---------------------------------------------------------------------------
# Invariant 1 — every `invokes` points at a real capability/workflow
# ---------------------------------------------------------------------------

def test_every_invokes_entry_resolves(registry):
    """Every name in Capability.invokes or WorkflowStep.invokes exists in the registry.

    Exceptions: a tiny allowlist for names that appear in prose/docs
    today but haven't been promoted to real registry entries yet. Add
    here sparingly.
    """
    from work_buddy.mcp_server.registry import Capability, WorkflowDefinition

    # These names are referenced in agent prose (not Python `invokes`)
    # and may eventually become real capabilities. If you grep the
    # knowledge store and don't find them as `workflow_name` or
    # `Capability.name`, they belong here.
    allowlist: set[str] = set()  # intentionally empty for now

    errors: list[str] = []
    for name, entry in registry.items():
        if isinstance(entry, Capability):
            for invoked in entry.invokes:
                if invoked not in registry and invoked not in allowlist:
                    errors.append(
                        f"Capability '{name}' invokes '{invoked}' which is not registered"
                    )
        elif isinstance(entry, WorkflowDefinition):
            for step in entry.steps:
                for invoked in step.invokes:
                    if invoked not in registry and invoked not in allowlist:
                        errors.append(
                            f"Workflow '{name}' step '{step.id}' invokes "
                            f"'{invoked}' which is not registered"
                        )

    assert not errors, "Dangling invokes references:\n  " + "\n  ".join(errors)


# ---------------------------------------------------------------------------
# Invariant 2 — auto_run callables use work_buddy.* prefix
# ---------------------------------------------------------------------------

def test_auto_run_callables_scoped_to_work_buddy(registry):
    """Every step.auto_run.callable starts with 'work_buddy.'.

    The conductor enforces this at execution time (security: prevents
    the workflow store from naming arbitrary import paths). Failing
    here early in CI is kinder than failing at runtime.
    """
    from work_buddy.mcp_server.registry import WorkflowDefinition

    errors: list[str] = []
    for name, entry in registry.items():
        if not isinstance(entry, WorkflowDefinition):
            continue
        for step in entry.steps:
            if step.auto_run is None:
                continue
            path = step.auto_run.callable
            if not path.startswith("work_buddy."):
                errors.append(
                    f"Workflow '{name}' step '{step.id}': auto_run.callable "
                    f"'{path}' does not start with 'work_buddy.'"
                )
    assert not errors, "Bad auto_run.callable paths:\n  " + "\n  ".join(errors)


# ---------------------------------------------------------------------------
# Invariant 3 — computed WorkflowDefinition.requires matches the resolver
# ---------------------------------------------------------------------------

def test_workflow_requires_matches_one_hop_union(registry):
    """WorkflowDefinition.requires should equal the one-hop step.requires ∪
    step.invokes-resolved .requires union.

    Guards against hand-edits to workflows.json that try to set
    workflow-level `requires` directly — the field is supposed to be
    computed, so any hand-authored value would be silently overwritten
    at the next registry rebuild.
    """
    from work_buddy.mcp_server.registry import (
        Capability,
        WorkflowDefinition,
        _compute_workflow_requires,
    )

    # Snapshot current workflow requires
    snapshots: dict[str, list[str]] = {
        name: list(entry.requires)
        for name, entry in registry.items()
        if isinstance(entry, WorkflowDefinition)
    }

    # Re-run the computation against a fresh copy — if the result
    # differs from what the registry currently holds, something
    # out-of-band has diverged.
    recomputed_registry = dict(registry)
    for name in list(recomputed_registry):
        entry = recomputed_registry[name]
        if isinstance(entry, WorkflowDefinition):
            # Build a shallow copy so we don't mutate shared state
            wf_copy = WorkflowDefinition(
                name=entry.name,
                description=entry.description,
                workflow_file=entry.workflow_file,
                execution=entry.execution,
                allow_override=entry.allow_override,
                steps=list(entry.steps),
                context=entry.context,
                slash_command=entry.slash_command,
                requires=[],
            )
            recomputed_registry[name] = wf_copy

    _compute_workflow_requires(recomputed_registry)

    errors: list[str] = []
    for name, before in snapshots.items():
        after = list(recomputed_registry[name].requires)
        if sorted(before) != sorted(after):
            errors.append(
                f"Workflow '{name}'.requires diverged:\n"
                f"    registry: {sorted(before)}\n"
                f"    recomputed: {sorted(after)}"
            )
    assert not errors, "\n".join(errors)


# ---------------------------------------------------------------------------
# Invariant 4 — morning-routine's invokes entries cover obsidian + google_calendar
# ---------------------------------------------------------------------------

def test_morning_routine_requires_includes_core_components(registry):
    """Defense against accidental removal of morning-routine's invokes lists.

    If someone edits workflows.json and breaks the flagship backfill,
    this test surfaces it immediately rather than silently at runtime.
    """
    wf = registry.get("morning-routine")
    assert wf is not None, "morning-routine workflow must be in the registry"
    assert "obsidian" in wf.requires, (
        f"morning-routine.requires lost 'obsidian'. Current: {wf.requires}"
    )
    assert "google_calendar" in wf.requires, (
        f"morning-routine.requires lost 'google_calendar'. Current: {wf.requires}"
    )
