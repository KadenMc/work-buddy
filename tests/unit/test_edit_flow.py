"""Unit tests for ``work_buddy.knowledge.edit_flow`` — the auto_run callables
behind the ``docs_edit`` workflow (resolve + commit).

Isolation mirrors ``test_knowledge_editor``: patch ``_STORE_DIR`` on every
module that captured it (the loader, the editor, and edit_flow itself) at a
temp directory, and invalidate the store cache before and after.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from work_buddy.knowledge import edit_flow as edit_flow_mod
from work_buddy.knowledge import editor as editor_mod
from work_buddy.knowledge import file_store
from work_buddy.knowledge import store as store_mod
from work_buddy.knowledge.edit_flow import commit_edit, resolve_for_edit


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setattr(edit_flow_mod, "_STORE_DIR", store_dir)
    monkeypatch.setattr(editor_mod, "_STORE_DIR", store_dir)
    monkeypatch.setattr(store_mod, "_STORE_DIR", store_dir)
    store_mod.invalidate_store()
    yield store_dir
    store_mod.invalidate_store()


def _seed(store_dir: Path, path: str, unit: dict[str, Any]) -> None:
    file_store.write_unit(store_dir, path, unit)
    store_mod.invalidate_store()


def _directions(name: str = "Foo", full: str = "Body.") -> dict[str, Any]:
    return {
        "kind": "directions", "name": name, "description": "d",
        "trigger": "when", "content": {"full": full},
    }


# ---------------------------------------------------------------------------
# resolve_for_edit
# ---------------------------------------------------------------------------

class TestResolveForEdit:

    def test_existing_unit_returns_path(self, tmp_store):
        _seed(tmp_store, "x/foo", _directions())
        r = resolve_for_edit(params={"path": "x/foo"})
        assert r["ok"] is True
        assert r["created"] is False
        assert r["path"] == "x/foo"
        assert r["file"].endswith("foo.md")
        assert r["kind"] == "directions"

    def test_missing_path_errors(self, tmp_store):
        r = resolve_for_edit(params={})
        assert r["ok"] is False
        assert "path" in r["error"].lower()

    def test_edit_nonexistent_without_create_errors(self, tmp_store):
        r = resolve_for_edit(params={"path": "x/ghost"})
        assert r["ok"] is False
        assert "not found" in r["error"].lower()

    def test_create_scaffolds_file(self, tmp_store):
        r = resolve_for_edit(params={"path": "x/new", "create": True, "kind": "directions"})
        assert r["ok"] is True
        assert r["created"] is True
        # the scaffold is on disk and loads as a directions unit
        data = file_store.read_unit(tmp_store, "x/new")
        assert data is not None
        assert data["kind"] == "directions"

    def test_create_without_kind_errors(self, tmp_store):
        r = resolve_for_edit(params={"path": "x/new", "create": True})
        assert r["ok"] is False
        assert "kind" in r["error"].lower()

    def test_create_existing_path_errors(self, tmp_store):
        _seed(tmp_store, "x/foo", _directions())
        r = resolve_for_edit(params={"path": "x/foo", "create": True, "kind": "directions"})
        assert r["ok"] is False
        assert "already exists" in r["error"].lower()


# ---------------------------------------------------------------------------
# commit_edit
# ---------------------------------------------------------------------------

class TestCommitEdit:

    def test_clean_edit_commits_and_requests_reconcile(self, tmp_store):
        _seed(tmp_store, "x/foo", _directions(full="Original."))
        # simulate the agent's native Edit
        file_store.write_unit(tmp_store, "x/foo", _directions(full="Edited body."))
        r = commit_edit(resolve={"path": "x/foo"})
        assert r["ok"] is True
        assert r["status"] == "ok"
        assert r["unit_errors"] == []
        assert r["__reconcile_store__"] is True

    def test_missing_file_reports_and_still_reconciles(self, tmp_store):
        r = commit_edit(resolve={"path": "x/ghost"})
        assert r["ok"] is False
        assert "__reconcile_store__" in r

    def test_broken_frontmatter_detected(self, tmp_store):
        _seed(tmp_store, "x/foo", _directions())
        # corrupt the file: unterminated frontmatter
        (tmp_store / "x" / "foo.md").write_text("---\nname: Foo\nno close", encoding="utf-8")
        store_mod.invalidate_store()
        r = commit_edit(resolve={"path": "x/foo"})
        assert r["ok"] is False
        assert "failed to load" in r["error"].lower()
        assert r["__reconcile_store__"] is True

    def test_duplicate_placeholder_blocks_commit(self, tmp_store):
        _seed(tmp_store, "x/foo", _directions())
        # an edit that introduces a duplicate placeholder (hard error)
        file_store.write_unit(
            tmp_store, "x/foo",
            _directions(full="<<wb:y>> and again <<wb:y>>"),
        )
        r = commit_edit(resolve={"path": "x/foo"})
        assert r["ok"] is False
        assert any(e["check"] == "placeholder_duplicate" for e in r["unit_errors"])

    def test_workflow_cycle_blocks_commit(self, tmp_store):
        wf = {
            "kind": "workflow", "name": "W", "description": "d",
            "workflow_name": "w",
            "steps": [
                {"id": "a", "step_type": "code", "depends_on": ["b"]},
                {"id": "b", "step_type": "code", "depends_on": ["a"]},
            ],
        }
        _seed(tmp_store, "x/wf", wf)
        r = commit_edit(resolve={"path": "x/wf"})
        assert r["ok"] is False
        assert any(e["check"] == "workflow_step_dag" for e in r["unit_errors"])
