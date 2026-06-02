"""Tests for conductor delivery of bound directions to bare reasoning steps.

When the conductor serves an instruction-less ``reasoning`` step, it resolves
the workflow's bound directions unit (``WorkflowDefinition.bound_directions_path``,
precomputed by the registry) and delivers its rendered content as the step's
instruction — so the directions reach the agent regardless of entry path
(slash command, nested ``wb_run``, or headless). When there is no binding the
step still gets the empty-instruction warning (the real defect). Any rendering
failure degrades to that same warning, never an exception.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge.model import DirectionsUnit


@pytest.fixture(autouse=True)
def _isolate_agents_dir(tmp_agents_dir):
    """Redirect agent-session writes (DAG persistence, consent.db) to a temp
    dir — start_workflow persists a DAG and mints a consent grant."""
    yield


def _register_reasoning_workflow(
    name: str,
    *,
    instruction: str = "",
    bound_directions_path: str | None = None,
):
    """Register a single-step reasoning workflow into the live registry.

    Returns the workflow name; caller is responsible for cleanup via the
    returned teardown (the autouse registry pop in each test).
    """
    from work_buddy.mcp_server.registry import (
        WorkflowDefinition,
        WorkflowStep,
        get_registry,
    )

    wf = WorkflowDefinition(
        name=name,
        description="Test fixture for bound-directions delivery.",
        workflow_file="test:in-memory",
        execution="main",
        steps=[
            WorkflowStep(
                id="think",
                name="Think (reasoning)",
                step_type="reasoning",
                depends_on=[],
                instruction=instruction,
            ),
        ],
        bound_directions_path=bound_directions_path,
    )
    get_registry()[name] = wf
    return wf


def _fake_store_with_directions(path: str, full: str):
    """A synthetic store dict holding one DirectionsUnit at ``path``."""
    return {
        path: DirectionsUnit(
            path=path, name="Bound Directions", description="d",
            content={"full": full}, workflow="ignored-here",
        )
    }


def test_bound_directions_delivered_to_bare_reasoning_step(monkeypatch):
    from work_buddy.mcp_server import conductor

    name = "test_bound_delivery"
    _register_reasoning_workflow(
        name, instruction="", bound_directions_path="test/dir",
    )
    sentinel = "FORMAT RULE: prefix every entry with #projects/<slug>."
    monkeypatch.setattr(
        "work_buddy.knowledge.store.load_store",
        lambda *a, **k: _fake_store_with_directions("test/dir", sentinel),
    )

    resp = conductor.start_workflow(name)
    try:
        step = resp["current_step"]
        assert step["id"] == "think"
        # The directions content is delivered into the served instruction...
        assert sentinel in step["instruction"]
        # ...with a provenance pointer to its source unit.
        assert step.get("directions_source") == "test/dir"
        assert "test/dir" in step["instruction"]
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)
        from work_buddy.mcp_server.registry import get_registry
        get_registry().pop(name, None)


def test_unbound_bare_reasoning_step_warns(monkeypatch, caplog):
    import logging

    from work_buddy.mcp_server import conductor

    name = "test_unbound_bare"
    _register_reasoning_workflow(name, instruction="", bound_directions_path=None)

    with caplog.at_level(logging.WARNING):
        resp = conductor.start_workflow(name)
    try:
        step = resp["current_step"]
        assert step["instruction"] == ""          # nothing to deliver
        assert "directions_source" not in step
        assert "no bound directions" in caplog.text
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)
        from work_buddy.mcp_server.registry import get_registry
        get_registry().pop(name, None)


def test_delivery_failure_degrades_to_warning(monkeypatch, caplog):
    """A binding that exists but cannot be rendered (store raises / unit
    missing) must not crash the step — it falls back to the warning."""
    import logging

    from work_buddy.mcp_server import conductor

    name = "test_delivery_failure"
    _register_reasoning_workflow(
        name, instruction="", bound_directions_path="test/missing",
    )

    def _boom(*a, **k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("work_buddy.knowledge.store.load_store", _boom)

    with caplog.at_level(logging.WARNING):
        resp = conductor.start_workflow(name)   # must not raise
    try:
        step = resp["current_step"]
        assert step["instruction"] == ""
        assert "directions_source" not in step
        # both the delivery-failure warning and the fallback warning fire
        assert "bound-directions delivery failed" in caplog.text
        assert "no bound directions" in caplog.text
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)
        from work_buddy.mcp_server.registry import get_registry
        get_registry().pop(name, None)


class _StubDag:
    """Minimal stand-in for a WorkflowDAG: ``_get_wf_def`` only reads ``.name``
    (``"<workflow_name>:<run_id>"``)."""

    def __init__(self, name: str):
        self.name = name


def test_real_update_journal_resolves_its_bound_directions():
    """Integration proof against the REAL registry + store: the actual
    ``update-journal`` workflow (whose ``synthesize``/``write`` reasoning steps
    are bare) resolves and renders its real bound directions unit. This is the
    nested-invocation gap the change exists for — exercised end-to-end through
    the registry binding and the tier() renderer, without running the
    workflow's collectors or restarting the MCP server."""
    from work_buddy.mcp_server import conductor

    rendered, src = conductor._resolve_bound_directions(
        _StubDag("update-journal:wf_test")
    )
    assert src == "journal/update-directions"
    assert rendered is not None
    # Sentinels from journal/update-directions — the Log-entry format rules the
    # agent would otherwise reach blind on the nested (morning-routine) path.
    assert "#projects/" in rendered
    assert "wb/journal/log" in rendered


def test_inline_instruction_is_not_overridden(monkeypatch):
    """A reasoning step that already carries an inline instruction must be
    served verbatim — delivery only fills genuinely-empty instructions."""
    from work_buddy.mcp_server import conductor

    name = "test_inline_kept"
    _register_reasoning_workflow(
        name, instruction="Do the inline thing.", bound_directions_path="test/dir",
    )

    # If delivery were (wrongly) attempted, this sentinel would appear.
    monkeypatch.setattr(
        "work_buddy.knowledge.store.load_store",
        lambda *a, **k: _fake_store_with_directions("test/dir", "SHOULD NOT APPEAR"),
    )

    resp = conductor.start_workflow(name)
    try:
        step = resp["current_step"]
        assert step["instruction"] == "Do the inline thing."
        assert "SHOULD NOT APPEAR" not in step["instruction"]
        assert "directions_source" not in step
    finally:
        conductor._ACTIVE_RUNS.pop(resp.get("workflow_run_id"), None)
        from work_buddy.mcp_server.registry import get_registry
        get_registry().pop(name, None)
