"""Pin the validate step inside the dev-document workflow.

Background: broken knowledge-store refs (orphan parents, dangling
command→unit mappings, missing required fields) are a recurring failure
mode when agents edit the store. The dev-document workflow runs
`docs_validate` as an auto_run step right after `apply` so breakage is
caught in the same edit pass, not the next unrelated commit.

These tests pin the wiring so a future workflow_update can't silently
drop the gate.
"""

from __future__ import annotations

from work_buddy.knowledge.store import load_store
from work_buddy.knowledge.validate import docs_validate


def test_dev_document_workflow_has_validate_step_after_apply():
    """The workflow DAG must be scan → propose → confirm → apply → validate → report."""
    store = load_store(force=True)
    unit = store["dev/dev-document"]
    step_ids = [s["id"] for s in unit.steps]
    assert step_ids == ["scan", "propose", "confirm", "apply", "validate", "report"], step_ids

    by_id = {s["id"]: s for s in unit.steps}

    validate = by_id["validate"]
    assert validate["step_type"] == "code"
    assert validate["depends_on"] == ["apply"]

    # Auto-run wiring points at the real callable, no kwargs needed.
    auto = validate.get("auto_run")
    assert auto, "validate step must be auto_run"
    assert auto["callable"] == "work_buddy.knowledge.validate.docs_validate"
    assert auto.get("kwargs") in ({}, None)

    # Report now depends on validate, not apply directly.
    assert by_id["report"]["depends_on"] == ["validate"]


def test_dev_document_validate_step_instruction_present():
    store = load_store(force=True)
    unit = store["dev/dev-document"]
    inst = unit.step_instructions.get("validate", "")
    # The instruction must at minimum tell the agent what the output fields
    # mean — this is the contract the report step reads against.
    assert "docs_validate" in inst
    assert "passed" in inst
    assert "errors" in inst


def test_docs_validate_auto_run_path_resolves_and_returns_expected_shape():
    """Smoke test: the exact dotted path the workflow names must import
    and call cleanly with no args, returning the shape the instruction
    promises."""
    result = docs_validate()
    for key in ("passed", "failed", "summary", "errors", "total_units", "checks_run"):
        assert key in result, f"docs_validate missing '{key}' in result: {result.keys()}"
    assert isinstance(result["passed"], bool)
    assert isinstance(result["failed"], int)
    assert isinstance(result["errors"], list)
