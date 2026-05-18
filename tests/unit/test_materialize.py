"""Unit tests for ``work_buddy.knowledge.materialize`` — the docs_checkout /
docs_commit materialization workflow.

Two layers:
  - Pure serializer/parser tests — round-trip fidelity, strict parsing,
    template scaffolding. No I/O.
  - Integration tests — checkout/commit against a temp store + temp artifact
    root, with the background re-index stubbed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from work_buddy.knowledge import editor as editor_mod
from work_buddy.knowledge import materialize as mat
from work_buddy.knowledge import store as store_mod
from work_buddy.knowledge.materialize import (
    build_template,
    docs_checkout,
    docs_commit,
    markdown_to_unit_dict,
    split_buffer,
    unit_dict_to_markdown,
)


# ===========================================================================
# Pure serializer / parser
# ===========================================================================

class TestRoundTrip:
    """unit_dict_to_markdown → markdown_to_unit_dict must be lossless."""

    def _roundtrip(self, path: str, unit_dict: dict[str, Any]) -> dict[str, Any]:
        return markdown_to_unit_dict(unit_dict_to_markdown(path, unit_dict))

    def test_directions_unit(self):
        unit = {
            "kind": "directions",
            "name": "Test Directions",
            "description": "A test directions unit.",
            "trigger": "when the user runs /test",
            "command": "wb-test",
            "tags": ["test", "directions"],
            "aliases": ["test alias"],
            "parents": ["dev"],
            "children": [],
            "content": {"full": "Do the thing.\n\nThen do the other thing."},
        }
        assert self._roundtrip("dev/test-directions", unit) == unit

    def test_system_unit(self):
        unit = {
            "kind": "system",
            "name": "Test System",
            "description": "A system unit.",
            "entry_points": ["work_buddy.test.module"],
            "tags": ["test"],
            "parents": ["architecture"],
            "content": {"full": "## How it works\n\nIt works well."},
        }
        assert self._roundtrip("architecture/test-system", unit) == unit

    def test_capability_unit_with_nested_parameters(self):
        """The headline case: a capability declaration's nested ``parameters``
        map round-trips natively through YAML — no JSON-string escaping."""
        unit = {
            "kind": "capability",
            "name": "Task Read",
            "description": "Read a task's full context.",
            "capability_name": "task_read",
            "category": "tasks",
            "op": "op.wb.task_read",
            "schema_version": "wb-capability/v1",
            "parameters": {
                "task_id": {
                    "type": "str",
                    "description": "Task ID (e.g., 't-a3f8c1e2')",
                    "required": True,
                },
                "deep": {
                    "type": "bool",
                    "description": "Include deep signals",
                    "required": False,
                },
            },
            "requires": ["obsidian"],
            "aliases": ["read task", "view task"],
            "tags": ["tasks", "read"],
            "parents": ["tasks"],
        }
        assert self._roundtrip("tasks/task_read", unit) == unit

    def test_concept_unit_minimal(self):
        unit = {
            "kind": "concept",
            "name": "A Concept",
            "description": "Concept description.",
            "content": {"full": "Narrative prose."},
        }
        assert self._roundtrip("architecture/a-concept", unit) == unit

    def test_multiline_dev_notes_roundtrip(self):
        unit = {
            "kind": "system",
            "name": "Has Dev Notes",
            "description": "x",
            "dev_notes": "Line one.\nLine two.\nLine three with: a colon.",
            "content": {"full": "body"},
        }
        assert self._roundtrip("architecture/dev-notes", unit) == unit

    def test_body_containing_horizontal_rule(self):
        """A markdown ``---`` inside the body must not be mistaken for the
        frontmatter delimiter."""
        unit = {
            "kind": "concept",
            "name": "HR Body",
            "description": "x",
            "content": {"full": "Section one.\n\n---\n\nSection two."},
        }
        assert self._roundtrip("architecture/hr-body", unit) == unit

    def test_summary_folds_into_content(self):
        unit = {
            "kind": "concept",
            "name": "With Summary",
            "description": "x",
            "content": {"full": "Full body.", "summary": "Short form."},
        }
        rt = self._roundtrip("architecture/with-summary", unit)
        assert rt["content"]["summary"] == "Short form."
        assert rt["content"]["full"] == "Full body."

    def test_unit_with_no_content(self):
        """A capability declaration typically has no content body."""
        unit = {
            "kind": "capability",
            "name": "No Body",
            "description": "x",
            "capability_name": "no_body",
            "category": "misc",
            "op": "op.wb.no_body",
            "schema_version": "wb-capability/v1",
            "parameters": {},
        }
        rt = self._roundtrip("misc/no_body", unit)
        assert "content" not in rt
        assert rt == unit

    def test_description_with_yaml_special_chars(self):
        """Colons, quotes, brackets in a description — YAML escaping handles
        what hand-JSON-escaping got wrong."""
        unit = {
            "kind": "concept",
            "name": "Tricky",
            "description": 'Has: a colon, "quotes", and [brackets].',
            "content": {"full": "body"},
        }
        assert self._roundtrip("architecture/tricky", unit) == unit

    def test_path_is_display_only(self):
        """The frontmatter ``path`` key is informational and dropped on parse."""
        unit = {"kind": "concept", "name": "P", "description": "d",
                "content": {"full": "b"}}
        md = unit_dict_to_markdown("architecture/p", unit)
        assert "path: architecture/p" in md
        assert "path" not in markdown_to_unit_dict(md)


class TestSplitBuffer:
    """split_buffer is strict — malformed input raises ValueError so
    docs_commit can reject and preserve the buffer."""

    def test_valid_buffer(self):
        fm, body = split_buffer("---\nname: X\n---\n\nbody text")
        assert fm == {"name": "X"}
        assert body == "body text"

    def test_no_frontmatter_raises(self):
        with pytest.raises(ValueError, match="no YAML frontmatter"):
            split_buffer("just body, no frontmatter")

    def test_unclosed_frontmatter_raises(self):
        with pytest.raises(ValueError, match="not closed"):
            split_buffer("---\nname: X\nstill in frontmatter")

    def test_malformed_yaml_raises(self):
        with pytest.raises(ValueError, match="invalid YAML"):
            split_buffer("---\nname: : : bad\n  - broken\n---\n\nbody")

    def test_non_mapping_frontmatter_raises(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            split_buffer("---\n- just\n- a\n- list\n---\n\nbody")


class TestBuildTemplate:
    @pytest.mark.parametrize("kind", [
        "directions", "system", "service", "integration",
        "reference", "concept", "capability",
    ])
    def test_template_parses_and_has_kind(self, kind):
        buffer = build_template(f"test/new-{kind}", kind)
        unit = markdown_to_unit_dict(buffer)
        assert unit["kind"] == kind
        assert "name" in unit and "description" in unit

    def test_capability_template_has_op_fields(self):
        unit = markdown_to_unit_dict(build_template("tasks/new_cap", "capability"))
        assert unit["op"].startswith("op.")
        assert unit["schema_version"] == "wb-capability/v1"
        assert "parameters" in unit
        assert unit["capability_name"]

    def test_directions_template_has_trigger(self):
        unit = markdown_to_unit_dict(build_template("dev/new-dir", "directions"))
        assert "trigger" in unit


# ===========================================================================
# Integration — checkout / commit against a temp store + temp artifacts
# ===========================================================================

@pytest.fixture
def materialized_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp store dir + temp artifact root + stubbed background re-index."""
    from work_buddy.artifacts import FilesystemStorage

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setattr(editor_mod, "_STORE_DIR", store_dir)
    monkeypatch.setattr(store_mod, "_STORE_DIR", store_dir)

    # Redirect artifact writes (the editing buffers) to a temp root.
    art_root = tmp_path / "artifacts"
    art_root.mkdir()
    import work_buddy.artifacts as artifacts_pkg
    monkeypatch.setattr(
        artifacts_pkg, "_default_store", FilesystemStorage(data_root=art_root),
    )

    # The background re-index rebuilds the global index singleton from the
    # store — pointless and side-effecting in a temp-store test.
    monkeypatch.setattr(mat, "_reindex_async", lambda: None)

    store_mod.invalidate_store()
    yield store_dir
    store_mod.invalidate_store()


