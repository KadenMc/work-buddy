"""docs_create / docs_update must support dev_notes and entry_points.

Background: the knowledge-store data model has long supported
``dev_notes`` (surfaced only in dev mode) and ``entry_points`` (dotted
module paths for navigation on ``system`` units), but neither
capability exposed them as parameters. Agents trying to create a new
system unit with dev notes had to fall back to direct JSON editing,
which bypasses ``create_unit``'s validation and parent/child bookkeeping.

This test pins the fix so a future refactor doesn't silently drop
these parameters again.
"""

from __future__ import annotations

import pathlib

import pytest

from work_buddy.knowledge import editor


@pytest.fixture
def tmp_store(tmp_path):
    """Point the editor at a throwaway knowledge store directory.

    Uses manual save/restore (not monkeypatch) so we can invalidate the
    global store cache AFTER restoring the real ``_STORE_DIR``. pytest
    monkeypatch teardown runs too late for that — it unwinds after
    our finalizer, leaving the cache pointing at our deleted tmp dir
    and breaking later tests (e.g. test_morning_routine's workflow
    discovery) with ``StopIteration`` on ``morning-routine`` lookup.
    """
    from work_buddy.knowledge import store as store_mod

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    # Seed an empty architecture.json so _best_file_for_new_path finds
    # a real destination for 'architecture/*' paths.
    arch = store_dir / "architecture.json"
    arch.write_text(
        '{"architecture": {"kind": "system", "name": "Architecture",'
        ' "description": "root", "children": []}}',
        encoding="utf-8",
    )

    saved_editor = editor._STORE_DIR
    saved_store = store_mod._STORE_DIR
    editor._STORE_DIR = store_dir
    store_mod._STORE_DIR = store_dir
    editor._invalidate_and_validate()
    try:
        yield store_dir
    finally:
        # Restore BEFORE re-invalidating, so the next cache rebuild
        # reads from the real knowledge store.
        editor._STORE_DIR = saved_editor
        store_mod._STORE_DIR = saved_store
        editor._invalidate_and_validate()


def test_create_unit_accepts_dev_notes_and_entry_points(tmp_store):
    result = editor.create_unit(
        path="architecture/example-sys",
        kind="system",
        name="Example System",
        description="A demo for this test",
        content_full="Body text",
        parents=["architecture"],
        dev_notes="Gotcha: don't regress the XYZ invariant.",
        entry_points=["work_buddy.example", "work_buddy.example.helpers"],
    )
    assert result.get("status") == "created"

    # Read it back through the editor's own load_store
    store = editor.load_store()
    unit = store["architecture/example-sys"]
    assert unit.dev_notes == "Gotcha: don't regress the XYZ invariant."
    assert unit.entry_points == [
        "work_buddy.example", "work_buddy.example.helpers",
    ]


def test_docs_create_mcp_wrapper_passes_dev_notes_through(tmp_store):
    """The MCP-facing wrapper (str args) must plumb dev_notes all the
    way to the stored unit, not silently drop it."""
    result = editor.docs_create(
        path="architecture/mcp-test",
        kind="system",
        name="MCP Test",
        description="Round-trip test through the MCP wrapper",
        content_full="body",
        parents="architecture",
        dev_notes="Landmine: W comes before X here.",
        entry_points="work_buddy.a, work_buddy.b",
    )
    assert result.get("status") == "created"

    unit = editor.load_store()["architecture/mcp-test"]
    assert unit.dev_notes == "Landmine: W comes before X here."
    assert unit.entry_points == ["work_buddy.a", "work_buddy.b"]


def test_docs_update_replaces_dev_notes(tmp_store):
    editor.docs_create(
        path="architecture/updatable",
        kind="system",
        name="U",
        description="d",
        dev_notes="OLD warning",
    )
    editor.docs_update(
        path="architecture/updatable",
        dev_notes="NEW warning with more nuance",
    )
    unit = editor.load_store()["architecture/updatable"]
    assert unit.dev_notes == "NEW warning with more nuance"


def test_docs_update_replaces_entry_points(tmp_store):
    editor.docs_create(
        path="architecture/entry-upd",
        kind="system",
        name="E",
        description="d",
        entry_points="work_buddy.old",
    )
    editor.docs_update(
        path="architecture/entry-upd",
        entry_points="work_buddy.new.a, work_buddy.new.b",
    )
    unit = editor.load_store()["architecture/entry-upd"]
    assert unit.entry_points == ["work_buddy.new.a", "work_buddy.new.b"]
