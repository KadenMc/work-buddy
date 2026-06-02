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