def _seed(store_dir: Path, stem: str, units: dict[str, dict[str, Any]]) -> None:
    (store_dir / f"{stem}.json").write_text(
        json.dumps(units, indent=2), encoding="utf-8",
    )
    store_mod.invalidate_store()


_DIRECTIONS_UNIT = {
    "kind": "directions",
    "name": "Sample Directions",
    "description": "A sample directions unit.",
    "trigger": "when testing",
    "content": {"full": "Original body content."},
    "parents": [],
    "children": [],
}


class TestCheckout:
    def test_checkout_existing_unit(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        result = docs_checkout(path="dev/sample")
        assert result["status"] == "checked_out"
        assert result["mode"] == "edit"
        assert result["kind"] == "directions"
        buf = Path(result["buffer_path"])
        assert buf.exists()
        # The buffer round-trips to the stored unit.
        parsed = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        assert parsed["name"] == "Sample Directions"

    def test_checkout_missing_path(self, materialized_env):
        _seed(materialized_env, "dev", {})
        result = docs_checkout(path="dev/nope")
        assert "error" in result
        assert "not found" in result["error"]

    def test_checkout_generated_unit_refused(self, materialized_env):
        """A unit that lives only in a _generated_*.json file has no
        hand-authored source — checkout must refuse it."""
        (materialized_env / "_generated_capabilities.json").write_text(
            json.dumps({"tasks/gen_cap": {
                "kind": "capability", "name": "Gen", "description": "d",
                "capability_name": "gen_cap", "category": "tasks",
            }}, indent=2),
            encoding="utf-8",
        )
        store_mod.invalidate_store()
        result = docs_checkout(path="tasks/gen_cap")
        assert "error" in result
        assert "generated" in result["error"]

    def test_checkout_workflow_refused(self, materialized_env):
        _seed(materialized_env, "workflows", {"dev/wf": {
            "kind": "workflow", "name": "WF", "description": "d",
            "workflow_name": "wf", "steps": [],
        }})
        result = docs_checkout(path="dev/wf")
        assert "error" in result
        assert "workflow" in result["error"].lower()

    def test_checkout_template(self, materialized_env):
        _seed(materialized_env, "dev", {})
        result = docs_checkout(path="dev/brand-new", kind="concept", template=True)
        assert result["status"] == "checked_out"
        assert result["mode"] == "create"
        buf = Path(result["buffer_path"])
        assert buf.exists()

    def test_checkout_template_existing_path_refused(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        result = docs_checkout(path="dev/sample", kind="concept", template=True)
        assert "error" in result
        assert "already exists" in result["error"]

    def test_checkout_template_requires_kind(self, materialized_env):
        _seed(materialized_env, "dev", {})
        result = docs_checkout(path="dev/x", template=True)
        assert "error" in result


class TestCommit:
    def test_commit_unchanged_roundtrip(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        co = docs_checkout(path="dev/sample")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["status"] == "committed"
        store_mod.invalidate_store()
        unit = store_mod.load_store()["dev/sample"]
        assert unit.content["full"] == "Original body content."

    def test_commit_edited_body(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        co = docs_checkout(path="dev/sample")
        buf = Path(co["buffer_path"])
        text = buf.read_text(encoding="utf-8")
        buf.write_text(text.replace("Original body content.", "Edited body!"),
                       encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["status"] == "committed"
        store_mod.invalidate_store()
        assert store_mod.load_store()["dev/sample"].content["full"] == "Edited body!"
        # Buffer consumed on success.
        assert not buf.exists()

    def test_commit_missing_checkout(self, materialized_env):
        _seed(materialized_env, "dev", {})
        result = docs_commit(checkout_id="20990101-000000_nonexistent")
        assert result["error"] == "checkout_not_found"

    def test_commit_template_creates_unit(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/anchor": {
            "kind": "concept", "name": "Anchor", "description": "d",
            "content": {"full": "anchor"}, "parents": [], "children": [],
        }})
        co = docs_checkout(path="dev/created", kind="concept", template=True)
        buf = Path(co["buffer_path"])
        # Fill the template placeholders with real values.
        unit = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        unit["name"] = "Created Unit"
        unit["description"] = "A freshly created unit."
        unit["content"] = {"full": "Real content."}
        buf.write_text(unit_dict_to_markdown("dev/created", unit), encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["status"] == "committed"
        store_mod.invalidate_store()
        store = store_mod.load_store()
        assert "dev/created" in store
        assert store["dev/created"].name == "Created Unit"

    def test_commit_reconciles_changed_parents(self, materialized_env):
        """Editing a unit's `parents` in the buffer reconciles the child links
        both ways — the old parent loses the child, the new parent gains it."""
        _seed(materialized_env, "dev", {
            "dev/p1": {"kind": "concept", "name": "P1", "description": "d",
                       "content": {"full": "p1"}, "parents": [],
                       "children": ["dev/child"]},
            "dev/p2": {"kind": "concept", "name": "P2", "description": "d",
                       "content": {"full": "p2"}, "parents": [], "children": []},
            "dev/child": {"kind": "concept", "name": "Child", "description": "d",
                          "content": {"full": "c"}, "parents": ["dev/p1"],
                          "children": []},
        })
        co = docs_checkout(path="dev/child")
        buf = Path(co["buffer_path"])
        unit = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        unit["parents"] = ["dev/p2"]
        buf.write_text(unit_dict_to_markdown("dev/child", unit), encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["status"] == "committed"
        store_mod.invalidate_store()
        store = store_mod.load_store()
        assert "dev/child" not in store["dev/p1"].children
        assert "dev/child" in store["dev/p2"].children

    def test_commit_missing_required_field_rejected(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        co = docs_checkout(path="dev/sample")
        buf = Path(co["buffer_path"])
        # Blank the name.
        unit = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        unit["name"] = ""
        buf.write_text(unit_dict_to_markdown("dev/sample", unit), encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["error"] == "missing_required_fields"
        # Buffer preserved for fix-and-retry.
        assert buf.exists()

    def test_commit_unknown_kind_rejected(self, materialized_env):
        """A typo'd / unsupported kind in the buffer frontmatter is rejected;
        the buffer survives."""
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        co = docs_checkout(path="dev/sample")
        buf = Path(co["buffer_path"])
        unit = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        unit["kind"] = "directons"  # typo
        buf.write_text(unit_dict_to_markdown("dev/sample", unit), encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["error"] == "unsupported_kind"
        assert buf.exists()

    def test_commit_malformed_buffer_rejected_buffer_survives(self, materialized_env):
        _seed(materialized_env, "dev", {"dev/sample": dict(_DIRECTIONS_UNIT)})
        co = docs_checkout(path="dev/sample")
        buf = Path(co["buffer_path"])
        buf.write_text("this buffer has no frontmatter at all", encoding="utf-8")
        result = docs_commit(checkout_id=co["checkout_id"])
        assert result["error"] == "buffer_parse_failed"
        assert buf.exists()


class TestDuplicatePlaceholderFailureTolerance:
    """The critical contract: a commit that fails the duplicate-placeholder
    check preserves the buffer, so the agent fixes it in place and retries —
    no re-sending the whole payload."""

    def test_reject_then_fix_then_succeed(self, materialized_env):
        _seed(materialized_env, "dev", {
            "dev/leaf": {
                "kind": "directions", "name": "Leaf", "description": "leaf",
                "trigger": "x", "content": {"full": "leaf body"},
                "parents": [], "children": [],
            },
            "dev/host": {
                "kind": "directions", "name": "Host", "description": "host",
                "trigger": "x", "content": {"full": "original"},
                "parents": [], "children": [],
            },
        })
        co = docs_checkout(path="dev/host")
        buf = Path(co["buffer_path"])

        # Write content with a duplicated placeholder.
        unit = markdown_to_unit_dict(buf.read_text(encoding="utf-8"))
        unit["content"] = {"full": "<<wb:dev/leaf>> mid <<wb:dev/leaf>>"}
        buf.write_text(unit_dict_to_markdown("dev/host", unit), encoding="utf-8")

        # First commit is rejected; buffer survives.
        rejected = docs_commit(checkout_id=co["checkout_id"])
        assert rejected["error"] == "placeholder_duplicate"
        assert buf.exists()
        store_mod.invalidate_store()
        # Store is untouched — original content preserved.
        assert store_mod.load_store()["dev/host"].content["full"] == "original"

        # Fix the buffer in place and re-commit.
        unit["content"] = {"full": "<<wb:dev/leaf>> only once now"}
        buf.write_text(unit_dict_to_markdown("dev/host", unit), encoding="utf-8")
        ok = docs_commit(checkout_id=co["checkout_id"])
        assert ok["status"] == "committed"
        assert not buf.exists()
        store_mod.invalidate_store()
        assert "only once" in store_mod.load_store()["dev/host"].content["full"]
