"""v5 Stage 4.9 — atomic cross-thread context-item migration."""

from __future__ import annotations

import pytest

from work_buddy.threads import migration_context, store
from work_buddy.threads.events import (
    KIND_CONTEXT_ADDED,
    KIND_CONTEXT_REMOVED,
)
from work_buddy.threads.migration_context import (
    ContextMigrationError,
    migrate_context,
)
from work_buddy.threads.models import ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


def _make_thread_with_items(*items):
    t = Thread(context_items=items)
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMigrateContextHappy:
    def test_moves_item_between_threads(self, fresh_db):
        a = ContextItem(id="A1", source="x", type="y", label="apple")
        b = ContextItem(id="A2", source="x", type="y", label="banana")
        src = _make_thread_with_items(a, b)
        dst = _make_thread_with_items()

        mig_id = migrate_context(
            item_id="ci-1",
            from_thread_id=src.thread_id,
            to_thread_id=dst.thread_id,
        )
        assert mig_id.startswith("mig-")

        src_after = store.get_thread(src.thread_id)
        dst_after = store.get_thread(dst.thread_id)
        # Source has only b
        assert [c.label for c in src_after.context_items] == ["banana"]
        # Dest has a
        assert [c.label for c in dst_after.context_items] == ["apple"]

    def test_linked_events_share_migration_id(self, fresh_db):
        a = ContextItem(id="A1", source="x", type="y", label="apple")
        src = _make_thread_with_items(a)
        dst = _make_thread_with_items()
        mig_id = migrate_context(
            item_id="ci-1",
            from_thread_id=src.thread_id,
            to_thread_id=dst.thread_id,
        )
        src_events = store.list_events(src.thread_id)
        dst_events = store.list_events(dst.thread_id)
        # Source has a context_removed
        rem = [e for e in src_events if e.kind == KIND_CONTEXT_REMOVED]
        add = [e for e in dst_events if e.kind == KIND_CONTEXT_ADDED]
        assert len(rem) == 1
        assert len(add) == 1
        assert rem[0].migration_id == mig_id
        assert add[0].migration_id == mig_id

    def test_lookup_by_raw_id_works(self, fresh_db):
        a = ContextItem(id="raw-id-foo", source="x", type="y", label="apple")
        src = _make_thread_with_items(a)
        dst = _make_thread_with_items()
        migrate_context(
            item_id="raw-id-foo",
            from_thread_id=src.thread_id,
            to_thread_id=dst.thread_id,
        )
        assert len(store.get_thread(src.thread_id).context_items) == 0
        assert len(store.get_thread(dst.thread_id).context_items) == 1

    def test_order_index_unchanged(self, fresh_db):
        # Per UX.md §9.4 — migration does NOT trigger re-seriation
        parent = Thread()
        store.insert_thread(parent)
        sub_a = Thread(
            parent_id=parent.thread_id, order_index=5,
            context_items=(ContextItem(id="ci-x", source="s", type="t", label="x"),),
        )
        sub_b = Thread(parent_id=parent.thread_id, order_index=2)
        store.insert_thread(sub_a)
        store.insert_thread(sub_b)
        migrate_context(
            item_id="ci-1",
            from_thread_id=sub_a.thread_id,
            to_thread_id=sub_b.thread_id,
        )
        assert store.get_thread(sub_a.thread_id).order_index == 5
        assert store.get_thread(sub_b.thread_id).order_index == 2


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestMigrateContextErrors:
    def test_same_thread_raises(self, fresh_db):
        src = _make_thread_with_items()
        with pytest.raises(ContextMigrationError):
            migrate_context(
                item_id="ci-1",
                from_thread_id=src.thread_id,
                to_thread_id=src.thread_id,
            )

    def test_unknown_source_raises(self, fresh_db):
        dst = _make_thread_with_items()
        with pytest.raises(ContextMigrationError) as exc_info:
            migrate_context(
                item_id="ci-1",
                from_thread_id="th-missing",
                to_thread_id=dst.thread_id,
            )
        assert "not found" in str(exc_info.value).lower()

    def test_unknown_dest_raises(self, fresh_db):
        a = ContextItem(id="x", source="s", type="t", label="x")
        src = _make_thread_with_items(a)
        with pytest.raises(ContextMigrationError):
            migrate_context(
                item_id="ci-1",
                from_thread_id=src.thread_id,
                to_thread_id="th-missing",
            )

    def test_unknown_item_raises(self, fresh_db):
        a = ContextItem(id="A1", source="x", type="y", label="apple")
        src = _make_thread_with_items(a)
        dst = _make_thread_with_items()
        with pytest.raises(ContextMigrationError):
            migrate_context(
                item_id="ci-99",  # out of range
                from_thread_id=src.thread_id,
                to_thread_id=dst.thread_id,
            )

    def test_invalid_render_id_format(self, fresh_db):
        # Non-numeric "ci-X" → falls through to raw-id lookup → not found
        a = ContextItem(id="A1", source="x", type="y", label="apple")
        src = _make_thread_with_items(a)
        dst = _make_thread_with_items()
        with pytest.raises(ContextMigrationError):
            migrate_context(
                item_id="ci-not-a-number",
                from_thread_id=src.thread_id,
                to_thread_id=dst.thread_id,
            )
