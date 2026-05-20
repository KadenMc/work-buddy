"""Tests for the file-per-unit knowledge-store codec and read/write seam."""

from __future__ import annotations

import pytest

from work_buddy.knowledge import file_store as fs
from work_buddy.knowledge.model import unit_from_dict


# ---------------------------------------------------------------------------
# Prose-kind codec round-trip
# ---------------------------------------------------------------------------

def _roundtrip(unit_dict: dict) -> dict:
    """Serialize a raw unit dict and parse it back."""
    return fs.markdown_to_unit_dict(fs.unit_dict_to_markdown(unit_dict))


@pytest.mark.parametrize("kind", [
    "directions", "system", "service", "integration",
    "reference", "concept", "capability",
])
def test_prose_kind_roundtrip(kind):
    unit_dict = {
        "kind": kind,
        "name": "Example Unit",
        "description": "A one-line description.",
        "tags": ["alpha", "beta"],
        "aliases": ["the example"],
        "parents": ["domain"],
        "content": {
            "summary": "Short summary.",
            "full": "## Heading\n\nFull body with **markdown**.\n\n- a\n- b",
        },
    }
    assert _roundtrip(unit_dict) == unit_dict


def test_capability_declaration_fields_roundtrip():
    unit_dict = {
        "kind": "capability",
        "name": "Task Toggle",
        "description": "Mark a task done.",
        "capability_name": "task_toggle",
        "category": "tasks",
        "parameters": {"task_id": {"type": "str", "required": True}},
        "mutates_state": True,
        "retry_policy": "verify_first",
        "consent_required": True,
        "consent_operations": ["task.write"],
        "op": "op.wb.task_toggle",
        "schema_version": "wb-capability/v1",
        "parents": ["tasks"],
    }
    assert _roundtrip(unit_dict) == unit_dict


def test_summary_rides_in_frontmatter():
    md = fs.unit_dict_to_markdown({
        "kind": "concept", "name": "N", "description": "D",
        "content": {"summary": "S", "full": "B"},
    })
    assert "summary: S" in md.split("---")[1]


def test_children_path_scope_never_serialized():
    md = fs.unit_dict_to_markdown({
        "kind": "concept", "name": "N", "description": "D",
        "parents": ["p"], "children": ["c1", "c2"],
        "path": "x/y", "scope": "system",
    })
    assert "children" not in md
    assert "\npath:" not in md
    assert "scope" not in md


def test_empty_body_unit():
    unit_dict = {
        "kind": "capability", "name": "N", "description": "D",
        "capability_name": "n", "category": "c",
    }
    md = fs.unit_dict_to_markdown(unit_dict)
    assert _roundtrip(unit_dict) == unit_dict


def test_frontmatter_key_order_stable():
    md = fs.unit_dict_to_markdown({
        "kind": "concept", "name": "N", "description": "D",
        "tags": ["t"], "content": {"summary": "S", "full": "B"},
    })
    fm_block = md.split("---")[1]
    assert fm_block.index("name:") < fm_block.index("kind:") \
        < fm_block.index("description:") < fm_block.index("summary:") \
        < fm_block.index("tags:")


def test_multiline_strings_use_block_style():
    md = fs.unit_dict_to_markdown({
        "kind": "concept", "name": "N",
        "description": "line one\nline two",
        "content": {"full": "B"},
    })
    # A multi-line scalar renders as a literal block, not an escaped string.
    assert "description: |" in md


# ---------------------------------------------------------------------------
# Workflow codec round-trip
# ---------------------------------------------------------------------------

def _workflow_dict() -> dict:
    return {
        "kind": "workflow",
        "name": "Example Flow",
        "description": "A two-step flow.",
        "workflow_name": "example-flow",
        "execution": "main",
        "steps": [
            {"id": "scan", "name": "Scan", "step_type": "code", "depends_on": []},
            {"id": "propose", "name": "Propose", "step_type": "reasoning",
             "depends_on": ["scan"]},
        ],
        "step_instructions": {
            "scan": "Scan instruction prose.",
            "propose": "Propose instruction prose.\n\nWith a paragraph.",
        },
        "content": {"full": "Workflow-level narrative context."},
        "parents": ["dev"],
    }


def test_workflow_roundtrip():
    wf = _workflow_dict()
    assert _roundtrip(wf) == wf


def test_workflow_steps_in_frontmatter_instructions_in_body():
    md = fs.unit_dict_to_markdown(_workflow_dict())
    fm_block, body = md.split("---\n\n", 1)
    assert "steps:" in fm_block
    assert "step_instructions" not in fm_block
    assert body.startswith("Workflow-level narrative context.")
    assert "## scan" in body
    assert "## propose" in body


def test_workflow_narrative_before_first_step_heading():
    md = fs.unit_dict_to_markdown(_workflow_dict())
    parsed = fs.markdown_to_unit_dict(md)
    assert parsed["content"]["full"] == "Workflow-level narrative context."


