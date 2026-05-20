"""workflow_create / workflow_update must round-trip DAG content through
the MCP-facing JSON-string schema without silently dropping fields.

Background: ``docs_create`` intentionally handles only prose-shaped units
(directions, system). Workflow units carry a ``steps`` DAG, per-step
``step_instructions``, and workflow-level knobs (``workflow_name``,
``execution``, ``allow_override``) that don't fit the prose schema.
``workflow_create`` / ``workflow_update`` are the sanctioned surface for
those — this test pins the round-trip so a future schema change can't
silently re-introduce the hand-edit-JSON gap.
"""

from __future__ import annotations

import pytest

from work_buddy.knowledge import editor


@pytest.fixture
def tmp_store(tmp_path):
    """Point the editor at a throwaway store.

    Same pattern as test_docs_dev_notes — manual save/restore so the
    cache invalidation order is right (restore real _STORE_DIR BEFORE
    the final rebuild, or later tests break).
    """
    from work_buddy.knowledge import store as store_mod

    from work_buddy.knowledge import file_store

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    # Seed a `dev` parent so workflows can point their parents at it.
    file_store.write_unit(store_dir, "dev", {
        "kind": "system", "name": "Dev", "description": "root",
    })

    saved_editor = editor._STORE_DIR
    saved_store = store_mod._STORE_DIR
    editor._STORE_DIR = store_dir
    store_mod._STORE_DIR = store_dir
    editor._invalidate_and_validate()
    try:
        yield store_dir
    finally:
        editor._STORE_DIR = saved_editor
        store_mod._STORE_DIR = saved_store
        editor._invalidate_and_validate()


def _sample_steps():
    return [
        {
            "id": "scan",
            "name": "Scan",
            "step_type": "code",
            "depends_on": [],
            "auto_run": {
                "callable": "work_buddy.fake.module.scan",
                "kwargs": {},
                "timeout": 10,
            },
        },
        {
            "id": "decide",
            "name": "Decide",
            "step_type": "reasoning",
            "depends_on": ["scan"],
        },
    ]


def _sample_instructions():
    return {
        "scan": "Auto-run step.",
        "decide": "Reasoning step. Emit {picked: str}.",
    }


def test_workflow_create_accepts_python_structures(tmp_store):
    """Pass ``steps`` as a list and ``step_instructions`` as a dict directly."""
    result = editor.workflow_create(
        path="dev/example-wf",
        name="Example Workflow",
        description="A test workflow",
        workflow_name="example-wf",
        steps=_sample_steps(),
        step_instructions=_sample_instructions(),
        execution="main",
        parents="dev",
    )
    assert result.get("status") == "created", result

    unit = editor.load_store()["dev/example-wf"]
    # WorkflowUnit carries the DAG as a first-class field
    assert unit.workflow_name == "example-wf"
    assert len(unit.steps) == 2
    assert unit.steps[0]["id"] == "scan"
    assert unit.steps[1]["depends_on"] == ["scan"]
    assert unit.step_instructions["scan"].startswith("Auto-run")


def test_workflow_create_accepts_json_strings(tmp_store):
    """The MCP gateway passes ``steps`` and ``step_instructions`` as JSON
    strings. The wrapper must parse and forward them unchanged."""
    import json

    result = editor.workflow_create(
        path="dev/json-wf",
        name="JSON Workflow",
        description="Round-trip via JSON strings",
        workflow_name="json-wf",
        steps=json.dumps(_sample_steps()),
        step_instructions=json.dumps(_sample_instructions()),
        parents="dev",
    )
    assert result.get("status") == "created", result

    unit = editor.load_store()["dev/json-wf"]
    assert len(unit.steps) == 2
    assert unit.steps[0]["auto_run"]["callable"] == "work_buddy.fake.module.scan"


def test_workflow_create_rejects_malformed_json(tmp_store):
    result = editor.workflow_create(
        path="dev/bad-wf",
        name="Bad",
        description="should fail",
        workflow_name="bad-wf",
        steps="not-json-at-all {",
        parents="dev",
    )
    assert "error" in result
    assert "steps" in result["error"].lower()


def test_workflow_create_rejects_empty_steps(tmp_store):
    result = editor.workflow_create(
        path="dev/empty-wf",
        name="Empty",
        description="should fail",
        workflow_name="empty-wf",
        steps=[],
        parents="dev",
    )
    assert "error" in result
    assert "steps" in result["error"].lower()


def test_workflow_update_replaces_steps(tmp_store):
    editor.workflow_create(
        path="dev/updatable-wf",
        name="U",
        description="d",
        workflow_name="updatable-wf",
        steps=_sample_steps(),
        parents="dev",
    )

    new_steps = [
        {"id": "alpha", "name": "A", "step_type": "reasoning", "depends_on": []},
    ]
    result = editor.workflow_update(
        path="dev/updatable-wf",
        steps=new_steps,
    )
    assert result.get("status") == "updated", result

    unit = editor.load_store()["dev/updatable-wf"]
    assert len(unit.steps) == 1
    assert unit.steps[0]["id"] == "alpha"


def test_workflow_update_merges_step_instructions(tmp_store):
    editor.workflow_create(
        path="dev/inst-wf",
        name="I",
        description="d",
        workflow_name="inst-wf",
        steps=_sample_steps(),
        step_instructions={"scan": "Original scan text.", "decide": "Original decide."},
        parents="dev",
    )

    editor.workflow_update(
        path="dev/inst-wf",
        step_instructions={"scan": "Updated scan text."},
    )

    unit = editor.load_store()["dev/inst-wf"]
    assert unit.step_instructions["scan"] == "Updated scan text."
    # Un-passed key preserved (shallow-merge behaviour is documented)
    assert unit.step_instructions["decide"] == "Original decide."


def test_workflow_create_writes_one_file_per_unit(tmp_store):
    """A new workflow unit is written to its own file at ``<path>.md``."""
    result = editor.workflow_create(
        path="dev/routed-wf",
        name="R",
        description="d",
        workflow_name="routed-wf",
        steps=_sample_steps(),
        parents="dev",
    )
    assert result["file"] == "dev/routed-wf.md"
    # A sibling directions unit lands at its own path-derived file.
    dir_result = editor.docs_create(
        path="dev/routed-dir",
        kind="directions",
        name="D",
        description="d",
        parents="dev",
    )
    assert dir_result["file"] == "dev/routed-dir.md"
