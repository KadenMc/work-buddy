"""Tests for the registry-build-time directions binding.

``_index_directions_by_workflow`` builds the reverse map (workflow store
path -> bound directions unit path) and ``_discover_workflows_from_store``
stamps each ``WorkflowDefinition`` with ``bound_directions_path``. The
conductor reads that field to deliver a workflow's bound directions to its
instruction-less reasoning steps on every entry path (see
``test_conductor_bound_directions.py``).

Unit tests use synthetic store dicts (mirroring
``test_knowledge_validate.py``); the integration test exercises the real
store + registry (mirroring ``test_dev_document_validate_step.py``).
"""

from __future__ import annotations

from work_buddy.knowledge.model import DirectionsUnit, WorkflowUnit
from work_buddy.mcp_server.registry import (
    WorkflowDefinition,
    _discover_workflows_from_store,
    _index_directions_by_workflow,
)


def _wf(path: str) -> WorkflowUnit:
    return WorkflowUnit(
        path=path,
        name=path,
        description="d",
        workflow_name=path.replace("/", "-"),
        steps=[{"id": "a", "step_type": "reasoning", "depends_on": []}],
    )


class TestIndexDirectionsByWorkflow:
    """The reverse index keys on the workflow STORE PATH (what
    ``DirectionsUnit.workflow`` holds), never the slug."""

    def test_maps_workflow_path_to_directions_path(self):
        wf = _wf("daily-journal/update-journal")
        directions = DirectionsUnit(
            path="journal/update-directions",
            name="Journal Update Directions",
            description="d",
            workflow="daily-journal/update-journal",
        )
        store = {wf.path: wf, directions.path: directions}
        idx = _index_directions_by_workflow(store)
        assert idx == {"daily-journal/update-journal": "journal/update-directions"}

    def test_keyed_on_path_not_slug(self):
        # The binding must be keyed on the store path. If it were keyed on the
        # slug ("update-journal"), this lookup would miss.
        wf = _wf("daily-journal/update-journal")
        directions = DirectionsUnit(
            path="journal/update-directions", name="X", description="d",
            workflow="daily-journal/update-journal",
        )
        idx = _index_directions_by_workflow({wf.path: wf, directions.path: directions})
        assert "daily-journal/update-journal" in idx
        assert "update-journal" not in idx

    def test_ignores_non_directions_and_unbound(self):
        wf = _wf("x")
        unbound = DirectionsUnit(path="d1", name="D1", description="d")  # workflow=None
        other = WorkflowUnit(
            path="y", name="y", description="d", workflow_name="y", steps=[],
        )
        idx = _index_directions_by_workflow(
            {wf.path: wf, unbound.path: unbound, other.path: other}
        )
        assert idx == {}

    def test_empty_store(self):
        assert _index_directions_by_workflow({}) == {}


class TestBoundDirectionsPathOnDefinition:
    """Integration: the real store + registry stamps ``bound_directions_path``
    onto each ``WorkflowDefinition``."""

    def test_bound_and_unbound_workflows(self):
        wfs = {w.name: w for w in _discover_workflows_from_store()}

        # Bound workflows with bare reasoning steps — the ones the delivery
        # fix exists for.
        assert wfs["update-journal"].bound_directions_path == "journal/update-directions"
        assert wfs["collect-and-orient"].bound_directions_path == "context/collect-directions"

        # A workflow with no bound directions unit resolves to None.
        assert wfs["docs-edit"].bound_directions_path is None

    def test_default_is_none(self):
        # The field defaults to None so any WorkflowDefinition built without
        # the binding (e.g. a test fixture) is backward-safe.
        wf = WorkflowDefinition(
            name="t", description="d", workflow_file="test:x", execution="main",
        )
        assert wf.bound_directions_path is None