def test_workflow_hash_heading_inside_instruction_is_not_a_split():
    """A ``## `` line whose text is not a known step id stays in its section."""
    wf = _workflow_dict()
    wf["step_instructions"]["scan"] = "Intro.\n\n## Not A Step\n\nStill scan prose."
    parsed = _roundtrip(wf)
    assert parsed["step_instructions"]["scan"] == \
        "Intro.\n\n## Not A Step\n\nStill scan prose."
    assert "propose" in parsed["step_instructions"]


def test_workflow_step_without_instruction():
    wf = _workflow_dict()
    del wf["step_instructions"]["scan"]
    parsed = _roundtrip(wf)
    assert "scan" not in parsed.get("step_instructions", {})
    assert parsed["step_instructions"]["propose"].startswith("Propose")


def test_workflow_no_narrative():
    wf = _workflow_dict()
    del wf["content"]
    parsed = _roundtrip(wf)
    assert "full" not in parsed.get("content", {})
    assert parsed["step_instructions"]["scan"] == "Scan instruction prose."


# ---------------------------------------------------------------------------
# Path ↔ file mapping
# ---------------------------------------------------------------------------

def test_path_to_file_and_back(tmp_path):
    f = fs.path_to_file(tmp_path, "tasks/task_read")
    assert f == tmp_path / "tasks" / "task_read.md"
    assert fs.file_to_path(tmp_path, f) == "tasks/task_read"


def test_domain_parent_and_directory_coexist(tmp_path):
    fs.write_unit(tmp_path, "tasks", {"kind": "system", "name": "Tasks",
                                      "description": "D"})
    fs.write_unit(tmp_path, "tasks/task_read", {"kind": "capability",
                                                "name": "R", "description": "D",
                                                "capability_name": "task_read",
                                                "category": "tasks"})
    assert (tmp_path / "tasks.md").is_file()
    assert (tmp_path / "tasks" / "task_read.md").is_file()
    assert set(fs.list_unit_paths(tmp_path)) == {"tasks", "tasks/task_read"}


# ---------------------------------------------------------------------------
# Read/write/delete/move seam
# ---------------------------------------------------------------------------

def test_write_read_unit(tmp_path):
    unit_dict = {"kind": "concept", "name": "N", "description": "D",
                 "content": {"full": "Body."}}
    fs.write_unit(tmp_path, "a/b", unit_dict)
    assert fs.read_unit(tmp_path, "a/b") == unit_dict


def test_read_missing_unit_returns_none(tmp_path):
    assert fs.read_unit(tmp_path, "nope") is None


def test_delete_unit_prunes_empty_dirs(tmp_path):
    fs.write_unit(tmp_path, "a/b/c", {"kind": "concept", "name": "N",
                                      "description": "D"})
    assert fs.delete_unit(tmp_path, "a/b/c") is True
    assert not (tmp_path / "a").exists()
    assert fs.delete_unit(tmp_path, "a/b/c") is False


def test_move_unit(tmp_path):
    unit_dict = {"kind": "concept", "name": "N", "description": "D"}
    fs.write_unit(tmp_path, "old/here", unit_dict)
    fs.move_unit(tmp_path, "old/here", "new/there")
    assert fs.read_unit(tmp_path, "old/here") is None
    assert fs.read_unit(tmp_path, "new/there") == unit_dict
    assert not (tmp_path / "old").exists()


# ---------------------------------------------------------------------------
# Bulk load + children derivation
# ---------------------------------------------------------------------------

def test_load_units_from_dir_derives_children(tmp_path):
    fs.write_unit(tmp_path, "domain", {"kind": "system", "name": "Domain",
                                       "description": "D"})
    fs.write_unit(tmp_path, "domain/child-a", {"kind": "concept", "name": "A",
                                               "description": "D",
                                               "parents": ["domain"]})
    fs.write_unit(tmp_path, "domain/child-b", {"kind": "concept", "name": "B",
                                               "description": "D",
                                               "parents": ["domain"]})
    units = fs.load_units_from_dir(tmp_path)
    assert units["domain"]["children"] == ["domain/child-a", "domain/child-b"]
    assert "children" not in units["domain/child-a"]


def test_load_units_typed_via_unit_from_dict(tmp_path):
    fs.write_unit(tmp_path, "x/cap", {
        "kind": "capability", "name": "Cap", "description": "D",
        "capability_name": "cap", "category": "x",
        "op": "op.wb.cap", "schema_version": "wb-capability/v1",
    })
    units = fs.load_units_from_dir(tmp_path)
    typed = unit_from_dict("x/cap", units["x/cap"])
    assert typed.kind == "capability"
    assert typed.op == "op.wb.cap"


def test_malformed_frontmatter_raises():
    with pytest.raises(ValueError):
        fs.markdown_to_unit_dict("no frontmatter here")
    with pytest.raises(ValueError):
        fs.markdown_to_unit_dict("---\nunterminated: true\n")
