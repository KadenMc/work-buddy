"""Conductor step-result schema validation, trimming, and store parsing.

Covers how ``advance_workflow`` treats a step's declared ``result_schema``
(missing keys, type mismatches, the empty-result parameter-name hint, and
steps with no schema), how prior step results are trimmed and capped, and
that ``result_schema`` survives loading workflows from the knowledge store.
"""

from __future__ import annotations

import pytest

from work_buddy.mcp_server import conductor
from work_buddy.mcp_server.conductor import (
    _cap_step_results,
    _relevant_step_results,
    advance_workflow,
)
from work_buddy.mcp_server.registry import _discover_workflows_from_store
from work_buddy.workflow import WorkflowDAG


@pytest.fixture(autouse=True)
def _isolate_agents_dir(tmp_agents_dir):
    """Give every test its own agent-session directory.

    Starting, completing, and advancing steps persists the DAG under the
    session's ``workflows/`` folder. A per-test directory keeps those
    writes out of the live data root and keeps parallel workers from
    writing and renaming the same file.
    """
    yield


def _two_step_run(
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    name: str,
    result_schema: dict | None,
) -> WorkflowDAG:
    """Register a running two-step DAG whose first step may declare a schema."""
    first_metadata = {"step_type": "reasoning", "instruction": "test"}
    if result_schema is not None:
        first_metadata["result_schema"] = result_schema
    dag = WorkflowDAG(name=name, description="test")
    dag.add_task("step1", name="First step", metadata=first_metadata)
    dag.add_task(
        "step2",
        name="Next step",
        depends_on=["step1"],
        metadata={"step_type": "reasoning", "instruction": "test"},
    )
    monkeypatch.setitem(conductor._ACTIVE_RUNS, run_id, dag)
    dag.start_task("step1")
    return dag


GROUPS_SCHEMA = {
    "required_keys": ["groups_by_action", "total_groups"],
    "key_types": {"groups_by_action": "dict", "total_groups": "int"},
}


def test_missing_required_key_is_rejected_and_step_stays_running(monkeypatch):
    dag = _two_step_run(monkeypatch, "wf_test1", "test-schema:wf_test1", GROUPS_SCHEMA)

    result = advance_workflow("wf_test1", {"presentation_updated": True, "changes": []})

    assert result.get("type") == "validation_error", f"got type={result.get('type')}"
    assert "groups_by_action" in result.get("error", ""), result.get("error", "")
    assert "hint" in result
    assert dag._graph.nodes["step1"].get("status") == "running"


def test_valid_result_is_accepted_after_a_rejected_attempt(monkeypatch):
    dag = _two_step_run(monkeypatch, "wf_test1", "test-schema:wf_test1", GROUPS_SCHEMA)
    advance_workflow("wf_test1", {"presentation_updated": True, "changes": []})

    result = advance_workflow("wf_test1", {"groups_by_action": {}, "total_groups": 5})

    assert result.get("type") in ("workflow_step", "workflow_complete"), (
        f"got type={result.get('type')}"
    )
    assert dag._graph.nodes["step1"].get("status") == "completed"


def test_type_mismatch_is_rejected_with_expected_and_actual_types(monkeypatch):
    _two_step_run(
        monkeypatch,
        "wf_test2",
        "test-schema2:wf_test2",
        {"required_keys": ["data"], "key_types": {"data": "dict"}},
    )

    result = advance_workflow("wf_test2", {"data": "not a dict"})

    assert result.get("type") == "validation_error", f"got type={result.get('type')}"
    error = result.get("error", "")
    assert "dict" in error and "str" in error, error


# When the agent forgets to pass step_result (or names the kwarg incorrectly
# so FastMCP drops it), the conductor sees an empty dict against a schema that
# requires keys. The generic "make sure your result dict has all the fields"
# hint misdirects in that case, because the real cause is upstream of the
# dict's contents. The error message and the hint both surface a
# parameter-name nudge instead.
FILES_SCHEMA = {"required_keys": ["units_read", "files_read"]}


def test_empty_result_points_at_the_step_result_parameter(monkeypatch):
    dag = _two_step_run(monkeypatch, "wf_test2b", "test-empty:wf_test2b", FILES_SCHEMA)

    # The gateway's _parse_params turns an omitted step_result into {}.
    result = advance_workflow("wf_test2b", {})

    assert result.get("type") == "validation_error", f"got type={result.get('type')}"
    error = result.get("error", "")
    hint = result.get("hint", "")
    assert "step_result" in error, error
    assert "result=" in error, error
    assert "step_result" in hint and "FastMCP" in hint, hint
    assert "presentation dict" not in hint, hint
    assert dag._graph.nodes["step1"].get("status") == "running"


def test_non_empty_result_missing_a_key_keeps_the_generic_hint(monkeypatch):
    # The parameter-name nudge is only for the empty case. A non-empty result
    # that still misses a key gets the generic data-structure framing.
    _two_step_run(monkeypatch, "wf_test2b", "test-empty:wf_test2b", FILES_SCHEMA)

    result = advance_workflow("wf_test2b", {"units_read": ["a"]})

    assert result.get("type") == "validation_error", f"got type={result.get('type')}"
    hint = result.get("hint", "")
    assert "FastMCP" not in hint and "presentation dict" in hint, hint
    assert "FastMCP" not in result.get("error", ""), result.get("error", "")


def test_step_without_a_schema_accepts_a_free_form_result(monkeypatch):
    _two_step_run(monkeypatch, "wf_test3", "test-noschema:wf_test3", None)

    result = advance_workflow("wf_test3", "literally anything")

    assert result.get("type") in ("workflow_step", "workflow_complete"), (
        f"got type={result.get('type')}"
    )


def test_relevant_step_results_without_a_workflow_definition_include_all():
    dag = WorkflowDAG(name="test-trim:wf_test4", description="test")
    for i in range(5):
        dag.add_task(
            f"s{i}",
            name=f"Step {i}",
            depends_on=[f"s{i - 1}"] if i > 0 else None,
            metadata={"step_type": "reasoning", "instruction": "test"},
        )
    for i in range(4):
        dag.start_task(f"s{i}")
        dag.complete_task(f"s{i}", result={"data": f"result_{i}", "big": "x" * 1000})

    # Without a registered WorkflowDefinition, trimming falls back to cap-all.
    trimmed = _relevant_step_results(dag, "s4", prior_step_id="s3")

    assert len(trimmed) == 4, f"expected 4, got {len(trimmed)} keys={list(trimmed)}"


def test_cap_step_results_truncates_oversized_results():
    capped = _cap_step_results({f"s{i}": {"data": "x" * 60000} for i in range(4)})

    oversized = [k for k, v in capped.items() if isinstance(v, dict) and v.get("_truncated")]
    assert len(oversized) == 4, f"expected 4 truncated, got {len(oversized)}"


def test_store_workflow_threads_result_schema_into_steps():
    # task-new is a stable fixture: its two reasoning steps (plan, confirm)
    # carry result_schema, so it shows whether _discover_workflows_from_store
    # threads the schema through into WorkflowStep.
    wf = next((w for w in _discover_workflows_from_store() if w.name == "task-new"), None)
    assert wf is not None, "task-new workflow not loaded from the knowledge store"

    steps = {s.id: s for s in wf.steps}
    plan = steps.get("plan")
    assert plan is not None and plan.result_schema is not None
    assert plan.result_schema.get("required_keys") == ["task_text"], plan.result_schema
    confirm = steps.get("confirm")
    assert confirm is not None and confirm.result_schema is not None
