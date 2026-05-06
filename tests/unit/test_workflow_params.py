"""Tests for caller-provided initial params on workflows.

Covers:
1. ``_validate_workflow_params`` strict policy (no schema rejects non-empty;
   schema enforces required + rejects unknown).
2. ``_resolve_params_source`` dotted-key walk.
3. ``WorkflowDAG`` round-trip preserves ``initial_params`` across save/load.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-workflow-params")

from work_buddy.mcp_server.conductor import (
    _resolve_params_source,
    _validate_workflow_params,
)
from work_buddy.mcp_server.registry import WorkflowDefinition, WorkflowStep


def _wf(params_schema=None) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="test-wf",
        description="for validation tests",
        workflow_file="store:test/test-wf",
        execution="main",
        steps=[WorkflowStep(id="s1", name="s1", instruction="x", step_type="code")],
        params_schema=params_schema or {},
    )


# --- Validation ---

def test_validate_no_schema_accepts_empty():
    ok, err = _validate_workflow_params(_wf(), None)
    assert ok and err is None
    ok, err = _validate_workflow_params(_wf(), {})
    assert ok and err is None


def test_validate_no_schema_rejects_non_empty():
    ok, err = _validate_workflow_params(_wf(), {"x": 1})
    assert ok is False
    assert "no params_schema" in err
    assert "x" in err


def test_validate_with_schema_accepts_valid():
    schema = {"x": {"type": "str", "required": True}, "y": {"type": "int"}}
    ok, err = _validate_workflow_params(_wf(schema), {"x": "hello"})
    assert ok and err is None
    ok, err = _validate_workflow_params(_wf(schema), {"x": "hello", "y": 7})
    assert ok and err is None


def test_validate_rejects_missing_required():
    schema = {"x": {"type": "str", "required": True}, "y": {"type": "int"}}
    ok, err = _validate_workflow_params(_wf(schema), {"y": 7})
    assert ok is False
    assert "Missing required" in err
    assert "x" in err


def test_validate_rejects_unknown_keys():
    schema = {"x": {"type": "str", "required": True}}
    ok, err = _validate_workflow_params(_wf(schema), {"x": "a", "junk": 1})
    assert ok is False
    assert "Unknown" in err
    assert "junk" in err


def test_validate_with_schema_accepts_empty_when_nothing_required():
    schema = {"x": {"type": "str"}}  # not required
    ok, err = _validate_workflow_params(_wf(schema), {})
    assert ok and err is None


# --- __params__ source resolution ---

def test_resolve_params_whole_dict():
    found, value = _resolve_params_source("__params__", {"a": 1, "b": 2})
    assert found is True
    assert value == {"a": 1, "b": 2}


def test_resolve_params_single_key():
    found, value = _resolve_params_source("__params__.a", {"a": 1, "b": 2})
    assert found is True
    assert value == 1


def test_resolve_params_nested_walk():
    found, value = _resolve_params_source(
        "__params__.outer.inner",
        {"outer": {"inner": "hit"}},
    )
    assert found is True
    assert value == "hit"


def test_resolve_params_missing_key():
    found, value = _resolve_params_source("__params__.missing", {"a": 1})
    assert found is False
    assert value is None


def test_resolve_params_missing_nested():
    found, value = _resolve_params_source(
        "__params__.outer.missing",
        {"outer": {"present": 1}},
    )
    assert found is False


def test_resolve_params_non_param_source_returns_not_found():
    # Sources not starting with __params__ are not this resolver's job.
    found, value = _resolve_params_source("other-step", {"a": 1})
    assert found is False
    assert value is None


def test_resolve_params_empty_initial_params():
    found, value = _resolve_params_source("__params__", None)
    assert found is True  # __params__ alone resolves; whole dict is empty
    assert value == {}
    found, value = _resolve_params_source("__params__.a", None)
    assert found is False


# --- DAG persistence ---

def test_dag_persists_initial_params(tmp_path, monkeypatch):
    """initial_params survives _save → load round-trip."""
    from work_buddy import workflow as wf_module
    from work_buddy.workflow import WorkflowDAG

    # Redirect get_session_dir so the save lands in tmp_path/agents/...
    monkeypatch.setattr(wf_module, "get_session_dir", lambda: tmp_path)

    dag = WorkflowDAG(name="rt-test", description="round trip")
    dag.add_task(task_id="s1", name="s1")
    dag.initial_params = {"project": "alpha", "limit": 5}  # type: ignore[attr-defined]
    saved_path = dag.save()

    loaded = WorkflowDAG.load(saved_path)
    assert getattr(loaded, "initial_params", None) == {"project": "alpha", "limit": 5}


def test_start_workflow_rejects_invalid_params(monkeypatch):
    """End-to-end: start_workflow returns {error:...} when params fail validation."""
    from work_buddy.mcp_server import conductor as conductor_mod

    # Stub get_entry to return a workflow with a strict schema.
    schema = {"x": {"type": "str", "required": True}}
    fake_entry = _wf(schema)
    monkeypatch.setattr(conductor_mod, "get_entry", lambda name: fake_entry)

    # Missing required key → error
    result = conductor_mod.start_workflow("test-wf", params={})
    assert "error" in result
    assert "Missing required" in result["error"]

    # Unknown key → error
    result = conductor_mod.start_workflow("test-wf", params={"x": "ok", "junk": 1})
    assert "error" in result
    assert "Unknown" in result["error"]


def test_start_workflow_stores_initial_params(monkeypatch, tmp_path):
    """start_workflow stashes valid params on the DAG and includes them in response."""
    from work_buddy.mcp_server import conductor as conductor_mod
    from work_buddy import workflow as wf_module

    schema = {"x": {"type": "str", "required": True}}
    fake_entry = _wf(schema)
    monkeypatch.setattr(conductor_mod, "get_entry", lambda name: fake_entry)
    monkeypatch.setattr(wf_module, "get_session_dir", lambda: tmp_path)
    # Don't actually grant consent
    monkeypatch.setattr(
        conductor_mod, "grant_workflow_consent",
        lambda *a, **kw: None, raising=False,
    )

    response = conductor_mod.start_workflow("test-wf", params={"x": "hello"})
    assert "error" not in response
    assert response.get("initial_params") == {"x": "hello"}

    run_id = response["workflow_run_id"]
    dag = conductor_mod._ACTIVE_RUNS[run_id]
    assert getattr(dag, "initial_params") == {"x": "hello"}


def test_dag_load_handles_missing_initial_params(tmp_path, monkeypatch):
    """Save files written before this feature must still load (initial_params=None)."""
    import json

    from work_buddy import workflow as wf_module
    from work_buddy.workflow import WorkflowDAG

    monkeypatch.setattr(wf_module, "get_session_dir", lambda: tmp_path)

    # Hand-craft an old-format save (no initial_params field)
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(exist_ok=True)
    legacy_path = wf_dir / "legacy.json"
    legacy_path.write_text(json.dumps({
        "name": "legacy",
        "description": "old save",
        "created_at": "2026-01-01T00:00:00+00:00",
        "saved_at": "2026-01-01T00:00:00+00:00",
        "nodes": {},
        "edges": [],
    }))

    loaded = WorkflowDAG.load(legacy_path)
    assert getattr(loaded, "initial_params", "SENTINEL") is None
