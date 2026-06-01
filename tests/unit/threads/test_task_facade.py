"""Phase-3 facade: ``Task(WorkItem)`` wraps the live task store.

Proves the transitional strangler facade reads through the real
``obsidian/tasks`` task_metadata store (it does not duplicate task
content), and that a Task is a WorkItem sibling of Thread — not a Thread,
not a threads-table citizen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.obsidian.tasks import store as task_store
from work_buddy.threads.models import Task, Thread
from work_buddy.threads.workitem import WorkItem


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
