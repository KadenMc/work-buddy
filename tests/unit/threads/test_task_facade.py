"""Phase-3 facade: ``Task(WorkItem)`` wraps the live task store.

Proves the transitional strangler facade reads through the real
``obsidian/tasks`` task_metadata store (it does not duplicate task
content), and that a Task is a WorkItem sibling of Thread — not a Thread,
not a threads-table citizen.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from work_buddy.obsidian.tasks import store as task_store
from work_buddy.threads.models import Task, Thread
from work_buddy.threads.workitem import WorkItem
from work_buddy.work_item import task_adapter


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch) -> Path:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    db_path = db_dir / "tasks.sqlite"
    monkeypatch.setattr(task_store, "_db_path", lambda: db_path)
    return db_path


def test_task_is_workitem_not_thread():
    task = Task()
    assert isinstance(task, WorkItem)
    assert not isinstance(task, Thread)
    assert task.subtype == "task"
    assert task.thread_id.startswith("t-")
    assert not hasattr(task, "fsm_state")


def test_live_row_returns_the_live_store_row(isolated_store):
    task_store.create("t-facade01", state="focused", urgency="high")
    task = Task(thread_id="t-facade01")
    row = task.live_row()
    assert row is not None
    # The facade returns exactly what the store returns — no caching, no
    # duplication of task content.
    assert row == task_store.get("t-facade01")
    assert row["state"] == "focused"
    assert row["urgency"] == "high"


def test_live_row_none_for_absent_task(isolated_store):
    assert Task(thread_id="t-missing0").live_row() is None


def test_from_store_row_maps_universal_slots(isolated_store):
    task_store.create("t-facade02", state="inbox", urgency="medium")
    row = task_store.get("t-facade02")
    task = Task.from_store_row(row)
    assert task.thread_id == "t-facade02"
    assert task.subtype == "task"
    # created_at carried across from the store row.
    assert task.created_at == row["created_at"]


def test_lineage_via_parent_id(isolated_store):
    # A Task spawned from a Thread records provenance through the
    # inherited WorkItem.parent_id slot (no separate spawned_from field
    # is added in Phase 3).
    parent = Thread()
    task = Task(parent_id=parent.thread_id)
    assert task.parent_id == parent.thread_id


# ----------------------------------------------------------------------
# Write surface — Task.load / Task.create + the instance write methods.
# Each instance method delegates to the task write port keyed on the
# Task's own id; Task.create delegates and returns the raw result dict.
# The delegation tests patch the adapter (not ``mutations``) to assert the
# layering — Task forwards to the port, the port owns the mutation.
# ----------------------------------------------------------------------


def test_load_returns_facade_for_existing_row(isolated_store):
    task_store.create("t-load0001", state="mit", urgency="high")
    task = Task.load("t-load0001")
    assert isinstance(task, Task)
    assert task.thread_id == "t-load0001"


def test_load_none_for_absent(isolated_store):
    assert Task.load("t-missing9") is None


def test_create_delegates_and_returns_result_dict():
    sentinel = {"success": True, "task_id": "t-new00001"}
    with patch.object(task_adapter, "create", return_value=sentinel) as m:
        result = Task.create("draft the paper", urgency="high", project="work-buddy")
    assert result is sentinel
    m.assert_called_once_with(
        "draft the paper",
        urgency="high",
        project="work-buddy",
        due_date=None,
        contract=None,
        summary=None,
        tags=None,
    )


def test_toggle_delegates_keyed_on_thread_id():
    sentinel = {"success": True}
    task = Task(thread_id="t-tog00001")
    with patch.object(task_adapter, "toggle", return_value=sentinel) as m:
        result = task.toggle(done=True)
    assert result is sentinel
    m.assert_called_once_with(
        "t-tog00001", done=True, file_path=None, done_date=None,
    )


def test_update_delegates_keyed_on_thread_id():
    sentinel = {"success": True}
    task = Task(thread_id="t-upd00001")
    with patch.object(task_adapter, "update", return_value=sentinel) as m:
        result = task.update(urgency="low", reason="re-triage")
    assert result is sentinel
    m.assert_called_once_with(
        "t-upd00001",
        state=None,
        urgency="low",
        complexity=None,
        contract=None,
        snooze_until=None,
        due_date=None,
        reason="re-triage",
        file_path=None,
    )


def test_set_description_delegates():
    sentinel = {"success": True}
    task = Task(thread_id="t-desc0001")
    with patch.object(task_adapter, "set_description", return_value=sentinel) as m:
        result = task.set_description("clearer text")
    assert result is sentinel
    m.assert_called_once_with("t-desc0001", "clearer text", file_path=None)


def test_set_tags_delegates():
    sentinel = {"success": True}
    task = Task(thread_id="t-tags0001")
    with patch.object(task_adapter, "set_tags", return_value=sentinel) as m:
        result = task.set_tags(["admin/uhn"])
    assert result is sentinel
    m.assert_called_once_with("t-tags0001", ["admin/uhn"])


def test_delete_delegates():
    sentinel = {"success": True}
    task = Task(thread_id="t-del00001")
    with patch.object(task_adapter, "delete", return_value=sentinel) as m:
        result = task.delete()
    assert result is sentinel
    m.assert_called_once_with("t-del00001")


def test_assign_delegates():
    sentinel = {"success": True}
    task = Task(thread_id="t-asg00001")
    with patch.object(task_adapter, "assign", return_value=sentinel) as m:
        result = task.assign()
    assert result is sentinel
    m.assert_called_once_with("t-asg00001")


# ----------------------------------------------------------------------
# Content-carrying read surface — a Task loaded from the store carries its
# row as a read cache (snapshot), so reads come from it rather than a second
# query. The cache is never authoritative: the store stays the source of
# truth, and mutations invalidate the snapshot rather than writing it back.
# ----------------------------------------------------------------------


def test_load_carries_content_one_query(isolated_store):
    """``load`` reads once; subsequent content reads hit the snapshot, not
    the store."""
    task_store.create("t-carry001", state="focused", urgency="high")
    with patch.object(task_store, "get", wraps=task_store.get) as spy:
        task = Task.load("t-carry001")
        # All three reads come from the carried snapshot.
        assert task.state == "focused"
        assert task.urgency == "high"
        assert task.row["state"] == "focused"
    assert spy.call_count == 1  # the single load read — no per-attribute query


def test_row_returns_a_copy(isolated_store):
    """``.row`` hands back a copy, so a caller mutating it cannot corrupt the
    snapshot (parity with ``store.get``)."""
    task_store.create("t-copy0001", state="inbox")
    task = Task.load("t-copy0001")
    snap = task.row
    snap["state"] = "MUTATED"
    assert task.row["state"] == "inbox"
    assert task.state == "inbox"


def test_accessors_none_for_absent_task(isolated_store):
    task = Task(thread_id="t-absent00")
    assert task.row is None
    assert task.state is None
    assert task.deleted_at is None


def test_accessor_lazy_fetches_for_constructed_task(isolated_store):
    """A Task built by id (not via ``load``) fills its snapshot on first read."""
    task_store.create("t-lazy0001", state="snoozed")
    task = Task(thread_id="t-lazy0001")
    assert task._row is None
    assert task.state == "snoozed"   # lazy fill
    assert task._row is not None     # now cached


def test_refresh_picks_up_external_change(isolated_store):
    """The snapshot is point-in-time; ``refresh`` re-reads current truth."""
    task_store.create("t-refr0001", state="inbox", urgency="low")
    task = Task.load("t-refr0001")
    assert task.urgency == "low"
    task_store.update("t-refr0001", urgency="high", reason="external edit")
    assert task.urgency == "low"     # stale snapshot, by design
    assert task.refresh() is task
    assert task.urgency == "high"    # re-read


def test_load_excludes_soft_deleted_by_default(isolated_store):
    task_store.create("t-delc0001", state="inbox")
    task_store.delete("t-delc0001")  # soft-delete
    assert Task.load("t-delc0001") is None
    revived = Task.load("t-delc0001", include_deleted=True)
    assert revived is not None
    assert revived.deleted_at is not None


def test_mutation_invalidates_snapshot(isolated_store):
    """A mutation invalidates the snapshot so the next read reflects the
    write — without the mutator ever writing the held snapshot back."""
    task_store.create("t-inval001", state="inbox", urgency="low")
    task = Task.load("t-inval001")
    assert task.urgency == "low"  # cached

    def fake_update(task_id, **kwargs):
        # Stand in for the real port→mutations write of a single field.
        task_store.update(task_id, urgency="high", reason="sim")
        return {"success": True}

    with patch.object(task_adapter, "update", side_effect=fake_update):
        task.update(urgency="high", reason="bump")

    assert task.urgency == "high"  # snapshot was invalidated → re-fetched


# ----------------------------------------------------------------------
# Task.query — the collection analogue of load: content-carrying Tasks
# straight from store.query, so iterating them adds no further reads.
# ----------------------------------------------------------------------


def test_query_returns_content_carrying_tasks(isolated_store):
    task_store.create("t-q0000001", state="inbox", urgency="high")
    task_store.create("t-q0000002", state="mit", urgency="low")
    tasks = Task.query()
    assert len(tasks) == 2
    assert all(isinstance(t, Task) for t in tasks)
    # Each Task carries its row from the query result — reads need no get.
    with patch.object(task_store, "get", wraps=task_store.get) as spy:
        rows = [t.row for t in tasks]
        _ = [t.state for t in tasks]
    assert spy.call_count == 0
    assert {r["task_id"] for r in rows} == {"t-q0000001", "t-q0000002"}


def test_query_filters_by_state(isolated_store):
    task_store.create("t-qf000001", state="inbox")
    task_store.create("t-qf000002", state="done")
    done = Task.query(state="done")
    assert [t.thread_id for t in done] == ["t-qf000002"]
