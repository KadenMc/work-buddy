"""create_unit / update_unit must preserve dev_notes and entry_points.

``dev_notes`` (surfaced only in dev mode) and ``entry_points`` (dotted module
paths for navigation on ``system`` / ``reference`` units) round-trip through the
file-per-unit codec. These tests pin that the internal write primitives carry
both fields through create and update so a future refactor can't silently drop
them. (Agents author content through the ``docs_edit`` workflow's native-Edit
flow; ``create_unit`` / ``update_unit`` are the validated write primitives.)
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

    from work_buddy.knowledge import file_store

    store_dir = tmp_path / "store"
    store_dir.mkdir()
    # Seed an `architecture` parent so 'architecture/*' units have a
    # real parent in the store.
    file_store.write_unit(store_dir, "architecture", {
        "kind": "system", "name": "Architecture", "description": "root",
    })

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
        kind="reference",
        name="Example Reference",
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


def test_update_unit_replaces_dev_notes(tmp_store):
    editor.create_unit(
        path="architecture/updatable",
        kind="system",
        name="U",
        description="d",
        dev_notes="OLD warning",
    )
    editor.update_unit(
        "architecture/updatable",
        {"dev_notes": "NEW warning with more nuance"},
    )
    unit = editor.load_store()["architecture/updatable"]
    assert unit.dev_notes == "NEW warning with more nuance"


def test_update_unit_replaces_entry_points(tmp_store):
    editor.create_unit(
        path="architecture/entry-upd",
        kind="reference",
        name="E",
        description="d",
        entry_points=["work_buddy.old"],
    )
    editor.update_unit(
        "architecture/entry-upd",
        {"entry_points": ["work_buddy.new.a", "work_buddy.new.b"]},
    )
    unit = editor.load_store()["architecture/entry-upd"]
    assert unit.entry_points == ["work_buddy.new.a", "work_buddy.new.b"]
